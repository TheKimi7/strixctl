#!/bin/sh
# strixctl installer.  Run once with sudo; after that nothing needs root.
#
#   sudo ./install.sh              install
#   sudo ./install.sh --uninstall  remove
#
# The point of the install is the udev rule and the boot-time grant service:
# together they hand the ASUS sysfs knobs to a group you are already in, so the
# GUI, the CLI and the tray icon all run unprivileged.

set -eu

PREFIX="${PREFIX:-/usr}"
LIBDIR="$PREFIX/lib/strixctl"
BINDIR="$PREFIX/bin"
GROUP="${STRIXCTL_GROUP:-users}"
SRC="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "install.sh must run as root (try: sudo ./install.sh)" >&2
    exit 1
fi

uninstall() {
    rm -f "$BINDIR/strixctl"
    rm -f /etc/udev/rules.d/99-strixctl.rules
    rm -f /usr/share/applications/strixctl.desktop
    systemctl disable --now strixctl-permissions.service 2>/dev/null || true
    rm -f /etc/systemd/system/strixctl-permissions.service
    rm -f /usr/lib/systemd/user/strixctl-apply.service
    rm -rf "$LIBDIR"
    systemctl daemon-reload
    udevadm control --reload-rules
    echo "strixctl removed.  Your config in ~/.config/strixctl was left alone."
    echo "Note: knobs stay group-writable until the next reboot."
    exit 0
}

[ "${1:-}" = "--uninstall" ] && uninstall

if ! getent group "$GROUP" >/dev/null; then
    echo "group '$GROUP' does not exist; set STRIXCTL_GROUP to one that does" >&2
    exit 1
fi

# A hand-rolled charge-limit oneshot writes the same sysfs attribute as
# strixctl-apply.service, with no ordering between them. Flag it rather than
# silently ending up with two units fighting over the threshold at boot.
for unit in $(systemctl list-unit-files --no-legend --state=enabled 2>/dev/null \
              | awk '{print $1}' | grep -iE 'batt|charge' || true); do
    case "$unit" in
        strixctl-*) continue ;;
        systemd-*)  continue ;;
    esac
    if systemctl cat "$unit" 2>/dev/null | grep -q charge_control_end_threshold; then
        echo "WARNING: $unit also writes charge_control_end_threshold."
        echo "  It will race strixctl-apply.service at boot. Disable it:"
        echo "    sudo systemctl disable --now $unit"
        echo
    fi
done

echo "Installing strixctl (group: $GROUP)"

install -d "$LIBDIR"
cp -r "$SRC/strixctl" "$LIBDIR/"
install -m 0755 "$SRC/share/strixctl-grant" "$LIBDIR/strixctl-grant"

cat > "$BINDIR/strixctl" <<LAUNCHER
#!/usr/bin/env python3
import sys
sys.path.insert(0, "$LIBDIR")
from strixctl.cli import main
sys.exit(main())
LAUNCHER
chmod 0755 "$BINDIR/strixctl"

# The grant script reads the group from the environment; bake in the choice.
sed -i "s|^GROUP=.*|GROUP=\"\${STRIXCTL_GROUP:-$GROUP}\"|" "$LIBDIR/strixctl-grant"

install -m 0644 "$SRC/udev/99-strixctl.rules" /etc/udev/rules.d/99-strixctl.rules
sed -i "s|GROUP=\"users\"|GROUP=\"$GROUP\"|" /etc/udev/rules.d/99-strixctl.rules
sed -i "s|/usr/lib/strixctl/strixctl-grant|$LIBDIR/strixctl-grant|" \
    /etc/udev/rules.d/99-strixctl.rules

install -m 0644 "$SRC/systemd/strixctl-permissions.service" \
    /etc/systemd/system/strixctl-permissions.service
sed -i "s|/usr/lib/strixctl/strixctl-grant|$LIBDIR/strixctl-grant|" \
    /etc/systemd/system/strixctl-permissions.service

install -d /usr/lib/systemd/user
install -m 0644 "$SRC/systemd/strixctl-apply.service" \
    /usr/lib/systemd/user/strixctl-apply.service
sed -i "s|/usr/bin/strixctl|$BINDIR/strixctl|" \
    /usr/lib/systemd/user/strixctl-apply.service

install -d /usr/share/applications
install -m 0644 "$SRC/strixctl.desktop" /usr/share/applications/strixctl.desktop

udevadm control --reload-rules
udevadm trigger --subsystem-match=hwmon --subsystem-match=power_supply \
    --subsystem-match=leds --subsystem-match=hidraw 2>/dev/null || true

systemctl daemon-reload
systemctl enable --now strixctl-permissions.service

echo
echo "Installed.  Checking what is writable now:"
"$BINDIR/strixctl" status || true

TARGET_USER="${SUDO_USER:-}"
if [ -n "$TARGET_USER" ] && ! id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx "$GROUP"; then
    echo
    echo "WARNING: $TARGET_USER is not in the '$GROUP' group."
    echo "  sudo usermod -aG $GROUP $TARGET_USER   # then log out and back in"
fi

echo
echo "Next:"
echo "  strixctl status              what is available"
echo "  strixctl gui                 the control window"
echo "  systemctl --user enable --now strixctl-apply.service"
echo "                               restore the saved profile at login"
