"""Regression tests for the Jellyfin session-restore boundary.

``JellyfinAuth.load()`` runs on every service start, from ``on_start()``'s
executor. Anything it raises propagates out of startup, and with
``Restart=on-failure`` plus ``StartLimitBurst=3`` in the unit file that
means three restarts and then a dead service — from a bad file on disk
rather than a bad server.

The Plex source this was ported from wraps the whole restore in
try/except for exactly that reason. These pin the same contract here: a
token file that is present but not usable yields False (service stays up,
setup page waits), never an exception.
"""

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
JELLYFIN_DIR = REPO_ROOT / "services" / "sources" / "jellyfin"


@pytest.fixture
def auth_module(monkeypatch, tmp_path):
    """Import jellyfin_auth with its token store redirected at tmp_path."""
    for p in (str(JELLYFIN_DIR), str(REPO_ROOT / "services")):
        if p not in sys.path:
            sys.path.insert(0, p)
    for mod in ("jellyfin_auth", "jellyfin_tokens", "jellyfin_api"):
        sys.modules.pop(mod, None)

    import jellyfin_tokens
    monkeypatch.setattr(jellyfin_tokens._store, "_dev_dir", str(tmp_path),
                        raising=False)
    monkeypatch.setattr(jellyfin_tokens._store, "path",
                        lambda: str(tmp_path / "jellyfin_tokens.json"))

    import jellyfin_auth
    return jellyfin_auth, tmp_path / "jellyfin_tokens.json"


def _write(path, payload):
    path.write_text(json.dumps(payload))


def test_token_without_server_url_is_not_usable(auth_module, monkeypatch):
    """The case that used to raise KeyError and take the service down."""
    jellyfin_auth, token_path = auth_module
    _write(token_path, {"access_token": "abc", "device_id": "d1"})
    monkeypatch.setattr(jellyfin_auth, "load_tokens",
                        lambda: json.loads(token_path.read_text()))
    assert jellyfin_auth.JellyfinAuth().load() is False


def test_empty_server_url_is_not_usable(auth_module, monkeypatch):
    jellyfin_auth, token_path = auth_module
    _write(token_path, {"access_token": "abc", "server_url": ""})
    monkeypatch.setattr(jellyfin_auth, "load_tokens",
                        lambda: json.loads(token_path.read_text()))
    assert jellyfin_auth.JellyfinAuth().load() is False


def test_non_string_server_url_is_not_usable(auth_module, monkeypatch):
    """A hand-edited file with e.g. a numeric server_url must not raise
    out of normalise_url() and kill startup."""
    jellyfin_auth, token_path = auth_module
    _write(token_path, {"access_token": "abc", "server_url": 8096})
    monkeypatch.setattr(jellyfin_auth, "load_tokens",
                        lambda: json.loads(token_path.read_text()))
    assert jellyfin_auth.JellyfinAuth().load() is False


def test_non_dict_token_file_is_not_usable(auth_module):
    """A token file holding a bare JSON string (not an object) is filtered
    to None by TokenStore.load(), so load() reports not-configured instead
    of raising AttributeError on tokens.get()."""
    jellyfin_auth, token_path = auth_module
    token_path.write_text('"abc123"')
    assert jellyfin_auth.JellyfinAuth().load() is False


def test_no_token_file_is_not_usable(auth_module, monkeypatch):
    jellyfin_auth, _token_path = auth_module
    monkeypatch.setattr(jellyfin_auth, "load_tokens", lambda: None)
    assert jellyfin_auth.JellyfinAuth().load() is False


def test_unverifiable_token_keeps_the_file(auth_module, monkeypatch):
    """A server that is merely down must not clear stored credentials.

    /resync retries the load later; deleting here would demand a fresh
    sign-in every time the Jellyfin box reboots before the BS5c does.
    """
    jellyfin_auth, token_path = auth_module
    payload = {"access_token": "abc", "server_url": "http://jf.local:8096",
               "device_id": "d1"}
    _write(token_path, payload)
    monkeypatch.setattr(jellyfin_auth, "load_tokens", lambda: dict(payload))
    monkeypatch.setattr(jellyfin_auth.JellyfinClient, "verify_token",
                        lambda self: False)

    assert jellyfin_auth.JellyfinAuth().load() is False
    assert os.path.exists(token_path)
