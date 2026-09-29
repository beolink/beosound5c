#!/usr/bin/env python3
"""shairport-sync's on-disk config — keeping the AirPlay name in sync.

shairport-sync advertises the ``name`` from its own config file
(/etc/beosound5c/shairport-sync.conf), which install/modules/airplay.sh writes
once. Same problem go-librespot had (see librespot_config.py): a rename from
the setup UI never reached the receiver, and two units could end up
advertising the same AirPlay name.

reconcile-services.sh calls this on every install, deploy and config save,
before it try-restarts the beo-* services — shairport-sync reads its config
only at startup, so that restart is what carries the rename through.

The name itself is the same one Spotify Connect uses ('BeoSound 5c Office'),
so the device shows up under one name in both pickers.

Usage:
    shairport_config.py [config.json] [shairport-sync.conf]   # sync, print if changed
    shairport_config.py --name [config.json]                  # print the name only
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile

try:
    from lib.librespot_config import spotify_connect_name as advertised_name
except ImportError:  # run as a script from services/lib/
    from librespot_config import spotify_connect_name as advertised_name

CONFIG_PATH = '/etc/beosound5c/config.json'
SHAIRPORT_CONF_PATH = '/etc/beosound5c/shairport-sync.conf'

# libconfig syntax: `name = "Office";` inside the general = { ... } block.
_NAME_LINE = re.compile(r'^(\s*)name\s*=\s*"(?:[^"\\]|\\.)*"\s*;.*$')


def _quote(name: str) -> str:
    """libconfig double-quoted string; an unescaped quote breaks the file."""
    escaped = name.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def set_name(conf_path: str, name: str) -> bool:
    """Point shairport-sync's ``name`` at `name`. True if the file changed.

    Missing file means AirPlay is not installed on this device — nothing to
    rename. Written to a temp file alongside and renamed over, so a power cut
    mid-write cannot leave shairport-sync a truncated config.
    """
    try:
        with open(conf_path) as f:
            lines = f.read().splitlines(keepends=True)
    except FileNotFoundError:
        return False

    for i, line in enumerate(lines):
        m = _NAME_LINE.match(line.rstrip('\n'))
        if not m:
            continue
        new_line = f'{m.group(1)}name = {_quote(name)};\n'
        if line == new_line:
            return False  # already correct — don't churn the file
        lines[i] = new_line
        break
    else:
        print(f'⚠️  {conf_path} has no name line — leaving it alone',
              file=sys.stderr)
        return False

    directory = os.path.dirname(conf_path) or '.'
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix='.shairport-sync.conf.')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(''.join(lines))
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, conf_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return True


def name_from_config(config_path: str = CONFIG_PATH) -> str:
    with open(config_path) as f:
        device = json.load(f).get('device') or ''
    return advertised_name(device)


def sync_from_config(config_path: str = CONFIG_PATH,
                     conf_path: str = SHAIRPORT_CONF_PATH) -> str | None:
    """Derive the AirPlay name from config.json and write it. Returns the new
    name if the file changed, else None."""
    name = name_from_config(config_path)
    return name if set_name(conf_path, name) else None


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == '--name':
        print(name_from_config(argv[2] if len(argv) > 2 else CONFIG_PATH))
        return 0
    config_path = argv[1] if len(argv) > 1 else CONFIG_PATH
    conf_path = argv[2] if len(argv) > 2 else SHAIRPORT_CONF_PATH
    changed = sync_from_config(config_path, conf_path)
    if changed:
        print(f'ℹ️  AirPlay name -> {changed}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
