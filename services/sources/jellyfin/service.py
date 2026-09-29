#!/usr/bin/env python3
"""
BeoSound 5c Jellyfin Source (beo-source-jellyfin)

Provides Jellyfin playback via Quick Connect (or username/password) login.
Plays on the configured player service (Sonos, BlueSound, etc.) via its
HTTP API — uses direct stream URLs via play_uri().
Source-managed track advancement (next/prev play the URL directly).

Port: 8781
"""

import asyncio
import base64
import json
import logging
import os
import ssl
import sys
import time
from datetime import datetime, timedelta

import aiohttp
from aiohttp import web

# Shared library (services/) — must come first so ``lib`` is importable
# by sibling modules that now import from it (e.g. jellyfin_tokens).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
# Sibling imports (this directory)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jellyfin_api import normalise_url
from jellyfin_auth import JellyfinAuth
from jellyfin_tokens import delete_tokens

from lib.source_base import SourceBase
from lib.digit_playlists import DigitPlaylistMixin

logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
log = logging.getLogger('beo-source-jellyfin')

# Configuration
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
PLAYLISTS_FILE = os.path.join(
    os.getenv('BS5C_BASE_PATH', PROJECT_ROOT),
    'web', 'json', 'jellyfin_playlists.json')
DIGIT_PLAYLISTS_FILE = os.path.join(
    os.getenv('BS5C_BASE_PATH', PROJECT_ROOT),
    'web', 'json', 'jellyfin_digit_playlists.json')

POLL_INTERVAL = 1  # fast poll for responsive track advancement (local HTTP)
PLAYLIST_REFRESH_COOLDOWN = 5 * 60
# How long a Quick Connect code stays on screen before /start-login
# asks the server for a fresh one.  Jellyfin expires the request after
# a few minutes, so re-issue well before that rather than leaving a
# dead code showing.
QUICK_CONNECT_REUSE = 180
NIGHTLY_REFRESH_HOUR = 5  # offset from Apple Music (2am), TIDAL (3am), Plex (4am)
FETCH_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fetch.py')

# Persistence for the last-played playlist/track so activate_playback can
# resume where the user left off across service or device restarts.
LAST_PLAYED_PATH_PROD = os.path.join(
    os.getenv('BS5C_CONFIG_DIR', '/etc/beosound5c'), 'jellyfin_last_played.json')
LAST_PLAYED_PATH_DEV = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'jellyfin_last_played.json')


def _find_token_file():
    """Find the token file path (same logic as tokens.py)."""
    paths = [
        "/etc/beosound5c/jellyfin_tokens.json",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "jellyfin_tokens.json"),
    ]
    for path in paths:
        if os.path.exists(path):
            return path
    for path in paths:
        d = os.path.dirname(path)
        if os.path.isdir(d) and os.access(d, os.W_OK):
            return path
    return paths[-1]


class JellyfinService(DigitPlaylistMixin, SourceBase):
    """Main Jellyfin source service with source-managed track advancement."""

    id = "jellyfin"
    name = "JELLYFIN"
    port = 8781
    manages_queue = True
    DIGIT_PLAYLISTS_FILE = DIGIT_PLAYLISTS_FILE
    action_map = {
        "play": "play",
        "pause": "pause",
        "go": "toggle",
        "next": "next",
        "prev": "prev",
        "right": "next",
        "left": "prev",
        "up": "next",
        "down": "prev",
        "stop": "stop",
        "0": "digit", "1": "digit", "2": "digit",
        "3": "digit", "4": "digit", "5": "digit",
        "6": "digit", "7": "digit", "8": "digit",
        "9": "digit",
    }

    def __init__(self):
        super().__init__()
        self.auth = JellyfinAuth()
        self.playlists = []
        self.state = "stopped"
        self.now_playing = None
        self._poll_task = None
        self._refresh_task = None
        self._nightly_task = None
        self._fetching_playlists = False
        self._last_refresh = 0
        self._last_refresh_wall = None
        self._last_refresh_duration = None
        self._pending_login = None  # (server_url, secret, code, created) during Quick Connect
        self._login_lock = asyncio.Lock()
        # Source-managed track advancement
        self._current_playlist = None  # full playlist dict with tracks
        self._current_index = 0
        self._last_play_time = 0
        self._last_play_id = None
        self._play_lock = asyncio.Lock()   # serializes track starts (no interleaved skips)
        self._expected_stream_url = None   # URL the player confirmed it started
        self._track_started_at = 0.0       # monotonic time of last confirmed start
        self._pending_stop_polls = 0       # consecutive 'stopped' poll observations

    async def on_start(self):
        # auth.load() connects to the Jellyfin server with blocking requests
        # (~10-20s against an unreachable server) — keep it off the loop
        # so startup can't hang past the systemd watchdog.
        loop = asyncio.get_running_loop()
        has_creds = await loop.run_in_executor(None, self.auth.load)

        if has_creds:
            self._load_playlists()
            self._load_last_played()
            self._detect_player()

            caps = await self.player_capabilities()
            if caps:
                log.info("Player service available - using player API")
            else:
                log.warning("No player service available")
        else:
            log.info("No Jellyfin credentials - waiting for setup via /setup")

        # Always register so JELLYFIN appears in menu
        await self.register("available")

        log.info("Jellyfin source ready (%s)",
                 "player service" if has_creds else "awaiting setup")

        if self.auth.is_configured:
            self._refresh_task = asyncio.create_task(
                self._delayed_refresh(delay=10))
            self._nightly_task = asyncio.create_task(
                self._nightly_refresh_loop())

    async def on_stop(self):
        for task in (self._poll_task, self._refresh_task, self._nightly_task):
            if task:
                task.cancel()
                try:
                    await task
                except Exception:
                    pass
        await self.register("gone")

    def _load_playlists(self):
        """Load playlists from the pre-fetched JSON file."""
        try:
            with open(PLAYLISTS_FILE) as f:
                self.playlists = json.load(f)
            log.info("Loaded %d playlists from disk", len(self.playlists))
        except (FileNotFoundError, json.JSONDecodeError) as e:
            log.warning("Could not load playlists: %s", e)
            self.playlists = []
        self._reload_digit_playlists()

    # ── Last-played persistence ──
    # Stores {playlist_id, index} on disk so `activate_playback` resumes
    # where the user left off after a service or device restart.

    def _last_played_path(self) -> str:
        if os.path.exists(os.path.dirname(LAST_PLAYED_PATH_PROD)):
            return LAST_PLAYED_PATH_PROD
        return LAST_PLAYED_PATH_DEV

    def _load_last_played(self):
        path = self._last_played_path()
        try:
            with open(path) as f:
                data = json.load(f)
            pid = data.get("playlist_id")
            idx = int(data.get("index", 0) or 0)
            if not pid:
                return
            for pl in self.playlists:
                if pl.get("id") == pid:
                    self._current_playlist = pl
                    tracks = pl.get("tracks", [])
                    self._current_index = max(0, min(idx, max(0, len(tracks) - 1)))
                    log.info("Loaded last played: playlist=%s (%s) index=%d",
                             pid, pl.get("name", "?"), self._current_index)
                    return
            log.info("Last-played playlist %s not in current library", pid)
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("Failed to load last played: %s", e)

    def _save_last_played(self):
        if not self._current_playlist:
            return
        path = self._last_played_path()
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({
                    "playlist_id": self._current_playlist.get("id"),
                    "index": self._current_index,
                }, f, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            log.warning("Failed to save last played: %s", e)

    # -- SourceBase hooks --

    def add_routes(self, app):
        app.router.add_get('/playlists', self._handle_playlists)
        app.router.add_get('/setup', self._handle_setup)
        app.router.add_get('/server', self._handle_server_info)
        app.router.add_post('/probe', self._handle_probe)
        app.router.add_options('/probe', self._handle_cors)
        app.router.add_post('/start-login', self._handle_start_login)
        app.router.add_options('/start-login', self._handle_cors)
        app.router.add_post('/check-login', self._handle_check_login)
        app.router.add_options('/check-login', self._handle_cors)
        app.router.add_post('/password-login', self._handle_password_login)
        app.router.add_options('/password-login', self._handle_cors)
        app.router.add_post('/logout', self._handle_logout)
        app.router.add_options('/logout', self._handle_cors)

    async def handle_status(self) -> dict:
        return {
            'state': self.state,
            'now_playing': self.now_playing,
            'playlist_count': len(self.playlists),
            'has_credentials': self.auth.is_configured,
            'user_name': self.auth.user_name,
            'server_name': self.auth.server_name,
            'server_url': self.auth.server_url,
            'last_refresh': self._last_refresh_wall.isoformat() if self._last_refresh_wall else None,
            'last_refresh_duration': self._last_refresh_duration,
            'digit_playlists': self._get_digit_names(),
            'fetching': self._fetching_playlists,
            'current_playlist': self._current_playlist.get('name') if self._current_playlist else None,
            'current_index': self._current_index,
        }

    async def handle_resync(self) -> dict:
        if self.auth.is_configured:
            state = self.state if self.state in ('playing', 'paused') else 'available'
            await self.register(state)
            await self._resync_media()
            return {'status': 'ok', 'resynced': True}

        # Auth not configured — try loading tokens from disk (server may
        # have been unreachable at startup but tokens exist on disk)
        loop = asyncio.get_running_loop()
        recovered = await loop.run_in_executor(None, self.auth.load)
        if recovered:
            log.info("Jellyfin auth recovered on resync — initialising")
            self._load_playlists()
            self._detect_player()
            await self.register("available")
            self._refresh_task = asyncio.create_task(
                self._delayed_refresh(delay=10))
            self._nightly_task = asyncio.create_task(
                self._nightly_refresh_loop())
            return {'status': 'ok', 'resynced': True, 'recovered': True}

        return {'status': 'ok', 'resynced': False}

    async def activate_playback(self):
        """Resume or start playback on source button press.
        Always re-sends content to the player — the shared player may have
        been taken over by another source since we last played."""
        if self._current_playlist:
            # Resume from current position in the last playlist
            await self._play_current_track()
        elif self.playlists:
            await self._play_playlist(self.playlists[0]['id'])

    async def handle_command(self, cmd, data) -> dict:
        if cmd == 'digit':
            digit = data.get('action', '0')
            playlist = self._get_digit_playlist(digit)
            if playlist:
                log.info("Digit %s -> playlist %s", digit, playlist.get('id'))
                await self._play_playlist(playlist['id'])
            else:
                log.info("No playlist mapped to digit %s", digit)

        elif cmd == 'play_playlist':
            playlist_id = data.get('playlist_id', '')
            track_index = data.get('track_index')
            await self._play_playlist(playlist_id, track_index)

        elif cmd == 'play_track':
            url = data.get('url', '')
            await self._play_track_url(url)

        elif cmd == 'toggle':
            await self._toggle()

        elif cmd == 'play':
            await self._resume()

        elif cmd == 'pause':
            await self._pause()

        elif cmd == 'next':
            await self._next()

        elif cmd == 'prev':
            await self._prev()

        elif cmd == 'play_index':
            index = data.get('index', 0)
            if self._current_playlist:
                tracks = self._current_playlist.get('tracks', [])
                if 0 <= index < len(tracks):
                    self._current_index = index
                    self._save_last_played()
                    await self._play_current_track()

        elif cmd == 'stop':
            await self._stop()

        elif cmd == 'refresh_playlists':
            await self._refresh_playlists()

        elif cmd == 'logout':
            await self._logout()

        else:
            return {'status': 'error', 'message': f'Unknown: {cmd}'}

        return {'state': self.state}

    # -- Playback control (source-managed track advancement) --

    async def _play_playlist(self, playlist_id, track_index=None):
        """Start playing a playlist. Stores playlist for source-managed advancement."""
        now = time.monotonic()
        if now - self._last_play_time < 2 and self._last_play_id == playlist_id:
            log.debug("Debounced duplicate play for %s", playlist_id)
            return
        self._last_play_time = now
        self._last_play_id = playlist_id
        playlist = None
        for pl in self.playlists:
            if pl.get('id') == playlist_id:
                playlist = pl
                break

        if not playlist:
            log.warning("Playlist %s not found", playlist_id)
            return

        tracks = playlist.get('tracks', [])
        if not tracks:
            log.warning("Playlist %s has no tracks", playlist_id)
            return

        index = track_index if track_index is not None else 0
        if index < 0 or index >= len(tracks):
            index = 0

        self._current_playlist = playlist
        self._current_index = index
        self._save_last_played()

        await self._play_current_track()

    async def _play_current_track(self):
        """Play the track at _current_index in _current_playlist.

        Serialized by _play_lock so rapid skips can't interleave (the slow
        artwork fetch made out-of-order metadata posts possible), and the
        optimistic pre-broadcast is reverted if the player refuses the track."""
        async with self._play_lock:
            await self._play_current_locked()

    async def _play_current_locked(self):
        if not self._current_playlist:
            return

        tracks = self._current_playlist.get('tracks', [])
        if not tracks or self._current_index >= len(tracks):
            log.info("End of playlist reached")
            await self._stop()
            return

        track = tracks[self._current_index]
        url = track.get('url')

        if not url:
            log.warning("Track %s has no stream URL, skipping",
                        track.get('name', '?'))
            # Try next track
            if self._current_index + 1 < len(tracks):
                self._current_index += 1
                await self._play_current_locked()
            return

        # Fetch artwork and embed as base64 (Jellyfin servers may use self-signed
        # HTTPS certs that the browser would reject for direct image loads)
        artwork = await self._fetch_artwork_base64(track.get("image", ""))

        prev_np = self.now_playing
        # Pre-broadcast metadata for instant PLAYING view update (optimistic —
        # reverted below if the play command fails)
        await self.post_media_update(
            title=track.get("name", ""),
            artist=track.get("artist", ""),
            artwork=artwork,
            state="playing",
            reason="track_change",
        )
        log.info("Playing [%d/%d] %s - %s",
                 self._current_index + 1, len(tracks),
                 track.get('artist', '?'), track.get('name', '?'))

        ok = await self.player_play(url=url)
        if ok:
            self.state = "playing"
            self.now_playing = {
                'title': track.get('name'),
                'artist': track.get('artist'),
                'image': track.get('image'),
                'total': len(tracks),
                'index': self._current_index,
            }
            self._expected_stream_url = url
            self._track_started_at = time.monotonic()
            self._pending_stop_polls = 0
            await self.register("playing", auto_power=True)
            self._start_polling()
        else:
            log.error("Player failed to start '%s' — reverting optimistic state",
                      track.get('name', '?'))
            await self._revert_failed_play(prev_np)

    async def _revert_failed_play(self, prev_np):
        """A play command failed — the player still plays whatever it played
        before (or nothing). Converge belief and UI back to that reality."""
        if prev_np is not None and await self.reassert_player_media():
            # Previous track still playing — restore belief incl. queue index
            self.now_playing = prev_np
            idx = prev_np.get('index')
            if idx is not None and self._current_index != idx:
                self._current_index = idx
                self._save_last_played()
        else:
            # Leave state != "playing" and stop polling — a stopped player
            # with state "playing" would auto-advance and burn through the
            # entire playlist at poll rate (e.g. when every cached stream
            # URL 401s after a token rotation).
            self.state = "stopped"
            self.now_playing = None
            self._stop_polling()
            await self.register("available")

    async def _fetch_artwork_base64(self, url):
        """Fetch artwork URL and return as base64 data URI.

        Uses an SSL context that accepts self-signed certs (common on
        local Jellyfin servers with 'Secure connections: Required').
        Returns the original URL as fallback if fetch fails.
        """
        if not url or url.startswith("data:"):
            return url
        try:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            conn = aiohttp.TCPConnector(ssl=ssl_ctx)
            async with aiohttp.ClientSession(connector=conn) as sess:
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    resp.raise_for_status()
                    data = await resp.read()
                    if data:
                        ct = resp.content_type or "image/jpeg"
                        b64 = base64.b64encode(data).decode("utf-8")
                        return f"data:{ct};base64,{b64}"
        except Exception as e:
            log.warning("Artwork fetch failed (%s): %s", url[:80], e)
        return url

    async def _play_track_url(self, url):
        """Play a specific track by its stream URL (standalone, no playlist context)."""
        log.info("Play track URL %s", url)
        ok = await self.player_play(url=url)
        if ok:
            self.state = "playing"
            self._current_playlist = None
            self._current_index = 0
            self._expected_stream_url = url
            self._track_started_at = time.monotonic()
            self._pending_stop_polls = 0
            await self.register("playing", auto_power=True)
            self._start_polling()
        else:
            log.error("Player failed to start track URL")
            await self._revert_failed_play(self.now_playing)

    async def get_queue(self, start=0, max_items=50) -> dict:
        """Return the current Jellyfin playlist as a queue."""
        if not self._current_playlist:
            return {"tracks": [], "current_index": -1, "total": 0}
        all_tracks = self._current_playlist.get('tracks', [])
        end = min(start + max_items, len(all_tracks))
        tracks = []
        for i in range(start, end):
            t = all_tracks[i]
            tracks.append({
                "id": f"q:{i}",
                "title": t.get("name", ""),
                "artist": t.get("artist", ""),
                "album": "",
                "artwork": t.get("image", ""),
                "index": i,
                "current": i == self._current_index,
            })
        return {
            "tracks": tracks,
            "current_index": self._current_index,
            "total": len(all_tracks),
        }

    async def _toggle(self):
        if self.state == "playing":
            await self._pause()
        elif self.state == "paused":
            await self._resume()
        elif self.state == "stopped" and self.playlists:
            first = self.playlists[0]
            await self._play_playlist(first['id'])

    async def _resume(self):
        if await self.player_resume():
            self.state = "playing"
            await self.register("playing", auto_power=True)
            self._start_polling()

    async def _pause(self):
        if await self.player_pause():
            self.state = "paused"
            await self.register("paused")

    async def _next(self):
        """Source-managed: advance to next track in playlist."""
        if self._current_playlist:
            tracks = self._current_playlist.get('tracks', [])
            if self._current_index + 1 < len(tracks):
                self._current_index += 1
                self._save_last_played()
                await self._play_current_track()
            else:
                log.info("Already at last track")
        else:
            # No playlist context — delegate to player
            if await self.player_next():
                await asyncio.sleep(0.5)
                await self._poll_now_playing()

    async def _prev(self):
        """Source-managed: go to previous track in playlist."""
        if self._current_playlist:
            if self._current_index > 0:
                self._current_index -= 1
                self._save_last_played()
                await self._play_current_track()
            else:
                log.info("Already at first track")
        else:
            # No playlist context — delegate to player
            if await self.player_prev():
                await asyncio.sleep(0.5)
                await self._poll_now_playing()

    async def _stop(self):
        await self.player_stop()
        self.state = "stopped"
        self.now_playing = None
        self._current_playlist = None
        self._current_index = 0
        self._expected_stream_url = None
        self._pending_stop_polls = 0
        self._stop_polling()
        await self.register("available")

    async def _refresh_playlists(self):
        """Re-fetch playlists by running fetch.py."""
        if getattr(self, '_refresh_running', False):
            log.info("Playlist refresh already running — skipping duplicate")
            return
        self._refresh_running = True
        self._fetching_playlists = True
        t0 = time.monotonic()
        try:
            token_file = _find_token_file()
            if not os.path.exists(token_file):
                log.error("Cannot refresh playlists - no token file")
                return

            log.info("Starting playlist refresh via fetch.py")
            proc = await asyncio.create_subprocess_exec(
                sys.executable, FETCH_SCRIPT,
                '--output', PLAYLISTS_FILE,
                '--token-file', token_file,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
            if proc.returncode == 0:
                self._load_playlists()
                self._last_refresh = time.monotonic()
                self._last_refresh_wall = datetime.now()
                self._last_refresh_duration = round(time.monotonic() - t0, 1)
                log.info("Playlist refresh complete (%d playlists, %.1fs)",
                         len(self.playlists), self._last_refresh_duration)
            else:
                err_msg = (stdout.decode() + stderr.decode())[-500:]
                log.error("fetch.py failed (rc=%d): %s", proc.returncode, err_msg)
        except asyncio.TimeoutError:
            # wait_for cancels the communicate() await but does NOT kill the
            # child — an orphan with a full stdout pipe blocks forever and
            # its late JSON write races the next refresh.  Reap it.
            log.error("Playlist refresh timed out — killing fetch subprocess")
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        except Exception as e:
            log.error("Playlist refresh failed: %s", e)
        finally:
            self._fetching_playlists = False
            self._refresh_running = False

    async def _delayed_refresh(self, delay):
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await self._refresh_playlists()
        except asyncio.CancelledError:
            return

    async def _nightly_refresh_loop(self):
        try:
            while True:
                now = datetime.now()
                target = now.replace(hour=NIGHTLY_REFRESH_HOUR, minute=0, second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
                delay = (target - now).total_seconds()
                log.info("Next nightly playlist refresh at %s (in %.0fh)",
                         target.strftime('%H:%M'), delay / 3600)
                await asyncio.sleep(delay)
                log.info("Nightly playlist refresh starting")
                await self._refresh_playlists()
        except asyncio.CancelledError:
            return

    def _should_refresh(self):
        return time.monotonic() - self._last_refresh > PLAYLIST_REFRESH_COOLDOWN

    async def _logout(self):
        """Clear Jellyfin tokens and playlists."""
        log.info("Logging out of Jellyfin")

        if self._refresh_task:
            self._refresh_task.cancel()
            self._refresh_task = None
        if self._nightly_task:
            self._nightly_task.cancel()
            self._nightly_task = None
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None

        self.auth.clear()
        self.playlists = []
        self.state = "stopped"
        self.now_playing = None
        self._fetching_playlists = False
        self._pending_login = None
        self._current_playlist = None
        self._current_index = 0

        try:
            path = delete_tokens()
            if path:
                log.info("Deleted token file: %s", path)
        except Exception as e:
            log.warning("Could not delete token file: %s", e)

        try:
            if os.path.exists(PLAYLISTS_FILE):
                os.unlink(PLAYLISTS_FILE)
                log.info("Deleted playlist file: %s", PLAYLISTS_FILE)
        except Exception as e:
            log.warning("Could not delete playlist file: %s", e)

        await self.register("available")
        log.info("Jellyfin logged out - ready for new setup")

    # -- Now-playing polling --

    def _start_polling(self):
        if self._poll_task and not self._poll_task.done():
            return
        self._poll_task = asyncio.create_task(self._poll_loop())

    def _stop_polling(self):
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None

    async def _poll_loop(self):
        try:
            while self.state in ("playing", "paused"):
                await self._poll_now_playing()
                await asyncio.sleep(POLL_INTERVAL)
        except asyncio.CancelledError:
            return

    async def _poll_now_playing(self):
        """Poll player state. Auto-advance to next track when current finishes."""
        try:
            state = await self.player_state()
            if state == "playing":
                self._pending_stop_polls = 0
                # Identity check: is the player still playing OUR stream?
                # Grace period after each start (player may briefly report the
                # previous URI), and substring match (players may wrap the URL
                # in a scheme prefix). Empty URI = player can't say — skip.
                if (self._expected_stream_url and self.state == "playing"
                        and time.monotonic() - self._track_started_at > 10):
                    uri = await self.player_track_uri()
                    if uri and (self._expected_stream_url not in uri
                                and uri not in self._expected_stream_url):
                        log.info("Player is playing other content — deactivating "
                                 "(uri=%.60s)", uri)
                        self.state = "stopped"
                        self.now_playing = None
                        await self.register("available")
                        return
                if self.state != "playing":
                    self.state = "playing"
                    await self.register("playing")
            elif state == "stopped" and self.state == "playing":
                if self._current_playlist:
                    await self._handle_playback_stopped()
                    return
                self.state = "stopped"
                self.now_playing = None
                await self.register("available")
            elif state == "paused" and self.state == "playing":
                self.state = "paused"
                await self.register("paused")
        except Exception as e:
            log.warning("Player state poll error: %s", e)

    async def _handle_playback_stopped(self):
        """Playing → stopped with a playlist loaded. Distinguish 'track
        finished' (advance) from 'stream died' / 'stopped externally'
        (deactivate) instead of blindly advancing."""
        # Debounce: require two consecutive stopped polls. An external stop
        # fires a router playback-override that deactivates this source within
        # ~1s — advancing on the first poll would resurrect playback the user
        # just stopped.
        self._pending_stop_polls += 1
        if self._pending_stop_polls < 2:
            return
        self._pending_stop_polls = 0

        played = time.monotonic() - self._track_started_at
        if played < 10:
            # Died almost immediately — dead stream URL, not a finished track.
            # Advancing would cascade through the whole playlist.
            log.warning("Track stopped after %.1fs — failed stream, not advancing",
                        played)
            self.state = "stopped"
            self.now_playing = None
            await self.register("available")
            return

        tracks = self._current_playlist.get('tracks', [])
        if self._current_index + 1 < len(tracks):
            log.info("Track finished, advancing to next")
            self._current_index += 1
            self._save_last_played()
            await self._play_current_track()
        else:
            log.info("Playlist finished")
            self.state = "stopped"
            self.now_playing = None
            await self.register("available")

    # -- Extra routes --

    async def _handle_playlists(self, request):
        if not self.auth.is_configured:
            return web.json_response({
                'setup_needed': True,
                'setup_url': f'http://localhost:{self.port}/setup',
            }, headers=self._cors_headers())
        if self._fetching_playlists and not self.playlists:
            return web.json_response({
                'loading': True,
            }, headers=self._cors_headers())

        if self._should_refresh() and not self._fetching_playlists:
            self._fetching_playlists = True
            log.info("Playlist view opened - refreshing in background")
            self._spawn(self._refresh_playlists(), name="refresh_playlists")

        return web.json_response(
            self.playlists,
            headers=self._cors_headers())

    async def _handle_server_info(self, request):
        """Pre-fill data for the setup page."""
        loop = asyncio.get_running_loop()
        url = await loop.run_in_executor(None, self.auth.configured_url)
        return web.json_response({
            'url': url,
            'connected': self.auth.is_configured,
            'server_name': self.auth.server_name,
            'user_name': self.auth.user_name,
        }, headers=self._cors_headers())

    async def _handle_probe(self, request):
        """Check that the address the user typed is a Jellyfin server."""
        data = await self._json_body(request)
        loop = asyncio.get_running_loop()
        try:
            server_url, info = await loop.run_in_executor(
                None, self.auth.probe, data.get('url', ''))
            quick = await loop.run_in_executor(
                None, self.auth.quick_connect_available, server_url)
        except ValueError as e:
            return web.json_response({'error': str(e)}, status=400,
                                     headers=self._cors_headers())
        except Exception as e:
            log.error("Jellyfin probe failed: %s", e)
            return web.json_response({'error': str(e)}, status=502,
                                     headers=self._cors_headers())
        return web.json_response({
            'url': server_url,
            'name': info.get('ServerName', 'Jellyfin'),
            'version': info.get('Version', ''),
            'quick_connect': quick,
        }, headers=self._cors_headers())

    async def _handle_start_login(self, request):
        """Start a Quick Connect request.

        Locked and debounced for the same reason as Plex's PIN flow: the
        setup page can be opened in an iframe that double-loads, and two
        codes in flight means the one on screen is the stale one.
        """
        data = await self._json_body(request)
        # Normalise for the debounce check: the page sends back whatever
        # /probe resolved to, but a hand-typed "host:8096" must still match
        # the pending "http://host:8096" rather than issuing a second code.
        url = normalise_url(data.get('url', ''))

        async with self._login_lock:
            if self._pending_login:
                pending_url, secret, code, created = self._pending_login
                if (normalise_url(pending_url) == url
                        and time.monotonic() - created < QUICK_CONNECT_REUSE):
                    log.info("Reusing existing Quick Connect code (%.0fs old)",
                             time.monotonic() - created)
                    return web.json_response(
                        {'code': code}, headers=self._cors_headers())
                self._pending_login = None

            loop = asyncio.get_running_loop()
            try:
                server_url, _info = await loop.run_in_executor(
                    None, self.auth.probe, url)
                secret, code = await loop.run_in_executor(
                    None, self.auth.start_quick_connect, server_url)
            except ValueError as e:
                return web.json_response({'error': str(e)}, status=400,
                                         headers=self._cors_headers())
            except Exception as e:
                log.error("Failed to start Quick Connect: %s", e)
                return web.json_response({'error': str(e)}, status=500,
                                         headers=self._cors_headers())

            self._pending_login = (server_url, secret, code, time.monotonic())
            log.info("Jellyfin Quick Connect started - code: %s", code)
            return web.json_response(
                {'code': code}, headers=self._cors_headers())

    async def _handle_check_login(self, request):
        """Check whether the Quick Connect request has been approved."""
        if not self._pending_login:
            return web.json_response(
                {'status': 'no_pending'}, headers=self._cors_headers())

        server_url, secret, _code, _created = self._pending_login
        loop = asyncio.get_running_loop()

        try:
            success = await loop.run_in_executor(
                None, self.auth.check_quick_connect, server_url, secret)
        except TimeoutError:
            self._pending_login = None
            log.info("Jellyfin Quick Connect request expired")
            return web.json_response(
                {'status': 'expired'}, headers=self._cors_headers())
        except ValueError as e:
            self._pending_login = None
            log.error("Jellyfin Quick Connect error: %s", e)
            return web.json_response(
                {'status': 'error', 'error': str(e)},
                headers=self._cors_headers())
        except Exception as e:
            self._pending_login = None
            log.error("Jellyfin Quick Connect check failed: %s", e)
            return web.json_response(
                {'status': 'expired', 'error': str(e)},
                headers=self._cors_headers())

        if not success:
            return web.json_response(
                {'status': 'pending'}, headers=self._cors_headers())

        self._pending_login = None
        await self._after_login()
        return web.json_response(
            {'status': 'ok'}, headers=self._cors_headers())

    async def _handle_password_login(self, request):
        """Sign in with a username and password.

        The fallback for servers with Quick Connect switched off — and the
        only path that works when the user has no other Jellyfin client
        signed in to approve a code from.
        """
        data = await self._json_body(request)
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, self.auth.login_password, data.get('url', ''),
                data.get('username', ''), data.get('password', ''))
        except ValueError as e:
            return web.json_response({'status': 'error', 'error': str(e)},
                                     status=400, headers=self._cors_headers())
        except Exception as e:
            log.error("Jellyfin password login failed: %s", e)
            return web.json_response({'status': 'error', 'error': str(e)},
                                     status=500, headers=self._cors_headers())

        self._pending_login = None
        await self._after_login()
        return web.json_response(
            {'status': 'ok'}, headers=self._cors_headers())

    async def _after_login(self):
        """Shared tail of both login paths: register and pull the library."""
        self._detect_player()
        await self.register("available")

        self._fetching_playlists = True
        if self._refresh_task:
            self._refresh_task.cancel()
        self._refresh_task = asyncio.create_task(self._delayed_refresh(delay=0))

        if not self._nightly_task or self._nightly_task.done():
            self._nightly_task = asyncio.create_task(self._nightly_refresh_loop())

        log.info("Jellyfin login successful (user: %s, server: %s)",
                 self.auth.user_name, self.auth.server_name)

    @staticmethod
    async def _json_body(request):
        """Request body as a dict — an empty one for anything unparseable."""
        try:
            data = await request.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    async def _handle_setup(self, request):
        """Serve the Jellyfin setup page."""
        html = '''<!DOCTYPE html><html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BeoSound 5c - Jellyfin Setup</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Helvetica Neue',-apple-system,sans-serif;background:#000;color:#fff;padding:20px;line-height:1.7}
.container{max-width:500px;margin:0 auto}
.header{text-align:center;margin-bottom:30px;padding-bottom:20px;border-bottom:1px solid #333}
h1{font-size:24px;font-weight:300;letter-spacing:2px;margin-bottom:8px}
.subtitle{color:#666;font-size:14px}
.step{background:#111;border-radius:8px;padding:20px;margin-bottom:16px;border:1px solid #222}
.step-number{display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;border:2px solid #00A4DC;color:#00A4DC;border-radius:50%;font-weight:600;font-size:14px;margin-right:12px}
.step-title{font-size:16px;font-weight:500;margin-bottom:12px;display:flex;align-items:center}
.step-content{color:#999;font-size:14px;margin-left:40px}
.step-content p{margin-bottom:8px}
label{display:block;color:#666;font-size:12px;text-transform:uppercase;letter-spacing:1px;margin:12px 0 6px}
input{width:100%;padding:12px;background:#000;border:1px solid #333;border-radius:4px;color:#fff;font-size:15px}
input:focus{outline:none;border-color:#00A4DC}
.submit-btn{display:block;width:100%;padding:14px;margin-top:20px;background:#00A4DC;border:none;border-radius:4px;color:#000;font-size:16px;font-weight:600;cursor:pointer;text-align:center;text-decoration:none}
.submit-btn:hover{background:#0090c0}
.submit-btn:disabled{background:#333;color:#666;cursor:not-allowed}
.secondary-btn{background:none;border:1px solid #333;color:#999}
.secondary-btn:hover{background:#111;border-color:#555}
.note{background:#0a0a0a;border:1px solid #222;border-radius:4px;padding:12px;margin:12px 0;font-size:13px;color:#666}
.status{margin-top:16px;color:#666;font-size:14px;min-height:20px}
.status.error{color:#e06c60}
.code{font-family:'SF Mono',Monaco,Consolas,monospace;font-size:42px;letter-spacing:10px;color:#00A4DC;text-align:center;padding:20px;background:#0a0a0a;border:1px solid #333;border-radius:4px;margin:16px 0}
.hidden{display:none}
.ok-container{display:none;text-align:center;margin-top:30px}
.ok{width:80px;height:80px;border:3px solid #00A4DC;border-radius:50%;display:flex;align-items:center;justify-content:center;margin:0 auto 20px;font-size:36px;color:#00A4DC}
</style></head><body>
<div class="container">
<div class="header"><h1>JELLYFIN SETUP</h1><div class="subtitle">BeoSound 5c</div></div>
<div id="setup-form">

<div class="step">
    <div class="step-title"><span class="step-number">1</span>Your Jellyfin server</div>
    <div class="step-content">
        <p>The address you open Jellyfin at on your network.</p>
        <label>Server address</label>
        <input id="url" type="url" placeholder="http://jellyfin.local:8096" autocapitalize="off" autocorrect="off" spellcheck="false">
        <button id="find-btn" class="submit-btn">Find server</button>
        <div id="find-status" class="status"></div>
    </div>
</div>

<div id="step-login" class="step hidden">
    <div class="step-title"><span class="step-number">2</span>Sign in</div>
    <div class="step-content">
        <div id="qc-block" class="hidden">
            <p>Enter this code in Jellyfin on your phone or computer, under
               <strong>Settings &rarr; Quick Connect</strong>.</p>
            <div id="qc-code" class="code"></div>
            <div id="qc-status" class="status">Waiting for you to approve the code&hellip;</div>
        </div>
        <div id="pw-block">
            <p id="pw-intro">Sign in with your Jellyfin username and password.</p>
            <label>Username</label>
            <input id="username" type="text" autocapitalize="off" autocorrect="off" spellcheck="false">
            <label>Password</label>
            <input id="password" type="password">
            <button id="pw-btn" class="submit-btn">Sign in</button>
            <div id="pw-status" class="status"></div>
        </div>
        <button id="toggle-btn" class="submit-btn secondary-btn hidden">Use a password instead</button>
    </div>
</div>

</div>
<div id="ok-container" class="ok-container">
    <div class="ok">&#10003;</div>
    <h1 style="font-size:24px;font-weight:300;margin-bottom:20px;letter-spacing:1px">Connected to Jellyfin</h1>
    <p style="color:#999">Playlists are loading now.<br>You can close this page.</p>
    <p class="note" style="margin-top:30px">The BeoSound 5c screen will update automatically.</p>
</div>
</div>

<script>
const BASE_URL = window.location.origin;
const $ = (id) => document.getElementById(id);
let serverUrl = '';
let pollTimer = null;

function setStatus(el, text, isError) {
    el.textContent = text || '';
    el.classList.toggle('error', !!isError);
}

function showDone() {
    if (pollTimer) clearInterval(pollTimer);
    $('setup-form').style.display = 'none';
    $('ok-container').style.display = 'block';
}

// Pre-fill from config.json / a previous login.
fetch(BASE_URL + '/server').then(r => r.json()).then(d => {
    if (d.url) $('url').value = d.url;
}).catch(() => {});

$('find-btn').addEventListener('click', async () => {
    const btn = $('find-btn');
    btn.disabled = true;
    setStatus($('find-status'), 'Looking for your server\\u2026');
    try {
        const resp = await fetch(BASE_URL + '/probe', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: $('url').value })
        });
        const data = await resp.json();
        if (!resp.ok || data.error) {
            setStatus($('find-status'), data.error || 'Could not reach that address.', true);
            btn.disabled = false;
            return;
        }
        serverUrl = data.url;
        $('url').value = data.url;
        setStatus($('find-status'), 'Found ' + data.name + (data.version ? ' (' + data.version + ')' : ''));
        $('step-login').classList.remove('hidden');
        if (data.quick_connect) {
            $('pw-block').classList.add('hidden');
            $('toggle-btn').classList.remove('hidden');
            startQuickConnect();
        } else {
            $('pw-intro').textContent =
                'Quick Connect is switched off on this server, so sign in with your username and password.';
        }
        btn.disabled = false;
    } catch (e) {
        setStatus($('find-status'), 'Error: ' + e.message, true);
        btn.disabled = false;
    }
});

$('toggle-btn').addEventListener('click', () => {
    const usingPassword = !$('pw-block').classList.contains('hidden');
    if (usingPassword) {
        $('pw-block').classList.add('hidden');
        $('qc-block').classList.remove('hidden');
        $('toggle-btn').textContent = 'Use a password instead';
        startQuickConnect();
    } else {
        if (pollTimer) clearInterval(pollTimer);
        $('qc-block').classList.add('hidden');
        $('pw-block').classList.remove('hidden');
        $('toggle-btn').textContent = 'Use Quick Connect instead';
    }
});

async function startQuickConnect() {
    $('qc-block').classList.remove('hidden');
    $('qc-code').textContent = '\\u00b7\\u00b7\\u00b7\\u00b7\\u00b7\\u00b7';
    setStatus($('qc-status'), 'Asking the server for a code\\u2026');
    try {
        const resp = await fetch(BASE_URL + '/start-login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: serverUrl })
        });
        const data = await resp.json();
        if (!resp.ok || data.error) {
            $('qc-code').textContent = '';
            setStatus($('qc-status'), data.error || 'Could not start Quick Connect.', true);
            return;
        }
        $('qc-code').textContent = data.code;
        setStatus($('qc-status'), 'Waiting for you to approve the code\\u2026');
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = setInterval(pollQuickConnect, 3000);
    } catch (e) {
        setStatus($('qc-status'), 'Error: ' + e.message, true);
    }
}

async function pollQuickConnect() {
    try {
        const resp = await fetch(BASE_URL + '/check-login', { method: 'POST' });
        const result = await resp.json();
        if (result.status === 'ok') {
            showDone();
        } else if (result.status === 'expired' || result.status === 'no_pending') {
            clearInterval(pollTimer);
            $('qc-code').textContent = '';
            setStatus($('qc-status'), 'The code expired. Press Find server to get a new one.', true);
        } else if (result.status === 'error') {
            clearInterval(pollTimer);
            $('qc-code').textContent = '';
            setStatus($('qc-status'), result.error || 'Sign-in failed.', true);
        }
    } catch (e) { /* keep polling */ }
}

$('pw-btn').addEventListener('click', async () => {
    const btn = $('pw-btn');
    btn.disabled = true;
    setStatus($('pw-status'), 'Signing in\\u2026');
    try {
        const resp = await fetch(BASE_URL + '/password-login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                url: serverUrl || $('url').value,
                username: $('username').value,
                password: $('password').value
            })
        });
        const data = await resp.json();
        if (resp.ok && data.status === 'ok') {
            showDone();
            return;
        }
        setStatus($('pw-status'), data.error || 'Sign-in failed.', true);
        btn.disabled = false;
    } catch (e) {
        setStatus($('pw-status'), 'Error: ' + e.message, true);
        btn.disabled = false;
    }
});
</script>
</body></html>'''
        return web.Response(text=html, content_type='text/html')

    async def _handle_logout(self, request):
        """HTTP endpoint for logout - called from system.html."""
        await self._logout()
        return web.json_response(
            {'status': 'ok', 'message': 'Logged out'},
            headers=self._cors_headers())


if __name__ == '__main__':
    service = JellyfinService()
    asyncio.run(service.run())
