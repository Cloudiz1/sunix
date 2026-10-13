# sunix

Personal Spotify recorder for Linux. Captures songs from the official Spotify client as they play, splits them into per-track MP3s, skips ads, and tags files with metadata + cover art from the Spotify Web API.

## Setup

System packages (Arch):

```bash
sudo pacman -S --needed pipewire ffmpeg python-dbus python-gobject
```

The venv must see system Python packages (for `dbus` and `gi`):

```bash
uv venv --system-site-packages --python 3.14
```

Spotify Web API credentials:

1. Go to <https://developer.spotify.com/dashboard> and create an app.
2. Copy `client_id` and `client_secret` into `config.toml`.

## Run

```bash
uv run -m src.main
```

Make sure Spotify is running and playing audio before you start.

Files are saved to `./recordings/` with names like `Artist - Track.mp3`. Logs go to `./recordings/session.log`.

To run tests:

```bash
uv run -m src.test_pipeline
uv run -m src.test_library
```

## How it works

Short version:

- `capture.py` finds Spotify's PipeWire stream node via `pw-cli` and spawns `pw-record`.
- `mpris.py` subscribes to D-Bus `PropertiesChanged` signals from Spotify.
- `tagger.py` looks up canonical metadata + cover art via the Spotify Web API (client credentials flow).
- `pipeline.py` runs `ffmpeg` per file to encode a track's PCM bytes into MP3.
- `ads.py` is a one-line check on `mpris:trackid` to skip ads.
- `library.py` tracks which Spotify track IDs have been recorded, so sunix skips duplicates on subsequent runs.
- `main.py` wires them together.

## Notes

- Spotify Free caps at 160 kbps; sunix encodes at 160 kbps MP3 to match. Premium streams 320 kbps, which sunix also matches automatically.
- "Pause" and "Stopped" playback states are not track boundaries — only metadata changes are.
- If Spotify disappears mid-session (crash, lid close, network drop), sunix finalizes the current track and exits cleanly.
- If the Web API lookup fails or 404s, files are still saved with `Artist - Track.mp3` filenames from MPRIS, but ID3 tags are left empty.
- **Dedup**: `recordings/.sunix.db` (SQLite) holds the set of recorded Spotify track IDs. On track-change, if the current track's ID is already in the DB, sunix silently writes the audio to `recordings/duplicate.pcm` (deleted on exit) and doesn't produce an MP3. To reset and re-record everything: `rm recordings/.sunix.db`.