"""Integration tests for the pipeline.

Generates synthetic PCM, runs it through pipeline.encode(), and checks
that the resulting MP3 has the expected tags and duration.
"""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import pipeline
from . import tagger


SAMPLE_RATE = 44100
CHANNELS = 2
SECONDS = 2


def make_sine_pcm() -> bytes:
    """2 seconds of 440Hz tone, s16le 44.1kHz stereo."""
    frames = SAMPLE_RATE * SECONDS
    out = bytearray()
    for i in range(frames):
        v = int(0.3 * 32767 * math.sin(2 * math.pi * 440 * i / SAMPLE_RATE))
        out.extend(struct.pack("<hh", v, v))
    return bytes(out)


def write_pcm_file(path: Path) -> Path:
    """Write a sine wave PCM file, return its path."""
    path.write_bytes(make_sine_pcm())
    return path


def test_encode_runs(out_dir: Path) -> Path:
    """encode() should produce a valid MP3 with ID3 tags."""
    if not shutil.which("ffmpeg"):
        print("SKIP: ffmpeg not in PATH")
        sys.exit(0)

    out_dir.mkdir(parents=True, exist_ok=True)

    pcm = write_pcm_file(out_dir / "sine.pcm")
    final_mp3 = pipeline.make_output_path(out_dir, "Test Artist", "Test Track")

    req = pipeline.EncodeRequest(
        pcm_path=pcm,
        metadata=tagger.TrackMetadata(
            artist="Test Artist",
            title="Test Track",
            album="Test Album",
            track_number=3,
            year="2024",
            genre="rock",
            cover_jpg=None,
        ),
        cover_path=None,
        final_mp3_path=final_mp3,
    )

    pipeline.encode(req)

    assert final_mp3.exists(), f"output file {final_mp3} doesn't exist"
    assert final_mp3.stat().st_size > 0
    assert not pcm.exists(), "PCM temp file should be cleaned up"

    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1", str(final_mp3)],
        capture_output=True, text=True, check=True,
    )
    print(f"  ffprobe output: {proc.stdout.strip()}")

    data = final_mp3.read_bytes()
    for needle in (b"Test Artist", b"Test Track", b"Test Album", b"rock"):
        assert needle in data, f"missing tag fragment: {needle!r}"

    print(f"  OK: {final_mp3.name}")
    return final_mp3


def test_encode_async_runs(out_dir: Path) -> Path:
    """encode_async() should encode in a background thread."""
    if not shutil.which("ffmpeg"):
        print("SKIP: ffmpeg not in PATH")
        sys.exit(0)

    out_dir.mkdir(parents=True, exist_ok=True)

    pcm = write_pcm_file(out_dir / "sine.pcm")
    final_mp3 = pipeline.make_output_path(out_dir, "Async", "Test")

    req = pipeline.EncodeRequest(
        pcm_path=pcm,
        metadata=tagger.TrackMetadata(
            artist="Async",
            title="Test",
            album="Async Album",
            track_number=None,
            year=None,
            genre=None,
            cover_jpg=None,
        ),
        cover_path=None,
        final_mp3_path=final_mp3,
    )

    t = pipeline.encode_async(req)
    t.join(timeout=30)
    assert not t.is_alive(), "encode thread didn't finish"

    assert final_mp3.exists()
    print(f"  OK: {final_mp3.name}")
    return final_mp3


def test_collision_suffix(tmp: Path) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    a = pipeline.make_output_path(tmp, "Artist", "Track")
    a.touch()
    b = pipeline.make_output_path(tmp, "Artist", "Track")
    assert b != a
    assert b.name.endswith("(2).mp3")
    print(f"  OK: collision gave {b.name}")


def test_make_output_path_safe() -> None:
    p = pipeline.make_output_path(Path("/tmp"), "../etc/passwd", "foo/bar")
    assert "/" not in p.name
    print(f"  OK: safe path: {p.name}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="sunix-test-"))
    print(f"using tmp dir: {tmp}")
    try:
        print("test_encode_runs:")
        test_encode_runs(tmp / "encode1")
        print("test_encode_async_runs:")
        test_encode_async_runs(tmp / "encode2")
        print("test_collision_suffix:")
        test_collision_suffix(tmp / "collide")
        print("test_make_output_path_safe:")
        test_make_output_path_safe()
        print("=== ALL TESTS PASSED ===")
        return 0
    except AssertionError as e:
        print(f"FAIL: {e}")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())