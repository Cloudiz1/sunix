"""Ad detection.

Pure functions, no I/O. The only signal we trust is `mpris:trackid`
starting with `spotify:ad:`. Anything else risks dropping a real song.
"""

from __future__ import annotations


def is_ad(metadata: dict | None) -> bool:
    """Return True if the given MPRIS metadata describes an ad.

    Spotify injects audio ads as regular player entries whose trackid
    follows the `spotify:ad:` convention. Anything else is music.
    """
    if not metadata:
        return False
    trackid = metadata.get("trackid") or ""
    return trackid.startswith("spotify:ad:")