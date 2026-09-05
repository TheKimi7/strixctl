"""Keyboard colour, driven through the OpenRGB CLI.

Why OpenRGB rather than raw HID
-------------------------------
There is no sysfs path to keyboard colour on this chassis: `asus::kbd_backlight`
exposes brightness only, and nothing under /sys/class/leds carries a colour
attribute. Colour means HID writes to the ASUS N-KEY device (0b05:1866).

Rather than reconstruct that packet format, strixctl shells out to OpenRGB,
which already implements it and is maintained by people with the hardware. The
cost is a subprocess and a few seconds per change; the benefit is that nobody
here is guessing bytes at an embedded controller.

The SDK socket protocol was considered and rejected: parsing the controller
data blob means several hundred lines of variable-length decoding (u16-prefixed
strings, mode and zone arrays, protocol-version-conditional fields) against a
spec that does not state its endianness. For a knob that changes on a profile
switch, `subprocess.run` is the better trade.

Device selection on the G712LU
------------------------------
OpenRGB reports the one physical keyboard six times -- once each on hidraw1 and
hidraw2, and four times on hidraw3 -- plus a seventh entry, a per-key "G533ZM"
one (also on hidraw3), because it has no G712LU profile ("device capabilities
not found").

Both kinds of entry drive this chassis; they are the same keyboard seen two
ways, and which one to use depends on the effect:

  * The 4-zone "ASUS Aura Keyboard" entries expose Static, Breathing and Color
    Cycle, and accept four independent colours -- one per zone. Confirmed by
    setting the zones to four different colours and looking at the keyboard.
  * The 83-LED per-key entry exposes fifteen effects, including Flashing,
    Spectrum Cycle and Rainbow Wave, which the 4-zone entries do not offer. A
    colour list sent here would paint the first four *keys* rather than the
    four zones, so it is only used for effects that need at most one colour.

An earlier version of this file claimed the per-key entry did not drive the
chassis. That was wrong: telling it to run Rainbow Wave visibly changed the
keyboard, as did setting it to a static colour.

So devices are matched on name, LED count and advertised effect, never on a
bare index -- the indices shuffle between runs.
"""

import os
import re
import shutil
import subprocess

from . import config as cfg

#: Names OpenRGB gives the working 4-zone interface.
NAME_PATTERN = re.compile(r"aura.*keyboard", re.IGNORECASE)

#: The G712 chassis is 4-zone. Kept in step with config.ZONES, which is what
#: the GUI and the stored profiles count in.
EXPECTED_ZONES = cfg.ZONES

#: Which OpenRGB entry serves an effect.
ZONED = "zoned"      # the 4-zone entries: per-zone colour, three effects
PERKEY = "perkey"    # the 83-LED entry: one colour, fifteen effects

#: The effects strixctl offers, in Aura Core's order. Deliberately a subset of
#: what the per-key entry advertises: the rest (Starry Night, Rain, the
#: Reactive family, Comet, Flash N Dash, Keystone) are written for per-key
#: hardware and have not been checked against a 4-zone board, so they are not
#: offered rather than offered untested.
MODES = (
    ("Static", ZONED),
    ("Breathing", ZONED),
    ("Flashing", PERKEY),
    ("Spectrum Cycle", PERKEY),
    ("Rainbow Wave", PERKEY),
)

MODE_ROLES = {name.lower(): role for name, role in MODES}

TIMEOUT = 30

#: Searched in order. AppRun paths come first because a type-2 AppImage needs
#: libfuse2, which Debian 13 does not ship -- extracting it is the usual fix.
CANDIDATE_BINARIES = (
    "~/Downloads/squashfs-root/AppRun",
    "~/Applications/squashfs-root/AppRun",
    "/opt/openrgb/AppRun",
    "/opt/OpenRGB/squashfs-root/AppRun",
    "~/.local/share/openrgb/squashfs-root/AppRun",
    "~/Downloads/OpenRGB.AppImage",
    "~/Applications/OpenRGB.AppImage",
)


class Device:
    def __init__(self, index, name):
        self.index = index
        self.name = name
        self.location = ""
        self.leds = 0
        self.modes = []

    @property
    def is_zoned(self):
        """A 4-zone entry: per-zone colour, but only three effects."""
        return bool(NAME_PATTERN.search(self.name)) and self.leds == EXPECTED_ZONES

    @property
    def is_perkey(self):
        """The per-key entry: every effect, but one colour for all of them."""
        return self.leds > EXPECTED_ZONES

    @property
    def usable(self):
        """Both kinds drive this keyboard; only strays are unusable."""
        return self.is_zoned or self.is_perkey

    def serves(self, mode):
        wanted = (mode or "").strip().lower()
        return any(wanted == m.strip().lower() for m in self.modes)

    def __repr__(self):
        return f"<{self.index}: {self.name} ({self.leds} zones, {self.location})>"


def find_binary(override=None):
    for candidate in filter(None, (override,)):
        path = os.path.expanduser(candidate)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    for name in ("openrgb", "OpenRGB"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in CANDIDATE_BINARIES:
        path = os.path.expanduser(candidate)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


#: Modes that ignore any colour you give them -- they cycle through their own.
COLOURLESS = ("color cycle", "spectrum cycle", "rainbow", "rainbow wave", "off")
#: Modes with nothing to animate, so no speed control.
SPEEDLESS = ("static", "direct", "off")


def parse_modes(text):
    """Pull mode names out of a line like: [Static] Breathing 'Color Cycle'.

    Names may be quoted when they contain spaces, and the device's currently
    active mode is wrapped in square brackets.
    """
    modes = []
    for quoted, bare in re.findall(r"'([^']+)'|(\[?[\w][\w-]*\]?)", text):
        name = (quoted or bare).strip().strip("[]")
        if name:
            modes.append(name)
    return modes


def mode_takes_color(mode):
    return mode.strip().lower() not in COLOURLESS


def mode_takes_speed(mode):
    return mode.strip().lower() not in SPEEDLESS


def parse_devices(text):
    """Parse `OpenRGB --list-devices` output into Device objects."""
    devices, current = [], None
    for line in text.splitlines():
        header = re.match(r"^(\d+):\s+(.*)$", line)
        if header:
            current = Device(int(header.group(1)), header.group(2).strip())
            devices.append(current)
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped.startswith("Location:"):
            current.location = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("LEDs:"):
            current.leds = len(re.findall(r"'[^']*'", stripped))
        elif stripped.startswith("Modes:"):
            current.modes = parse_modes(stripped.split(":", 1)[1])
    return devices


class Aura:
    """Keyboard colour via OpenRGB. Zone count is fixed by the chassis."""

    def __init__(self, config=None, settings=None):
        self.settings = settings or {}
        self.binary = find_binary(self.settings.get("binary"))
        self.config = config or {}
        self._devices = None

    # --------------------------------------------------------------- probing

    @property
    def available(self):
        return self.binary is not None

    def why_unavailable(self, mode=None):
        if self.binary is None:
            return ("OpenRGB not found. Install it and either put it on PATH or "
                    "set openrgb.binary in config.json — searched: "
                    + ", ".join(CANDIDATE_BINARIES))
        if mode is not None and self.device_for(mode) is None:
            return ("OpenRGB found at %s, but no entry it lists offers %r"
                    % (self.binary, mode))
        if self.find_keyboard() is None:
            return ("OpenRGB found at %s, but it lists no %d-zone Aura keyboard"
                    % (self.binary, EXPECTED_ZONES))
        return "available"

    def _run(self, args):
        return subprocess.run(
            [self.binary, "--noautoconnect", *args],
            capture_output=True, text=True, timeout=TIMEOUT)

    def list_devices(self, refresh=False):
        """Devices OpenRGB can see. Cached per instance -- detection is ~1.3s."""
        if self._devices is not None and not refresh:
            return self._devices
        if self.binary is None:
            self._devices = []
            return self._devices
        try:
            result = self._run(["--list-devices"])
        except (OSError, subprocess.SubprocessError):
            self._devices = []
            return self._devices
        self._devices = parse_devices(result.stdout)
        return self._devices

    def find_keyboard(self, refresh=False):
        """The 4-zone entry -- the default target and what `zones` counts."""
        for device in self.list_devices(refresh=refresh):
            if device.is_zoned:
                return device
        return None

    def device_for(self, mode, refresh=False):
        """The entry that serves `mode`, per the MODES table.

        Falls back to any entry advertising the effect, so a mode that moves
        between entries in a future OpenRGB release still resolves.
        """
        role = MODE_ROLES.get((mode or "").strip().lower(), ZONED)
        devices = self.list_devices(refresh=refresh)
        preferred = (lambda d: d.is_zoned) if role == ZONED else (
            lambda d: d.is_perkey)
        for test in (lambda d: preferred(d) and d.serves(mode),
                     lambda d: d.usable and d.serves(mode)):
            for device in devices:
                if test(device):
                    return device
        return self.find_keyboard() if role == ZONED else None

    @property
    def zones(self):
        return EXPECTED_ZONES

    # --------------------------------------------------------------- writing

    def apply(self, settings):
        colors = normalise_colors(settings, EXPECTED_ZONES)
        return self.set_colors(colors,
                               mode=settings.get("mode", "Static"),
                               speed=settings.get("speed"))

    @property
    def modes(self):
        """The effects strixctl offers that this hardware actually reports.

        The MODES table is the menu; the hardware decides what stays on it, so
        an OpenRGB that stops advertising an effect drops it from the UI rather
        than leaving a control that silently does nothing.
        """
        devices = [d for d in self.list_devices() if d.usable]
        return [name for name, _role in MODES
                if any(d.serves(name) for d in devices)]

    def set_colors(self, colors, mode="Static", speed=None):
        """Set every zone at once. Returns the device that was written."""
        if self.binary is None:
            raise OSError(self.why_unavailable())

        index = self.settings.get("device_index")
        if index is None:
            device = self.device_for(mode)
            if device is None:
                raise OSError(self.why_unavailable(mode))
            index = device.index
        else:
            device = next((d for d in self.list_devices() if d.index == index), None)

        args = ["--device", str(index), "--mode", mode]
        if speed is not None and mode_takes_speed(mode):
            args += ["--speed", str(max(0, min(100, int(speed))))]
        if mode_takes_color(mode):
            # A colour list is per-zone on a 4-zone entry, but per-*key* on the
            # 83-LED one, where it would paint four keys and leave the rest
            # dark. Effects served by that entry therefore send a single
            # colour, which OpenRGB applies to every LED.
            single = device is not None and device.is_perkey
            chosen = [colors[0]] if single else colors
            args += ["--color",
                     ",".join("%02x%02x%02x" % tuple(c) for c in chosen)]

        try:
            result = self._run(args)
        except subprocess.TimeoutExpired as exc:
            raise OSError(f"OpenRGB timed out after {TIMEOUT}s") from exc
        except OSError as exc:
            raise OSError(f"could not run {self.binary}: {exc}") from exc

        # OpenRGB exits 0 even when it rejects the request outright -- an
        # unsupported mode prints "Error: ..." and still returns success -- so
        # the output has to be read rather than the exit status trusted.
        output = (result.stdout or "") + (result.stderr or "")
        for line in output.splitlines():
            if line.strip().startswith("Error:"):
                raise OSError("OpenRGB: " + line.strip()[len("Error:"):].strip())
        if result.returncode != 0:
            raise OSError(f"OpenRGB exited {result.returncode}")
        return device


def rgb_to_hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(c))) for c in rgb)


def hex_to_rgb(text):
    """Parse a colour written as #rgb, #rrggbb, or either without the hash."""
    cleaned = text.strip().lstrip("#")
    if len(cleaned) == 3:
        cleaned = "".join(c * 2 for c in cleaned)
    if len(cleaned) != 6:
        raise ValueError(f"{text!r} is not a 3- or 6-digit hex colour")
    try:
        return [int(cleaned[i:i + 2], 16) for i in (0, 2, 4)]
    except ValueError:
        raise ValueError(f"{text!r} is not a hex colour") from None


def normalise_colors(settings, zones):
    """Accept either the 4-zone `colors` list or a legacy single `color`."""
    colors = settings.get("colors")
    if not colors:
        single = settings.get("color") or [255, 255, 255]
        colors = [single] * zones
    colors = [list(c) for c in colors][:zones]
    while len(colors) < zones:
        colors.append(list(colors[-1]) if colors else [255, 255, 255])
    return [[max(0, min(255, int(channel))) for channel in color]
            for color in colors]
