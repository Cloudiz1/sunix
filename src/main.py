"""sunix — main loop.

Wires the modules together. Runs the recorder.

Architecture:

  pw-record → pump thread → router.current_file (lock-protected)
                                    │
                                    ▼
                          on track-change:
                            finalize previous (close PCM, encode MP3)
                            if ad or already recorded → write to duplicate.pcm
                            else → open new PCM, attach router

The pump runs for the whole session. Each track-change closes the
previous PCM file (atomically, under lock) and either:
  - redirects to duplicate.pcm (skip case: ad or already-recorded), or
  - opens a new PCM and redirects (real new track).
Encoding happens in a background thread per track, so the main loop
can keep going while ffmpeg chews through prior tracks.

Run with: `uv run -m src.main`
"""

from __future__ import annotations

import datetime
import os
import signal
import sys
import tempfile
import threading
from pathlib import Path
from typing import IO, Optional

from . import ads
from . import capture
from . import config as cfg
from . import library
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

    lib = library.Library(out_dir / ".sunix.db")
    log.write("library opened")

    duplicate_path = out_dir / "duplicate.pcm"
    duplicate_path.write_bytes(b"")  # truncate on startup
    duplicate_file = open(duplicate_path, "ab")
    router.swap(duplicate_file)

    state = RecorderState(
        router=router,
        tagger=tagger.Tagger(cfg_obj.spotify),
        library=lib,
        out_dir=out_dir,
        log=log,
        duplicate_path=duplicate_path,
    )

    listener = mpris.MprisListener(
        on_track_change=lambda info: state.on_track_change(info),
        on_disappeared=lambda: log.write("Spotify disappeared; exiting"),
    )

    def sigint(_signum, _frame):
        log.write("SIGINT received; finalizing")
        state.finalize_pending()
        listener.stop()

    signal.signal(signal.SIGINT, sigint)

    log.write("listening for track changes...")
    listener.run()

    state.finalize_pending()
    router.stop()
    capture.stop(cap)
    duplicate_file.close()
    duplicate_path.unlink(missing_ok=True)
    lib.close()
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
    """Owns the current PCM file + the metadata about to be applied.

    Lives on the main thread. The pump thread doesn't touch this.
    """

    def __init__(
        self,
        router: PumpRouter,
        tagger: tagger.Tagger,
        library: library.Library,
        out_dir: Path,
        log: "SessionLog",
        duplicate_path: Path,
    ) -> None:
        self._router = router
        self._tagger = tagger
        self._library = library
        self._out_dir = out_dir
        self._log = log
        self._duplicate_path = duplicate_path

        # The PCM file currently receiving audio for the current track.
        self._current_path: Optional[Path] = None
        self._current_meta: Optional[tagger.TrackMetadata] = None
        self._current_cover: Optional[Path] = None
        self._current_id: Optional[str] = None

    def on_track_change(self, info: mpris.TrackInfo) -> None:
        self._log.write(
            f"track change: trackid={info.trackid} artist={info.artist!r} title={info.title!r}"
        )

        # Always finalize the previous track first, regardless of what
        # we'll do with the new one.
        self.finalize_pending()

        # Ad: redirect pump to duplicate.pcm, don't open new PCM.
        if ads.is_ad({"trackid": info.trackid}):
            self._attach_duplicate()
            self._log.write("  -> ad, writing to duplicate.pcm")
            return

        bare_id = tagger.parse_spotify_id(info.trackid)
        if not bare_id:
            # Unparseable; treat like ad.
            self._attach_duplicate()
            self._log.write("  -> unparseable trackid, writing to duplicate.pcm")
            return

        if self._library.has(bare_id):
            self._attach_duplicate()
            self._log.write(f"  -> already recorded, writing to duplicate.pcm")
            return

        # Real new track: look up metadata, open PCM, attach.
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

        cover_path: Optional[Path] = None
        if meta.cover_jpg:
            fd, cover_name = tempfile.mkstemp(suffix=".jpg", prefix="sunix-cover-")
            with os.fdopen(fd, "wb") as cf:
                cf.write(meta.cover_jpg)
            cover_path = Path(cover_name)

        fd, pcm_name = tempfile.mkstemp(suffix=".pcm", prefix="sunix-")
        pcm_file = os.fdopen(fd, "wb")
        pcm_path = Path(pcm_name)

        self._router.swap(pcm_file)

        self._current_path = pcm_path
        self._current_meta = meta
        self._current_cover = cover_path
        self._current_id = bare_id

        self._log.write(f"  -> recording: {meta.artist} - {meta.title}")

    def finalize_pending(self) -> None:
        """Close the current track's PCM file (if any) and hand it to the encoder."""
        old_file = self._router.swap(None)
        # swap() already closed old_file.

        pcm_path = self._current_path
        meta = self._current_meta
        cover_path = self._current_cover
        bare_id = self._current_id

        self._current_path = None
        self._current_meta = None
        self._current_cover = None
        self._current_id = None

        if pcm_path is None or old_file is None:
            return

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
        if bare_id is not None:
            self._library.add(bare_id)
        self._log.write(f"  -> encoding: {final_path.name}")

    def _attach_duplicate(self) -> None:
        """Redirect the pump to duplicate.pcm for unwanted audio."""
        # Append mode so we don't truncate whatever's already there.
        # On exit we delete the file, so it doesn't accumulate.
        dup_file = open(self._duplicate_path, "ab")
        self._router.swap(dup_file)


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