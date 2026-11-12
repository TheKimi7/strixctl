"""GTK3 control window.

GTK3 rather than GTK4 because this targets Cinnamon, which is GTK3 native and
whose tray API (XApp) is GTK3 only.  Nothing here needs root: every write goes
to an attribute udev has already handed to the users group, so an unwritable
knob is a UI state to render, not an exception to catch.

How the window thinks
---------------------
Every control is independent. The fans and the keyboard each have their own
page, their own unapplied state and their own Apply, and applying one never
touches the other. The power mode and the charge limit belong to the machine
rather than to a page, so they live on Home and take effect the moment they
are picked.

`self.config` is the working copy and `self._disk` is what is saved. A section
whose two copies differ is unapplied; Apply saves and applies that section
alone, so an unapplied edit on the other page never rides along.

Anything that runs OpenRGB goes through StrixWindow._in_background: one call
takes a couple of seconds and may take thirty, which is far too long to hold
the GTK main loop.

All styling hangs off the `strix-*` classes in CSS below, and is limited to
size and spacing: colours and fonts come from the desktop theme.
"""

import copy
import threading

import cairo
import gi

# Both must be pinned. `from gi.repository import Gdk, Gtk` resolves Gdk first,
# and with gir1.2-gtk-4.0 also installed an unversioned Gdk loads 4.0, which
# then collides with Gtk 3.0.
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango

from . import apply as apply_mod
from . import aura as aura_mod
from . import config as cfg
from . import hw
from . import theme
from .colorwheel import ColorEditor

#: Used only when the NeoPOP skin is off; with it on the curves are
#: monochrome and told apart by theme.CURVE_DASH instead.
CURVE_COLORS = {"cpu": (0.20, 0.55, 0.95), "gpu": (0.95, 0.45, 0.15)}

#: Set once, by the window, before anything is drawn. The cairo widgets read
#: it at draw time to decide whether to paint in colour or in ink.
SKIN = False
FAN_TITLES = {"cpu": "CPU fan", "gpu": "GPU fan"}
CHARGE_CHOICES = (60, 80, 100)

#: The plain look, for `"theme": "system"` in config.json. The skin's own
#: stylesheet lives in theme.py.
PLAIN_CSS = b"""
.strix-tile { padding: 10px 14px; border: 1px solid alpha(@theme_fg_color, 0.14);
              border-radius: 6px; }
.strix-title { font-size: 170%; font-weight: bold; }
.strix-subtitle { font-size: 85%; opacity: 0.6; }
.strix-tile-title { font-size: 85%; opacity: 0.6; }
.strix-tile-value { font-size: 170%; font-weight: bold; }
.strix-tile-sub { font-size: 85%; opacity: 0.6; }
.strix-heading { font-weight: bold; }
.strix-dim { opacity: 0.6; }
.strix-point entry { min-width: 0; padding-left: 4px; padding-right: 4px; }
"""


def clamp_point(points, index, temp, percent):
    """Pull one point back inside the curves FanCurve.validate() accepts.

    Each point is held between its neighbours, and the last point keeps its
    speed at the ceiling and only moves in temperature. Used by the graph and
    the number boxes alike, so neither can produce a curve Apply would reject.
    """
    low_t = points[index - 1][0] if index > 0 else 0
    high_t = points[index + 1][0] if index + 1 < len(points) else 110
    low_p = points[index - 1][1] if index > 0 else 0
    high_p = points[index + 1][1] if index + 1 < len(points) else 100
    temp = max(low_t, min(high_t, int(temp)))
    percent = max(low_p, min(high_p, int(percent)))
    if index == len(points) - 1:
        percent = hw.FanCurve.CEILING
    return temp, percent


def speed_at(points, temp):
    """The speed a curve asks for at `temp`, read off straight line segments."""
    if not points:
        return None
    if temp <= points[0][0]:
        return points[0][1]
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        if temp <= t1:
            if t1 == t0:
                return p1
            return p0 + (p1 - p0) * (temp - t0) / (t1 - t0)
    return points[-1][1]


def _label(text="", *classes, xalign=0, wrap=False):
    label = Gtk.Label(label=text, xalign=xalign)
    label.set_line_wrap(wrap)
    for name in classes:
        label.get_style_context().add_class(name)
    return label


#: States in which a plate is flush with the page, so it has no planes.
_FLUSH = (Gtk.StateFlags.ACTIVE | Gtk.StateFlags.CHECKED
          | Gtk.StateFlags.INSENSITIVE)


def _face_of(widget):
    """(fill, flush) for a plate: which face colour, and is it pressed in.

    Holding a plate only moves it, as in the reference. Turning the face over
    means something else: this plate is the chosen one, or the one that acts.
    """
    style = widget.get_style_context()
    if style.has_class("strix-tile"):
        return theme.PLATE_FILL["card"], False
    flags = widget.get_state_flags()
    held = bool(flags & Gtk.StateFlags.ACTIVE)
    if flags & Gtk.StateFlags.INSENSITIVE:
        return theme.PLATE_FILL["off"], True
    if flags & Gtk.StateFlags.CHECKED:
        return theme.PLATE_FILL["down"], True
    if style.has_class("suggested-action"):
        return theme.PLATE_FILL["down"], held
    return theme.PLATE_FILL["rest"], held


def _plate_draw(widget, cr):
    """Paint a plate: the face, and the two planes that stand it up.

    Runs before GTK draws the widget, so the label lands on top of the face.
    Both planes are the reference's 45 degree shear, which falls out of
    starting the plane's far edge `edge` pixels further down (the right one)
    or along (the bottom one). They meet on the diagonal through the corner,
    so the corner is mitred rather than painted twice.

    Pressed, the face moves into the space the planes were in and they are
    not drawn at all, which is what makes a pressed plate read as flush.
    """
    edge = theme.EDGE
    width, height = widget.get_allocated_width(), widget.get_allocated_height()
    right, bottom = width - edge, height - edge
    if right <= 0 or bottom <= 0:
        return False
    face, flush = _face_of(widget)
    cr.set_source_rgb(*face)
    if flush:
        cr.rectangle(edge, edge, right, bottom)
        cr.fill()
        return False
    cr.rectangle(0, 0, right, bottom)
    cr.fill()
    cr.set_source_rgb(*theme.POP_FILL["bottom"])
    cr.move_to(0, bottom)
    cr.line_to(right, bottom)
    cr.line_to(width, height)
    cr.line_to(edge, height)
    cr.close_path()
    cr.fill()
    cr.set_source_rgb(*theme.POP_FILL["right"])
    cr.move_to(right, 0)
    cr.line_to(width, edge)
    cr.line_to(width, height)
    cr.line_to(right, bottom)
    cr.close_path()
    cr.fill()
    return False


def plate(widget, big=False):
    """Make a widget a NeoPOP plate, drawn rather than themed.

    The face has to be drawn rather than left to CSS: GTK3 clips a widget's
    drawing to its CSS margin box, so a plate that reserved room for the
    planes with a margin could not then reach it. Instead the plate fills its
    whole allocation, the planes take the last `theme.EDGE` pixels of it, and
    the stylesheet pads the label inwards by the same amount.

    A no-op when the skin is off, so call sites never have to ask.
    """
    if not SKIN:
        return widget
    style = widget.get_style_context()
    style.add_class("strix-pop")
    if big:
        style.add_class("strix-big")       # the reference's size="big"
    widget.connect("draw", _plate_draw)
    return widget


class Choice(Gtk.Box):
    """A row of joined buttons of which exactly one is down.

    The visible alternative to a drop-down: every option is on screen, and the
    current one is obvious. `on_pick(key)` fires for clicks only, never for
    set_active().
    """

    def __init__(self, on_pick=None):
        # Joined into one block under the desktop theme, but spaced apart
        # under the skin, where every plate stands up on its own planes.
        super().__init__(spacing=6 if SKIN else 0)
        if not SKIN:
            self.get_style_context().add_class("linked")
        self.on_pick = on_pick
        self._buttons = {}
        self._quiet = False

    def set_options(self, options):
        """`options` is [(key, label), ...]. Keeps the selection if it survives."""
        keep = self.get_active()
        for child in self.get_children():
            self.remove(child)
        self._buttons = {}
        first = None
        self._quiet = True
        try:
            for key, text in options:
                button = Gtk.RadioButton.new_with_label_from_widget(first, text)
                button.set_mode(False)
                plate(button)
                first = first or button
                button.connect("toggled", self._on_toggled, key)
                self.pack_start(button, False, False, 0)
                self._buttons[key] = button
        finally:
            self._quiet = False
        self.show_all()
        self.set_active(keep)

    def get_active(self):
        for key, button in self._buttons.items():
            if button.get_active():
                return key
        return None

    def set_active(self, key):
        if key not in self._buttons:
            return
        self._quiet = True
        try:
            self._buttons[key].set_active(True)
        finally:
            self._quiet = False

    def _on_toggled(self, button, key):
        if button.get_active() and not self._quiet and self.on_pick:
            self.on_pick(key)


class Tile(Gtk.Box):
    """One reading on the home page: a small title, a big value, a footnote."""

    def __init__(self, title):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.get_style_context().add_class("strix-tile")
        plate(self)             # a card stands up the same way a button does
        self.value = _label("–", "strix-tile-value")
        self.value.set_ellipsize(Pango.EllipsizeMode.END)
        self.sub = _label("", "strix-tile-sub")
        self.sub.set_ellipsize(Pango.EllipsizeMode.END)
        self.pack_start(_label(title, "strix-tile-title"), False, False, 0)
        self.pack_start(self.value, False, False, 0)
        self.pack_start(self.sub, False, False, 0)

    def show_reading(self, value, sub=""):
        self.value.set_text(value)
        self.sub.set_text(sub)


class CurvePlot(Gtk.DrawingArea):
    """Both fan curves on one graph; the selected fan's points can be dragged.

    Holds no curve of its own: it draws the lists it is given and reports a
    drag through `on_point_moved(fan, index, temp, percent)`, already clamped.
    """

    LEFT, RIGHT, TOP, BOTTOM = 44, 16, 34, 24
    GRAB_RADIUS = 12

    def __init__(self, on_point_moved=None):
        super().__init__()
        self.curves = {"cpu": [], "gpu": []}
        self.selected = "cpu"
        self.on_point_moved = on_point_moved
        self.editable = True
        self.temp_now = None
        self.rpm = {}
        self.caption = ""
        self._drag = None      # index held for the whole gesture
        self._hover = None
        self.set_size_request(-1, 180)

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

    def show_curves(self, curves, selected, editable, caption=""):
        self.curves = curves
        self.selected = selected
        self.editable = editable
        self.caption = caption
        if not editable:
            self._drag = self._hover = None
        self.queue_draw()

    def show_readings(self, temp, rpm):
        if (temp, rpm) != (self.temp_now, self.rpm):
            self.temp_now, self.rpm = temp, dict(rpm)
            self.queue_draw()

    # ------------------------------------------------------------- geometry

    def _temp_range(self):
        """Widen the axis to hold every point, so none is drawn off the edge."""
        temps = [t for points in self.curves.values() for t, _p in points]
        low = min([20] + [t // 10 * 10 for t in temps])
        high = max([100] + [-(-t // 10) * 10 for t in temps])
        return low, high

    def _plot_area(self):
        return (self.LEFT, self.TOP,
                max(1, self.get_allocated_width() - self.LEFT - self.RIGHT),
                max(1, self.get_allocated_height() - self.TOP - self.BOTTOM))

    def _to_xy(self, temp, percent):
        left, top, width, height = self._plot_area()
        low, high = self._temp_range()
        temp = max(low, min(high, temp))
        return (left + width * (temp - low) / (high - low),
                top + height * (1 - percent / 100))

    def _from_xy(self, x, y):
        left, top, width, height = self._plot_area()
        low, high = self._temp_range()
        temp = low + (high - low) * (x - left) / width
        percent = 100 * (1 - (y - top) / height)
        return int(round(temp)), int(round(percent))

    def _nearest(self, x, y):
        """Nearest point of the selected fan within GRAB_RADIUS, or None."""
        best, best_distance = None, self.GRAB_RADIUS ** 2
        for index, (temp, percent) in enumerate(self.curves.get(self.selected, [])):
            px, py = self._to_xy(temp, percent)
            distance = (px - x) ** 2 + (py - y) ** 2
            if distance <= best_distance:
                best, best_distance = index, distance
        return best

    # ------------------------------------------------------------- gestures

    def on_button_press(self, _widget, event):
        if not self.editable or event.button != 1:
            return False
        # Resolve the grabbed point once and hold it: curves often contain
        # duplicate coordinates, so re-resolving on every motion event could
        # hop between them mid-drag.
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
                        self.get_display(),
                        "grab" if hover is not None else "default"))
                self.queue_draw()
            return False

        temp, percent = self._from_xy(event.x, event.y)
        temp, percent = clamp_point(self.curves[self.selected], self._drag,
                                    temp, percent)
        if self.on_point_moved is not None:
            self.on_point_moved(self.selected, self._drag, temp, percent)
        return True

    # ------------------------------------------------------------- painting

    @staticmethod
    def _ink(cr, fan, alpha):
        """Set the colour and dash for one fan's line."""
        if SKIN:
            cr.set_source_rgba(*theme.PLOT["line"], alpha)
            cr.set_dash(theme.CURVE_DASH[fan])
        else:
            cr.set_source_rgba(*CURVE_COLORS[fan], alpha)

    def on_draw(self, _widget, cr):
        left, top, width, height = self._plot_area()
        fg = self.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        low, high = self._temp_range()
        cr.select_font_face("Sans")
        cr.set_font_size(10)
        cr.set_line_width(1)

        # Grid with a number on every line, so a point can be read off it.
        for percent in (0, 25, 50, 75, 100):
            y = top + height * (1 - percent / 100)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.12)
            cr.move_to(left, y)
            cr.line_to(left + width, y)
            cr.stroke()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.6)
            text = f"{percent}%"
            cr.move_to(left - 8 - cr.text_extents(text).width, y + 3)
            cr.show_text(text)
        for temp in range(low, high + 1, 10):
            x, _y = self._to_xy(temp, 0)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.07)
            cr.move_to(x, top)
            cr.line_to(x, top + height)
            cr.stroke()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.6)
            text = f"{temp}°"
            cr.move_to(x - cr.text_extents(text).width / 2, top + height + 16)
            cr.show_text(text)

        # Where the CPU is right now, so the curve can be read against it.
        show_now = self.temp_now is not None and low <= self.temp_now <= high
        if show_now:
            x, _y = self._to_xy(self.temp_now, 0)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.45)
            cr.set_dash([4, 4])
            cr.move_to(x, top)
            cr.line_to(x, top + height)
            cr.stroke()
            cr.set_dash([])
            text = f"CPU {self.temp_now}°C"
            cr.move_to(left + width - cr.text_extents(text).width, top - 14)
            cr.show_text(text)

        # The fan being edited goes on top, the other one stays in the back.
        order = [fan for fan in self.curves if fan != self.selected] + [self.selected]
        for fan in order:
            points = self.curves.get(fan) or []
            if not points:
                continue
            front = fan == self.selected or not self.editable
            alpha = 1.0 if front else (0.5 if SKIN else 0.35)
            self._ink(cr, fan, alpha)
            cr.set_line_width(2.5 if front else 1.5)
            for index, (temp, percent) in enumerate(points):
                x, y = self._to_xy(temp, percent)
                cr.line_to(x, y) if index else cr.move_to(x, y)
            cr.stroke()
            cr.set_dash([])
            if front and self.editable:
                # Square handles, to match the plates everywhere else.
                for index, (temp, percent) in enumerate(points):
                    x, y = self._to_xy(temp, percent)
                    size = 11 if index in (self._drag, self._hover) else 7
                    if SKIN:
                        cr.rectangle(x - size / 2, y - size / 2, size, size)
                    else:
                        cr.arc(x, y, size / 2, 0, 6.2832)
                    cr.fill()
            if show_now:
                x, y = self._to_xy(self.temp_now, speed_at(points, self.temp_now))
                if SKIN:
                    cr.set_source_rgba(*theme.PLOT["line"], alpha)
                    cr.rectangle(x - 6, y - 6, 12, 12)
                    cr.fill()
                    cr.set_source_rgb(*_rgb(theme.INK["page"]))
                    cr.rectangle(x - 3, y - 3, 6, 6)
                    cr.fill()
                else:
                    cr.set_source_rgba(fg.red, fg.green, fg.blue, alpha)
                    cr.arc(x, y, 6, 0, 6.2832)
                    cr.fill()
                    cr.set_source_rgba(*CURVE_COLORS[fan], alpha)
                    cr.arc(x, y, 3.5, 0, 6.2832)
                    cr.fill()

        # Legend. The sample is drawn with the curve's own line style, so a
        # monochrome graph still says which line is which.
        cr.set_font_size(11)
        if SKIN and theme.HEAVY:
            cr.select_font_face(theme.HEAVY, cairo.FONT_SLANT_NORMAL,
                                cairo.FONT_WEIGHT_BOLD)
        x = left
        for fan in ("cpu", "gpu"):
            speed = self.rpm.get(fan)
            text = FAN_TITLES[fan] + (f"  {speed} rpm" if speed is not None else "")
            self._ink(cr, fan, 1.0)
            cr.set_line_width(2.5)
            cr.move_to(x, top - 18)
            cr.line_to(x + 16, top - 18)
            cr.stroke()
            cr.set_dash([])
            cr.move_to(x + 22, top - 14)
            cr.show_text(text)
            x += cr.text_extents(text).width + 46
        if self.caption:
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.6)
            cr.move_to(left + width - cr.text_extents(self.caption).width - 8,
                       top + height - 8)
            cr.show_text(self.caption)


class KeyboardStrip(Gtk.DrawingArea):
    """The keyboard's four zones drawn left to right, as they sit on the desk.

    Click a zone to pick the one the colour wheel edits. `paint()` decides how
    it is drawn: per zone, one colour throughout, or the effect's own cycle.
    """

    ZONED, SINGLE, CYCLE = "zoned", "single", "cycle"

    def __init__(self, on_zone=None, height=58):
        super().__init__()
        self.colours = [[255, 255, 255]] * cfg.ZONES
        self.how = self.ZONED
        self.active = 0
        self.on_zone = on_zone
        self.set_size_request(-1, height)
        if on_zone is not None:
            self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
            self.connect("button-press-event", self.on_press)
        self.connect("draw", self.on_draw)

    def paint(self, colours, how, active=0):
        self.colours = [list(c) for c in colours]
        self.how, self.active = how, active
        self.queue_draw()

    def on_press(self, widget, event):
        if self.how != self.ZONED or not self.get_sensitive():
            return False
        zone = int(event.x * cfg.ZONES / max(1, widget.get_allocated_width()))
        self.on_zone(max(0, min(cfg.ZONES - 1, zone)))
        return True

    def on_draw(self, widget, cr):
        width = widget.get_allocated_width()
        height = widget.get_allocated_height()
        fg = self.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        dim = 1.0 if self.get_sensitive() else 0.35
        if self.how == self.CYCLE:
            # A stand-in for "the effect picks": the hues, left to right.
            for x in range(width):
                cr.set_source_rgba(*_hue(x / max(1, width)), dim)
                cr.rectangle(x, 0, 1.5, height)
                cr.fill()
            return False
        gap = 4 if self.how == self.ZONED else 0
        span = (width - gap * (cfg.ZONES - 1)) / cfg.ZONES
        for zone in range(cfg.ZONES):
            colour = self.colours[0 if self.how == self.SINGLE else zone]
            cr.set_source_rgba(*(c / 255 for c in colour), dim)
            cr.rectangle(zone * (span + gap), 0, span + (0 if gap else 0.5), height)
            cr.fill()
        if self.how == self.ZONED and self.on_zone is not None:
            # The selected zone gets a paper rule with an ink keyline inside,
            # which stays visible whatever colour the zone is set to.
            x = self.active * (span + gap)
            cr.set_line_width(3)
            if SKIN:
                cr.set_source_rgba(*theme.PLOT["line"], dim)
            else:
                cr.set_source_rgba(fg.red, fg.green, fg.blue, dim)
            cr.rectangle(x + 1.5, 1.5, span - 3, height - 3)
            cr.stroke()
            cr.set_line_width(1)
            cr.set_source_rgba(0, 0, 0, 0.6 * dim)
            cr.rectangle(x + 3.5, 3.5, span - 7, height - 7)
            cr.stroke()
        return False


def _rgb(value):
    """"#rrggbb" as the 0-1 triple cairo wants."""
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _hue(position):
    """RGB for a hue in 0-1, at full saturation and value."""
    sector = (position % 1.0) * 6
    rise = sector - int(sector)
    return [(1, rise, 0), (1 - rise, 1, 0), (0, 1, rise),
            (0, 1 - rise, 1), (rise, 0, 1), (1, 0, 1 - rise)][int(sector) % 6]


class StrixWindow(Gtk.Window):
    #: Shown at the foot of the keyboard page until OpenRGB has answered.
    _aura_note = "Looking for the keyboard…"

    def __init__(self):
        super().__init__(title="strixctl")
        self.set_default_size(720, 700)

        self.config = cfg.load()
        startup_notes = list(cfg.warnings)

        # Before any widget exists: the skin loads fonts, and Pango caches
        # its font map the moment something is measured.
        global SKIN
        SKIN = theme.apply(self.config)
        provider = Gtk.CssProvider()
        provider.load_from_data(PLAIN_CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            - (1 if SKIN else 0))
        self._disk = copy.deepcopy(self.config)
        self._seen_mtime = cfg.mtime()
        self.battery = hw.Battery()
        self.platform = hw.PlatformProfile()
        self.fans = hw.Fans()
        self.kbd = hw.KeyboardBacklight()
        self.sensors = hw.Sensors()
        self.aura = aura_mod.Aura(settings=self.config.get("openrgb"))
        self.fan = "cpu"            # which curve the fan page edits
        self.zone = 0               # which zone the colour wheel edits
        self._alive = True
        self._busy = False
        self._aura_ready = False
        #: section name -> (apply button, revert button, "not applied" label)
        self._actions = {}
        # True while widgets are being primed: a primed widget must not be
        # mistaken for the user changing something.
        self._loading = True
        self.connect("destroy", self._on_destroy)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.get_style_context().add_class("strix-root")
        self.add(outer)
        self.infobar = self._build_infobar()
        outer.pack_start(self.infobar, False, False, 0)
        outer.pack_start(self._build_header(), False, False, 0)

        self.stack = Gtk.Stack()
        self.stack.add_titled(self._build_home_page(), "home", "Home")
        self.stack.add_titled(self._build_fan_page(), "fans", "Fans")
        self.stack.add_titled(self._build_keyboard_page(), "keyboard", "Keyboard")
        switcher = Gtk.StackSwitcher()
        switcher.set_stack(self.stack)
        switcher.set_halign(Gtk.Align.START)
        for tab in switcher.get_children():
            plate(tab)          # the switcher builds its own buttons
        switcher.set_margin_start(14)
        outer.pack_start(switcher, False, False, 0)
        outer.pack_start(self.stack, True, True, 0)

        self.status = _label("", "strix-dim", wrap=True)
        self.status.set_margin_start(14)
        self.status.set_margin_end(14)
        self.status.set_margin_bottom(10)
        outer.pack_start(self.status, False, False, 0)

        self.load_widgets()
        if startup_notes:
            self.set_status(" ".join(startup_notes))

        self._tick()
        GLib.timeout_add_seconds(2, self._tick)
        if self.aura.available:
            self._in_background(self.aura.list_devices, self._aura_probed)
        else:
            self._aura_probed(None, None)

    def present_all(self):
        """Show the window; show_all() alone would unhide what is meant to hide."""
        self.show_all()
        self.infobar.set_visible(bool(self._infobar_label.get_text()))
        self._sync_fan_page()
        self._sync_keyboard_page()

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

    # ------------------------------------------------------------------ chrome

    def _build_header(self):
        """The name of the thing, and the machine it is talking to."""
        box = Gtk.Box(spacing=10)
        box.set_margin_start(14)
        box.set_margin_end(14)
        box.set_margin_top(10)
        box.set_margin_bottom(8)
        board = _label(hw.board_name().upper(), "strix-subtitle")
        board.set_valign(Gtk.Align.END)
        board.set_margin_bottom(4)
        box.pack_start(_label("strixctl", "strix-title"), False, False, 0)
        box.pack_start(board, False, False, 0)
        return box

    def _build_infobar(self):
        bar = Gtk.InfoBar()
        bar.set_message_type(Gtk.MessageType.WARNING)
        label = _label("", wrap=True)
        bar.get_content_area().add(label)
        self._infobar_label = label
        bar.set_no_show_all(True)

        readonly = [name for name, backend in (
            ("charge limit", self.battery), ("power mode", self.platform),
            ("fan curves", self.fans))
            if backend.available and not backend.writable]
        if readonly:
            label.set_text(
                "Read-only: " + ", ".join(readonly) + ".  Run install.sh once "
                "(it installs a udev rule) and log back in to enable control.")
            bar.show()
            bar.get_content_area().show_all()
        return bar

    # ------------------------------------------------------------------- pages

    @staticmethod
    def _page():
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_border_width(12)
        return page

    def _action_row(self, section, apply_label, on_apply):
        """The Apply/Revert pair for one page, live only when it is unapplied."""
        row = Gtk.Box(spacing=8)
        note = _label("", "strix-dim", xalign=1)
        apply_button = Gtk.Button(label=apply_label)
        apply_button.get_style_context().add_class("suggested-action")
        apply_button.connect("clicked", on_apply)
        plate(apply_button, big=True)
        revert = Gtk.Button(label="Revert")
        revert.connect("clicked", self.on_revert, section)
        plate(revert, big=True)
        row.pack_end(apply_button, False, False, 0)
        row.pack_end(revert, False, False, 0)
        row.pack_end(note, True, True, 4)
        self._actions[section] = (apply_button, revert, note)
        return row

    def _build_home_page(self):
        page = self._page()

        tiles = Gtk.Box(spacing=10, homogeneous=True)
        self.tile_temp = Tile("CPU")
        self.tile_fans = {fan: Tile(FAN_TITLES[fan]) for fan in ("cpu", "gpu")}
        self.tile_kbd = Tile("Keyboard")
        self.home_strip = KeyboardStrip(height=8)
        self.tile_kbd.pack_start(self.home_strip, False, False, 2)
        for tile in (self.tile_temp, self.tile_fans["cpu"],
                     self.tile_fans["gpu"], self.tile_kbd):
            tiles.pack_start(tile, True, True, 0)
        page.pack_start(tiles, False, False, 0)

        grid = Gtk.Grid(column_spacing=14, row_spacing=10)
        grid.set_margin_top(6)
        grid.attach(_label("Power mode", "strix-heading"), 0, 0, 1, 1)
        self.power_choice = Choice(self.on_power_picked)
        self.power_choice.set_options(
            [(name, name.capitalize()) for name in self.platform.choices])
        self.power_choice.set_sensitive(self.platform.writable)
        self.power_choice.set_tooltip_text(
            "How hard the firmware lets the CPU and GPU run.")
        grid.attach(self.power_choice, 1, 0, 2, 1)

        grid.attach(_label("Fans", "strix-heading"), 0, 1, 1, 1)
        self.home_fans = _label("")
        self.home_fans.set_hexpand(True)
        grid.attach(self.home_fans, 1, 1, 1, 1)
        edit_fans = Gtk.Button(label="Edit the curve")
        edit_fans.connect("clicked",
                          lambda *_: self.stack.set_visible_child_name("fans"))
        plate(edit_fans)
        grid.attach(edit_fans, 2, 1, 1, 1)

        grid.attach(_label("Keyboard", "strix-heading"), 0, 2, 1, 1)
        self.home_kbd = _label("")
        grid.attach(self.home_kbd, 1, 2, 1, 1)
        edit_kbd = Gtk.Button(label="Edit the colour")
        edit_kbd.connect("clicked",
                         lambda *_: self.stack.set_visible_child_name("keyboard"))
        plate(edit_kbd)
        grid.attach(edit_kbd, 2, 2, 1, 1)

        grid.attach(_label("Battery", "strix-heading"), 0, 3, 1, 1)
        if self.battery.available:
            row = Gtk.Box(spacing=10)
            row.pack_start(_label("Stop charging at", "strix-dim"), False, False, 0)
            self.charge_choice = Choice(self.on_charge_picked)
            self.charge_choice.set_sensitive(self.battery.writable)
            row.pack_start(self.charge_choice, False, False, 0)
            grid.attach(row, 1, 3, 2, 1)
        else:
            grid.attach(_label("no charge limit on this battery", "strix-dim"),
                        1, 3, 2, 1)
        page.pack_start(grid, False, False, 0)

        page.pack_start(_label(
            "The power mode and the charge limit take effect as soon as you "
            "pick one.", "strix-dim", wrap=True), False, False, 2)
        return page

    def _build_fan_page(self):
        page = self._page()
        if not self.fans.available:
            page.pack_start(_label(
                "No asus_custom_fan_curve hwmon node on this machine.",
                "strix-dim", wrap=True), False, False, 0)
            return page

        row = Gtk.Box(spacing=10)
        self.fan_mode = Choice(self.on_fan_mode_picked)
        self.fan_mode.set_options([("auto", "Firmware decides"),
                                   ("curve", "My curve")])
        self.fan_mode.set_sensitive(self.fans.writable)
        row.pack_start(self.fan_mode, False, False, 0)

        self.fan_pick = Choice(self.on_fan_picked)
        self.fan_pick.set_options([(fan, FAN_TITLES[fan]) for fan in ("cpu", "gpu")])
        self.fan_pick.set_active("cpu")
        self.fan_pick.set_tooltip_text("Which fan's curve to edit.")
        row.pack_end(self.fan_pick, False, False, 0)
        self.fan_pick_label = _label("Editing", "strix-dim")
        row.pack_end(self.fan_pick_label, False, False, 0)
        page.pack_start(row, False, False, 0)

        self.plot = CurvePlot(on_point_moved=self.on_point_moved)
        page.pack_start(self.plot, True, True, 0)

        self.fan_hint = _label("", "strix-dim", wrap=True)
        page.pack_start(self.fan_hint, False, False, 0)

        self.fan_tools = Gtk.Box(spacing=8)
        stock = Gtk.Button(label="Start from the firmware curve")
        stock.set_tooltip_text(
            "Replace both curves with the firmware's own, as a starting shape.")
        stock.connect("clicked", self.on_load_stock)
        plate(stock)
        self.numbers_toggle = Gtk.ToggleButton(label="Edit as numbers")
        self.numbers_toggle.connect("toggled", self.on_numbers_toggled)
        plate(self.numbers_toggle)
        self.fan_tools.pack_start(stock, False, False, 0)
        self.fan_tools.pack_start(self.numbers_toggle, False, False, 0)
        page.pack_start(self.fan_tools, False, False, 0)

        # One fan's eight points as two rows of boxes: the same numbers as the
        # graph, for when a drag is not exact enough.
        self.numbers = Gtk.Grid(column_spacing=6, row_spacing=4)
        self.numbers.get_style_context().add_class("strix-point")
        self.numbers.attach(_label("Point", "strix-dim"), 0, 0, 1, 1)
        self.numbers.attach(_label("°C", "strix-dim"), 0, 1, 1, 1)
        self.numbers.attach(_label("%", "strix-dim"), 0, 2, 1, 1)
        self.point_entries = []
        for index in range(hw.FAN_POINTS):
            self.numbers.attach(_label(str(index + 1), "strix-dim", xalign=0.5),
                                index + 1, 0, 1, 1)
            pair = []
            for kind, line in ((0, 1), (1, 2)):
                entry = Gtk.Entry()
                entry.set_width_chars(4)
                entry.set_max_length(3)
                entry.set_alignment(0.5)
                entry.set_input_purpose(Gtk.InputPurpose.DIGITS)
                entry.connect("activate", self.on_point_typed, index, kind)
                entry.connect("focus-out-event", self.on_point_left, index, kind)
                self.numbers.attach(entry, index + 1, line, 1, 1)
                pair.append(entry)
            self.point_entries.append(pair)
        self.point_entries[-1][1].set_sensitive(False)
        self.point_entries[-1][1].set_tooltip_text(
            "Fixed at 100% so the fans can always reach full speed.")
        page.pack_start(self.numbers, False, False, 0)

        page.pack_end(self._action_row("fans", "Apply to the fans",
                                       self.on_apply_fans), False, False, 0)
        return page

    def _build_keyboard_page(self):
        page = self._page()

        # The full menu for now; _aura_probed trims it to what the hardware
        # reports once OpenRGB has answered.
        self._modes = ([name for name, _role in aura_mod.MODES]
                       if self.aura.available else ["Static"])
        self.effect_choice = Choice(self.on_effect_picked)
        self.effect_choice.set_options([(mode, mode) for mode in self._modes])
        page.pack_start(self.effect_choice, False, False, 0)

        self.strip = KeyboardStrip(on_zone=self.on_zone_picked)
        page.pack_start(self.strip, False, False, 0)
        self.zone_hint = _label("", "strix-dim", wrap=True)
        page.pack_start(self.zone_hint, False, False, 0)

        # The colour editor on the left, and beside it whatever else the
        # effect takes. Side by side so the page fits without scrolling.
        body = Gtk.Box(spacing=24)
        self.colour_editor = ColorEditor()
        self.colour_editor.connect("color-changed", self.on_editor_colour)
        body.pack_start(self.colour_editor, False, False, 0)

        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.speed_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.speed_row.pack_start(_label("Speed", "strix-dim"), False, False, 0)
        self.speed_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0, 100, 5)
        self.speed_scale.set_draw_value(False)
        self.speed_scale.set_size_request(220, -1)
        for mark, name in ((0, "slow"), (100, "fast")):
            self.speed_scale.add_mark(mark, Gtk.PositionType.BOTTOM, name)
        self.speed_scale.connect("value-changed", self.on_speed_changed)
        self.speed_row.pack_start(self.speed_scale, False, False, 0)
        side.pack_start(self.speed_row, False, False, 0)

        self.match_button = Gtk.Button(label="Use this colour on every zone")
        self.match_button.set_halign(Gtk.Align.START)
        self.match_button.connect("clicked", self.on_match_zones)
        plate(self.match_button)
        side.pack_start(self.match_button, False, False, 0)
        body.pack_start(side, False, False, 0)
        page.pack_start(body, False, False, 0)

        self.kbd_note = _label("", "strix-dim", wrap=True)
        page.pack_start(self.kbd_note, False, False, 0)
        page.pack_end(self._action_row("aura", "Apply to the keyboard",
                                       self.on_apply_aura), False, False, 0)
        return page

    # ----------------------------------------------------------------- state

    def _aura_settings(self):
        return self.config.setdefault("aura", {})

    def _fan_settings(self):
        return self.config.setdefault("fans", {})

    def _curves(self):
        return cfg.curves(self.config)

    def _dirty(self, section):
        return self.config.get(section) != self._disk.get(section)

    @property
    def dirty(self):
        return any(self._dirty(section) for section in self._actions)

    def _refresh_actions(self):
        """Something moved: light up the Apply that owns it, and refresh Home."""
        for section, (apply_button, revert, note) in self._actions.items():
            dirty = self._dirty(section)
            apply_button.set_sensitive(dirty and not self._busy)
            revert.set_sensitive(dirty and not self._busy)
            note.set_text("Not applied yet" if dirty else "")
        self._sync_home()

    def _set_busy(self, busy):
        self._busy = busy
        self._refresh_actions()

    def _save(self, section):
        """Save one section, leaving the other's unapplied edits on the floor."""
        self._disk[section] = copy.deepcopy(self.config[section])
        cfg.save(self._disk)
        self._seen_mtime = cfg.mtime()

    def load_widgets(self):
        """Prime every widget from the working copy."""
        # Stored colours may be short or missing. Fill them in on both copies
        # first, so that doing so is not mistaken for an edit.
        for conf in (self.config, self._disk):
            settings = conf.setdefault("aura", {})
            settings["colors"] = aura_mod.normalise_colors(settings, cfg.ZONES)
            settings.pop("color", None)

        self._loading = True
        try:
            if self.fans.available:
                self.fan_mode.set_active(
                    "curve" if self._fan_settings().get("mode") == "curve"
                    else "auto")
            settings = self._aura_settings()
            wanted = settings.get("mode", "Static")
            self.effect_choice.set_active(
                wanted if wanted in self._modes else self._modes[0])
            self.speed_scale.set_value(settings.get("speed", 50))
            if self.battery.available:
                self._load_charge()
        finally:
            self._loading = False
        self._sync_fan_page()
        self._sync_keyboard_page()
        self._refresh_actions()

    def _load_charge(self):
        battery = self.config.get("battery") or {}
        stored = int(battery.get("limit", 80))
        values = sorted(set(CHARGE_CHOICES)
                        | ({stored} if 20 <= stored < 100 else set()))
        self.charge_choice.set_options(
            [(value, "100% (no limit)" if value == 100 else f"{value}%")
             for value in values])
        self.charge_choice.set_active(stored if battery.get("enabled") else 100)

    def _tick(self):
        """Refresh the live readings. Returns False once the window is gone."""
        if not self._alive:
            return False
        # The tray and the CLI write the same file. Follow them, unless that
        # would throw away something being edited here.
        if cfg.mtime() != self._seen_mtime and not self._busy:
            self._seen_mtime = cfg.mtime()
            if not self.dirty:
                self.config = cfg.load()
                self._disk = copy.deepcopy(self.config)
                self.load_widgets()

        temp, rpm = self.sensors.cpu_temp(), self.sensors.rpm()
        self.tile_temp.show_reading(
            f"{temp}°C" if temp is not None else "–",
            f"{self.platform.profile} mode" if self.platform.available else "")
        if self.platform.available and not self._loading:
            # Always show the mode the machine is actually in, even if
            # something else changed it.
            self.power_choice.set_active(self.platform.profile)
        if self.fans.available:
            armed = self.fans.armed
            for fan, tile in self.tile_fans.items():
                speed = rpm.get(fan)
                tile.show_reading(
                    f"{speed} rpm" if speed is not None else "–",
                    "your curve" if armed.get(fan) else "firmware curve")
            self.plot.show_readings(temp, rpm)
        # Brightness is the desktop's to change, so it is read every tick
        # rather than only when something here is edited.
        self._sync_home()
        note = self._aura_note
        if self.kbd.available:
            note = (f"Brightness is {self.kbd.brightness} of "
                    f"{self.kbd.max_brightness}, set with the keyboard's own "
                    "keys or the power applet. " + note)
        self.kbd_note.set_text(note)
        return True

    # ------------------------------------------------------------ home page

    def _sync_home(self):
        self.home_fans.set_text(
            "Your curve" if self._fan_settings().get("mode") == "curve"
            else "Firmware decides")
        settings = self._aura_settings()
        mode = settings.get("mode", "Static")
        self.home_kbd.set_text(mode)
        self.home_strip.paint(aura_mod.normalise_colors(settings, cfg.ZONES),
                              self._strip_mode(mode))
        self.tile_kbd.show_reading(
            mode,
            f"brightness {self.kbd.brightness} of {self.kbd.max_brightness}"
            if self.kbd.available else "")

    def on_power_picked(self, choice):
        """Set the power mode now, then put the fan setting back.

        The firmware reloads its own curve when the mode changes, so a custom
        curve has to be re-armed afterwards or it silently stops applying.
        """
        if self._loading:
            return
        for conf in (self.config, self._disk):
            conf["power"] = choice
        # Save the on-disk copy, not the working one: an unapplied curve or
        # colour must not ride along with a power mode change.
        cfg.save(self._disk)
        self._seen_mtime = cfg.mtime()
        try:
            self.platform.profile = choice
        except (OSError, ValueError) as exc:
            self.set_status(f"Could not set the power mode: {exc}")
            return
        results = apply_mod.rearm_fans(self._disk)
        failures = [detail for _knob, state, detail in results if state == "failed"]
        text = f"Power mode {choice}."
        if failures:
            text += " The fans could not be set back: " + "; ".join(failures)
        elif (self._disk.get("fans") or {}).get("mode") == "curve":
            text += " Your fan curve was armed again."
        self.set_status(text)

    def on_charge_picked(self, value):
        """The charge limit is machine-wide, so it is written straight away."""
        if self._loading:
            return
        for conf in (self.config, self._disk):
            battery = conf.setdefault("battery", {})
            battery["enabled"] = value < 100
            if value < 100:
                battery["limit"] = value
        cfg.save(self._disk)
        self._seen_mtime = cfg.mtime()
        if not self.battery.writable:
            self.set_status("Saved, but the charge limit is read-only.")
            return
        try:
            self.battery.limit = value
            self.set_status("Charge limit off." if value == 100
                            else f"Charging now stops at {value}%.")
        except (OSError, ValueError) as exc:
            self.set_status(f"Could not set the charge limit: {exc}")

    # ------------------------------------------------------------- fan page

    def _sync_fan_page(self):
        if not self.fans.available:
            return
        custom = self._fan_settings().get("mode") == "curve"
        editable = custom and self.fans.writable
        curves = self._curves()
        caption = ""
        if not custom:
            # Show the curve that is really in charge when we can: with no
            # custom curve armed, the readback is the firmware's own.
            if not any(self.fans.armed.values()):
                curves = self.fans.curves()
                caption = "the firmware's curve, read from the machine"
            else:
                caption = "your curve, which the fans are not using"
        self.plot.show_curves(curves, self.fan, editable, caption)
        for widget in (self.fan_pick, self.fan_pick_label, self.fan_tools):
            widget.set_visible(custom)
        self.numbers.set_visible(custom and self.numbers_toggle.get_active())
        self.fan_hint.set_text(
            f"Drag the {FAN_TITLES[self.fan]}'s points. The ringed dot is where "
            "each fan sits on its curve right now. The last point stays at "
            "100% so the fans can always reach full speed."
            if custom else
            "The firmware runs the fans. Pick “My curve” to shape "
            "them yourself.")
        self._fill_numbers()

    def _fill_numbers(self):
        points = self._curves().get(self.fan, [])
        for (temp_entry, pct_entry), (temp, percent) in zip(self.point_entries, points):
            temp_entry.set_text(str(temp))
            pct_entry.set_text(str(percent))

    def _set_point(self, fan, index, temp, percent):
        settings = self._fan_settings()
        points = cfg.points(settings.get(fan) or [])
        if index >= len(points):
            return
        points[index] = clamp_point(points, index, temp, percent)
        settings[fan] = [list(point) for point in points]
        self._sync_fan_page()
        self._refresh_actions()

    def on_point_moved(self, fan, index, temp, percent):
        self._set_point(fan, index, temp, percent)

    def on_point_typed(self, entry, index, kind):
        points = self._curves().get(self.fan, [])
        if index >= len(points):
            return
        try:
            value = int(entry.get_text())
        except ValueError:
            self._fill_numbers()
            return
        temp, percent = points[index]
        if value == (temp, percent)[kind]:
            return
        self._set_point(self.fan, index,
                        value if kind == 0 else temp,
                        value if kind == 1 else percent)

    def on_point_left(self, entry, _event, index, kind):
        self.on_point_typed(entry, index, kind)
        return False

    def on_fan_mode_picked(self, mode):
        if self._loading:
            return
        self._fan_settings()["mode"] = mode
        self._sync_fan_page()
        self._refresh_actions()

    def on_fan_picked(self, fan):
        self.fan = fan
        self._sync_fan_page()

    def on_numbers_toggled(self, _button):
        self._sync_fan_page()

    def on_load_stock(self, _button):
        """Seed both curves with the firmware's own for the mode in force."""
        try:
            curves = self.fans.factory_curves()
        except (OSError, ValueError) as exc:
            self.set_status(f"Could not read the firmware curve: {exc}")
            return
        settings = self._fan_settings()
        raised = False
        for fan, points in curves.items():
            fixed = hw.with_full_ceiling(points)
            raised = raised or fixed != points
            settings[fan] = [list(point) for point in fixed]
        running = self.platform.profile if self.platform.available else "current"
        text = (f"Loaded the firmware's {running} curve. "
                "Apply it to the fans to use it.")
        if raised:
            text += (" It stops short of full speed, so the last point was "
                     "raised to 100%.")
        self.set_status(text)
        self._sync_fan_page()
        self._refresh_actions()

    def on_apply_fans(self, _button):
        settings = self._fan_settings()
        if settings.get("mode") == "curve":
            for fan, points in self._curves().items():
                try:
                    hw.FanCurve.validate(points)
                except ValueError as exc:
                    self.set_status(
                        f"The {FAN_TITLES[fan]} curve cannot be used: {exc}")
                    return
        self._save("fans")
        self._run_section("fans", apply_mod.apply_fans, "the fans")

    # -------------------------------------------------------- keyboard page

    def _aura_probed(self, _devices, error):
        """OpenRGB has listed its devices (or is absent): finish the page."""
        self._aura_ready = True
        if not self.aura.available:
            self._aura_note = ("Colour needs OpenRGB, which was not found: run "
                               "get-openrgb.sh from the strixctl checkout.")
        elif error is not None:
            self._aura_note = f"OpenRGB failed: {error}"
        elif self.aura.find_keyboard() is None:
            self._aura_note = self.aura.why_unavailable()
        else:
            self._aura_note = "Colour changes take a couple of seconds to land."

        modes = (self.aura.modes if self.aura.available else None) or ["Static"]
        if modes != self._modes:
            self._modes = modes
            self.effect_choice.set_options([(mode, mode) for mode in modes])
            wanted = self._aura_settings().get("mode", "Static")
            self.effect_choice.set_active(wanted if wanted in modes else modes[0])
        self._sync_keyboard_page()
        self._refresh_actions()
        self._tick()

    @staticmethod
    def _strip_mode(mode):
        if not aura_mod.mode_takes_color(mode):
            return KeyboardStrip.CYCLE
        if aura_mod.MODE_ROLES.get(mode.strip().lower()) == aura_mod.PERKEY:
            return KeyboardStrip.SINGLE
        return KeyboardStrip.ZONED

    def _sync_keyboard_page(self):
        """Show only the controls the chosen effect listens to."""
        settings = self._aura_settings()
        mode = settings.get("mode", "Static")
        how = self._strip_mode(mode)
        # Until the probe has answered, asking would block on OpenRGB.
        usable = self.aura.available and (
            not self._aura_ready or self.aura.device_for(mode) is not None)
        if how != KeyboardStrip.ZONED:
            self.zone = 0
        colours = aura_mod.normalise_colors(settings, cfg.ZONES)
        self.strip.paint(colours, how, self.zone)
        self.strip.set_sensitive(usable)
        self.colour_editor.set_visible(how != KeyboardStrip.CYCLE)
        self.colour_editor.set_sensitive(usable)
        self.match_button.set_visible(how == KeyboardStrip.ZONED)
        self.speed_row.set_visible(aura_mod.mode_takes_speed(mode))
        self.speed_row.set_sensitive(usable)

        previous, self._loading = self._loading, True
        try:
            self.colour_editor.set_rgb(colours[self.zone])
        finally:
            self._loading = previous

        if not usable:
            self.zone_hint.set_text(
                "" if not self.aura.available
                else f"OpenRGB does not offer {mode} on this keyboard.")
        elif how == KeyboardStrip.CYCLE:
            self.zone_hint.set_text(f"{mode} runs through its own colours.")
        elif how == KeyboardStrip.SINGLE:
            self.zone_hint.set_text(f"{mode} uses one colour for the whole keyboard.")
        else:
            self.zone_hint.set_text(
                f"Zone {self.zone + 1} of {cfg.ZONES} is selected. Click "
                "another zone on the strip to colour it.")

    def on_effect_picked(self, mode):
        if self._loading:
            return
        self._aura_settings()["mode"] = mode
        self._sync_keyboard_page()
        self._refresh_actions()

    def on_zone_picked(self, zone):
        self.zone = zone
        self._sync_keyboard_page()

    def on_editor_colour(self, editor):
        if self._loading:
            return
        settings = self._aura_settings()
        colours = aura_mod.normalise_colors(settings, cfg.ZONES)
        colours[self.zone] = list(editor.rgb)
        settings["colors"] = colours
        self.strip.paint(colours, self._strip_mode(settings.get("mode", "Static")),
                         self.zone)
        self._refresh_actions()

    def on_speed_changed(self, scale):
        if self._loading:
            return
        self._aura_settings()["speed"] = int(scale.get_value())
        self._refresh_actions()

    def on_match_zones(self, _button):
        settings = self._aura_settings()
        colours = aura_mod.normalise_colors(settings, cfg.ZONES)
        settings["colors"] = [list(colours[self.zone]) for _ in colours]
        self._sync_keyboard_page()
        self._refresh_actions()

    def on_apply_aura(self, _button):
        self._save("aura")
        self._run_section("aura", apply_mod.apply_aura, "the keyboard")

    # ------------------------------------------------------- apply / revert

    def _run_section(self, section, work, what):
        """Apply one section off the main loop, from the saved copy."""
        conf = copy.deepcopy(self._disk)

        def run():
            results = []
            work(conf, results)
            return results

        def done(results, error):
            self._set_busy(False)
            if error is not None:
                self.set_status(f"Could not set {what}: {error}")
                return
            failures = [f"{knob}: {detail}"
                        for knob, state, detail in results if state == "failed"]
            if failures:
                self.set_status("Problems: " + "; ".join(failures))
            else:
                self.set_status(f"Applied to {what}.")
            if section == "fans":
                self._sync_fan_page()
            self._tick()

        self._set_busy(True)
        self.set_status(f"Applying to {what}…")
        self._in_background(run, done)

    def on_revert(self, _button, section):
        self.config[section] = copy.deepcopy(self._disk[section])
        self.load_widgets()
        self.set_status("Changes dropped.")

    def set_status(self, text):
        self.status.set_text(text)


def main():
    window = StrixWindow()
    window.connect("destroy", Gtk.main_quit)
    window.present_all()
    Gtk.main()
    return 0
