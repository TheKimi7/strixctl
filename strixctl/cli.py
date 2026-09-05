"""Command line front end.  Also the entry point the systemd user unit calls."""

import argparse
import sys

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


def cmd_status(args):
    conf = cfg.load()
    battery, platform = hw.Battery(), hw.PlatformProfile()
    fans, kbd = hw.Fans(), hw.KeyboardBacklight()
    aura = aura_mod.Aura(settings=conf.get("openrgb"))

    print(f"strixctl -- {hw.board_name()}")
    print(f"  active profile       {conf.get('active_profile')}")
    print()
    print(_state_line("battery limit",
                      battery.available, battery.writable,
                      f"{battery.limit}%" if battery.limit else "?"))
    print(_state_line("platform profile",
                      platform.available, platform.writable, platform.profile))
    print(_state_line("fan curves",
                      fans.available, fans.writable,
                      "custom" if fans.available and fans.cpu.armed else "firmware"))
    print(_state_line("kbd brightness",
                      kbd.available, kbd.writable,
                      f"{kbd.brightness}/{kbd.max_brightness}" if kbd.available else "-"))
    keyboard = aura.find_keyboard() if aura.available else None
    print(_state_line("kbd colour", aura.available, keyboard is not None,
                      f"{keyboard.leds} zones" if keyboard else "no device"))
    if aura.available and keyboard is None:
        print(f"    {aura.why_unavailable()}")
    if fans.available:
        print()
        for name, curve in fans.curves().items():
            pretty = "  ".join(f"{t}C:{p}" for t, p in curve)
            print(f"  fan {name}: {pretty}")
    return 0


def cmd_apply(args):
    results = apply_mod.apply_all(args.profile)
    failed = 0
    for knob, state, detail in results:
        marker = {"ok": "+", "skipped": ".", "unavailable": ".", "failed": "!"}[state]
        if state == "failed":
            failed += 1
        print(f"{marker} {knob:<18} {detail}")
    return 1 if failed else 0


def cmd_profile(args):
    conf = cfg.load()
    if not args.name:
        for name in conf["profiles"]:
            marker = "*" if name == conf.get("active_profile") else " "
            print(f"{marker} {name}")
        return 0
    if args.name not in conf["profiles"]:
        print(f"no profile named {args.name!r}", file=sys.stderr)
        return 1
    conf["active_profile"] = args.name
    cfg.save(conf)
    return cmd_apply(argparse.Namespace(profile=args.name))


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


def cmd_fans(args):
    fans = hw.Fans()
    if not fans.available:
        print("no asus_custom_fan_curve hwmon node", file=sys.stderr)
        return 1
    if not fans.writable:
        print("fan curve attributes are read-only -- run install.sh", file=sys.stderr)
        return 1
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
        print("\nz = zoned entry, k = per-key entry — both are the same "
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

    prof_aura = (cfg.profile(conf) or {}).get("aura", {})
    mode = args.mode or prof_aura.get("mode", "Static")
    speed = args.speed if args.speed is not None else prof_aura.get("speed")
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
        prof = cfg.profile(conf)
        stored = prof.setdefault("aura", {})
        stored["colors"] = colors
        stored["mode"] = mode
        if speed is not None:
            stored["speed"] = speed
        cfg.save(conf)
        print(f"saved to profile {conf['active_profile']}")
    return 0


def cmd_gui(args):
    from .gui import main as gui_main
    return gui_main()


def cmd_tray(args):
    from .tray import main as tray_main
    return tray_main()


def build_parser():
    parser = argparse.ArgumentParser(
        prog="strixctl", description="ASUS ROG laptop controls for Linux")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="show every knob and whether it is writable")

    p_apply = sub.add_parser("apply", help="apply the saved profile")
    p_apply.add_argument("profile", nargs="?", help="profile name (default: active)")

    p_profile = sub.add_parser("profile", help="list or switch profiles")
    p_profile.add_argument("name", nargs="?")

    p_battery = sub.add_parser("battery", help="set the charge limit, or 'off'")
    p_battery.add_argument("limit", help="percent (20-100) or 'off'")

    p_fans = sub.add_parser("fans", help="fan controls")
    p_fans.add_argument("action", choices=["reset"])

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
                          help="also store them in the active profile")
    sub.add_parser("gui", help="launch the GTK window")
    sub.add_parser("tray", help="run the Cinnamon tray icon")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "status": cmd_status, "apply": cmd_apply, "profile": cmd_profile,
        "battery": cmd_battery, "fans": cmd_fans,
        "colour": cmd_colour, "gui": cmd_gui, "tray": cmd_tray,
    }
    if args.command is None:
        return cmd_status(args)
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
