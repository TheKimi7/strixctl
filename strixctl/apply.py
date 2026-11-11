"""Push the stored settings onto the hardware.

Shared by the CLI, the GUI and the systemd --user unit that restores state at
login. Each knob has its own function, and each is independent: applying a fan
curve never touches the keyboard, and a colour change never touches the fans.
One unwritable knob must not stop the rest, so every step records its own
result rather than raising.

Results are (knob, state, detail), where state is ok, skipped, unavailable or
failed.
"""

from . import config as cfg
from . import hw


def apply_battery(conf, results):
    battery = hw.Battery()
    settings = conf.get("battery") or {}
    if not settings.get("enabled"):
        results.append(("battery", "skipped", "limit disabled in config"))
        return
    if not battery.available:
        results.append(("battery", "unavailable", "no charge_control_end_threshold"))
        return
    try:
        battery.limit = settings.get("limit", 100)
        results.append(("battery", "ok", f"limit {settings.get('limit')}%"))
    except (OSError, ValueError) as exc:
        results.append(("battery", "failed", str(exc)))


def apply_power(conf, results):
    """The ACPI platform profile: quiet, balanced or performance."""
    wanted = conf.get("power")
    if not wanted:
        results.append(("power", "skipped", "no power mode stored"))
        return
    platform = hw.PlatformProfile()
    if not platform.available:
        results.append(("power", "unavailable", "no platform_profile"))
        return
    try:
        platform.profile = wanted
        results.append(("power", "ok", wanted))
    except (OSError, ValueError) as exc:
        results.append(("power", "failed", str(exc)))


def apply_fans(conf, results):
    """Arm the stored curves, or hand both fans back to the firmware."""
    fans = hw.Fans()
    if not fans.available:
        results.append(("fans", "unavailable", "no asus_custom_fan_curve hwmon"))
        return
    settings = conf.get("fans") or {}
    if settings.get("mode") == "curve":
        stored = cfg.curves(conf)
        for fan, curve in fans:
            try:
                curve.apply(stored[fan])
                results.append((f"fan_{fan}", "ok", "curve armed"))
            except (OSError, ValueError, KeyError) as exc:
                results.append((f"fan_{fan}", "failed", str(exc)))
    else:
        try:
            fans.reset()
            results.append(("fans", "ok", "firmware curve"))
        except OSError as exc:
            results.append(("fans", "failed", str(exc)))


def apply_aura(conf, results):
    # Keyboard brightness is not applied here or anywhere else: the desktop
    # owns it, and the colour write below is sent at the level it holds.
    from . import aura

    settings = conf.get("aura") or {}
    if not settings:
        results.append(("kbd_colour", "skipped", "no colour stored"))
        return
    backend = aura.Aura(settings=conf.get("openrgb"))
    if not backend.available:
        results.append(("kbd_colour", "unavailable", backend.why_unavailable()))
        return
    try:
        device = backend.apply(settings)
    except OSError as exc:
        results.append(("kbd_colour", "failed", str(exc)))
        return
    mode = settings.get("mode", "Static")
    if not aura.mode_takes_color(mode):
        detail = mode
    else:
        colors = aura.normalise_colors(settings, backend.zones)
        if device is not None and device.is_perkey:
            colors = colors[:1]
        detail = " ".join("#%02x%02x%02x" % tuple(c) for c in colors)
    results.append(("kbd_colour", "ok", detail))


def rearm_fans(conf=None):
    """Put the stored fan setting back without touching anything else.

    For when the kernel has dropped a custom curve on its own: it does that on
    every power mode change, and a resume from sleep can do it too.
    """
    results = []
    apply_fans(conf if conf is not None else cfg.load(), results)
    return results


def apply_all(conf=None):
    """Everything, in the order the hardware prefers.

    The power mode goes on before the fans because the firmware reloads its
    own curve when the mode changes, which would silently drop a custom curve
    armed beforehand.
    """
    conf = cfg.load() if conf is None else conf
    results = []
    apply_battery(conf, results)
    apply_power(conf, results)
    apply_fans(conf, results)
    apply_aura(conf, results)
    return results
