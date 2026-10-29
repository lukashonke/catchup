"""The Textual app: header, state tabs, the item list and the details pane."""

import sqlite3
from collections.abc import Callable
from datetime import datetime
from functools import partial
from typing import ClassVar, TypeGuard

from textual import events, on, work
from textual.actions import SkipAction
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.content import Content
from textual.screen import ModalScreen
from textual.style import Style
from textual.timer import Timer
from textual.widgets import Button, Input, OptionList, Static

from catchup import CatchupError, agent
from catchup.config import Config
from catchup.opener import open_item
from catchup.store import Item, State, Store, utc_now
from catchup.tui import theme
from catchup.tui.data import TABS, Snapshot, StoreThread, load
from catchup.tui.dialogs import NoteEditor, SnoozePicker
from catchup.tui.settings import Settings
from catchup.tui.widgets import Details, Filter, ItemList, day, day_time, matches
from catchup.when import parse_every, parse_until

POLL_SECONDS = 2.0
SEEN_SECONDS = 1.0
AGENT_CHECK_SECONDS = 30.0
NARROW_COLUMNS = 110
TINY_COLUMNS = 62
_CHANGED = {"done": "Done", "ignored": "Ignored", "open": "Reopened"}

# A change takes the store and the item id, and returns the changed item.
Change = Callable[[Store, int], Item]
_CHANGES: dict[str, Change] = {
    "done": lambda store, item_id: store.done(item_id, actor="user"),
    "ignore": lambda store, item_id: store.ignore(item_id, actor="user"),
    "reopen": lambda store, item_id: store.reopen(item_id, actor="user"),
    "snooze-1d": lambda store, item_id: _snooze(store, item_id, parse_until("1d", utc_now())),
    "snooze-3d": lambda store, item_id: _snooze(store, item_id, parse_until("3d", utc_now())),
    "snooze-mon": lambda store, item_id: _snooze(store, item_id, parse_until("mon", utc_now())),
}


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
    BINDINGS: ClassVar = [
        Binding("left", "tab(-1)", "Previous tab", show=False),
        Binding("right", "tab(1)", "Next tab", show=False),
        Binding("space", "change('done')", "Done", show=False),
        Binding("backspace", "change('ignore')", "Ignore", show=False),
        Binding("ctrl+enter", "open_link", "Open the first link", show=False),
        Binding("u", "undo", "Undo", show=False),
        Binding("z", "snooze", "Snooze", show=False),
        Binding("n", "note", "Note", show=False),
        Binding("slash", "filter", "Filter", show=False),
        Binding("escape", "clear_filter", "Clear the filter", show=False),
        Binding("c", "run_agent", "Catch up now", show=False),
        Binding("s", "settings", "Settings", show=False),
        Binding("q", "close", "Quit", show=False),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]

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
        self.agent_state = agent.RunState(started_at=None, running=False)
        # The items as they were before each state change made in this session, latest last.
        self._undo: list[Item] = []
        # The new item that counts as seen when its timer fires, unless the selection moves first.
        self._seen: tuple[int, Timer] | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="bar"):
            yield Static(id="summary")
            yield Static(id="last-run")
        yield Static(id="tabs")
        with Horizontal(id="filter-bar"):
            yield Static("Filter", id="filter-label")
            yield Filter(placeholder="title, body or link label", id="filter")
        with Horizontal(id="panes"):
            yield ItemList(id="list")
            yield Details(id="details")
        yield Static(id="footer")

    async def on_mount(self) -> None:
        self._show_config()
        self.query_one(ItemList).focus()
        await self.reload()
        self.set_interval(POLL_SECONDS, self.poll)
        self.set_interval(AGENT_CHECK_SECONDS, self.check_agent)
        await self.check_agent()

    def _show_config(self) -> None:
        """Show the parts of the screen that the settings change."""
        self.query_one("#open", Button).label = self.config.open_label
        keys = [
            ("←→", "tab"),
            ("enter", self.config.open_label),
            ("ctrl+enter", "link"),
            ("space", "done"),
            ("⌫", "ignore"),
            *([("c", "catch up now")] if self.config.agent_prompt else []),
            ("s", "settings"),
            ("q", "quit"),
        ]
        self.query_one("#footer", Static).update(
            Content("  ").join(
                Content.assemble((f" {key} ", f"bold {theme.INK} on {theme.DIM}"), f" {what}")
                for key, what in keys
            )
        )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # The inbox keys act only on the inbox: not under a dialog or while typing a filter.
        # Textual's own keys, such as tab between fields, and ctrl+c work everywhere.
        inbox = {binding.action.partition("(")[0] for binding in self.BINDINGS} - {"quit"}
        typing = isinstance(self.focused, Input)
        return action not in inbox or not (isinstance(self.screen, ModalScreen) or typing)

    def action_close(self) -> None:
        self.exit()

    async def check_agent(self) -> None:
        """Start the agent run when it is due, and show whether it runs."""
        try:
            if agent.is_due(self.config, utc_now()):
                self._run_agent()
            self.agent_state = agent.read_state()
        except (CatchupError, OSError) as e:
            self.notify(str(e), title="Agent run failed", severity="error", markup=False)
        if self.snapshot:
            self._show_header(self.snapshot)

    def action_run_agent(self) -> None:
        if not self.config.agent_prompt:
            raise SkipAction
        self._run_agent()

    @work(group="agent", exclusive=True)
    async def _run_agent(self) -> None:
        try:
            process = await agent.start(self.config, utc_now())
        except (CatchupError, OSError) as e:
            self.notify(str(e), title="Agent run failed", severity="error", markup=False)
            return
        if process is None:
            self.notify("The agent is already running.", markup=False)
            return
        self.notify("Running in the background.", title="Agent started", markup=False)
        await self.check_agent()
        code = await process.wait()
        if code != 0 and self.is_running:
            self.notify(
                f"It exited with code {code}. The output is in {agent.log_path()}.",
                title="Agent run failed",
                severity="error",
                timeout=20,
                markup=False,
            )
        if self.is_running:
            await self.check_agent()

    def action_settings(self) -> None:
        self.push_screen(Settings(self.config), self._settings_saved)

    async def _settings_saved(self, config: Config | None) -> None:
        if config is None:
            return
        self.config = config
        self._show_config()
        self.notify("Settings saved.", markup=False)
        await self.check_agent()

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
        self._show_list()

    def _show_list(self) -> None:
        """Show the rows of the snapshot that match the filter, keeping the selected item."""
        if self.snapshot is None:
            return
        query = self.query_one(Filter).value.strip()
        waiting = [item for item in self.snapshot.waiting if matches(item, query)]
        feed = [item for item in self.snapshot.feed if matches(item, query)]
        selected = self.item.id if self.item else None
        self.query_one(ItemList).show(self.tab, waiting, feed, selected, utc_now(), bool(query))
        self._show_selected()

    def _show_header(self, snapshot: Snapshot) -> None:
        summary = Content.assemble(
            ("catchup", f"bold {theme.ACCENT}"),
            "   ",
            (f"{snapshot.counts['open']} waiting", theme.FOREGROUND),
            (f" · {snapshot.urgent} urgent", theme.URGENT if snapshot.urgent else theme.DIM),
            (f" · {snapshot.new} new", theme.ACCENT if snapshot.new else theme.DIM),
        )
        self.query_one("#summary", Static).update(summary)
        last = snapshot.last_agent_at
        state = self.agent_state
        if state.running and state.started_at:
            last_run = f"agent running since {state.started_at.astimezone():%H:%M}"
        elif last:
            last_run = f"last agent run {_time_and_day(last)}"
        else:
            last_run = "the agent has not run yet"
        if self.config.agent_prompt and not state.running:
            due = (state.started_at or utc_now()) + parse_every(self.config.agent_every)
            last_run += f" · next {max(due, utc_now()).astimezone():%H:%M}"
        self.query_one("#last-run", Static).update(last_run)
        tabs = []
        for state in TABS:
            label = f" {state.capitalize()} {snapshot.counts[state]} "
            style = f"bold {theme.INK} on {theme.ACCENT}" if state == self.tab else theme.DIM
            tabs.append(Content.styled(label, style).stylize(Style.from_meta({"tab": state})))
        self.query_one("#tabs", Static).update(Content(" ").join(tabs))

    def _show_selected(self) -> None:
        """Show the item that the list highlights in the details pane."""
        selected = self.query_one(ItemList).selected_id
        items = [*self.snapshot.waiting, *self.snapshot.feed] if self.snapshot else []
        self.item = next((item for item in items if item.id == selected), None)
        self._load_details(self.item)
        self._watch_seen()

    def _watch_seen(self) -> None:
        """Mark the selected item seen once it stays selected for SEEN_SECONDS."""
        item = self.item
        if self._seen and (item is None or item.id != self._seen[0]):
            self._seen[1].stop()
            self._seen = None
        if item and item.new and self._seen is None:
            self._seen = (item.id, self.set_timer(SEEN_SECONDS, partial(self._mark_seen, item.id)))

    @work(group="seen")
    async def _mark_seen(self, item_id: int) -> None:
        self._seen = None
        try:
            await self.store.run(lambda store: store.mark_seen(item_id))
        except (CatchupError, sqlite3.Error):
            # The poll reports a broken database; a missed mark only keeps the dot.
            return
        # Seeing an item is not an event, so the poll would not notice it.
        if self.is_running:
            await self.reload()

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

    async def action_tab(self, step: int) -> None:
        index = TABS.index(self.tab) + step
        if 0 <= index < len(TABS):
            await self.show_tab(TABS[index])

    def action_filter(self) -> None:
        self.query_one("#filter-bar").display = True
        self.query_one(Filter).focus()

    @on(Input.Changed, "#filter")
    def _filter_changed(self) -> None:
        self._show_list()

    @on(Input.Submitted, "#filter")
    def _filter_submitted(self) -> None:
        # The bar stays while a filter is set, so the list never looks complete when it is not.
        self.query_one("#filter-bar").display = bool(self.query_one(Filter).value.strip())
        self.query_one(ItemList).focus()

    def on_filter_closed(self) -> None:
        self.action_clear_filter()

    def action_clear_filter(self) -> None:
        bar = self.query_one("#filter-bar")
        if not bar.display:
            raise SkipAction
        self.query_one(Filter).value = ""
        bar.display = False
        self.query_one(ItemList).focus()

    def action_open_link(self) -> None:
        if self.item and self.item.links:
            self.open_url(self.item.links[0].url)

    async def action_change(self, action: str) -> None:
        # The keys act only where the details pane shows the matching button.
        if _waiting_open(self.item):
            await self._change(self.item, _CHANGES[action])

    def action_snooze(self) -> None:
        if _waiting_open(self.item):
            # Bound to the item now, since a reload while the picker is open can move the selection.
            picked = partial(self._snooze_picked, self.item)
            self.push_screen(SnoozePicker(self.item.title), picked)

    async def _snooze_picked(self, item: Item, until: datetime | None) -> None:
        if until is not None:
            await self._change(item, lambda store, item_id: _snooze(store, item_id, until))

    def action_note(self) -> None:
        if self.item:
            self.push_screen(NoteEditor(self.item.title), partial(self._note_written, self.item))

    async def _note_written(self, item: Item, note: str | None) -> None:
        if note is None:
            return
        try:
            await self.store.run(lambda store: store.update(item.id, note=note, actor="user"))
        except (CatchupError, sqlite3.Error) as e:
            self.notify(str(e), title="Note not saved", severity="error", markup=False)
            return
        await self.reload()
        self.notify(item.title, title="Note added", markup=False)

    def on_item_list_open_requested(self) -> None:
        self._open_in_claude()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        self.query_one(ItemList).focus()
        if self.item is None:
            return
        if event.button.id == "open":
            self._open_in_claude()
        else:
            await self._change(self.item, _CHANGES[str(event.button.id)])

    async def _change(self, before: Item, change: Change) -> None:
        """Apply a state change to an item and report it."""
        try:
            item = await self.store.run(lambda store: change(store, before.id))
        except (CatchupError, sqlite3.Error) as e:
            self.notify(str(e), title="Not changed", severity="error", markup=False)
            return
        self._undo.append(before)
        await self.reload()
        if item.snoozed_until:
            self.notify(
                item.title, title=f"Snoozed until {day_time(item.snoozed_until)}", markup=False
            )
        else:
            self.notify(item.title, title=_CHANGED[str(item.state)], markup=False)

    async def action_undo(self) -> None:
        if not self._undo:
            self.notify("Nothing to undo.", markup=False)
            return
        before = self._undo[-1]
        try:
            self.item = await self.store.run(lambda store: _restore(store, before))
        except (CatchupError, sqlite3.Error) as e:
            self.notify(str(e), title="Not undone", severity="error", markup=False)
            return
        self._undo.pop()
        await self.reload()
        self.notify(before.title, title="Undone", markup=False)

    @work(group="open")
    async def _open_in_claude(self) -> None:
        item = self.item
        if item is None:
            return
        # Titles and errors quote other people's messages, so they are never read as markup.
        label = self.config.open_label
        self.notify(item.title, title=label, markup=False)
        try:
            await open_item(item, self.config)
        except CatchupError as e:
            self.notify(str(e), title=f"{label} failed", severity="error", timeout=12, markup=False)
            return
        try:
            await self.store.run(lambda store: store.mark_opened(item.id))
        except (CatchupError, sqlite3.Error) as e:
            self.notify(str(e), title="Not recorded as opened", severity="error", markup=False)
            return
        if self.is_running:
            await self.reload()


def _snooze(store: Store, item_id: int, until: datetime) -> Item:
    return store.snooze(item_id, until, actor="user")


def _waiting_open(item: Item | None) -> TypeGuard[Item]:
    return item is not None and item.kind == "waiting" and item.state in ("open", "snoozed")


def _restore(store: Store, item: Item) -> Item:
    """Put a waiting item back into the state it had in `item`, as the user's change."""
    if item.snoozed_until and item.snoozed_until > utc_now():
        return store.snooze(item.id, item.snoozed_until, note="undo", actor="user")
    change = {"done": store.done, "ignored": store.ignore}.get(str(item.state), store.reopen)
    return change(item.id, note="undo", actor="user")


def _time_and_day(moment: datetime) -> str:
    return f"{moment.astimezone():%H:%M} · {day(moment)}"
