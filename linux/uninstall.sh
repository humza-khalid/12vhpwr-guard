#!/usr/bin/env bash
# 12VHPWR Guard, Linux uninstaller (v1.5).
#
#   sudo bash linux/uninstall.sh            remove the service, the code and the command
#   sudo bash linux/uninstall.sh --purge    also remove the config, the shutdown note and logs
#
# Without --purge these are kept, so a reinstall picks up where it left off:
#   /etc/12vhpwr-guard        your config.ini
#   /var/lib/12vhpwr-guard    last_shutdown.json
#   /var/log/12vhpwr-guard    flight recorder CSVs
#
# Stopping the service releases any GPU limit the daemon holds. If the recovery marker shows the
# card was limited, this prints how to put it back by hand.

set -euo pipefail

UNIT_NAME="12vhpwr-guard.service"
PREFIX="/opt/12vhpwr-guard"
CONF_DIR="/etc/12vhpwr-guard"
VAR_DIR="/var/lib/12vhpwr-guard"
LOG_DIR="/var/log/12vhpwr-guard"
RUN_DIR="/run/12vhpwr-guard"
MARKER="$RUN_DIR/mitigation.active"
UNIT_DEST="/etc/systemd/system/$UNIT_NAME"
MODULES_LOAD="/etc/modules-load.d/12vhpwr-guard.conf"
CLI_LINK="/usr/local/bin/12vhpwr-guard"
GPU_UV_CONF="/etc/gpu-uv/gpu-uv.conf"

info() { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
    echo "usage: sudo bash $0 [--purge]"
    echo "  --purge   also delete $CONF_DIR, $VAR_DIR and $LOG_DIR"
}

PURGE=no
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=yes ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

[[ "$(uname -s)" == "Linux" ]] || die "this uninstaller is for Linux. On Windows use uninstall.bat."
[[ "${EUID:-$(id -u)}" -eq 0 ]] || die "run it as root: sudo bash $0"

# The saved power limit, in whole watts, from the marker the daemon writes while the GPU is
# limited. Empty when there is no marker or it cannot be read.
saved_limit_watts() {
    [[ -f "$MARKER" ]] || return 0
    /usr/bin/python3 -c '
import json, sys
try:
    mw = json.load(open(sys.argv[1])).get("saved_power_limit_mw")
    print(round(int(mw) / 1000) if mw else "")
except Exception:
    print("")
' "$MARKER" 2>/dev/null || true
}

# Looked at before stopping as well as after. The daemon releases the limit on a clean stop
# and systemd then removes /run/12vhpwr-guard with the marker in it, so "after" alone would
# almost never see it. A marker present before the stop means the card was limited a moment
# ago, which is worth a word even when the release most likely worked.
MARKER_BEFORE=no
SAVED_W=""
if [[ -f "$MARKER" ]]; then
    MARKER_BEFORE=yes
    SAVED_W="$(saved_limit_watts)"
fi

# ------------------------------------------------------------------ service
if [[ -f "$UNIT_DEST" ]] || systemctl cat "$UNIT_NAME" >/dev/null 2>&1; then
    info "Stopping and disabling $UNIT_NAME (this releases any GPU limit)"
    systemctl disable --now "$UNIT_NAME" || warn "systemctl disable --now failed; continuing."
else
    info "$UNIT_NAME is not installed"
fi

MARKER_AFTER=no
if [[ -f "$MARKER" ]]; then
    MARKER_AFTER=yes
    [[ -n "$SAVED_W" ]] || SAVED_W="$(saved_limit_watts)"
fi

rm -f "$UNIT_DEST"
systemctl daemon-reload
systemctl reset-failed "$UNIT_NAME" >/dev/null 2>&1 || true

# ------------------------------------------------------------------ command and code
# Only remove the command if it is our link, never a file someone else put there.
if [[ -L "$CLI_LINK" ]] && [[ "$(readlink "$CLI_LINK")" == "$PREFIX/"* ]]; then
    info "Removing $CLI_LINK"
    rm -f "$CLI_LINK"
fi

if [[ -d "$PREFIX" ]]; then
    info "Removing $PREFIX"
    rm -rf "$PREFIX"
fi

# i2c-dev itself stays loaded until the next boot: other tools may be using it.
if [[ -f "$MODULES_LOAD" ]]; then
    info "Removing $MODULES_LOAD"
    rm -f "$MODULES_LOAD"
fi

# ------------------------------------------------------------------ data
purge_data() {
    info "Purging $CONF_DIR, $VAR_DIR and $LOG_DIR"
    rm -rf "$CONF_DIR" "$VAR_DIR" "$LOG_DIR" "$RUN_DIR"
}

if [[ "$PURGE" == yes ]]; then
    purge_data
else
    info "Kept (delete by hand, or run again with --purge):"
    for d in "$CONF_DIR" "$VAR_DIR" "$LOG_DIR"; do
        if [[ -e "$d" ]]; then echo "    $d"; fi
    done
fi

# ------------------------------------------------------------------ card recovery
if [[ "$MARKER_BEFORE" == yes || "$MARKER_AFTER" == yes ]]; then
    echo
    if [[ "$MARKER_AFTER" == yes ]]; then
        warn "The guard's recovery marker is still there after stopping, so the GPU may still be"
        warn "limited (clock locked low and/or a reduced power limit)."
    else
        warn "The guard was limiting the GPU when it was stopped. A clean stop puts the card back,"
        warn "but if clocks or the power limit still look low (nvidia-smi -q -d CLOCK,POWER):"
    fi
    if [[ -f "$GPU_UV_CONF" ]]; then
        # With gpu-uv the clock must not simply be reset: that would leave its offset uncapped.
        warn "  Restore your gpu-uv settings: sudo gpu-uv apply"
        warn "  Then the power limit if needed: sudo nvidia-smi -pl ${SAVED_W:-<your usual watts>}"
    else
        warn "  Unlock the clocks:          sudo nvidia-smi -rgc"
        warn "  Restore the power limit:    sudo nvidia-smi -pl ${SAVED_W:-<your usual watts>}"
    fi
    warn "  A reboot also clears both."
fi

echo
info "12VHPWR Guard is uninstalled."
