"""Loading and saving of the optional `config.toml`."""

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import tomlkit

from catchup import CatchupError
from catchup.when import parse_every

DEFAULT_AGENT_COMMAND = ("claude", "-p", "--permission-mode", "auto")
DEFAULT_CONTEXT_COMMAND = ("claude", "-p", "--model", "haiku", "--permission-mode", "auto")
DEFAULT_CONTEXT_PROMPT = (
    "Fetch any relevant context for this item from the connected tools and MCPs, and summarize"
    " it. The goal is to catch the user up on this one item. Only read: do not post, send or"
    " change anything. Reply with the summary in Markdown and nothing else."
)
DEFAULT_TODOIST_COMMAND = ("td",)


@dataclass(frozen=True)
class Config:
    """User settings; every field is optional."""

    open_command: list[str] | None = None
    open_label: str = "Open in Claude"
    open_space_command: list[str] | None = None
    open_space_label: str = "Open in Claude, new space"
    # The agent run is off while there is no prompt.
    agent_prompt: str | None = None
    agent_command: tuple[str, ...] = DEFAULT_AGENT_COMMAND
    agent_every: str = "1h"
    context_prompt: str = DEFAULT_CONTEXT_PROMPT
    context_command: tuple[str, ...] = DEFAULT_CONTEXT_COMMAND
    # The Todoist tab is off by default, as it needs the Todoist CLI.
    todoist: bool = False
    todoist_command: tuple[str, ...] = DEFAULT_TODOIST_COMMAND
    todoist_filter: str = "today | overdue"


def default_config_path() -> Path:
    """Return the config path under `$XDG_CONFIG_HOME`, else under `~/.config`."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(config_home) / "catchup" / "config.toml"


def load_config(path: Path | None = None) -> Config:
    """Read and validate the config file; a missing file gives the defaults."""
    path = path or default_config_path()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Config()
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise CatchupError(
            f"cannot read the config at {path}: {e}; fix the file or remove it"
        ) from e
    return validate(data, path)


def validate(data: dict[str, Any], path: Path) -> Config:
    """Return the config that `data` describes; raise CatchupError naming the first bad key."""
    command = _open_command(data, "open_command", path)
    label = _label(data, "open_label", Config.open_label, path)
    space_command = _open_command(data, "open_space_command", path)
    space_label = _label(data, "open_space_label", Config.open_space_label, path)
    prompt = data.get("agent_prompt")
    if prompt is not None and not isinstance(prompt, str):
        raise CatchupError(f"agent_prompt in {path} must be a string")
    agent_command = data.get("agent_command", list(DEFAULT_AGENT_COMMAND))
    if not _is_argv(agent_command):
        raise CatchupError(
            f"agent_command in {path} must be a non-empty list of strings,"
            ' for example agent_command = ["claude", "-p", "--permission-mode", "auto"]'
        )
    context_prompt = data.get("context_prompt", DEFAULT_CONTEXT_PROMPT)
    if not (isinstance(context_prompt, str) and context_prompt.strip()):
        raise CatchupError(f"context_prompt in {path} must be a non-empty string")
    context_command = data.get("context_command", list(DEFAULT_CONTEXT_COMMAND))
    if not _is_argv(context_command):
        raise CatchupError(
            f"context_command in {path} must be a non-empty list of strings,"
            ' for example context_command = ["claude", "-p", "--model", "haiku"]'
        )
    every = data.get("agent_every", Config.agent_every)
    if not isinstance(every, str):
        raise CatchupError(f'agent_every in {path} must be a string such as "1h"')
    try:
        parse_every(every)
    except CatchupError as e:
        raise CatchupError(f"agent_every in {path}: {e}") from None
    todoist = data.get("todoist", Config.todoist)
    if not isinstance(todoist, bool):
        raise CatchupError(f"todoist in {path} must be true or false")
    todoist_command = data.get("todoist_command", list(DEFAULT_TODOIST_COMMAND))
    if not _is_argv(todoist_command):
        raise CatchupError(
            f"todoist_command in {path} must be a non-empty list of strings,"
            ' for example todoist_command = ["td"]'
        )
    todoist_filter = data.get("todoist_filter", Config.todoist_filter)
    if not (isinstance(todoist_filter, str) and todoist_filter.strip()):
        raise CatchupError(
            f"todoist_filter in {path} must be a non-empty Todoist filter,"
            ' for example todoist_filter = "today | overdue"'
        )
    return Config(
        open_command=command,
        open_label=label,
        open_space_command=space_command,
        open_space_label=space_label,
        agent_prompt=(prompt or "").strip() or None,
        agent_command=tuple(agent_command),
        agent_every=every,
        context_prompt=context_prompt.strip(),
        context_command=tuple(context_command),
        todoist=todoist,
        todoist_command=tuple(todoist_command),
        todoist_filter=todoist_filter.strip(),
    )


def save_config(config: Config, path: Path | None = None) -> None:
    """Write `config` into the file, keeping its comments and any keys catchup does not know."""
    path = path or default_config_path()
    try:
        document = tomlkit.parse(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        document = tomlkit.document()
    defaults = Config()
    for field in fields(Config):
        value = getattr(config, field.name)
        # A key at its default is left out, so a later change of the default reaches the user.
        if value is None or value == getattr(defaults, field.name):
            document.pop(field.name, None)
        elif isinstance(value, str) and "\n" in value:
            document[field.name] = tomlkit.string(value, multiline=True)
        else:
            document[field.name] = list(value) if isinstance(value, tuple) else value
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".toml.tmp")
    temporary.write_text(tomlkit.dumps(document), encoding="utf-8")
    temporary.replace(path)


def _open_command(data: dict[str, Any], key: str, path: Path) -> list[str] | None:
    command = data.get(key)
    if command is not None and not _is_argv(command):
        raise CatchupError(
            f"{key} in {path} must be a non-empty list of strings,"
            f' for example {key} = ["/path/to/my-opener.sh"]'
        )
    return command


def _label(data: dict[str, Any], key: str, default: str, path: Path) -> str:
    label = data.get(key, default)
    if not (isinstance(label, str) and label.strip()):
        raise CatchupError(
            f'{key} in {path} must be a non-empty string, for example {key} = "Open in Codex"'
        )
    return label.strip()


def _is_argv(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(arg, str) for arg in value)
