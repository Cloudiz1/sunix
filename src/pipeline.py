"""Per-track encoding.

Takes a closed PCM file (raw s16le 44.1kHz stereo) plus optional metadata
and cover art, and produces a final MP3 in the output directory.

Each track's encoding runs in its own thread, decoupled from the live
audio pump. By the time we get here, the PCM file is fully written
and closed — ffmpeg treats it as a static input.

`make_output_path` handles filename collisions: "Artist - Track.mp3",
then "(2)", "(3)", etc.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from .tagger import TrackMetadata


@dataclass(frozen=True)
class EncodeRequest:
    """Inputs for one MP3 encode."""
    pcm_path: Path
    metadata: TrackMetadata | None
    cover_path: Path | None
    final_mp3_path: Path
    bitrate: str = "160k"


def make_output_path(out_dir: Path, artist: str, title: str) -> Path:
    """`Artist - Track.mp3`, with `(2)`, `(3)` suffixes on conflict."""
    def safe(s: str) -> str:
        return s.replace("/", "-").replace("\0", "").strip() or "Unknown"

    base = out_dir / f"{safe(artist)} - {safe(title)}.mp3"

    if not base.exists():
        return base

    stem = base.stem
    suffix = base.suffix
    for n in range(2, 1000):
        candidate = out_dir / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
    return base


def encode(req: EncodeRequest) -> None:
    """Run ffmpeg on the closed PCM file → final MP3 path.

    Blocks until ffmpeg exits. On success, deletes the PCM temp file
    and (if present) the cover art temp file. On failure, deletes
    any partial output and re-raises.
    """
    if not shutil.which("ffmpeg"):
        print("error: ffmpeg not found in PATH", file=sys.stderr)
        sys.exit(1)

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        # Input 0: PCM s16le 44.1kHz stereo.
        "-f", "s16le",
        "-ar", "44100",
        "-ac", "2",
        "-i", str(req.pcm_path),
    ]

    if req.cover_path:
        cmd += ["-i", str(req.cover_path)]

    cmd += ["-map", "0:a:0"]
    if req.cover_path:
        cmd += [
            "-map", "1:v:0",
            "-c:v", "copy",
            "-disposition:v:0", "attached_pic",
        ]

    cmd += ["-c:a", "libmp3lame", "-b:a", req.bitrate]

    if req.metadata:
        m = req.metadata
        cmd += ["-metadata", f"artist={m.artist}"]
        cmd += ["-metadata", f"title={m.title}"]
        cmd += ["-metadata", f"album={m.album}"]
        if m.track_number is not None:
            cmd += ["-metadata", f"track={m.track_number}"]
        if m.year:
            cmd += ["-metadata", f"date={m.year}"]
        if m.genre:
            cmd += ["-metadata", f"genre={m.genre}"]

    cmd += ["-f", "mp3", str(req.final_mp3_path)]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        # Clean up any partial output.
        req.final_mp3_path.unlink(missing_ok=True)
        stderr = e.stderr.decode(errors="replace") if e.stderr else ""
        print(f"ffmpeg failed for {req.final_mp3_path.name}: {stderr}", file=sys.stderr)
        return

    # Clean up temps. PCM is consumed; cover was embedded into the MP3.
    req.pcm_path.unlink(missing_ok=True)
    if req.cover_path:
        req.cover_path.unlink(missing_ok=True)


def encode_async(req: EncodeRequest) -> threading.Thread:
    """Spawn `encode()` in a daemon thread. Returns the thread."""
    t = threading.Thread(target=encode, args=(req,), daemon=True)
    t.start()
    return t