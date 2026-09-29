#!/bin/bash
# =============================================================================
# BeoSound 5c Installer — shairport-sync + nqptp (AirPlay 2 receiver)
# =============================================================================
#
# Unlike go-librespot there is no usable prebuilt binary: Debian ships
# shairport-sync 4.3.7, but built for AirPlay 1 only (its dependency list has
# no libplist, libsodium or ffmpeg), and nqptp is not packaged at all. AirPlay
# 2 therefore has to be compiled from source.

SHAIRPORT_BINARY="/usr/local/bin/shairport-sync"
SHAIRPORT_CONFIG="/etc/beosound5c/shairport-sync.conf"
AIRPLAY_BUILD_DIR="/var/tmp/beo-airplay-build"

# Gated on config: this is a multi-minute compile with a long list of -dev
# packages, so it only runs when AIRPLAY is in the menu and the player is
# local (shairport-sync writes into this Pi's PipeWire graph; on a Sonos or
# Bluesound device there is nothing for it to play into). Adding AIRPLAY
# later: edit config.json, then `sudo install/install.sh system`.
_airplay_wanted() {
    [ -f "$CONFIG_FILE" ] || return 1
    grep -q '"AIRPLAY"' "$CONFIG_FILE" || return 1
    local PLAYER_TYPE
    PLAYER_TYPE=$(python3 -c "import json;print(json.load(open('$CONFIG_FILE')).get('player',{}).get('type','local'))" 2>/dev/null || echo local)
    [ "$PLAYER_TYPE" = "local" ]
}

install_airplay() {
    if ! _airplay_wanted; then
        log_info "AirPlay: not in menu (or player is not local) — skipping shairport-sync build"
        return 0
    fi

    log_section "Installing shairport-sync (AirPlay 2)"

    if [ -x "$SHAIRPORT_BINARY" ] && "$SHAIRPORT_BINARY" -V 2>/dev/null | grep -q "AirPlay2"; then
        log_info "shairport-sync with AirPlay 2 already installed"
    else
        _airplay_build || return
    fi

    _airplay_dbus_policy
    _airplay_config
    _airplay_units
    # Existing config: keep the advertised name in step with config.json
    # (reconcile-services.sh does this on every config save as well).
    python3 "$INSTALL_DIR/services/lib/shairport_config.py" "$CONFIG_FILE" "$SHAIRPORT_CONFIG" \
        || log_warn "Could not sync AirPlay name"

    # The unit runs shairport-sync through this wrapper (it resolves
    # airplay.mode into --service-type). An OTA rsync does not always preserve
    # the executable bit, and a non-executable ExecStart fails the unit.
    chmod +x "$INSTALL_DIR/services/system/beo-shairport-launch.sh" 2>/dev/null || true
}

_airplay_build() {
    log_info "Installing build dependencies..."
    # A stale index 404s on the first -dev package whose version moved on
    # (seen with libglib2.0-dev on trixie) — refresh before the big install.
    apt-get update -qq || log_warn "apt-get update failed — trying with the existing index"
    # plistutil (libplist-utils) is a build-time *binary* dependency of
    # --with-airplay-2 and is NOT pulled in by libplist-dev; configure fails
    # late without it.
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential git autoconf automake libtool \
        libpopt-dev libconfig-dev libasound2-dev libavahi-client-dev libssl-dev \
        libsoxr-dev libplist-dev libplist-utils libsodium-dev libavutil-dev \
        libavcodec-dev libavformat-dev uuid-dev libgcrypt20-dev xxd \
        libpulse-dev libglib2.0-dev || {
        log_warn "Could not install AirPlay build dependencies — skipping"
        return 1
    }

    mkdir -p "$AIRPLAY_BUILD_DIR"

    # ── nqptp: the PTP clock daemon AirPlay 2 requires ──
    if ! systemctl is-enabled nqptp >/dev/null 2>&1; then
        log_info "Building nqptp..."
        rm -rf "$AIRPLAY_BUILD_DIR/nqptp"
        git clone --depth 1 https://github.com/mikebrady/nqptp.git \
            "$AIRPLAY_BUILD_DIR/nqptp" || return 1
        (
            cd "$AIRPLAY_BUILD_DIR/nqptp" || exit 1
            autoreconf -fi && ./configure --with-systemd-startup && make && make install
        ) || { log_warn "nqptp build failed — skipping AirPlay"; return 1; }
        systemctl enable --now nqptp
        log_success "nqptp installed and running"
    else
        log_info "nqptp already installed"
    fi

    # ── shairport-sync ──
    log_info "Building shairport-sync with AirPlay 2 (a few minutes)..."
    rm -rf "$AIRPLAY_BUILD_DIR/shairport-sync"
    git clone --depth 1 https://github.com/mikebrady/shairport-sync.git \
        "$AIRPLAY_BUILD_DIR/shairport-sync" || return 1
    (
        cd "$AIRPLAY_BUILD_DIR/shairport-sync" || exit 1
        autoreconf -fi
        # --with-pulseaudio, NOT --with-pipewire: the native PipeWire backend
        # needs libpipewire-0.3-dev, and where PipeWire comes from backports
        # the dev package is older than the runtime, so apt would downgrade a
        # working PipeWire. The PulseAudio backend reaches PipeWire through
        # pipewire-pulse, the same path go-librespot uses into beo_tone_sink.
        # (Note: "--with-pa" is silently ignored by configure and yields an
        # ALSA-only binary — a trap worth spelling out.)
        ./configure \
            --sysconfdir=/etc \
            --with-pulseaudio \
            --with-alsa \
            --with-avahi \
            --with-ssl=openssl \
            --with-soxr \
            --with-airplay-2 \
            --with-metadata \
            --with-dbus-interface
        make -j"$(nproc)" && make install
    ) || { log_warn "shairport-sync build failed — skipping AirPlay"; return 1; }

    log_success "shairport-sync installed: $("$SHAIRPORT_BINARY" -V 2>/dev/null)"
}

_airplay_units() {
    # install-services.sh installs every unit in the registry on a full
    # install, but post-update.sh only refreshes units that already exist and
    # `install.sh system` never touches units at all — so a device that gains
    # AIRPLAY after its first install would have nothing for
    # reconcile-services.sh to enable. Install the two this module needs.
    local UID_
    UID_=$(id -u "$INSTALL_USER")
    local unit
    for unit in beo-shairport.service beo-source-airplay.service; do
        sed -e "s|__USER__|$INSTALL_USER|g" \
            -e "s|__HOME__|$INSTALL_HOME|g" \
            -e "s|__UID__|$UID_|g" \
            "$INSTALL_DIR/services/system/$unit" > "/etc/systemd/system/$unit"
    done
    systemctl daemon-reload
    log_success "Installed beo-shairport and beo-source-airplay units"
}

_airplay_dbus_policy() {
    # shairport-sync runs as the service user so it can reach that user's
    # PipeWire graph, but upstream's D-Bus policy only lets root and the
    # packaged "shairport-sync" user own the bus name. Without this the
    # transport buttons cannot be proxied back to the sender.
    local POLICY="/etc/dbus-1/system.d/beo-shairport-dbus.conf"
    cat > "$POLICY" << DBUS_EOF
<!DOCTYPE busconfig PUBLIC
          "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
          "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <policy user="$INSTALL_USER">
    <allow own="org.gnome.ShairportSync"/>
  </policy>
</busconfig>
DBUS_EOF
    systemctl reload dbus 2>/dev/null || true
    log_success "D-Bus policy installed for $INSTALL_USER"
}

_airplay_config() {
    if [ -f "$SHAIRPORT_CONFIG" ]; then
        log_info "shairport-sync config already exists"
        return
    fi

    # The AirPlay name is the same one Spotify Connect advertises
    # ("BeoSound 5c Office"), derived from config.json's device name by
    # services/lib/shairport_config.py, so the unit appears under one name
    # in both pickers.
    local DEVICE_NAME
    DEVICE_NAME=$(python3 "$INSTALL_DIR/services/lib/shairport_config.py" --name "$CONFIG_FILE" 2>/dev/null \
        || echo "BeoSound 5c")

    log_info "Creating shairport-sync config (name: $DEVICE_NAME)..."
    cat > "$SHAIRPORT_CONFIG" << SPCFG_EOF
// BeoSound 5c — shairport-sync (AirPlay 2 receiver)
//
// Audio leaves through the PulseAudio backend, which lands in the service
// user's PipeWire graph on beo_tone_sink (the tone filter chain) and
// continues to the DAC — the same route go-librespot takes, so AirPlay gets
// tone control and the normal PowerLink/MasterLink output for free.

general = {
	name = "$DEVICE_NAME";
	output_backend = "pulseaudio";
	interpolation = "soxr";

	// The BeoSound owns volume: the wheel, the Beo4 remote and MasterLink
	// all drive the real output level. Letting the sender's slider also
	// attenuate the stream would put two volume controls in series, so full
	// scale is passed through. Mirrors go-librespot's external_volume: true.
	ignore_volume_control = "yes";

	// beo-source-airplay proxies transport buttons back to the sender here.
	dbus_service_bus = "system";
};

metadata = {
	enabled = "yes";
	include_cover_art = "yes";
	pipe_name = "/tmp/shairport-sync-metadata";
	// Never let a missing reader stall playback.
	pipe_timeout = 5000;
};

diagnostics = {
	log_verbosity = 0;
};
SPCFG_EOF

    chown "$INSTALL_USER:$INSTALL_USER" "$SHAIRPORT_CONFIG"
    log_success "shairport-sync config created"
}
