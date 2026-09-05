#!/bin/sh
# Fetch OpenRGB, which strixctl uses for keyboard colour and nothing else.
# Every other control works without it.
#
# Deliberately not folded into install.sh. That script runs as root, and
# pulling a 33 MB binary off the internet and unpacking it with root privileges
# is a poor trade for saving one command. Nothing here needs privileges, and it
# refuses to run with them.
#
#   ./get-openrgb.sh           into ~/.local/share/strixctl/openrgb
#   ./get-openrgb.sh --here    into ./openrgb, beside this script
#
# Debian 13 ships no libfuse2, so a type-2 AppImage cannot mount itself. It is
# extracted instead, which needs nothing but the AppImage.

set -eu

VERSION="1.0rc3.1"
URL="https://codeberg.org/OpenRGB/OpenRGB/releases/download/release_candidate_${VERSION}/OpenRGB_${VERSION}_x86_64_5e81e26.AppImage"
SHA256="42910311b364ae525ca593f53f5fadcf746b4de41e9e49302a5aa5dd614a608a"

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/strixctl/openrgb"
[ "${1:-}" = "--here" ] && DEST="$SRC/openrgb"

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as yourself, not with sudo. It installs into your home," >&2
    echo "and OpenRGB does not need root once the udev rule is in place." >&2
    exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
    echo "curl is needed to download OpenRGB (sudo apt install curl)" >&2
    exit 1
fi

if command -v openrgb >/dev/null 2>&1; then
    echo "OpenRGB is already on PATH at $(command -v openrgb). Nothing to do."
    exit 0
fi

APPRUN="$DEST/squashfs-root/AppRun"
if [ -x "$APPRUN" ]; then
    echo "Already present at $APPRUN"
    exit 0
fi

mkdir -p "$DEST"
tmp="$(mktemp "${TMPDIR:-/tmp}/openrgb.XXXXXX")"
trap 'rm -f "$tmp"' EXIT INT TERM

echo "Downloading OpenRGB $VERSION"
curl -fL --progress-bar -o "$tmp" "$URL"

# A pinned checksum, because this script hands the result an RGB controller on
# your USB bus. A mismatch means the release moved or the download is damaged;
# either way it should not be run.
echo "Verifying"
if ! printf '%s  %s\n' "$SHA256" "$tmp" | sha256sum -c - >/dev/null 2>&1; then
    echo "Checksum mismatch. Refusing to use this download." >&2
    echo "  expected $SHA256" >&2
    echo "  got      $(sha256sum < "$tmp" | cut -d' ' -f1)" >&2
    exit 1
fi

chmod +x "$tmp"
echo "Extracting into $DEST"
rm -rf "$DEST/squashfs-root"
(cd "$DEST" && "$tmp" --appimage-extract >/dev/null)

if [ ! -x "$APPRUN" ]; then
    echo "Extraction finished but produced no AppRun at $APPRUN" >&2
    exit 1
fi

echo
"$APPRUN" --version 2>/dev/null | head -1 || true
echo "Installed at $APPRUN"
echo
echo "strixctl searches this location on its own. Confirm with:"
echo "    strixctl status          # 'kbd colour' should show the zone count"
echo "    strixctl colour list     # what OpenRGB can see"
