#!/usr/bin/env python3
"""
BeoSound 5c AirPlay 2 Source (beo-source-airplay)

shairport-sync is the actual AirPlay 2 receiver: it terminates the session
from the iPhone/Mac and writes audio straight into PipeWire through the
PulseAudio backend — the same path go-librespot already uses to reach
beo_tone_sink, so AirPlay audio gets the tone filter chain and the normal
PowerLink/MasterLink output for free.

This service is only the bridge between that daemon and the router.  It reads
shairport-sync's metadata pipe, translates the event stream into router source
states, and publishes track metadata and cover art to the PLAYING view.

AirPlay is the only *push* source on the device: playback starts because
someone picked BeoSound 5c on their phone, not because the user selected a
menu item here.  So the pipe reader — not handle_command — drives
registration, and handle_activate deliberately refuses to claim "playing"
when no sender is connected.

Config (config.json):
    "airplay": { "pipe": "/tmp/shairport-sync-metadata" }

Requires shairport-sync built --with-airplay-2 --with-metadata, plus nqptp.

Port: 8775
"""

import asyncio
import base64
import binascii
import logging
import os
import re
import sys

from aiohttp import web

# Shared library (services/) — must come first so ``lib`` is importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from lib.config import cfg
from lib.endpoints import AIRPLAY_PORT, source_url
from lib.source_base import SourceBase

logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
log = logging.getLogger('beo-source-airplay')

DEFAULT_PIPE = "/tmp/shairport-sync-metadata"

# shairport-sync reports progress as RTP frame counts at the AirPlay rate.
RTP_RATE = 44100

# A pause arriving as part of a seek or a track change is followed almost
# immediately by a resume.  Holding the pause briefly keeps the PLAYING view
# from flickering between states on every skip.
PAUSE_DEBOUNCE = 0.6

# The pipe only exists while shairport-sync is running, and it closes between
# sessions; both are normal, so reopening is a routine loop rather than an error.
PIPE_REOPEN_DELAY = 2.0

# How long to wait after dropping a session before forcing state back to idle,
# if the expected pend never arrives. Long enough for the router's source
# switch to complete first.
DROP_RECONCILE_DELAY = 1.5

# Cover art arrives base64-encoded inside a single item, so the buffer has to
# hold a whole image.  Beyond this something is desynchronised and the buffer
# is dropped rather than grown without bound.
MAX_BUFFER = 8 * 1024 * 1024

DBUS_TIMEOUT = 4.0
DBUS_DEST = "org.gnome.ShairportSync"
DBUS_PATH = "/org/gnome/ShairportSync"
DBUS_IFACE = "org.gnome.ShairportSync"
DBUS_REMOTE_IFACE = "org.gnome.ShairportSync.RemoteControl"

# How often to reconcile playback state against shairport-sync itself.
# pbeg/pfls/prsm on the metadata pipe are one-shot events: they are missed
# entirely when this service restarts mid-stream, which otherwise leaves the
# source stuck on "available" while audio is plainly playing. shairport-sync's
# own PlayerState is authoritative and stays correct even on AirPlay 2 buffered
# streams, where there is no DACP channel at all.
STATE_POLL_INTERVAL = 5.0

# Metadata items are a flat stream of <item> elements.  The <data> element is
# absent for pure event codes (play begin, play end, ...).
_ITEM_RE = re.compile(
    rb"<item>\s*"
    rb"<type>([0-9a-fA-F]{8})</type>\s*"
    rb"<code>([0-9a-fA-F]{8})</code>\s*"
    rb"<length>(\d+)</length>\s*"
    rb"(?:<data encoding=\"base64\">\s*([A-Za-z0-9+/=\s]*?)\s*</data>\s*)?"
    rb"</item>",
    re.DOTALL,
)

# "core" items carry the track metadata (DMAP names).
_CORE_FIELDS = {
    "minm": "title",
    "asar": "artist",
    "asal": "album",
}


def _fourcc(hex_bytes: bytes) -> str:
    """Decode shairport-sync's hex-encoded four-character codes."""
    try:
        return bytes.fromhex(hex_bytes.decode("ascii")).decode("ascii", "replace")
    except (ValueError, UnicodeDecodeError):
        return ""


def _sniff_image(data: bytes) -> str:
    """Cover art arrives without a declared type; PNG and JPEG are what senders send."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return "image/jpeg"


class AirPlayService(SourceBase):
    id = "airplay"
    name = "AirPlay"
    port = AIRPLAY_PORT
    # The audio lands in this device's own PipeWire graph, so as far as the
    # router is concerned this is local playback — even though no beo-player-*
    # service is involved and this source pushes its own metadata.
    player = "local"
    action_map = {
        "play": "toggle",
        "pause": "toggle",
        "go": "toggle",
        "next": "next",
        "prev": "prev",
        "left": "prev",
        "right": "next",
        "up": "next",
        "down": "prev",
        "stop": "stop",
    }

    def __init__(self):
        super().__init__()
        self._pipe = cfg("airplay", "pipe", default=DEFAULT_PIPE)
        self._buf = bytearray()
        self._pending: dict = {}      # metadata bundle being collected
        self._meta = {"title": "", "artist": "", "album": ""}
        self._artwork = b""
        self._artwork_mime = "image/jpeg"
        self._artwork_rev = 0
        self._sender = ""
        self._play_state = "idle"     # idle | playing | paused
        self._pause_task: asyncio.Task | None = None
        self._duration = 0
        self._position = 0
        # Dedupe gate for post_media_update. Senders re-send the same metadata
        # bundle every few seconds, so pushing on every ``mden`` floods the
        # router with identical updates.
        self._last_pushed: tuple | None = None

    # ── Lifecycle ──

    async def on_start(self):
        await self.register("available")
        self._spawn(self._pipe_loop(), name="metadata-pipe")
        self._spawn(self._state_poll_loop(), name="state-poll")
        log.info("Reading shairport-sync metadata from %s", self._pipe)

    async def on_stop(self):
        self._cancel_pause()

    # ── Metadata pipe ──

    async def _pipe_loop(self):
        while True:
            try:
                await self._read_pipe()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Metadata pipe error: %s", e)
            await asyncio.sleep(PIPE_REOPEN_DELAY)

    async def _read_pipe(self):
        """Read one writer-session worth of metadata.

        The FIFO is opened non-blocking so this never stalls the event loop
        while shairport-sync is idle: with no writer, reads simply park on
        readability.  When shairport-sync closes its end we get EOF and the
        caller reopens.
        """
        if not os.path.exists(self._pipe):
            return

        fd = os.open(self._pipe, os.O_RDONLY | os.O_NONBLOCK)
        pipe_file = os.fdopen(fd, "rb", 0)
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), pipe_file)
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break          # writer closed — session over
                self._buf += chunk
                await self._drain_buffer()
        finally:
            transport.close()
            self._buf.clear()

    async def _drain_buffer(self):
        if len(self._buf) > MAX_BUFFER:
            log.warning("Metadata buffer overflow (%d bytes) — dropping", len(self._buf))
            self._buf.clear()
            return

        end = 0
        for m in _ITEM_RE.finditer(bytes(self._buf)):
            end = m.end()
            await self._on_item(m)
        if end:
            del self._buf[:end]

    async def _on_item(self, m: re.Match):
        typ = _fourcc(m.group(1))
        code = _fourcc(m.group(2))
        payload = b""
        if m.group(4):
            try:
                payload = base64.b64decode(m.group(4))
            except (binascii.Error, ValueError):
                log.debug("Undecodable payload for %s/%s", typ, code)

        if typ == "core":
            field = _CORE_FIELDS.get(code)
            if field:
                self._pending[field] = payload.decode("utf-8", "replace")
        elif typ == "ssnc":
            await self._on_ssnc(code, payload)

    async def _on_ssnc(self, code: str, payload: bytes):
        if code == "mdst":            # metadata bundle start
            self._pending = {}
        elif code == "mden":          # metadata bundle end
            await self._flush_metadata()
        elif code == "PICT":
            await self._on_artwork(payload)
        elif code in ("pbeg", "prsm"):
            await self._set_playing(navigate=(code == "pbeg"))
        elif code == "pfls":
            self._schedule_pause()
        elif code == "pend":
            await self._end_playback()
        elif code == "aend":
            await self._end_playback()
        elif code == "snam":
            self._sender = payload.decode("utf-8", "replace")
            log.info("AirPlay sender: %s", self._sender)
        elif code == "prgr":
            await self._on_progress(payload.decode("ascii", "replace"))
        else:
            log.debug("Unhandled ssnc code: %s", code)

    async def _flush_metadata(self):
        if not self._pending:
            return
        self._meta.update(self._pending)
        self._pending = {}
        log.info("Track: %s — %s", self._meta.get("artist", ""), self._meta.get("title", ""))
        if self._play_state in ("playing", "paused"):
            await self._push_media()

    async def _on_artwork(self, payload: bytes):
        if not payload:
            return
        self._artwork = payload
        self._artwork_mime = _sniff_image(payload)
        # The URL carries a revision so the UI refetches instead of showing
        # the previous track's cover from cache.
        self._artwork_rev += 1
        if self._play_state in ("playing", "paused"):
            await self._push_media()

    async def _on_progress(self, text: str):
        """``prgr`` is "start/current/end" in RTP frames."""
        try:
            start, current, end = (int(x) for x in text.split("/"))
        except ValueError:
            return
        if end <= start:
            return
        self._duration = int((end - start) / RTP_RATE)
        self._position = max(0, int((current - start) / RTP_RATE))
        if self._play_state == "playing":
            await self._push_media()

    # ── Playback state ──

    async def _set_playing(self, navigate: bool = False):
        self._cancel_pause()
        was_idle = self._play_state == "idle"
        already_playing = self._play_state == "playing"
        self._play_state = "playing"
        # pbeg/prsm repeat during a session; re-registering an unchanged state
        # just adds router churn.
        if not already_playing:
            await self.register("playing", auto_power=True,
                                navigate=navigate and was_idle)
        await self._push_media()

    def _schedule_pause(self):
        self._cancel_pause()
        self._pause_task = self._spawn(self._delayed_pause(), name="pause-debounce")

    async def _delayed_pause(self):
        await asyncio.sleep(PAUSE_DEBOUNCE)
        if self._play_state != "playing":
            return
        self._play_state = "paused"
        await self.register("paused")
        await self._push_media()

    def _cancel_pause(self):
        if self._pause_task and not self._pause_task.done():
            self._pause_task.cancel()
        self._pause_task = None

    async def _end_playback(self):
        self._cancel_pause()
        if self._play_state == "idle":
            return
        self._play_state = "idle"
        self._meta = {"title": "", "artist": "", "album": ""}
        self._artwork = b""
        self._duration = self._position = 0
        self._last_pushed = None
        await self.register("available")

    async def _push_media(self, reason: str = "track_change", force: bool = False):
        artwork = ""
        if self._artwork:
            artwork = source_url(self.port, f"/artwork?rev={self._artwork_rev}")
        state = "playing" if self._play_state == "playing" else "paused"
        key = (self._meta.get("title", ""), self._meta.get("artist", ""),
               self._meta.get("album", ""), artwork, state,
               self._duration, self._position)
        if not force and key == self._last_pushed:
            return
        self._last_pushed = key
        await self.post_media_update(
            title=self._meta.get("title", ""),
            artist=self._meta.get("artist", ""),
            album=self._meta.get("album", ""),
            artwork=artwork,
            state=state,
            duration=self._duration,
            position=self._position,
            reason=reason,
        )

    # ── Remote control (DACP, proxied by shairport-sync over D-Bus) ──

    async def _busctl(self, *args: str) -> str | None:
        """Run busctl and return stdout, or None if the call failed.

        shairport-sync owns the DACP connection to the sender, so everything
        here goes through its D-Bus interface rather than speaking DACP
        directly.  busctl is used instead of a Python D-Bus binding to keep
        this source dependency-free.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "busctl", "--system", *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as e:
            log.warning("busctl unavailable: %s", e)
            return None
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=DBUS_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            log.warning("busctl %s timed out", " ".join(args))
            return None
        if proc.returncode != 0:
            log.warning("busctl %s failed: %s",
                        " ".join(args), err.decode("utf-8", "replace").strip())
            return None
        return out.decode("utf-8", "replace")

    async def _remote(self, method: str) -> bool:
        """Send a transport command back to the AirPlay sender.

        Only possible while shairport-sync holds a DACP channel to the sender.
        In practice that means an AirPlay 1 / "Realtime" stream: AirPlay 2
        buffered streams carry no DACP-ID or Active-Remote token, so there is
        nothing to talk back to.  The D-Bus method returns success either way,
        which is why availability is checked here instead of trusting the
        call's own result — otherwise every dead button reports "sent".

        Failure is non-fatal: audio and metadata keep working.
        """
        if not await self._remote_available():
            log.info("Remote control unavailable (no DACP channel to the "
                     "sender) — %s not sent", method)
            return False
        out = await self._busctl("call", DBUS_DEST, DBUS_PATH,
                                 DBUS_REMOTE_IFACE, method)
        if out is None:
            return False
        log.info("Remote command sent: %s", method)
        return True

    async def _reconcile_after_drop(self):
        """Backstop for a dropped session that emitted no pend."""
        await asyncio.sleep(DROP_RECONCILE_DELAY)
        await self._end_playback()   # no-op if pend already landed

    async def _drop_session(self) -> bool:
        """Force the AirPlay session to end."""
        out = await self._busctl("call", DBUS_DEST, DBUS_PATH,
                                 DBUS_IFACE, "DropSession")
        if out is None:
            return False
        log.info("AirPlay session dropped")
        return True

    async def _remote_available(self) -> bool:
        """Whether shairport-sync currently holds a controllable sender.

        Preferred over tracking abeg/aend ourselves: those are missed entirely
        when this service restarts mid-session, whereas shairport-sync always
        knows whether it currently holds a channel to talk back on.
        """
        out = await self._busctl("get-property", DBUS_DEST, DBUS_PATH,
                                 DBUS_REMOTE_IFACE, "Available")
        return bool(out) and out.split()[-1].strip() == "true"

    async def _player_state(self) -> str:
        """shairport-sync's own view of the stream: Playing / Paused /
        Stopped / Not Available.  Correct even without a DACP channel."""
        out = await self._busctl("get-property", DBUS_DEST, DBUS_PATH,
                                 DBUS_REMOTE_IFACE, "PlayerState")
        if not out:
            return ""
        m = re.search(r'"([^"]*)"', out)   # busctl prints: s "Playing"
        return m.group(1) if m else ""

    # ── State reconciliation ──

    async def _state_poll_loop(self):
        while True:
            await asyncio.sleep(STATE_POLL_INTERVAL)
            try:
                await self._reconcile_state()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("State reconcile failed: %s", e)

    async def _reconcile_state(self):
        """Pull our state back in line with shairport-sync's.

        Covers the cases the pipe's one-shot events miss: a restart while a
        stream is already running, and senders that pause without emitting
        pfls.  A pause already being debounced is left alone so this does not
        race _delayed_pause.
        """
        state = await self._player_state()
        if state == "Playing" and self._play_state != "playing":
            log.info("Reconcile: shairport is playing, adopting stream")
            await self._set_playing()
        elif state == "Paused" and self._play_state == "playing":
            if self._pause_task and not self._pause_task.done():
                return
            log.info("Reconcile: shairport is paused")
            self._play_state = "paused"
            await self.register("paused")
            await self._push_media()
        elif state in ("Stopped", "Not Available") and self._play_state != "idle":
            log.info("Reconcile: shairport has stopped, ending playback")
            await self._end_playback()

    # ── Router interface ──

    async def handle_command(self, cmd: str, data: dict) -> dict:
        if cmd == "toggle":
            return {"sent": await self._remote("PlayPause")}
        if cmd == "next":
            return {"sent": await self._remote("Next")}
        if cmd == "prev":
            return {"sent": await self._remote("Previous")}
        if cmd == "stop":
            # The router forwards stop when another source takes over.  Leave
            # registration alone: the registry is already mid-switch and
            # re-registering here would race its own transition and flash an
            # idle PLAYING view.
            self._cancel_pause()
            if await self._remote("Pause"):
                if self._play_state == "playing":
                    self._play_state = "paused"
                return {"sent": True}
            # The sender cannot be paused, and it does not care that another
            # source is starting — it keeps pushing audio into the same sink,
            # so both sources would play over each other.  Ending the session
            # is the only way to actually silence AirPlay here.
            dropped = await self._drop_session()
            if dropped:
                # Ending the session normally produces pend on the pipe, which
                # reconciles state for us. This is the backstop for when it
                # doesn't — deferred, because registering "available" inline
                # would hit exactly the mid-switch race described above.
                self._spawn(self._reconcile_after_drop(), name="post-drop")
            return {"sent": False, "dropped": dropped}
        return {}

    async def handle_activate(self, data: dict):
        """AIRPLAY selected from the menu.

        Deliberately does not use the base implementation: that registers
        "playing" unconditionally, which for a push source would claim
        playback while no phone is connected.
        """
        if self._play_state == "playing":
            await self.register("playing", auto_power=True)
            await self._push_media(reason="activate", force=True)
            return
        if self._play_state == "paused" and await self._remote_available():
            await self.register("playing", auto_power=True)
            await self._push_media(reason="activate", force=True)
            await self._remote("Play")
            return
        # Nothing is streaming — stay available and show the idle screen.
        await self.register("available")
        await self.post_media_update(
            title=self.name, artist="", album="",
            state="paused", reason="activate")

    async def handle_resync(self) -> dict:
        if self._play_state in ("playing", "paused"):
            await self.register(self._play_state)
            await self._push_media(reason="resync")
        else:
            await self.register("available")
        return {"status": "ok", "resynced": True}

    async def handle_status(self) -> dict:
        return {
            "source": self.id,
            "name": self.name,
            "play_state": self._play_state,
            "sender": self._sender,
            "pipe": self._pipe,
            "pipe_present": os.path.exists(self._pipe),
            "has_artwork": bool(self._artwork),
            "duration": self._duration,
            "position": self._position,
            **self._meta,
        }

    # ── Cover art ──

    def add_routes(self, app: web.Application):
        app.router.add_get("/artwork", self._handle_artwork)

    async def _handle_artwork(self, request):
        if not self._artwork:
            return web.Response(status=404, headers=self._cors_headers())
        return web.Response(
            body=self._artwork,
            content_type=self._artwork_mime,
            headers={**self._cors_headers(), "Cache-Control": "no-cache"},
        )


if __name__ == "__main__":
    asyncio.run(AirPlayService().run())
