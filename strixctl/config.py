"""User configuration: the fan curves, the keyboard colour, and the two
machine-wide knobs (power mode and charge limit).

Lives in ~/.config/strixctl/config.json so the whole thing stays writable
without root.

Every setting here is independent. There are no named profiles: the fans know
nothing about the keyboard, and changing one never writes the other.

Format versions
---------------
1 (no "version" key): fan points were (temp, pwm 0-255), and everything was
  bundled into named profiles (Silent/Balanced/Performance), each carrying its
  own curves, colour and keyboard brightness.
2: fan points are (temp, percent 0-100), the last point of every curve is 100,
  and kbd_brightness is gone. Still bundled into profiles.
3: no profiles. One fan curve, one keyboard colour, one power mode.

An older file is converted the first time it is loaded, and the original is
kept beside it as config.v1.json or config.v2.json.
"""

import copy
import json
import os
import shutil
import time

from . import hw

VERSION = 3

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "strixctl")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

#: Anything load() had to do behind the user's back, for the front ends to show.
warnings = []

#: The G712LU's balanced firmware curves in percent, the seed for a fresh
#: config. They are only a starting shape: "Start from the firmware curve"
#: reads the real one for whichever power mode is in force.
STOCK_CPU = [(35, 3), (52, 12), (58, 23), (64, 36),
             (70, 50), (76, 67), (80, 83), (80, 83)]
STOCK_GPU = [(35, 15), (35, 15), (45, 23), (50, 34),
             (56, 46), (63, 63), (70, 80), (70, 80)]


def _listed(points):
    return [list(point) for point in hw.with_full_ceiling(points)]


#: The G712 chassis is 4-zone, left to right.
ZONES = 4

DEFAULTS = {
    "version": VERSION,
    "battery": {"limit": 80, "enabled": False},
    # null means "search the usual places"; see aura.CANDIDATE_BINARIES.
    "openrgb": {"binary": None, "device_index": None},
    # "monochrome" (the default, and what any older value means) or
    # "system", which leaves the desktop's accent colour alone; see theme.py.
    "theme": "monochrome",
    # null means "leave the power mode wherever it is"; nothing is forced on a
    # machine whose owner has never picked one.
    "power": None,
    "fans": {"mode": "auto",
             "cpu": _listed(STOCK_CPU),
             "gpu": _listed(STOCK_GPU)},
    "aura": {"mode": "Static", "speed": 50, "colors": [[255, 255, 255]] * ZONES},
}


def _merge(base, override):
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _to_v2(data):
    """Fan points from the kernel's 0-255 to percent, and no brightness."""
    for prof in (data.get("profiles") or {}).values():
        if not isinstance(prof, dict):
            continue
        prof.pop("kbd_brightness", None)
        for key in ("fan_cpu", "fan_gpu"):
            if key not in prof:
                continue
            try:
                prof[key] = _listed([(int(t), hw.pwm_to_pct(p)) for t, p in prof[key]])
            except (TypeError, ValueError):
                continue


def _to_v3(data):
    """Flatten the active profile out into plain settings, and drop the rest."""
    profiles = data.pop("profiles", None) or {}
    name = data.pop("active_profile", None)
    prof = profiles.get(name)
    if not isinstance(prof, dict):
        prof = next((p for p in profiles.values() if isinstance(p, dict)), {})
    data["power"] = prof.get("platform_profile")
    curves = {}
    for fan, stock in (("cpu", STOCK_CPU), ("gpu", STOCK_GPU)):
        raw = prof.get(f"fan_{fan}")
        try:
            curves[fan] = _listed([(int(t), int(p)) for t, p in raw])
        except (TypeError, ValueError):
            curves[fan] = _listed(stock)
    data["fans"] = {"mode": "curve" if prof.get("fan_mode") == "curve" else "auto",
                    **curves}
    if isinstance(prof.get("aura"), dict):
        data["aura"] = prof["aura"]


def migrate(data):
    """Bring a parsed config up to VERSION in place.

    Returns the version it started at, or 0 when there was nothing to do.
    """
    if not isinstance(data, dict):
        return 0
    start = data.get("version", 1)
    if not isinstance(start, int) or start >= VERSION:
        return 0
    if start < 2:
        _to_v2(data)
    if start < 3:
        _to_v3(data)
    data["version"] = VERSION
    return start


def _set_aside(name):
    """Copy the file on disk out of the way; returns the copy's path or None."""
    target = os.path.join(CONFIG_DIR, name)
    try:
        if os.path.exists(target):
            stamp = time.strftime("%Y%m%d-%H%M%S")
            root, ext = os.path.splitext(target)
            target = f"{root}.{stamp}{ext}"
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

    # Read before migrating: _to_v3 takes these away.
    was_named = data.get("active_profile")
    started_at = migrate(data)
    if started_at:
        kept = _set_aside(f"config.v{started_at}.json")
        note = []
        if started_at < 2:
            note.append("Fan speeds are percent now.")
        if was_named:
            note.append(f"Profiles are gone, so the settings from {was_named} "
                        "are now simply the settings.")
        note.append(f"The old file is at {kept}." if kept else "")
        try:
            save(data)
            warnings.append(" ".join(part for part in note if part))
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


def curves(config):
    """Both stored curves as [(temp, percent), ...], keyed by fan."""
    fans = config.get("fans") or {}
    return {fan: points(fans.get(fan) or []) for fan in ("cpu", "gpu")}


def points(raw):
    """Normalise JSON [[t, p], ...] into [(t, p), ...], p in percent."""
    return [(int(t), int(p)) for t, p in raw]
