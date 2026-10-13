"""Library: tracks which Spotify tracks have already been recorded.

Single-table SQLite database at `recordings/.sunix.db`. Just a set
membership check — given a Spotify track id, has sunix already saved
this song?

The DB file can be deleted (`rm recordings/.sunix.db`) to reset
sunix's memory; nothing else depends on it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


class Library:
    def __init__(self, db_path: Path) -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS recorded (spotify_id TEXT PRIMARY KEY)"
        )
        self._conn.commit()

    def has(self, spotify_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM recorded WHERE spotify_id = ? LIMIT 1",
            (spotify_id,),
        )
        return cur.fetchone() is not None

    def add(self, spotify_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO recorded (spotify_id) VALUES (?)",
            (spotify_id,),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()