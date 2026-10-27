#!/usr/bin/env bash
# 12VHPWR Guard, Linux installer (v1.5).
#
#   sudo bash linux/install.sh
#
# Installs the daemon from the repo checkout this script lives in:
#   code     /opt/12vhpwr-guard            (same layout as the repo, root owned, 0644)
#   config   /etc/12vhpwr-guard/config.ini (only when there is none yet; never overwritten)
#   i2c-dev  /etc/modules-load.d/12vhpwr-guard.conf, and loaded now
#   service  /etc/systemd/system/12vhpwr-guard.service, enabled and started
#   command  /usr/local/bin/12vhpwr-guard -> /opt/12vhpwr-guard/linux/guard_cli.py
#
# Safe to run again: a second run is an upgrade. The service is stopped first (which releases
# any GPU limit cleanly), the files are replaced, and the service is started again. The config
# and the data directories are never touched.

set -euo pipefail

UNIT_NAME="12vhpwr-guard.service"
PREFIX="/opt/12vhpwr-guard"
CONF_DIR="/etc/12vhpwr-guard"
CONF_FILE="$CONF_DIR/config.ini"
UNIT_DEST="/etc/systemd/system/$UNIT_NAME"
MODULES_LOAD="/etc/modules-load.d/12vhpwr-guard.conf"
CLI_LINK="/usr/local/bin/12vhpwr-guard"
PYTHON="/usr/bin/python3"

# Everything the daemon imports, as paths relative to the repo root. The installed tree mirrors
# the repo, so guardd.py finds the shared modules one directory up exactly as it does in a
# checkout.
FILES=(
    guard_core.py
    gpu_mitigation.py
    astral_i2c.py
    linux/guardd.py
    linux/astral_linux.py
    linux/notify.py
    linux/guard_cli.py
)

info() { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------------ preflight
[[ "$(uname -s)" == "Linux" ]] || die "this installer is for Linux. On Windows use install.bat."
[[ "${EUID:-$(id -u)}" -eq 0 ]] || die "run it as root: sudo bash $0"
command -v systemctl >/dev/null 2>&1 || die "systemd (systemctl) is required."

[[ -x "$PYTHON" ]] || die "$PYTHON not found. Install python3 from your distribution (3.10 or newer)."
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "$PYTHON is $("$PYTHON" -c 'import platform; print(platform.python_version())'); 3.10 or newer is needed."

# Resolve the checkout from this script's own location, so it works from any directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"

# Check every source file before changing anything, so a broken checkout installs nothing.
for f in "${FILES[@]}" linux/12vhpwr-guard.service linux/config.ini.example; do
    [[ -f "$REPO_ROOT/$f" ]] || die "missing $REPO_ROOT/$f. Run this from a complete checkout."
done

# GPU limiting goes through the NVIDIA driver (NVML). Without it the daemon still reads the pins
# and can still shut down, it just cannot limit the GPU first, so this warns rather than stops.
if ! command -v nvidia-smi >/dev/null 2>&1; then
    warn "nvidia-smi not found. Is the NVIDIA driver installed? Without it the guard cannot limit"
    warn "the GPU and will run shutdown-only."
elif ! nvidia-smi >/dev/null 2>&1; then
    warn "nvidia-smi is installed but failed. Check the NVIDIA driver (run nvidia-smi to see why)."
    warn "Until it works the guard cannot limit the GPU and will run shutdown-only."
fi

# A kernel driver bound to the pin monitor chip (0x2B) on one of the GPU's own I2C adapters
# owns it, and the guard steps aside rather than share the chip: it will report "not protected"
# until that driver is unloaded. Only NVIDIA adapters are checked, since other devices at 0x2B
# on a motherboard bus have nothing to do with the card.
for client in /sys/bus/i2c/devices/*-002b; do
    [[ -e "$client/driver" ]] || continue
    adapter="${client##*/}"
    adapter="i2c-${adapter%-002b}"
    adapter_name="$(cat "/sys/bus/i2c/devices/$adapter/name" 2>/dev/null || true)"
    [[ "$adapter_name" == NVIDIA* ]] || continue
    driver="$(basename "$(readlink -f "$client/driver")")"
    module="$driver"
    if [[ -e "$client/driver/module" ]]; then
        module="$(basename "$(readlink -f "$client/driver/module")")"
    fi
    warn "kernel driver '$driver' is bound to the pin monitor chip at 0x2B on $adapter ($adapter_name)."
    warn "The guard will not read the chip while a driver owns it, so it will report NOT PROTECTED."
    warn "This is usually astral-hwmon. Unload it (sudo modprobe -r $module) and stop it loading at"
    warn "boot (remove it from /etc/modules-load.d or blacklist it), then restart the guard:"
    warn "sudo systemctl restart 12vhpwr-guard"
done

# ------------------------------------------------------------------ upgrade: stop first
UPGRADE=no
if [[ -f "$UNIT_DEST" ]]; then
    UPGRADE=yes
    info "Existing install found, upgrading. Stopping the service (this releases any GPU limit)."
    systemctl stop "$UNIT_NAME" || warn "systemctl stop failed; continuing with the upgrade."
fi

# ------------------------------------------------------------------ code
info "Installing the guard to $PREFIX"
install -d -m 0755 -o root -g root "$PREFIX" "$PREFIX/linux"
for f in "${FILES[@]}"; do
    install -m 0644 -o root -g root "$REPO_ROOT/$f" "$PREFIX/$f"
done
# The CLI is run directly through its symlink, so it is the one executable file.
chmod 0755 "$PREFIX/linux/guard_cli.py"
# Bytecode from the previous version is regenerated on the next start anyway; clearing it keeps
# the tree an exact copy of what was just installed.
rm -rf "$PREFIX/__pycache__" "$PREFIX/linux/__pycache__"

# ------------------------------------------------------------------ config
install -d -m 0755 -o root -g root "$CONF_DIR"
if [[ -e "$CONF_FILE" ]]; then
    info "Keeping your existing $CONF_FILE"
else
    install -m 0644 -o root -g root "$REPO_ROOT/linux/config.ini.example" "$CONF_FILE"
    info "Wrote the default config to $CONF_FILE"
fi
# Always refreshed, so the current keys and defaults are there to compare against.
install -m 0644 -o root -g root "$REPO_ROOT/linux/config.ini.example" "$CONF_DIR/config.ini.example"

# ------------------------------------------------------------------ i2c-dev
info "Loading i2c-dev now and at every boot ($MODULES_LOAD)"
install -d -m 0755 /etc/modules-load.d
printf '# Loaded at boot for 12VHPWR Guard, which reads the GPU pin monitor over /dev/i2c-N.\ni2c-dev\n' \
    > "$MODULES_LOAD"
chmod 0644 "$MODULES_LOAD"
modprobe i2c-dev || warn "modprobe i2c-dev failed; the service also tries it on every start."

# ------------------------------------------------------------------ command
info "Linking $CLI_LINK"
install -d -m 0755 /usr/local/bin
ln -sfn "$PREFIX/linux/guard_cli.py" "$CLI_LINK"

# ------------------------------------------------------------------ service
info "Installing and starting $UNIT_NAME"
install -m 0644 -o root -g root "$REPO_ROOT/linux/12vhpwr-guard.service" "$UNIT_DEST"
systemctl daemon-reload
STARTED=yes
if ! systemctl enable --now "$UNIT_NAME"; then
    STARTED=no
    warn "the service did not start. The status and recent log below say why."
fi

# ------------------------------------------------------------------ report
echo
systemctl --no-pager status "$UNIT_NAME" || true
echo
if [[ "$STARTED" == yes ]]; then
    info "Waiting a few seconds for the first readings"
    sleep 5
    "$CLI_LINK" status || true
else
    journalctl -u "$UNIT_NAME" -n 30 --no-pager || true
fi
echo
if [[ "$UPGRADE" == yes ]]; then
    info "Upgrade finished."
else
    info "Install finished."
fi
echo "    Status:      12vhpwr-guard status"
echo "    Log:         12vhpwr-guard log -f"
echo "    Test notify: sudo 12vhpwr-guard test-notify"
echo "    Config:      $CONF_FILE (then: sudo systemctl restart 12vhpwr-guard)"
echo "    Uninstall:   sudo bash $SCRIPT_DIR/uninstall.sh"
