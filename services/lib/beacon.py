"""Startup beacon — a tiny anonymous "hello" to beosound5c.com.

Why this exists: it is genuinely delightful to watch old BeoSound 5s come
back to life around the world, and every new dot on that map makes the
developer's day. That's the entire reason. There is no analytics platform
behind this, no tracking across services, and nothing is ever shared or
sold.

What is sent (one POST per service startup — everything is listed here):

  device_id    a random UUID4, made up on the device the first time it
               pings and kept in a `device_id` file next to this code. It
               is not derived from any hardware identifier — nothing about
               the machine can be worked out from it. Re-image the SD card
               and you get a new one; that just means one extra dot on the
               map, which is a fine trade.
  version      software version string (e.g. "v0.9.2")
  sources      names of enabled sources (e.g. "spotify", "cd") — names
               only, never credentials or config values
  player_type  "sonos", "bluesound", "heos", or "local"
  volume_type  volume adapter name (e.g. "beolab5", "powerlink")

The server infers a country from the request IP (via Cloudflare) and keeps
only the country name. What is never sent: hostnames, device names,
account names, credentials, IPs from config, or anything about what you
listen to.

Nothing is sent before the owner has been asked.  The first ping waits for
``setup_complete`` in config.json, which is written when the web setup screen
is saved — the same screen that carries the "Anonymous startup ping" toggle.
A device that is installed but never set up never phones home.

Opting out is one command and everything else works exactly the same:

    touch ~/beosound5c/NO_TELEMETRY

(The web config UI has a toggle for this — unchecking "Anonymous startup
ping" creates this file for you.)
"""

import json
import logging
import os
import uuid

import aiohttp

logger = logging.getLogger('beacon')

BEACON_URL = 'https://beosound5c.com/api/beacon'

# Older versions derived the id as uuid5(_LEGACY_NAMESPACE, onboard MAC),
# which is reversible — the MAC keyspace is small enough to brute-force
# against this (public) source.  Devices still holding such an id swap it for
# a random one on next startup.  Safe to delete once the fleet has rolled over.
_LEGACY_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, 'beosound5c.com')
_LEGACY_IFACES = ('eth0', 'wlan0')

_KNOWN_SYSTEM_KEYS = frozenset({
    'device', 'menu', 'scenes', 'player', 'volume',
    'home_assistant', 'transport', 'showing', 'join',
    'bluetooth', 'remote',
})


def _is_legacy_mac_derived(device_id: str) -> bool:
    """True if this id is the old uuid5-of-our-own-MAC value.

    Only ever matches on the machine the id was derived from, so it cannot
    misfire on a random id that happens to belong to another device.
    """
    for iface in _LEGACY_IFACES:
        try:
            with open(f'/sys/class/net/{iface}/address') as f:
                mac = f.read().strip().lower()
        except OSError:
            continue
        if mac and mac != '00:00:00:00:00:00' \
                and device_id == str(uuid.uuid5(_LEGACY_NAMESPACE, mac)):
            return True
    return False


def _get_or_create_device_id(base_path: str) -> str:
    id_file = os.path.join(base_path, 'device_id')
    try:
        if os.path.isfile(id_file):
            existing = open(id_file).read().strip()
            if existing:
                try:
                    uuid.UUID(existing)
                except ValueError:
                    # Corrupted file (e.g. truncated by a power cut) — fall
                    # through and mint a new one.  The device shows up as a
                    # second dot on the map; that is the whole cost.
                    logger.debug('Invalid device_id %r — replacing', existing)
                else:
                    if not _is_legacy_mac_derived(existing):
                        return existing
                    logger.info('Replacing MAC-derived device_id with a random one')

        device_id = str(uuid.uuid4())
        with open(id_file, 'w') as f:
            f.write(device_id + '\n')
        return device_id
    except Exception as e:
        logger.debug('device_id file unavailable: %s', e)
        return 'unknown'


def _build_payload(base_path: str) -> dict:
    # Version
    try:
        version = open(os.path.join(base_path, 'VERSION')).read().strip()
    except Exception:
        version = 'unknown'

    # Sources: top-level config keys that aren't system sections
    try:
        from lib.config import load_config
        config = load_config()
        sources = [k for k, v in config.items()
                   if k not in _KNOWN_SYSTEM_KEYS and isinstance(v, dict)]
        player_type = (config.get('player') or {}).get('type', 'unknown')
        volume_type = (config.get('volume') or {}).get('type', 'unknown')
    except Exception:
        sources = []
        player_type = 'unknown'
        volume_type = 'unknown'

    return {
        'device_id':   _get_or_create_device_id(base_path),
        'version':     version,
        'sources':     sources,
        'player_type': player_type,
        'volume_type': volume_type,
    }


def _setup_complete() -> bool:
    """True once the owner has saved the web setup screen.

    Until then they have not seen the telemetry toggle, so there is no
    consent to act on and nothing is sent.  Unreadable config counts as
    not-set-up: staying quiet is the safe direction.
    """
    try:
        from lib.config import load_config
        return bool(load_config().get('setup_complete'))
    except Exception as e:
        logger.debug('Could not read setup_complete: %s', e)
        return False


async def send_beacon(base_path: str) -> None:
    """Fire-and-forget. Logs result at DEBUG; never raises."""
    try:
        opt_out = os.path.join(base_path, 'NO_TELEMETRY')
        if os.path.isfile(opt_out):
            logger.debug('Telemetry disabled (NO_TELEMETRY file present)')
            return

        if not _setup_complete():
            logger.debug('Setup not finished — no beacon until the owner has '
                         'seen the telemetry toggle')
            return

        payload = _build_payload(base_path)

        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(BEACON_URL, json=payload) as resp:
                body = await resp.json()
                logger.info(
                    'Beacon sent — version=%s country=%s',
                    payload['version'], body.get('country', '?'),
                )
    except Exception as e:
        logger.debug('Beacon failed (non-fatal): %s', e)
