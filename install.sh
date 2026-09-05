#!/bin/sh
# strixctl installer.  Run once with sudo; after that nothing needs root.
#
#   sudo ./install.sh              install
#   sudo ./install.sh --with-deps  install, apt-getting the GTK bits first
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
    # $LIBDIR goes wholesale below, which takes get-openrgb.sh with it. What it
    # downloaded lives in the user's home and is left alone.
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

WITH_DEPS=""
[ "${1:-}" = "--with-deps" ] && WITH_DEPS=1

if ! getent group "$GROUP" >/dev/null; then
    echo "group '$GROUP' does not exist; set STRIXCTL_GROUP to one that does" >&2
    exit 1
fi

# Dependencies first, so this is a prerequisite check rather than a post-mortem.
# The CLI imports nothing outside the standard library, so missing GTK bindings
# are a warning and not a failure: 'strixctl status' works either way, and only
# the window and the tray need them.
probe_missing() {
    python3 - <<'PROBE'
missing = []
try:
    import gi
except Exception:
    missing.append("python3-gi")
else:
    try:
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # noqa: F401
    except Exception:
        missing.append("gir1.2-gtk-3.0")
    try:
        gi.require_foreign("cairo")
    except Exception:
        missing.append("python3-gi-cairo")
    try:
        gi.require_version("XApp", "1.0")
        from gi.repository import XApp  # noqa: F401
    except Exception:
        missing.append("gir1.2-xapp-1.0")
try:
    import cairo  # noqa: F401
except Exception:
    missing.append("python3-cairo")
print(" ".join(dict.fromkeys(missing)))
PROBE
}

MISSING="$(probe_missing)"
if [ -n "$MISSING" ]; then
    if [ -n "$WITH_DEPS" ]; then
        if ! command -v apt-get >/dev/null 2>&1; then
            echo "--with-deps needs apt-get, which is not on this system." >&2
            echo "Install these with your own package manager: $MISSING" >&2
            exit 1
        fi
        echo "Installing: $MISSING"
        apt-get install -y $MISSING
        MISSING="$(probe_missing)"
        [ -n "$MISSING" ] && echo "WARNING: still missing after apt: $MISSING"
    else
        echo "The window and tray need packages that are not installed:"
        echo "    $MISSING"
        echo "  Install them with:"
        echo "    sudo apt install $MISSING"
        echo "  Or re-run this script as: sudo ./install.sh --with-deps"
        echo "  Carrying on regardless. The CLI does not need them."
        echo
    fi
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

# Copied so it still works once the clone is gone. It refuses to run as root,
# so it is only ever invoked by the user afterwards, never from here.
install -m 0755 "$SRC/get-openrgb.sh" "$LIBDIR/get-openrgb.sh"

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

# Keyboard colour is the one control with an external dependency. Say so here
# rather than leaving the user to work out why that page is greyed out.
TARGET_HOME="$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)"
if ! command -v openrgb >/dev/null 2>&1 &&
   [ ! -x "$TARGET_HOME/.local/share/strixctl/openrgb/squashfs-root/AppRun" ]; then
    echo
    echo "Keyboard colour needs OpenRGB, which was not found."
    echo "  Fetch it as yourself, not with sudo, from this checkout:"
    echo "      ./get-openrgb.sh"
    echo "  Or later, once this checkout is gone:"
    echo "      $LIBDIR/get-openrgb.sh"
    echo "  Everything else works without it."
fi

echo
echo "Next:"
echo "  strixctl status              what is available"
echo "  strixctl gui                 the control window"
echo "  systemctl --user enable --now strixctl-apply.service"
echo "                               restore the saved profile at login"
