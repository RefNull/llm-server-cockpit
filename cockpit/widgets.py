"""Shared cockpit infrastructure: one ConfirmModal so every screen asks the same way before
an action that isn't a no-op under dry-run (build, rollback, download, restart llama-swap),
and one shared stylesheet (SHARED_CSS, set as CockpitApp.CSS) so layout/scroll bugs get
fixed once here rather than rediscovered and patched separately in every screen.

Convention every screen follows: wrap the top-level compose() content in a VerticalScroll
(or a Horizontal of VerticalScrolls for a multi-column layout) so it scrolls instead of
clipping when the terminal is shorter than the content — this was the actual bug the
Settings tab hit before its scroll containers were made explicit and independently bounded.
Group related fields inside `classes="panel"` boxes with a `classes="panel-title"` heading;
use `classes="button-row"` for a horizontal row of action buttons, `classes="status-text"`
for a muted status/result line, and `classes="error-text"` for inline validation errors —
defined once below instead of redeclared per screen.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

SHARED_CSS = """
.panel {
    border: round $primary;
    padding: 1 2;
    margin-bottom: 1;
}
.panel-title {
    text-style: bold;
}
.button-row {
    height: auto;
    margin-top: 1;
    margin-bottom: 1;
}
.button-row Button {
    margin-right: 2;
}
.status-text {
    color: $text-muted;
    margin-top: 1;
}
.error-text {
    color: $error;
    margin-top: 1;
}
"""


class ConfirmModal(ModalScreen[bool]):
    """Usage inside a @work async handler: confirmed = await self.app.push_screen_wait(
    ConfirmModal("Rebuild cuda?")); if not confirmed: return
    """

    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
    }
    #confirm-dialog {
        width: 60;
        height: auto;
        border: thick $background 80%;
        background: $surface;
        padding: 1 2;
    }
    #confirm-message {
        margin-bottom: 1;
    }
    """

    def __init__(self, message: str, confirm_label: str = "Confirm", danger: bool = False) -> None:
        super().__init__()
        self.message = message
        self.confirm_label = confirm_label
        self.danger = danger

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Static(self.message, id="confirm-message")
            with Horizontal():
                yield Button(self.confirm_label, variant="error" if self.danger else "primary", id="confirm-yes")
                yield Button("Cancel", id="confirm-no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")
