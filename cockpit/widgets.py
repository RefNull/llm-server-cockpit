"""Shared cockpit widgets — one ConfirmModal so every screen asks the same way before an
action that isn't a no-op under dry-run (build, rollback, download, restart llama-swap).
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


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
