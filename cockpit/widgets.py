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
  in-table   — not a Button: a `TableAction` declared on a SingleClickDataTable, rendered as
               `[ Update ]` rich Text in its own column and dispatched on a single click.
  inline     — a bare `Button(...)` inside `classes="inline-row"`, h3 so it lines up with
               the `Input` beside it. Styled distinctly on purpose.

Spacing tokens ($space-normal / $space-section / $space-edge) are NOT declared in this
stylesheet. Textual parses every CSS source against only the app's `get_css_variables()`,
so a `$var: value` written here is invisible to any screen's `DEFAULT_CSS` and referencing
it there raises UnresolvedVariableError at mount. They live in `CockpitApp.SPACE_TOKENS`
(cockpit/app.py), which feeds `get_css_variables()` and therefore reaches every source.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Callable
from rich.text import Text, TextType
from textual import work
from textual.app import ComposeResult, SuspendNotSupported
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.message import Message
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widget import Widget
from textual.widgets import Button, DataTable, Static
from textual.widgets.data_table import CellType, ColumnKey

from provision.common import Runner

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

/* Screen archetype tokens (DESIGN.md §8). Standardized action containers and key-value form
   fields shared across all screens. The `.res-*` gauge rows that used to live here went with
   the Dashboard's live measurement (plans/04) — nothing else ever used them. */
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
/* Action-row overflow at narrow widths (DESIGN.md §9). A single-row action row (the QA
   3c/5b fix) can hold enough buttons that it extends past screen.region.right at the 80x24
   floor even though it fits comfortably at normal widths ("Preview YAML" in deploy.py,
   "Remove selected" in scripts.py, "Check / Enable Tailscale" in settings.py were all
   unreachable there). Reuses the same Screen.-narrow/-wide hook as .columns-responsive
   (DESIGN.md §2): under -narrow the row stacks one button per line instead of staying a
   single row that runs off-screen; -wide is untouched, so normal-width screens keep the
   single row the QA fix asked for. Applies to every .action-row-* everywhere, since any
   future screen with enough buttons can hit this same overflow. */
Screen.-narrow .action-row-primary, Screen.-narrow .action-row-secondary {
    layout: vertical;
    height: auto;
}
Screen.-narrow .action-row-primary Button, Screen.-narrow .action-row-secondary Button {
    margin-bottom: $space-normal;
}
/* No `align-vertical: middle` here. It was declared and silently did nothing: Textual does
   not offset a short child inside a `height: auto` Horizontal, so a 1-line label sat on row 0
   of a 3-line Input — i.e. on its top border, not beside its value. The two margin rules below
   do the alignment explicitly instead of declaring an intent the layout never honoured. */
.form-row {
    height: auto;
    margin-bottom: $space-normal;
}
/* An Input/Select/Switch is 3 cells tall with its text on row 1, so the label drops one row to
   sit beside the value rather than above it. */
.form-label {
    width: 20;
    margin-top: $space-normal;
    text-style: bold;
    color: $text-muted;
}
/* A plain-text value (a status readout, the VPN resolution preview) is 1 cell tall with no
   border, so it takes the same offset as the label. Without this a multi-line status rendered
   a full line below its own label — most visible on WOL Status, whose three lines started
   under "Status" instead of beside it. Type + class outranks the bare `.status-text` rule
   above, so a value carrying both classes resolves here and not there. */
.form-row Static.form-field {
    margin-top: $space-normal;
}
/* `max-width` caps a field that would otherwise stretch the full width of a single-column
   panel. 20 + 40 = 60 cells per form row, so two panels fit side by side inside the 115-cell
   usable width above the breakpoint (see .columns-responsive) — which is the point: the
   Settings sub-tabs were a single tall column that had to be scrolled. */
.form-field {
    width: 1fr;
    max-width: 40;
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
/* Dashboard-left/right column gutter (Defect 4 cosmetic fix, plans/03-ui-qa-pass.md remediation
   pass). This used to live in DashboardScreen.DEFAULT_CSS, which is silently the wrong place: a
   widget's DEFAULT_CSS is SCOPED_CSS=True by default, and Textual's parser (css/parse.py) only
   leaves a rule's selector alone when its FIRST token already names the scope type — otherwise
   it prepends an implicit "DashboardScreen " ancestor requirement. A rule starting with
   "Screen.-wide ..." got rewritten to require a DashboardScreen ancestor of a Screen, which can
   never match (Screen is always the ancestor, never the descendant), so margin-right silently
   stayed 0 at every width — confirmed live: #dashboard-left.styles.margin was Spacing(0,0,1,0)
   at 121x30 with the rule still present in DEFAULT_CSS. SHARED_CSS is App.CSS, registered with
   an empty scope, so the identical selector works here the same way .columns-responsive already
   does above. */
/* A single cell of whitespace was not enough to tell the left column's content from the
   right's — a long "Endpoint: ..." line in the right column read as a continuation of the
   GPU row beside it. A rule plus $space-section either side gives 5 cells and an unambiguous
   boundary, which is cheaper to read than more whitespace would be at any width that still
   leaves room for the columns themselves. */
/* Both columns fill the row, so the divider below runs its full length. This has to be here
   rather than in DashboardScreen.DEFAULT_CSS: `.panel` sets `height: auto` and lives in this
   sheet, which is App.CSS — a higher tier than any widget's DEFAULT_CSS — so an id selector
   over there loses to a class selector over here regardless of specificity. Measured: the rule
   in DEFAULT_CSS left `styles.height` as `auto` and the rule was simply inert. */
Screen.-wide DashboardScreen #dashboard-left,
Screen.-wide DashboardScreen #dashboard-right {
    height: 1fr;
}
Screen.-wide DashboardScreen #dashboard-left {
    margin-right: $space-section;
    padding-right: $space-section;
    border-right: solid $surface-lighten-2;
}
Screen.-narrow DashboardScreen #dashboard-left {
    margin-right: 0;
    padding-right: 0;
    border-right: none;
    margin-bottom: $space-section;
}
/* Same treatment for the Settings sub-tabs' paired panels — same reason, same rule shape, and
   here too it has to live in SHARED_CSS rather than SettingsScreen.DEFAULT_CSS: a scoped
   DEFAULT_CSS rule beginning with "Screen..." is rewritten to require a SettingsScreen
   ancestor of a Screen, which never matches. */
/* Each side is a .settings-column wrapper rather than a bare .panel, because a column can
   hold more than one panel (System Services stacks Wake-on-LAN above Tailscale). */
SettingsScreen .settings-column {
    width: 1fr;
    height: auto;
}
Screen.-wide SettingsScreen .settings-columns > .settings-column:first-of-type {
    margin-right: $space-section;
    padding-right: $space-section;
    border-right: solid $surface-lighten-2;
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
/* Zebra phase (DESIGN.md §4.4, second half). Textual tints the EVEN rows (0, 2, 4...) and
   leaves the odd ones at $surface. Combined with the transparent resting cursor below, that
   desynced the stripes at the top of every table: the cursor rests on row 0, repaints it
   $surface, and rows 0 and 1 then read as one double-height band with the alternation
   starting one row late — "the first two lines are always highlighted". Swapping the phase so
   the tint lands on the odd rows makes row 0's cursor repaint a no-op, and the stripes run
   correctly from the first row. */
DataTable > .datatable--even-row {
    background: $surface;
}
DataTable > .datatable--odd-row {
    background: $surface-darken-1 40%;
}
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


def escape_markup(value: str) -> str:
    """Escape operator/vendor data for a markup-enabled Static. **Not `rich.markup.escape`.**

    Textual 8.2.8 parses its own content markup, and it accepts tags Rich does not: Rich's
    tag regex requires `[a-z#/@]` after the bracket, Textual's does not. So a PCI product name
    like `Intel Corporation DG2 [Arc A770]` is left untouched by BOTH `rich.markup.escape` and
    `textual.markup.escape` — each is built around the Rich-era rule — and is then eaten by
    Textual's parser, truncating the value at the bracket with no error. Measured in a live
    app at 70 cells: the plain string rendered as `gpu-intel · Intel Corporation DG2`.

    A literal backslash is the only escape that survives, so this doubles backslashes first
    and then escapes every `[`. Use it for anything an operator or a vendor supplies — a
    container name, a script id, a device name. Table cells use `rich.text.Text` instead
    (DESIGN.md §4.6), which bypasses markup parsing entirely and needs none of this.
    """
    return value.replace("\\", "\\\\").replace("[", "\\[")


_SERVICE_GLYPHS: dict[bool | None, tuple[str, str]] = {
    True: ("✔", "$success"),
    False: ("✖", "$error"),
    None: ("●", "$text-muted"),
}


def service_row(name: str, state: bool | None, detail: str = "", width: int = 22) -> str:
    """One service line for the Dashboard: **name first, then the glyph, then muted detail.**

    The glyph used to lead (`✔ llama-swap: running`), which put it in a different column on
    every line and made the one ✖ in a list of ✔ hard to find. Name-then-glyph gives a single
    scannable column, which is the only thing the mark is for.

    `state` is three-valued on purpose: True is running/enabled, False is not, and **None is
    "not managed or not knowable"** — a host without docker, a timer that was never installed.
    An error is None with the error text as `detail`; it is not a ✖, because ✖ means "this is
    off" and an unreachable daemon is not the same claim.

    Returns console markup, so `name` and `detail` go through `escape_markup` — a container
    named `[prod] web` would otherwise be truncated to nothing with no error. Padding is
    applied before escaping, since the escape is invisible in the rendered width.
    """
    glyph, colour = _SERVICE_GLYPHS[state]
    line = f"{escape_markup(name.ljust(width))} [{colour}]{glyph}[/]"
    if detail:
        line += f"  [$text-muted]{escape_markup(detail)}[/]"
    return line


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

    Screens don't normally call this directly — declare a `TableAction` and let
    `SingleClickDataTable.add_action_column` own rendering, width and dispatch.
    """
    return Text(f"[ {label} ]", style=f"bold {AMBER_THEME.error}" if destructive else "")


@dataclass(frozen=True)
class TableAction:
    """One per-row action column on a SingleClickDataTable (DESIGN.md §9, in-table archetype).

    Declared once next to the table's other columns; rendering, column width, click dispatch
    and confirmation all follow from the declaration rather than being re-done per screen.

    id          the action name handed back to `CockpitScreenBase.handle_table_action`, and
                also the DataTable column key (one column per action, so they coincide).
    label       the text inside the brackets: `[ Update ]`. A callable of the row key instead
                makes the column a *state toggle* — one column that reads `[ Start ]` on a
                stopped row and `[ Stop ]` on a running one. Two static columns would reserve
                both widths forever; the Scripts table only fits its six operations because
                start/stop and enable/disable are one toggle column each. A callable label
                must pass `width=`, since there is no single label to measure.
    width       column width override. Default is `len(label) + 4` (`action_cell`'s contract).
    destructive removes/deletes: $error-styled cell, and confirmed before it fires.
    confirm     prompt for a non-destructive action that still changes host state (a service
                restart). `{row}` is substituted with the row key and `{action}` with this
                row's resolved label. Destructive actions get a default prompt and don't need
                this.
    requires_root
                this action writes under /etc, /opt or /usr/local, or drives systemctl/apt.
                The confirm gate then elevates first (see CockpitScreenBase.confirm) and the
                handler must use `self.privileged_runner`.
    available   predicate on the row key: when it returns False the cell renders blank and
                clicking it does nothing. This is how "Update" appears only on rows that
                actually have an update. None = always available.
    """

    id: str
    label: str | Callable[[str], str]
    width: int | None = None
    destructive: bool = False
    confirm: str | None = None
    requires_root: bool = False
    available: Callable[[str], bool] | None = None

    @property
    def column_width(self) -> int:
        """`action_cell`'s width contract: the label plus two brackets and two spaces."""
        if self.width is not None:
            return self.width
        if callable(self.label):
            raise ValueError(f"action {self.id!r}: a callable label must pass an explicit width=")
        return len(self.label) + 4

    def resolve_label(self, row_key: str) -> str:
        return self.label(row_key) if callable(self.label) else self.label

    def cell(self, row_key: str) -> Text:
        if self.available is not None and not self.available(row_key):
            return Text("")
        label = self.resolve_label(row_key)
        if not label:
            return Text("")
        return action_cell(label, destructive=self.destructive)

    def confirm_message(self, row_key: str) -> str | None:
        """None = fire immediately. Anything else goes through CockpitScreenBase.confirm()."""
        label = self.resolve_label(row_key)
        if self.confirm is not None:
            return self.confirm.format(row=row_key, action=label)
        if self.destructive:
            return f"{label} {row_key}?"
        return None


class TableActionInvoked(Message):
    """Posted by SingleClickDataTable when an action cell is clicked. Bubbles to the enclosing
    CockpitScreenBase, which handles confirmation and then calls `handle_table_action` — a
    screen should not handle this message itself or it bypasses the confirm step."""

    def __init__(self, table: DataTable, action: TableAction, row_key: str) -> None:
        super().__init__()
        self.table = table
        self.action = action
        self.row_key = row_key

    @property
    def control(self) -> DataTable:
        return self.table


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
    two clicks to select an unvisited row). A tick-able table (Backends, Downloads models,
    Scripts) needs one click per row to toggle it, so this mirrors DataTable._on_click but always
    posts the selection message instead of only when the click matches the existing cursor.

    It also hosts the per-row action columns (DESIGN.md §9, in-table archetype). Declare them
    *after* every data column — `action_cells()` returns them in declaration order, to be
    splatted onto the end of each `add_row` call:

        table.add_column("ID", width=20)
        table.add_action_column(TableAction("update", "Update", available=self._has_update))
        table.add_action_column(TableAction("remove", "Remove", destructive=True))
        ...
        table.add_row(row_id, *table.action_cells(row_id), key=row_id)

    A click on an action cell posts TableActionInvoked, which CockpitScreenBase turns into a
    (confirmed, if the action asks for it) call to the screen's `handle_table_action`. It never
    moves the cursor and never posts RowSelected, so an action column and a tick-box column
    coexist on the same table without fighting.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Insertion-ordered: this is also the left-to-right order of the action columns.
        self._actions: dict[str, TableAction] = {}

    def add_action_column(self, action: TableAction) -> ColumnKey:
        """Add `action` as its own column. Width comes from the label (DESIGN.md §4 satisfied
        without the caller restating it) and the column key is the action id."""
        if action.id in self._actions:
            raise ValueError(f"action id {action.id!r} is already a column on this table")
        self._actions[action.id] = action
        return super().add_column("", width=action.column_width, key=action.id)

    def action_cells(self, row_key: str) -> list[Text]:
        """This row's action cells, in declared order — splat onto the end of add_row()."""
        return [action.cell(row_key) for action in self._actions.values()]

    def refresh_action_cells(self, row_key: str) -> None:
        """Re-evaluate one row's action cells in place after its state changed (e.g. an update
        was applied, so the Update cell should now be blank). `update_cell` per cell rather than
        clear() + re-add_row(): a rebuild resets the cursor and scroll position."""
        for action in self._actions.values():
            self.update_cell(row_key, action.id, action.cell(row_key))

    def _action_at_column(self, column_index: int) -> TableAction | None:
        if not self._actions or not 0 <= column_index < len(self.columns):
            return None
        return self._actions.get(self.ordered_columns[column_index].key.value)

    async def _on_click(self, event) -> None:
        # prevent_default() first, unconditionally: Textual dispatches an event to *every*
        # handler down the MRO, so without it DataTable._on_click also runs after this one.
        # That was the tick-box bug — this override moves the cursor to the clicked cell, which
        # makes the base handler's `new_coordinate == self.cursor_coordinate` test true, so it
        # posted a second RowSelected and the screen toggled the row straight back off.
        # event.stop() does not cover this: it stops bubbling to the parent, not MRO dispatch.
        # Branches below that want the base behaviour call super() explicitly.
        event.prevent_default()
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
        action = self._action_at_column(column_index)
        if action is not None:
            row_key, _ = self.coordinate_to_cell_key(Coordinate(row_index, column_index))
            key = row_key.value
            if key is not None and (action.available is None or action.available(key)):
                self.post_message(TableActionInvoked(self, action, key))
            event.stop()
            return
        self.cursor_coordinate = Coordinate(row_index, column_index)
        self._post_selected_message()
        self._scroll_cursor_into_view(animate=True)
        event.stop()


async def acquire_sudo(app) -> Runner | None:
    """Authenticate for one privileged action and return an elevated Runner (None on failure).

    `sudo -n -v` first: with a live credential cache this needs no password and no suspend, so
    a run of several privileged actions prompts once rather than every time. Only when that
    fails does the TUI step aside for a real prompt — Textual owns the terminal, so a password
    prompt cannot be rendered from inside it.

    Module-level rather than a CockpitScreenBase method because RetainedBuildsModal is a
    ModalScreen (a different widget-tree root) and its rollback/remove write under prefix_root
    just the same.
    """
    if shutil.which("sudo") is None:
        app.notify("sudo is not installed — restart the cockpit as root to run this", severity="error")
        return None
    if subprocess.run(["sudo", "-n", "-v"], capture_output=True).returncode != 0:
        try:
            with app.suspend():
                print("\nllm-server-cockpit needs root for this action.")
                authenticated = subprocess.run(["sudo", "-v"]).returncode == 0
        except SuspendNotSupported:
            # Some drivers can't hand the terminal back (headless, and the test pilot). There
            # is nowhere to render a password prompt, so say what to do instead of failing
            # with an opaque traceback from inside a worker.
            app.notify(
                "cannot prompt for a sudo password here — restart with: sudo bin/cockpit",
                severity="error",
            )
            return None
        if not authenticated:
            app.notify("sudo authentication failed or was cancelled", severity="error")
            return None
    return Runner(sudo=True)


def root_note(message: str) -> str:
    """Appended to a confirm prompt for an action that needs root and hasn't got it."""
    return message + (
        "\n\nThis needs root. Confirming will ask for your sudo password in the terminal, "
        "then run the action as root."
    )


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
    3. Per-row table actions land in `handle_table_action` already confirmed. A screen declares
       `TableAction`s on its table and implements one `handle_table_action`; it never handles
       TableActionInvoked itself, which is what keeps "destructive actions ask first" a
       property of the declaration rather than of each call site remembering to await confirm().
    """

    _privileged_runner: Runner | None = None
    _first_view_done: bool = False

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

    def on_first_view(self) -> None:
        """This tab's initial data load, run the first time it is actually visible.

        Defaults to the same work as a refresh, which is what most tabs want. Override only
        when first view legitimately does more than a refresh does — BuildsScreen's upstream
        version check is the one case (a network call that 'r' deliberately does not repeat).
        """
        self.on_refresh_requested()

    def ensure_first_view(self) -> bool:
        """Run on_first_view() once, the first time this tab becomes visible.

        Textual mounts every TabPane's content up front, so without this every screen's initial
        load fires at launch even though the operator can only see one of them — 20 subprocesses
        and 4 GitHub calls to render the Dashboard. Data reads therefore belong here, not in
        on_mount; on_mount stays for structure (table columns, form population) that costs
        nothing.
        """
        if self._first_view_done:
            return False
        self._first_view_done = True
        self.on_first_view()
        return True

    @property
    def privileged_runner(self) -> Runner:
        """The Runner for work behind `confirm(requires_root=True)` — never `self.runner`.

        A separate object on purpose: `self.runner` is shared app-wide, so flipping its sudo
        flag would leak elevation into actions that must NOT run as root. An HF download under
        sudo writes root-owned files into models_dir, which is the operator's to manage.
        """
        return self._privileged_runner if self._privileged_runner is not None else self.runner

    async def confirm(
        self,
        message: str,
        *,
        confirm_label: str = "Confirm",
        mutates_system: bool = False,
        danger: bool = False,
        requires_root: bool = False,
    ) -> bool:
        """Await a ConfirmModal. `mutates_system=True` forces the DESIGN.md §5 danger tier
        (host-level state, a service restart, or an irreversible change) — pass it instead of
        deciding `danger=` independently at each call site.

        `requires_root=True` marks an action that writes under /etc, /opt or /usr/local, or
        drives systemctl/apt. The cockpit is normally launched unprivileged (bin/cockpit), so
        such an action used to fail with a raw non-zero exit from `install` or `systemctl`
        deep inside a worker. The prompt now says root is needed, and confirming authenticates
        before the action runs. Handlers behind this flag must use `self.privileged_runner`.
        """
        needs_sudo = requires_root and os.geteuid() != 0
        if needs_sudo:
            message = root_note(message)
        confirmed = bool(
            await self.app.push_screen_wait(
                ConfirmModal(message, confirm_label=confirm_label, danger=mutates_system or danger)
            )
        )
        if not confirmed:
            return False
        if needs_sudo:
            elevated = await acquire_sudo(self.app)
            if elevated is None:
                return False
            self._privileged_runner = elevated
        return True

    @work
    async def _on_table_action_invoked(self, event: TableActionInvoked) -> None:
        """Confirmation gate for every per-row table action. @work because push_screen_wait
        needs a worker context; the message pump is not blocked while the modal is up."""
        event.stop()
        message = event.action.confirm_message(event.row_key)
        if message is None and event.action.requires_root:
            # A root-needing action always confirms: the sudo prompt is part of that dialog.
            message = f"{event.action.resolve_label(event.row_key)} {event.row_key}?"
        if message is not None and not await self.confirm(
            message,
            confirm_label=event.action.resolve_label(event.row_key),
            mutates_system=True,
            requires_root=event.action.requires_root,
        ):
            return
        await self.handle_table_action(event.action.id, event.row_key, event.table)

    async def handle_table_action(self, action_id: str, row_key: str, table: DataTable) -> None:
        """Run a per-row action already confirmed by `_on_table_action_invoked`. Implement this
        on any screen that declares a TableAction; `action_id` is the TableAction's id and
        `row_key` the clicked row's key."""
        raise NotImplementedError(
            f"{type(self).__name__} declares a TableAction but implements no handle_table_action()"
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

    # TableAction's own contracts, no App needed.
    assert TableAction("u", "Update").column_width == len("[ Update ]")
    assert TableAction("u", "Update").confirm_message("row-1") is None
    assert TableAction("d", "Delete", destructive=True).confirm_message("row-1") == "Delete row-1?"
    assert (
        TableAction("r", "Restart", confirm="Restart {row} now?").confirm_message("svc")
        == "Restart svc now?"
    )
    assert TableAction("u", "Update", available=lambda k: k == "yes").cell("no").plain == ""

    import asyncio

    asyncio.run(_driven_click_check())

    print("cockpit.widgets self-check OK")


async def _driven_click_check() -> None:
    """The half of the self-check that needs a real running app and real mouse clicks.

    Three properties, all of them things that regress silently because nothing raises when
    they break — the table just quietly does the wrong thing:
      a) clicking an action cell dispatches that action for that row, and only there;
      b) a destructive action does not run until the operator confirms;
      c) a tick-box click toggles the row ON and the state survives a table rebuild.
    (c) is the item-2h regression test: DataTable click handling used to fire RowSelected twice
    per click (see SingleClickDataTable._on_click), so every toggle immediately undid itself.
    """
    from textual.app import App
    from textual.widgets import DataTable

    fired: list[tuple[str, str]] = []

    class _Screen(CockpitScreenBase):
        def compose(self) -> ComposeResult:
            with VerticalScroll():
                table = SingleClickDataTable(id="t")
                table.cursor_type = "row"
                yield table

        def on_mount(self) -> None:
            super().on_mount()
            table = self.query_one("#t", SingleClickDataTable)
            table.add_column("", width=3)
            table.add_column("ID", width=10)
            table.add_action_column(TableAction("update", "Update", available=lambda k: k != "c"))
            table.add_action_column(TableAction("remove", "Remove", destructive=True))
            self.ticked: set[str] = set()
            self.rebuild()

        def rebuild(self) -> None:
            table = self.query_one("#t", SingleClickDataTable)
            table.clear()
            for key in ("a", "b", "c"):
                table.add_row(
                    selection_marker(key in self.ticked), key, *table.action_cells(key), key=key
                )

        def on_refresh_requested(self) -> None:
            self.rebuild()

        def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
            self.ticked.symmetric_difference_update({event.row_key.value})
            self.rebuild()

        async def handle_table_action(self, action_id: str, row_key: str, table: DataTable) -> None:
            fired.append((action_id, row_key))

    class _App(App):
        CSS = SHARED_CSS
        SPACE_TOKENS = {"space-normal": "1", "space-section": "2", "space-edge": "3"}

        def get_css_variables(self) -> dict[str, str]:
            return {**super().get_css_variables(), **self.SPACE_TOKENS}

        def compose(self) -> ComposeResult:
            yield _Screen()

    app = _App()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = app.query_one(_Screen)
        table = app.query_one("#t", SingleClickDataTable)

        async def click(row: int, column: int) -> None:
            region = table._get_cell_region(Coordinate(row, column))
            await pilot.click(table, offset=(region.x + 1, region.y))
            await pilot.pause()

        # (c) tick-box: one click ticks row 1, and the rebuild it triggers doesn't untick it.
        await click(1, 0)
        assert screen.ticked == {"b"}, f"tick-box toggle lost: {screen.ticked}"
        screen.on_refresh_requested()
        await pilot.pause()
        assert screen.ticked == {"b"}, f"tick state did not survive a rebuild: {screen.ticked}"
        assert table.get_cell_at(Coordinate(1, 0)).plain == "[x]"
        await click(1, 0)
        assert screen.ticked == set(), f"second click did not untick: {screen.ticked}"

        # (a) action dispatch: right action, right row, and no row toggle from an action click.
        await click(0, 2)
        assert fired == [("update", "a")], f"action dispatch wrong: {fired}"
        assert screen.ticked == set(), "an action click must not toggle the row"
        # ...and an unavailable cell is inert (row "c" has no Update).
        await click(2, 2)
        assert fired == [("update", "a")], f"unavailable action fired: {fired}"

        # (b) destructive action: nothing happens until the ConfirmModal is answered.
        fired.clear()
        await click(0, 3)
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal), "a destructive action must ask first"
        assert app.screen.danger is True, "a destructive action must use the §5 danger tier"
        await pilot.press("escape")
        await pilot.pause()
        assert fired == [], f"a cancelled destructive action must not run: {fired}"

        await click(0, 3)
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)
        await pilot.click("#confirm-yes")
        await pilot.pause()
        assert fired == [("remove", "a")], f"confirmed destructive action did not run: {fired}"

        # In-place refresh of one row's action cells, no rebuild.
        table.refresh_action_cells("a")
        assert table.get_cell_at(Coordinate(0, 2)).plain == "[ Update ]"


if __name__ == "__main__":
    _self_check()
