"""Token store wrapper for Jellyfin credentials.

Persists ``access_token`` + server/user metadata.  Jellyfin access
tokens don't expire on their own (they're revoked from the server's
Devices page), so there's no refresh flow — same shape as Plex.

``device_id`` is part of the payload rather than derived: Jellyfin ties
a token to the device that requested it, so the id has to survive
restarts or the server accumulates a new device entry per boot.
"""

import os
import uuid

from lib.token_store import TokenStore

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_store = TokenStore("jellyfin_tokens.json", dev_dir=SCRIPT_DIR)


def load_tokens():
    return _store.load()


def save_tokens(access_token, server_url, server_name, user_id, user_name,
                device_id):
    return _store.save({
        "access_token": access_token,
        "server_url": server_url,
        "server_name": server_name,
        "user_id": user_id,
        "user_name": user_name,
        "device_id": device_id,
    })


def load_or_create_device_id():
    """Return the persisted device id, creating (and storing) one if absent.

    Called before the first login, when there is no token file yet — the
    id has to exist in time for the ``Authorization`` header, so it is
    merged into the store immediately rather than waiting for tokens.
    """
    tokens = _store.load() or {}
    device_id = tokens.get("device_id")
    if device_id:
        return device_id
    device_id = uuid.uuid4().hex
    try:
        _store.save_merge({"device_id": device_id})
    except OSError:
        # Not fatal — a login can still complete with an ephemeral id;
        # it just registers a fresh device on the Jellyfin server.
        pass
    return device_id


def delete_tokens():
    return _store.delete()
