"""The list of items and the details pane."""

from datetime import datetime, timedelta
from typing import Any, ClassVar

from rich.console import Console, ConsoleOptions, RenderResult
from rich.measure import Measurement
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.css.query import NoMatches
from textual.message import Message
from textual.style import Style
from textual.widgets import Button, Input, Label, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from catchup.context import Context
from catchup.opener import prompt_of
from catchup.store import Event, Item, State, utc_now
from catchup.tui import theme
from catchup.when import format_age

_MARKERS = {"urgent": "!", "low": "↓"}
_LINK_LABEL_LENGTH = 40
# The steps of a fetch that a context shows while it runs.
_SHOWN_STEPS = 8
_EMPTY = {
    "open": "Nothing is waiting on you.",
    "snoozed": "Nothing snoozed.",
    "done": "Nothing done yet.",
    "ignored": "Nothing ignored. Items you ignore stay muted here, even when new messages arrive.",
}


def day(moment: datetime) -> str:
    """Return a local date such as `Thu 29 Oct`."""
    local = moment.astimezone()
    return f"{local:%a} {local.day} {local:%b}"


def day_time(moment: datetime) -> str:
    """Return a local date and time such as `Thu 29 Oct 12:05`."""
    return f"{day(moment)} {moment.astimezone():%H:%M}"


def age_color(item: Item, now: datetime) -> str:
    """Return the color of an item's age: yellow after 3 days, red after 7, for waiting items."""
    days = (now - item.created_at).days
    if item.kind == "fyi" or days < 3:
        return theme.DIM
    return theme.URGENT if days >= 7 else theme.ACCENT


class _Line:
    """One line of text cut to the width it gets, so the ellipsis never follows a space."""

    def __init__(self, text: Text) -> None:
        self.text = text

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        text = self.text.copy()
        if text.cell_len > options.max_width:
            text.truncate(options.max_width - 1)
            text.rstrip()
            text.append("…")
        yield text


class KeyHints:
    """Keys and what they do, on one line that ends with the last hint that fits whole."""

    def __init__(self, keys: list[tuple[str, str]]) -> None:
        self.keys = keys

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        return Measurement(0, self._line(options.max_width).cell_len)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield self._line(options.max_width)

    def _line(self, width: int) -> Text:
        line = Text()
        for key, what in self.keys:
            hint = Text.assemble((f" {key} ", f"bold {theme.INK} on {theme.DIM}"), f" {what}")
            gap = "  " if line else ""
            if line.cell_len + len(gap) + hint.cell_len > width:
                break
            line.append(gap)
            line.append_text(hint)
        return line


def _context_mark(item: Item | None, context: Context | None, frame: str) -> Text:
    """Return the mark of a row's context; a row without `item` has no agent to change it."""
    if context is None:
        return Text("")
    if context.state == "fetching":
        return Text(frame, style=theme.ACCENT)
    if context.state == "failed":
        return Text("✕", style=theme.URGENT)
    if item and context.stale(item):
        return Text("◇", style=theme.DIM)
    return Text("◆", style=theme.ACCENT)


def _row(item: Item, now: datetime, context: Context | None = None, frame: str = "") -> Table:
    fyi = item.kind == "fyi"
    if fyi:
        marker = Text("·", style=theme.FAINT)
    else:
        marker = Text(
            _MARKERS.get(item.priority, ""),
            style=f"bold {theme.URGENT}" if item.priority == "urgent" else theme.FAINT,
        )
    link_type = item.links[0].type or "" if item.links else ""
    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(width=1, no_wrap=True)
    grid.add_column(width=1, no_wrap=True)
    grid.add_column(ratio=1)
    grid.add_column(width=8, no_wrap=True)
    grid.add_column(width=4, no_wrap=True, justify="right")
    grid.add_column(width=1, no_wrap=True)
    if item.new:
        status = Text("•", style=f"bold {theme.ACCENT}")
    else:
        status = Text("↗" if item.opened_at else "", style=theme.DIM)
    grid.add_row(
        status,
        marker,
        _Line(Text(item.title, style=theme.FEED if fyi else "")),
        Text(link_type, style=theme.LINK_COLORS.get(link_type, theme.DIM)),
        Text(format_age(now - item.created_at), style=age_color(item, now)),
        _context_mark(item, context, frame),
    )
    return grid


def _section(left: str, right: str) -> Table:
    grid = Table.grid(expand=True)
    grid.add_column(ratio=1)
    grid.add_column(justify="right")
    # Room for the rows' context mark, so the count lines up with their ages.
    grid.add_column(width=2)
    grid.add_row(Text(left, style=f"bold {theme.DIM}"), Text(right, style=theme.DIM), Text(""))
    return grid


class ItemList(OptionList):
    """The waiting items of a state tab, and on the Open tab the feed below them."""

    BINDINGS: ClassVar = [Binding("enter", "open", "Context", show=False)]

    class OpenRequested(Message):
        """Enter was pressed on the selected item."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.contexts: dict[str, Context] = {}
        self.items: dict[int, Item] = {}
        self.now = utc_now()
        # The spinner frame of the rows whose context is being fetched.
        self.frame = ""

    def show(
        self,
        tab: State,
        waiting: list[Item],
        feed: list[Item],
        selected: int | None,
        now: datetime,
        filtered: bool = False,
    ) -> None:
        """Replace the rows and select `selected`, the row in its place if it is gone, else the first."""
        previous = self.highlighted
        self.items = {item.id: item for item in [*waiting, *feed]}
        self.now = now
        options: list[Option] = []
        if tab == "open":
            options.append(Option(_section("WAITING ON YOU", str(len(waiting))), disabled=True))
        options += [Option(self._row(item), id=str(item.id)) for item in waiting]
        if not waiting:
            empty = "Nothing matches the filter." if filtered else _EMPTY[tab]
            options.append(Option(Text(empty, style=theme.FAINT), disabled=True))
        if feed:
            options.append(Option(Text(""), disabled=True))
            options.append(Option(_section("FYI", "newest first"), disabled=True))
            options += [Option(self._row(item), id=str(item.id)) for item in feed]
        self.set_options(options)

        enabled = [i for i, option in enumerate(options) if not option.disabled]
        ids = [option.id for option in options]
        if selected is not None and str(selected) in ids:
            self.highlighted = ids.index(str(selected))
        elif enabled and selected is not None:
            # The selected item left this tab: select the one that took its place.
            self.highlighted = next((i for i in enabled if i >= (previous or 0)), enabled[-1])
        elif enabled:
            self.highlighted = enabled[0]
        else:
            self.highlighted = None

    def show_contexts(self, contexts: dict[str, Context], frame: str) -> None:
        """Redraw the rows whose context mark changed, or that spin while it is fetched."""
        for item_id, item in self.items.items():
            key = str(item_id)
            before, after = self.contexts.get(key), contexts.get(key)
            if before != after or (after and after.state == "fetching"):
                self.replace_option_prompt(key, _row(item, self.now, after, frame))
        self.contexts = contexts
        self.frame = frame

    def _row(self, item: Item) -> Table:
        return _row(item, self.now, self.contexts.get(str(item.id)), self.frame)

    @property
    def selected_id(self) -> int | None:
        """The id of the selected item, if any."""
        option = self.highlighted_option
        return int(option.id) if option and option.id else None

    def action_open(self) -> None:
        if self.selected_id is not None:
            self.post_message(self.OpenRequested())


class Filter(Input):
    """The filter bar over the list; Esc clears and closes it."""

    BINDINGS: ClassVar = [Binding("escape", "close", "Clear the filter", show=False)]

    class Closed(Message):
        """Esc was pressed in the filter bar."""

    def action_close(self) -> None:
        self.post_message(self.Closed())


def matches(item: Item, query: str) -> bool:
    """Return whether the title, the body or a link label contains `query`, ignoring case."""
    text = " ".join([item.title, item.body or "", *(link.label for link in item.links)])
    return query.casefold() in text.casefold()


class Details(VerticalScroll):
    """Everything about the selected item, with the buttons that act on it."""

    def compose(self) -> ComposeResult:
        yield Static(id="d-title")
        yield Static(id="d-meta")
        yield Markdown(id="d-body")
        yield Static(id="d-links")
        # Two groups, so a narrow pane can put the second one on its own line.
        with Horizontal(id="d-actions"):
            with Horizontal(id="actions-main"):
                yield Button("Open", id="open", classes="primary", compact=True)
                yield Button("Done", id="done", compact=True)
                yield Button("Reopen", id="reopen", compact=True)
            with Horizontal(id="actions-more"):
                yield Label("Snooze", id="snooze-label")
                yield Button("1d", id="snooze-1d", classes="small", compact=True)
                yield Button("3d", id="snooze-3d", classes="small", compact=True)
                yield Button("Mon", id="snooze-mon", classes="small", compact=True)
                yield Button("Ignore", id="ignore", compact=True)
        yield ContextPanel(id="d-context")
        yield Static(id="d-prompt-label", classes="label")
        yield Static(id="d-prompt")
        yield Static("HISTORY", id="d-history-label", classes="label")
        yield Static(id="d-history")
        yield Static("Select an item.", id="d-empty")

    def show(self, item: Item | None, events: list[Event], now: datetime) -> None:
        """Show `item` with its history, or a hint when nothing is selected."""
        self.set_class(item is None, "-empty")
        if item is None:
            return
        self.query_one("#d-title", Static).update(Content(item.title))
        self.query_one("#d-meta", Static).update(_meta(item, now))
        body = self.query_one("#d-body", Markdown)
        body.set_class(not item.body, "-hidden")
        body.update(item.body or "")
        links = self.query_one("#d-links", Static)
        links.set_class(not item.links, "-hidden")
        links.update(_links(item))

        waiting_open = item.kind == "waiting" and item.state in ("open", "snoozed")
        closed = item.kind == "waiting" and not waiting_open
        self.query_one("#done").display = waiting_open
        self.query_one("#actions-more").display = waiting_open
        self.query_one("#reopen").display = closed
        # On an FYI, the open button is the only action, so it does not need to stand out.
        self.query_one("#open").set_class(item.kind == "waiting", "primary")

        label = "PROMPT SET BY AGENT" if item.prompt else "PROMPT (DEFAULT)"
        self.query_one("#d-prompt-label", Static).update(label)
        self.query_one("#d-prompt", Static).update(Content(prompt_of(item)))
        self.query_one("#d-history", Static).update(_history(events))


class ContextPanel(Vertical):
    """A context: the latest steps while it is fetched, then the answer, or why it failed."""

    def __init__(self, label: bool = True, id: str | None = None) -> None:
        super().__init__(id=id)
        self.label = label
        self._shown: tuple[Context | None, str, Item | None] = (None, "", None)

    def compose(self) -> ComposeResult:
        if self.label:
            yield Static(classes="label cx-label")
        yield Static(classes="cx-status")
        yield Markdown(classes="cx-body")

    def on_mount(self) -> None:
        self.show(*self._shown)

    def show(self, context: Context | None, frame: str, item: Item | None = None) -> None:
        """Show `context`, or nothing when there is none; `item` tells whether it is stale."""
        self._shown = (context, frame, item)
        self.display = context is not None
        if context is None:
            return
        try:
            status = self.query_one(".cx-status", Static)
            body = self.query_one(".cx-body", Markdown)
        except NoMatches:
            return  # Not composed yet; on_mount shows it.
        if self.label:
            label = Content.assemble("CONTEXT  ", context_meta(context, item))
            self.query_one(".cx-label", Static).update(label)
        status.display = context.state != "ready"
        body.display = context.state == "ready"
        if context.state == "ready":
            if body.source != context.text:
                body.update(context.text)
        elif context.state == "failed":
            # Fetched from other people's messages, so a failure shows as text, not Markdown.
            status.update(Content(context.text))
        else:
            status.update(_progress(context, frame))


def context_meta(context: Context, item: Item | None) -> Content:
    """Return when the context was fetched, and whether it failed or may be out of date."""
    if context.state == "fetching":
        return Content.styled(f"started {day_time(context.started_at)}", theme.DIM)
    when = f"fetched {day_time(context.started_at)}"
    if context.state == "failed":
        return Content.assemble(("the fetch failed", theme.URGENT), (f" · {when}", theme.DIM))
    if item and context.stale(item):
        return Content.assemble(
            (when, theme.DIM), ("  · the agent changed the item since", theme.ACCENT)
        )
    return Content.styled(when, theme.DIM)


def _progress(context: Context, frame: str) -> Content:
    lines = [Content.styled(f"{frame} fetching the context…", theme.ACCENT)]
    for step in context.steps[-_SHOWN_STEPS:]:
        if step.kind == "tool":
            lines.append(Content.assemble(("› ", theme.FAINT), (step.text, theme.FOREGROUND)))
        else:
            lines.append(Content.styled(step.text, f"italic {theme.DIM}"))
    return Content("\n").join(lines)


def _meta(item: Item, now: datetime) -> Content:
    if item.kind == "fyi":
        state = "FYI"
    elif item.state == "snoozed" and item.snoozed_until:
        state = f"snoozed until {day_time(item.snoozed_until)}"
    else:
        state = str(item.state)
    parts = [
        Content.styled(f" {state} ", f"{theme.FOREGROUND} on {theme.SELECTED}"),
        Content(f" · first seen {day(item.created_at)} · {_ago(now - item.created_at)}"),
    ]
    if item.opened_at:
        parts.append(Content(f" · opened {_ago(now - item.opened_at)}"))
    if item.kind == "waiting" and item.priority == "urgent":
        parts += [Content(" · "), Content.styled("urgent", theme.URGENT)]
    return Content.assemble(*parts)


def _ago(elapsed: timedelta) -> str:
    age = format_age(elapsed)
    return "just now" if age == "now" else f"{age} ago"


def _links(item: Item) -> Content:
    chips: list[Content] = []
    for index, link in enumerate(item.links):
        link_type = link.type or "Web"
        label = link.label
        if len(label) > _LINK_LABEL_LENGTH:
            label = label[: _LINK_LABEL_LENGTH - 1].rstrip() + "…"
        # Non-breaking spaces keep a chip on one line when the chips wrap.
        label = label.replace(" ", "\u00a0")
        color = theme.LINK_COLORS.get(link_type, theme.DIM)
        chip = Content.assemble(
            (f"\u00a0{link_type.upper()}\u00a0", f"bold {color} on {theme.BUTTON}"),
            (f"{label}\u00a0", f"{theme.FOREGROUND} on {theme.BUTTON}"),
        ).stylize(Style.from_meta({"link": index}))
        chips.append(chip)
    return Content(" ").join(chips)


def _history(events: list[Event]) -> Table:
    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, style=theme.DIM)
    grid.add_column(no_wrap=True, width=5)
    grid.add_column(ratio=1, style=theme.NOTE)
    for event in events:
        who = Text("you", style=theme.ACCENT) if event.actor == "user" else Text("agent", theme.OK)
        what = f"{event.action}: {event.note}" if event.note else event.action
        grid.add_row(day_time(event.at), who, what)
    return grid
