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

import subprocess

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

SHARED_CSS = """
.panel {
    border: round $primary;
    padding: 0 1;
    margin-bottom: 1;
}
.panel-title {
    text-style: bold;
}
.section-title {
    text-style: bold;
    color: $accent;
    margin-top: 1;
    margin-bottom: 1;
}
.close-button {
    min-width: 4;
    width: 4;
    height: 1;
    border: none;
    padding: 0;
    dock: right;
}
.inline-row {
    height: auto;
    align-vertical: middle;
}
.inline-row Input {
    width: 1fr;
    margin-right: 1;
}
.inline-row Button {
    min-width: 10;
    height: 3;
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


def run_shell_capture(cmd: str, timeout: float = 5.0) -> str:
    """Run a fixed (never user-supplied) shell one-liner and return its stdout, for a quick
    read-only info popup (lspci, ip a) — not a provisioning action, nothing here mutates
    anything, so this stays a plain synchronous call rather than going through Runner/@work."""
    try:
        result = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout)
        return (result.stdout or result.stderr or "(no output)").strip()
    except Exception as e:
        return f"error running command: {e}"


class InfoModal(ModalScreen[None]):
    """Read-only scrollable popup for showing command output. Usage: self.app.push_screen(
    InfoModal("PCIe devices", run_shell_capture(cmd))) — scrolling is defined once here so
    every such popup (lspci, ip a, future ones) gets it for free instead of reimplementing it.
    """

    BINDINGS = [("escape", "dismiss_modal", "Close")]

    DEFAULT_CSS = """
    InfoModal {
        align: center middle;
    }
    #info-dialog {
        width: 80%;
        height: 80%;
        border: thick $background 80%;
        background: $surface;
        padding: 1 2;
    }
    #info-header {
        height: auto;
        margin-bottom: 1;
    }
    #info-title {
        width: 1fr;
        text-style: bold;
    }
    #info-body {
        height: 1fr;
    }
    """

    def __init__(self, title: str, content: str) -> None:
        super().__init__()
        self.info_title = title
        self.content_text = content

    def compose(self) -> ComposeResult:
        with Vertical(id="info-dialog"):
            with Horizontal(id="info-header"):
                yield Static(self.info_title, id="info-title")
                yield Button("×", id="info-close", classes="close-button", variant="error")
            with VerticalScroll(id="info-body"):
                yield Static(self.content_text)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "info-close":
            self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    """Usage inside a @work async handler: confirmed = await self.app.push_screen_wait(
    ConfirmModal("Rebuild cuda?")); if not confirmed: return
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

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
            with Horizontal(classes="button-row"):
                yield Button(self.confirm_label, variant="error" if self.danger else "primary", id="confirm-yes")
                yield Button("Cancel", id="confirm-no")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")
