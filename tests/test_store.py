import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from catchup import CatchupError
from catchup.links import Link
from catchup.store import SCHEMA_VERSION, Store

T0 = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> Store:
    return Store(tmp_path / "catchup.db", clock=clock)


def test_add_waiting_item_starts_open_with_created_event(store: Store) -> None:
    item = store.add(
        "waiting",
        "Review pricing MR",
        priority="urgent",
        links=[Link("MR", "https://github.com/o/r/pull/1"), Link("Spec", "https://x.test", "doc")],
        note="asked in #pricing",
    )
    assert item.state == "open"
    assert item.links == (
        Link("MR", "https://github.com/o/r/pull/1", "MR"),
        Link("Spec", "https://x.test", "Doc"),
    )
    assert item.created_at == item.updated_at == T0
    assert item.last_event is not None
    assert (item.last_event.action, item.last_event.actor, item.last_event.note) == (
        "created",
        "agent",
        "asked in #pricing",
    )


def test_fyi_has_no_state_and_rejects_state_changes(store: Store) -> None:
    fyi = store.add("fyi", "Deploy finished")
    assert fyi.state is None
    for change in (store.done, store.ignore, store.reopen):
        with pytest.raises(CatchupError, match="is an FYI"):
            change(fyi.id)
    with pytest.raises(CatchupError, match="is an FYI"):
        store.snooze(fyi.id, T0 + timedelta(days=1))
    assert [e.action for e in store.history(fyi.id)] == ["created"]


def test_state_changes_record_events_with_actor(store: Store, clock: Clock) -> None:
    item = store.add("waiting", "Answer Jana")
    clock.now = T0 + timedelta(hours=1)
    assert store.done(item.id, note="replied").state == "done"
    assert store.reopen(item.id).state == "open"
    assert store.ignore(item.id, actor="user").state == "ignored"
    history = store.history(item.id)
    assert [(e.action, e.actor) for e in history] == [
        ("created", "agent"),
        ("done", "agent"),
        ("reopened", "agent"),
        ("ignored", "user"),
    ]
    assert history[1].note == "replied"
    assert store.get(item.id).updated_at == clock.now


def test_snooze_expires_on_read(store: Store, clock: Clock) -> None:
    item = store.add("waiting", "Decide on vendor")
    until = T0 + timedelta(days=1)
    snoozed = store.snooze(item.id, until)
    assert (snoozed.state, snoozed.snoozed_until) == ("snoozed", until)
    assert store.list() == []
    assert [i.id for i in store.list(state="snoozed")] == [item.id]

    clock.now = until
    expired = store.get(item.id)
    assert (expired.state, expired.snoozed_until) == ("open", None)
    assert [i.id for i in store.list()] == [item.id]
    assert store.list(state="snoozed") == []


def test_snooze_rejects_a_past_moment(store: Store) -> None:
    item = store.add("waiting", "x")
    with pytest.raises(CatchupError, match="in the past"):
        store.snooze(item.id, T0)


def test_update_changes_fields_and_replaces_links(store: Store) -> None:
    item = store.add("waiting", "Old", links=[Link("a", "https://a.test")])
    updated = store.update(
        item.id, title="New", priority="low", links=[Link("b", "https://slack.com/x")]
    )
    assert (updated.title, updated.priority) == ("New", "low")
    assert updated.links == (Link("b", "https://slack.com/x", "Slack"),)
    assert updated.last_event is not None and updated.last_event.action == "updated"


def test_update_with_only_a_note_records_a_note_event(store: Store) -> None:
    item = store.add("waiting", "x")
    noted = store.update(item.id, note="still waiting on legal")
    assert noted.last_event is not None
    assert (noted.last_event.action, noted.last_event.note) == ("note", "still waiting on legal")
    with pytest.raises(CatchupError, match="nothing to update"):
        store.update(item.id)


def test_list_orders_waiting_by_priority_then_age_then_fyis_newest_first(store: Store) -> None:
    normal_old = store.add("waiting", "normal old")
    fyi_old = store.add("fyi", "fyi old")
    low = store.add("waiting", "low", priority="low")
    urgent = store.add("waiting", "urgent", priority="urgent")
    normal_new = store.add("waiting", "normal new")
    fyi_new = store.add("fyi", "fyi new")
    store.done(store.add("waiting", "closed").id)

    assert [i.id for i in store.list(kind="all")] == [
        urgent.id,
        normal_old.id,
        normal_new.id,
        low.id,
        fyi_new.id,
        fyi_old.id,
    ]
    assert [i.id for i in store.list(kind="fyi")] == [fyi_new.id, fyi_old.id]
    assert [i.title for i in store.list(state="done", kind="all")] == ["closed"]


def test_list_since_filters_by_updated_at(store: Store, clock: Clock) -> None:
    old = store.add("waiting", "old")
    store.add("waiting", "quiet")
    clock.now = T0 + timedelta(days=5)
    store.update(old.id, note="poke")
    assert [i.id for i in store.list(since=T0 + timedelta(days=1))] == [old.id]


def test_missing_item_gives_a_clear_error(store: Store) -> None:
    with pytest.raises(CatchupError, match="item 42 does not exist"):
        store.get(42)
    with pytest.raises(CatchupError, match="item 42 does not exist"):
        store.done(42)


def test_invalid_values_are_rejected(store: Store) -> None:
    with pytest.raises(CatchupError, match="invalid priority"):
        store.add("waiting", "x", priority="high")  # type: ignore[arg-type]
    with pytest.raises(CatchupError, match="one line"):
        store.add("waiting", "two\nlines")


def test_change_detection_and_last_agent_event(store: Store, clock: Clock) -> None:
    assert (store.last_event_id(), store.last_agent_event_at()) == (0, None)
    item = store.add("waiting", "x")
    first = store.last_event_id()
    clock.now = T0 + timedelta(hours=2)
    store.done(item.id, actor="user")
    assert store.last_event_id() > first
    assert store.last_agent_event_at() == T0


def test_second_connection_sees_writes_and_schema_is_versioned(tmp_path: Path) -> None:
    path = tmp_path / "catchup.db"
    with Store(path) as writer, Store(path) as reader:
        writer.add("waiting", "shared")
        assert [i.title for i in reader.list()] == ["shared"]
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "catchup.db"
    Store(path).close()
    with sqlite3.connect(path) as db:
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(CatchupError, match="upgrade catchup"):
        Store(path)


def test_user_notes_are_only_the_notes_the_user_wrote(store: Store) -> None:
    first = store.add("waiting", "Answer Jana")
    second = store.add("fyi", "Deploy finished")
    store.update(first.id, note="the agent's note")
    store.update(first.id, note="close when Tomas replies", actor="user")
    store.done(first.id, note="replied", actor="user")
    store.update(second.id, note="ask Ken about it", actor="user")
    notes = store.user_notes()
    assert {item_id: [n.note for n in events] for item_id, events in notes.items()} == {
        first.id: ["close when Tomas replies"],
        second.id: ["ask Ken about it"],
    }


def test_mark_opened_records_a_user_event(store: Store, clock: Clock) -> None:
    item = store.add("fyi", "Deploy finished")
    assert item.opened_at is None
    clock.now = T0 + timedelta(hours=1)
    opened = store.mark_opened(item.id)
    assert opened.opened_at == opened.updated_at == clock.now
    assert opened.last_event is not None
    assert (opened.last_event.action, opened.last_event.actor) == ("opened", "user")


def test_an_item_is_new_until_seen_and_again_after_an_agent_change(
    store: Store, clock: Clock
) -> None:
    item = store.add("waiting", "Answer Jana")
    assert store.get(item.id).new
    clock.now = T0 + timedelta(minutes=5)
    store.mark_seen(item.id)
    seen = store.get(item.id)
    assert not seen.new
    assert seen.updated_at == T0 and [e.action for e in store.history(item.id)] == ["created"]

    clock.now = T0 + timedelta(minutes=10)
    store.update(item.id, note="the agent's note")
    store.update(item.id, note="the user's note", actor="user")
    store.done(item.id)
    assert not store.get(item.id).new
    store.update(item.id, title="Answer Jana today")
    assert store.get(item.id).new


def test_version_1_database_gains_seen_at_with_its_items_seen(tmp_path: Path) -> None:
    path = tmp_path / "catchup.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE items (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                body TEXT, priority TEXT NOT NULL DEFAULT 'normal', links TEXT NOT NULL DEFAULT '[]',
                prompt TEXT, state TEXT, snoozed_until TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL);
            CREATE TABLE events (id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL, at TEXT NOT NULL,
                actor TEXT NOT NULL, action TEXT NOT NULL, note TEXT);
            INSERT INTO items (kind, title, state, created_at, updated_at)
                VALUES ('waiting', 'old', 'open', '2026-10-01T09:00:00Z', '2026-10-01T09:00:00Z');
            INSERT INTO events (item_id, at, actor, action)
                VALUES (1, '2026-10-01T09:00:00Z', 'agent', 'created');
            PRAGMA user_version = 1;
            """
        )
    with Store(path, clock=lambda: T0) as store:
        assert not store.get(1).new
        assert store.add("waiting", "fresh").new
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 2
