"""The NeoPOP skin: black, white, and plates you can see standing up.

Why this exists
---------------
The window used to borrow the desktop theme, which made it look like every
other GTK dialog. This gives it a face of its own, built on three ideas taken
from CRED's neopop-web, whose elevated button it follows closely:

  * **Plates, not panels.** Every control is a solid rectangle. No rounded
    corners, no gradients, no soft shadows.
  * **Real extrusion, not a shadow.** A plate's depth is two side planes
    sheared at 45 degrees: a lit one down its right edge and a dark one along
    its bottom. They are drawn in cairo rather than in CSS, because GTK3 has
    neither pseudo-elements nor transforms. Pressing a plate drops it the
    three pixels into the space the planes occupied, and the planes go, so the
    plate reads as flush with the page. The allocation never changes, so
    nothing around it moves.
  * **Black and white.** Ink and paper, and the greys the reference uses
    between them. Colour in this window means data: the keyboard's real zone
    colours, and red when something failed. The two fan curves are told apart
    by line style rather than by hue.

Geometry, taken from @cred/neopop-web's `Button` with `variant="primary"`,
`kind="elevated"`, `colorMode="dark"`: a 3px edge at 45 degrees, the face in
#0d0d0d with white text and no border, the right plane #8A8A8A and the bottom
plane #3D3D3D. `size="big"` is a 50px face with 30px of side padding, and its
label is 14px at weight 700 with 0.2px of letter-spacing.

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

#: Ink, paper, and the greys the reference uses between them.
INK = {
    "page":  "#000000",   # behind everything
    "panel": "#0d0d0d",   # a card's face
    "face":  "#0d0d0d",   # a plate's face
    "rule":  "#3d3d3d",   # hairlines, and anything switched off
    "dim":   "#8a8a8a",   # secondary text
    "paper": "#ffffff",   # primary text, and a pressed plate's face
    "alarm": "#c2453a",   # failures only
}

#: How far a plate stands above the page, in pixels. The angle is fixed at 45
#: degrees by the shear, so the width is the only number worth naming.
EDGE = 3

#: The two side planes. Light down the right edge, dark along the bottom.
POP = {"right": "#8a8a8a", "bottom": "#3d3d3d"}

#: A plate's face, by state. These are here rather than in the stylesheet
#: because gui.plate() fills the face itself: GTK3 clips a widget's drawing
#: to its CSS margin box, so a plate drawn from CSS has nowhere to put the
#: planes. The face is painted before GTK draws the label over it.
PLATE = {
    "rest": INK["face"],     # a plate standing up
    "down": INK["paper"],    # held, chosen, or the one plate that acts
    "off":  INK["panel"],    # insensitive: flush, and nothing to press
    "card": INK["panel"],    # a tile, which never presses
}


def triple(value):
    """A "#rrggbb" string as the 0-1 triple cairo wants."""
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


#: The same planes and faces, ready for cairo.
POP_FILL = {side: triple(colour) for side, colour in POP.items()}
PLATE_FILL = {state: triple(colour) for state, colour in PLATE.items()}

#: What the cairo-drawn widgets paint with, as 0-1 triples.
PLOT = {
    "line": triple(INK["paper"]),
    "dim": triple(INK["dim"]),
    "alarm": triple(INK["alarm"]),
}

#: The fan curves are monochrome, so the dash pattern is what tells them
#: apart. Kept here so the graph and its legend cannot disagree.
CURVE_DASH = {"cpu": [], "gpu": [5, 4]}

_CSS = """
@define-color strix_page %(page)s;
@define-color strix_panel %(panel)s;
@define-color strix_face %(face)s;
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
.strix-dim { color: @strix_dim; opacity: 1; }
.strix-alarm { color: %(alarm)s; }

.strix-title {
    font-family: %(title_css)s;
    font-weight: 700;
    font-size: 152%%;
    letter-spacing: 1px;
    color: @strix_paper;
}
/* opacity: 1 throughout, because the desktop-theme stylesheet underneath
   fades these to 0.6 and the palette should be what is on the screen. */
.strix-subtitle { font-family: %(heavy_css)s; font-weight: 800; font-size: 78%%;
                  letter-spacing: 2px; color: @strix_dim; opacity: 1; }
.strix-heading { font-family: %(heavy_css)s; font-weight: 800; font-size: 92%%;
                 letter-spacing: 1px; color: @strix_paper; }
.strix-tile-title { font-family: %(heavy_css)s; font-weight: 800; font-size: 76%%;
                    letter-spacing: 2px; color: @strix_dim; opacity: 1; }
.strix-tile-value { font-family: %(heavy_css)s; font-weight: 800;
                    font-size: 162%%; color: @strix_paper; }
.strix-tile-sub { font-size: 82%%; color: @strix_dim; opacity: 1; }

/* A card is a plate too: the same face and the same two planes, drawn by
   gui.plate(). The extra right and bottom padding is the room the planes
   stand in, so nothing inside the card is drawn under them. */
.strix-tile {
    background-color: transparent;
    /* The stylesheet underneath gives a card a hairline, which GTK would
       draw after the face and so across the planes. */
    border: none;
    padding: 7px %(tile_x)spx %(tile_y)spx 10px;
}

/* Plates. The face and its two planes are filled in by gui.plate(); what is
   left to CSS is the type, the label colour, and the %(edge)spx the plate
   moves when it is pressed, which is done by swapping the padding rather
   than by moving anything, so the allocation never changes and nothing
   around the plate reflows.

   Scoped to .strix-pop throughout: nothing else GTK draws as a button, now
   or later, should end up with a label sitting %(edge)spx off centre. */
button.strix-pop {
    font-family: %(heavy_css)s;
    font-weight: 700;
    font-size: 14px;
    letter-spacing: 0.2px;
    background-image: none;
    background-color: transparent;
    color: @strix_paper;
    border: none;
    box-shadow: none;
    margin: 0;
    min-height: 34px;
    padding: 0 19px %(edge)spx 16px;
    transition: none;
}
/* The reference's size="big": a 50px face with 30px either side. */
button.strix-pop.strix-big { min-height: 50px; padding: 0 33px %(edge)spx 30px; }

button.strix-pop:active, button.strix-pop:checked,
button.strix-pop:disabled {
    padding: %(edge)spx 16px 0 19px;
}
button.strix-pop.strix-big:active, button.strix-pop.strix-big:checked,
button.strix-pop.strix-big:disabled {
    padding: %(edge)spx 30px 0 33px;
}

/* Held is not chosen: holding a plate moves it and nothing else, so only a
   chosen plate and the one plate that acts turn their face over. */
button.strix-pop:checked label,
button.strix-pop.suggested-action label { color: @strix_page; }
button.strix-pop:disabled label,
button.strix-pop.suggested-action:disabled label { color: @strix_rule; }

/* The page switcher is the same plate, a little wider. Both halves need the
   stackswitcher in front of them: without it the rule above, with its two
   classes, outweighs this one and a held tab would change width. */
stackswitcher button.strix-pop { padding: 0 23px %(edge)spx 20px; }
stackswitcher button.strix-pop:active,
stackswitcher button.strix-pop:checked,
stackswitcher button.strix-pop:disabled {
    padding: %(edge)spx 20px 0 23px;
}

/* Anything that is a button to GTK but not a plate to us: flat, and at least
   the right colours. */
button { background-image: none; background-color: @strix_face;
         color: @strix_paper; border: none; box-shadow: none; }

/* Inputs are flat. A field is not a thing that stands up. */
.strix-point entry {
    font-family: %(heavy_css)s;
    font-weight: 800;
    background-color: @strix_panel;
    color: @strix_paper;
    border: 2px solid @strix_rule;
    box-shadow: none;
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

/* The R/G/B steppers. Not plates: they are part of the field they step. */
spinbutton { background-color: @strix_panel; color: @strix_paper; }
spinbutton button {
    margin: 0;
    padding: 2px 8px;
    min-height: 0;
    min-width: 0;
    border-width: 0 0 0 2px;
    border-style: solid;
    border-color: @strix_rule;
}

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
    box-shadow: none;
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
    values["edge"] = EDGE
    # A card's own padding, plus the room its planes stand in, so that what
    # you see inside a card is inset by the same amount all the way round.
    values["tile_x"] = 10 + EDGE
    values["tile_y"] = 7 + EDGE
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
