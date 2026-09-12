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

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, DataTable, Static

AMBER_THEME = Theme(
    name="cockpit-amber",
    primary="#e5a93c",
    secondary="#d97706",
    accent="#f5a623",
    warning="#f59e0b",
    error="#ef4444",
    success="#10b981",
    background="#121214",
    surface="#1a1a1e",
    panel="#1e1e24",
    dark=True,
)

SHARED_CSS = """
Header {
    background: #e5a93c;
    color: #000000;
    text-style: bold;
}
Tabs {
    background: transparent;
}
Tab {
    padding: 0 2;
}
/* No custom Tab.-active rule here on purpose. Textual's built-in Tabs:focus rule already
   paints the active tab with a solid $block-cursor-background (AMBER_THEME.primary) and a
   contrasting dark $block-cursor-foreground — legible out of the box, and it's what every
   screenshot below was verified against. The app previously added its own `Tab.-active {
   color: #f5a623; ... }` on top of that: same property, different CSS source (app-level flat
   rule vs. the widget's own nested `&:focus { &.-active {...} } ` rule), and that combination
   made Textual drop the tab's label glyphs entirely — not just poor contrast, no text at all —
   confirmed by directly inspecting the rendered Tab (correct resolved color/background, empty
   painted cells) and reproduced with intentionally high-contrast colors instead of amber, which
   also came out blank. Leave Tab's active styling to Textual's default rather than fight it. */
.panel {
    /* Textual's Vertical container defaults to height: 1fr — fine for a flex layout, wrong
       for a grouped box of fields meant to size to its own content. Without this override,
       every Vertical(classes="panel") inside a VerticalScroll stretches to fill an even share
       of the scroll viewport instead of hugging its content, opening large dead gaps between
       panels (most visible once a panel holds little content, e.g. a short DataTable). */
    height: auto;
    padding: 0 1;
    margin-bottom: 1;
}
.panel-title {
    text-style: bold;
}
.subtitle {
    color: $text-muted;
    margin-bottom: 1;
    text-style: italic;
}
.section-title {
    text-style: bold;
    color: $accent;
    margin-top: 1;
    margin-bottom: 1;
}
.thin-button {
    height: 1;
    min-width: 10;
    border: none;
    padding: 0 1;
    margin-right: 1;
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
.data-table {
    height: auto;
    max-height: 15;
    margin-bottom: 1;
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


def selection_marker(selected: bool) -> Text:
    """Column-0 tick mark for a tick-able DataTable row — see SingleClickDataTable. Plain text,
    not a Checkbox widget: selection state lives in the screen's own `set[str]` of row keys,
    toggled on DataTable.RowSelected, and the table is rebuilt (clear() + re-add_row()) so this
    marker always reflects current state rather than being independently mutated."""
    return Text("[x]" if selected else "[ ]", style="bold" if selected else "")


class SingleClickDataTable(DataTable):
    """A DataTable that selects a row on the first click instead of Textual's default, which
    requires the cursor to already be on a row before a click there counts as a selection (i.e.
    two clicks to select an unvisited row). A tick-able table (Installs backends, Downloads
    models) needs one click per row to toggle it, so this mirrors DataTable._on_click but always
    posts the selection message instead of only when the click matches the existing cursor."""

    async def _on_click(self, event) -> None:
        self._set_hover_cursor(True)
        meta = event.style.meta
        if "row" not in meta or "column" not in meta:
            return
        row_index = meta["row"]
        column_index = meta["column"]
        is_header_click = self.show_header and row_index == -1
        is_row_label_click = self.show_row_labels and column_index == -1
        if is_header_click or is_row_label_click or not self.show_cursor or self.cursor_type == "none":
            await super()._on_click(event)
            return
        self.cursor_coordinate = Coordinate(row_index, column_index)
        self._post_selected_message()
        self._scroll_cursor_into_view(animate=True)
        event.stop()


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
