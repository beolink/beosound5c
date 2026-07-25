#!/bin/bash
# =============================================================================
# flight-recorder.sh — persistent journald for post-mortem debugging
# =============================================================================
# sd-hardening mounts /var/log as tmpfs and sets journald to volatile, so
# after a lockup + power cycle there is nothing to read. This tool carves
# the journal back out onto the SD card, size-capped, so the log survives:
#
#   - /var/lib/beosound5c/journal (real SD storage) is bind-mounted onto
#     /var/log/journal (journald's hard-coded persistent path)
#   - a journald drop-in sets Storage=persistent with SystemMaxUse=64M,
#     overriding sd-hardening's Storage=volatile in journald.conf
#
# SD wear is modest: journald batches and the cap bounds total writes.
# Pair with the beo-metrics line from beo-health.sh (every 5 min) to see
# the resource trajectory leading up to a crash.
#
# Usage (on the device):
#   sudo ./tools/flight-recorder.sh enable
#   sudo ./tools/flight-recorder.sh disable   # keeps captured logs on SD
#   ./tools/flight-recorder.sh status
#
# Post-mortem after a lockup + power cycle:
#   journalctl --list-boots
#   journalctl -b -1 -e                       # end of the previous boot
#   journalctl -b -1 -t beo-metrics           # resource trajectory
#   journalctl -b -1 -k | tail -50            # kernel (OOM, USB, WiFi)
# =============================================================================
set -euo pipefail

PERSIST_DIR="/var/lib/beosound5c/journal"
MOUNT_POINT="/var/log/journal"
FSTAB_LINE="$PERSIST_DIR $MOUNT_POINT none bind,nofail 0 0"
DROPIN_DIR="/etc/systemd/journald.conf.d"
DROPIN="$DROPIN_DIR/50-flight-recorder.conf"

need_root() {
    if [ "$EUID" -ne 0 ]; then
        echo "Error: must run as root (use sudo)" >&2
        exit 1
    fi
}

enable_recorder() {
    need_root
    mkdir -p "$PERSIST_DIR"
    chown root:systemd-journal "$PERSIST_DIR"
    chmod 2755 "$PERSIST_DIR"

    if ! grep -qF "$MOUNT_POINT" /etc/fstab; then
        echo "$FSTAB_LINE" >> /etc/fstab
        echo "Added bind mount to /etc/fstab"
    fi
    systemctl daemon-reload
    if ! mountpoint -q "$MOUNT_POINT"; then
        mkdir -p "$MOUNT_POINT"
        mount "$MOUNT_POINT"
        echo "Mounted $PERSIST_DIR -> $MOUNT_POINT"
    fi

    mkdir -p "$DROPIN_DIR"
    cat > "$DROPIN" <<'EOF'
# Installed by tools/flight-recorder.sh — persistent journal for
# post-mortem debugging. Overrides sd-hardening's Storage=volatile.
[Journal]
Storage=persistent
SystemMaxUse=64M
SystemMaxFileSize=8M
EOF
    systemctl restart systemd-journald
    # Move this boot's RAM journal into the persistent store so the
    # recorder starts with history, not from zero.
    journalctl --flush 2>/dev/null || true
    # tmpfiles normally grants the adm group read on /var/log/journal via
    # ACLs; our bind-mount source isn't covered by those rules, so apply
    # them here — otherwise journalctl silently skips these files unless
    # run as root.
    setfacl -Rnm g:adm:rX -m d:g:adm:rX "$PERSIST_DIR" 2>/dev/null || true

    echo "Flight recorder enabled."
    status_recorder
}

disable_recorder() {
    need_root
    rm -f "$DROPIN"
    systemctl restart systemd-journald
    if mountpoint -q "$MOUNT_POINT"; then
        umount "$MOUNT_POINT" || echo "Warning: could not unmount $MOUNT_POINT (will clear at reboot)"
    fi
    sed -i "\|$MOUNT_POINT|d" /etc/fstab
    systemctl daemon-reload
    echo "Flight recorder disabled. Captured logs kept in $PERSIST_DIR"
}

status_recorder() {
    if [ -f "$DROPIN" ] && mountpoint -q "$MOUNT_POINT"; then
        echo "Flight recorder: ENABLED"
    else
        echo "Flight recorder: disabled (drop-in: $([ -f "$DROPIN" ] && echo present || echo absent), mount: $(mountpoint -q "$MOUNT_POINT" && echo active || echo inactive))"
    fi
    [ -d "$PERSIST_DIR" ] && echo "Stored: $(du -sh "$PERSIST_DIR" 2>/dev/null | cut -f1) in $PERSIST_DIR"
    journalctl --disk-usage 2>/dev/null || true
    echo "Boots on record:"
    journalctl --list-boots 2>/dev/null | tail -5 || true
}

case "${1:-status}" in
    enable)  enable_recorder ;;
    disable) disable_recorder ;;
    status)  status_recorder ;;
    *) echo "Usage: $0 [enable|disable|status]" >&2; exit 1 ;;
esac
