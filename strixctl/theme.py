"""The NeoPOP skin: flat plates, hard rules, and a press you can feel.

Why this exists
---------------
The window used to borrow the desktop theme, which made it look like every
other GTK dialog. This gives it a face of its own, built on three ideas
borrowed from NeoPOP:

  * **Plates, not panels.** Every control is a solid rectangle with a hard
    rule around it. No rounded corners, no gradients, no soft shadows.
  * **Depth from offset, not blur.** A plate casts a stack of 1px hard
    shadows down and to the right, so it reads as a slab standing above the
    page. Pressing it swaps the margins: the plate slides into its own
    shadow and the allocation never changes, so nothing around it jumps.
  * **Monochrome.** Ink and paper only. Colour in this window means data:
    the keyboard's real zone colours, and red when something failed. The two
    fan curves are told apart by line style rather than by hue.

The palette is defined here rather than taken from anywhere else, and the
greys are warm on purpose so they do not land on the usual framework values.

Fonts
-----
Gilroy and Cirka are not redistributable, so no font ships with strixctl.
Drop the files into ~/.local/share/strixctl/fonts and they are picked up at
start-up through fontconfig's app-font list, which keeps them private to this
process: nothing is installed system-wide. Without them the skin falls back
to the desktop's own font and still works.

Everything here is optional. `config.json` with `"theme": "system"` turns the
whole module off and the window goes back to the desktop theme.
"""

import ctypes
import ctypes.util
import os

#: Where to drop Gilroy and Cirka. Not shipped: see the module docstring.
FONT_DIR = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
    "strixctl", "fonts")

#: Families the skin asks for, in the order it would like them.
HEAVY_FAMILIES = ("Gilroy ExtraBold", "Gilroy")
TITLE_FAMILIES = ("Cirka", "Cirka Variable")

#: Resolved at load time; "" means "whatever the desktop uses".
HEAVY = ""
TITLE = ""

#: Ink and paper. Warm blacks, so they are nobody's default grey.
INK = {
    "page":  "#0c0b09",   # behind everything
    "panel": "#151310",   # a card's face
    "face":  "#211d17",   # a plate's face
    "edge":  "#050504",   # the shadow a plate casts
    "rule":  "#3b362b",   # hairlines and plate borders
    "dim":   "#8f887a",   # secondary text
    "paper": "#f2eee4",   # primary text, and a lit plate's face
    "alarm": "#c2453a",   # failures only
}

#: What the cairo-drawn widgets paint with, as 0-1 triples.
PLOT = {
    "line": (0.949, 0.933, 0.894),   # paper
    "dim": (0.561, 0.533, 0.478),
    "alarm": (0.761, 0.271, 0.227),
}

#: The fan curves are monochrome, so the dash pattern is what tells them
#: apart. Kept here so the graph and its legend cannot disagree.
CURVE_DASH = {"cpu": [], "gpu": [5, 4]}

_CSS = """
@define-color strix_page %(page)s;
@define-color strix_panel %(panel)s;
@define-color strix_face %(face)s;
@define-color strix_edge %(edge)s;
@define-color strix_rule %(rule)s;
@define-color strix_dim %(dim)s;
@define-color strix_paper %(paper)s;

/* The desktop's accent would otherwise show through on selections,
   check marks and the slider's filled half. */
@define-color theme_selected_bg_color @strix_paper;
@define-color theme_selected_fg_color @strix_page;

window.background, .strix-root { background-color: @strix_page; color: @strix_paper; }

* {
    border-radius: 0;
    outline-style: none;
    text-shadow: none;
    -gtk-icon-shadow: none;
}

label { color: @strix_paper; }
button:active label, button:checked label,
button.suggested-action label { color: @strix_page; }
button:disabled label, button.suggested-action:disabled label {
    color: @strix_rule; }
.strix-dim { color: @strix_dim; opacity: 1; }
.strix-alarm { color: %(alarm)s; }

.strix-title {
    font-family: %(title_css)s;
    font-weight: 700;
    font-size: 152%%;
    letter-spacing: 1px;
    color: @strix_paper;
}
.strix-subtitle { font-family: %(heavy_css)s; font-weight: 800; font-size: 78%%;
                  letter-spacing: 2px; color: @strix_dim; }
.strix-heading { font-family: %(heavy_css)s; font-weight: 800; font-size: 92%%;
                 letter-spacing: 1px; color: @strix_paper; }
.strix-tile-title { font-family: %(heavy_css)s; font-weight: 800; font-size: 76%%;
                    letter-spacing: 2px; color: @strix_dim; }
.strix-tile-value { font-family: %(heavy_css)s; font-weight: 800;
                    font-size: 162%%; color: @strix_paper; }
.strix-tile-sub { font-size: 82%%; color: @strix_dim; }

/* A card: a flat plate with a hard rule and an offset slab under it. */
.strix-tile {
    background-color: @strix_panel;
    border: 2px solid @strix_rule;
    padding: 7px 10px;
    margin: 0 5px 5px 0;
    box-shadow: 1px 1px 0 @strix_edge, 2px 2px 0 @strix_edge,
                3px 3px 0 @strix_edge, 4px 4px 0 @strix_edge,
                5px 5px 0 @strix_edge;
}

/* Buttons are plates. At rest they stand 4px above the page; pressed, they
   sit down into their own shadow. The margins swap, so the space the button
   occupies never changes and nothing reflows. */
button, button.toggle, button.radio {
    font-family: %(heavy_css)s;
    font-weight: 800;
    font-size: 92%%;
    letter-spacing: 1px;
    background-image: none;
    background-color: @strix_face;
    color: @strix_paper;
    border: 2px solid @strix_rule;
    padding: 6px 14px;
    margin: 0 4px 4px 0;
    box-shadow: 1px 1px 0 @strix_edge, 2px 2px 0 @strix_edge,
                3px 3px 0 @strix_edge, 4px 4px 0 @strix_edge;
    transition: none;
}
button:hover { background-color: @strix_rule; }
button:active, button:checked {
    background-color: @strix_paper;
    color: @strix_page;
    border-color: @strix_paper;
    box-shadow: none;
    margin: 4px 0 0 4px;
}
button:disabled {
    background-color: @strix_panel;
    color: @strix_rule;
    border-color: @strix_rule;
    box-shadow: 1px 1px 0 @strix_edge, 2px 2px 0 @strix_edge;
    margin: 2px 2px 2px 2px;
}
button.suggested-action { background-color: @strix_paper; color: @strix_page;
                          border-color: @strix_paper; }
button.suggested-action:hover { background-color: #ffffff; }
button.suggested-action:disabled { background-color: @strix_panel;
                                   color: @strix_rule; border-color: @strix_rule; }

/* The page switcher reuses the same plates. */
stackswitcher button { padding: 7px 18px; }

.strix-point entry {
    font-family: %(heavy_css)s;
    font-weight: 800;
    background-color: @strix_panel;
    color: @strix_paper;
    border: 2px solid @strix_rule;
    box-shadow: 1px 1px 0 @strix_edge, 2px 2px 0 @strix_edge;
    margin: 0 2px 2px 0;
    padding: 3px 4px;
    min-width: 0;
}
.strix-point entry:focus { border-color: @strix_paper; }
.strix-point entry:disabled { color: @strix_rule; }

entry {
    background-color: @strix_panel;
    color: @strix_paper;
    border: 2px solid @strix_rule;
    box-shadow: none;
    padding: 5px 7px;
}
entry:focus { border-color: @strix_paper; }

spinbutton { background-color: @strix_panel; color: @strix_paper; }
spinbutton button { margin: 0; box-shadow: none; border-width: 0 0 0 2px;
                    padding: 2px 8px; }

/* The slider is a plate on a rail, not a pill on a groove. */
scale trough {
    background-color: @strix_panel;
    border: 2px solid @strix_rule;
    min-height: 10px;
}
scale highlight { background-color: @strix_paper; }
scale slider {
    background-color: @strix_paper;
    border: 2px solid @strix_page;
    box-shadow: 1px 1px 0 @strix_edge, 2px 2px 0 @strix_edge;
    min-width: 16px;
    min-height: 16px;
    margin: -8px;
}
scale value, scale marks label { color: @strix_dim; font-size: 80%%; }

separator { background-color: @strix_rule; min-height: 2px; min-width: 2px; }

infobar, infobar.warning { background-color: %(alarm)s; }
infobar label { color: @strix_paper; }

tooltip { background-color: @strix_page; border: 2px solid @strix_paper; }
tooltip label { color: @strix_paper; }
"""


def _families():
    """Font families fontconfig can resolve, after the app fonts are added."""
    try:
        import gi
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import PangoCairo
        return {family.get_name()
                for family in PangoCairo.FontMap.get_default().list_families()}
    except Exception:
        return set()


def load_fonts(directory=FONT_DIR):
    """Add FONT_DIR to this process's font list. Safe to call more than once.

    fontconfig keeps app fonts per process, so nothing here touches the
    system's font configuration. It has to run before anything draws, because
    Pango caches the font map on first use.
    """
    global HEAVY, TITLE
    if os.path.isdir(directory):
        try:
            name = ctypes.util.find_library("fontconfig")
            if name:
                fontconfig = ctypes.CDLL(name)
                fontconfig.FcConfigAppFontAddDir.argtypes = [
                    ctypes.c_void_p, ctypes.c_char_p]
                fontconfig.FcConfigAppFontAddDir(None, directory.encode())
        except (OSError, AttributeError):
            pass    # no fontconfig, or a build without it: fall back below
    have = _families()
    HEAVY = next((f for f in HEAVY_FAMILIES if f in have), "")
    TITLE = next((f for f in TITLE_FAMILIES if f in have), "") or HEAVY
    return bool(HEAVY)


def stylesheet():
    values = dict(INK)
    # Quoted family names, or "inherit" so the desktop's font survives.
    values["heavy_css"] = f'"{HEAVY}"' if HEAVY else "inherit"
    values["title_css"] = f'"{TITLE}"' if TITLE else "inherit"
    return (_CSS % values).encode()


def wanted(config):
    """Is the skin switched on? `"theme": "system"` in config.json turns it off."""
    return (config.get("theme") or "neopop").lower() != "system"


def apply(config):
    """Load the fonts and put the stylesheet on the screen. Returns True if on.

    Called before any widget is built. A failure here is cosmetic, so it is
    reported by returning False rather than by raising into the UI.
    """
    if not wanted(config):
        return False
    load_fonts()
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gdk, Gtk
        provider = Gtk.CssProvider()
        provider.load_from_data(stylesheet())
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    except Exception:
        return False
    return True
