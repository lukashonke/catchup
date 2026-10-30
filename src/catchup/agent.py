"""The agent run: the command that fills the inbox, started in the background on a schedule.

The run outlives the TUI that started it, and several TUIs may be open at once. A small state
file next to the database records the process id and the start time of the last run, and a lock
around reading and writing it makes sure only one TUI starts each run.

A supervisor process (`python -m catchup.agent`) runs the command, so the time limit and the
outcome do not depend on the TUI staying open: it stops a run that takes longer than
TIME_LIMIT, writes the exit code into the state file, and notes both ends of the run in the log.
"""

import fcntl
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from catchup import CatchupError
from catchup.config import Config
from catchup.store import default_db_path
from catchup.when import parse_every

# Time awake only: the clock behind it stops while the computer sleeps.
TIME_LIMIT = timedelta(minutes=30)
# The code that `timeout` from coreutils uses for a stopped command.
TIMED_OUT = 124
_STOP_GRACE_SECONDS = 10
# The log keeps the newest runs; past the limit, start() drops the older half.
_LOG_LIMIT_BYTES = 1_000_000


@dataclass(frozen=True)
class RunState:
    """The last agent run, as far as the state file knows."""

    started_at: datetime | None
    running: bool
    # None while the run goes on, or when its supervisor ended before it could record it.
    exit_code: int | None = None

    @property
    def failed(self) -> bool:
        return not self.running and self.exit_code not in (None, 0)


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
    # Checked here, as the supervisor would only find out after the start was reported.
    if not shutil.which(config.agent_command[0], path=environ.get("PATH")):
        raise CatchupError(f"cannot run {config.agent_command[0]}: command not found")
    # Nothing here awaits: the lock is a blocking flock, and read_state() takes it on the
    # same event loop, so holding it across an await would freeze the TUI.
    with _locked() as path:
        if _read(path).running:
            return None
        _trim_log()
        # A file rather than a pipe, so the prompt needs no writer that outlives this call.
        with tempfile.TemporaryFile() as prompt, log_path().open("a", encoding="utf-8") as output:
            prompt.write(config.agent_prompt.encode())
            prompt.seek(0)
            try:
                process = subprocess.Popen(
                    # -I: the home directory, the cwd, must not shadow the catchup package.
                    [sys.executable, "-I", "-m", "catchup.agent", *config.agent_command],
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


def _trim_log() -> None:
    path = log_path()
    try:
        if path.stat().st_size <= _LOG_LIMIT_BYTES:
            return
    except FileNotFoundError:
        return
    with path.open("rb") as log:
        log.seek(-_LOG_LIMIT_BYTES // 2, os.SEEK_END)
        log.readline()  # Start at a whole line.
        rest = log.read()
    path.write_bytes(rest)


def supervise(argv: list[str], limit: timedelta = TIME_LIMIT) -> int:
    """Run the agent command, stop it past `limit`, record how it ended, and return its code."""
    print(f"--- {stamp()} started: {shlex.join(argv)}", flush=True)
    # Its own process group, so stopping it also stops the tools it started.
    child = subprocess.Popen(argv, start_new_session=True)
    # Stopping the supervisor stops the run with it, and the exit code still gets recorded.
    signal.signal(signal.SIGTERM, lambda *_: stop_group(child.pid, signal.SIGTERM))
    try:
        code = child.wait(timeout=limit.total_seconds())
        ending = f"exited with code {code}"
    except subprocess.TimeoutExpired:
        stop_group(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=_STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            stop_group(child.pid, signal.SIGKILL)
            child.wait()
        code = TIMED_OUT
        ending = f"stopped after {_minutes(limit)}: it ran too long"
    print(f"--- {stamp()} {ending}", flush=True)
    with _locked() as path:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            data = {}
        # Only the run this supervisor started, not a later one that replaced it.
        if data.get("pid") == os.getpid():
            path.write_text(json.dumps({**data, "exit_code": code}), encoding="utf-8")
    return code


def stop_group(pgid: int, sig: signal.Signals) -> None:
    """Send `sig` to a process group that may have ended already."""
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def stamp() -> str:
    """Return the local time as the logs show it."""
    return f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S}"


def _minutes(limit: timedelta) -> str:
    return f"{int(limit.total_seconds() // 60)} minutes"


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
        exit_code = data.get("exit_code")
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return RunState(started_at=None, running=False)
    running = exit_code is None and process_alive(pid)
    return RunState(started_at=started_at, running=running, exit_code=exit_code)


def process_alive(pid: int) -> bool:
    """Return whether a process with this id runs."""
    # shortcut: a recycled process id reads as a running agent until that process exits;
    # store the process start time too if this ever blocks a run for long.
    if pid <= 0:
        return False  # os.kill() would signal a process group instead.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


if __name__ == "__main__":
    sys.exit(supervise(sys.argv[1:]))
