#!/bin/bash
# =============================================================================
# Remove the guest Samba share that installs up to v0.9.4 created
# =============================================================================
# Older installers apt-installed `samba` and appended this to smb.conf:
#
#   [USB-Music]
#       path = /mnt/usb-music
#       guest ok = yes          <- no credentials needed
#       wide links = yes        <- symlinks may escape the share root
#
# It was a convenience for copying music onto a USB drive; nothing in the
# system ever depended on it (files reach Sonos/Bluesound/HEOS over HTTP from
# the USB source service — services/lib/file_playback/remote_player.py).  An
# unauthenticated file server on every device is not worth that, so it is gone.
#
# This script undoes it: drop our stanza, and when nothing else on the device
# uses samba, stop the daemons and purge the packages.  Idempotent, and a
# no-op on installs that never had the share.
#
# Run standalone as root (called by install.sh and post-update.sh):
#   sudo ./install/modules/samba-cleanup.sh
# =============================================================================

# Overridable so tests can run this against a temp file (see
# tests/unit/python/test_samba_cleanup.py).
SMB_CONF="${SMB_CONF:-/etc/samba/smb.conf}"

log() { echo "[samba-cleanup] $*"; }

[ -f "$SMB_CONF" ] || exit 0
grep -q '^\[USB-Music\]' "$SMB_CONF" 2>/dev/null || exit 0

log "Removing legacy USB-Music share from $SMB_CONF"
# smb.conf may be hand-edited — keep a backup before rewriting it.
cp "$SMB_CONF" "${SMB_CONF}.bs5c-backup"

# Delete from the [USB-Music] header up to the next section header (or EOF).
python3 - "$SMB_CONF" << 'PYEOF'
import re
import sys

path = sys.argv[1]
with open(path) as f:
    text = f.read()
text = re.sub(r'\n*^\[USB-Music\][^\[]*', '\n\n', text, flags=re.MULTILINE)
with open(path, 'w') as f:
    f.write(text)
PYEOF
log "Share removed (backup at ${SMB_CONF}.bs5c-backup)"

# A share section beyond the Debian defaults means the owner uses samba for
# something of their own — reload it and leave the packages in place.
OTHER_SHARES=$(grep -oE '^\[[^]]+\]' "$SMB_CONF" 2>/dev/null \
    | grep -vxE '\[global\]|\[printers\]|\[print\$\]|\[homes\]' || true)
if [ -n "$OTHER_SHARES" ]; then
    log "Other shares present ($(echo "$OTHER_SHARES" | tr '\n' ' ')) — keeping samba"
    systemctl reload smbd 2>/dev/null || systemctl restart smbd 2>/dev/null || true
    exit 0
fi

log "No other shares — stopping and purging samba"
systemctl disable --now smbd nmbd 2>/dev/null || true
DEBIAN_FRONTEND=noninteractive apt-get purge -y samba samba-common-bin >/dev/null 2>&1 || true
DEBIAN_FRONTEND=noninteractive apt-get autoremove -y >/dev/null 2>&1 || true
log "Samba removed"
