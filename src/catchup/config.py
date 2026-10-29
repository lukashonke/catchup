"""Loading of the optional `config.toml`."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from catchup import CatchupError


@dataclass(frozen=True)
class Config:
    """User settings; every field is optional."""

    open_command: list[str] | None = None


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
    command = data.get("open_command")
    if command is not None and not (
        isinstance(command, list) and command and all(isinstance(arg, str) for arg in command)
    ):
        raise CatchupError(
            f"open_command in {path} must be a non-empty list of strings,"
            ' for example open_command = ["/path/to/my-opener.sh"]'
        )
    return Config(open_command=command)
