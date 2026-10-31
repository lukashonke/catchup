"""Contexts: background on one item, fetched on request by a quick separate agent run.

Each item and each Todoist task has at most one context, kept as a file in `contexts/` next to
the database and named by its key: the item id, or `todoist-` and the task id. It is a file, not
a row, since it is a disposable cache and not part of the item's history. Like the agent run, a
fetch runs under a supervisor (`python -m catchup.context`) in its own session, so it outlives
the TUI: the supervisor stops a fetch past TIME_LIMIT, then writes the answer, or why there is
none, into the file. A lock around the files keeps that write after the TUI's own.

When the command prints Claude Code's stream-json, the supervisor writes each tool call and
each text into the file while the fetch runs, and keeps only the answer at the end; the log
keeps every step. Any other command's output is the answer as it is.
"""

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

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
# The steps a context file keeps while it is fetched, and how long one may be.
_STEPS = 30
_STEP_LENGTH = 160
# The input that says most about a tool call, in this order; else its first text input.
_TOOL_INPUTS = ("command", "query", "pattern", "url", "file_path", "skill", "description")

ContextState = Literal["fetching", "ready", "failed"]


@dataclass(frozen=True)
class Step:
    """One step of a fetch: a tool call, or text the agent wrote between them."""

    kind: Literal["tool", "text"]
    text: str


@dataclass(frozen=True)
class Context:
    """The context of one item or task: being fetched, ready to read, or failed with the reason."""

    key: str
    state: ContextState
    started_at: datetime
    # The Markdown answer when ready, the reason when failed, empty while fetching.
    text: str = ""
    # The latest steps, while fetching.
    steps: tuple[Step, ...] = ()

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


def read_all() -> dict[str, Context]:
    """Return every context, by key."""
    with _locked() as directory:
        contexts = (_read(path) for path in directory.glob("*.json"))
        return {context.key: context for context in contexts if context}


def fetch(
    key: str,
    item: Item,
    notes: Sequence[Event],
    config: Config,
    environ: Mapping[str, str] = os.environ,
) -> bool:
    """Start fetching the context of `item`, kept under `key`; return False when one already runs.

    Raise CatchupError when MAX_FETCHES already run or the command cannot start.
    """
    # The key names a file, and a task id comes from Todoist.
    if not key.replace("-", "").isalnum():
        raise CatchupError(f"cannot keep a context under {key!r}")
    command = config.context_command
    if not shutil.which(command[0], path=environ.get("PATH")):
        raise CatchupError(f"cannot run {command[0]}: command not found")
    # Nothing here awaits, as in agent.start(): the lock is a blocking flock.
    with _locked() as directory:
        fetching = [c for c in map(_read, directory.glob("*.json")) if c and c.state == "fetching"]
        if any(context.key == key for context in fetching):
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
                    [sys.executable, "-I", "-m", "catchup.context", key, *command],
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
        _write(directory, key, {"state": "fetching", "pid": process.pid})
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


def supervise(key: str, argv: list[str], limit: timedelta = TIME_LIMIT) -> int:
    """Run the fetch, stop it past `limit`, store the answer or the reason, return the exit code."""
    started = utc_now()
    with log_path().open("a", encoding="utf-8") as log:
        print(f"--- {stamp()} {key} started", file=log, flush=True)
        # Its own process group, so stopping it also stops the tools it started.
        child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=log, start_new_session=True)
        signal.signal(signal.SIGTERM, lambda *_: stop_group(child.pid, signal.SIGTERM))
        assert child.stdout is not None
        output = _Output(key, log)
        reader = threading.Thread(target=output.read, args=(child.stdout,), daemon=True)
        reader.start()
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
        # A tool the fetch left running in the background may hold the output open.
        reader.join(timeout=_STOP_GRACE_SECONDS)
        text = output.answer()
        if code == 0 and text and not output.failed:
            record = {"state": "ready", "text": text}
        else:
            if code is None:
                reason = f"It took longer than {int(limit.total_seconds() // 60)} minutes."
            elif code == 0 and not text:
                reason = "It printed nothing."
            elif code == 0:
                reason = "It reported an error."
            else:
                reason = f"It exited with code {code}."
            tail = "\n".join(text.splitlines()[-_ERROR_LINES:])
            record = {"state": "failed", "text": f"{reason} The log is in {log_path()}.\n\n{tail}"}
        ending = record["state"] if code is not None else "stopped: it ran too long"
        print(f"--- {stamp()} {key} {ending}", file=log, flush=True)
    with _locked() as directory:
        path = directory / f"{key}.json"
        # Only the fetch this supervisor started, not a later one that replaced it.
        if _raw(path).get("pid") == os.getpid():
            _write(directory, key, {**record, "started_at": started.isoformat()})
    return 1 if code is None else code


class _Output:
    """What a fetch prints: Claude Code's stream of steps and its answer, or plain text."""

    def __init__(self, key: str, log: Any) -> None:
        self.key = key
        self.log = log
        self.steps: list[Step] = []
        self.result: str | None = None
        self.failed = False
        self.lines: list[str] = []

    def read(self, stream: Iterable[bytes]) -> None:
        for raw in stream:
            line = raw.decode(errors="replace")
            event = _event(line)
            if event is None:
                self.lines.append(line)
            elif event["type"] == "result":
                self.result = str(event.get("result") or "")
                self.failed = bool(event.get("is_error"))
            elif steps := _steps(event):
                self.steps = (self.steps + steps)[-_STEPS:]
                for step in steps:
                    print(f"    {step.kind}: {step.text}", file=self.log, flush=True)
                self._show()

    def answer(self) -> str:
        """Return the answer: the stream's result, else everything that was printed."""
        return (self.result if self.result is not None else "".join(self.lines)).strip()

    def _show(self) -> None:
        steps = [{"kind": step.kind, "text": step.text} for step in self.steps]
        with _locked() as directory:
            data = _raw(directory / f"{self.key}.json")
            # Only into the fetch this supervisor started, as for the answer.
            if data.get("pid") == os.getpid():
                _write(directory, self.key, {**data, "steps": steps})


def _event(line: str) -> dict[str, Any] | None:
    """Return the stream-json event on the line, or None when the line is something else."""
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) and isinstance(event.get("type"), str) else None


def _steps(event: dict[str, Any]) -> list[Step]:
    message = event.get("message")
    if event["type"] != "assistant" or not isinstance(message, dict):
        return []
    steps = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and (text := " ".join(str(block.get("text")).split())):
            steps.append(Step("text", _cut(text)))
        elif block.get("type") == "tool_use":
            steps.append(Step("tool", _cut(_tool(str(block.get("name")), block.get("input")))))
    return steps


def _tool(name: str, args: object) -> str:
    """Return a tool call as a line, such as `Slack · slack_search_public  retry limit`."""
    # An MCP tool is named mcp__<server>__<tool>, and the server says the most.
    parts = name.split("__")
    if len(parts) == 3 and parts[0] == "mcp":
        name = f"{parts[1].removeprefix('claude_ai_').replace('_', ' ')} · {parts[2]}"
    if not isinstance(args, dict):
        return name
    texts = [args.get(key) for key in _TOOL_INPUTS] + list(args.values())
    detail = next((text for text in texts if isinstance(text, str) and text.strip()), "")
    return f"{name}  {' '.join(detail.split())}".strip()


def _cut(text: str) -> str:
    return text if len(text) <= _STEP_LENGTH else text[: _STEP_LENGTH - 1] + "…"


@contextmanager
def _locked() -> Iterator[Path]:
    directory = contexts_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield directory


def _write(directory: Path, key: str, data: dict[str, object]) -> None:
    data = {"started_at": utc_now().isoformat(), **data}
    temporary = directory / f"{key}.json.tmp"
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.replace(directory / f"{key}.json")


def _raw(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read(path: Path) -> Context | None:
    data = _raw(path)
    try:
        state = data["state"]
        started_at = datetime.fromisoformat(str(data["started_at"])).astimezone(UTC)
    except (ValueError, KeyError):
        return None
    if state == "fetching" and not process_alive(int(data.get("pid") or 0)):
        # The supervisor ended without writing an answer, for example when it was killed.
        return Context(path.stem, "failed", started_at, f"The fetch stopped. See {log_path()}.")
    steps = data.get("steps") if isinstance(data.get("steps"), list) else []
    return Context(
        path.stem,
        state,
        started_at,
        str(data.get("text") or ""),
        tuple(
            Step(step["kind"], str(step.get("text")))
            for step in steps
            if isinstance(step, dict) and step.get("kind") in ("tool", "text")
        ),
    )


if __name__ == "__main__":
    sys.exit(supervise(sys.argv[1], sys.argv[2:]))
