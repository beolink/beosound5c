"""
B&O Mozart volume adapter — controls volume and power via the Mozart local
REST API (B&O's public OpenAPI, mozart-open-api).

Real Mozart products serve ``/api/v1`` on port **80** (the official
``mozart-api`` client and Home Assistant's ``bang_olufsen`` integration both
assume it), no auth on the LAN.

  GET /api/v1/sound/volume        -> VolumeState {level:{level}, muted:{muted}, maximum:{level}, default:{level}}
  PUT /api/v1/sound/volume/level  <- VolumeLevel {"level": 0-100}
  GET /api/v1/state/power         -> PowerStateEnum {"value": on|networkStandby|standby|shutdown|storage}
  PUT /api/v1/state/power         <- {"value": "on"}
  PUT /api/v1/state/standby       -> network standby

(There is no GET on /sound/volume/level — only PUT — so the level is read
from the VolumeState.)

EXPERIMENTAL — request/response shapes are per the OpenAPI and the
decompiled B&O app; not yet validated on hardware.
"""

import asyncio
import logging

import aiohttp

from .base import VolumeAdapter

logger = logging.getLogger("beo-router.volume.mozart")

MOZART_PORT = 80
_POWER_CACHE_TTL = 30.0  # seconds — avoid a round-trip on every wheel tick


class MozartVolume(VolumeAdapter):
    def __init__(self, ip: str, max_volume: int, session: aiohttp.ClientSession):
        super().__init__(max_volume, debounce_ms=50)
        self._session = session
        self._base = f"http://{ip}:{MOZART_PORT}/api/v1"
        self._power_cache: bool | None = None
        self._power_cache_time: float = 0

    async def _apply_volume(self, volume: float) -> None:
        try:
            async with self._session.put(
                f"{self._base}/sound/volume/level",
                json={"level": int(round(volume))},
                timeout=aiohttp.ClientTimeout(total=5)) as resp:
                resp.raise_for_status()
                logger.info("-> Mozart volume: %.0f%%", volume)
        except Exception as e:
            logger.warning("Mozart unreachable for volume set: %s", e)

    async def get_volume(self) -> float | None:
        try:
            async with self._session.get(
                f"{self._base}/sound/volume",
                timeout=aiohttp.ClientTimeout(total=5)) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
                return float(volume_level_from_state(data))
        except Exception as e:
            logger.warning("Could not read Mozart volume: %s", e)
            return None

    # -- Power (network standby) --

    def is_on_cached(self) -> bool | None:
        return self._power_cache

    async def is_on(self) -> bool:
        now = asyncio.get_running_loop().time()
        if self._power_cache is not None and (now - self._power_cache_time) < _POWER_CACHE_TTL:
            return self._power_cache
        try:
            async with self._session.get(
                f"{self._base}/state/power",
                timeout=aiohttp.ClientTimeout(total=2)) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
                self._power_cache = power_is_on(data)
                self._power_cache_time = now
                return self._power_cache
        except Exception as e:
            logger.warning("Could not read Mozart power state: %s", e)
            return self._power_cache if self._power_cache is not None else True

    async def power_on(self) -> None:
        try:
            async with self._session.put(
                f"{self._base}/state/power", json={"value": "on"},
                timeout=aiohttp.ClientTimeout(total=5)) as resp:
                resp.raise_for_status()
                logger.info("Mozart power on: HTTP %d", resp.status)
                self._power_cache = True
                self._power_cache_time = asyncio.get_running_loop().time()
        except Exception as e:
            logger.warning("Could not power on Mozart: %s", e or type(e).__name__)

    async def power_off(self) -> None:
        try:
            async with self._session.put(
                f"{self._base}/state/standby",
                timeout=aiohttp.ClientTimeout(total=5)) as resp:
                resp.raise_for_status()
                logger.info("Mozart standby: HTTP %d", resp.status)
                self._power_cache = False
                self._power_cache_time = asyncio.get_running_loop().time()
        except Exception as e:
            logger.warning("Could not put Mozart in standby: %s", e or type(e).__name__)


def volume_level_from_state(data) -> int:
    """Level out of a VolumeState (``{"level": {"level": n}}``); tolerate a
    bare VolumeLevel (``{"level": n}``)."""
    if not isinstance(data, dict):
        return 0
    lvl = data.get("level", 0)
    if isinstance(lvl, dict):
        lvl = lvl.get("level", 0)
    try:
        return int(lvl)
    except (TypeError, ValueError):
        return 0


def power_is_on(data) -> bool:
    """PowerStateEnum ``{"value": ...}`` -> True only for ``on``."""
    value = data.get("value") if isinstance(data, dict) else data
    return str(value).lower() == "on"
