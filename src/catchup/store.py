"""The SQLite store of items and events; the only code that writes the database."""

# Postponed annotations: the `list` method would otherwise shadow the builtin in later hints.
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self, get_args

from catchup import CatchupError
from catchup.links import Link, resolve

Kind = Literal["waiting", "fyi"]
State = Literal["open", "snoozed", "done", "ignored"]
Priority = Literal["urgent", "normal", "low"]
Actor = Literal["agent", "user"]
Action = Literal["created", "updated", "done", "snoozed", "reopened", "ignored", "note"]

SCHEMA_VERSION = 1

_SCHEMA = (
    """
    CREATE TABLE items (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('waiting', 'fyi')),
        title TEXT NOT NULL,
        body TEXT,
        priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('urgent', 'normal', 'low')),
        links TEXT NOT NULL DEFAULT '[]',
        prompt TEXT,
        state TEXT CHECK (state IN ('open', 'snoozed', 'done', 'ignored')),
        snoozed_until TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE events (
        id INTEGER PRIMARY KEY,
        item_id INTEGER NOT NULL REFERENCES items (id),
        at TEXT NOT NULL,
        actor TEXT NOT NULL CHECK (actor IN ('agent', 'user')),
        action TEXT NOT NULL,
        note TEXT
    )
    """,
    "CREATE INDEX events_item_id ON events (item_id)",
)

# A snoozed item whose date has passed counts as open; this is the SQL form of that rule.
_EFFECTIVE_STATE = (
    "CASE WHEN i.state = 'snoozed' AND i.snoozed_until <= :now THEN 'open' ELSE i.state END"
)


@dataclass(frozen=True)
class Event:
    """One entry in an item's history: who did what to the item, and when."""

    id: int
    item_id: int
    at: datetime
    actor: Actor
    action: Action
    note: str | None


@dataclass(frozen=True)
class Item:
    """A waiting item or an FYI, with its effective state and its most recent event."""

    id: int
    kind: Kind
    title: str
    body: str | None
    priority: Priority
    links: tuple[Link, ...]
    prompt: str | None
    state: State | None
    snoozed_until: datetime | None
    created_at: datetime
    updated_at: datetime
    last_event: Event | None


def default_db_path() -> Path:
    """Return the database path from `$CATCHUP_DB`, else from `$XDG_DATA_HOME`, else `~/.local/share`."""
    if path := os.environ.get("CATCHUP_DB"):
        return Path(path)
    data_home = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(data_home) / "catchup" / "catchup.db"


def utc_now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


class Store:
    """Items and their events in one SQLite database, safe to share between processes."""

    def __init__(
        self, path: Path | None = None, *, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self.path = path or default_db_path()
        self._clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA foreign_keys = ON")
            self._migrate()
        except sqlite3.Error as e:
            self._db.close()
            raise CatchupError(f"cannot open the database at {self.path}: {e}") from e
        except CatchupError:
            self._db.close()
            raise

    def close(self) -> None:
        """Close the database connection."""
        self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def add(
        self,
        kind: Kind,
        title: str,
        *,
        body: str | None = None,
        priority: Priority = "normal",
        links: Sequence[Link] = (),
        prompt: str | None = None,
        note: str | None = None,
        actor: Actor = "agent",
    ) -> Item:
        """Create an item and its `created` event; waiting items start open, FYIs have no state."""
        _check(kind, Kind, "kind")
        _check(priority, Priority, "priority")
        _check(actor, Actor, "actor")
        _check_title(title)
        now = self._now()
        with self._write():
            item_id = self._db.execute(
                "INSERT INTO items (kind, title, body, priority, links, prompt, state,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    kind,
                    title,
                    body,
                    priority,
                    _links_json(links),
                    prompt,
                    "open" if kind == "waiting" else None,
                    _ts(now),
                    _ts(now),
                ),
            ).lastrowid
            assert item_id is not None
            self._event(item_id, now, actor, "created", note)
        return self.get(item_id)

    def update(
        self,
        item_id: int,
        *,
        title: str | None = None,
        body: str | None = None,
        priority: Priority | None = None,
        links: Sequence[Link] | None = None,
        prompt: str | None = None,
        note: str | None = None,
        actor: Actor = "agent",
    ) -> Item:
        """Change the given fields (None leaves a field as it is); with only a note, record a `note` event."""
        _check(actor, Actor, "actor")
        changes: dict[str, str | None] = {}
        if title is not None:
            _check_title(title)
            changes["title"] = title
        if body is not None:
            changes["body"] = body
        if priority is not None:
            _check(priority, Priority, "priority")
            changes["priority"] = priority
        if links is not None:
            changes["links"] = _links_json(links)
        if prompt is not None:
            changes["prompt"] = prompt
        if not changes and note is None:
            raise CatchupError(f"nothing to update on item {item_id}; pass a field or a note")
        now = self._now()
        with self._write():
            self._require(item_id)
            changes["updated_at"] = _ts(now)
            assignments = ", ".join(f"{column} = ?" for column in changes)
            self._db.execute(
                f"UPDATE items SET {assignments} WHERE id = ?", (*changes.values(), item_id)
            )
            self._event(item_id, now, actor, "updated" if len(changes) > 1 else "note", note)
        return self.get(item_id)

    def done(self, item_id: int, *, note: str | None = None, actor: Actor = "agent") -> Item:
        """Mark a waiting item done."""
        return self._set_state(item_id, "done", "done", None, note, actor)

    def ignore(self, item_id: int, *, note: str | None = None, actor: Actor = "agent") -> Item:
        """Mute a waiting item."""
        return self._set_state(item_id, "ignored", "ignored", None, note, actor)

    def reopen(self, item_id: int, *, note: str | None = None, actor: Actor = "agent") -> Item:
        """Make a waiting item open again."""
        return self._set_state(item_id, "open", "reopened", None, note, actor)

    def snooze(
        self,
        item_id: int,
        until: datetime,
        *,
        note: str | None = None,
        actor: Actor = "agent",
    ) -> Item:
        """Hide a waiting item until a future moment, after which it counts as open."""
        if until <= self._now():
            raise CatchupError(
                f"cannot snooze item {item_id} until {until:%Y-%m-%d %H:%M} UTC: that is in the past"
            )
        return self._set_state(item_id, "snoozed", "snoozed", until, note, actor)

    def get(self, item_id: int) -> Item:
        """Return one item with its effective state and last event."""
        rows = self._select("i.id = :id", {"id": item_id})
        if not rows:
            raise _not_found(item_id)
        return rows[0]

    def history(self, item_id: int) -> list[Event]:
        """Return all events of an item, oldest first."""
        self._require(item_id)
        rows = self._db.execute(
            "SELECT * FROM events WHERE item_id = ? ORDER BY id", (item_id,)
        ).fetchall()
        return [_event(row) for row in rows]

    def list(
        self,
        *,
        state: State | Literal["all"] = "open",
        kind: Kind | Literal["all"] = "waiting",
        since: datetime | None = None,
    ) -> list[Item]:
        """Return matching items: waiting ones by priority then oldest first, then FYIs newest first.

        FYIs have no state, so they match only the `open` and `all` state filters,
        the same way the TUI shows the feed under its Open tab.
        """
        where = ["1"]
        params: dict[str, object] = {}
        if kind != "all":
            where.append("i.kind = :kind")
            params["kind"] = kind
        if state == "open":
            where.append(f"(i.kind = 'fyi' OR {_EFFECTIVE_STATE} = 'open')")
        elif state != "all":
            where.append(f"{_EFFECTIVE_STATE} = :state")
            params["state"] = state
        if since is not None:
            where.append("i.updated_at >= :since")
            params["since"] = _ts(since)
        return self._select(" AND ".join(where), params)

    def last_agent_event_at(self) -> datetime | None:
        """Return when the agent last changed anything, or None if it never has."""
        at = self._db.execute("SELECT max(at) FROM events WHERE actor = 'agent'").fetchone()[0]
        return _dt(at) if at else None

    def last_event_id(self) -> int:
        """Return a number that grows with every change, so a reader can poll it cheaply."""
        return self._db.execute("SELECT coalesce(max(id), 0) FROM events").fetchone()[0]

    def _set_state(
        self,
        item_id: int,
        state: State,
        action: Action,
        until: datetime | None,
        note: str | None,
        actor: Actor,
    ) -> Item:
        _check(actor, Actor, "actor")
        now = self._now()
        with self._write():
            if self._require(item_id) == "fyi":
                raise CatchupError(
                    f"item {item_id} is an FYI, and FYIs have no state;"
                    " only waiting items can be marked done, ignored, reopened or snoozed"
                )
            self._db.execute(
                "UPDATE items SET state = ?, snoozed_until = ?, updated_at = ? WHERE id = ?",
                (state, _ts(until) if until else None, _ts(now), item_id),
            )
            self._event(item_id, now, actor, action, note)
        return self.get(item_id)

    def _select(self, where: str, params: dict[str, object]) -> list[Item]:
        rows = self._db.execute(
            f"""
            SELECT i.*, {_EFFECTIVE_STATE} AS effective_state,
                   e.id AS e_id, e.at AS e_at, e.actor AS e_actor,
                   e.action AS e_action, e.note AS e_note
            FROM items i
            LEFT JOIN events e ON e.id = (SELECT max(id) FROM events WHERE item_id = i.id)
            WHERE {where}
            ORDER BY i.kind = 'fyi',
                     CASE WHEN i.kind = 'waiting' THEN
                         CASE i.priority WHEN 'urgent' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END
                     END,
                     CASE WHEN i.kind = 'waiting' THEN i.id END,
                     i.id DESC
            """,
            {"now": _ts(self._now()), **params},
        ).fetchall()
        return [_item(row) for row in rows]

    def _require(self, item_id: int) -> Kind:
        row = self._db.execute("SELECT kind FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise _not_found(item_id)
        return row["kind"]

    def _event(
        self, item_id: int, at: datetime, actor: Actor, action: Action, note: str | None
    ) -> None:
        self._db.execute(
            "INSERT INTO events (item_id, at, actor, action, note) VALUES (?, ?, ?, ?, ?)",
            (item_id, _ts(at), actor, action, note),
        )

    @contextmanager
    def _write(self) -> Iterator[None]:
        # IMMEDIATE takes the write lock up front, so a concurrent writer waits on the
        # busy timeout instead of failing mid-transaction.
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def _migrate(self) -> None:
        with self._write():
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise CatchupError(
                    f"the database at {self.path} has schema version {version}, newer than"
                    f" this catchup understands ({SCHEMA_VERSION}); upgrade catchup"
                )
            if version == 0:
                for statement in _SCHEMA:
                    self._db.execute(statement)
                self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _now(self) -> datetime:
        return self._clock().astimezone(UTC).replace(microsecond=0)


def _check(value: str, allowed: object, name: str) -> None:
    choices = get_args(allowed)
    if value not in choices:
        raise CatchupError(f"invalid {name} {value!r}; use one of {', '.join(choices)}")


def _check_title(title: str) -> None:
    if not title.strip():
        raise CatchupError("the title is empty; give the item a one-line title")
    if "\n" in title:
        raise CatchupError("the title has more than one line; put the rest in the body")


def _not_found(item_id: int) -> CatchupError:
    return CatchupError(
        f"item {item_id} does not exist; run `catchup list --state all --kind all` to see items"
    )


def _links_json(links: Sequence[Link]) -> str:
    return json.dumps(
        [{"label": link.label, "url": link.url, "type": link.type} for link in map(resolve, links)]
    )


def _ts(moment: datetime) -> str:
    # One fixed format, so timestamps compare correctly as strings inside SQL.
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dt(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _event(row: sqlite3.Row) -> Event:
    return Event(
        row["id"], row["item_id"], _dt(row["at"]), row["actor"], row["action"], row["note"]
    )


def _item(row: sqlite3.Row) -> Item:
    state = row["effective_state"]
    last_event = None
    if row["e_id"] is not None:
        last_event = Event(
            row["e_id"], row["id"], _dt(row["e_at"]), row["e_actor"], row["e_action"], row["e_note"]
        )
    return Item(
        id=row["id"],
        kind=row["kind"],
        title=row["title"],
        body=row["body"],
        priority=row["priority"],
        links=tuple(Link(**link) for link in json.loads(row["links"])),
        prompt=row["prompt"],
        state=state,
        snoozed_until=_dt(row["snoozed_until"]) if state == "snoozed" else None,
        created_at=_dt(row["created_at"]),
        updated_at=_dt(row["updated_at"]),
        last_event=last_event,
    )
