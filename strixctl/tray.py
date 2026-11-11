"""Cinnamon tray icon.

Uses XApp.StatusIcon rather than AppIndicator because Cinnamon (and XFCE, and
MATE) speak XApp natively, and gir1.2-xapp-1.0 is already a dependency of a
Cinnamon desktop -- so this adds nothing to install.

The tray is also the one long-lived strixctl process, so it does the watching
that a daemon would otherwise be needed for:

  * The kernel drops a custom fan curve whenever the platform profile changes,
    including when the firmware's own hotkey changes it. The tray notices,
    switches to the strixctl profile that goes with the new platform profile,
    and applies it.
  * A resume from sleep can leave the firmware curve running. The tray notices
    the wake-up and puts the profile's curve back.
  * The window and the CLI edit the same config file, so the menu is rebuilt
    when that file changes.

None of it happens when the tray is not running; curves then stay as the last
`strixctl apply` left them, as before.
"""

import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("XApp", "1.0")
from gi.repository import GLib, Gtk, XApp

from . import apply as apply_mod
from . import config as cfg
from . import hw

POLL_SECONDS = 2
#: A gap between the two clocks growing by more than this means the machine
#: slept: CLOCK_BOOTTIME counts time asleep and CLOCK_MONOTONIC does not.
RESUME_JUMP = 5.0


def _slept_for():
    try:
        return (time.clock_gettime(time.CLOCK_BOOTTIME)
                - time.clock_gettime(time.CLOCK_MONOTONIC))
    except (AttributeError, OSError):
        return 0.0


class Tray:
    def __init__(self):
        self.icon = XApp.StatusIcon()
        self.icon.set_icon_name("computer")
        self.platform = hw.PlatformProfile()
        self._busy = False
        self._note = ""
        self._seen_config = cfg.mtime()
        self._seen_platform = self.platform.profile
        self._pending_platform = None
        self._seen_sleep = _slept_for()
        self.refresh()
        GLib.timeout_add_seconds(POLL_SECONDS, self.watch)

    def refresh(self):
        conf = cfg.load()
        battery = hw.Battery()
        active = conf.get("active_profile")

        limit = battery.limit if battery.available else None
        tooltip = f"strixctl: {active}"
        if limit is not None:
            tooltip += f", charge stop {limit}%"
        if self._note:
            tooltip += f"\n{self._note}"
        self.icon.set_tooltip_text(tooltip)

        menu = Gtk.Menu()
        for name in conf["profiles"]:
            item = Gtk.CheckMenuItem(label=name)
            item.set_draw_as_radio(True)
            item.set_active(name == active)
            item.set_sensitive(not self._busy)
            item.connect("activate", self.on_profile, name)
            menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())

        if self._note:
            note = Gtk.MenuItem(label=self._note)
            note.set_sensitive(False)
            menu.append(note)

        reset = Gtk.MenuItem(label="Fans: hand back to firmware")
        reset.connect("activate", self.on_fan_reset)
        menu.append(reset)

        window = Gtk.MenuItem(label="Open strixctl…")
        window.connect("activate", self.on_open)
        menu.append(window)

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda *_: Gtk.main_quit())
        menu.append(quit_item)

        menu.show_all()
        self.icon.set_secondary_menu(menu)
        self.icon.set_primary_menu(menu)
        self._seen_config = cfg.mtime()

    # ------------------------------------------------------------- applying

    def _run(self, work, label):
        """Run an apply off the main loop; OpenRGB alone takes seconds."""
        if self._busy:
            return
        self._busy = True

        def runner():
            try:
                results = work()
            except Exception as exc:
                results = [(label, "failed", str(exc))]
            GLib.idle_add(self._finished, results)

        threading.Thread(target=runner, daemon=True).start()

    def _finished(self, results):
        self._busy = False
        failures = [f"{knob}: {detail}"
                    for knob, state, detail in results if state == "failed"]
        self._note = ("Last apply failed: " + "; ".join(failures)) if failures else ""
        # Our own writes are not news to the watcher.
        self._seen_platform = self.platform.profile
        self._pending_platform = None
        self.refresh()
        return False

    def switch_to(self, name):
        conf = cfg.load()
        if name not in conf["profiles"]:
            return
        conf["active_profile"] = name
        cfg.save(conf)
        self.refresh()
        self._run(lambda: apply_mod.apply_all(name), "profile")

    def on_profile(self, item, name):
        if not item.get_active() or self._busy:
            return
        self.switch_to(name)

    def on_fan_reset(self, _item):
        fans = hw.Fans()
        if fans.available and fans.writable:
            fans.reset()

    def on_open(self, _item):
        from .gui import StrixWindow
        window = StrixWindow()
        window.show_all()
        window.infobar.set_visible(bool(window._infobar_label.get_text()))

    # ------------------------------------------------------------- watching

    def watch(self):
        if self._busy:
            return True

        if cfg.mtime() != self._seen_config:
            self.refresh()

        slept = _slept_for()
        woke = slept - self._seen_sleep > RESUME_JUMP
        self._seen_sleep = slept

        now = self.platform.profile if self.platform.available else None
        if now != self._seen_platform:
            # Act on the second sighting, not the first: `strixctl apply` from
            # another process changes the platform profile a moment before it
            # arms its curve, and jumping in between would only race it.
            if self._pending_platform != now:
                self._pending_platform = now
                return True
            self._pending_platform = None
            self._seen_platform = now
            self.on_platform_changed(now)
        elif woke:
            self._run(apply_mod.rearm_fans, "fans")
        return True

    def on_platform_changed(self, platform):
        """The platform profile moved without the tray moving it."""
        conf = cfg.load()
        active = cfg.profile(conf) or {}
        if active.get("platform_profile") == platform:
            # The window or the CLI applied the active profile. Only step in
            # if that left a curve the profile wants disarmed.
            fans = hw.Fans()
            if (active.get("fan_mode") == "curve" and fans.available
                    and not all(fans.armed.values())):
                self._run(apply_mod.rearm_fans, "fans")
            return
        name = cfg.profile_for_platform(conf, platform)
        if name is not None:
            self.switch_to(name)


def main():
    Tray()
    Gtk.main()
    return 0


if __name__ == "__main__":
    main()
