"""Access to the store from the TUI without blocking its event loop."""

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TypeVar

from catchup.store import Item, State, Store

T = TypeVar("T")

TABS: tuple[State, ...] = ("open", "snoozed", "done", "ignored")


class StoreThread:
    """Runs store calls one at a time on a single thread, which owns the SQLite connection."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._store: Store | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="catchup-store")

    async def run(self, call: Callable[[Store], T]) -> T:
        """Run `call` with the store on the store thread and return its result."""
        return await asyncio.get_running_loop().run_in_executor(self._executor, self._call, call)

    def close(self) -> None:
        """Close the connection on its own thread and stop the thread."""
        self._executor.submit(lambda: self._store and self._store.close())
        self._executor.shutdown(wait=True)

    def _call(self, call: Callable[[Store], T]) -> T:
        if self._store is None:
            self._store = Store(self._path)
        return call(self._store)


@dataclass(frozen=True)
class Snapshot:
    """What the TUI shows for one state tab, read in one go."""

    event_id: int
    counts: dict[State, int]
    urgent: int
    waiting: list[Item]
    feed: list[Item]
    last_agent_at: datetime | None


def load(store: Store, tab: State) -> Snapshot:
    """Read the waiting items of every state tab, and the feed when the tab is Open."""
    # Read first, so a change that lands while loading shows up as a newer id on the next poll.
    event_id = store.last_event_id()
    waiting: dict[State, list[Item]] = {state: store.list(state=state) for state in TABS}
    return Snapshot(
        event_id=event_id,
        counts={state: len(items) for state, items in waiting.items()},
        urgent=sum(item.priority == "urgent" for item in waiting["open"]),
        waiting=waiting[tab],
        feed=store.list(kind="fyi") if tab == "open" else [],
        last_agent_at=store.last_agent_event_at(),
    )
