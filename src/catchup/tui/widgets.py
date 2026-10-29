"""The list of items and the details pane."""

from datetime import datetime
from typing import ClassVar

from rich.console import Console, ConsoleOptions, RenderResult
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.style import Style
from textual.widgets import Button, Label, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from catchup.opener import prompt_of
from catchup.store import Event, Item, State
from catchup.tui import theme
from catchup.when import format_age

_MARKERS = {"urgent": "!", "low": "↓"}
_LINK_LABEL_LENGTH = 40
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


def _row(item: Item, now: datetime) -> Table:
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
    grid.add_column(ratio=1)
    grid.add_column(width=8, no_wrap=True)
    grid.add_column(width=4, no_wrap=True, justify="right")
    grid.add_row(
        marker,
        _Line(Text(item.title, style=theme.FEED if fyi else "")),
        Text(link_type, style=theme.LINK_COLORS.get(link_type, theme.DIM)),
        Text(format_age(now - item.created_at), style=age_color(item, now)),
    )
    return grid


def _section(left: str, right: str) -> Table:
    grid = Table.grid(expand=True)
    grid.add_column(ratio=1)
    grid.add_column(justify="right")
    grid.add_row(Text(left, style=f"bold {theme.DIM}"), Text(right, style=theme.DIM))
    return grid


class ItemList(OptionList):
    """The waiting items of a state tab, and on the Open tab the feed below them."""

    BINDINGS: ClassVar = [Binding("enter", "open", "Open in Claude", show=False)]

    class OpenRequested(Message):
        """Enter was pressed on the selected item."""

    def show(
        self, tab: State, waiting: list[Item], feed: list[Item], selected: int | None, now: datetime
    ) -> None:
        """Replace the rows and select `selected`, the row in its place if it is gone, else the first."""
        previous = self.highlighted
        options: list[Option] = []
        if tab == "open":
            options.append(Option(_section("WAITING ON YOU", str(len(waiting))), disabled=True))
        options += [Option(_row(item, now), id=str(item.id)) for item in waiting]
        if not waiting:
            options.append(Option(Text(_EMPTY[tab], style=theme.FAINT), disabled=True))
        if feed:
            options.append(Option(Text(""), disabled=True))
            options.append(Option(_section("FYI", "newest first"), disabled=True))
            options += [Option(_row(item, now), id=str(item.id)) for item in feed]
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

    @property
    def selected_id(self) -> int | None:
        """The id of the selected item, if any."""
        option = self.highlighted_option
        return int(option.id) if option and option.id else None

    def action_open(self) -> None:
        if self.selected_id is not None:
            self.post_message(self.OpenRequested())


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
                yield Button("Open in Claude", id="open", classes="primary", compact=True)
                yield Button("Done", id="done", compact=True)
                yield Button("Reopen", id="reopen", compact=True)
            with Horizontal(id="actions-more"):
                yield Label("Snooze", id="snooze-label")
                yield Button("1d", id="snooze-1d", classes="small", compact=True)
                yield Button("3d", id="snooze-3d", classes="small", compact=True)
                yield Button("Mon", id="snooze-mon", classes="small", compact=True)
                yield Button("Ignore", id="ignore", compact=True)
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
        # On an FYI, Open in Claude is the only action, so it does not need to stand out.
        self.query_one("#open").set_class(item.kind == "waiting", "primary")

        label = "PROMPT SET BY AGENT" if item.prompt else "PROMPT (DEFAULT)"
        self.query_one("#d-prompt-label", Static).update(label)
        self.query_one("#d-prompt", Static).update(Content(prompt_of(item)))
        self.query_one("#d-history", Static).update(_history(events))


def _meta(item: Item, now: datetime) -> Content:
    if item.kind == "fyi":
        state = "FYI"
    elif item.state == "snoozed" and item.snoozed_until:
        state = f"snoozed until {day_time(item.snoozed_until)}"
    else:
        state = str(item.state)
    parts = [
        Content.styled(f" {state} ", f"{theme.FOREGROUND} on {theme.SELECTED}"),
        Content(f" · first seen {day(item.created_at)} · {format_age(now - item.created_at)} ago"),
    ]
    if item.kind == "waiting" and item.priority == "urgent":
        parts += [Content(" · "), Content.styled("urgent", theme.URGENT)]
    return Content.assemble(*parts)


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
