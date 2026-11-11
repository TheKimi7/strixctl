"""Apply a stored profile to the hardware.

Shared by the CLI, the GUI and the systemd --user unit that restores state at
login.  Each step is independent: one unwritable knob must not stop the rest.
"""

from . import config as cfg
from . import hw


def apply_battery(conf, results):
    battery = hw.Battery()
    settings = conf.get("battery", {})
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


def apply_profile(conf, name, results):
    prof = cfg.profile(conf, name)
    if prof is None:
        results.append(("profile", "failed", f"no profile named {name!r}"))
        return

    platform = hw.PlatformProfile()
    wanted = prof.get("platform_profile")
    if wanted and platform.available:
        try:
            platform.profile = wanted
            results.append(("platform_profile", "ok", wanted))
        except (OSError, ValueError) as exc:
            results.append(("platform_profile", "failed", str(exc)))

    # The firmware reloads its own curve when the platform profile changes, so
    # the custom curve has to go on afterwards, never before.
    apply_fans(prof, results)
    apply_aura(conf, prof, results)


def apply_fans(prof, results):
    """Arm the profile's curves, or hand both fans back to the firmware."""
    fans = hw.Fans()
    if not fans.available:
        return
    if prof.get("fan_mode") == "curve":
        for fan, curve in fans:
            try:
                curve.apply(cfg.points(prof[f"fan_{fan}"]))
                results.append((f"fan_{fan}", "ok", "curve armed"))
            except (OSError, ValueError, KeyError) as exc:
                results.append((f"fan_{fan}", "failed", str(exc)))
    else:
        try:
            fans.reset()
            results.append(("fans", "ok", "firmware curve"))
        except OSError as exc:
            results.append(("fans", "failed", str(exc)))


def apply_aura(conf, prof, results):
    # Keyboard brightness is not applied here or anywhere else: the desktop
    # owns it, and the colour write below is sent at the level it holds.
    from . import aura
    backend = aura.Aura(settings=conf.get("openrgb"))
    wanted_aura = prof.get("aura")
    if wanted_aura and backend.available:
        try:
            device = backend.apply(wanted_aura)
            colors = aura.normalise_colors(wanted_aura, backend.zones)
            mode = wanted_aura.get("mode", "Static")
            if not aura.mode_takes_color(mode):
                detail = mode
            else:
                if device is not None and device.is_perkey:
                    colors = colors[:1]
                detail = " ".join("#%02x%02x%02x" % tuple(c) for c in colors)
            results.append(("kbd_colour", "ok", detail))
        except OSError as exc:
            results.append(("kbd_colour", "failed", str(exc)))
    elif wanted_aura:
        results.append(("kbd_colour", "unavailable", backend.why_unavailable()))


def rearm_fans(name=None):
    """Put the profile's fan setting back without touching anything else.

    For when the kernel has dropped a custom curve on its own: it does that on
    every platform profile change, and a resume from sleep can do it too.
    """
    conf = cfg.load()
    results = []
    prof = cfg.profile(conf, name)
    if prof is not None:
        apply_fans(prof, results)
    return results


def apply_all(name=None):
    conf = cfg.load()
    results = []
    apply_battery(conf, results)
    apply_profile(conf, name or conf.get("active_profile"), results)
    return results
