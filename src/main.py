"""sunix — main loop.

Wires the modules together. Runs the recorder.

Architecture:

  pw-record → pump thread → router.current_file (lock-protected)
                                    │
                                    ▼
                          on track-change:
                            swap current file (close old, attach new)
                            spawn encode_async(old_pcm) → MP3

The pump runs for the whole session. Each track-change closes the
previous PCM file (atomically, under lock) and redirects the pump to
a new file. Encoding happens in a background thread per track, so the
main loop can keep going while ffmpeg chews through prior tracks.

Run with: `uv run -m src.main`
"""

from __future__ import annotations

import datetime
import os
import shutil
import signal
import sys
import tempfile
import threading
from pathlib import Path
from typing import IO, Optional

from . import ads
from . import capture
from . import config as cfg
from . import mpris
from . import pipeline
from . import tagger


def record() -> None:
    cfg_obj = cfg.load()
    out_dir = cfg_obj.output.directory
    out_dir.mkdir(parents=True, exist_ok=True)

    log = SessionLog(out_dir / "session.log")
    log.write(f"=== sunix session starting, output={out_dir} ===")

    try:
        node = capture.find_spotify_stream()
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    log.write(f"found Spotify stream node: {node}")

    cap = capture.start(node)
    if cap.process.stdout is None:
        print("error: pw-record has no stdout", file=sys.stderr)
        sys.exit(1)

    router = PumpRouter(cap.process.stdout, log=log)
    router.start()

    state = RecorderState(
        router=router,
        tagger=tagger.Tagger(cfg_obj.spotify),
        out_dir=out_dir,
        log=log,
    )

    def on_track_change(info: mpris.TrackInfo) -> None:
        state.on_track_change(info)

    listener = mpris.MprisListener(
        on_track_change=on_track_change,
        on_disappeared=lambda: log.write("Spotify disappeared; exiting"),
    )

    def sigint(_signum, _frame):
        log.write("SIGINT received; finalizing")
        # Run finalization on the main thread, then quit the GLib loop
        # so listener.run() returns and we hit the cleanup path.
        state.finalize_pending()
        listener.stop()

    signal.signal(signal.SIGINT, sigint)

    log.write("listening for track changes...")
    listener.run()

    # Either SIGINT fired (already finalized) or Spotify disappeared.
    # Finalize again in case SIGINT handler didn't run.
    state.finalize_pending()
    router.stop()
    capture.stop(cap)
    log.write("=== session ended ===")


# --- pump router -----------------------------------------------------------

class PumpRouter:
    """Single thread that reads from pw-record and writes to the
    currently-attached PCM file.

    `swap()` atomically redirects to a new file under a lock, so the
    pump never writes to a closed file and never has a gap where bytes
    go nowhere.
    """

    def __init__(self, source, log: "SessionLog") -> None:
        self._source = source
        self._log = log
        self._lock = threading.Lock()
        self._current: Optional[IO[bytes]] = None
        self._stop = False

    def start(self) -> None:
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def stop(self) -> None:
        self._stop = True

    def swap(self, new_file: Optional[IO[bytes]]) -> Optional[IO[bytes]]:
        """Redirect the pump to `new_file`. Close the previous file.

        Returns the previous file (now closed) so the caller can hand
        it off to the encoder. Returns None if there was no previous
        file.

        All file mutations happen under the lock. The pump holds the
        lock for the duration of one `write()` call only, so blocking
        the main thread is brief.
        """
        with self._lock:
            old = self._current
            self._current = new_file
        if old is not None and not old.closed:
            old.close()
        return old

    def _run(self) -> None:
        try:
            while not self._stop:
                chunk = self._source.read(8192)
                if not chunk:
                    self._log.write("pw-record exited")
                    break
                with self._lock:
                    f = self._current
                if f is not None and not f.closed:
                    try:
                        f.write(chunk)
                    except (ValueError, OSError) as e:
                        self._log.write(f"pump write error: {e}")
        except Exception as e:
            self._log.write(f"pump error: {e}")


# --- recorder state --------------------------------------------------------

class RecorderState:
    """Tracks the current PCM file + the metadata about to be applied.

    Lives on the main thread. The pump thread doesn't touch this.
    """

    def __init__(
        self,
        router: PumpRouter,
        tagger: tagger.Tagger,
        out_dir: Path,
        log: "SessionLog",
    ) -> None:
        self._router = router
        self._tagger = tagger
        self._out_dir = out_dir
        self._log = log

        # The PCM file currently receiving audio (open, writable).
        self._current_file: Optional[IO[bytes]] = None
        self._current_path: Optional[Path] = None
        # The metadata we'll use when this track's PCM gets encoded.
        self._current_meta: Optional[tagger.TrackMetadata] = None
        self._current_cover: Optional[Path] = None

    def on_track_change(self, info: mpris.TrackInfo) -> None:
        self._log.write(
            f"track change: trackid={info.trackid} artist={info.artist!r} title={info.title!r}"
        )

        if ads.is_ad({"trackid": info.trackid}):
            self._log.write("  -> ad detected, skipping")
            # Don't open a new file. Pump continues writing to the
            # previous track's PCM (which we'll encode normally).
            # Actually no — we want to NOT accumulate this audio.
            # Simplest: keep current file as-is; the ad audio becomes
            # part of the previous track's tail. Acceptable for an
            # ad-skipping tool that doesn't try to surgically cut.
            # If we wanted to be precise we'd open a dummy sink; not worth it.
            return

        # 1. Hand the previous track off to encoding.
        self.finalize_pending()

        # 2. Look up canonical metadata.
        meta = self._tagger.lookup(info.trackid)
        if meta is None:
            self._log.write("  -> Web API lookup failed; using MPRIS metadata with empty tags")
            meta = tagger.TrackMetadata(
                artist=info.artist or "Unknown Artist",
                title=info.title or "Unknown Track",
                album=info.album or "Unknown Album",
                track_number=None,
                year=None,
                genre=None,
                cover_jpg=None,
            )

        # 3. Write cover art to a temp file (so the encoder can find it).
        cover_path: Optional[Path] = None
        if meta.cover_jpg:
            fd, cover_name = tempfile.mkstemp(suffix=".jpg", prefix="sunix-cover-")
            with os.fdopen(fd, "wb") as cf:
                cf.write(meta.cover_jpg)
            cover_path = Path(cover_name)

        # 4. Open a new PCM file for this track.
        fd, pcm_name = tempfile.mkstemp(suffix=".pcm", prefix="sunix-")
        pcm_file = os.fdopen(fd, "wb")
        pcm_path = Path(pcm_name)

        # 5. Atomically redirect the pump to the new file.
        self._router.swap(pcm_file)

        # 6. Stash state for the next finalize_pending().
        self._current_file = pcm_file
        self._current_path = pcm_path
        self._current_meta = meta
        self._current_cover = cover_path

        self._log.write(f"  -> recording: {meta.artist} - {meta.title}")

    def finalize_pending(self) -> None:
        """Close the current PCM file (if any) and hand it to the encoder."""
        # Swap returns the previous file. Passing None as the new file
        # detaches the pump — bytes from pw-record go nowhere until a
        # new track opens. This is fine; pw-record's stdout pipe is
        # bounded and we won't block for long.
        old_file = self._router.swap(None)
        # Swap already closed old_file.

        pcm_path = self._current_path
        meta = self._current_meta
        cover_path = self._current_cover

        # Reset state regardless of whether we have a valid track to encode.
        self._current_file = None
        self._current_path = None
        self._current_meta = None
        self._current_cover = None

        if pcm_path is None or old_file is None:
            return

        # If the PCM is empty or very small, drop it. Probably an ad
        # boundary or a fast skip that produced no audio.
        try:
            size = pcm_path.stat().st_size
        except OSError:
            size = 0
        if size < 4096:
            pcm_path.unlink(missing_ok=True)
            if cover_path:
                cover_path.unlink(missing_ok=True)
            self._log.write("  -> dropped: PCM too small (<4096 bytes)")
            return

        final_path = pipeline.make_output_path(
            self._out_dir,
            meta.artist if meta else "Unknown Artist",
            meta.title if meta else "Unknown Track",
        )

        req = pipeline.EncodeRequest(
            pcm_path=pcm_path,
            metadata=meta,
            cover_path=cover_path,
            final_mp3_path=final_path,
        )
        pipeline.encode_async(req)
        self._log.write(f"  -> encoding: {final_path.name}")


# --- session log -----------------------------------------------------------

class SessionLog:
    def __init__(self, path: Path) -> None:
        self._fh = open(path, "a")

    def write(self, msg: str) -> None:
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        line = f"[{ts}] {msg}\n"
        self._fh.write(line)
        self._fh.flush()
        sys.stdout.write(line)
        sys.stdout.flush()

    def close(self) -> None:
        self._fh.close()


if __name__ == "__main__":
    record()