"""Minimal Jellyfin REST client.

Jellyfin has no maintained first-party Python client, and the handful of
endpoints this source needs (login, playlists, albums, stream URLs) is
small enough that a third-party library would be more dependency than
value.  Everything here is synchronous ``requests``: the service calls
it from an executor, ``fetch.py`` runs it in its own process — the same
split Plex's ``plexapi`` usage has.

Version compatibility: Jellyfin moved user-scoped library queries from
``/Users/{userId}/Items`` to ``/Items?userId=`` around 10.9 and removed
the old path later, so :meth:`JellyfinClient.user_items` tries the new
shape first and falls back on 404.
"""

from __future__ import annotations

import os
import urllib.parse

import requests
import urllib3

# Self-hosted Jellyfin behind a reverse proxy often has a self-signed
# certificate; the same trade-off Plex makes on the LAN.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CLIENT_NAME = "BeoSound 5c"
DEVICE_NAME = "BeoSound 5c"

# Containers a networked player (Sonos, BlueSound, HEOS) or local mpv can
# take as-is; anything else the server transcodes to MP3 on the fly.
DIRECT_PLAY_CONTAINERS = "mp3,aac,m4a,flac,wav,ogg"
TRANSCODE_CONTAINER = "mp3"
# /universal only direct-plays a file whose bitrate is under this cap, so
# it has to sit far above lossless rates (WAV is ~1.4 Mbps) or every
# FLAC/WAV gets transcoded to lossy MP3 despite the Container list.
# Jellyfin's own clients pass the same value and let the list decide.
MAX_STREAMING_BITRATE = 140000000

DEFAULT_TIMEOUT = 15


def client_version() -> str:
    """Project version string, used in the Jellyfin device list."""
    root = os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
    try:
        with open(os.path.join(root, "VERSION")) as f:
            return f.read().strip().lstrip("v") or "0"
    except OSError:
        return "0"


def normalise_url(url: str) -> str:
    """Accept what a user types and return a usable base URL.

    ``jellyfin.local:8096`` and ``http://jellyfin.local:8096/`` both have
    to work — the setup page is filled in on a phone.
    """
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    return url


def auth_header(token: str | None, device_id: str) -> str:
    """Build the ``Authorization: MediaBrowser ...`` header value."""
    parts = [
        f'Client="{CLIENT_NAME}"',
        f'Device="{DEVICE_NAME}"',
        f'DeviceId="{device_id}"',
        f'Version="{client_version()}"',
    ]
    if token:
        parts.append(f'Token="{token}"')
    return "MediaBrowser " + ", ".join(parts)


class JellyfinError(Exception):
    """An API call failed in a way the caller should surface to the user."""


class JellyfinClient:
    """Thin wrapper over the Jellyfin HTTP API for one server + user."""

    def __init__(self, base_url, device_id, token=None, user_id=None,
                 timeout=DEFAULT_TIMEOUT):
        self.base_url = normalise_url(base_url)
        self.device_id = device_id
        self.token = token
        self.user_id = user_id
        self.timeout = timeout
        self._session = requests.Session()
        self._session.verify = False

    # ── Plumbing ──

    def _headers(self):
        return {
            "Authorization": auth_header(self.token, self.device_id),
            "Accept": "application/json",
        }

    def _request(self, method, path, *, params=None, json=None,
                 timeout=None, raise_for_status=True):
        url = f"{self.base_url}{path}"
        resp = self._session.request(
            method, url, params=params, json=json,
            headers=self._headers(), timeout=timeout or self.timeout)
        if raise_for_status:
            resp.raise_for_status()
        return resp

    def get(self, path, **params):
        return self._request("GET", path, params=params).json()

    def post(self, path, json=None, **params):
        resp = self._request("POST", path, params=params, json=json)
        if not resp.content:
            return {}
        return resp.json()

    # ── Server ──

    def public_info(self):
        """``/System/Info/Public`` — reachable without a token."""
        return self.get("/System/Info/Public")

    def server_name(self):
        try:
            return self.public_info().get("ServerName") or "Jellyfin"
        except Exception:
            return "Jellyfin"

    def verify_token(self):
        """True if the stored token is still accepted by the server."""
        try:
            self.get("/Users/Me")
            return True
        except Exception:
            return False

    # ── Auth ──

    def authenticate_by_name(self, username, password):
        """Username/password login.  Returns the auth result dict."""
        return self.post("/Users/AuthenticateByName",
                         json={"Username": username, "Pw": password or ""})

    def quick_connect_enabled(self):
        """Whether the server has Quick Connect switched on."""
        try:
            return bool(self.get("/QuickConnect/Enabled"))
        except Exception:
            return False

    def quick_connect_initiate(self):
        """Start a Quick Connect request.  Returns ``{Secret, Code}``.

        10.9 made this a POST; 10.8 only answers GET, hence the fallback.
        """
        try:
            return self.post("/QuickConnect/Initiate")
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status not in (404, 405):
                raise
            return self.get("/QuickConnect/Initiate")

    def quick_connect_state(self, secret):
        """Poll a pending Quick Connect request.

        Returns the state dict, or None once the server has forgotten the
        request (it expires after a few minutes).
        """
        resp = self._request("GET", "/QuickConnect/Connect",
                             params={"secret": secret},
                             raise_for_status=False)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def authenticate_with_quick_connect(self, secret):
        """Exchange an authorised Quick Connect secret for a token."""
        return self.post("/Users/AuthenticateWithQuickConnect",
                         json={"Secret": secret})

    # ── Library ──

    def user_items(self, **params):
        """``/Items`` scoped to the logged-in user, across API versions."""
        params = {k: v for k, v in params.items() if v is not None}
        params["userId"] = self.user_id
        resp = self._request("GET", "/Items", params=params,
                             raise_for_status=False)
        if resp.status_code == 404 and self.user_id:
            # Pre-10.9 server: the user-scoped path is the only one there.
            params.pop("userId", None)
            resp = self._request("GET", f"/Users/{self.user_id}/Items",
                                 params=params)
        resp.raise_for_status()
        return resp.json().get("Items", [])

    def audio_playlists(self):
        """Audio playlists visible to the user, alphabetical."""
        items = self.user_items(
            IncludeItemTypes="Playlist", Recursive="true",
            SortBy="SortName", SortOrder="Ascending",
            Fields="ChildCount,DateCreated,DateLastMediaAdded")
        # MediaType is absent on some server versions — keep those rather
        # than silently dropping the user's whole playlist list.
        return [i for i in items
                if i.get("MediaType") in (None, "", "Audio")]

    def playlist_items(self, playlist_id):
        params = {"Fields": "PrimaryImageAspectRatio"}
        if self.user_id:
            params["userId"] = self.user_id
        resp = self._request("GET", f"/Playlists/{playlist_id}/Items",
                             params=params, timeout=60)
        return resp.json().get("Items", [])

    def recent_albums(self, limit=50):
        return self.user_items(
            IncludeItemTypes="MusicAlbum", Recursive="true",
            SortBy="DateCreated", SortOrder="Descending", Limit=limit,
            Fields="ChildCount,DateCreated")

    def album_tracks(self, album_id):
        return self.user_items(
            ParentId=album_id,
            SortBy="ParentIndexNumber,IndexNumber,SortName",
            SortOrder="Ascending")

    def has_music_library(self):
        """True if the user can see any music at all."""
        try:
            return bool(self.user_items(
                IncludeItemTypes="MusicAlbum,Audio", Recursive="true", Limit=1))
        except Exception:
            return False

    # ── URLs ──

    def stream_url(self, item_id):
        """A direct HTTP URL the player can hand to its own decoder.

        ``/universal`` lets the server decide: direct-play when the file
        is already in a container the player understands, transcode to
        MP3 when it isn't.  The token rides along as ``api_key`` because
        players fetch this URL themselves and can't set headers.
        """
        params = {
            "UserId": self.user_id or "",
            "DeviceId": self.device_id,
            "api_key": self.token or "",
            "MaxStreamingBitrate": MAX_STREAMING_BITRATE,
            "Container": DIRECT_PLAY_CONTAINERS,
            "TranscodingContainer": TRANSCODE_CONTAINER,
            "TranscodingProtocol": "http",
            "AudioCodec": "mp3",
        }
        query = urllib.parse.urlencode(params)
        return f"{self.base_url}/Audio/{item_id}/universal?{query}"

    def image_url(self, item, max_height=500):
        """Primary artwork URL for an item, or None when it has none.

        Falls back to the parent album's image so a track ripped without
        embedded art still shows the album cover.  Jellyfin serves images
        unauthenticated, so no token is needed here.
        """
        tags = item.get("ImageTags") or {}
        primary = tags.get("Primary")
        if primary:
            item_id = item.get("Id")
        elif item.get("AlbumPrimaryImageTag") and item.get("AlbumId"):
            primary = item["AlbumPrimaryImageTag"]
            item_id = item["AlbumId"]
        elif item.get("ParentPrimaryImageTag") and item.get("ParentPrimaryImageItemId"):
            primary = item["ParentPrimaryImageTag"]
            item_id = item["ParentPrimaryImageItemId"]
        else:
            return None
        query = urllib.parse.urlencode(
            {"maxHeight": max_height, "tag": primary})
        return f"{self.base_url}/Items/{item_id}/Images/Primary?{query}"


def track_artist(item):
    """Best available artist name for an audio item."""
    artists = item.get("Artists") or []
    if artists:
        return artists[0]
    if item.get("AlbumArtist"):
        return item["AlbumArtist"]
    for entry in item.get("ArtistItems") or []:
        if entry.get("Name"):
            return entry["Name"]
    return "Unknown"
