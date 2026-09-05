"""Cinnamon tray icon.

Uses XApp.StatusIcon rather than AppIndicator because Cinnamon (and XFCE, and
MATE) speak XApp natively, and gir1.2-xapp-1.0 is already a dependency of a
Cinnamon desktop -- so this adds nothing to install.
"""

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("XApp", "1.0")
from gi.repository import Gtk, XApp

from . import apply as apply_mod
from . import config as cfg
from . import hw


class Tray:
    def __init__(self):
        self.icon = XApp.StatusIcon()
        self.icon.set_icon_name("computer")
        self.refresh()

    def refresh(self):
        conf = cfg.load()
        battery = hw.Battery()
        active = conf.get("active_profile")

        limit = battery.limit if battery.available else None
        tooltip = f"strixctl: {active}"
        if limit is not None:
            tooltip += f", charge stop {limit}%"
        self.icon.set_tooltip_text(tooltip)

        menu = Gtk.Menu()
        for name in conf["profiles"]:
            item = Gtk.CheckMenuItem(label=name)
            item.set_draw_as_radio(True)
            item.set_active(name == active)
            item.connect("activate", self.on_profile, name)
            menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())

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

    def on_profile(self, item, name):
        if not item.get_active():
            return
        conf = cfg.load()
        conf["active_profile"] = name
        cfg.save(conf)
        apply_mod.apply_all(name)
        self.refresh()

    def on_fan_reset(self, _item):
        fans = hw.Fans()
        if fans.available and fans.writable:
            fans.reset()

    def on_open(self, _item):
        from .gui import StrixWindow
        window = StrixWindow()
        window.show_all()
        window.infobar.set_visible(bool(window._infobar_label.get_text()))


def main():
    Tray()
    Gtk.main()
    return 0


if __name__ == "__main__":
    main()
