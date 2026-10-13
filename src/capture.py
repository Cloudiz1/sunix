"""PipeWire audio capture.

Finds Spotify's audio stream node and spawns `pw-record` to dump it
as raw PCM on stdout.

On modern PipeWire setups, Spotify doesn't own its own sink — it
creates a `Stream/Output/Audio` node (with `node.name = "spotify"` and
`application.name = "Spotify"`) and PipeWire routes that stream into
whichever sink the user has selected. We discover the stream node via
`pw-cli ls Node` and capture from it directly. `pw-record --target`
accepts a node name and links to the node without needing a sink
monitor indirection.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class Capture:
    """A running pw-record subprocess.

    raw_rate / raw_channels describe the PCM format on stdout so the
    pipeline can read it correctly.
    """
    process: subprocess.Popen
    raw_rate: int
    raw_channels: int


def find_spotify_stream() -> str:
    """Return the node name of Spotify's audio output stream.

    Looks for a PipeWire node with `application.name = "Spotify"` and
    `media.class = "Stream/Output/Audio"`. Returns its `node.name`,
    which `pw-record --target` accepts.

    Raises RuntimeError if Spotify isn't running or isn't currently
    playing audio (no stream node exists while paused or stopped).
    """
    if not shutil.which("pw-cli"):
        raise RuntimeError("pw-cli not found in PATH; install pipewire")

    try:
        out = subprocess.check_output(
            ["pw-cli", "ls", "Node"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"pw-cli failed: {e.stderr or e}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("pw-cli timed out") from e

    # pw-cli prints one node per record. Each record starts with a
    # line of the form "\tid <N>, type PipeWire:Interface:Node/3" and
    # continues with property lines until the next "id" line.
    current: dict[str, str] = {}
    for line in out.splitlines():
        stripped = line.strip()
        # New record: \tid <N>, type PipeWire:Interface:Node/3
        if stripped.startswith("id ") and "type PipeWire:Interface:Node" in stripped:
            if _is_spotify_stream(current):
                name = current.get("node.name")
                if name:
                    return name
            current = {}
            continue

        if not stripped or "=" not in stripped:
            continue

        key, _, rhs = stripped.partition("=")
        key = key.strip()
        rhs = rhs.strip().strip('"')
        current[key] = rhs

    # Check the last record too.
    if _is_spotify_stream(current):
        name = current.get("node.name")
        if name:
            return name

    raise RuntimeError(
        "Spotify's audio stream not found. Is Spotify running and playing audio?"
    )


def _is_spotify_stream(props: dict[str, str]) -> bool:
    return (
        props.get("application.name") == "Spotify"
        and props.get("media.class") == "Stream/Output/Audio"
    )


def start(node_name: str) -> Capture:
    """Spawn pw-record targeting Spotify's stream node."""
    cmd = [
        "pw-record",
        "--target", node_name,
        "--rate", "44100",
        "--channels", "2",
        "--format", "s16",
        "-",  # write to stdout
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        print("error: pw-record not found in PATH", file=sys.stderr)
        print("install with: sudo pacman -S pipewire", file=sys.stderr)
        sys.exit(1)

    return Capture(
        process=proc,
        raw_rate=44100,
        raw_channels=2,
    )


def stop(cap: Capture) -> None:
    """Stop pw-record cleanly."""
    if cap.process.poll() is None:
        cap.process.terminate()
        try:
            cap.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cap.process.kill()