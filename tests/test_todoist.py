from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from conftest import StandIn

from catchup import CatchupError, todoist


async def test_fetch_lists_the_inbox_first_then_projects_with_their_sections(
    td: StandIn,
) -> None:
    tasks = todoist.parse(await todoist.fetch(td.command, "today | overdue"))
    assert [(t.title, t.project, t.section) for t in tasks.tasks] == [
        ("Pay the rent", "Inbox", None),
        ("Water the plants", "Inbox", None),
        ("Deploy the fix", "Work", "Ops"),
    ]
    rent, plants, fix = tasks.tasks
    assert (rent.p, fix.p, plants.p) == (1, 3, 4)
    assert (plants.recurring, plants.due_time, plants.due_string) == (
        True,
        time(15),
        "every day at 15:00",
    )
    assert rent.labels == ("home", "money")
    assert sorted(td.calls()) == [
        "project list --json --all --full",
        "section list --project id:work --json --all",
        "task list --filter today | overdue --json --all",
    ]


async def test_fetch_tells_signed_out_from_other_failures(td: StandIn) -> None:
    td.fail("task-list", "API_ERROR", "Bad filter")
    with pytest.raises(CatchupError, match="td task list failed: Bad filter") as failed:
        await todoist.fetch(td.command, "today")
    assert not isinstance(failed.value, todoist.SignedOut)

    td.heal("task-list")
    td.fail("all", "NO_TOKEN", "No API token found.")
    with pytest.raises(todoist.SignedOut, match="not signed in"):
        await todoist.fetch(td.command, "today")


async def test_output_that_is_not_a_td_list_is_an_error(td: StandIn) -> None:
    (td.directory / "project-list.out").write_text('{"results": "none"}')
    with pytest.raises(CatchupError, match="td project list printed no list of results"):
        await todoist.fetch(td.command, "today")


async def test_a_missing_td_says_how_to_install_it(tmp_path: Path) -> None:
    with pytest.raises(todoist.NotInstalled, match="npm install -g @doist/todoist-cli"):
        await todoist.complete((str(tmp_path / "td"),), "i1")


async def test_actions_name_the_task_by_id(td: StandIn) -> None:
    await todoist.complete(td.command, "i1")
    await todoist.reschedule(td.command, "i1", "2026-11-02")
    await todoist.login(td.command)
    assert td.calls() == [
        "task complete id:i1",
        "task reschedule id:i1 2026-11-02",
        "auth login --json",
    ]


def test_a_timed_due_date_with_a_zone_is_shown_in_local_time() -> None:
    raw = {
        "fetched_at": datetime.now(UTC).isoformat(),
        "projects": [],
        "sections": [],
        "tasks": [{"id": "1", "content": "Call", "due": {"date": "2026-11-02T14:00:00Z"}}],
    }
    local = datetime(2026, 11, 2, 14, tzinfo=UTC).astimezone()
    [call] = todoist.parse(raw).tasks
    assert (call.due_date, call.due_time, call.project) == (local.date(), local.time(), "Other")


def test_parse_rejects_a_broken_cache() -> None:
    with pytest.raises(CatchupError, match="press R"):
        todoist.parse({"tasks": [{"content": "no id"}]})


def _task(due_time: time | None) -> todoist.Task:
    return todoist.Task(
        "1", "Call", "", 1, date(2026, 11, 2), due_time, "", False, "Inbox", None, (), ""
    )


@pytest.mark.parametrize(
    ("until", "due_time", "expected"),
    [
        # In 3 hours keeps the time it names.
        (timedelta(hours=3), None, "{now:%Y-%m-%dT%H:%M}:00"),
        # A day keeps the task's own time, or none.
        (timedelta(days=3), time(9, 30), "{in3:%Y-%m-%d}T09:30:00"),
        (timedelta(days=3), None, "{in3:%Y-%m-%d}"),
    ],
)
def test_due_value_keeps_the_task_time_on_a_picked_day(
    until: timedelta, due_time: time | None, expected: str
) -> None:
    now = datetime(2026, 10, 30, 10, 15, tzinfo=UTC)
    value = todoist.due_value(_task(due_time), now + until, now)
    local = (now + until).astimezone()
    assert value == expected.format(now=local, in3=local)


def test_due_value_of_a_midnight_is_a_day() -> None:
    now = datetime.now(UTC)
    midnight = datetime.combine(date.today() + timedelta(days=1), time()).astimezone()
    assert todoist.due_value(_task(None), midnight, now) == midnight.date().isoformat()


def test_cache_sits_next_to_the_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CATCHUP_DB", str(tmp_path / "data" / "catchup.db"))
    assert todoist.read_cache() is None
    todoist.write_cache({"tasks": []})
    assert todoist.read_cache() == {"tasks": []}
    assert todoist.cache_path() == tmp_path / "data" / "todoist.json"
