"""Configuration loader.

Reads `config.toml` from the project root. Two sections: [spotify]
for Web API credentials, [output] for the recordings directory.
"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).parent.parent.resolve()
CONFIG_PATH = PROJECT_ROOT / "config.toml"


@dataclass(frozen=True)
class SpotifyConfig:
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class OutputConfig:
    directory: Path


@dataclass(frozen=True)
class Config:
    spotify: SpotifyConfig
    output: OutputConfig


def load(path: Path = CONFIG_PATH) -> Config:
    if not path.exists():
        print(f"error: config.toml not found at {path}", file=sys.stderr)
        print("create it with [spotify] client_id/client_secret and [output] directory", file=sys.stderr)
        sys.exit(1)

    with path.open("rb") as f:
        raw = tomllib.load(f)

    try:
        spotify = SpotifyConfig(
            client_id=raw["spotify"]["client_id"],
            client_secret=raw["spotify"]["client_secret"],
        )
    except KeyError as e:
        print(f"error: missing config key {e}", file=sys.stderr)
        sys.exit(1)

    output = OutputConfig(
        directory=Path(raw.get("output", {}).get("directory", "./recordings")).resolve(),
    )

    return Config(spotify=spotify, output=output)