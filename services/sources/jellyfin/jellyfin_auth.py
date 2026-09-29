"""
Jellyfin authentication.

Two login paths, both driven from the setup page:

  * **Quick Connect** — the phone-friendly one, and the closest analogue
    to Plex's PIN OAuth: the device asks the server for a six-character
    code, the user types it into any Jellyfin client they're already
    signed in to, and the device exchanges the approved secret for a
    token.  Requires the server admin to have Quick Connect enabled.
  * **Username / password** — always available, and the only option when
    Quick Connect is off.

Unlike Plex there is no cloud directory to discover servers through, so
the server URL is always supplied by the user (pre-filled from
``config.json`` → ``jellyfin.url`` when it's set).  Tokens don't expire,
so there's no refresh flow.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jellyfin_api import JellyfinClient, normalise_url
from jellyfin_tokens import load_or_create_device_id, load_tokens, save_tokens

# Shared library (services/)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from lib.config import cfg

log = logging.getLogger('beo-source-jellyfin')


class JellyfinAuth:
    """Manages Jellyfin credentials for the long-running service."""

    def __init__(self):
        self._client = None
        self._token = None
        self._server_url = None
        self._server_name = None
        self._user_id = None
        self._user_name = None
        self._device_id = None

    # ── Session ──

    def load(self):
        """Load tokens from disk and verify them.  True if usable."""
        tokens = load_tokens()
        if not tokens or not tokens.get('access_token'):
            if tokens is not None:
                log.info("Token file exists but incomplete - waiting for setup")
            else:
                log.info("No Jellyfin tokens found - use the setup page to connect")
            return False

        server_url = tokens.get('server_url')
        if not server_url or not isinstance(server_url, str):
            # A token with nowhere to spend it — the setup page has to run
            # again. Returning False keeps the service up in the "not
            # configured" state instead of killing it at startup.
            log.warning("Token file has no server_url - waiting for setup")
            return False

        self._device_id = tokens.get('device_id') or load_or_create_device_id()
        client = JellyfinClient(
            server_url, self._device_id,
            token=tokens['access_token'], user_id=tokens.get('user_id'))

        if not client.verify_token():
            # Reachability and revocation look the same from here, and the
            # service retries on /resync — don't delete anything.
            log.warning("Could not verify Jellyfin token against %s "
                        "(server down, or the token was revoked)",
                        server_url)
            return False

        self._client = client
        self._token = tokens['access_token']
        self._server_url = client.base_url
        self._server_name = tokens.get('server_name') or client.server_name()
        self._user_id = tokens.get('user_id')
        self._user_name = tokens.get('user_name')
        log.info("Jellyfin session restored (user: %s, server: %s)",
                 self._user_name or "unknown", self._server_name)
        return True

    # ── Server discovery ──

    def configured_url(self):
        """Server URL to pre-fill the setup form with."""
        tokens = load_tokens() or {}
        return normalise_url(tokens.get('server_url')
                             or cfg("jellyfin", "url", default="") or "")

    def probe(self, url):
        """Check that ``url`` is a Jellyfin server.  Returns its info dict.

        Tries HTTPS when the user gave HTTP (and vice versa) the way the
        Plex flow does — a reverse-proxied server on the LAN is as likely
        to be one as the other.
        """
        url = normalise_url(url)
        if not url:
            raise ValueError("Enter your Jellyfin server address.")

        candidates = [url]
        if url.startswith("http://"):
            candidates.append(url.replace("http://", "https://", 1))
        else:
            candidates.append(url.replace("https://", "http://", 1))

        last_error = None
        for candidate in candidates:
            try:
                client = JellyfinClient(
                    candidate, self._device_id or load_or_create_device_id(),
                    timeout=8)
                info = client.public_info()
                if info.get("Id") or info.get("ServerName"):
                    log.info("Jellyfin server found at %s (%s %s)", candidate,
                             info.get("ServerName", "?"), info.get("Version", "?"))
                    return candidate, info
                last_error = "responded, but not like a Jellyfin server"
            except Exception as e:
                log.info("No Jellyfin server at %s: %s", candidate, e)
                last_error = "no answer"

        raise ValueError(
            f"No Jellyfin server at {url} ({last_error}). Check the address "
            f"and port — Jellyfin usually runs on 8096.")

    # ── Quick Connect ──

    def quick_connect_available(self, url):
        client = self._anon_client(url)
        return client.quick_connect_enabled()

    def start_quick_connect(self, url):
        """Begin a Quick Connect request.  Returns ``(secret, code)``."""
        client = self._anon_client(url)
        if not client.quick_connect_enabled():
            raise ValueError(
                "Quick Connect is switched off on this server. Enable it in "
                "Jellyfin under Dashboard → General → Quick Connect, or sign "
                "in with your username and password below.")
        result = client.quick_connect_initiate()
        secret, code = result.get("Secret"), result.get("Code")
        if not secret or not code:
            raise ValueError("Jellyfin did not return a Quick Connect code.")
        log.info("Jellyfin Quick Connect started (code: %s)", code)
        return secret, code

    def check_quick_connect(self, url, secret):
        """Poll a pending Quick Connect request.

        Returns True once authorised (credentials are saved), False while
        still waiting.  Raises TimeoutError if the request expired.
        """
        client = self._anon_client(url)
        state = client.quick_connect_state(secret)
        if state is None:
            raise TimeoutError("Quick Connect request expired")
        if not state.get("Authenticated"):
            return False

        result = client.authenticate_with_quick_connect(secret)
        self._store_auth_result(client.base_url, result)
        return True

    # ── Username / password ──

    def login_password(self, url, username, password):
        """Sign in with credentials.  Raises ValueError on bad input."""
        if not username:
            raise ValueError("Enter your Jellyfin username.")
        server_url, _info = self.probe(url)
        client = self._anon_client(server_url)
        try:
            result = client.authenticate_by_name(username, password)
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (401, 403):
                raise ValueError("Wrong username or password.") from e
            raise ValueError(f"Jellyfin login failed: {e}") from e
        self._store_auth_result(client.base_url, result)
        return True

    # ── Internals ──

    def _anon_client(self, url):
        if not self._device_id:
            self._device_id = load_or_create_device_id()
        return JellyfinClient(normalise_url(url), self._device_id)

    def _store_auth_result(self, server_url, result):
        """Persist a successful authentication and open the session."""
        token = result.get("AccessToken")
        user = result.get("User") or {}
        if not token:
            raise ValueError("Jellyfin did not return an access token.")

        client = JellyfinClient(server_url, self._device_id, token=token,
                                user_id=user.get("Id"))
        if not client.has_music_library():
            raise ValueError(
                "This Jellyfin account can't see any music. Add a Music "
                "library in Jellyfin (Dashboard → Libraries) and make sure "
                "the user has access to it.")

        self._client = client
        self._token = token
        self._server_url = client.base_url
        self._server_name = client.server_name()
        self._user_id = user.get("Id")
        self._user_name = user.get("Name")

        save_tokens(
            access_token=token,
            server_url=self._server_url,
            server_name=self._server_name,
            user_id=self._user_id,
            user_name=self._user_name,
            device_id=self._device_id,
        )
        log.info("Jellyfin login complete (user: %s, server: %s)",
                 self._user_name, self._server_name)

    def clear(self):
        """Forget the session (the token file is deleted by the caller)."""
        self._client = None
        self._token = None
        self._server_url = None
        self._server_name = None
        self._user_id = None
        self._user_name = None

    # ── Properties ──

    @property
    def client(self):
        return self._client

    @property
    def token(self):
        return self._token

    @property
    def is_configured(self):
        return self._client is not None

    @property
    def user_name(self):
        return self._user_name

    @property
    def server_url(self):
        return self._server_url

    @property
    def server_name(self):
        return self._server_name
