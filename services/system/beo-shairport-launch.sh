#!/bin/sh
# Launch shairport-sync with the AirPlay service type taken from config.json.
#
#   "airplay": { "mode": "auto" | "airplay2" | "classic" }
#
# The choice lives in the BeoSound's own config rather than in the generated
# shairport-sync.conf, so switching is a config edit plus a restart.
#
# It is a real trade-off, not a preference:
#   airplay2  multi-room grouping and the Home app, but no transport control
#             back to the sender (buffered streams carry no DACP credentials)
#   classic   AirPlay 1 only, so no multi-room or Home app, but the stream is
#             realtime and the BeoSound's transport keys can drive the sender
#   auto      AirPlay 2 when nqptp is running, classic otherwise (default)
#
# See docs/airplay.md.
set -e

CONFIG=/etc/beosound5c/config.json
BINARY=/usr/local/bin/shairport-sync
CONF=/etc/beosound5c/shairport-sync.conf

MODE=$(python3 - "$CONFIG" <<'PY' 2>/dev/null || echo auto
import json, sys
VALID = {"auto", "airplay2", "classic", "airplay1"}
try:
    mode = (json.load(open(sys.argv[1])).get("airplay") or {}).get("mode", "auto")
except Exception:
    mode = "auto"
# An unrecognised value must not stop the receiver from starting at all.
print(mode if mode in VALID else "auto")
PY
)

echo "Starting shairport-sync with service type: $MODE"
exec "$BINARY" -c "$CONF" --service-type="$MODE"
