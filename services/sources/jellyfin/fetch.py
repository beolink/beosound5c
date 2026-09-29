#!/usr/bin/env python3
"""
Fetch all Jellyfin audio playlists and recent albums for the authenticated
user.  Auto-detects digit playlists by name pattern (e.g. "5: Dinner" ->
digit 5).  Run via the beo-source-jellyfin service to keep playlists
updated.

Token source: --token-file <path> pointing to jellyfin_tokens.json.
"""

import json
import os
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, '..', '..', '..'))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'services'))
sys.path.insert(0, SCRIPT_DIR)

from jellyfin_api import JellyfinClient, track_artist
from lib.digit_playlists import detect_digit_playlist, build_digit_mapping

# service.py resolves these through BS5C_BASE_PATH too — both processes
# have to agree on where the digit map lives or the service reads a stale
# file the fetch never writes.
WEB_JSON = os.path.join(os.getenv('BS5C_BASE_PATH', PROJECT_ROOT), 'web', 'json')
DIGIT_PLAYLISTS_FILE = os.path.join(WEB_JSON, 'jellyfin_digit_playlists.json')
DEFAULT_OUTPUT_FILE = os.path.join(WEB_JSON, 'jellyfin_playlists.json')

RECENT_ALBUM_LIMIT = 50


def log(msg):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] {msg}")


def change_signature(item):
    """A value that changes when a playlist's or album's contents change.

    Jellyfin has no per-playlist "updated at" the way Plex does, so the
    cache key is built from what it does expose: when the container was
    created / last gained media, plus how many children it has.  Adding,
    removing or reordering-by-count all move it; an in-place rename of a
    track does not, which the nightly full refresh eventually catches.
    """
    stamp = (item.get('DateLastMediaAdded') or item.get('DateCreated') or '')
    return f"{stamp}:{item.get('ChildCount', '')}"


def convert_track(client, item, fallback_artist=None, fallback_image=None):
    artist = track_artist(item)
    if artist == 'Unknown' and fallback_artist:
        artist = fallback_artist
    return {
        'name': item.get('Name') or 'Unknown',
        'artist': artist,
        'id': str(item.get('Id')),
        'url': client.stream_url(item.get('Id')),
        'image': client.image_url(item) or fallback_image,
    }


def fetch_playlist_tracks(client, playlist_id):
    try:
        items = client.playlist_items(playlist_id)
    except Exception as e:
        log(f"  Error fetching tracks: {e}")
        return []
    return [convert_track(client, t) for t in items
            if t.get('MediaType') in (None, '', 'Audio')]


def fetch_album_tracks(client, album):
    try:
        items = client.album_tracks(album.get('Id'))
    except Exception as e:
        log(f"  Error fetching album tracks: {e}")
        return []
    album_artist = album.get('AlbumArtist') or 'Unknown'
    album_image = client.image_url(album)
    return [convert_track(client, t, fallback_artist=album_artist,
                          fallback_image=album_image)
            for t in items if t.get('MediaType') in (None, '', 'Audio')]


def main():
    force = '--force' in sys.argv

    output_file = DEFAULT_OUTPUT_FILE
    if '--output' in sys.argv:
        idx = sys.argv.index('--output')
        if idx + 1 < len(sys.argv):
            output_file = sys.argv[idx + 1]

    token_file = None
    if '--token-file' in sys.argv:
        idx = sys.argv.index('--token-file')
        if idx + 1 < len(sys.argv):
            token_file = sys.argv[idx + 1]

    if not token_file:
        log("ERROR: --token-file is required")
        return 1

    try:
        with open(token_file) as f:
            tokens = json.load(f)
    except Exception as e:
        log(f"ERROR: Could not load token file: {e}")
        return 1

    if not tokens.get('access_token'):
        log("ERROR: No access_token in token file")
        return 1

    log("=== Jellyfin Playlist Fetch Starting ===")
    if force:
        log("Force mode: fetching all tracks regardless of cache")

    client = JellyfinClient(
        tokens['server_url'], tokens.get('device_id', ''),
        token=tokens['access_token'], user_id=tokens.get('user_id'),
        timeout=30)
    # public_info() raises on an unreachable server; server_name() swallows
    # everything and would let a fetch-against-nothing continue and replace
    # the cached library with an empty file.
    try:
        info = client.public_info()
    except Exception as e:
        log(f"ERROR: Could not connect to Jellyfin server: {e}")
        return 1
    log(f"Connected to Jellyfin server: {info.get('ServerName') or 'Jellyfin'}")

    # Load cached data for incremental sync
    cache = {}
    if not force and os.path.exists(output_file):
        try:
            with open(output_file, 'r') as f:
                cached_playlists = json.load(f)
            for cp in cached_playlists:
                cache[cp['id']] = {
                    'updatedAt': cp.get('updatedAt', ''),
                    'tracks': cp.get('tracks', []),
                }
            log(f"Loaded cache with {len(cache)} playlists")
            # Stream URLs embed api_key; if the token has been revoked and
            # re-issued the cached URLs 401 and the player stops on the
            # first track. Drop the cache when the first URL we find no
            # longer carries the current token.
            cur_tok = tokens['access_token']
            for _pc in cache.values():
                for _tr in _pc.get('tracks', []):
                    _u = _tr.get('url', '')
                    if _u and cur_tok not in _u:
                        log("Access token changed - invalidating cache for full refresh")
                        cache = {}
                        break
                else:
                    continue
                break
        except Exception as e:
            log(f"Could not load cache: {e}")

    # A failed library read must abort before the disk writes below — an
    # empty result from an error is indistinguishable from a genuinely
    # empty library and would wipe the cached playlists and digit map.
    log("Fetching playlists from Jellyfin server")
    try:
        raw_playlists = client.audio_playlists()
    except Exception as e:
        log(f"ERROR: Could not fetch playlists: {e}")
        return 1
    log(f"Found {len(raw_playlists)} audio playlists")

    log("Fetching recent albums")
    try:
        raw_albums = client.recent_albums(RECENT_ALBUM_LIMIT)
    except Exception as e:
        log(f"ERROR: Could not fetch recent albums: {e}")
        return 1
    log(f"Found {len(raw_albums)} recent albums")

    all_playlists = []
    for pl in raw_playlists:
        all_playlists.append({
            'id': f"playlist:{pl.get('Id')}",
            'name': pl.get('Name') or 'Untitled',
            'image': client.image_url(pl),
            'updatedAt': change_signature(pl),
            '_raw': pl,
            '_type': 'playlist',
        })

    for album in raw_albums:
        artist = album.get('AlbumArtist') or 'Unknown Artist'
        all_playlists.append({
            'id': f"album:{album.get('Id')}",
            'name': f"{album.get('Name') or 'Untitled'}\n({artist})",
            'image': client.image_url(album),
            'updatedAt': change_signature(album),
            '_raw': album,
            '_type': 'album',
        })

    # Split into cached vs needs-fetch
    playlists_with_tracks = []
    to_fetch = []
    skipped = 0

    for pl in all_playlists:
        cached = cache.get(pl['id'])
        if (cached and cached['updatedAt']
                and cached['updatedAt'] == pl.get('updatedAt', '')):
            pl['tracks'] = cached['tracks']
            playlists_with_tracks.append(pl)
            log(f"  {pl['name'].split(chr(10))[0]} (unchanged)")
            skipped += 1
        else:
            to_fetch.append(pl)

    fetched = 0
    if to_fetch:
        log(f"Fetching tracks for {len(to_fetch)} playlists/albums...")
        for pl in to_fetch:
            try:
                raw = pl.pop('_raw', None)
                pl_type = pl.pop('_type', 'playlist')
                if raw:
                    if pl_type == 'album':
                        tracks = fetch_album_tracks(client, raw)
                    else:
                        tracks = fetch_playlist_tracks(client, raw.get('Id'))
                    pl['tracks'] = tracks
                    log(f"  {pl['name'].split(chr(10))[0]}: {len(tracks)} tracks")
                else:
                    pl['tracks'] = []
                playlists_with_tracks.append(pl)
                fetched += 1
            except Exception as e:
                log(f"  {pl['name'].split(chr(10))[0]}: ERROR {e}")
                pl['tracks'] = []
                playlists_with_tracks.append(pl)
                fetched += 1

    for pl in playlists_with_tracks:
        pl.pop('_raw', None)
        pl.pop('_type', None)

    log(f"Fetched {fetched}, skipped {skipped} unchanged")

    before = len(playlists_with_tracks)
    playlists_with_tracks = [p for p in playlists_with_tracks if p.get('tracks')]
    if before != len(playlists_with_tracks):
        log(f"Filtered out {before - len(playlists_with_tracks)} empty playlists")

    # Playlists first (sorted by name), then albums (sorted by name)
    playlists_only = sorted(
        [p for p in playlists_with_tracks if p['id'].startswith('playlist:')],
        key=lambda p: p['name'].lower())
    albums_only = sorted(
        [p for p in playlists_with_tracks if p['id'].startswith('album:')],
        key=lambda p: p['name'].lower())
    playlists_with_tracks = playlists_only + albums_only

    if fetched == 0 and len(playlists_with_tracks) == len(cache):
        log("No changes - skipping disk write")
        return 0

    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    # Atomic write — a concurrent fetch (or a reader mid-write) must never
    # see a truncated file; corrupt JSON means zero playlists until the
    # next clean refresh.
    _tmp = output_file + '.tmp'
    with open(_tmp, 'w') as f:
        json.dump(playlists_with_tracks, f, indent=2)
    os.replace(_tmp, output_file)
    log(f"Saved {len(playlists_with_tracks)} playlists to {output_file}")

    digit_mapping = build_digit_mapping(playlists_with_tracks)
    os.makedirs(os.path.dirname(DIGIT_PLAYLISTS_FILE), exist_ok=True)
    with open(DIGIT_PLAYLISTS_FILE, 'w') as f:
        json.dump(digit_mapping, f, indent=2)
    pinned = sum(1 for d in "0123456789"
                 if d in digit_mapping and detect_digit_playlist(digit_mapping[d]['name']) is not None)
    log(f"Saved digit playlists ({pinned} pinned, {len(digit_mapping) - pinned} auto-filled)")

    log("=== Done ===")
    return 0


if __name__ == '__main__':
    exit(main())
