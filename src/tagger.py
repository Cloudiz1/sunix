"""Spotify Web API client.

Uses client credentials flow (no user OAuth). Caches the bearer token
until it expires. Exposes a single lookup() method that returns merged
metadata + cover art bytes for a given track id.

We don't use the `spotipy` library; the API surface we need is small
enough that stdlib `urllib` is cleaner than another dependency.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import SpotifyConfig


TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"


@dataclass(frozen=True)
class TrackMetadata:
    """Everything we know about one track, ready to embed in an MP3."""
    artist: str
    title: str
    album: str
    track_number: int | None
    year: str | None        # release_date is sometimes "YYYY", sometimes "YYYY-MM-DD"
    genre: str | None
    cover_jpg: bytes | None  # 300x300 JPG, or None if unavailable


def parse_spotify_id(trackid: str) -> str:
    """Extract the bare Spotify track id from any of:
      - `spotify:track:XXXX` (Spotify URI form)
      - `/com/spotify/track/XXXX` (MPRIS form, D-Bus object path)
      - just `XXXX`

    Returns "" if the input doesn't contain a recognizable id.
    """
    for sep in ("/", ":"):
        if sep in trackid:
            trackid = trackid.rsplit(sep, 1)[-1]
            break
    return trackid or ""


class Tagger:
    def __init__(self, cfg: SpotifyConfig) -> None:
        self._cfg = cfg
        self._token: str | None = None
        self._token_expiry: float = 0.0

    def lookup(self, trackid: str) -> TrackMetadata | None:
        """Fetch canonical metadata + cover art for a Spotify track id.

        Returns None on any error; callers should fall back to MPRIS metadata.
        """
        trackid = parse_spotify_id(trackid)
        if not trackid:
            return None

        try:
            data = self._get_json(f"{API_BASE}/tracks/{trackid}")
            cover = self._fetch_cover(data.get("album", {}).get("images", []))
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, KeyError):
            return None

        return self._parse(data, cover)

    def _parse(self, data: dict[str, Any], cover: bytes | None) -> TrackMetadata:
        artists = [a["name"] for a in data.get("artists", []) if "name" in a]
        album_obj = data.get("album", {})

        # Genres live on the artist object, not on /tracks.
        genre: str | None = None
        if artists:
            artist_id = data["artists"][0].get("id")
            if artist_id:
                try:
                    artist_data = self._get_json(f"{API_BASE}/artists/{artist_id}")
                    genres = artist_data.get("genres", [])
                    genre = ", ".join(genres) if genres else None
                except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
                    pass

        release = album_obj.get("release_date") or None
        year = release[:4] if release else None

        return TrackMetadata(
            artist=", ".join(artists) or "Unknown Artist",
            title=data.get("name") or "Unknown Track",
            album=album_obj.get("name") or "Unknown Album",
            track_number=data.get("track_number"),
            year=year,
            genre=genre,
            cover_jpg=cover,
        )

    def _fetch_cover(self, images: list[dict]) -> bytes | None:
        """Pick the 300x300 cover from the images array, or the closest match."""
        if not images:
            return None

        target = 300 * 300
        best = min(images, key=lambda img: abs((img.get("width", 0) * img.get("height", 0)) - target))
        url = best.get("url")
        if not url:
            return None

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "sunix/0.1"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError):
            return None

    # --- token plumbing -----------------------------------------------------

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry:
            return self._token

        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
        }).encode()

        auth = f"{self._cfg.client_id}:{self._cfg.client_secret}".encode()
        b64 = base64.b64encode(auth).decode()

        req = urllib.request.Request(
            TOKEN_URL,
            data=body,
            headers={
                "Authorization": f"Basic {b64}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        self._token = data["access_token"]
        # Spotify tokens last 3600s; refresh a minute early.
        self._token_expiry = time.time() + data.get("expires_in", 3600) - 60
        return self._token

    def _get_json(self, url: str) -> dict:
        token = self._get_token()
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())