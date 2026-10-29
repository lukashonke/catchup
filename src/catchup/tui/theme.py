"""The dark palette of the TUI, shared by the Textual theme and the rows rendered in Python."""

from textual.theme import Theme

BACKGROUND = "#111318"
PANEL = "#161920"
CHROME = "#0c0d11"
LINE = "#262a33"
FOREGROUND = "#d7dae1"
BRIGHT = "#f1f3f7"
DIM = "#7d8492"
FAINT = "#4c525e"
FEED = "#959ba7"
NOTE = "#b7bcc6"
SELECTED = "#232836"
HOVER = "#1a1d25"
BUTTON = "#1d212b"
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
    background=BACKGROUND,
    surface=BACKGROUND,
    panel=PANEL,
    dark=True,
    text_alpha=1.0,
    variables={
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
