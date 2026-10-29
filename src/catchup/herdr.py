"""The bundled opener for herdr: a new tab in the current workspace with Claude in it."""

import json
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from catchup import CatchupError
from catchup.opener import run_command

_LABEL_LENGTH = 32


async def open_in_herdr(item_id: int, title: str, prompt: str, environ: Mapping[str, str]) -> None:
    """Create a tab labeled with the title in the home directory, start Claude and send the prompt."""
    herdr = environ.get("HERDR_BIN_PATH") or "herdr"
    # Inside a herdr pane this names the caller's workspace; without it herdr picks the focused one.
    workspace = environ.get("HERDR_WORKSPACE_ID")
    tab = await _call(
        environ,
        herdr,
        "tab",
        "create",
        *(("--workspace", workspace) if workspace else ()),
        "--cwd",
        str(Path.home()),
        "--label",
        _label(title),
        "--focus",
    )
    pane = tab["root_pane"]["pane_id"]
    # Agent names must be unique among live agents, and the same item can be opened twice.
    name = f"catchup-{item_id}-{secrets.token_hex(2)}"
    await _call(environ, herdr, "agent", "start", name, "--kind", "claude", "--pane", pane)
    # herdr reads a leading "-" as an option even after "--"; a space keeps it text.
    text = " " + prompt if prompt.startswith("-") else prompt
    await _call(environ, herdr, "agent", "prompt", name, text)


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
