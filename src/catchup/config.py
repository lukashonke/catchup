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


@dataclass(frozen=True)
class Config:
    """User settings; every field is optional."""

    open_command: list[str] | None = None
    open_label: str = "Open in Claude"
    # The agent run is off while there is no prompt.
    agent_prompt: str | None = None
    agent_command: tuple[str, ...] = DEFAULT_AGENT_COMMAND
    agent_every: str = "1h"


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
    command = data.get("open_command")
    if command is not None and not _is_argv(command):
        raise CatchupError(
            f"open_command in {path} must be a non-empty list of strings,"
            ' for example open_command = ["/path/to/my-opener.sh"]'
        )
    label = data.get("open_label", Config.open_label)
    if not (isinstance(label, str) and label.strip()):
        raise CatchupError(
            f'open_label in {path} must be a non-empty string, for example open_label = "Open in Codex"'
        )
    prompt = data.get("agent_prompt")
    if prompt is not None and not isinstance(prompt, str):
        raise CatchupError(f"agent_prompt in {path} must be a string")
    agent_command = data.get("agent_command", list(DEFAULT_AGENT_COMMAND))
    if not _is_argv(agent_command):
        raise CatchupError(
            f"agent_command in {path} must be a non-empty list of strings,"
            ' for example agent_command = ["claude", "-p", "--permission-mode", "auto"]'
        )
    every = data.get("agent_every", Config.agent_every)
    if not isinstance(every, str):
        raise CatchupError(f'agent_every in {path} must be a string such as "1h"')
    try:
        parse_every(every)
    except CatchupError as e:
        raise CatchupError(f"agent_every in {path}: {e}") from None
    return Config(
        open_command=command,
        open_label=label.strip(),
        agent_prompt=(prompt or "").strip() or None,
        agent_command=tuple(agent_command),
        agent_every=every,
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


def _is_argv(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(arg, str) for arg in value)
