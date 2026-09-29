#!/bin/bash
# Restart every running beo-* service after a code update.
#
# Order matters. beo-input is restarted LAST and on its own: a caller that
# runs inside beo-input's cgroup (the OTA updater, the config-save handler)
# dies the moment beo-input stops, and `systemctl restart a b c` issues its
# jobs one unit at a time — so any unit listed after beo-input would never
# be restarted. beo-ui is restarted after the backends, with a pause, so
# Chromium loads against services that are already up.
#
# Usage: restart-services.sh [delay-seconds]
#   post-update.sh runs this from a transient systemd unit with a delay, so
#   an updater older than v0.10.1 finishes its own (partial) restart first.
set -u

DELAY="${1:-0}"
if [ "$DELAY" -gt 0 ] 2>/dev/null; then
    sleep "$DELAY"
fi

# Never include a restart helper itself (the transient unit post-update.sh
# runs us from is named bs5c-*, but guard against any *update-restart*
# unit regardless): restarting our own unit would loop forever.
ACTIVE=$(systemctl list-units --state=active --no-legend --plain 'beo-*.service' \
         | awk '{print $1}' | grep -v 'update-restart' || true)
BACKENDS=$(echo "$ACTIVE" | grep -Ev '^(beo-ui|beo-input)\.service$' || true)

if [ -n "$BACKENDS" ]; then
    # shellcheck disable=SC2086
    systemctl restart $BACKENDS \
        || echo "restart-services: some backend(s) failed to restart" >&2
fi

if echo "$ACTIVE" | grep -qx 'beo-ui.service'; then
    sleep 3
    systemctl restart beo-ui.service \
        || echo "restart-services: beo-ui failed to restart" >&2
fi

if echo "$ACTIVE" | grep -qx 'beo-input.service'; then
    systemctl restart beo-input.service
fi
