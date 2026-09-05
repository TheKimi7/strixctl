"""Aura Sync-style colour editor: a hue ring with a saturation/value square.

Why a hand-drawn widget
-----------------------
GTK3 ships two colour pickers and neither matches what Aura Sync shows.
`GtkColorButton` opens a dialog whose front page is a fixed palette, and the
editor behind it is a saturation/value plane with a *linear* hue slider.
`GtkHSV` is closer -- it is a real hue ring -- but it inscribes a rotating
triangle rather than an upright square, so the pointer moves differently for
the same colour.

Aura Sync uses a ring with an upright square inside it. Matching that is a
DrawingArea and some Cairo, which is the same shape as the CurvePlot widget in
gui.py: draw in `on_draw`, hit-test in the button and motion handlers.

The square is inscribed in the inner circle, so its side is
inner_radius * sqrt(2), with saturation on x and value on y.
"""

import colorsys
import math

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GObject, Gtk

from .aura import hex_to_rgb, rgb_to_hex

#: Fraction of the radius given over to the hue ring.
RING_WIDTH = 0.26
#: Segments used to fake a conical gradient. 360 is one per degree, which is
#: smooth at any size this widget is ever drawn at.
RING_SEGMENTS = 360
#: Nothing smaller than this is comfortable to aim at with a mouse.
MIN_SIZE = 200


class ColorWheel(Gtk.DrawingArea):
    """Hue ring plus saturation/value square. Emits `color-changed`."""

    __gsignals__ = {
        "color-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__()
        self.set_size_request(MIN_SIZE, MIN_SIZE)
        # HSV is the widget's own state rather than RGB, because RGB cannot
        # represent "which hue am I on" once value reaches zero: black would
        # reset the ring marker to red every time someone dragged to the
        # bottom of the square.
        self._hsv = [0.0, 1.0, 1.0]
        self._drag = None
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK
                        | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK)
        self.connect("draw", self.on_draw)
        self.connect("button-press-event", self.on_button_press)
        self.connect("button-release-event", self.on_button_release)
        self.connect("motion-notify-event", self.on_motion)

    # ------------------------------------------------------------- colour

    @property
    def rgb(self):
        r, g, b = colorsys.hsv_to_rgb(*self._hsv)
        return [int(round(r * 255)), int(round(g * 255)), int(round(b * 255))]

    def set_rgb(self, rgb, notify=False):
        """Set the colour. Silent by default so callers can sync without loops."""
        r, g, b = (max(0, min(255, int(c))) / 255 for c in rgb)
        hue, sat, val = colorsys.rgb_to_hsv(r, g, b)
        # Grey reports no hue and black reports neither hue nor saturation.
        # Carrying the previous values forward stops both markers jumping home
        # when someone drags into a corner of the square.
        if sat == 0:
            hue = self._hsv[0]
            if val == 0:
                sat = self._hsv[1]
        self._hsv = [hue, sat, val]
        self.queue_draw()
        if notify:
            self.emit("color-changed")

    # ----------------------------------------------------------- geometry

    def _metrics(self):
        width = self.get_allocated_width()
        height = self.get_allocated_height()
        cx, cy = width / 2, height / 2
        outer = min(width, height) / 2 - 2
        inner = outer * (1 - RING_WIDTH)
        side = inner * math.sqrt(2)
        return cx, cy, outer, inner, side

    def _square_rect(self):
        cx, cy, _outer, _inner, side = self._metrics()
        return cx - side / 2, cy - side / 2, side

    # ------------------------------------------------------------ drawing

    def on_draw(self, _widget, cr):
        cx, cy, outer, inner, _side = self._metrics()
        self._draw_ring(cr, cx, cy, outer, inner)
        self._draw_square(cr)
        self._draw_ring_marker(cr, cx, cy, outer, inner)
        self._draw_square_marker(cr)
        return False

    def _draw_ring(self, cr, cx, cy, outer, inner):
        step = 2 * math.pi / RING_SEGMENTS
        # Each wedge spans rather more than its own step. Anything less leaves
        # the background showing through as radial stripes; abutting exactly
        # still shows seams, because Cairo antialiases both edges.
        span = step * 2
        for i in range(RING_SEGMENTS):
            angle = i * step
            r, g, b = colorsys.hsv_to_rgb(i / RING_SEGMENTS, 1.0, 1.0)
            cr.set_source_rgb(r, g, b)
            cr.new_path()
            # -pi/2 puts hue 0 at twelve o'clock rather than three.
            cr.arc(cx, cy, outer, angle - math.pi / 2,
                   angle + span - math.pi / 2)
            cr.arc_negative(cx, cy, inner, angle + span - math.pi / 2,
                            angle - math.pi / 2)
            cr.close_path()
            cr.fill()

    def _draw_square(self, cr):
        x, y, side = self._square_rect()
        hue = self._hsv[0]
        r, g, b = colorsys.hsv_to_rgb(hue, 1.0, 1.0)

        cr.set_source_rgb(r, g, b)
        cr.rectangle(x, y, side, side)
        cr.fill()

        # White at the left for saturation, black at the bottom for value.
        white = cairo.LinearGradient(x, y, x + side, y)
        white.add_color_stop_rgba(0, 1, 1, 1, 1)
        white.add_color_stop_rgba(1, 1, 1, 1, 0)
        cr.set_source(white)
        cr.rectangle(x, y, side, side)
        cr.fill()

        black = cairo.LinearGradient(x, y, x, y + side)
        black.add_color_stop_rgba(0, 0, 0, 0, 0)
        black.add_color_stop_rgba(1, 0, 0, 0, 1)
        cr.set_source(black)
        cr.rectangle(x, y, side, side)
        cr.fill()

    def _draw_ring_marker(self, cr, cx, cy, outer, inner):
        angle = self._hsv[0] * 2 * math.pi - math.pi / 2
        mid = (outer + inner) / 2
        mx, my = cx + math.cos(angle) * mid, cy + math.sin(angle) * mid
        radius = (outer - inner) / 2 - 1
        # Black under white so the marker stays visible over yellow as well
        # as over blue.
        cr.set_line_width(3)
        cr.set_source_rgb(0, 0, 0)
        cr.arc(mx, my, radius, 0, 2 * math.pi)
        cr.stroke()
        cr.set_line_width(1.5)
        cr.set_source_rgb(1, 1, 1)
        cr.arc(mx, my, radius, 0, 2 * math.pi)
        cr.stroke()

    def _draw_square_marker(self, cr):
        x, y, side = self._square_rect()
        _hue, sat, val = self._hsv
        mx = x + sat * side
        my = y + (1 - val) * side
        cr.set_line_width(3)
        cr.set_source_rgb(0, 0, 0)
        cr.arc(mx, my, 6, 0, 2 * math.pi)
        cr.stroke()
        cr.set_line_width(1.5)
        cr.set_source_rgb(1, 1, 1)
        cr.arc(mx, my, 6, 0, 2 * math.pi)
        cr.stroke()

    # ------------------------------------------------------------- input

    def _hit(self, x, y):
        cx, cy, outer, inner, _side = self._metrics()
        distance = math.hypot(x - cx, y - cy)
        if inner <= distance <= outer:
            return "ring"
        sx, sy, side = self._square_rect()
        if sx <= x <= sx + side and sy <= y <= sy + side:
            return "square"
        return None

    def _apply(self, target, x, y):
        if target == "ring":
            cx, cy, *_ = self._metrics()
            angle = math.atan2(y - cy, x - cx) + math.pi / 2
            self._hsv[0] = (angle / (2 * math.pi)) % 1.0
        else:
            sx, sy, side = self._square_rect()
            self._hsv[1] = max(0.0, min(1.0, (x - sx) / side))
            self._hsv[2] = max(0.0, min(1.0, 1 - (y - sy) / side))
        self.queue_draw()
        self.emit("color-changed")

    def on_button_press(self, _widget, event):
        target = self._hit(event.x, event.y)
        if target:
            self._drag = target
            self._apply(target, event.x, event.y)
        return True

    def on_button_release(self, _widget, _event):
        self._drag = None
        return True

    def on_motion(self, _widget, event):
        # Once a drag starts it keeps its target, so sliding off the ring into
        # the square does not hand control to the square mid-gesture.
        if self._drag:
            self._apply(self._drag, event.x, event.y)
        return True


class ColorEditor(Gtk.Box):
    """The wheel plus hex and R/G/B entries, all kept in step.

    Emits `color-changed` when the user changes the colour by any of the three
    routes. `set_rgb` is silent, so a caller syncing the editor to a newly
    selected zone does not read back as a user edit.
    """

    __gsignals__ = {
        "color-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        # One guard for every cross-update path. Without it, setting the hex
        # entry from the wheel re-enters through `changed` and fights the
        # wheel for the low bits of the colour.
        self._syncing = False

        self.wheel = ColorWheel()
        self.wheel.connect("color-changed", self.on_wheel_changed)
        self.pack_start(self.wheel, False, False, 0)

        fields = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        fields.set_valign(Gtk.Align.CENTER)

        self.preview = Gtk.DrawingArea()
        self.preview.set_size_request(96, 32)
        self.preview.connect("draw", self.on_preview_draw)
        fields.pack_start(self.preview, False, False, 0)

        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        self.spins = []
        for row, label in enumerate(("R", "G", "B")):
            grid.attach(Gtk.Label(label=label, xalign=1), 0, row, 1, 1)
            spin = Gtk.SpinButton.new_with_range(0, 255, 1)
            spin.set_numeric(True)
            spin.set_width_chars(4)
            spin.connect("value-changed", self.on_spin_changed)
            grid.attach(spin, 1, row, 1, 1)
            self.spins.append(spin)

        grid.attach(Gtk.Label(label="Hex", xalign=1), 0, 3, 1, 1)
        self.hex_entry = Gtk.Entry()
        self.hex_entry.set_width_chars(8)
        self.hex_entry.set_max_length(7)
        # `activate` (Enter) and focus-out only: reacting to every keystroke
        # would fight the user halfway through typing "#ff0000".
        self.hex_entry.connect("activate", self.on_hex_committed)
        self.hex_entry.connect("focus-out-event", self.on_hex_focus_out)
        grid.attach(self.hex_entry, 1, 3, 1, 1)

        fields.pack_start(grid, False, False, 0)
        self.pack_start(fields, False, False, 0)

        self.set_rgb([255, 255, 255])

    # ------------------------------------------------------------- colour

    @property
    def rgb(self):
        return self.wheel.rgb

    def set_rgb(self, rgb, notify=False):
        self.wheel.set_rgb(rgb)
        self._sync_fields(self.wheel.rgb)
        if notify:
            self.emit("color-changed")

    def _sync_fields(self, rgb):
        self._syncing = True
        try:
            for spin, value in zip(self.spins, rgb):
                spin.set_value(value)
            self.hex_entry.set_text(rgb_to_hex(rgb))
        finally:
            self._syncing = False
        self.preview.queue_draw()

    # -------------------------------------------------------------- draw

    def on_preview_draw(self, widget, cr):
        r, g, b = (c / 255 for c in self.wheel.rgb)
        cr.set_source_rgb(r, g, b)
        cr.rectangle(0, 0, widget.get_allocated_width(),
                     widget.get_allocated_height())
        cr.fill_preserve()
        cr.set_line_width(1)
        cr.set_source_rgba(0, 0, 0, 0.4)
        cr.stroke()
        return False

    # ------------------------------------------------------------ signals

    def on_wheel_changed(self, _wheel):
        if self._syncing:
            return
        self._sync_fields(self.wheel.rgb)
        self.emit("color-changed")

    def on_spin_changed(self, _spin):
        if self._syncing:
            return
        rgb = [int(spin.get_value()) for spin in self.spins]
        self.wheel.set_rgb(rgb)
        self._syncing = True
        try:
            self.hex_entry.set_text(rgb_to_hex(rgb))
        finally:
            self._syncing = False
        self.preview.queue_draw()
        self.emit("color-changed")

    def on_hex_committed(self, _entry):
        if self._syncing:
            return
        try:
            rgb = hex_to_rgb(self.hex_entry.get_text())
        except ValueError:
            # Put the last good value back rather than reporting an error for
            # something the user can see is half-typed.
            self._sync_fields(self.wheel.rgb)
            return
        self.wheel.set_rgb(rgb)
        self._sync_fields(self.wheel.rgb)
        self.emit("color-changed")

    def on_hex_focus_out(self, _entry, _event):
        self.on_hex_committed(_entry)
        return False
