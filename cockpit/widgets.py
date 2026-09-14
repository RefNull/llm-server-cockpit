"""Shared cockpit infrastructure: one ConfirmModal so every screen asks the same way before
an action that isn't a no-op under dry-run (build, rollback, download, restart llama-swap),
and one shared stylesheet (SHARED_CSS, set as CockpitApp.CSS) so layout/scroll bugs get
fixed once here rather than rediscovered and patched separately in every screen.

Convention every screen follows: wrap the top-level compose() content in a VerticalScroll
(or a Horizontal of VerticalScrolls for a multi-column layout) so it scrolls instead of
clipping when the terminal is shorter than the content — this was the actual bug the
Settings tab hit before its scroll containers were made explicit and independently bounded.
Group related fields inside `classes="panel"` boxes with a `classes="panel-title"` heading;
use `classes="action-row-primary"` (or `-secondary` for subordinate actions) for a horizontal
row of action buttons, `classes="status-text"` for a muted status/result line, and
`classes="error-text"` for inline validation errors — defined once below instead of
redeclared per screen.

Buttons come in exactly three archetypes (DESIGN.md §9) and nothing else. They are named,
not lettered, because §8's *screen* archetypes own A/B/C and "archetype B" meaning both a
table-driven screen and an in-table cell was a collision waiting to be misread:
  action-row — `Button(..., classes="thin-button")`, h1/min-width 10/no border. The only
               general-purpose button. Colour via `variant=` only.
  in-table   — not a Button: `action_cell("Update")` -> rich Text in its own DataTable
               column, click-dispatched (wired up in Phase 2).
  inline     — a bare `Button(...)` inside `classes="inline-row"`, h3 so it lines up with
               the `Input` beside it. Styled distinctly on purpose.

Spacing tokens ($space-normal / $space-section / $space-edge) are NOT declared in this
stylesheet. Textual parses every CSS source against only the app's `get_css_variables()`,
so a `$var: value` written here is invisible to any screen's `DEFAULT_CSS` and referencing
it there raises UnresolvedVariableError at mount. They live in `CockpitApp.SPACE_TOKENS`
(cockpit/app.py), which feeds `get_css_variables()` and therefore reaches every source.
"""
from __future__ import annotations

import re
import subprocess
from importlib import import_module
from pathlib import Path
from rich.text import Text, TextType
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widget import Widget
from textual.widgets import Button, DataTable, Static
from textual.widgets.data_table import CellType, ColumnKey

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
    /* No horizontal padding on purpose (DESIGN.md §2 "Side inset"). `.panel` paints no border
       and no background, so side padding here was invisible indentation that put panelled
       screens (Dashboard, Builds, Settings) one cell further in than bare-table screens
       (Containers, Scripts, Deploy, Downloads). The single side inset is #main-tabs'. */
    margin-bottom: $space-normal;
}
.panel-title {
    text-style: bold;
}
.subtitle {
    color: $text-muted;
    margin-bottom: $space-normal;
    text-style: italic;
}
.section-title {
    text-style: bold;
    color: $accent;
    margin-top: $space-normal;
    margin-bottom: $space-normal;
}
/* The action-row button archetype (DESIGN.md §9) — the only general-purpose button in the
   app. Textual's default Button is `height: auto` with `border-top/bottom: tall` and
   `min-width: 16`, i.e.
   3 cells tall and 16 wide; this flattens it to one row and lets a short label be short.
   Textual's own `Button(compact=True)` was evaluated as the native mechanism and rejected:
   it only drops the border (measured h1), and leaves `min-width: 16` and no right margin,
   so "Edit" still reserves 16 cells. Colour stays the job of `variant=`. */
.thin-button {
    height: 1;
    min-width: 10;
    border: none;
    padding: 0 $space-normal;
    margin-right: $space-normal;
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
    margin-right: $space-normal;
}
/* The inline button archetype (DESIGN.md §9) — deliberately 3 cells tall so it lines up
   with the `Input` it sits beside, and coloured $secondary so it reads as "acts on this field", not
   as a page-level action that happens to be fat. Borders are set explicitly to match the
   background; Textual's default paints them from $surface, which looks broken on a tinted
   button. */
.inline-row Button {
    min-width: 10;
    height: 3;
    background: $secondary;
    color: $text-secondary;
    border-top: tall $secondary-lighten-1;
    border-bottom: tall $secondary-darken-2;
}
.inline-row Button:hover {
    background: $secondary-lighten-1;
    border-top: tall $secondary-lighten-2;
}
.status-text {
    color: $text-muted;
    margin-top: $space-normal;
}
.error-text {
    color: $error;
    margin-top: $space-normal;
}
.data-table {
    height: auto;
    max-height: 15;
    margin-bottom: $space-normal;
}

/* Screen archetype tokens (DESIGN.md §8). Standardized rows, gauges, action containers,
   and key-value form fields shared across all screens. */
.res-row {
    height: 1;
    align-vertical: middle;
    margin-bottom: 0;
}
.res-label {
    width: 6;
    text-style: bold;
    color: $accent;
}
.res-row ProgressBar {
    width: 1fr;
    height: 1;
}
.res-val {
    width: 18;
    text-align: right;
    color: $text-muted;
}
.action-row-primary {
    height: auto;
    align: left middle;
    margin-top: $space-normal;
    margin-bottom: 0;
}
.action-row-secondary {
    height: auto;
    align: left middle;
    margin-top: $space-normal;
    margin-bottom: 0;
}
/* One gap rule for every action row, replacing the single-screen
   `SettingsScreen .action-row-primary Button { margin-right: 1 }` that used to be the only
   place action-row buttons didn't abut. The action-row archetype carries its own
   margin-right; this also catches anything that lands in an action row without it. */
.action-row-primary Button, .action-row-secondary Button {
    margin-right: $space-normal;
}
.form-row {
    height: auto;
    align-vertical: middle;
    margin-bottom: $space-normal;
}
.form-label {
    width: 24;
    text-style: bold;
    color: $text-muted;
}
.form-field {
    width: 1fr;
}

/* Responsive breakpoint hooks (DESIGN.md §2, §3.5). CockpitApp.HORIZONTAL_BREAKPOINTS makes
   Textual stamp exactly one of these classes onto the Screen on every resize; layout reflow
   is expressed here as CSS, never as an on_resize geometry calculation in a screen.
   Any multi-column Horizontal that must collapse below 121 cells carries .columns-responsive. */
Screen.-narrow .columns-responsive {
    layout: vertical;
    height: auto;
}
Screen.-wide .columns-responsive {
    layout: horizontal;
}

/* No resting highlight on an un-focused table (DESIGN.md §4.4). A freshly mounted DataTable
   painted two overlapping bands before the operator had touched anything: the row-0 block
   cursor (show_cursor defaults True and cursor_coordinate defaults (0,0)) and the amber
   $secondary-muted band on every fixed column. Both read as "this row/column is selected"
   when nothing is.

   This is CSS only, never `show_cursor=False` — DataTable._on_click gates every selection
   message on show_cursor, so turning it off silently kills all click handling and keyboard
   navigation. Here the cursor still exists and still dispatches; it just paints nothing until
   the table has focus, at which point the :focus rules below repaint it. `--fixed` is pinned
   to $surface (DataTable's own background) so a pinned column reads as ordinary cells. */
DataTable > .datatable--cursor {
    background: transparent;
    color: $foreground;
    text-style: none;
}
DataTable > .datatable--fixed-cursor {
    background: transparent;
    color: $foreground;
}
DataTable > .datatable--fixed {
    background: $surface;
    color: $foreground;
}
DataTable:focus > .datatable--cursor {
    background: $block-cursor-background;
    color: $block-cursor-foreground;
    text-style: $block-cursor-text-style;
}
DataTable:focus > .datatable--fixed-cursor {
    background: $block-cursor-background;
    color: $block-cursor-foreground;
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


def action_cell(label: str, *, destructive: bool = False) -> Text:
    """The in-table button archetype (DESIGN.md §9): a per-row action rendered *as a table
    cell*, in its own column, e.g. `[ Update ]`. Not a Button — a Textual Widget has no `__rich_console__`,
    so `DataTable` cannot hold one; clicks are dispatched from the cell's style meta instead
    (the mechanism SingleClickDataTable below already uses; wired to actions in Phase 2).

    Returns `rich.text.Text`, never a `str`: the app console has markup enabled, so a raw
    "[ Update ]" would be eaten as a Rich tag and render as the empty string.

    Column width contract: `len(longest label in the column) + 4` (two brackets, two spaces).
    Destructive actions (Remove, Delete) take the theme's error colour rather than a
    differently-shaped label, so "this one is dangerous" reads at a glance down the column.
    """
    return Text(f"[ {label} ]", style=f"bold {AMBER_THEME.error}" if destructive else "")


class CockpitDataTable(DataTable):
    """DataTable with the DESIGN.md §4 column contract enforced at the API boundary.

    Textual's DataTable is a ScrollView with `overflow-x: auto`: a column without an explicit
    width sizes to its content, so one long cell silently pushes later columns off-screen with
    no visual cue that they exist. Rather than re-auditing every screen for that, `add_column`
    refuses a call that omits `width=`, and `add_columns` (the plural convenience wrapper,
    which has no width parameter at all) is refused outright. Failing loudly at construction
    is the point — this raises during on_mount, not at some later render.
    """

    def add_column(
        self,
        label: TextType,
        *,
        width: int | None = None,
        key: str | None = None,
        default: CellType | None = None,
    ) -> ColumnKey:
        if width is None:
            raise ValueError(
                f"DESIGN.md §4: {type(self).__name__}.add_column({label!r}) must pass an explicit width="
            )
        return super().add_column(label, width=width, key=key, default=default)

    def add_columns(self, *labels: TextType) -> list[ColumnKey]:
        raise ValueError(
            "DESIGN.md §4: add_columns() can't express per-column widths — "
            "call add_column(label, width=N) once per column instead"
        )


class SingleClickDataTable(CockpitDataTable):
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
        padding: $space-normal $space-section;
    }
    #info-header {
        height: auto;
        margin-bottom: $space-normal;
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
        padding: $space-normal $space-section;
    }
    #confirm-message {
        margin-bottom: $space-normal;
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
            with Horizontal(classes="action-row-primary"):
                yield Button(
                    self.confirm_label,
                    variant="error" if self.danger else "primary",
                    id="confirm-yes",
                    classes="thin-button",
                )
                yield Button("Cancel", id="confirm-no", classes="thin-button")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")


class CockpitScreenBase(Widget):
    """Base for every tab widget in cockpit/screens/ — not a Textual Screen (they are mounted
    inside TabPanes, see cockpit/app.py).

    Two things it makes structural rather than conventional:

    1. `on_refresh_requested()` is abstract. It used to be duck-typed — app.py did
       `if hasattr(widget, "on_refresh_requested")` — so a new tab that forgot it simply
       never responded to the global `r` binding, silently. Now the class won't mount.
       (Textual dispatches `on_mount` to every handler in the MRO, not just the most derived
       one, so a subclass defining its own `on_mount` does not shadow this check.)
    2. `confirm(..., mutates_system=True)` collapses the DESIGN.md §5 danger tier into one
       argument: "this restarts a service / changes host state / is irreversible" is what a
       call site knows, and the $error styling follows from it rather than being re-decided
       per call site.
    """

    def on_mount(self) -> None:
        if type(self).on_refresh_requested is CockpitScreenBase.on_refresh_requested:
            raise NotImplementedError(
                f"{type(self).__name__} must implement on_refresh_requested() "
                "(DESIGN.md — every tab answers the global 'r' refresh binding)"
            )
        scroll_bounded = (
            isinstance(self, VerticalScroll)
            or bool(self.query(VerticalScroll))
            or any(isinstance(ancestor, VerticalScroll) for ancestor in self.ancestors)
        )
        if not scroll_bounded:
            raise RuntimeError(
                f"DESIGN.md §3: {type(self).__name__} must contain (or sit inside) a VerticalScroll "
                "so its content scrolls instead of clipping below the 80x24 floor"
            )

    def on_refresh_requested(self) -> None:
        """Re-read this tab's state. Called by CockpitApp.action_refresh_all ('r')."""
        raise NotImplementedError

    async def confirm(
        self,
        message: str,
        *,
        confirm_label: str = "Confirm",
        mutates_system: bool = False,
        danger: bool = False,
    ) -> bool:
        """Await a ConfirmModal. `mutates_system=True` forces the DESIGN.md §5 danger tier
        (host-level state, a service restart, or an irreversible change) — pass it instead of
        deciding `danger=` independently at each call site."""
        return bool(
            await self.app.push_screen_wait(
                ConfirmModal(message, confirm_label=confirm_label, danger=mutates_system or danger)
            )
        )


def _self_check() -> None:
    """`python3 -m cockpit.widgets` — asserts the two DESIGN.md contracts this module exists
    to enforce still bite. Not a test framework, just the smallest thing that fails if the
    enforcement is ever quietly weakened."""
    table = CockpitDataTable()
    try:
        table.add_column("no width")
    except ValueError:
        pass
    else:
        raise AssertionError("DESIGN.md §4: add_column() without width= must raise")
    try:
        table.add_columns("a", "b")
    except ValueError:
        pass
    else:
        raise AssertionError("DESIGN.md §4: add_columns() must raise")
    # The positive path (add_column with a width) needs a running App for text measurement —
    # it's covered by every screen's on_mount instead, not duplicated here.

    class _Forgetful(CockpitScreenBase):
        pass

    try:
        _Forgetful().on_mount()
    except NotImplementedError:
        pass
    else:
        raise AssertionError("a screen without on_refresh_requested() must not mount")

    # Every $token SHARED_CSS references must be one CockpitApp actually declares. Textual
    # resolves variables per CSS source against get_css_variables() only, so an undeclared
    # one is not a typo that degrades — it raises UnresolvedVariableError at mount and the
    # app won't start. Imported here (not at module scope) because app.py imports this module.
    from cockpit.app import CockpitApp

    referenced = set(re.findall(r"\$space-[a-z-]+", SHARED_CSS))
    declared = {f"${name}" for name in CockpitApp.SPACE_TOKENS}
    if not referenced <= declared:
        raise AssertionError(f"SHARED_CSS references undeclared spacing tokens: {referenced - declared}")
    if re.search(r"^\s*\$space-[a-z-]+\s*:", SHARED_CSS, re.M):
        raise AssertionError("spacing tokens must live in CockpitApp.SPACE_TOKENS, not SHARED_CSS")
    values = list(CockpitApp.SPACE_TOKENS.values())
    if len(set(values)) != len(values):
        raise AssertionError(f"DESIGN.md §1: spacing tokens must have distinct values, got {values}")

    # DESIGN.md §1: no bare 1/2/3 as a margin/padding in any screen's DEFAULT_CSS. The old
    # check only looked at SHARED_CSS, which is the one file that was already compliant — it
    # could not see the ~22 hardcoded values that were sitting in cockpit/screens/*.py. This
    # walks the real DEFAULT_CSS attributes (not the file text) so comments and docstrings
    # can't trip it, and only inspects margin/padding: `height: 10` or `width: 1fr` is not
    # spacing. `0` and `auto` stay literal — there is no token for "no gap".
    offenders = []
    for path in sorted((Path(__file__).parent / "screens").glob("*.py")):
        module = import_module(f"cockpit.screens.{path.stem}")
        for obj in vars(module).values():
            # `obj.__module__` filter: a screen module imports Textual's own widgets into its
            # namespace, and Input/Select/TextArea carry bare paddings of their own.
            if not isinstance(obj, type) or obj.__module__ != module.__name__:
                continue
            if "DEFAULT_CSS" not in vars(obj):
                continue
            for prop, value in re.findall(
                r"^\s*((?:margin|padding)(?:-top|-right|-bottom|-left)?)\s*:\s*([^;]+);",
                vars(obj)["DEFAULT_CSS"],
                re.M,
            ):
                bare = [p for p in value.split() if not p.startswith("$") and p not in ("0", "auto")]
                if bare:
                    offenders.append(f"{obj.__qualname__}: {prop}: {value.strip()}")
    if offenders:
        raise AssertionError(
            "DESIGN.md §1: screen DEFAULT_CSS must use a $space-* token for margin/padding, "
            f"not a bare value — {offenders}"
        )

    # The in-table archetype renders through rich Text, not str — a str would be eaten by console markup.
    assert action_cell("Update").plain == "[ Update ]"
    assert AMBER_THEME.error in str(action_cell("Remove", destructive=True).style)

    print("cockpit.widgets self-check OK")


if __name__ == "__main__":
    _self_check()
