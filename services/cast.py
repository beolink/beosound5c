#!/usr/bin/env python3
"""
BeoSound 5c screen mirroring (beo-cast)

Shows the BeoSound's own interface on a Google Cast receiver — a Chromecast,
a Google TV, or a television with Cast built in.

It does not stream pixels. The interface is already a web page, so the
receiver is told to open it and renders it itself:

    beo-cast ──(Cast launch)──▶ receiver ──(HTTP)──▶ beo-http ──▶ the UI

That is not the obvious design, so it is worth saying why. Screen capture
would mean encoding H.264 on a Pi 5, which has no hardware encoder — the Pi 4
did, the 5 dropped it — and then delivering it as HLS, which costs seconds of
latency. Handing over a URL costs nothing, arrives instantly, and renders at
the receiver's own resolution instead of upscaling a 1024x768 panel.

It is a second client rather than a mirror in the strict sense. That turns out
to be what you want: beo-input broadcasts wheel and button events to every
connected browser, so the receiver follows the panel as it is used, and
js/ui-scale.js fits the 4:3 composition to whatever shape the receiver has.

Config (config.json):
    "cast": {
        "receiver": "Living Room TV",   # friendly name; null = ask each time
        "auto_start": false,            # re-cast on service start
        "url": null,                    # default: this device's own UI
        "app_id": "84912283"            # DashCast, loads an arbitrary URL
    }

Port: 8784
"""

import asyncio
import json
import logging
import signal
import socket
import time

from aiohttp import web

# No sys.path juggling: this module sits directly in services/, so Python
# already has its directory on sys.path and lib/ imports as it stands.
from lib.background_tasks import BackgroundTaskSet
from lib.config import cfg
from lib.correlation import install_logging
from lib.endpoints import CAST_PORT
from lib.watchdog import watchdog_loop

log = install_logging("beo-cast")

# DashCast: a published receiver that loads any URL you hand it. Using it
# means no Cast developer registration, which would otherwise be a paid
# prerequisite for showing your own page on your own television.
DEFAULT_APP_ID = "84912283"

# Discovery walks mDNS and is slow by nature; receivers also drop off the
# network entirely when they sleep, so a miss is normal rather than an error.
DISCOVERY_TIMEOUT = 20.0

# How often to check that the receiver is still showing us. A television
# switched to another input, or woken from standby, comes back with a
# different app running.
RECONCILE_INTERVAL = 30.0

# Launching DashCast on top of a running app intermittently lands on the
# default receiver instead, and then its namespace is not there to send the
# URL to. Quitting first and retrying clears it. Measured 2 failures in 5
# naive attempts, none once the quit-and-retry was in place.
LAUNCH_ATTEMPTS = 4
LAUNCH_BACKOFF = 5.0


def _local_address_towards(host: str) -> str | None:
    """This device's address on the interface that reaches ``host``.

    The receiver has to fetch the UI over the network, so "localhost" is no
    use to it. Opening a UDP socket binds a route without sending anything,
    which picks the right source address on a device with more than one.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((host, 9))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError as e:
        log.warning("Could not work out our address towards %s: %s", host, e)
        return None


class CastScreen:
    def __init__(self):
        self._app_id = str(cfg("cast", "app_id", default=DEFAULT_APP_ID)
                           or DEFAULT_APP_ID)
        self._configured = cfg("cast", "receiver", default=None)
        self._url_override = cfg("cast", "url", default=None)
        # Wanted and confirmed are not the same thing. A receiver that is
        # asleep at boot cannot be cast to yet, but it is still where the UI
        # belongs, and the reconcile loop needs to know that to keep trying.
        self._target: str | None = None       # where the UI belongs
        self._active = False                  # and whether it got there
        self._receivers: list[dict] = []
        self._last_scan = 0.0
        self._last_error: str | None = None
        self._runner: web.AppRunner | None = None
        self._tasks = BackgroundTaskSet(log, label="cast")
        self._lock = asyncio.Lock()

    # ── pychromecast lives on threads ──
    #
    # The library is synchronous and starts its own zeroconf browser, so every
    # call goes through the executor rather than blocking the event loop.

    @staticmethod
    def _discover_sync(timeout: float):
        import pychromecast
        casts, browser = pychromecast.get_chromecasts(timeout=timeout)
        try:
            return [{
                "name": c.cast_info.friendly_name,
                "model": c.cast_info.model_name,
                "host": str(c.cast_info.host),
            } for c in casts]
        finally:
            browser.stop_discovery()

    def _cast_sync(self, receiver: str, url: str) -> str:
        import pychromecast
        from pychromecast.controllers.dashcast import DashCastController

        casts, browser = pychromecast.get_chromecasts(timeout=DISCOVERY_TIMEOUT)
        try:
            tv = next((c for c in casts
                       if c.cast_info.friendly_name == receiver), None)
            if tv is None:
                raise LookupError(f"{receiver!r} is not answering — asleep?")
            tv.wait(timeout=DISCOVERY_TIMEOUT)
            last = None
            for attempt in range(1, LAUNCH_ATTEMPTS + 1):
                try:
                    tv.quit_app()
                    time.sleep(LAUNCH_BACKOFF)
                    tv.wait(timeout=DISCOVERY_TIMEOUT)
                    controller = DashCastController()
                    tv.register_handler(controller)
                    controller.load_url(url, force=True)
                    return url
                except Exception as e:      # noqa: BLE001 — retried below
                    last = e
                    log.info("Cast attempt %d/%d failed: %s",
                             attempt, LAUNCH_ATTEMPTS, e)
                    time.sleep(LAUNCH_BACKOFF)
            raise RuntimeError(f"could not launch on {receiver!r}: {last}")
        finally:
            browser.stop_discovery()

    @staticmethod
    def _showing_sync(receiver: str, app_id: str) -> bool:
        import pychromecast
        casts, browser = pychromecast.get_chromecasts(timeout=DISCOVERY_TIMEOUT)
        try:
            tv = next((c for c in casts
                       if c.cast_info.friendly_name == receiver), None)
            if tv is None:
                return False
            tv.wait(timeout=DISCOVERY_TIMEOUT)
            return tv.status is not None and tv.status.app_id == app_id
        except Exception:                   # noqa: BLE001 — treated as "no"
            return False
        finally:
            browser.stop_discovery()

    @staticmethod
    def _stop_sync(receiver: str) -> None:
        import pychromecast
        casts, browser = pychromecast.get_chromecasts(timeout=DISCOVERY_TIMEOUT)
        try:
            tv = next((c for c in casts
                       if c.cast_info.friendly_name == receiver), None)
            if tv is not None:
                tv.wait(timeout=DISCOVERY_TIMEOUT)
                tv.quit_app()
        finally:
            browser.stop_discovery()

    # ── what to show ──

    def _ui_url(self, receiver_host: str | None) -> str:
        if self._url_override:
            return str(self._url_override)
        addr = _local_address_towards(receiver_host) if receiver_host else None
        # The cache-buster is not politeness: DashCast ignores a load for a URL
        # it is already showing, so without it a re-cast after a UI update is
        # a no-op and the receiver keeps the stale page.
        return f"http://{addr or socket.gethostname()}/?cast={int(time.time())}"

    def _host_of(self, receiver: str) -> str | None:
        for r in self._receivers:
            if r["name"] == receiver:
                return r["host"]
        return None

    # ── operations ──

    async def refresh(self) -> list[dict]:
        loop = asyncio.get_running_loop()
        self._receivers = await loop.run_in_executor(
            None, self._discover_sync, DISCOVERY_TIMEOUT)
        self._last_scan = time.monotonic()
        log.info("Found %d Cast receiver(s): %s", len(self._receivers),
                 ", ".join(r["name"] for r in self._receivers) or "none")
        return self._receivers

    async def start(self, receiver: str) -> None:
        async with self._lock:
            # Recorded before the attempt, so a receiver that is asleep now
            # is still picked up by the reconcile loop when it wakes.
            self._target = receiver
            self._active = False
            if not any(r["name"] == receiver for r in self._receivers):
                await self.refresh()
            url = self._ui_url(self._host_of(receiver))
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._cast_sync, receiver, url)
            self._active = True
            self._last_error = None
            log.info("Mirroring to %r: %s", receiver, url)

    async def stop(self) -> None:
        async with self._lock:
            receiver, self._target = self._target, None
            self._active = False
            if not receiver:
                return
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._stop_sync, receiver)
            log.info("Stopped mirroring to %r", receiver)

    async def _reconcile_loop(self):
        """Put the UI back if the receiver stopped showing it.

        A television that was switched to another input, or that slept and
        woke, comes back running something else. Nothing announces that, so
        it has to be noticed by looking.
        """
        while True:
            await asyncio.sleep(RECONCILE_INTERVAL)
            if not self._target:
                continue
            try:
                loop = asyncio.get_running_loop()
                showing = await loop.run_in_executor(
                    None, self._showing_sync, self._target, self._app_id)
                if not showing:
                    log.info("%r is not showing the UI — casting",
                             self._target)
                    await self.start(self._target)
                else:
                    self._active = True
            except Exception as e:          # noqa: BLE001 — retried next tick
                self._last_error = str(e)
                log.info("Reconcile failed (will retry): %s", e)

    # ── HTTP ──

    @staticmethod
    def _cors():
        return {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        }

    async def _handle_status(self, request):
        if request.method == "OPTIONS":
            return web.Response(headers=self._cors())
        return web.json_response({
            "casting": self._active,
            "wanted": self._target,
            "receiver": self._target if self._active else None,
            "configured": self._configured,
            "receivers": self._receivers,
            "last_scan_age": (round(time.monotonic() - self._last_scan, 1)
                              if self._last_scan else None),
            "error": self._last_error,
        }, headers=self._cors())

    async def _handle_receivers(self, request):
        if request.method == "OPTIONS":
            return web.Response(headers=self._cors())
        try:
            await self.refresh()
        except Exception as e:              # noqa: BLE001 — reported to caller
            self._last_error = str(e)
            return web.json_response({"error": str(e), "receivers": []},
                                     status=503, headers=self._cors())
        return web.json_response({"receivers": self._receivers},
                                 headers=self._cors())

    async def _handle_cast(self, request):
        if request.method == "OPTIONS":
            return web.Response(headers=self._cors())
        try:
            body = await request.json()
        except Exception:                   # noqa: BLE001 — empty body is fine
            body = {}
        receiver = body.get("receiver") or self._configured
        if not receiver:
            return web.json_response(
                {"error": "no receiver given and none configured"},
                status=400, headers=self._cors())
        try:
            await self.start(str(receiver))
        except Exception as e:              # noqa: BLE001 — reported to caller
            self._last_error = str(e)
            log.warning("Cast to %r failed: %s", receiver, e)
            return web.json_response({"error": str(e)}, status=502,
                                     headers=self._cors())
        return web.json_response({"casting": self._active,
                                  "receiver": self._target},
                                 headers=self._cors())

    async def _handle_stop(self, request):
        if request.method == "OPTIONS":
            return web.Response(headers=self._cors())
        try:
            await self.stop()
        except Exception as e:              # noqa: BLE001 — reported to caller
            self._last_error = str(e)
            return web.json_response({"error": str(e)}, status=502,
                                     headers=self._cors())
        return web.json_response({"casting": False}, headers=self._cors())

    # ── lifecycle ──

    async def run(self):
        app = web.Application()
        for path, handler in (("/status", self._handle_status),
                              ("/receivers", self._handle_receivers),
                              ("/cast", self._handle_cast),
                              ("/stop", self._handle_stop)):
            app.router.add_get(path, handler)
            app.router.add_post(path, handler)
            app.router.add_options(path, handler)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, "0.0.0.0", CAST_PORT).start()
        log.info("HTTP API on port %d (app_id=%s)", CAST_PORT, self._app_id)

        self._tasks.spawn(watchdog_loop(), name="watchdog")
        self._tasks.spawn(self._reconcile_loop(), name="reconcile")

        if self._configured and cfg("cast", "auto_start", default=False):
            self._tasks.spawn(self._auto_start(), name="auto_start")

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)
        try:
            await stop_event.wait()
        finally:
            await self._tasks.cancel_all()
            if self._runner:
                await self._runner.cleanup()

    async def _auto_start(self):
        """Cast on boot, without holding up the rest of start-up.

        The receiver may well be asleep when the BeoSound comes up, so a
        failure here is logged and left to the reconcile loop.
        """
        try:
            await self.start(str(self._configured))
        except Exception as e:              # noqa: BLE001 — reconcile retries
            self._last_error = str(e)
            log.info("Auto-start could not reach %r yet: %s",
                     self._configured, e)


if __name__ == "__main__":
    logging.getLogger("pychromecast").setLevel(logging.WARNING)
    logging.getLogger("zeroconf").setLevel(logging.WARNING)
    asyncio.run(CastScreen().run())
