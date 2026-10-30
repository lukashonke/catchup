"""The dialogs over the inbox: the snooze picker, the note editor, the keys and the context."""

import shutil
import subprocess
from datetime import datetime
from typing import ClassVar

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Input, Markdown, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from catchup import CatchupError
from catchup.context import Context
from catchup.store import Item, utc_now
from catchup.tui import theme
from catchup.tui.widgets import day_time
from catchup.when import parse_until

SNOOZE_CHOICES = (
    ("3h", "3 hours"),
    ("tomorrow", "Tomorrow"),
    ("3d", "In 3 days"),
    ("mon", "Next Monday"),
    ("1w", "In a week"),
)


class SnoozePicker(ModalScreen[datetime | None]):
    """Pick when a snoozed item comes back; dismiss with that moment, or None on cancel."""

    BINDINGS: ClassVar = [
        Binding("escape", "dismiss(None)", "Cancel", show=False),
        *(
            Binding(str(number), f"pick({number - 1})", show=False)
            for number in range(1, len(SNOOZE_CHOICES) + 1)
        ),
    ]

    def __init__(self, title: str, heading: str = "Snooze until") -> None:
        super().__init__()
        self.item_title = title
        self.heading = heading

    def compose(self) -> ComposeResult:
        now = utc_now()
        with Vertical(classes="dialog"):
            yield Static(self.heading, classes="dialog-title")
            yield Static(Content(self.item_title), classes="dialog-hint")
            yield OptionList(
                *(
                    Option(_choice(number, label, parse_until(value, now)))
                    for number, (value, label) in enumerate(SNOOZE_CHOICES, 1)
                ),
                id="snooze-choices",
            )
            yield Input(placeholder="or type 3h, 2d, 1w, mon, tomorrow, YYYY-MM-DD", id="snooze-at")
            yield Static("1-5 pick · tab type a time · esc cancel", classes="dialog-keys")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # Digits belong to the time being typed, not to the choices.
        return action != "pick" or not isinstance(self.focused, Input)

    def action_pick(self, index: int) -> None:
        self.dismiss(parse_until(SNOOZE_CHOICES[index][0], utc_now()))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.action_pick(event.option_index)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        try:
            until = parse_until(event.value.strip(), utc_now())
            if until <= utc_now():
                raise CatchupError(f"{event.value.strip()} is in the past; pick a later time")
        except CatchupError as e:
            self.notify(str(e), title="Not snoozed", severity="error", markup=False)
            return
        self.dismiss(until)


class NoteEditor(ModalScreen[str | None]):
    """Write a note on an item; dismiss with the text, or None on cancel or when it is empty."""

    BINDINGS: ClassVar = [
        Binding("escape", "dismiss(None)", "Cancel", show=False),
        Binding("ctrl+s", "save", "Save", show=False),
    ]

    def __init__(self, title: str) -> None:
        super().__init__()
        self.item_title = title

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static("Note", classes="dialog-title")
            yield Static(Content(self.item_title), classes="dialog-hint")
            yield TextArea(id="note", soft_wrap=True)
            yield Static(
                "The agent reads your notes on its next run.\nctrl+s save · esc cancel",
                classes="dialog-keys",
            )

    def action_save(self) -> None:
        self.dismiss(self.query_one(TextArea).text.strip() or None)


class KeysHelp(ModalScreen[None]):
    """The list of every key, including the ones the footer has no room for."""

    BINDINGS: ClassVar = [Binding("escape,question_mark,q", "dismiss", "Close", show=False)]

    def __init__(
        self, open_label: str, open_space_label: str, keys: list[tuple[str, str]] | None = None
    ) -> None:
        super().__init__()
        self.open_label = open_label
        self.open_space_label = open_space_label
        # The keys of another tab, such as the Todoist tab, in place of the inbox keys.
        self.keys = keys

    def compose(self) -> ComposeResult:
        keys = self.keys or [
            ("↑ ↓", "select an item"),
            ("← →", "previous or next tab"),
            ("enter", "fetch the context; once it is ready, show it"),
            ("a", self.open_label),
            ("A, ctrl+a", self.open_space_label),
            ("ctrl+enter", "open the first link in the browser"),
            ("space", "done"),
            ("backspace", "ignore"),
            ("z", "snooze until a time you pick"),
            ("u", "undo the last state change"),
            ("n", "write a note for the agent"),
            ("/", "filter the list; esc clears the filter"),
            ("c", "catch up now: start the agent run"),
            ("s", "settings"),
            ("?", "this list"),
            ("q", "quit"),
        ]
        grid = Table.grid(padding=(0, 2))
        grid.add_column(no_wrap=True, style=f"bold {theme.ACCENT}")
        grid.add_column()
        for key, what in keys:
            grid.add_row(key, Text(what))
        with Vertical(classes="dialog"):
            yield Static("Keys", classes="dialog-title")
            yield Static(grid, id="keys")
            yield Static("esc close", classes="dialog-keys")


class ContextView(ModalScreen[str | None]):
    """An item's context, in a popup; dismiss with "fetch" to fetch it again, else None."""

    BINDINGS: ClassVar = [
        Binding("escape,q,enter", "dismiss(None)", "Close", show=False),
        Binding("r", "dismiss('fetch')", "Fetch again", show=False),
        Binding("y", "copy", "Copy", show=False),
    ]

    def __init__(self, item: Item, context: Context) -> None:
        super().__init__()
        self.item = item
        self.context = context

    def compose(self) -> ComposeResult:
        context = self.context
        when = f"fetched {day_time(context.started_at)}"
        if context.state == "failed":
            meta = Content.assemble(("the fetch failed", theme.URGENT), f" · {when}")
        elif context.stale(self.item):
            meta = Content.assemble(
                (when, theme.DIM), ("  · the agent changed the item since", theme.ACCENT)
            )
        else:
            meta = Content(when)
        with Vertical(id="context-box"):
            yield Static("esc close · r fetch again · y copy", id="c-keys")
            with VerticalScroll(id="context"):
                yield Static(Content(self.item.title), id="c-title")
                yield Static(meta, id="c-meta")
                # Fetched from other people's messages, so a failure shows as text, not Markdown.
                if context.state == "failed":
                    yield Static(Content(context.text), id="c-error")
                else:
                    yield Markdown(context.text, id="c-body")

    def action_copy(self) -> None:
        # Terminals and multiplexers often drop the escape code copy_to_clipboard() sends.
        if shutil.which("pbcopy"):
            subprocess.run(["pbcopy"], input=self.context.text.encode(), check=False)
        else:
            self.app.copy_to_clipboard(self.context.text)
        self.notify("The context is on the clipboard.", markup=False)


def _choice(number: int, label: str, until: datetime) -> Table:
    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(width=1)
    grid.add_column(ratio=1)
    grid.add_column(justify="right")
    grid.add_row(
        Text(str(number), style=f"bold {theme.ACCENT}"),
        Text(label),
        Text(day_time(until), style=theme.DIM),
    )
    return grid
