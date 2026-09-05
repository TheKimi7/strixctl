"""User configuration: the battery limit plus named profiles.

Lives in ~/.config/strixctl/config.json so the whole thing stays writable
without root.  A profile bundles everything you would switch at once: the
platform profile, both fan curves, keyboard brightness and (once the Aura
backend is verified) a keyboard colour.
"""

import copy
import json
import os

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "strixctl")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

#: Stock G712LU curves, used as the seed for a fresh config.
STOCK_CPU = [(35, 7), (52, 30), (58, 58), (64, 91),
             (70, 127), (76, 170), (80, 211), (80, 211)]
STOCK_GPU = [(35, 38), (35, 38), (45, 58), (50, 86),
             (56, 117), (63, 160), (70, 204), (70, 204)]


def _scaled(points, factor, floor=0):
    out = []
    last = -1
    for temp, pwm in points:
        value = max(floor, min(255, int(round(pwm * factor))), last)
        out.append((temp, value))
        last = value
    return out


#: The G712 chassis is 4-zone, left to right.
ZONES = 4

DEFAULTS = {
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
            "kbd_brightness": 1,
            "aura": {"mode": "Static", "speed": 50,
                     "colors": [[0, 80, 255]] * 4},
        },
        "Balanced": {
            "platform_profile": "balanced",
            "fan_mode": "auto",
            "fan_cpu": list(map(list, STOCK_CPU)),
            "fan_gpu": list(map(list, STOCK_GPU)),
            "kbd_brightness": 2,
            "aura": {"mode": "Static", "speed": 50,
                     "colors": [[255, 255, 255]] * 4},
        },
        "Performance": {
            "platform_profile": "performance",
            "fan_mode": "curve",
            "fan_cpu": _scaled(STOCK_CPU, 1.3, floor=40),
            "fan_gpu": _scaled(STOCK_GPU, 1.3, floor=40),
            "kbd_brightness": 3,
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


def load():
    try:
        with open(CONFIG_PATH) as fh:
            return _merge(DEFAULTS, json.load(fh))
    except (OSError, ValueError):
        return copy.deepcopy(DEFAULTS)


def save(config):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(config, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, CONFIG_PATH)


def profile(config, name=None):
    name = name or config.get("active_profile")
    return config["profiles"].get(name)


def points(raw):
    """Normalise JSON [[t, p], ...] into [(t, p), ...]."""
    return [(int(t), int(p)) for t, p in raw]
