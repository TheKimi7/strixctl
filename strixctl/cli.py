"""Command line front end.  Also the entry point the systemd user unit calls."""

import argparse
import filecmp
import os
import sys
import time

from . import apply as apply_mod
from . import aura as aura_mod
from . import config as cfg
from . import hw

OK = "ok"


def _state_line(label, available, writable, value):
    if not available:
        state = "not present"
    elif not writable:
        state = "read-only (run install.sh)"
    else:
        state = "ready"
    return f"  {label:<20} {str(value):<28} {state}"


def _curve_text(points):
    return "  ".join(f"{t}C:{p}%" for t, p in points)


def _fan_state(fans):
    armed = fans.armed
    if all(armed.values()):
        return "custom"
    if not any(armed.values()):
        return "firmware"
    return ", ".join(f"{name} {'custom' if on else 'firmware'}"
                     for name, on in armed.items())


def stale_install():
    """Files that differ between this installed copy and the checkout it came from.

    An install that has fallen behind the checkout is the classic way for a
    fix to "not work", so status says so instead of leaving it to be found.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    marker = os.path.join(os.path.dirname(here), "INSTALLED_FROM")
    try:
        with open(marker) as fh:
            source = fh.read().strip()
    except OSError:
        return None, []
    theirs = os.path.join(source, "strixctl")
    if not os.path.isdir(theirs):
        return source, []
    names = sorted(n for n in set(os.listdir(here)) | set(os.listdir(theirs))
                   if n.endswith(".py"))
    differing = []
    for name in names:
        mine, other = os.path.join(here, name), os.path.join(theirs, name)
        if not (os.path.exists(mine) and os.path.exists(other)
                and filecmp.cmp(mine, other, shallow=False)):
            differing.append(name)
    return source, differing


def _print_warnings():
    for warning in cfg.warnings:
        print(f"note: {warning}", file=sys.stderr)


def cmd_status(args):
    conf = cfg.load()
    _print_warnings()
    battery, platform = hw.Battery(), hw.PlatformProfile()
    fans, kbd = hw.Fans(), hw.KeyboardBacklight()
    sensors = hw.Sensors()
    aura = aura_mod.Aura(settings=conf.get("openrgb"))

    print(f"strixctl -- {hw.board_name()}")
    print()
    print(_state_line("battery limit",
                      battery.available, battery.writable,
                      f"{battery.limit}%" if battery.limit else "?"))
    print(_state_line("power mode",
                      platform.available, platform.writable, platform.profile))
    print(_state_line("fan curves",
                      fans.available, fans.writable,
                      _fan_state(fans) if fans.available else "-"))
    keyboard = aura.find_keyboard() if aura.available else None
    print(_state_line("kbd colour", aura.available, keyboard is not None,
                      f"{keyboard.leds} zones" if keyboard else "no device"))
    if aura.available and keyboard is None:
        print(f"    {aura.why_unavailable()}")
    if kbd.available:
        # Not a strixctl control, so no ready/read-only state to report.
        print(f"  {'kbd brightness':<20} "
              f"{f'{kbd.brightness}/{kbd.max_brightness}':<28} set by the desktop")
    if fans.available:
        print()
        temp, rpm = sensors.cpu_temp(), sensors.rpm()
        if temp is not None:
            print(f"  cpu temperature      {temp}C")
        armed = fans.armed
        for name, curve in fans.curves().items():
            speed = rpm.get(name)
            what = "custom curve" if armed[name] else "firmware curve"
            print(f"  fan {name}: {f'{speed} rpm' if speed is not None else '? rpm':<9} "
                  f"{what}: {_curve_text(curve)}")
    source, differing = stale_install()
    if differing:
        print()
        print(f"  WARNING: this install is out of step with {source}")
        print(f"    differs: {', '.join(differing)}")
        print(f"    re-run: sudo {source}/install.sh")
    return 0


def _report(results):
    failed = 0
    for knob, state, detail in results:
        marker = {"ok": "+", "skipped": ".", "unavailable": ".", "failed": "!"}[state]
        if state == "failed":
            failed += 1
        print(f"{marker} {knob:<18} {detail}")
    return 1 if failed else 0


def cmd_apply(args):
    """Everything that is stored, which is what the login unit runs."""
    results = apply_mod.apply_all()
    _print_warnings()
    return _report(results)


def cmd_power(args):
    """Set the power mode, and put the fan setting back afterwards.

    The firmware reloads its own curve whenever the mode changes, so a custom
    curve has to be re-armed or it silently stops applying.
    """
    platform = hw.PlatformProfile()
    if not platform.available:
        print("no platform_profile on this machine", file=sys.stderr)
        return 1
    if not args.mode:
        for name in platform.choices:
            print(f"{'*' if name == platform.profile else ' '} {name}")
        return 0
    conf = cfg.load()
    try:
        platform.profile = args.mode
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    conf["power"] = args.mode
    cfg.save(conf)
    print(f"power mode {args.mode}")
    return _report(apply_mod.rearm_fans(conf))


def cmd_battery(args):
    conf = cfg.load()
    if args.limit == "off":
        conf["battery"]["enabled"] = False
        cfg.save(conf)
        battery = hw.Battery()
        if battery.writable:
            battery.limit = 100
        print("charge limit off (100%)")
        return 0
    conf["battery"] = {"limit": int(args.limit), "enabled": True}
    cfg.save(conf)
    battery = hw.Battery()
    if not battery.writable:
        print("saved, but the sysfs attribute is read-only -- run install.sh",
              file=sys.stderr)
        return 1
    battery.limit = int(args.limit)
    print(f"charge limit {args.limit}%")
    return 0


#: `fans test` holds each of these for SETTLE seconds. Flat up to 70 C and
#: full speed above it, so the test cannot cook anything.
TEST_STEPS = (40, 75)
TEST_SETTLE = 18
TEST_MIN_SWING = 300


def _plateau(percent):
    return [(30, percent), (40, percent), (50, percent), (60, percent),
            (65, percent), (70, percent), (75, 100), (80, 100)]


def _watch(sensors, seconds, label):
    """Print readings for `seconds`; return the last rpm reading."""
    rpm = {}
    end = time.monotonic() + seconds
    while True:
        rpm = sensors.rpm()
        speeds = "  ".join(f"{name} {value} rpm" for name, value in rpm.items())
        print(f"  {label:<14} {sensors.cpu_temp()}C  {speeds}", flush=True)
        if time.monotonic() >= end:
            return rpm
        time.sleep(3)


def cmd_fans_test(fans):
    """Arm two flat curves and see whether the fans follow them."""
    sensors = hw.Sensors()
    if not sensors.fan_paths:
        print("no fan speed sensors to measure with", file=sys.stderr)
        return 1
    temp = sensors.cpu_temp()
    if temp is not None and temp >= 70:
        print(f"CPU is at {temp}C; the test holds the fans at a fixed speed "
              "below 70C, so run it when the machine is idle.", file=sys.stderr)
        return 1
    was_armed = {name: curve.read() for name, curve in fans if curve.armed}
    readings = {}
    print("Takes about a minute. The fans will change speed twice.")
    try:
        readings["before"] = _watch(sensors, 6, "now")
        for percent in TEST_STEPS:
            for _name, curve in fans:
                curve.apply(_plateau(percent))
            readings[percent] = _watch(sensors, TEST_SETTLE, f"held at {percent}%")
    finally:
        # Whatever happens, do not leave a test curve armed.
        fans.reset()
        for name, points in was_armed.items():
            getattr(fans, name).apply(points)
    print("  restored:", _fan_state(fans))

    low, high = readings[TEST_STEPS[0]], readings[TEST_STEPS[1]]
    failed = 0
    for name in sensors.fan_paths:
        if low.get(name) is None or high.get(name) is None:
            print(f"! {name} fan: no reading")
            failed += 1
            continue
        swing = high[name] - low[name]
        if swing >= TEST_MIN_SWING:
            print(f"+ {name} fan follows a custom curve "
                  f"({low[name]} rpm at {TEST_STEPS[0]}%, "
                  f"{high[name]} rpm at {TEST_STEPS[1]}%)")
        else:
            print(f"! {name} fan did not respond "
                  f"({low[name]} rpm at {TEST_STEPS[0]}%, "
                  f"{high[name]} rpm at {TEST_STEPS[1]}%)")
            failed += 1
    return 1 if failed else 0


def cmd_fans(args):
    fans = hw.Fans()
    if not fans.available:
        print("no asus_custom_fan_curve hwmon node", file=sys.stderr)
        return 1
    if args.action == "watch":
        sensors = hw.Sensors()
        try:
            while True:
                _watch(sensors, 0, _fan_state(fans))
                time.sleep(2)
        except KeyboardInterrupt:
            return 0
    if not fans.writable:
        print("fan curve attributes are read-only -- run install.sh", file=sys.stderr)
        return 1
    if args.action == "test":
        return cmd_fans_test(fans)
    fans.reset()
    print("both fans handed back to the firmware curve")
    return 0


def cmd_colour(args):
    """Set every zone, or list what OpenRGB can see."""
    conf = cfg.load()
    aura = aura_mod.Aura(settings=conf.get("openrgb"))
    if not aura.available:
        print(aura.why_unavailable(), file=sys.stderr)
        return 1

    if args.colors == ["list"]:
        for device in aura.list_devices():
            if device.is_zoned:
                mark, role = "z", "per-zone colour, three effects"
            elif device.is_perkey:
                mark, role = "k", "one colour, every effect"
            else:
                mark, role = " ", ""
            print(f"{mark} {device.index}: {device.name} "
                  f"({device.leds} leds, {device.location})  {role}")
        print("\nz = zoned entry, k = per-key entry. Both are the same "
              "keyboard, and\nthe effect decides which one a write goes to.")
        if aura.modes:
            print("\neffects:")
            for mode in aura.modes:
                device = aura.device_for(mode)
                bits = []
                if aura_mod.mode_takes_color(mode):
                    bits.append("colour")
                if aura_mod.mode_takes_speed(mode):
                    bits.append("speed")
                print(f"  {mode:<16} device {device.index if device else '-':<3} "
                      f"{', '.join(bits) or 'no options'}")
        return 0

    try:
        colors = [aura_mod.hex_to_rgb(value) for value in args.colors]
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    colors = aura_mod.normalise_colors({"colors": colors}, aura.zones)

    stored = conf.get("aura") or {}
    mode = args.mode or stored.get("mode", "Static")
    speed = args.speed if args.speed is not None else stored.get("speed")
    try:
        device = aura.set_colors(colors, mode=mode, speed=speed)
    except OSError as exc:
        print(exc, file=sys.stderr)
        return 1
    # Report what was actually sent: the per-key entry takes one colour, so
    # echoing four here would claim a gradient that never left the process.
    if not aura_mod.mode_takes_color(mode):
        detail = mode
    else:
        sent = colors[:1] if (device is not None and device.is_perkey) else colors
        detail = "%s %s" % (mode,
                            " ".join("#%02x%02x%02x" % tuple(c) for c in sent))
    print(f"set {detail} -> {device}")

    if args.save:
        stored = conf.setdefault("aura", {})
        stored["colors"] = colors
        stored["mode"] = mode
        if speed is not None:
            stored["speed"] = speed
        cfg.save(conf)
        print(f"saved to {cfg.CONFIG_PATH}")
    return 0


#: The GUI and tray are the only parts with dependencies beyond the standard
#: library, and they are system packages rather than anything pip can supply:
#: a GObject typelib is not a Python package. So they are imported lazily and
#: the failure is reported in terms of what to install.
GTK_PACKAGES = "python3-gi python3-gi-cairo gir1.2-gtk-3.0"
TRAY_PACKAGES = GTK_PACKAGES + " gir1.2-xapp-1.0"


def _missing_bindings(what, packages, exc):
    print(f"{what} needs GTK 3 bindings that are not installed:", file=sys.stderr)
    print(f"  {exc}", file=sys.stderr)
    print("On Debian or Ubuntu:", file=sys.stderr)
    print(f"  sudo apt install {packages}", file=sys.stderr)
    print("Everything else works without them; try 'strixctl status'.",
          file=sys.stderr)
    return 1


def cmd_gui(args):
    # ValueError as well as ImportError: a missing typelib fails inside
    # gi.require_version, not at the import itself.
    try:
        from .gui import main as gui_main
    except (ImportError, ValueError) as exc:
        return _missing_bindings("The window", GTK_PACKAGES, exc)
    return gui_main()


def cmd_tray(args):
    try:
        from .tray import main as tray_main
    except (ImportError, ValueError) as exc:
        return _missing_bindings("The tray icon", TRAY_PACKAGES, exc)
    return tray_main()


def build_parser():
    parser = argparse.ArgumentParser(
        prog="strixctl", description="ASUS ROG laptop controls for Linux")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="show every knob and whether it is writable")

    sub.add_parser("apply", help="apply every saved setting")

    p_power = sub.add_parser(
        "power", help="set the power mode, or list the modes this machine has")
    p_power.add_argument("mode", nargs="?",
                         help="quiet, balanced or performance")

    p_battery = sub.add_parser("battery", help="set the charge limit, or 'off'")
    p_battery.add_argument("limit", help="percent (20-100) or 'off'")

    p_fans = sub.add_parser(
        "fans", help="reset to the firmware curve, test that custom curves "
                     "take effect, or watch speeds and temperature")
    p_fans.add_argument("action", choices=["reset", "test", "watch"])

    p_colour = sub.add_parser(
        "colour", help="set keyboard zone colours, or 'list' the devices")
    p_colour.add_argument("colors", nargs="+", metavar="HEX",
                          help="one colour for all zones, or one per zone; "
                               "or the word 'list'")
    p_colour.add_argument("--mode", default=None,
                          help="effect name: " + ", ".join(
                              name for name, _role in aura_mod.MODES))
    p_colour.add_argument("--speed", type=int, default=None,
                          help="0-100, for effects that animate")
    p_colour.add_argument("--save", action="store_true",
                          help="also store them in config.json")
    sub.add_parser("gui", help="launch the GTK window")
    sub.add_parser("tray", help="run the Cinnamon tray icon")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "status": cmd_status, "apply": cmd_apply, "power": cmd_power,
        "battery": cmd_battery, "fans": cmd_fans,
        "colour": cmd_colour, "gui": cmd_gui, "tray": cmd_tray,
    }
    if args.command is None:
        return cmd_status(args)
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
