"""Regression tests: Sonos metadata must stay silent during radio playback.

Kitchen bug (Aug 2026): after a radio play, the monitor's retry broadcast
pushed the stream's ICY title (no artwork) once the 3s suppress window
passed, clobbering the radio source's programme title + SR artwork in the
router.  Fix: the persistent _radio_playback flag — while the current track
is our radio stream, fetch_media_data deliberately returns None and the
monitor never arms a retry.  Sonos rewrites stream URIs (x-rincon-mp3radio://
→ aac://…), so recognition is by scheme family + flag, never by URL match.
"""

from __future__ import annotations

import asyncio
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from test_sonos_external_start import _install_fake_soco

# The URI Sonos actually reported on Kitchen while playing
# x-rincon-mp3radio://live1.sr.se/p1-aac-320:
REWRITTEN_RADIO_URI = "aac://http://edge2.sr.se/p1-aac-320"
SPOTIFY_URI = "x-sonos-spotify:spotify%3atrack%3aabc123?sid=9"


@pytest.fixture
def radio_player():
    """MediaServer with a stubbed viewer and REAL fetch_media_data."""
    _install_fake_soco()
    from players.sonos import MediaServer
    p = MediaServer()
    p.sonos_viewer = MagicMock()
    p.broadcast_media_update = AsyncMock()
    p.trigger_wake = AsyncMock()
    p.notify_router_playback_override = AsyncMock()
    return p


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestIsRadioStream:
    def test_rewritten_uri_matches_while_flag_set(self, radio_player):
        radio_player._radio_playback = True
        assert radio_player._is_radio_stream(REWRITTEN_RADIO_URI)

    def test_original_scheme_matches_while_flag_set(self, radio_player):
        radio_player._radio_playback = True
        assert radio_player._is_radio_stream(
            "x-rincon-mp3radio://live1.sr.se/p1-aac-320")

    def test_no_match_without_flag(self, radio_player):
        """An externally started aac:// stream is not ours — Sonos keeps
        metadata ownership (ICY title broadcasts as before)."""
        radio_player._radio_playback = False
        assert not radio_player._is_radio_stream(REWRITTEN_RADIO_URI)

    def test_spotify_uri_never_matches(self, radio_player):
        radio_player._radio_playback = True
        assert not radio_player._is_radio_stream(SPOTIFY_URI)

    def test_none_uri_never_matches(self, radio_player):
        radio_player._radio_playback = True
        assert not radio_player._is_radio_stream(None)


class TestFetchMediaDataRadioDrop:
    def test_fetch_returns_none_for_radio_stream(self, radio_player):
        """The choke point: every broadcast path (eager, monitor, retry,
        join) goes through fetch_media_data, so the deliberate drop here
        keeps the ICY title from ever reaching the router."""
        p = radio_player
        p._radio_playback = True
        p.sonos_viewer.get_current_track_info = MagicMock(return_value={
            "uri": REWRITTEN_RADIO_URI,
            "title": "Godmorgon, världen!",
            "artist": "", "album": "",
        })

        assert _run(p.fetch_media_data()) is None

    def test_eager_broadcast_on_play_start_stays_silent(self, radio_player):
        """_on_playback_started fires on the stopped→playing transition
        right after the radio play command.  It clears the suppress window
        and eagerly fetches — the fetch guard must keep it silent."""
        p = radio_player
        p._radio_playback = True
        p._current_playback_state = "stopped"
        p.sonos_viewer.get_current_track_info = MagicMock(return_value={
            "uri": REWRITTEN_RADIO_URI,
            "title": "Godmorgon, världen!",
            "artist": "", "album": "",
        })

        _run(p._on_playback_started())

        assert p.broadcast_media_update.await_count == 0
        assert p.trigger_wake.await_count == 1  # screen still wakes

    def test_zpstr_placeholder_still_dropped(self, radio_player):
        """The pre-existing ZPSTR guard must survive the new one."""
        p = radio_player
        p._radio_playback = False
        p.sonos_viewer.get_current_track_info = MagicMock(return_value={
            "uri": SPOTIFY_URI,
            "title": "ZPSTR_BUFFERING",
            "artist": "", "album": "",
        })

        assert _run(p.fetch_media_data()) is None


class TestRadioPlaybackFlag:
    def test_play_radio_sets_flag(self, radio_player):
        """play(radio=True) must set the flag even if the SoCo call fails —
        it is assigned before any network I/O."""
        p = radio_player
        p.sonos_viewer.get_coordinator = MagicMock(
            side_effect=RuntimeError("offline"))
        _run(p.play(url="https://live1.sr.se/p1-aac-320", radio=True))
        assert p._radio_playback is True

    def test_play_non_radio_clears_flag(self, radio_player):
        p = radio_player
        p._radio_playback = True
        p.sonos_viewer.get_coordinator = MagicMock(
            side_effect=RuntimeError("offline"))
        _run(p.play(uri="https://open.spotify.com/playlist/xyz"))
        assert p._radio_playback is False
