"""The agent run: the command that fills the inbox, started in the background on a schedule.

The run outlives the TUI that started it, and several TUIs may be open at once. A small state
file next to the database records the process id and the start time of the last run, and a lock
around reading and writing it makes sure only one TUI starts each run.
"""

import fcntl
import json
import os
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from catchup import CatchupError
from catchup.config import Config
from catchup.store import default_db_path
from catchup.when import parse_every


@dataclass(frozen=True)
class RunState:
    """The last agent run, as far as the state file knows."""

    started_at: datetime | None
    running: bool


def state_dir() -> Path:
    """Return the directory of the state file and the log: the database's directory."""
    return default_db_path().parent


def log_path() -> Path:
    """Return the file that holds the output of the last run."""
    return state_dir() / "agent.log"


def read_state() -> RunState:
    """Return when the last run started and whether it still runs."""
    with _locked() as path:
        return _read(path)


def is_due(config: Config, now: datetime) -> bool:
    """Return whether the agent is configured, not running, and its interval has passed."""
    if not config.agent_prompt:
        return False
    state = read_state()
    if state.running:
        return False
    return state.started_at is None or now - state.started_at >= parse_every(config.agent_every)


def start(
    config: Config, now: datetime, environ: Mapping[str, str] = os.environ
) -> subprocess.Popen[bytes] | None:
    """Start the agent with the prompt on stdin; return None when a run is already going.

    Raise CatchupError when the agent is not configured or the command cannot start.
    """
    if not config.agent_prompt:
        raise CatchupError("the agent has no prompt; set agent_prompt in the settings")
    # Nothing here awaits: the lock is a blocking flock, and read_state() takes it on the
    # same event loop, so holding it across an await would freeze the TUI.
    with _locked() as path:
        if _read(path).running:
            return None
        # A file rather than a pipe, so the prompt needs no writer that outlives this call.
        with tempfile.TemporaryFile() as prompt, log_path().open("w", encoding="utf-8") as output:
            prompt.write(config.agent_prompt.encode())
            prompt.seek(0)
            try:
                process = subprocess.Popen(
                    config.agent_command,
                    cwd=Path.home(),
                    env=dict(environ),
                    stdin=prompt,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    # Its own session, so closing the TUI's terminal does not stop the run.
                    start_new_session=True,
                )
            except OSError as e:
                raise CatchupError(
                    f"cannot run {config.agent_command[0]}: {e.strerror or e}"
                ) from e
        path.write_text(
            json.dumps({"pid": process.pid, "started_at": now.astimezone(UTC).isoformat()}),
            encoding="utf-8",
        )
    return process


@contextmanager
def _locked() -> Iterator[Path]:
    """Hold an exclusive lock between TUIs while reading and writing the state file."""
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "agent.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield directory / "agent.json"


def _read(path: Path) -> RunState:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pid, started_at = int(data["pid"]), datetime.fromisoformat(data["started_at"])
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return RunState(started_at=None, running=False)
    return RunState(started_at=started_at, running=_alive(pid))


def _alive(pid: int) -> bool:
    # shortcut: a recycled process id reads as a running agent until that process exits;
    # store the process start time too if this ever blocks a run for long.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
