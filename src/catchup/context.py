"""Contexts: background on one item, fetched on request by a quick separate agent run.

Each item has at most one context, kept as a file in `contexts/` next to the database rather
than in the database, since it is a disposable cache and not part of the item's history. Like the
agent run, a fetch runs under a supervisor (`python -m catchup.context`) in its own session, so it
outlives the TUI: the supervisor stops a fetch past TIME_LIMIT, then writes the answer, or why
there is none, into the file. A lock around the files keeps that write after the TUI's own.
"""

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from catchup import CatchupError
from catchup.agent import process_alive, stamp, stop_group
from catchup.config import Config
from catchup.store import Event, Item, default_db_path, utc_now

# Time awake only, as for the agent run.
TIME_LIMIT = timedelta(minutes=5)
MAX_FETCHES = 3
_STOP_GRACE_SECONDS = 5
# The end of a failed fetch's output that the context view shows.
_ERROR_LINES = 20

ContextState = Literal["fetching", "ready", "failed"]


@dataclass(frozen=True)
class Context:
    """The context of one item: being fetched, ready to read, or failed with the reason."""

    item_id: int
    state: ContextState
    started_at: datetime
    # The Markdown answer when ready, the reason when failed, empty while fetching.
    text: str = ""

    def stale(self, item: Item) -> bool:
        """Return whether the agent changed the item after this fetch started."""
        # The store keeps whole seconds; a change in the second the fetch started reads as older.
        started = self.started_at.replace(microsecond=0)
        return item.agent_changed_at is not None and item.agent_changed_at > started


def contexts_dir() -> Path:
    """Return the directory of the context files: `contexts/` next to the database."""
    return default_db_path().parent / "contexts"


def log_path() -> Path:
    """Return the file that holds the start and end of every fetch, and what they printed."""
    return default_db_path().parent / "context.log"


def read_all() -> dict[int, Context]:
    """Return the context of every item that has one, by item id."""
    with _locked() as directory:
        contexts = (_read(path) for path in directory.glob("*.json"))
        return {context.item_id: context for context in contexts if context}


def fetch(
    item: Item, notes: Sequence[Event], config: Config, environ: Mapping[str, str] = os.environ
) -> bool:
    """Start fetching the item's context; return False when a fetch for it already runs.

    Raise CatchupError when MAX_FETCHES already run or the command cannot start.
    """
    command = config.context_command
    if not shutil.which(command[0], path=environ.get("PATH")):
        raise CatchupError(f"cannot run {command[0]}: command not found")
    # Nothing here awaits, as in agent.start(): the lock is a blocking flock.
    with _locked() as directory:
        fetching = [c for c in map(_read, directory.glob("*.json")) if c and c.state == "fetching"]
        if any(context.item_id == item.id for context in fetching):
            return False
        if len(fetching) >= MAX_FETCHES:
            raise CatchupError(
                f"{MAX_FETCHES} contexts are being fetched already. Try again when one is ready."
            )
        with tempfile.TemporaryFile() as prompt:
            prompt.write(prompt_for(item, notes, config).encode())
            prompt.seek(0)
            try:
                process = subprocess.Popen(
                    # -I: the home directory, the cwd, must not shadow the catchup package.
                    [sys.executable, "-I", "-m", "catchup.context", str(item.id), *command],
                    cwd=Path.home(),
                    env=dict(environ),
                    stdin=prompt,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    # Its own session, so closing the TUI's terminal does not stop the fetch.
                    start_new_session=True,
                )
            except OSError as e:
                raise CatchupError(f"cannot run {command[0]}: {e.strerror or e}") from e
        _write(directory, item.id, {"state": "fetching", "pid": process.pid})
    return True


def prompt_for(item: Item, notes: Sequence[Event], config: Config) -> str:
    """Return the configured context prompt followed by the item it is about."""
    parts = [config.context_prompt, "## The item", f"Title: {item.title}"]
    if item.body:
        parts.append(item.body)
    if item.links:
        parts.append("Links:\n" + "\n".join(f"- {link.label}: {link.url}" for link in item.links))
    if written := [event.note for event in notes if event.actor == "user" and event.note]:
        parts.append("The user's notes:\n" + "\n".join(f"- {note}" for note in written))
    return "\n\n".join(parts)


def supervise(item_id: int, argv: list[str], limit: timedelta = TIME_LIMIT) -> int:
    """Run the fetch, stop it past `limit`, store the answer or the reason, return the exit code."""
    started = utc_now()
    with tempfile.TemporaryFile() as out, log_path().open("a", encoding="utf-8") as log:
        print(f"--- {stamp()} item {item_id} started", file=log, flush=True)
        # Its own process group, so stopping it also stops the tools it started.
        child = subprocess.Popen(argv, stdout=out, stderr=log, start_new_session=True)
        signal.signal(signal.SIGTERM, lambda *_: stop_group(child.pid, signal.SIGTERM))
        try:
            code = child.wait(timeout=limit.total_seconds())
        except subprocess.TimeoutExpired:
            stop_group(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=_STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                stop_group(child.pid, signal.SIGKILL)
                child.wait()
            code = None
        out.seek(0)
        text = out.read().decode(errors="replace").strip()
        if code == 0 and text:
            record = {"state": "ready", "text": text}
        else:
            if code is None:
                reason = f"It took longer than {int(limit.total_seconds() // 60)} minutes."
            elif code == 0:
                reason = "It printed nothing."
            else:
                reason = f"It exited with code {code}."
            tail = "\n".join(text.splitlines()[-_ERROR_LINES:])
            record = {"state": "failed", "text": f"{reason} The log is in {log_path()}.\n\n{tail}"}
        ending = record["state"] if code is not None else "stopped: it ran too long"
        print(f"--- {stamp()} item {item_id} {ending}", file=log, flush=True)
    with _locked() as directory:
        path = directory / f"{item_id}.json"
        # Only the fetch this supervisor started, not a later one that replaced it.
        if _raw(path).get("pid") == os.getpid():
            _write(directory, item_id, {**record, "started_at": started.isoformat()})
    return 1 if code is None else code


@contextmanager
def _locked() -> Iterator[Path]:
    directory = contexts_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield directory


def _write(directory: Path, item_id: int, data: dict[str, object]) -> None:
    data = {"started_at": utc_now().isoformat(), **data}
    temporary = directory / f"{item_id}.json.tmp"
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.replace(directory / f"{item_id}.json")


def _raw(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read(path: Path) -> Context | None:
    data = _raw(path)
    try:
        item_id, state = int(path.stem), data["state"]
        started_at = datetime.fromisoformat(str(data["started_at"])).astimezone(UTC)
    except (ValueError, KeyError):
        return None
    if state == "fetching" and not process_alive(int(data.get("pid") or 0)):
        # The supervisor ended without writing an answer, for example when it was killed.
        return Context(item_id, "failed", started_at, f"The fetch stopped. See {log_path()}.")
    return Context(item_id, state, started_at, str(data.get("text") or ""))


if __name__ == "__main__":
    sys.exit(supervise(int(sys.argv[1]), sys.argv[2:]))
