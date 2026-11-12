"""Cinnamon tray icon.

Uses XApp.StatusIcon rather than AppIndicator because Cinnamon (and XFCE, and
MATE) speak XApp natively, and gir1.2-xapp-1.0 is already a dependency of a
Cinnamon desktop -- so this adds nothing to install.

The menu carries the power mode, because that is the one knob worth changing
without opening a window. Everything else lives in the window.

The tray is also the one long-lived strixctl process, so it does the watching
that a daemon would otherwise be needed for:

  * A resume from sleep can leave the firmware curve running, so the tray
    notices the wake-up and puts the stored fan setting back.
  * The window and the CLI edit the same config file, so the menu is rebuilt
    when that file changes.

It changes nothing on its own beyond that; only a click does.

None of it happens when the tray is not running; the fans then stay as the
last `strixctl apply` left them, as before.
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
from . import theme

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
        # The menu is the first thing drawn, and the window may open later in
        # this same process, so the skin has to be in place before either.
        theme.apply(cfg.load())
        self.icon = XApp.StatusIcon()
        self.icon.set_icon_name("computer")
        self.platform = hw.PlatformProfile()
        self._busy = False
        self._note = ""
        self._seen_config = cfg.mtime()
        self._seen_sleep = _slept_for()
        self.refresh()
        GLib.timeout_add_seconds(POLL_SECONDS, self.watch)

    def refresh(self):
        conf = cfg.load()
        battery = hw.Battery()
        fans = hw.Fans()

        limit = battery.limit if battery.available else None
        tooltip = "strixctl"
        if self.platform.available:
            tooltip += f": {self.platform.profile}"
        if limit is not None:
            tooltip += f", charge stop {limit}%"
        if fans.available:
            tooltip += (", your fan curve" if all(fans.armed.values())
                        else ", firmware fan curve")
        if self._note:
            tooltip += f"\n{self._note}"
        self.icon.set_tooltip_text(tooltip)

        menu = Gtk.Menu()
        if self.platform.available:
            heading = Gtk.MenuItem(label="Power mode")
            heading.set_sensitive(False)
            menu.append(heading)
            active = self.platform.profile
            for name in self.platform.choices:
                item = Gtk.CheckMenuItem(label=f"   {name}")
                item.set_draw_as_radio(True)
                item.set_active(name == active)
                item.set_sensitive(not self._busy and self.platform.writable)
                item.connect("activate", self.on_power, name)
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
        self.refresh()
        return False

    def on_power(self, item, name):
        """Set the power mode, then put the stored fan setting back.

        The firmware reloads its own curve when the mode changes, so a custom
        curve has to be re-armed or it silently stops applying.
        """
        if not item.get_active() or self._busy or name == self.platform.profile:
            return
        conf = cfg.load()
        try:
            self.platform.profile = name
        except (OSError, ValueError) as exc:
            self._note = f"Could not set the power mode: {exc}"
            self.refresh()
            return
        conf["power"] = name
        cfg.save(conf)
        self._seen_config = cfg.mtime()
        self._finished(apply_mod.rearm_fans(conf))

    def on_fan_reset(self, _item):
        fans = hw.Fans()
        if fans.available and fans.writable:
            fans.reset()
            self.refresh()

    def on_open(self, _item):
        from .gui import StrixWindow
        StrixWindow().present_all()

    # ------------------------------------------------------------- watching

    def watch(self):
        if self._busy:
            return True

        if cfg.mtime() != self._seen_config:
            self.refresh()

        slept = _slept_for()
        woke = slept - self._seen_sleep > RESUME_JUMP
        self._seen_sleep = slept
        if woke:
            self._run(apply_mod.rearm_fans, "fans")
        return True


def main():
    Tray()
    Gtk.main()
    return 0


if __name__ == "__main__":
    main()
