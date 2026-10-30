import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

# A stand-in for the Todoist CLI: it records each call, prints the reply set for its first two
# arguments, or fails with the error set for them or for every call. It never reaches Todoist.
_SCRIPT = """#!/bin/sh
dir=$(dirname "$0")
printf '%s\\n' "$*" >> "$dir/calls"
name="$1-$2"
for err in "$dir/$name.err" "$dir/all.err"; do
    [ -f "$err" ] && { cat "$err" >&2; exit 1; }
done
[ -f "$dir/$name.out" ] && cat "$dir/$name.out"
exit 0
"""


def task(id: str, title: str, project: str, day: date, **fields: Any) -> dict[str, Any]:
    """Return a task as `td task list --json` prints it."""
    data: dict[str, Any] = {
        "id": id,
        "content": title,
        "description": "",
        "priority": 1,
        "due": {"isRecurring": False, "string": "", "date": day.isoformat(), "lang": "en"},
        "projectId": project,
        "sectionId": None,
        "parentId": None,
        "labels": [],
        "url": f"https://app.todoist.com/app/task/{id}",
    }
    return {**data, **fields}


class StandIn:
    """A stand-in td in its own directory, with replies that tests can change."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir()
        path = directory / "td"
        path.write_text(_SCRIPT)
        path.chmod(0o755)
        self.command = (str(path),)
        today = date.today()
        self.reply(
            "task-list",
            [
                task("w1", "Deploy the fix", "work", today, priority=2, sectionId="s1"),
                task(
                    "i1",
                    "Pay the rent",
                    "inbox",
                    today - timedelta(days=2),
                    priority=4,
                    labels=["home", "money"],
                    description="Transfer **before** noon.",
                ),
                task(
                    "i2",
                    "Water the plants",
                    "inbox",
                    today,
                    due={
                        "isRecurring": True,
                        "string": "every day at 15:00",
                        "date": f"{today.isoformat()}T15:00:00",
                        "lang": "en",
                    },
                ),
            ],
        )
        self.reply(
            "project-list",
            [
                {"id": "work", "name": "Work", "inboxProject": False},
                {"id": "inbox", "name": "Inbox", "inboxProject": True},
            ],
        )
        self.reply("section-list", [{"id": "s1", "name": "Ops", "projectId": "work"}])

    def reply(self, name: str, results: list[dict[str, Any]]) -> None:
        """Make `td <name>` print `results` the way td prints a list."""
        (self.directory / f"{name}.out").write_text(
            json.dumps({"results": results, "nextCursor": None})
        )

    def fail(self, name: str, code: str, message: str) -> None:
        """Make `td <name>` fail with a td error; `all` fails every call."""
        error = {"error": {"code": code, "message": message}}
        (self.directory / f"{name}.err").write_text(json.dumps(error))

    def heal(self, name: str) -> None:
        """Take back what `fail` set for `name`."""
        (self.directory / f"{name}.err").unlink()

    def calls(self) -> list[str]:
        """Return the arguments of each call so far, one string per call."""
        try:
            return (self.directory / "calls").read_text().splitlines()
        except FileNotFoundError:
            return []


@pytest.fixture
def td(tmp_path: Path) -> StandIn:
    return StandIn(tmp_path / "td-bin")
