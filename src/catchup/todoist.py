"""Todoist through its CLI `td`: the tasks of the Todoist tab, and the actions on them.

Catchup calls `td` rather than the Todoist API, so it never holds a token: `td` signs in
and keeps the token in the system's credential store. Every call runs `td` without a shell,
in a subprocess the event loop waits on, so a slow network never freezes the TUI.
"""

import asyncio
import json
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from catchup import CatchupError
from catchup.opener import run_command
from catchup.store import default_db_path

INSTALL_COMMAND = "npm install -g @doist/todoist-cli"
# The codes td reports when it has no token or Todoist rejects the one it has.
_SIGNED_OUT = {"NO_TOKEN", "AUTH_ERROR"}


class NotInstalled(CatchupError):
    """The td command is not on the PATH."""


class SignedOut(CatchupError):
    """td has no Todoist account signed in, or Todoist rejected its token."""


@dataclass(frozen=True)
class Task:
    """A Todoist task as the Todoist tab shows it."""

    id: str
    title: str
    description: str
    # Todoist's API numbers are the reverse of the p-labels: 4 is p1, the most urgent, 1 is p4.
    priority: int
    due_date: date | None
    # The local time of day, for a task due at a time.
    due_time: time | None
    due_string: str
    recurring: bool
    project: str
    section: str | None
    labels: tuple[str, ...]
    url: str

    @property
    def p(self) -> int:
        """The priority as Todoist's labels number it: 1 is the most urgent, 4 the least."""
        return 5 - self.priority


@dataclass(frozen=True)
class Tasks:
    """The tasks that match the filter, inbox first, then by project; and when td listed them."""

    tasks: list[Task]
    fetched_at: datetime


async def fetch(command: Sequence[str], query: str) -> dict[str, Any]:
    """Return what td lists for `query`: the tasks, the projects and their sections.

    The result is the raw JSON that `parse` reads and the cache keeps.
    """
    tasks, projects = await asyncio.gather(
        _json(command, "task", "list", "--filter", query, "--json", "--all"),
        _json(command, "project", "list", "--json", "--all", "--full"),
    )
    # td lists sections one project at a time, so ask only for the projects that need them.
    sectioned = sorted({t["projectId"] for t in tasks["results"] if t.get("sectionId")})
    sections = await asyncio.gather(
        *(
            _json(command, "section", "list", "--project", f"id:{p}", "--json", "--all")
            for p in sectioned
        )
    )
    return {
        "fetched_at": datetime.now(UTC).isoformat(),
        "tasks": tasks["results"],
        "projects": projects["results"],
        "sections": [section for listed in sections for section in listed["results"]],
    }


def parse(raw: dict[str, Any]) -> Tasks:
    """Return the tasks in `raw`, the inbox first, then each project in Todoist's order.

    Within a project the oldest due date comes first, then the higher priority, then the time.
    Raise CatchupError when `raw` is not what `fetch` returns.
    """
    try:
        projects = {p["id"]: (index, p) for index, p in enumerate(raw["projects"])}
        sections = {s["id"]: s["name"] for s in raw["sections"]}
        keyed = []
        for order, data in enumerate(raw["tasks"]):
            index, project = projects.get(data.get("projectId"), (len(projects), {}))
            # The inbox is where a task without a project lives, so it comes first.
            group = -1 if project.get("inboxProject") else index
            task = _task(data, project.get("name", "Other"), sections.get(data.get("sectionId")))
            due = (task.due_date or date.max, -task.priority, task.due_time or time.max)
            keyed.append(((group, *due, order), task))
        fetched_at = datetime.fromisoformat(raw["fetched_at"])
    except (KeyError, TypeError, ValueError) as e:
        raise CatchupError(
            f"cannot read the Todoist tasks: {e!r}; press R to load them again"
        ) from e
    return Tasks([task for _, task in sorted(keyed, key=lambda pair: pair[0])], fetched_at)


def _task(data: dict[str, Any], project: str, section: str | None) -> Task:
    due = data.get("due") or {}
    due_date, due_time = parse_due(due.get("date"))
    return Task(
        id=data["id"],
        title=data["content"],
        description=data.get("description") or "",
        priority=data.get("priority", 1),
        due_date=due_date,
        due_time=due_time,
        due_string=due.get("string") or "",
        recurring=bool(due.get("isRecurring")),
        project=project,
        section=section,
        labels=tuple(data.get("labels") or ()),
        url=data.get("url") or "",
    )


def parse_due(value: str | None) -> tuple[date | None, time | None]:
    """Return the local date and time of a due value: a date, a floating time, or a UTC time."""
    if not value:
        return None, None
    if "T" not in value:
        return date.fromisoformat(value), None
    moment = datetime.fromisoformat(value)
    # A time with a zone is fixed; one without is floating, in whatever zone the user is in.
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    return moment.date(), moment.time()


def due_value(task: Task, until: datetime, now: datetime) -> str:
    """Return the date `td task reschedule` takes for moving `task` to `until`.

    A pick within a day that is not a midnight, such as "in 3 hours", keeps its time. Any other
    pick names a day, and the task keeps its own time of day on it, if it has one.
    """
    local = until.astimezone()
    if local.time() != time() and until - now < timedelta(days=1):
        return f"{local:%Y-%m-%dT%H:%M}:00"
    if task.due_time is not None:
        return f"{local.date().isoformat()}T{task.due_time:%H:%M}:00"
    return local.date().isoformat()


async def complete(command: Sequence[str], task_id: str) -> None:
    """Complete the task; a recurring task moves on to its next date."""
    await _run(command, "task", "complete", f"id:{task_id}")


async def reschedule(command: Sequence[str], task_id: str, due: str) -> None:
    """Move the task to `due`, a date or a date and time from `due_value`, keeping its recurrence."""
    await _run(command, "task", "reschedule", f"id:{task_id}", due)


async def login(command: Sequence[str]) -> None:
    """Sign td in to Todoist; td opens the browser and waits for the user to allow it."""
    await _run(command, "auth", "login", "--json")


def cache_path() -> Path:
    """Return the file that keeps the last task list: next to the database."""
    return default_db_path().parent / "todoist.json"


def read_cache() -> dict[str, Any] | None:
    """Return the task list td last returned, or None when there is none to read."""
    try:
        return json.loads(cache_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def write_cache(raw: dict[str, Any]) -> None:
    """Keep `raw` as the last task list, replacing the file in one step."""
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(raw), encoding="utf-8")
    temporary.replace(path)


async def _json(command: Sequence[str], *args: str) -> dict[str, Any]:
    output = await _run(command, *args)
    try:
        return json.loads(output)
    except ValueError:
        raise CatchupError(
            f"td {args[0]} {args[1]} printed no JSON; check that {command[0]} is the Todoist CLI"
        ) from None


async def _run(command: Sequence[str], *args: str) -> str:
    """Run td with `args` and return what it printed; raise CatchupError on failure."""
    if not shutil.which(command[0]):
        raise NotInstalled(
            f"cannot run {command[0]}: command not found; install the Todoist CLI"
            f" with {INSTALL_COMMAND}"
        )
    result = await run_command([*command, *args], os.environ)
    if result.returncode == 0:
        return result.stdout
    code, message = _error(result.stderr or result.stdout)
    if code in _SIGNED_OUT:
        raise SignedOut(f"td is not signed in to Todoist: {message}")
    raise CatchupError(f"td {args[0]} {args[1]} failed: {message}")


def _error(output: str) -> tuple[str | None, str]:
    """Return the code and the message of a td error; with --json, td prints them as JSON."""
    try:
        error = json.loads(output)["error"]
        return error.get("code"), error.get("message") or "no message"
    except (ValueError, KeyError, TypeError, AttributeError):
        return None, output.strip() or "no output"
