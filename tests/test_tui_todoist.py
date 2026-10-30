import asyncio
import dataclasses
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from conftest import StandIn, task
from test_tui import add, text
from textual.pilot import Pilot
from textual.widgets import Checkbox, Markdown

from catchup import todoist
from catchup.config import Config, load_config
from catchup.store import Store
from catchup.tui.app import CatchupApp
from catchup.tui.dialogs import KeysHelp, NoteEditor, SnoozePicker
from catchup.tui.todoist import TaskList, TodoistView

pytestmark = pytest.mark.usefixtures("no_agent_runs")


def rows(app: CatchupApp) -> list[str | None]:
    """The task list rows as task ids, with None for project headers and blank lines."""
    return [option.id for option in app.query_one(TaskList).options]


async def settle(app: CatchupApp, pilot: Pilot[None]) -> None:
    """Wait for the workers, and for the ones they start, such as the read after an action."""
    for _ in range(4):
        await app.workers.wait_for_complete()
        await pilot.pause()


async def todoist_tab(app: CatchupApp, pilot: Pilot[None]) -> None:
    await pilot.pause()
    await pilot.press("left")
    await settle(app, pilot)


async def test_the_tab_is_off_by_default(db: Path) -> None:
    app = CatchupApp(Config())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("left")
        assert app.tab == "open" and "Todoist" not in text(app, "#tabs")


async def test_the_tab_lists_the_tasks_by_project_inbox_first(db: Path, td: StandIn) -> None:
    app = CatchupApp(Config(todoist=True, todoist_command=td.command))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert app.tab == "open"
        assert text(app, "#tabs").split()[:3] == ["Todoist", "│", "Open"]
        await todoist_tab(app, pilot)
        assert app.tab == "todoist"
        assert text(app, "#tabs").split()[:3] == ["Todoist", "3", "│"]
        assert rows(app) == [None, "i1", "i2", None, None, "w1"]
        lines = text(app, "#t-list").splitlines()
        assert lines[0].split() == ["INBOX", "2"]
        assert lines[1].split() == ["●", "Pay", "the", "rent", "@home", "@money", "2d", "overdue"]
        assert lines[2].split() == ["Water", "the", "plants", "↻", "15:00"]
        assert lines[4].split() == ["WORK", "1"]
        assert text(app, "#t-status").startswith("today | overdue · 3 tasks · updated")

        assert text(app, "#t-title") == "Pay the rent"
        assert text(app, "#t-meta") == "p1  · Inbox · @home @money"
        assert "2d overdue" in text(app, "#t-due")
        assert app.query_one("#t-body", Markdown).source == "Transfer **before** noon."
        assert text(app, "#t-link").replace("\xa0", " ") == "TODOIST app.todoist.com/app/task/i1"
        await pilot.press("down", "down")
        assert text(app, "#t-meta") == "p3  · Work › Ops"
        await pilot.press("up")
        assert "↻ every day at 15:00" in text(app, "#t-due")

        await pilot.press("right")
        await pilot.pause()
        assert app.tab == "open" and not app.query_one(TodoistView).display


async def test_the_last_list_shows_at_start_before_td_answers(db: Path, td: StandIn) -> None:
    config = Config(todoist=True, todoist_command=td.command)
    app = CatchupApp(config)
    async with app.run_test(size=(140, 40)) as pilot:
        await todoist_tab(app, pilot)
    assert todoist.cache_path().exists()

    td.fail("all", "API_ERROR", "Network down")
    app = CatchupApp(config)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert text(app, "#tabs").split()[:2] == ["Todoist", "3"]
        await todoist_tab(app, pilot)
        assert rows(app) == [None, "i1", "i2", None, None, "w1"]
        # td lists the tasks and the projects at once, so either can fail first.
        assert "failed: Network down" in text(app, "#t-status").partition("not updated: ")[2]


async def test_space_completes_at_once_and_a_read_meanwhile_does_not_bring_it_back(
    db: Path, td: StandIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    finish = asyncio.Event()
    completed: list[str] = []

    async def complete(command: object, task_id: str) -> None:
        completed.append(task_id)
        await finish.wait()

    monkeypatch.setattr("catchup.todoist.complete", complete)
    app = CatchupApp(Config(todoist=True, todoist_command=td.command))
    async with app.run_test(size=(140, 40)) as pilot:
        await todoist_tab(app, pilot)
        await pilot.press("space")
        assert rows(app) == [None, "i2", None, None, "w1"]
        await pilot.pause()
        assert completed == ["i1"]
        # td still lists it, as it would before the completion lands.
        await pilot.press("R")
        await pilot.pause(0.3)
        assert rows(app) == [None, "i2", None, None, "w1"]

        td.reply("task-list", [task("w1", "Deploy the fix", "work", date.today())])
        finish.set()
        await settle(app, pilot)
        assert rows(app) == [None, "w1"]
        assert text(app, "#tabs").split()[:2] == ["Todoist", "1"]


async def test_a_failed_action_is_reported_and_the_row_comes_back(
    db: Path, td: StandIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = CatchupApp(Config(todoist=True, todoist_command=td.command))
    notices: list[str | None] = []
    monkeypatch.setattr(app, "notify", lambda message, title=None, **_: notices.append(title))
    async with app.run_test(size=(140, 40)) as pilot:
        await todoist_tab(app, pilot)
        td.fail("task-complete", "NOT_FOUND", "Task not found")
        await pilot.press("space")
        await settle(app, pilot)
        assert rows(app) == [None, "i1", "i2", None, None, "w1"]
    assert "Not changed in Todoist" in notices
    assert "task complete id:i1" in td.calls()


async def test_z_reschedules_keeping_the_time_of_a_timed_task(db: Path, td: StandIn) -> None:
    app = CatchupApp(Config(todoist=True, todoist_command=td.command))
    async with app.run_test(size=(140, 40)) as pilot:
        await todoist_tab(app, pilot)
        await pilot.press("z")
        assert isinstance(app.screen, SnoozePicker)
        assert text(app, ".dialog-title") == "Reschedule to"
        await pilot.press("2")
        await settle(app, pilot)
        await pilot.press("down", "z", "2")
        await settle(app, pilot)
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    assert [call for call in td.calls() if "reschedule" in call] == [
        f"task reschedule id:i1 {tomorrow}",
        f"task reschedule id:i2 {tomorrow}T15:00:00",
    ]


async def test_enter_opens_the_task_in_todoist_and_a_opens_it_in_claude(
    db: Path, td: StandIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    open_item = AsyncMock()
    monkeypatch.setattr("catchup.tui.todoist.open_item", open_item)
    app = CatchupApp(Config(todoist=True, todoist_command=td.command))
    async with app.run_test(size=(140, 40)) as pilot:
        opened: list[str] = []
        monkeypatch.setattr(app, "open_url", opened.append)
        await todoist_tab(app, pilot)
        await pilot.press("enter", "ctrl+enter")
        assert opened == ["https://app.todoist.com/app/task/i1"] * 2
        await pilot.press("a")
        await settle(app, pilot)
    assert open_item.await_args is not None
    item = open_item.await_args.args[0]
    assert item.title == "Pay the rent"
    assert item.prompt == (
        "Pay the rent\n\nTransfer **before** noon.\n\n"
        "Todoist task: https://app.todoist.com/app/task/i1\n\nHelp me handle this."
    )


async def test_inbox_keys_do_nothing_on_the_todoist_tab(db: Path, td: StandIn) -> None:
    item_id = add(db, "waiting", "Answer Ken", 1)
    app = CatchupApp(Config(todoist=True, todoist_command=td.command))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("space")
        await app.workers.wait_for_complete()
        await todoist_tab(app, pilot)
        await pilot.press("backspace", "n", "u", "slash")
        await settle(app, pilot)
        assert not isinstance(app.screen, NoteEditor)
        assert not app.query_one("#filter-bar").display
        assert rows(app) == [None, "i1", "i2", None, None, "w1"]
        assert text(app, "#footer-keys").startswith("space  complete   enter  open in Todoist")
        await pilot.press("question_mark")
        assert isinstance(app.screen, KeysHelp)
        assert "reschedule to a time you pick" in text(app, "#keys")
    # u did not undo the done from the Open tab.
    with Store(db) as store:
        assert store.get(int(item_id)).state == "done"


async def test_a_missing_td_says_how_to_install_it_and_loads_once_it_is_there(
    db: Path, td: StandIn, tmp_path: Path
) -> None:
    config = Config(todoist=True, todoist_command=(str(tmp_path / "nowhere" / "td"),))
    app = CatchupApp(config)
    async with app.run_test(size=(140, 40)) as pilot:
        await todoist_tab(app, pilot)
        setup = text(app, "#t-setup")
        assert "This tab needs td, the Todoist CLI." in setup
        assert "npm install -g @doist/todoist-cli" in setup
        assert "loads once td is on the PATH" in setup
        assert text(app, "#tabs").split()[:2] == ["Todoist", "│"]

        app.config = dataclasses.replace(config, todoist_command=td.command)
        await pilot.press("R")
        await settle(app, pilot)
        assert not app.query_one("#t-setup").display
        assert rows(app)[1] == "i1"


async def test_signed_out_td_asks_for_l_which_signs_in_and_loads(db: Path, td: StandIn) -> None:
    td.fail("all", "NO_TOKEN", "No API token found.")
    app = CatchupApp(Config(todoist=True, todoist_command=td.command))
    async with app.run_test(size=(140, 40)) as pilot:
        await todoist_tab(app, pilot)
        assert "Press L to sign in." in text(app, "#t-setup")
        td.heal("all")
        await pilot.press("L")
        await settle(app, pilot)
        assert rows(app)[1] == "i1"
    assert "auth login --json" in td.calls()
    # Signed in, L does not start another login.
    assert td.calls().count("auth login --json") == 1


async def test_settings_turn_the_todoist_tab_on(db: Path) -> None:
    app = CatchupApp(Config())
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        # A click, so the test fails when the checkbox is hidden.
        await pilot.click("#todoist")
        assert app.screen.query_one("#todoist", Checkbox).value
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert text(app, "#tabs").split()[0] == "Todoist"
    assert load_config().todoist is True
