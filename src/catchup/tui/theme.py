"""The dark palette of the TUI, shared by the Textual theme and the rows rendered in Python.

The screen keeps the terminal's own background, so catchup blends in with the tabs around it.
"""

from textual.theme import Theme

# Dark text on bright chips, such as the selected tab and the primary button.
INK = "#111318"
PANEL = "#161920"
CHROME = "#0c0d11"
LINE = "#3b404b"
FOREGROUND = "#d7dae1"
BRIGHT = "#f1f3f7"
DIM = "#7d8492"
FAINT = "#4c525e"
FEED = "#959ba7"
NOTE = "#b7bcc6"
SELECTED = "#363c48"
HOVER = "#2e333d"
BUTTON = "#30353f"
ACCENT = "#e9b65c"
URGENT = "#f0806e"
OK = "#7cc79a"

LINK_COLORS = {
    "MR": "#f0a35e",
    "Slack": "#b99cf0",
    "Email": "#79b4f2",
    "Ticket": "#66cdc0",
    "Doc": "#e59ac4",
    "Artifact": "#9bd17a",
    "Web": "#9aa1ad",
}

THEME = Theme(
    name="catchup",
    primary=ACCENT,
    accent=ACCENT,
    warning=ACCENT,
    error=URGENT,
    success=OK,
    foreground=FOREGROUND,
    background="ansi_default",
    surface="ansi_default",
    panel=PANEL,
    dark=True,
    ansi=True,
    text_alpha=1.0,
    variables={
        "ink": INK,
        # Textual's ANSI-mode widget styles use these for hover and pressed states.
        "ansi-background": SELECTED,
        "ansi-foreground": FOREGROUND,
        "chrome": CHROME,
        "line": LINE,
        "bright": BRIGHT,
        "dim": DIM,
        "faint": FAINT,
        "selected": SELECTED,
        "hover": HOVER,
        "button": BUTTON,
    },
)
