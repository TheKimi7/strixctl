"""GTK3 control window.

GTK3 rather than GTK4 because this targets Cinnamon, which is GTK3 native and
whose tray API (XApp) is GTK3 only.  Nothing here needs root: every write goes
to an attribute udev has already handed to the users group, so an unwritable
knob is a UI state to render, not an exception to catch.

Anything that runs OpenRGB goes through StrixWindow._in_background: one call
takes a couple of seconds and may take thirty, which is far too long to hold
the GTK main loop.
"""

import threading

import gi

# Both must be pinned. `from gi.repository import Gdk, Gtk` resolves Gdk first,
# and with gir1.2-gtk-4.0 also installed an unversioned Gdk loads 4.0, which
# then collides with Gtk 3.0.
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from . import apply as apply_mod
from . import aura as aura_mod
from . import config as cfg
from . import hw
from .colorwheel import ColorEditor

CURVE_COLORS = {"cpu": (0.20, 0.55, 0.95), "gpu": (0.95, 0.45, 0.15)}
TEMP_MIN, TEMP_MAX = 20, 100


class CurvePlot(Gtk.DrawingArea):
    """Both fan curves, with draggable control points.

    The spin buttons stay the source of truth -- a drag writes back into them
    and the redraw reads them again -- so there is exactly one copy of the curve
    and no way for the plot and the numbers to disagree.
    """

    PAD = 26
    GRAB_RADIUS = 10

    def __init__(self, on_point_moved=None):
        super().__init__()
        self.curves = {"cpu": [], "gpu": []}
        self.on_point_moved = on_point_moved
        self.editable = True
        self._drag = None      # (fan, index) held for the whole gesture
        self._hover = None
        self.temp_now = None   # CPU temperature, drawn as a marker
        self.set_size_request(-1, 200)

        # A DrawingArea has an empty event mask by default: without this the
        # handlers below connect successfully and simply never fire.
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK
                        | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK
                        | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.connect("draw", self.on_draw)
        self.connect("button-press-event", self.on_button_press)
        self.connect("button-release-event", self.on_button_release)
        self.connect("motion-notify-event", self.on_motion)
        self.connect("leave-notify-event", self.on_leave)

    def update(self, curves):
        self.curves = curves
        self.queue_draw()

    def set_temperature(self, temp):
        if temp != self.temp_now:
            self.temp_now = temp
            self.queue_draw()

    def set_editable(self, editable):
        self.editable = editable
        self._drag = self._hover = None
        self.queue_draw()

    # ------------------------------------------------------------- geometry

    def _plot_area(self):
        return (self.PAD, self.PAD,
                self.get_allocated_width() - 2 * self.PAD,
                self.get_allocated_height() - 2 * self.PAD)

    def _to_xy(self, temp, pwm):
        left, top, width, height = self._plot_area()
        temp = max(TEMP_MIN, min(TEMP_MAX, temp))
        x = left + width * (temp - TEMP_MIN) / (TEMP_MAX - TEMP_MIN)
        y = top + height * (1 - pwm / 100)
        return x, y

    def _from_xy(self, x, y):
        left, top, width, height = self._plot_area()
        temp = TEMP_MIN + (TEMP_MAX - TEMP_MIN) * (x - left) / max(width, 1)
        pwm = 100 * (1 - (y - top) / max(height, 1))
        return int(round(temp)), int(round(pwm))

    def _nearest(self, x, y):
        """Nearest control point within GRAB_RADIUS screen pixels, or None."""
        best, best_distance = None, self.GRAB_RADIUS ** 2
        for fan, points in self.curves.items():
            for index, (temp, pwm) in enumerate(points):
                px, py = self._to_xy(temp, pwm)
                distance = (px - x) ** 2 + (py - y) ** 2
                if distance <= best_distance:
                    best, best_distance = (fan, index), distance
        return best

    # ------------------------------------------------------------- gestures

    def on_button_press(self, _widget, event):
        if not self.editable or event.button != 1:
            return False
        # Resolve the grabbed point once and hold it: stock curves contain
        # duplicate coordinates (80C:83% twice on CPU, 35C:15% twice on GPU), so
        # re-resolving on every motion event could hop between them mid-drag.
        self._drag = self._nearest(event.x, event.y)
        return self._drag is not None

    def on_button_release(self, _widget, _event):
        self._drag = None
        return False

    def on_leave(self, _widget, _event):
        self._hover = None
        self.queue_draw()
        return False

    def on_motion(self, _widget, event):
        if not self.editable:
            return False
        if self._drag is None:
            hover = self._nearest(event.x, event.y)
            if hover != self._hover:
                self._hover = hover
                window = self.get_window()
                if window is not None:
                    window.set_cursor(Gdk.Cursor.new_from_name(
                        self.get_display(), "grab" if hover else "default"))
                self.queue_draw()
            return False

        fan, index = self._drag
        temp, pwm = self._from_xy(event.x, event.y)
        temp, pwm = self._clamp(fan, index, temp, pwm)
        if self.on_point_moved is not None:
            self.on_point_moved(fan, index, temp, pwm)
        return True

    def _clamp(self, fan, index, temp, pwm):
        """Keep the drag inside the set of curves FanCurve.validate() accepts.

        Constraining as the point moves means the user can never draw a shape
        that Apply would then reject.
        """
        points = self.curves[fan]
        low_t = points[index - 1][0] if index > 0 else 0
        high_t = points[index + 1][0] if index + 1 < len(points) else 110
        low_p = points[index - 1][1] if index > 0 else 0
        high_p = points[index + 1][1] if index + 1 < len(points) else 100

        temp = max(low_t, min(high_t, temp))
        pwm = max(low_p, min(high_p, pwm))

        # Points are non-decreasing, so the last one is the curve's ceiling and
        # the only thing standing between the machine and a curve that cannot
        # cool it. It moves sideways only: its speed stays at full.
        if index == len(points) - 1:
            pwm = hw.FanCurve.CEILING
        return temp, pwm

    # ------------------------------------------------------------- painting

    def on_draw(self, _widget, cr):
        left, top, width, height = self._plot_area()
        style = self.get_style_context()
        fg = style.get_color(Gtk.StateFlags.NORMAL)

        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.12)
        cr.set_line_width(1)
        for step in range(6):
            y = top + height * step / 5
            cr.move_to(left, y)
            cr.line_to(left + width, y)
        cr.stroke()

        # Where the CPU is right now, so the curve can be read against it.
        if self.temp_now is not None and TEMP_MIN <= self.temp_now <= TEMP_MAX:
            x, _y = self._to_xy(self.temp_now, 0)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.45)
            cr.set_dash([4, 4])
            cr.move_to(x, top)
            cr.line_to(x, top + height)
            cr.stroke()
            cr.set_dash([])
            cr.select_font_face("Sans")
            cr.set_font_size(10)
            cr.move_to(x + 4, top + 10)
            cr.show_text(f"{self.temp_now}\u00b0C now")

        for fan, points in self.curves.items():
            if not points:
                continue
            alpha = 1.0 if self.editable else 0.4
            cr.set_source_rgba(*CURVE_COLORS[fan], alpha)
            cr.set_line_width(2)
            for index, (temp, pwm) in enumerate(points):
                x, y = self._to_xy(temp, pwm)
                cr.line_to(x, y) if index else cr.move_to(x, y)
            cr.stroke()
            for index, (temp, pwm) in enumerate(points):
                x, y = self._to_xy(temp, pwm)
                active = (fan, index) in (self._drag, self._hover)
                cr.arc(x, y, 6 if active else 3.5, 0, 6.2832)
                cr.fill()

        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
        cr.select_font_face("Sans")
        cr.set_font_size(10)
        cr.move_to(left, top + height + 16)
        cr.show_text(f"{TEMP_MIN}\u00b0C")
        cr.move_to(left + width - 30, top + height + 16)
        cr.show_text(f"{TEMP_MAX}\u00b0C")
        cr.move_to(4, top + 4)
        cr.show_text("100%")
        cr.move_to(8, top + height)
        cr.show_text("0%")


class FanEditor(Gtk.Grid):
    """Eight (temperature, percent) spin-button rows for one fan.

    The last row's speed is pinned at 100%: see hw.FanCurve.CEILING.
    """

    def __init__(self, fan, on_change):
        super().__init__(column_spacing=8, row_spacing=4)
        self.fan = fan
        self.on_change = on_change
        self.rows = []

        header = Gtk.Label(xalign=0)
        header.set_markup(f"<b>{fan.upper()} fan</b>")
        self.attach(header, 0, 0, 3, 1)
        for label, column in (("°C", 1), ("%", 2)):
            heading = Gtk.Label(label=label)
            heading.get_style_context().add_class("dim-label")
            self.attach(heading, column, 1, 1, 1)

        for index in range(8):
            self.attach(Gtk.Label(label=f"{index + 1}", xalign=1), 0, index + 2, 1, 1)
            temp = Gtk.SpinButton.new_with_range(0, 110, 1)
            pwm = Gtk.SpinButton.new_with_range(0, 100, 1)
            for spin, column in ((temp, 1), (pwm, 2)):
                spin.connect("value-changed", lambda *_: self.on_change())
                self.attach(spin, column, index + 2, 1, 1)
            self.rows.append((temp, pwm))
        self.rows[-1][1].set_tooltip_text(
            "Fixed at 100% so the fans can always reach full speed.")

    def set_points(self, points):
        for (temp_spin, pwm_spin), (temp, pwm) in zip(self.rows, points):
            temp_spin.set_value(temp)
            pwm_spin.set_value(pwm)

    def get_points(self):
        return [(int(t.get_value()), int(p.get_value())) for t, p in self.rows]

    def set_sensitive_rows(self, sensitive):
        for temp_spin, pwm_spin in self.rows:
            temp_spin.set_sensitive(sensitive)
            pwm_spin.set_sensitive(sensitive)
        self.rows[-1][1].set_sensitive(False)


class ZoneSwatch(Gtk.ToggleButton):
    """One zone, shown as its own colour and used to pick what the wheel edits.

    A ToggleButton rather than a ColorButton: clicking must select the zone for
    editing, not open GTK's colour dialog -- the dialog is the palette this
    page exists to replace.
    """

    def __init__(self, zone):
        super().__init__()
        self.zone = zone
        self.colour = [255, 255, 255]
        self.set_tooltip_text(f"Edit zone {zone + 1}")
        area = Gtk.DrawingArea()
        area.set_size_request(52, 26)
        area.connect("draw", self.on_draw)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        label = Gtk.Label(label=f"Zone {zone + 1}")
        label.get_style_context().add_class("dim-label")
        box.pack_start(label, False, False, 0)
        box.pack_start(area, False, False, 0)
        self.add(box)
        self._area = area

    def set_colour(self, rgb):
        self.colour = list(rgb)
        self._area.queue_draw()

    def on_draw(self, widget, cr):
        width = widget.get_allocated_width()
        height = widget.get_allocated_height()
        r, g, b = (c / 255 for c in self.colour)
        cr.set_source_rgb(r, g, b)
        cr.rectangle(0, 0, width, height)
        cr.fill_preserve()
        cr.set_line_width(1)
        cr.set_source_rgba(0, 0, 0, 0.4)
        cr.stroke()
        return False


class StrixWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="strixctl")
        self.set_default_size(720, 620)
        self.set_border_width(0)

        self.config = cfg.load()
        self.battery = hw.Battery()
        self.platform = hw.PlatformProfile()
        self.fans = hw.Fans()
        self.kbd = hw.KeyboardBacklight()
        self.sensors = hw.Sensors()
        self._alive = True
        self._aura_ready = False
        self.connect("destroy", self._on_destroy)
        # True for the whole of construction: the page builders prime widgets,
        # and a primed widget must not write to hardware or touch self.status.
        self._loading = True

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(outer)

        self.status = Gtk.Label(xalign=0)
        self.status.get_style_context().add_class("dim-label")
        self.status.set_margin_start(12)
        self.status.set_margin_bottom(8)

        self.infobar = self._build_infobar()
        outer.pack_start(self.infobar, False, False, 0)
        outer.pack_start(self._build_profile_bar(), False, False, 0)

        notebook = Gtk.Notebook()
        notebook.set_border_width(12)
        notebook.append_page(self._build_fan_page(), Gtk.Label(label="Fans"))
        notebook.append_page(self._build_keyboard_page(), Gtk.Label(label="Keyboard"))
        notebook.append_page(self._build_battery_page(), Gtk.Label(label="Battery"))
        outer.pack_start(notebook, True, True, 0)

        outer.pack_start(self.status, False, False, 0)

        self.load_profile_into_widgets()
        if cfg.warnings:
            self.set_status(" ".join(cfg.warnings))

        self._tick()
        GLib.timeout_add_seconds(2, self._tick)
        if self.aura.available:
            self._in_background(self.aura.list_devices, self._aura_probed)
        else:
            self._aura_probed(None, None)

    def _on_destroy(self, _window):
        self._alive = False

    def _in_background(self, work, done):
        """Run `work()` off the main loop, then `done(result, error)` on it."""
        def runner():
            try:
                result, error = work(), None
            except Exception as exc:  # reported through `done`, not swallowed
                result, error = None, exc

            def finish():
                if self._alive:
                    done(result, error)
                return False
            GLib.idle_add(finish)
        threading.Thread(target=runner, daemon=True).start()

    def _set_busy(self, busy):
        for button in (self.apply_button, self.save_button, self.preview_button):
            button.set_sensitive(not busy)

    def _tick(self):
        """Refresh the live readings. Returns False once the window is gone."""
        if not self._alive:
            return False
        if self.fans.available:
            temp, rpm = self.sensors.cpu_temp(), self.sensors.rpm()
            armed = self.fans.armed
            bits = []
            if temp is not None:
                bits.append(f"CPU {temp}\u00b0C")
            for fan in ("cpu", "gpu"):
                if rpm.get(fan) is not None:
                    bits.append(f"{fan.upper()} fan {rpm[fan]} rpm")
            if all(armed.values()):
                bits.append("custom curve running")
            elif not any(armed.values()):
                bits.append("firmware curve running")
            else:
                bits.append("mixed: " + ", ".join(
                    f"{fan} {'custom' if on else 'firmware'}"
                    for fan, on in armed.items()))
            self.telemetry.set_text("     ".join(bits))
            self.plot.set_temperature(temp)
        if self.kbd.available:
            self.kbd_note.set_text(
                "Brightness follows the desktop: use the keyboard's own keys "
                "or the power applet. It is at "
                f"{self.kbd.brightness} of {self.kbd.max_brightness} now, and "
                "colour changes are sent at that level.")
        return True

    # ------------------------------------------------------------------ chrome

    def _build_infobar(self):
        bar = Gtk.InfoBar()
        bar.set_message_type(Gtk.MessageType.WARNING)
        label = Gtk.Label(xalign=0)
        label.set_line_wrap(True)
        bar.get_content_area().add(label)
        self._infobar_label = label
        bar.set_no_show_all(True)

        readonly = [name for name, backend in (
            ("battery", self.battery), ("platform profile", self.platform),
            ("fan curves", self.fans))
            if backend.available and not backend.writable]
        if readonly:
            label.set_text(
                "Read-only: " + ", ".join(readonly) + ".  Run install.sh once "
                "(it installs a udev rule) and log back in to enable control.")
            bar.show()
            bar.get_content_area().show_all()
        return bar

    def _build_profile_bar(self):
        box = Gtk.Box(spacing=8)
        box.set_border_width(12)
        box.pack_start(Gtk.Label(label="Profile"), False, False, 0)

        self.profile_combo = Gtk.ComboBoxText()
        for name in self.config["profiles"]:
            self.profile_combo.append_text(name)
        names = list(self.config["profiles"])
        active = self.config.get("active_profile")
        self.profile_combo.set_active(names.index(active) if active in names else 0)
        self.profile_combo.connect("changed", self.on_profile_changed)
        box.pack_start(self.profile_combo, False, False, 0)

        save = self.save_button = Gtk.Button(label="Save")
        save.connect("clicked", self.on_save)
        apply_button = self.apply_button = Gtk.Button(label="Apply")
        apply_button.get_style_context().add_class("suggested-action")
        apply_button.connect("clicked", self.on_apply)
        box.pack_end(apply_button, False, False, 0)
        box.pack_end(save, False, False, 0)
        return box

    # ------------------------------------------------------------------- pages

    def _build_fan_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_border_width(12)

        if not self.fans.available:
            page.pack_start(self._unavailable(
                "No asus_custom_fan_curve hwmon node on this machine."),
                False, False, 0)
            return page

        if self.platform.available:
            row = Gtk.Box(spacing=8)
            row.pack_start(Gtk.Label(label="Platform profile"), False, False, 0)
            self.platform_combo = Gtk.ComboBoxText()
            for choice in self.platform.choices:
                self.platform_combo.append_text(choice)
            self.platform_combo.set_sensitive(self.platform.writable)
            row.pack_start(self.platform_combo, False, False, 0)
            note = Gtk.Label(xalign=0)
            note.get_style_context().add_class("dim-label")
            note.set_text("takes effect on Apply, with the rest of the profile")
            row.pack_start(note, False, False, 0)
            page.pack_start(row, False, False, 0)

        self.fan_mode = Gtk.CheckButton(label="Use a custom curve")
        self.fan_mode.connect("toggled", self.on_fan_mode_toggled)
        page.pack_start(self.fan_mode, False, False, 0)

        self.telemetry = Gtk.Label(xalign=0)
        page.pack_start(self.telemetry, False, False, 0)

        self.plot = CurvePlot(on_point_moved=self.on_point_dragged)
        page.pack_start(self.plot, False, False, 0)

        hint = Gtk.Label(xalign=0)
        hint.get_style_context().add_class("dim-label")
        hint.set_text("Drag a point to reshape the curve. Speeds are percent of "
                      "full. The last point is fixed at 100%, so the fans can "
                      "always reach full speed; it only moves sideways.")
        hint.set_line_wrap(True)
        page.pack_start(hint, False, False, 0)

        editors = Gtk.Box(spacing=24, homogeneous=True)
        self.fan_editors = {}
        for fan in ("cpu", "gpu"):
            editor = FanEditor(fan, self.on_curve_edited)
            self.fan_editors[fan] = editor
            editors.pack_start(editor, True, True, 0)
        page.pack_start(editors, False, False, 0)

        buttons = Gtk.Box(spacing=8)
        stock = Gtk.Button(label="Load firmware curve")
        stock.connect("clicked", self.on_load_stock)
        reset = Gtk.Button(label="Hand back to firmware")
        reset.connect("clicked", self.on_fan_reset)
        buttons.pack_start(stock, False, False, 0)
        buttons.pack_start(reset, False, False, 0)
        page.pack_start(buttons, False, False, 0)
        return page

    def _build_keyboard_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_border_width(12)

        # No brightness control here, on purpose: the desktop already has one
        # and two controls for one knob only ever disagree.
        self.kbd_note = Gtk.Label(xalign=0)
        self.kbd_note.set_line_wrap(True)
        self.kbd_note.get_style_context().add_class("dim-label")
        if self.kbd.available:
            page.pack_start(self.kbd_note, False, False, 0)
            page.pack_start(Gtk.Separator(), False, False, 6)

        self.aura = aura_mod.Aura(settings=self.config.get("openrgb"))
        header = Gtk.Label(xalign=0)
        header.set_markup("<b>Colour</b>")
        page.pack_start(header, False, False, 0)

        effect_row = Gtk.Box(spacing=8)
        effect_row.pack_start(Gtk.Label(label="Effect"), False, False, 0)
        self.effect_combo = Gtk.ComboBoxText()
        # The full menu for now; _aura_probed trims it to what the hardware
        # reports once OpenRGB has answered.
        self._modes = ([name for name, _role in aura_mod.MODES]
                       if self.aura.available else ["Static"])
        for mode in self._modes:
            self.effect_combo.append_text(mode)
        self.effect_combo.connect("changed", self.on_effect_changed)
        effect_row.pack_start(self.effect_combo, False, False, 0)
        page.pack_start(effect_row, False, False, 0)

        self.speed_row = Gtk.Box(spacing=8)
        self.speed_row.pack_start(Gtk.Label(label="Speed"), False, False, 0)
        self.speed_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0, 100, 5)
        self.speed_scale.set_digits(0)
        for mark, name in ((0, "slow"), (50, ""), (100, "fast")):
            self.speed_scale.add_mark(mark, Gtk.PositionType.BOTTOM, name)
        self.speed_scale.connect("value-changed", self.on_speed_changed)
        self.speed_row.pack_start(self.speed_scale, True, True, 0)
        page.pack_start(self.speed_row, False, False, 0)

        self.zone_colours = [[255, 255, 255] for _ in range(cfg.ZONES)]
        self.active_zone = 0
        zones = Gtk.Box(spacing=8)
        self.zone_swatches = []
        for zone in range(cfg.ZONES):
            swatch = ZoneSwatch(zone)
            swatch.connect("toggled", self.on_zone_selected, zone)
            zones.pack_start(swatch, False, False, 0)
            self.zone_swatches.append(swatch)
        self.zone_swatches[0].set_active(True)
        page.pack_start(zones, False, False, 0)

        self.colour_editor = ColorEditor()
        self.colour_editor.connect("color-changed", self.on_editor_colour)
        page.pack_start(self.colour_editor, False, False, 0)

        self.zone_hint = Gtk.Label(xalign=0)
        self.zone_hint.get_style_context().add_class("dim-label")
        self.zone_hint.set_line_wrap(True)
        page.pack_start(self.zone_hint, False, False, 0)

        actions = Gtk.Box(spacing=8)
        all_zones = Gtk.Button(label="Set all zones to zone 1")
        all_zones.connect("clicked", self.on_match_zones)
        preview = self.preview_button = Gtk.Button(label="Preview on keyboard")
        preview.connect("clicked", self.on_preview_colour)
        actions.pack_start(all_zones, False, False, 0)
        actions.pack_start(preview, False, False, 0)
        page.pack_start(actions, False, False, 0)

        note = self.aura_note = Gtk.Label(xalign=0)
        note.set_line_wrap(True)
        note.get_style_context().add_class("dim-label")
        note.set_text("Asking OpenRGB what it can see\u2026")
        page.pack_start(note, False, False, 0)
        return page

    def _aura_probed(self, _devices, error):
        """OpenRGB has listed its devices (or is absent): finish the page."""
        self._aura_ready = True
        if not self.aura.available:
            self.aura_note.set_text(
                "Keyboard colour needs OpenRGB, which was not found.\n\n"
                "This chassis exposes no colour attribute in sysfs, so colour "
                "means HID writes to the N-KEY device, and strixctl hands that job "
                "to OpenRGB rather than reimplementing it.\n\n"
                + self.aura.why_unavailable())
        elif error is not None:
            self.aura_note.set_text(f"OpenRGB failed: {error}")
        else:
            device = self.aura.find_keyboard()
            if device is not None:
                self.aura_note.set_text(
                    f"Driven through OpenRGB ({self.aura.binary}): "
                    f"{device.name}, {device.leds} zones on {device.location}. "
                    "Each change spawns OpenRGB, so it takes a couple of seconds.")
            else:
                self.aura_note.set_text(self.aura.why_unavailable())

        modes = (self.aura.modes if self.aura.available else None) or ["Static"]
        if modes != self._modes:
            wanted = self.effect_combo.get_active_text()
            previous, self._loading = self._loading, True
            try:
                self._modes = modes
                self.effect_combo.remove_all()
                for mode in modes:
                    self.effect_combo.append_text(mode)
                self.effect_combo.set_active(
                    modes.index(wanted) if wanted in modes else 0)
            finally:
                self._loading = previous
        self.sync_effect_sensitivity()

    def _build_battery_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_border_width(12)

        if not self.battery.available:
            page.pack_start(self._unavailable(
                "No charge_control_end_threshold on this battery."),
                False, False, 0)
            return page

        self.battery_switch = Gtk.CheckButton(label="Limit charging")
        self.battery_switch.set_active(self.config["battery"].get("enabled", False))
        self.battery_switch.connect("toggled", self.on_battery_toggled)
        page.pack_start(self.battery_switch, False, False, 0)

        self.battery_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 20, 100, 5)
        self.battery_scale.set_digits(0)
        for mark in (20, 60, 80, 100):
            self.battery_scale.add_mark(mark, Gtk.PositionType.BOTTOM, f"{mark}%")
        self.battery_scale.set_value(self.config["battery"].get("limit", 80))
        self.battery_scale.connect("value-changed", self.on_battery_limit)
        page.pack_start(self.battery_scale, False, False, 0)

        note = Gtk.Label(xalign=0)
        note.set_line_wrap(True)
        note.get_style_context().add_class("dim-label")
        note.set_text(
            "ASUS exposes only a stop threshold, so there is no separate "
            "recharge floor: charging simply halts at this level.")
        page.pack_start(note, False, False, 0)

        self.battery_switch.toggled()
        return page

    @staticmethod
    def _unavailable(text):
        label = Gtk.Label(xalign=0)
        label.set_line_wrap(True)
        label.get_style_context().add_class("dim-label")
        label.set_text(text)
        return label

    # -------------------------------------------------------------- behaviour

    def _profile(self):
        return cfg.profile(self.config, self.profile_combo.get_active_text()) or {}

    def load_profile_into_widgets(self):
        self._loading = True
        prof = self._profile()
        if self.platform.available:
            choices = self.platform.choices
            wanted = prof.get("platform_profile") or self.platform.profile
            if wanted in choices:
                self.platform_combo.set_active(choices.index(wanted))
        if self.fans.available:
            self.fan_mode.set_active(prof.get("fan_mode") == "curve")
            for fan, editor in self.fan_editors.items():
                editor.set_points(cfg.points(prof.get(f"fan_{fan}", [])))
            self.on_fan_mode_toggled(self.fan_mode)
        settings = prof.get("aura", {})
        self.zone_colours = [list(colour) for colour
                             in aura_mod.normalise_colors(settings, cfg.ZONES)]
        self._refresh_zone_swatches()
        self.select_zone(self.active_zone)
        modes = self._modes
        wanted = settings.get("mode", "Static")
        self.effect_combo.set_active(modes.index(wanted) if wanted in modes else 0)
        self.speed_scale.set_value(settings.get("speed", 50))
        self.sync_effect_sensitivity()
        self._loading = False
        self.refresh_plot()

    def refresh_plot(self):
        if self.fans.available:
            self.plot.update({fan: editor.get_points()
                              for fan, editor in self.fan_editors.items()})

    def on_profile_changed(self, _combo):
        self.config["active_profile"] = self.profile_combo.get_active_text()
        self.load_profile_into_widgets()

    def on_point_dragged(self, fan, index, temp, pwm):
        """Write a drag back into the spin buttons, which drive the redraw."""
        temp_spin, pwm_spin = self.fan_editors[fan].rows[index]
        temp_spin.set_value(temp)
        pwm_spin.set_value(pwm)

    def on_curve_edited(self):
        if not self._loading:
            self.refresh_plot()

    def on_fan_mode_toggled(self, button):
        custom = button.get_active()
        for editor in self.fan_editors.values():
            editor.set_sensitive_rows(custom and self.fans.writable)
        self.plot.set_editable(custom and self.fans.writable)

    def on_load_stock(self, _button):
        """Seed the editors with the firmware's curve for the profile in force."""
        try:
            curves = self.fans.factory_curves()
        except (OSError, ValueError) as exc:
            self.set_status(f"Could not read the firmware curve: {exc}")
            return
        raised = False
        for fan, editor in self.fan_editors.items():
            fixed = hw.with_full_ceiling(curves[fan])
            raised = raised or fixed != curves[fan]
            editor.set_points(fixed)
        self.refresh_plot()
        running = self.platform.profile if self.platform.available else "current"
        text = f"Loaded the firmware's {running} curve."
        if raised:
            text += (" It stops short of full speed, so the last point was "
                     "raised to 100%.")
        chosen = (self.platform_combo.get_active_text()
                  if self.platform.available else None)
        if chosen and chosen != running:
            text += (f" This profile is set to {chosen}, which is not what "
                     "the machine is running yet.")
        self.set_status(text)

    def on_fan_reset(self, _button):
        try:
            self.fans.reset()
            self.set_status("Both fans handed back to the firmware curve.")
        except OSError as exc:
            self.set_status(f"Could not reset fans: {exc}")

    def _zone_colours(self):
        return [list(colour) for colour in self.zone_colours]

    def _refresh_zone_swatches(self):
        for swatch, colour in zip(self.zone_swatches, self.zone_colours):
            swatch.set_colour(colour)

    def sync_effect_sensitivity(self):
        """Grey out what the chosen effect ignores, the way Aura Core does."""
        mode = self.effect_combo.get_active_text() or "Static"
        # Until the probe has answered, asking would block on OpenRGB.
        usable = self.aura.available and (
            not self._aura_ready or self.aura.device_for(mode) is not None)
        takes_colour = aura_mod.mode_takes_color(mode)
        # Effects served by the per-key entry take one colour for the whole
        # board, so the zones past the first have nothing to say.
        single = aura_mod.MODE_ROLES.get(mode.strip().lower()) == aura_mod.PERKEY

        self.colour_editor.set_sensitive(usable and takes_colour)
        for index, swatch in enumerate(self.zone_swatches):
            swatch.set_sensitive(usable and takes_colour
                                 and (index == 0 or not single))
        self.speed_row.set_sensitive(usable and aura_mod.mode_takes_speed(mode))

        if single and self.active_zone != 0:
            self.select_zone(0)
        if not usable:
            self.zone_hint.set_text("")
        elif not takes_colour:
            self.zone_hint.set_text(
                f"{mode} generates its own colours, so the wheel is idle.")
        elif single:
            self.zone_hint.set_text(
                f"{mode} runs on the per-key entry, which takes one colour for "
                "the whole keyboard, so zone 1's colour is used.")
        else:
            self.zone_hint.set_text(
                "Pick a zone, then set its colour with the wheel, the R/G/B "
                "boxes or the hex field.")

    def select_zone(self, zone):
        """Make `zone` the one the wheel edits, without echoing back a change."""
        self.active_zone = zone
        previous, self._loading = self._loading, True
        try:
            for index, swatch in enumerate(self.zone_swatches):
                swatch.set_active(index == zone)
            self.colour_editor.set_rgb(self.zone_colours[zone])
        finally:
            self._loading = previous

    def on_zone_selected(self, button, zone):
        if self._loading:
            return
        if not button.get_active():
            # These are plain ToggleButtons with no group, so the radio
            # behaviour is ours to enforce: clicking the lit swatch would
            # otherwise leave nothing selected while the wheel carried on
            # editing that zone.
            if zone == self.active_zone:
                previous, self._loading = self._loading, True
                try:
                    button.set_active(True)
                finally:
                    self._loading = previous
            return
        self.select_zone(zone)

    def on_editor_colour(self, editor):
        if self._loading:
            return
        self.zone_colours[self.active_zone] = list(editor.rgb)
        self.zone_swatches[self.active_zone].set_colour(editor.rgb)
        self._profile().setdefault("aura", {})["colors"] = self._zone_colours()

    def on_effect_changed(self, combo):
        self.sync_effect_sensitivity()
        if self._loading:
            return
        self._profile().setdefault("aura", {})["mode"] = combo.get_active_text()

    def on_speed_changed(self, scale):
        if self._loading:
            return
        self._profile().setdefault("aura", {})["speed"] = int(scale.get_value())

    def on_match_zones(self, _button):
        first = list(self.zone_colours[0])
        self.zone_colours = [list(first) for _ in self.zone_colours]
        self._refresh_zone_swatches()
        self.select_zone(self.active_zone)
        self._profile().setdefault("aura", {})["colors"] = self._zone_colours()

    def on_preview_colour(self, _button):
        """Push the current swatches to the keyboard without saving them."""
        colours = self._zone_colours()
        mode = self.effect_combo.get_active_text()
        speed = int(self.speed_scale.get_value())

        def done(_device, error):
            self._set_busy(False)
            if error is not None:
                self.set_status(f"Could not set colour: {error}")
            else:
                self.set_status(f"Sent {mode} to the keyboard.")

        self._set_busy(True)
        self.set_status(f"Sending {mode} to the keyboard\u2026")
        self._in_background(
            lambda: self.aura.set_colors(colours, mode=mode, speed=speed), done)

    def on_battery_toggled(self, button):
        self.battery_scale.set_sensitive(button.get_active() and self.battery.writable)
        if self._loading:
            return
        self.config["battery"]["enabled"] = button.get_active()
        if not button.get_active() and self.battery.writable:
            try:
                self.battery.limit = 100
                self.set_status("Charge limit off (100%).")
            except OSError as exc:
                self.set_status(f"Could not clear the limit: {exc}")

    def on_battery_limit(self, scale):
        value = int(scale.get_value())
        self.config["battery"]["limit"] = value
        if self.battery.writable and self.battery_switch.get_active():
            try:
                self.battery.limit = value
                self.set_status(f"Charge limit {value}%.")
            except (OSError, ValueError) as exc:
                self.set_status(f"Could not set the limit: {exc}")

    def collect(self):
        """Fold the widget state back into the in-memory config."""
        prof = self._profile()
        if self.platform.available:
            prof["platform_profile"] = self.platform_combo.get_active_text()
        if self.fans.available:
            prof["fan_mode"] = "curve" if self.fan_mode.get_active() else "auto"
            for fan, editor in self.fan_editors.items():
                prof[f"fan_{fan}"] = [list(point) for point in editor.get_points()]
        aura_settings = prof.setdefault("aura", {})
        aura_settings["colors"] = self._zone_colours()
        aura_settings["mode"] = self.effect_combo.get_active_text() or "Static"
        aura_settings["speed"] = int(self.speed_scale.get_value())
        return self.config

    def on_save(self, _button):
        cfg.save(self.collect())
        self.set_status(f"Saved to {cfg.CONFIG_PATH}.")

    def on_apply(self, _button):
        cfg.save(self.collect())
        name = self.profile_combo.get_active_text()

        def done(results, error):
            self._set_busy(False)
            if error is not None:
                self.set_status(f"Apply failed: {error}")
                return
            failures = [f"{knob}: {detail}"
                        for knob, state, detail in results if state == "failed"]
            if failures:
                self.set_status("Applied with problems: " + "; ".join(failures))
            else:
                applied = [knob for knob, state, _ in results if state == "ok"]
                self.set_status("Applied: " + ", ".join(applied) if applied
                                else "Nothing to apply.")
            self._tick()

        self._set_busy(True)
        self.set_status(f"Applying {name}\u2026")
        self._in_background(lambda: apply_mod.apply_all(name), done)

    def set_status(self, text):
        self.status.set_text(text)


def main():
    window = StrixWindow()
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    window.infobar.set_visible(bool(window._infobar_label.get_text()))
    Gtk.main()
    return 0
