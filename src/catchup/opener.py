"""The open action: building the prompt and running the command that opens an item."""

import asyncio
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence

from catchup import CatchupError
from catchup.config import Config, default_config_path
from catchup.store import Item


def default_prompt(item: Item) -> str:
    """Return the prompt for an item the agent wrote no prompt for."""
    parts = [item.title]
    if item.body:
        parts.append(item.body)
    if item.links:
        parts.append("\n".join(link.url for link in item.links))
    parts.append("Help me handle this.")
    return "\n\n".join(parts)


def prompt_of(item: Item) -> str:
    """Return the prompt that the open action sends: the agent's, else the default one."""
    return item.prompt or default_prompt(item)


def item_env(item: Item) -> dict[str, str]:
    """Return the environment variables that carry the item to `open_command`."""
    return {
        "CATCHUP_ID": str(item.id),
        "CATCHUP_TITLE": item.title,
        "CATCHUP_PROMPT": prompt_of(item),
        "CATCHUP_LINKS": "\n".join(link.url for link in item.links),
    }


async def open_item(
    item: Item, config: Config, environ: Mapping[str, str] = os.environ, space: bool = False
) -> None:
    """Open the item with its command, else with herdr inside herdr; raise CatchupError on failure.

    With `space`, it opens in a new herdr space through `open_space_command` instead of a tab.
    """
    key, command, label = (
        ("open_space_command", config.open_space_command, config.open_space_label)
        if space
        else ("open_command", config.open_command, config.open_label)
    )
    if command:
        result = await run_command(command, {**environ, **item_env(item)})
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no output"
            raise CatchupError(f"{command[0]} exited with code {result.returncode}: {detail}")
    elif environ.get("HERDR_ENV") == "1":
        from catchup.herdr import open_in_herdr

        await open_in_herdr(item.id, item.title, prompt_of(item), environ, space=space)
    else:
        raise CatchupError(
            f"{label} has nothing to run. Set {key} in {default_config_path()},"
            f' for example {key} = ["/path/to/my-opener.sh"]'
        )


async def run_command(
    argv: Sequence[str], env: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run `argv` without a shell, in its own session; raise CatchupError when it cannot start."""
    # Files rather than pipes: a command that outlives the TUI must not die of a closed pipe.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
        except OSError as e:
            raise CatchupError(f"cannot run {argv[0]}: {e.strerror or e}") from e
        code = await process.wait()
        out.seek(0)
        err.seek(0)
        return subprocess.CompletedProcess(
            list(argv),
            code,
            out.read().decode(errors="replace"),
            err.read().decode(errors="replace"),
        )
