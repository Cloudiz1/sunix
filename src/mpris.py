"""MPRIS client for Spotify.

Subscribes to `PropertiesChanged` signals on
`org.mpris.MediaPlayer2.spotify` and exposes them as Python callbacks.

We use `dbus-python` (the official binding, ships with Arch's
python-dbus package). GLib is needed for the main loop that receives
signal messages.

D-Bus interface contract (excerpted from the MPRIS spec):

    org.mpris.MediaPlayer2.spotify /org/mpris/MediaPlayer2
        org.freedesktop.DBus.Properties.PropertiesChanged(dict, list)
            -> emitted whenever metadata or playback state changes

    org.mpris.MediaPlayer2.Player.Metadata -> dict
    org.mpris.MediaPlayer2.Player.PlaybackStatus -> "Playing" | "Paused" | "Stopped"
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib


SPOTIFY_BUS_NAME = "org.mpris.MediaPlayer2.spotify"
SPOTIFY_OBJECT_PATH = "/org/mpris/MediaPlayer2"
PLAYER_INTERFACE = "org.mpris.MediaPlayer2.Player"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"


@dataclass
class TrackInfo:
    trackid: str = ""
    artist: str = ""
    title: str = ""
    album: str = ""
    length_us: int = 0


class MprisListener:
    """A blocking D-Bus connection subscribed to PropertiesChanged.

    The .on_track_change callback fires with a TrackInfo whenever
    Spotify's `Metadata` property changes.

    Use .run() to block until Spotify disappears from the bus, or
    KeyboardInterrupt.
    """

    def __init__(self, on_track_change: Callable[[TrackInfo], None], on_disappeared: Callable[[], None] | None = None) -> None:
        self.on_track_change = on_track_change
        self.on_disappeared = on_disappeared or (lambda: None)
        self._loop: GLib.MainLoop | None = None
        self._last_trackid: str = ""

    def run(self) -> None:
        # Required to receive signals on the default bus.
        DBusGMainLoop(set_as_default=True)
        bus = dbus.SessionBus()

        # Verify Spotify is on the bus by trying to fetch Metadata.
        try:
            proxy = bus.get_object(SPOTIFY_BUS_NAME, SPOTIFY_OBJECT_PATH)
            props_iface = dbus.Interface(proxy, PROPERTIES_INTERFACE)
            current = props_iface.Get(PLAYER_INTERFACE, "Metadata")
            self._last_trackid = _trackid_from(current)
        except dbus.DBusException as e:
            print(f"mpris: spotify not on bus: {e}", file=sys.stderr)
            raise _SpotifyGone(e)

        # Subscribe to PropertiesChanged signals.
        bus.add_signal_receiver(
            self._on_properties_changed,
            signal_name="PropertiesChanged",
            dbus_interface=PROPERTIES_INTERFACE,
            bus_name=SPOTIFY_BUS_NAME,
            path=SPOTIFY_OBJECT_PATH,
        )

        # Watch for the bus name disappearing.
        bus.watch_name_owner(
            SPOTIFY_BUS_NAME,
            self._on_name_owner_changed,
        )

        self._loop = GLib.MainLoop()
        try:
            self._loop.run()
        except KeyboardInterrupt:
            return

    def stop(self) -> None:
        if self._loop is not None and self._loop.is_running():
            self._loop.quit()

    # --- signal handlers ---------------------------------------------------

    def _on_properties_changed(self, *args, **kwargs) -> None:
        """PropertiesChanged signal from the Properties interface.

        dbus-python passes the signal body as positional args:
            (interface_name: str, changed: dict, invalidated: list)
        Some signals may include extra args (e.g. Seeked has just one
        numeric arg); we ignore those by signature.
        """
        if len(args) < 2:
            return
        interface = args[0]
        changed = args[1]
        # We only care about changes to the Player interface.
        if str(interface) != PLAYER_INTERFACE:
            return

        metadata = changed.get("Metadata")
        if metadata is None:
            return

        info = _parse_metadata(metadata)
        if info.trackid and info.trackid != self._last_trackid:
            self._last_trackid = info.trackid
            self.on_track_change(info)

    def _on_name_owner_changed(self, new_owner: str) -> None:
        # dbus-python's watch_name_owner calls back with just the new
        # owner string. Empty string means Spotify has no owner anymore.
        if new_owner == "":
            self.on_disappeared()
            self.stop()


class _SpotifyGone(Exception):
    pass


# --- helpers ---------------------------------------------------------------

def _trackid_from(metadata: dict) -> str:
    """`mpris:trackid` looks like `/com/spotify/track/XXXX`."""
    tid = metadata.get("mpris:trackid", "")
    return str(tid)


def _parse_metadata(metadata: dict) -> TrackInfo:
    artists = metadata.get("xesam:artist", []) or []
    artist = ", ".join(str(a) for a in artists) if artists else ""
    return TrackInfo(
        trackid=_trackid_from(metadata),
        artist=artist,
        title=str(metadata.get("xesam:title", "")),
        album=str(metadata.get("xesam:album", "")),
        length_us=int(metadata.get("mpris:length", 0)),
    )