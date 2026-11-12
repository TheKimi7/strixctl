"""A thin monochrome layer over whatever GTK theme the desktop is using.

Why this exists
---------------
The window should look like the rest of the desktop, so it brings almost no
look of its own. It takes the theme's own widgets, its own font, its own
light or dark colours, and changes two things:

  * **Sizes and weights** for the handful of `strix-*` classes the window
    uses, so a reading looks like a reading and a footnote like a footnote.
  * **The accent.** A desktop accent is usually a strong blue or green, and
    it would land on the Apply button, the chosen option in a row, the
    slider's filled half and the focus ring. Those are states, not data, so
    they are pulled back to a grey mixed from the theme's own foreground and
    background. The only colour left in the window is the keyboard's real
    zone colours, and whatever the theme uses to say something failed.

Everything is derived from `@theme_fg_color` and `@theme_bg_color` with
mix() and alpha(), and there is not one hex value in the stylesheet, which is
what keeps it right on a light theme and on a dark one.

`"theme": "system"` in config.json drops the accent rules too, leaving the
sizes, in case you would rather have your desktop's accent back.
"""

#: Shared by both stylesheets: what the window needs that no GTK theme can
#: know, which is only ever a size, a weight or a little spacing.
_SIZES = """
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

#: The accent, pulled back to grey. A GTK theme hardcodes its accent inside
#: its own widget rules as often as it uses the named colours, so the named
#: colours are redefined *and* the handful of widgets this window actually
#: shows are named outright.
_MONO = """
@define-color theme_selected_bg_color mix(@theme_bg_color, @theme_fg_color, 0.62);
@define-color theme_selected_fg_color @theme_bg_color;
@define-color accent_bg_color mix(@theme_bg_color, @theme_fg_color, 0.62);
@define-color accent_fg_color @theme_bg_color;
@define-color accent_color mix(@theme_bg_color, @theme_fg_color, 0.62);

selection, :selected {
    background-color: mix(@theme_bg_color, @theme_fg_color, 0.62);
    color: @theme_bg_color;
}

/* Apply, and the chosen option of a row. */
button.suggested-action, button:checked, button:active {
    background-image: none;
    background-color: mix(@theme_bg_color, @theme_fg_color, 0.62);
    color: @theme_bg_color;
    border-color: mix(@theme_bg_color, @theme_fg_color, 0.45);
}
button.suggested-action:hover, button:checked:hover {
    background-color: mix(@theme_bg_color, @theme_fg_color, 0.72);
}
button.suggested-action:disabled, button:checked:disabled {
    background-image: none;
    background-color: alpha(@theme_fg_color, 0.14);
    color: alpha(@theme_fg_color, 0.4);
}

/* The speed slider: the filled half and the handle. */
scale highlight, scale fill, progressbar progress {
    background-image: none;
    background-color: mix(@theme_bg_color, @theme_fg_color, 0.62);
}
scale slider {
    background-image: none;
    background-color: mix(@theme_bg_color, @theme_fg_color, 0.78);
    border-color: alpha(@theme_fg_color, 0.3);
}

/* The point grid, and the R/G/B and hex fields. */
entry:focus, spinbutton:focus, entry:focus-within {
    border-color: alpha(@theme_fg_color, 0.55);
    box-shadow: none;
}
*:focus-visible, *:focus {
    outline-color: alpha(@theme_fg_color, 0.45);
}

/* A checkbox or radio that still draws itself in the accent. */
check:checked, radio:checked {
    background-image: none;
    background-color: mix(@theme_bg_color, @theme_fg_color, 0.62);
    color: @theme_bg_color;
    border-color: mix(@theme_bg_color, @theme_fg_color, 0.45);
}
"""

#: The fan curves share one colour, so the dash pattern is what tells them
#: apart. Kept here so the graph and its legend cannot disagree.
CURVE_DASH = {"cpu": [], "gpu": [5, 4]}


def wanted(config):
    """Is the accent pulled back to grey? `"theme": "system"` leaves it alone."""
    return (config.get("theme") or "monochrome").lower() != "system"


def stylesheet(config=None):
    """The whole stylesheet: sizes always, the monochrome rules unless off."""
    css = _SIZES
    if config is None or wanted(config):
        css += _MONO
    return css.encode()


def apply(config):
    """Put the stylesheet on the screen. Returns True if the greys went on.

    Called before any widget is built. A failure here is cosmetic, so it is
    reported by returning False rather than by raising into the UI.
    """
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gdk, Gtk
        provider = Gtk.CssProvider()
        provider.load_from_data(stylesheet(config))
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    except Exception:
        return False
    return wanted(config)
