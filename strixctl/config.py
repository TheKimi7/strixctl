"""User configuration: the battery limit plus named profiles.

Lives in ~/.config/strixctl/config.json so the whole thing stays writable
without root.  A profile bundles everything you would switch at once: the
platform profile, both fan curves and the keyboard colour.

Keyboard brightness is deliberately not in here. The desktop owns it.

Format versions
---------------
1 (no "version" key): fan points were (temp, pwm 0-255) and profiles carried a
  kbd_brightness.
2: fan points are (temp, percent 0-100), the last point of every curve is 100,
  and kbd_brightness is gone.

A version 1 file is converted when it is first loaded, and the original is
kept beside it as config.v1.json.
"""

import copy
import json
import os
import shutil
import time

from . import hw

VERSION = 2

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "strixctl")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

#: Anything load() had to do behind the user's back, for the front ends to show.
warnings = []

#: The G712LU's balanced firmware curves in percent, the seed for a fresh
#: config. They are only a starting shape: "Load firmware curve" reads the real
#: one for whichever platform profile is in force.
STOCK_CPU = [(35, 3), (52, 12), (58, 23), (64, 36),
             (70, 50), (76, 67), (80, 83), (80, 83)]
STOCK_GPU = [(35, 15), (35, 15), (45, 23), (50, 34),
             (56, 46), (63, 63), (70, 80), (70, 80)]


def _scaled(points, factor, floor=0):
    out = []
    last = -1
    for temp, percent in points:
        value = max(floor, min(100, int(round(percent * factor))), last)
        out.append((temp, value))
        last = value
    return [list(point) for point in hw.with_full_ceiling(out)]


#: The G712 chassis is 4-zone, left to right.
ZONES = 4

DEFAULTS = {
    "version": VERSION,
    "battery": {"limit": 80, "enabled": False},
    # null means "search the usual places"; see aura.CANDIDATE_BINARIES.
    "openrgb": {"binary": None, "device_index": None},
    "active_profile": "Balanced",
    "profiles": {
        "Silent": {
            "platform_profile": "quiet",
            "fan_mode": "curve",
            "fan_cpu": _scaled(STOCK_CPU, 0.75),
            "fan_gpu": _scaled(STOCK_GPU, 0.75),
            "aura": {"mode": "Static", "speed": 50,
                     "colors": [[0, 80, 255]] * 4},
        },
        "Balanced": {
            "platform_profile": "balanced",
            "fan_mode": "auto",
            "fan_cpu": _scaled(STOCK_CPU, 1),
            "fan_gpu": _scaled(STOCK_GPU, 1),
            "aura": {"mode": "Static", "speed": 50,
                     "colors": [[255, 255, 255]] * 4},
        },
        "Performance": {
            "platform_profile": "performance",
            "fan_mode": "curve",
            "fan_cpu": _scaled(STOCK_CPU, 1.3, floor=15),
            "fan_gpu": _scaled(STOCK_GPU, 1.3, floor=15),
            # A gradient across the four zones, so the zone layout is visible
            # the first time someone switches to this profile.
            "aura": {"mode": "Static", "speed": 50,
                     "colors": [[255, 0, 0], [255, 60, 0],
                                [255, 120, 0], [255, 180, 0]]},
        },
    },
}


def _merge(base, override):
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def migrate(data):
    """Bring a parsed config up to VERSION in place. Returns True if it changed."""
    if not isinstance(data, dict) or data.get("version", 1) >= VERSION:
        return False
    for prof in (data.get("profiles") or {}).values():
        if not isinstance(prof, dict):
            continue
        prof.pop("kbd_brightness", None)
        for key in ("fan_cpu", "fan_gpu"):
            if key not in prof:
                continue
            try:
                converted = [(int(t), hw.pwm_to_pct(p)) for t, p in prof[key]]
            except (TypeError, ValueError):
                continue
            prof[key] = [list(point) for point in hw.with_full_ceiling(converted)]
    data["version"] = VERSION
    return True


def _set_aside(name):
    """Copy the file on disk out of the way; returns the copy's path or None."""
    target = os.path.join(CONFIG_DIR, name)
    try:
        if not os.path.exists(target):
            shutil.copy2(CONFIG_PATH, target)
        return target
    except OSError:
        return None


def load():
    del warnings[:]
    try:
        with open(CONFIG_PATH) as fh:
            raw = fh.read()
    except OSError:
        return copy.deepcopy(DEFAULTS)
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("top level is not an object")
    except ValueError as exc:
        # Falling back to defaults is right; doing it silently is not, because
        # the next save would overwrite whatever the user had.
        kept = _set_aside("config.broken-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
        warnings.append(
            f"{CONFIG_PATH} is not valid JSON ({exc}); using defaults."
            + (f" The unreadable file was copied to {kept}." if kept else ""))
        return copy.deepcopy(DEFAULTS)

    if migrate(data):
        kept = _set_aside("config.v1.json")
        try:
            save(data)
            warnings.append(
                "Config converted to percent fan speeds; every curve now ends "
                "at 100%." + (f" The old file is at {kept}." if kept else ""))
        except OSError:
            pass
    return _merge(DEFAULTS, data)


def save(config):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    config["version"] = VERSION
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(config, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, CONFIG_PATH)


def mtime():
    try:
        return os.stat(CONFIG_PATH).st_mtime_ns
    except OSError:
        return 0


def profile(config, name=None):
    name = name or config.get("active_profile")
    return config["profiles"].get(name)


def profile_for_platform(config, platform):
    """Name of a profile that uses `platform`, preferring the active one."""
    active = config.get("active_profile")
    names = [active] + [n for n in config["profiles"] if n != active]
    for name in names:
        prof = config["profiles"].get(name)
        if prof and prof.get("platform_profile") == platform:
            return name
    return None


def points(raw):
    """Normalise JSON [[t, p], ...] into [(t, p), ...], p in percent."""
    return [(int(t), int(p)) for t, p in raw]
