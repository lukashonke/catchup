"""The settings screen, which edits `config.toml`."""

import shlex
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static, TextArea

from catchup import CatchupError
from catchup.config import Config, default_config_path, save_config, validate


class Settings(ModalScreen[Config | None]):
    """Edit the settings and save them; dismiss with the new config, or None on cancel."""

    BINDINGS: ClassVar = [
        Binding("escape", "dismiss(None)", "Cancel", show=False),
        Binding("ctrl+s", "save", "Save", show=False),
    ]

    def __init__(self, config: Config, path: Path | None = None) -> None:
        super().__init__()
        self.config = config
        self.path = path or default_config_path()

    def compose(self) -> ComposeResult:
        config = self.config
        with VerticalScroll(id="settings"):
            yield Static("Settings", id="s-title")
            yield Static(f"Saved to {self.path}", id="s-path")
            yield Label("Open action name", classes="s-label")
            yield Input(config.open_label, id="open_label")
            yield Label("Open command", classes="s-label")
            yield Static(
                "Gets the item in CATCHUP_ID, CATCHUP_TITLE, CATCHUP_PROMPT and CATCHUP_LINKS."
                " Empty uses the herdr opener inside herdr.",
                classes="s-hint",
            )
            yield Input(_join(config.open_command), id="open_command")
            yield Label("Agent prompt", classes="s-label")
            yield Static(
                "What the agent run is told to do. Empty turns the agent run off.",
                classes="s-hint",
            )
            yield TextArea(config.agent_prompt or "", id="agent_prompt", soft_wrap=True)
            yield Label("Agent command", classes="s-label")
            yield Static("Runs in your home directory with the prompt on stdin.", classes="s-hint")
            yield Input(_join(config.agent_command), id="agent_command")
            yield Label("Run the agent every", classes="s-label")
            yield Input(config.agent_every, id="agent_every", placeholder="30m, 1h or 4h")
            with Horizontal(id="s-actions"):
                yield Button("Save", id="save", classes="primary", compact=True)
                yield Button("Cancel", id="cancel", compact=True)
            yield Static("ctrl+s save · esc cancel", id="s-keys")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "save":
            self.action_save()
        else:
            self.dismiss(None)

    def action_save(self) -> None:
        data: dict[str, object] = {
            "open_label": self.query_one("#open_label", Input).value,
            "agent_prompt": self.query_one("#agent_prompt", TextArea).text,
            "agent_every": self.query_one("#agent_every", Input).value.strip(),
        }
        try:
            for key in ("open_command", "agent_command"):
                text = self.query_one(f"#{key}", Input).value
                try:
                    argv = shlex.split(text)
                except ValueError as e:
                    raise CatchupError(f"{key} in {self.path}: {e}") from None
                # An empty command falls back to the default, as a missing key does.
                if argv:
                    data[key] = argv
            config = validate(data, self.path)
            save_config(config, self.path)
        except (CatchupError, OSError) as e:
            self.notify(str(e), title="Not saved", severity="error", markup=False)
            return
        self.dismiss(config)


def _join(argv: list[str] | tuple[str, ...] | None) -> str:
    return shlex.join(argv) if argv else ""
