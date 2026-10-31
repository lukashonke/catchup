"""The Todoist tab: the tasks td lists for the filter, and the keys that act on them.

The tab shows the last list at once, from the cache, and reads a new one in the background:
when you switch to it, every REFRESH_SECONDS while it is shown, after each action, and on R.
An action changes the row at once and then reads the list again, which corrects the row if
the action failed.
"""

import asyncio
import dataclasses
from collections.abc import Awaitable
from datetime import date, datetime
from functools import partial
from typing import ClassVar, Literal

from rich.table import Table
from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.style import Style
from textual.widgets import Markdown, OptionList, Static
from textual.widgets.option_list import Option

from catchup import CatchupError, todoist
from catchup.context import Context
from catchup.links import Link
from catchup.opener import open_item
from catchup.store import Item, utc_now
from catchup.todoist import Task
from catchup.tui import theme
from catchup.tui.dialogs import SnoozePicker
from catchup.tui.widgets import ContextPanel, _context_mark, _Line, _section

REFRESH_SECONDS = 60.0
RED = "#e44332"
# Todoist's priority colors in its dark theme, by p-label; p4 has none.
PRIORITY_COLORS = {1: "#ff7066", 2: "#ff9a14", 3: "#5297ff"}
_MARK = "●"
_LINK_LENGTH = 40

FOOTER_KEYS = [
    ("space", "complete"),
    ("z", "reschedule"),
    ("R", "refresh"),
]

Problem = Literal["missing", "signed-out"]


def help_keys(open_label: str, open_space_label: str) -> list[tuple[str, str]]:
    """Return the keys of the Todoist tab for the `?` list."""
    return [
        ("↑ ↓", "select a task"),
        ("← →", "previous or next tab"),
        ("space", "complete the task"),
        ("enter", "fetch the context; once it is ready, show it"),
        ("ctrl+enter", "open the task in Todoist"),
        ("z", "reschedule to a time you pick"),
        ("a", f"{open_label} with the task"),
        ("A, ctrl+a", f"{open_space_label} with the task"),
        ("R", "read the tasks from Todoist again"),
        ("L", "sign in, when td is signed out"),
        ("c", "catch up now: start the agent run"),
        ("s", "settings"),
        ("?", "this list"),
        ("q", "quit"),
    ]


def _day(day: date) -> str:
    return f"{day:%a} {day.day} {day:%b}"


def _when(task: Task) -> str:
    """Return the due date and time, such as `Thu 29 Oct 15:00`."""
    if task.due_date is None:
        return "no due date"
    return _day(task.due_date) + (f" {task.due_time:%H:%M}" if task.due_time else "")


def due_text(task: Task, now: datetime) -> tuple[str, bool]:
    """Return the due date as a row shows it, such as `2d overdue`, and whether it is overdue."""
    if task.due_date is None:
        return "", False
    local = now.astimezone()
    days = (local.date() - task.due_date).days
    if days > 0:
        return f"{days}d overdue", True
    if days == 0 and task.due_time is not None:
        return f"{task.due_time:%H:%M}", task.due_time < local.time()
    return {0: "today", -1: "tomorrow"}.get(days, _day(task.due_date)), False


def context_key(task: Task) -> str:
    """Return the key of the task's context, apart from the inbox items' keys."""
    return f"todoist-{task.id}"


def _row(task: Task, now: datetime, context: Context | None = None, frame: str = "") -> Table:
    color = PRIORITY_COLORS.get(task.p)
    title = Text(task.title)
    if task.labels:
        title.append("  " + " ".join(f"@{label}" for label in task.labels), style=theme.DIM)
    due, overdue = due_text(task, now)
    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(width=1, no_wrap=True)
    grid.add_column(ratio=1)
    grid.add_column(width=1, no_wrap=True)
    grid.add_column(width=12, no_wrap=True, justify="right")
    grid.add_column(width=1, no_wrap=True)
    grid.add_row(
        Text(_MARK if color else "", style=color or ""),
        _Line(title),
        Text("↻" if task.recurring else "", style=theme.DIM),
        Text(due, style=theme.URGENT if overdue else theme.DIM),
        _context_mark(None, context, frame),
    )
    return grid


class TaskList(OptionList):
    """The tasks, under a header for each project."""

    def __init__(self, id: str | None = None) -> None:
        super().__init__(id=id)
        self.tasks: dict[str, Task] = {}
        self.contexts: dict[str, Context] = {}
        self.now = utc_now()
        # The spinner frame of the rows whose context is being fetched.
        self.frame = ""

    def show(self, tasks: list[Task], selected: str | None, now: datetime, empty: str) -> None:
        """Replace the rows and select `selected`, the row in its place if it is gone, else the first."""
        previous = self.highlighted
        self.tasks = {task.id: task for task in tasks}
        self.now = now
        options: list[Option] = []
        for index, task in enumerate(tasks):
            if index == 0 or task.project != tasks[index - 1].project:
                if options:
                    options.append(Option(Text(""), disabled=True))
                count = sum(other.project == task.project for other in tasks)
                options.append(Option(_section(task.project.upper(), str(count)), disabled=True))
            context = self.contexts.get(context_key(task))
            options.append(Option(_row(task, now, context, self.frame), id=task.id))
        if not tasks:
            options.append(Option(Text(empty, style=theme.FAINT), disabled=True))
        self.set_options(options)

        enabled = [i for i, option in enumerate(options) if not option.disabled]
        ids = [option.id for option in options]
        if selected is not None and selected in ids:
            self.highlighted = ids.index(selected)
        elif enabled and selected is not None:
            # The selected task left the list: select the one that took its place.
            self.highlighted = next((i for i in enabled if i >= (previous or 0)), enabled[-1])
        elif enabled:
            self.highlighted = enabled[0]
        else:
            self.highlighted = None

    def show_contexts(self, contexts: dict[str, Context], frame: str) -> None:
        """Redraw the rows whose context mark changed, or that spin while it is fetched."""
        for task_id, task in self.tasks.items():
            key = context_key(task)
            before, after = self.contexts.get(key), contexts.get(key)
            if before != after or (after and after.state == "fetching"):
                self.replace_option_prompt(task_id, _row(task, self.now, after, frame))
        self.contexts = contexts
        self.frame = frame


class TaskDetails(VerticalScroll):
    """Everything about the selected task."""

    def compose(self) -> ComposeResult:
        yield Static(id="t-title")
        yield Static(id="t-meta")
        yield Static(id="t-due")
        yield Markdown(id="t-body")
        yield Static(id="t-link")
        yield ContextPanel(id="t-context")
        yield Static("Select a task.", id="t-empty")

    def show(self, task: Task | None, now: datetime) -> None:
        """Show `task`, or a hint when nothing is selected."""
        self.set_class(task is None, "-empty")
        if task is None:
            return
        self.query_one("#t-title", Static).update(Content(task.title))
        color = PRIORITY_COLORS.get(task.p, theme.DIM)
        place = f"{task.project} › {task.section}" if task.section else task.project
        meta = [
            Content.styled(f" p{task.p} ", f"bold {color} on {theme.BUTTON}"),
            Content(f" · {place}"),
        ]
        if task.labels:
            meta.append(Content(" · "))
            meta.append(Content.styled(" ".join(f"@{label}" for label in task.labels), theme.NOTE))
        self.query_one("#t-meta", Static).update(Content.assemble(*meta))
        self.query_one("#t-due", Static).update(_due(task, now))
        body = self.query_one("#t-body", Markdown)
        body.set_class(not task.description, "-hidden")
        body.update(task.description)
        link = self.query_one("#t-link", Static)
        link.set_class(not task.url, "-hidden")
        # Cut like the inbox's link labels; non-breaking spaces keep the chip on one line.
        label = task.url.removeprefix("https://")
        if len(label) > _LINK_LENGTH:
            label = label[: _LINK_LENGTH - 1] + "…"
        link.update(
            Content.assemble(
                ("\u00a0TODOIST\u00a0", f"bold {RED} on {theme.BUTTON}"),
                (f"{label}\u00a0", f"{theme.FOREGROUND} on {theme.BUTTON}"),
            ).stylize(Style.from_meta({"todoist-link": task.url}))
        )


def _due(task: Task, now: datetime) -> Content:
    if task.due_date is None:
        return Content.styled("no due date", theme.DIM)
    text, overdue = due_text(task, now)
    parts = [Content.styled("due ", theme.DIM), Content(_when(task))]
    if overdue:
        parts.append(Content.styled(f" · {text}", theme.URGENT))
    if task.recurring:
        parts.append(Content.styled(f" · ↻ {task.due_string}", theme.DIM))
    return Content.assemble(*parts)


class TodoistView(Vertical, can_focus=True):
    """The Todoist tab: a status line, then the tasks and the details, or the setup step."""

    BINDINGS: ClassVar = [
        Binding("space", "complete", "Complete", show=False),
        # Before the list's own Enter, which would select the row.
        Binding("enter", "context", "Context", show=False, priority=True),
        Binding("ctrl+enter", "browse", "Open in Todoist", show=False),
        Binding("z", "reschedule", "Reschedule", show=False),
        Binding("a", "claude", "Open in Claude", show=False),
        Binding("A,shift+a,ctrl+a", "claude(True)", "Open in Claude in a new space", show=False),
        Binding("R", "refresh", "Refresh", show=False),
        Binding("L", "login", "Sign in", show=False),
    ]

    class Changed(Message):
        """The tasks changed, so the count on the tab may have."""

    def __init__(self, id: str | None = None) -> None:
        super().__init__(id=id)
        # None until the cache or td gives a list.
        self.tasks: list[Task] | None = None
        self.fetched_at: datetime | None = None
        self.selected: Task | None = None
        self.problem: Problem | None = None
        self.error: str | None = None
        self.signing_in = False
        self._busy = False
        self._again = False
        # Actions sent to td that have not finished; a list read meanwhile may predate them.
        self._pending = 0

    def compose(self) -> ComposeResult:
        yield Static(id="t-status")
        with Horizontal(id="t-panes"):
            yield TaskList(id="t-list")
            yield TaskDetails(id="t-details")
        yield Static(id="t-setup")

    async def on_mount(self) -> None:
        raw = await asyncio.to_thread(todoist.read_cache)
        if raw is not None:
            try:
                self._take(todoist.parse(raw))
            except CatchupError:
                pass  # A broken cache is replaced by the next read.
        self.set_interval(REFRESH_SECONDS, self._tick)
        self._show()

    @property
    def count(self) -> int | None:
        """The number of tasks the tab shows, or None while it shows none."""
        return None if self.tasks is None or self.problem else len(self.tasks)

    def focus_list(self) -> None:
        """Focus the task list, or the tab itself while it shows a setup step."""
        task_list = self.query_one(TaskList)
        (task_list if task_list.display and self.query_one("#t-panes").display else self).focus()

    def _tick(self) -> None:
        if self.display:
            self.refresh_tasks()

    def refresh_tasks(self) -> None:
        """Read the tasks from td in the background, unless a read is on its way."""
        if self._busy:
            self._again = True
            return
        self._refresh()

    @work(group="todoist-refresh")
    async def _refresh(self) -> None:
        self._busy = True
        self._show_status()
        try:
            while True:
                self._again = False
                config = self.app.config
                raw, tasks, failure = None, None, None
                try:
                    raw = await todoist.fetch(config.todoist_command, config.todoist_filter)
                    tasks = todoist.parse(raw)
                except CatchupError as e:
                    failure = e
                if self._pending:
                    return  # The last action to finish reads the tasks again.
                if not self._again:
                    break
        finally:
            self._busy = False
        if not self.app.is_running:  # Shutting down; the widgets may be gone.
            return
        self.problem = None
        self.error = None
        if isinstance(failure, todoist.NotInstalled):
            self.problem = "missing"
        elif isinstance(failure, todoist.SignedOut):
            self.problem = "signed-out"
        elif failure is not None:
            self.error = str(failure)
        if tasks is not None:
            self._take(tasks)
        self._show()
        if raw is not None:
            try:
                await asyncio.to_thread(todoist.write_cache, raw)
            except OSError as e:
                self.error = f"cannot keep the tasks in {todoist.cache_path()}: {e}"
                self._show_status()

    def _take(self, tasks: todoist.Tasks) -> None:
        self.tasks = tasks.tasks
        self.fetched_at = tasks.fetched_at

    def _show(self) -> None:
        """Show the status line and the tasks, or the setup step."""
        setup = self._setup()
        self.query_one("#t-panes").display = setup is None
        panel = self.query_one("#t-setup", Static)
        panel.display = setup is not None
        if setup is not None:
            panel.update(setup)
        self._show_status()
        now = utc_now()
        if self.tasks is None:
            empty = "Reading the tasks from Todoist…"
        else:
            empty = f"Nothing matches {self.app.config.todoist_filter}."
        selected = self.selected.id if self.selected else None
        self.query_one(TaskList).show(self.tasks or [], selected, now, empty)
        self._show_selected()
        if self.has_focus or self.query_one(TaskList).has_focus:
            self.focus_list()
        self.post_message(self.Changed())

    def _setup(self) -> Content | None:
        if self.signing_in:
            return Content.assemble(
                ("Signing in to Todoist…\n\n", f"bold {theme.BRIGHT}"),
                "Allow td in the browser window it opened. The tab loads when you are done.",
            )
        if self.problem == "missing":
            command = self.app.config.todoist_command[0]
            return Content.assemble(
                ("This tab needs td, the Todoist CLI.\n\n", f"bold {theme.BRIGHT}"),
                f"catchup cannot find {command} on the PATH. Install it with\n\n",
                (f"    {todoist.INSTALL_COMMAND}\n\n", theme.ACCENT),
                "and the tab loads once td is on the PATH. If td is somewhere else,"
                " set the Todoist command in the settings (s).",
            )
        if self.problem == "signed-out":
            return Content.assemble(
                ("td is not signed in to Todoist.\n\n", f"bold {theme.BRIGHT}"),
                "Press ",
                ("L", f"bold {theme.ACCENT}"),
                " to sign in. td opens your browser and keeps the token itself;"
                " catchup never sees it.",
            )
        return None

    def _show_status(self) -> None:
        config = self.app.config
        parts = [Content.styled(config.todoist_filter, theme.FOREGROUND)]
        if self.count is not None:
            parts.append(Content(f" · {self.count} tasks"))
        if self._busy:
            parts.append(Content(" · updating…"))
        elif self.error:
            parts.append(Content.styled(" · not updated: ", theme.URGENT))
            parts.append(Content(self.error))
        if self.fetched_at and not self.problem:
            parts.append(Content(f" · updated {self.fetched_at.astimezone():%H:%M}"))
        self.query_one("#t-status", Static).update(Content.assemble(*parts))

    def _show_selected(self) -> None:
        option = self.query_one(TaskList).highlighted_option
        selected = option.id if option else None
        self.selected = next((t for t in self.tasks or [] if t.id == selected), None)
        self.query_one(TaskDetails).show(self.selected, utc_now())
        self._show_context()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        # The inbox handles the highlights of its own list; this one is the tab's.
        event.stop()
        self._show_selected()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()

    def on_click(self, event: events.Click) -> None:
        if url := event.style.meta.get("todoist-link"):
            event.stop()
            self.app.open_url(url)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # The task keys act only while the tab shows tasks, and L only while signed out.
        if action == "login":
            return self.problem == "signed-out" and not self.signing_in
        if action in ("complete", "context", "browse", "reschedule", "claude"):
            return self.selected is not None and self._setup() is None
        return True

    def action_refresh(self) -> None:
        self.refresh_tasks()

    def action_context(self) -> None:
        if task := self.selected:
            self.app.open_context(context_key(task), context_item(task))

    def show_contexts(self, contexts: dict[str, Context], frame: str) -> None:
        """Show the context marks of the tasks, and the context of the selected one."""
        self.query_one(TaskList).show_contexts(contexts, frame)
        self._show_context()

    def _show_context(self) -> None:
        task_list = self.query_one(TaskList)
        found = task_list.contexts.get(context_key(self.selected)) if self.selected else None
        self.query_one(ContextPanel).show(found, task_list.frame)

    def action_browse(self) -> None:
        if self.selected and self.selected.url:
            self.app.open_url(self.selected.url)

    def action_complete(self) -> None:
        task = self.selected
        if task is None or self.tasks is None:
            return
        self.tasks = [other for other in self.tasks if other.id != task.id]
        self._show()
        command = self.app.config.todoist_command
        self._act(task, todoist.complete(command, task.id), "Completed")

    def action_reschedule(self) -> None:
        if task := self.selected:
            picked = partial(self._reschedule, task)
            self.app.push_screen(SnoozePicker(task.title, heading="Reschedule to"), picked)

    def _reschedule(self, task: Task, until: datetime | None) -> None:
        if until is None or self.tasks is None:
            return
        due = todoist.due_value(task, until, utc_now())
        due_date, due_time = todoist.parse_due(due)
        moved = dataclasses.replace(task, due_date=due_date, due_time=due_time)
        self.tasks = [moved if other.id == task.id else other for other in self.tasks]
        self._show()
        command = self.app.config.todoist_command
        self._act(task, todoist.reschedule(command, task.id, due), f"Rescheduled to {_when(moved)}")

    @work(group="todoist-act")
    async def _act(self, task: Task, action: Awaitable[None], done: str) -> None:
        """Wait for an action on `task` to finish in td, report it, and read the tasks again."""
        self._pending += 1
        try:
            await action
        except CatchupError as e:
            self.app.notify(str(e), title="Not changed in Todoist", severity="error", markup=False)
        else:
            self.app.notify(task.title, title=done, markup=False)
        finally:
            self._pending -= 1
        if self.app.is_running:
            self.refresh_tasks()

    def action_claude(self, space: bool = False) -> None:
        if self.selected:
            self._open_in_claude(self.selected, space)

    @work(group="todoist-open")
    async def _open_in_claude(self, task: Task, space: bool) -> None:
        config = self.app.config
        label = config.open_space_label if space else config.open_label
        # Titles and errors quote what other people wrote, so they are never read as markup.
        self.app.notify(task.title, title=label, markup=False)
        try:
            await open_item(as_item(task), config, space=space)
        except CatchupError as e:
            self.app.notify(
                str(e),
                title=f"{label} failed",
                severity="error",
                timeout=12,
                markup=False,
            )

    def action_login(self) -> None:
        self._login()

    @work(group="todoist-login")
    async def _login(self) -> None:
        self.signing_in = True
        self._show()
        try:
            await todoist.login(self.app.config.todoist_command)
        except CatchupError as e:
            self.app.notify(str(e), title="Not signed in", severity="error", markup=False)
        finally:
            self.signing_in = False
        if self.app.is_running:
            self._show()
            self.refresh_tasks()


def context_item(task: Task) -> Item:
    """Return the task as the item its context is fetched for, with what td can tell about it."""
    place = f"{task.project} › {task.section}" if task.section else task.project
    facts = [f"Todoist task id:{task.id}", f"project {place}", f"p{task.p}"]
    if task.due_date:
        facts.append(f"due {_when(task)}" + (f", {task.due_string}" if task.recurring else ""))
    if task.labels:
        facts.append("labels " + " ".join(f"@{label}" for label in task.labels))
    read = (
        f"Read the task and its comments with `td task view id:{task.id}`"
        f" and `td comment list id:{task.id}`."
    )
    body = "\n\n".join(part for part in (task.description, " · ".join(facts), read) if part)
    return dataclasses.replace(as_item(task), body=body)


def as_item(task: Task) -> Item:
    """Return the task as an item, which is what the open command takes, with its own prompt."""
    now = utc_now()
    parts = [task.title, task.description, f"Todoist task: {task.url}", "Help me handle this."]
    return Item(
        # A task is no item and has no item id; the store never gives out 0.
        id=0,
        kind="waiting",
        title=task.title,
        body=task.description or None,
        priority="normal",
        links=(Link("Todoist", task.url, "Web"),) if task.url else (),
        prompt="\n\n".join(part for part in parts if part),
        state="open",
        snoozed_until=None,
        created_at=now,
        updated_at=now,
        opened_at=None,
        new=False,
        last_event=None,
    )
