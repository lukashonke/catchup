"""Herdr support: the bundled opener, and the inbox counts in the label of the TUI's tab.

Both talk to the Herdr app through `herdr-app`. `HC_TERMINAL_ID`, which herdr-core sets in each
of its terminals, tells that the TUI runs inside Herdr.
"""

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from catchup import CatchupError
from catchup.opener import run_command

_LABEL_LENGTH = 32
# The counts TabTitle appends, so a label left behind by a crashed TUI loses them again.
# shortcut: a label that ends in a number of its own, such as "sprint 42", loses it too;
# keep the original label in a file if that ever bites.
_COUNTS = re.compile(r"\s+\d+( · \d+ new)?$")


async def open_in_herdr(
    title: str, prompt: str, environ: Mapping[str, str], space: bool = False
) -> None:
    """Start Claude with the prompt in a new Herdr tab, or with `space` in a new space.

    The tab or space is named after the title, starts in the home directory, and is shown at once.
    """
    name = _label(title)
    args = {"folder": str(Path.home()), "argv": ["claude"], "task": prompt}
    if space:
        await _do(environ, "space.new", name=name, tab=name, **args)
    else:
        # Without a space, the tab goes to the one shown: the TUI's, as the key was pressed there.
        await _do(environ, "tab.new", name=name, **args)


class TabTitle:
    """Show the inbox counts after the name of the Herdr tab the TUI runs in."""

    def __init__(self, environ: Mapping[str, str]) -> None:
        self.environ = environ
        self.terminal = environ.get("HC_TERMINAL_ID") or None
        # Found on a show, with the tab's name.
        self.tab: str | None = None
        self.name: str | None = None
        # The name without counts; what the counts follow.
        self.base: str | None = None
        self.shown: str | None = None

    async def show(self, counts: str) -> None:
        """Name the tab `<base> <counts>` unless it shows that already; raise CatchupError."""
        if self.terminal is None:
            return
        if self.base is None:
            found = await self._find()
            # Not in a tab yet, or never, as for a hidden worker: look again on the next show.
            if found is None:
                return
            self.tab, self.name = found
            self.base = _COUNTS.sub("", self.name).strip() or "catchup"
        text = f"{self.base} {counts}"
        if text != self.shown:
            await _do(self.environ, "tab.rename", id=self.tab, name=text)
            self.shown = text

    async def restore(self) -> None:
        """Give the tab back its name from before the counts; raise CatchupError on failure."""
        if self.tab is None or self.base is None or self.shown is None:
            return
        # An empty name gives an unnamed tab back its number.
        name = self.base if self.name else ""
        await _do(self.environ, "tab.rename", id=self.tab, name=name)
        self.shown = None

    async def _find(self) -> tuple[str, str] | None:
        """Return the ID and name of the tab that shows the TUI's terminal."""
        layout = await _herdr(self.environ, "ls")
        for space in layout.get("spaces", []):
            for tab in space.get("tabs", []):
                if any(t.get("terminal") == self.terminal for t in tab.get("terminals", [])):
                    return tab["id"], tab.get("name") or ""
        return None


def _label(title: str) -> str:
    if len(title) <= _LABEL_LENGTH:
        return title
    cut = title[: _LABEL_LENGTH - 1]
    # Break at a word when that keeps most of the room.
    if " " in cut[_LABEL_LENGTH // 2 :]:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip() + "…"


async def _do(environ: Mapping[str, str], command: str, **args: Any) -> dict[str, Any]:
    # One JSON object, so a prompt that starts with "-" is never read as an option.
    return await _herdr(environ, "do", command, json.dumps(args))


async def _herdr(environ: Mapping[str, str], *args: str) -> dict[str, Any]:
    result = await run_command(["herdr-app", *args], environ)
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError:
        response = {}
    if not isinstance(response, dict):
        response = {}
    if result.returncode != 0 or "error" in response:
        message = response.get("error") or result.stderr.strip()
        what = args[1] if args[0] == "do" else args[0]
        raise CatchupError(f"herdr-app {what} failed: {message or 'no output'}")
    return response
