"""The Textual app: header, state tabs, the item list and the details pane."""

import sqlite3
from datetime import datetime
from typing import ClassVar

from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.content import Content
from textual.style import Style
from textual.widgets import Button, OptionList, Static

from catchup import CatchupError
from catchup.config import Config
from catchup.opener import open_item
from catchup.store import Item, State, Store, utc_now
from catchup.tui import theme
from catchup.tui.data import TABS, Snapshot, StoreThread, load
from catchup.tui.widgets import Details, ItemList, day, day_time
from catchup.when import parse_until

POLL_SECONDS = 2.0
NARROW_COLUMNS = 110
TINY_COLUMNS = 62
_CHANGED = {"done": "Done", "ignored": "Ignored", "open": "Reopened"}


class CatchupApp(App[None]):
    """Read the items the agent wrote and act on them."""

    CSS_PATH = "catchup.tcss"
    TITLE = "catchup"
    ENABLE_COMMAND_PALETTE = False
    HORIZONTAL_BREAKPOINTS = [  # noqa: RUF012
        (0, "-tiny"),
        (TINY_COLUMNS, "-narrow"),
        (NARROW_COLUMNS, "-wide"),
    ]
    BINDINGS: ClassVar = [Binding("ctrl+c", "quit", "Quit", show=False, priority=True)]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.register_theme(theme.THEME)
        self.theme = theme.THEME.name
        self.config = config
        self.store = StoreThread()
        self.tab: State = "open"
        self.snapshot: Snapshot | None = None
        self.item: Item | None = None
        # Ages and expired snoozes change with time alone, so the minute is part of the poll key.
        self._poll_key: tuple[int, int] | None = None
        self._poll_error: str | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="bar"):
            yield Static(id="summary")
            yield Static(id="last-run")
        yield Static(id="tabs")
        with Horizontal(id="panes"):
            yield ItemList(id="list")
            yield Details(id="details")
        yield Static(
            Content.from_markup(
                "[b $background on $dim] ↑↓ [/] select  [b $background on $dim] enter [/] open in"
                " Claude  [b $background on $dim] click [/] a link to open it"
                "  [b $background on $dim] ctrl+q [/] quit"
            ),
            id="footer",
        )

    async def on_mount(self) -> None:
        self.query_one(ItemList).focus()
        await self.reload()
        self.set_interval(POLL_SECONDS, self.poll)

    def on_unmount(self) -> None:
        self.store.close()

    async def poll(self) -> None:
        """Reload when the database changed or a minute passed."""
        try:
            event_id = await self.store.run(Store.last_event_id)
            if (event_id, utc_now().minute) != self._poll_key:
                await self.reload()
        except sqlite3.Error as e:
            # Report a lasting failure once, not on every poll.
            if str(e) != self._poll_error:
                self.notify(f"Cannot read the database: {e}", severity="error", markup=False)
            self._poll_error = str(e)
        else:
            self._poll_error = None

    async def reload(self) -> None:
        """Read the current tab from the store and redraw, keeping the selected item."""
        tab = self.tab
        snapshot = await self.store.run(lambda store: load(store, tab))
        # Textual removes the widgets before it stops the app's timers and workers,
        # so a read that finishes during shutdown has nothing left to show it in.
        if not self.is_running:
            return
        now = utc_now()
        self.snapshot = snapshot
        self._poll_key = (snapshot.event_id, now.minute)
        self._show_header(snapshot)
        selected = self.item.id if self.item else None
        self.query_one(ItemList).show(tab, snapshot.waiting, snapshot.feed, selected, now)
        self._show_selected()

    def _show_header(self, snapshot: Snapshot) -> None:
        summary = Content.assemble(
            ("catchup", f"bold {theme.ACCENT}"),
            "   ",
            (f"{snapshot.counts['open']} waiting", theme.FOREGROUND),
            (f" · {snapshot.urgent} urgent", theme.URGENT if snapshot.urgent else theme.DIM),
        )
        self.query_one("#summary", Static).update(summary)
        last = snapshot.last_agent_at
        last_run = f"last agent run {_time_and_day(last)}" if last else "the agent has not run yet"
        self.query_one("#last-run", Static).update(last_run)
        tabs = []
        for state in TABS:
            label = f" {state.capitalize()} {snapshot.counts[state]} "
            style = f"bold {theme.BACKGROUND} on {theme.ACCENT}" if state == self.tab else theme.DIM
            tabs.append(Content.styled(label, style).stylize(Style.from_meta({"tab": state})))
        self.query_one("#tabs", Static).update(Content(" ").join(tabs))

    def _show_selected(self) -> None:
        """Show the item that the list highlights in the details pane."""
        selected = self.query_one(ItemList).selected_id
        items = [*self.snapshot.waiting, *self.snapshot.feed] if self.snapshot else []
        self.item = next((item for item in items if item.id == selected), None)
        self._load_details(self.item)

    @work(exclusive=True, group="details")
    async def _load_details(self, item: Item | None) -> None:
        history = []
        if item is not None:
            item_id = item.id
            history = await self.store.run(lambda store: store.history(item_id))
            if not self.is_running:  # Shutting down; see reload().
                return
        self.query_one(Details).show(item, history, utc_now())

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self.item is None or event.option.id != str(self.item.id):
            self._show_selected()

    async def on_click(self, event: events.Click) -> None:
        # State tabs and link chips are spans of text that carry what they open in their style.
        meta = event.style.meta
        if "tab" in meta:
            await self.show_tab(meta["tab"])
        elif "link" in meta and self.item and meta["link"] < len(self.item.links):
            self.open_url(self.item.links[meta["link"]].url)

    async def show_tab(self, tab: State) -> None:
        """Switch to a state tab."""
        if tab != self.tab:
            self.tab = tab
            self.item = None
            await self.reload()
        self.query_one(ItemList).focus()

    def on_item_list_open_requested(self) -> None:
        self._open_in_claude()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        self.query_one(ItemList).focus()
        if self.item is None:
            return
        if event.button.id == "open":
            self._open_in_claude()
            return
        item_id = self.item.id
        actions = {
            "done": lambda store: store.done(item_id, actor="user"),
            "ignore": lambda store: store.ignore(item_id, actor="user"),
            "reopen": lambda store: store.reopen(item_id, actor="user"),
            "snooze-1d": lambda store: _snooze(store, item_id, "1d"),
            "snooze-3d": lambda store: _snooze(store, item_id, "3d"),
            "snooze-mon": lambda store: _snooze(store, item_id, "mon"),
        }
        try:
            item = await self.store.run(actions[str(event.button.id)])
        except (CatchupError, sqlite3.Error) as e:
            self.notify(str(e), title="Not changed", severity="error", markup=False)
            return
        await self.reload()
        if item.snoozed_until:
            self.notify(
                item.title, title=f"Snoozed until {day_time(item.snoozed_until)}", markup=False
            )
        else:
            self.notify(item.title, title=_CHANGED[str(item.state)], markup=False)

    @work(group="open")
    async def _open_in_claude(self) -> None:
        item = self.item
        if item is None:
            return
        # Titles and errors quote other people's messages, so they are never read as markup.
        self.notify(item.title, title="Opening in Claude", markup=False)
        try:
            await open_item(item, self.config)
        except CatchupError as e:
            self.notify(
                str(e), title="Open in Claude failed", severity="error", timeout=12, markup=False
            )


def _snooze(store: Store, item_id: int, until: str) -> Item:
    return store.snooze(item_id, parse_until(until, utc_now()), actor="user")


def _time_and_day(moment: datetime) -> str:
    return f"{moment.astimezone():%H:%M} · {day(moment)}"
