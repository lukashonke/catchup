"""herdr support: the bundled opener, and the inbox counts in the label of the TUI's tab."""

import json
import re
import secrets
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
    item_id: int, title: str, prompt: str, environ: Mapping[str, str], space: bool = False
) -> None:
    """Start Claude with the prompt in a new herdr tab, or with `space` in a new space.

    The tab or space is labeled with the title and starts in the home directory.
    """
    herdr = environ.get("HERDR_BIN_PATH") or "herdr"
    where = ["--cwd", str(Path.home()), "--label", _label(title), "--focus"]
    # Both answers name the pane they start with as root_pane.
    if space:
        created = await _call(environ, herdr, "workspace", "create", *where)
    else:
        # Inside a herdr pane this names the caller's workspace; without it herdr picks the focused one.
        workspace = environ.get("HERDR_WORKSPACE_ID")
        flag = ("--workspace", workspace) if workspace else ()
        created = await _call(environ, herdr, "tab", "create", *flag, *where)
    pane = created["root_pane"]["pane_id"]
    # Agent names must be unique among live agents, and the same item can be opened twice.
    name = f"catchup-{item_id}-{secrets.token_hex(2)}"
    await _call(environ, herdr, "agent", "start", name, "--kind", "claude", "--pane", pane)
    # herdr reads a leading "-" as an option even after "--"; a space keeps it text.
    text = " " + prompt if prompt.startswith("-") else prompt
    await _call(environ, herdr, "agent", "prompt", name, text)


class TabTitle:
    """Show the inbox counts after the label of the herdr tab the TUI runs in."""

    def __init__(self, environ: Mapping[str, str]) -> None:
        self.environ = environ
        self.tab = environ.get("HERDR_TAB_ID") if environ.get("HERDR_ENV") == "1" else None
        # The tab's label without counts, read on the first show; also what restore() puts back.
        self.base: str | None = None
        self.shown: str | None = None

    async def show(self, counts: str) -> None:
        """Label the tab `<base> <counts>` unless it shows that already; raise CatchupError."""
        if self.tab is None:
            return
        herdr = self.environ.get("HERDR_BIN_PATH") or "herdr"
        if self.base is None:
            label = (await _call(self.environ, herdr, "tab", "get", self.tab))["tab"]["label"]
            self.base = _COUNTS.sub("", label).strip() or "catchup"
        text = f"{self.base} {counts}"
        if text != self.shown:
            await _call(self.environ, herdr, "tab", "rename", self.tab, text)
            self.shown = text

    async def restore(self) -> None:
        """Give the tab back its label without counts; raise CatchupError on failure."""
        if self.tab is None or self.base is None or self.shown is None:
            return
        herdr = self.environ.get("HERDR_BIN_PATH") or "herdr"
        await _call(self.environ, herdr, "tab", "rename", self.tab, self.base)
        self.shown = None


def _label(title: str) -> str:
    if len(title) <= _LABEL_LENGTH:
        return title
    cut = title[: _LABEL_LENGTH - 1]
    # Break at a word when that keeps most of the room.
    if " " in cut[_LABEL_LENGTH // 2 :]:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip() + "…"


async def _call(environ: Mapping[str, str], *argv: str) -> dict[str, Any]:
    result = await run_command(argv, environ)
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError:
        response = {}
    if result.returncode != 0 or "error" in response:
        message = response.get("error", {}).get("message") or result.stderr.strip()
        raise CatchupError(f"herdr {argv[1]} {argv[2]} failed: {message or 'no output'}")
    return response.get("result", {})
