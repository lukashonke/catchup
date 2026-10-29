import asyncio
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from textual.geometry import Region
from textual.widgets import Input

from catchup.config import Config, load_config
from catchup.store import Kind, Link, Priority, Store, utc_now
from catchup.tui.app import CatchupApp
from catchup.tui.dialogs import NoteEditor, SnoozePicker
from catchup.tui.settings import Settings
from catchup.tui.widgets import Details, ItemList
from catchup.when import parse_until


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "catchup.db"
    monkeypatch.setenv("CATCHUP_DB", str(path))
    return path


def add(
    path: Path,
    kind: Kind,
    title: str,
    days: float,
    priority: Priority = "normal",
    body: str | None = None,
) -> str:
    """Add an item created `days` ago and return its id as the list uses it."""
    with Store(path, clock=lambda: utc_now() - timedelta(days=days)) as store:
        return str(store.add(kind, title, priority=priority, body=body).id)


def ids(app: CatchupApp) -> list[str | None]:
    """The list rows as item ids, with None for section headers, blank lines and hints."""
    return [option.id for option in app.query_one(ItemList).options]


def text(app: CatchupApp, selector: str) -> str:
    """The text a widget shows on screen, one line per row, without trailing spaces."""
    widget = app.query_one(selector)
    strips = widget.render_lines(Region(0, 0, *widget.region.size))
    return "\n".join(strip.text.rstrip() for strip in strips).strip()


async def test_open_tab_lists_waiting_items_then_the_feed(db: Path) -> None:
    normal_old = add(db, "waiting", "Normal old", 5)
    fyi_old = add(db, "fyi", "FYI old", 4)
    low = add(db, "waiting", "Low", 3, "low")
    urgent = add(db, "waiting", "Urgent", 1, "urgent")
    fyi_new = add(db, "fyi", "FYI new", 0.5)
    with Store(db) as store:
        store.done(int(add(db, "waiting", "Closed", 2)))

    app = CatchupApp(Config())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert ids(app) == [None, urgent, normal_old, low, None, None, fyi_new, fyi_old]
        assert "3 waiting · 1 urgent" in text(app, "#summary")
        assert "Open 3" in text(app, "#tabs") and "Done 1" in text(app, "#tabs")


async def test_selecting_an_item_shows_its_details(db: Path) -> None:
    add(db, "waiting", "First", 2)
    add(db, "waiting", "Review the rate limiter", 1, body="Tomás asks for a **review**.")

    app = CatchupApp(Config())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("down")
        await app.workers.wait_for_complete()
        assert text(app, "#d-title") == "Review the rate limiter"
        assert text(app, "#d-meta").startswith("open  · first seen")
        assert "PROMPT (DEFAULT)" in text(app, "#d-prompt-label")
        assert text(app, "#d-prompt").endswith("Help me handle this.")
        assert text(app, "#d-history").split()[-2:] == ["agent", "created"]


async def test_done_moves_the_item_to_the_done_tab_as_a_user_event(db: Path) -> None:
    item_id = add(db, "waiting", "Answer Ken", 1)
    other = add(db, "waiting", "Approve the budget", 0.5)

    app = CatchupApp(Config())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.click("#done")
        await app.workers.wait_for_complete()
        assert ids(app) == [None, other]
        assert app.query_one(ItemList).selected_id == int(other)

        await app.show_tab("done")
        assert ids(app) == [item_id]

    with Store(db) as store:
        last = store.history(int(item_id))[-1]
    assert (last.action, last.actor) == ("done", "user")


async def test_enter_opens_the_selected_item(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item_id = add(db, "waiting", "Answer Ken", 1)
    with Store(db) as store:
        store.mark_seen(int(item_id))
    open_item = AsyncMock()
    monkeypatch.setattr("catchup.tui.app.open_item", open_item)
    config = Config(open_command=["/opt/open.sh"])

    app = CatchupApp(config)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await app.workers.wait_for_complete()
        assert "opened just now" in text(app, "#d-meta")
        assert text(app, "#list").splitlines()[1].split() == ["↗", "Answer", "Ken", "1d"]
    assert open_item.await_args is not None
    item, used_config = open_item.await_args.args
    assert (item.title, used_config) == ("Answer Ken", config)
    with Store(db) as store:
        last = store.history(item.id)[-1]
    assert (last.action, last.actor) == ("opened", "user")


async def test_narrow_terminal_puts_details_below_the_list(db: Path) -> None:
    add(db, "waiting", "Answer Ken", 1)

    app = CatchupApp(Config())
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        item_list, details = app.query_one("#list").region, app.query_one("#details").region
        assert details.y >= item_list.bottom
        assert details.x == item_list.x and details.width == item_list.width == 90

        await pilot.resize_terminal(140, 40)
        await pilot.pause()
        item_list, details = app.query_one("#list").region, app.query_one("#details").region
        assert details.x >= item_list.right and details.y == item_list.y


async def test_tiny_terminal_wraps_buttons_and_truncates_titles_at_a_word(db: Path) -> None:
    add(db, "waiting", "The staging database moves to the new cluster on Thursday", 1)

    app = CatchupApp(Config())
    async with app.run_test(size=(50, 40)) as pilot:
        await pilot.pause()
        main, more = app.query_one("#actions-main").region, app.query_one("#actions-more").region
        assert more.y > main.y
        details = app.query_one("#details").region
        assert all(button.region.right <= details.right for button in app.query("Button"))

        row = text(app, "#list").splitlines()[1]
        assert "…" in row and " …" not in row


async def test_poll_shows_new_items_and_keeps_the_selection(db: Path) -> None:
    add(db, "waiting", "Answer Ken", 2)
    selected = add(db, "waiting", "Approve the budget", 1)

    app = CatchupApp(Config())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("down")
        urgent = add(db, "waiting", "Fix the outage", 0, "urgent")
        await app.poll()
        assert ids(app)[1] == urgent
        assert app.query_one(ItemList).selected_id == int(selected)


async def test_closing_while_a_store_read_is_in_flight_does_not_fail(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(db, "waiting", "Answer Ken", 2)
    add(db, "waiting", "Approve the budget", 1)

    app = CatchupApp(Config())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        details = app.query_one(Details)
        run = app.store.run

        async def run_until_shutdown(call: Callable[[Store], object]) -> object:
            result = await run(call)
            while details.is_attached:
                await asyncio.sleep(0)
            return result

        # Textual removes the widgets before it stops the app's timers and workers; holding
        # each read until then hits that window every time instead of now and then.
        monkeypatch.setattr(app.store, "run", run_until_shutdown)
        await pilot.press("down")
        app.run_worker(app.poll())


async def test_keys_close_items_and_switch_tabs(db: Path) -> None:
    done = add(db, "waiting", "Answer Ken", 2)
    ignored = add(db, "waiting", "Approve the budget", 1)
    fyi = add(db, "fyi", "Release shipped", 0)

    app = CatchupApp(Config())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("space")
        await app.workers.wait_for_complete()
        await pilot.press("backspace")
        await app.workers.wait_for_complete()
        # The FYI is selected now, and FYIs have no state to change.
        await pilot.press("space")
        await app.workers.wait_for_complete()
        assert ids(app) == [None, None, None, None, fyi]

        await pilot.press("left", "right", "right")
        await app.workers.wait_for_complete()
        assert app.tab == "done" and ids(app) == [done]
        await pilot.press("right", "right")
        await app.workers.wait_for_complete()
        assert app.tab == "ignored" and ids(app) == [ignored]


async def test_ctrl_enter_opens_the_first_link(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with Store(db) as store:
        store.add("waiting", "Review !412", links=[Link("!412", "https://example.com/mr/412")])

    app = CatchupApp(Config(open_label="Open in Codex"))
    async with app.run_test(size=(140, 40)) as pilot:
        opened: list[str] = []
        monkeypatch.setattr(app, "open_url", opened.append)
        await pilot.pause()
        await pilot.press("ctrl+enter")
        assert opened == ["https://example.com/mr/412"]
        assert "Open in Codex" in text(app, "#open")


async def test_settings_save_to_the_config_and_take_effect(
    db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    add(db, "waiting", "Answer Ken", 1)

    app = CatchupApp(Config())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        app.screen.query_one("#open_label", Input).value = "Open in Codex"
        app.screen.query_one("#agent_every", Input).value = "2h"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert "Open in Codex" in text(app, "#open")
        assert "enter  Open in Codex" in text(app, "#footer")

    saved = load_config()
    assert (saved.open_label, saved.agent_every, saved.agent_prompt) == (
        "Open in Codex",
        "2h",
        None,
    )


async def test_settings_refuse_a_bad_interval(
    db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    app = CatchupApp(Config())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        app.screen.query_one("#agent_every", Input).value = "daily"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, Settings)
    assert not (tmp_path / "config").exists()


async def test_c_starts_the_agent_run(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("catchup.agent.is_due", lambda config, now: False)
    start = AsyncMock(return_value=None)
    monkeypatch.setattr("catchup.agent.start", start)

    app = CatchupApp(Config(agent_prompt="Sync my inbox."))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("r")
        await app.workers.wait_for_complete()
        assert start.await_count == 0
        await pilot.press("c")
        await app.workers.wait_for_complete()
        assert start.await_count == 1
        assert "c  catch up now" in text(app, "#footer")


async def test_u_undoes_the_latest_state_change(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = add(db, "waiting", "Answer Ken", 2)
    second = add(db, "waiting", "Approve the budget", 1)
    until = utc_now() + timedelta(days=2)
    with Store(db) as store:
        store.snooze(int(second), until)

    app = CatchupApp(Config())
    async with app.run_test(size=(140, 40)) as pilot:
        notices: list[str | None] = []
        monkeypatch.setattr(app, "notify", lambda message, title=None, **_: notices.append(title))
        await pilot.pause()
        await pilot.press("space")
        await app.workers.wait_for_complete()
        await app.show_tab("snoozed")
        await pilot.press("backspace")
        await app.workers.wait_for_complete()

        await pilot.press("u")
        await app.workers.wait_for_complete()
        assert ids(app) == [second]
        await app.show_tab("open")
        await pilot.press("u")
        await app.workers.wait_for_complete()
        assert ids(app) == [None, first]
        await pilot.press("u")
        assert notices[-3:] == ["Undone", "Undone", None]

    with Store(db) as store:
        restored = store.get(int(second))
        last = store.history(int(first))[-1]
    assert restored.snoozed_until == until.replace(microsecond=0)
    assert (last.action, last.actor, last.note) == ("reopened", "user", "undo")


async def test_z_picks_a_snooze_time_or_takes_a_typed_one(db: Path) -> None:
    picked = add(db, "waiting", "Answer Ken", 2)
    typed = add(db, "waiting", "Approve the budget", 1)
    fyi = add(db, "fyi", "Release shipped", 0)

    app = CatchupApp(Config())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("z")
        assert isinstance(app.screen, SnoozePicker)
        await pilot.press("escape")
        assert not isinstance(app.screen, SnoozePicker)

        await pilot.press("z", "2")
        await app.workers.wait_for_complete()
        await pilot.press("z", "tab", "4", "8", "h", "enter")
        await app.workers.wait_for_complete()
        # Only the FYI is left, and FYIs cannot be snoozed.
        await pilot.press("z")
        assert not isinstance(app.screen, SnoozePicker)
        assert ids(app) == [None, None, None, None, fyi]

    with Store(db) as store:
        tomorrow = parse_until("tomorrow", utc_now())
        assert store.get(int(picked)).snoozed_until == tomorrow
        until = store.get(int(typed)).snoozed_until
    assert until is not None and timedelta(hours=47) < until - utc_now() <= timedelta(hours=48)


async def test_snooze_picker_keeps_a_bad_time_open(db: Path) -> None:
    add(db, "waiting", "Answer Ken", 2)

    app = CatchupApp(Config())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("z", "tab", *"2020-01-01", "enter")
        await pilot.pause()
        assert isinstance(app.screen, SnoozePicker)
        assert app.screen.query_one(Input).value == "2020-01-01"


async def test_n_adds_a_note_as_the_user(db: Path) -> None:
    fyi = add(db, "fyi", "Release shipped", 0)

    app = CatchupApp(Config())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("n", "escape")
        await pilot.press("n", "space", "ctrl+s")
        assert not isinstance(app.screen, NoteEditor)
        await pilot.press("n", *"Close when Tomas replies", "ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "note: Close when Tomas replies" in text(app, "#d-history")

    with Store(db) as store:
        assert [(e.actor, e.action, e.note) for e in store.history(int(fyi))] == [
            ("agent", "created", None),
            ("user", "note", "Close when Tomas replies"),
        ]


async def test_new_items_get_a_dot_until_selected_for_a_moment(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("catchup.tui.app.SEEN_SECONDS", 0.2)
    seen = add(db, "waiting", "Answer Ken", 2)
    add(db, "waiting", "Approve the budget", 1)
    add(db, "fyi", "Release shipped", 0)
    with Store(db) as store:
        store.mark_seen(int(seen))

    app = CatchupApp(Config())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert "· 2 new" in text(app, "#summary")
        rows = text(app, "#list").splitlines()
        assert [row.split()[0] for row in (rows[1], rows[2], rows[5])] == ["Answer", "•", "•"]

        # Passing over a row does not count as seeing it.
        await pilot.press("down", "down")
        await pilot.pause(0.4)
        await app.workers.wait_for_complete()
        assert "· 1 new" in text(app, "#summary")
        assert text(app, "#list").splitlines()[2].split()[0] == "•"


async def test_tab_moves_between_settings_fields(db: Path) -> None:
    app = CatchupApp(Config())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        first = app.focused
        await pilot.press("tab")
        assert app.focused is not first and isinstance(app.focused, Input)
