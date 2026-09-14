# Cockpit Design Standards

This document defines the interface standards, token system, terminal geometry contracts, and widget interaction rules for the Textual TUI (`cockpit/`). All screens (`cockpit/screens/*.py`) must adhere to these specifications.

---

## 1. Design Tokens

### Colors
Colors are defined strictly through the registered Textual theme `AMBER_THEME` in [`cockpit/widgets.py`](file:///Users/nystrom/GitHub/llm-server-cockpit/cockpit/widgets.py#L25-L37) as the single source of truth.
- **Rule**: Zero hex literals are permitted in screen-level `DEFAULT_CSS` or `SHARED_CSS` component rules.
- **Theme Tokens**: Screens must reference Textual generated theme variables:
  - `$primary`: Primary brand accent, focused states, primary action buttons.
  - `$secondary`: Secondary interactive borders and subdued indicators.
  - `$accent`: Headings, active tab underlines, section accents.
  - `$warning`: Non-fatal alerts, pending operations, cautionary status.
  - `$error`: Destructive action buttons, validation failure text (`.error-text`).
  - `$success`: Successful operations, operational status indicators.
  - `$background`: Base terminal canvas background.
  - `$surface`: Modal dialog containers, popups, elevated panels.
  - `$panel`: Grouped fieldsets (`.panel`), card containers.
  - `$text-muted`: Subtitles (`.subtitle`), secondary captions, hints, inert table cells.
  - `$text-disabled`: Inactive controls, disabled buttons.

### Spacing Scale
Spacing tokens are defined in cell units. Every token has a **distinct** value — a token whose value duplicates another's is a naming fiction, not a scale, and gets collapsed (`$space-tight` was a second name for `1` and has been removed; its two uses became `$space-normal`).

- `$space-normal: 1`: The default gap. Margin between consecutive rows/controls, button right margins, label bottom margins, panel bottom margins, gap below a tab bar.
- `$space-section: 2`: Boundary separation between distinct screen regions and dialog container padding.
- `$space-edge: 3`: The app's side inset. Applied to `#main-tabs` (which insets every screen) and to `CockpitHeader`, which sits outside the tabs and so has to restate it — see §2.

**Where they are declared (this is load-bearing, not bookkeeping).** Tokens live in `CockpitApp.SPACE_TOKENS` (`cockpit/app.py`), fed to Textual through `App.get_css_variables()`. They are **not** declared as `$name: value` lines in `SHARED_CSS`.

Textual parses each CSS source — `App.CSS` and every widget's `DEFAULT_CSS` — against `get_css_variables()` alone. A variable declared *inside* `App.CSS` is visible only within that same source; referencing it from a screen's `DEFAULT_CSS` raises `UnresolvedVariableError` and the app fails to start. Routing them through `get_css_variables()` is what makes a token usable in both places, which is the whole point of having a scale.

- **Rule**: Screen-level `DEFAULT_CSS` must use a spacing token, never a bare `1`/`2`/`3`, for any margin or padding. `0` and `auto` stay literal — there is no token for "no gap". Non-spacing integers (`height: 10`, `min-height: 14`, `width: 1fr`) are not spacing and stay literal.
- **Known exception, in `SHARED_CSS` only**: `Tab { padding: 0 2 }`. That is a widget's internal label metric (the same category as `.res-label { width: 6 }`), not separation between elements, and calling it `$space-section` — "boundary separation between distinct screen regions" — would be a naming fiction of exactly the kind the distinct-values rule exists to prevent. It stays literal and stays out of the check's scope.
- **Enforcement**: `python3 -m cockpit.widgets` asserts:
  - every `$space-*` referenced in `SHARED_CSS` is declared in `SPACE_TOKENS`;
  - no `$space-*: value` declaration has crept back into `SHARED_CSS`;
  - all token values are distinct;
  - **no class defined in `cockpit/screens/*.py` has a bare margin/padding value in its own `DEFAULT_CSS`.** This last one is the rule above, and it is the reason the check is worth running: the first three only ever looked at `SHARED_CSS`, the one file that was already compliant, while ~22 hardcoded `1`s and `2`s sat in the screens. It reads the live `DEFAULT_CSS` attribute rather than the file text (so comments and docstrings can't trip it), filters on `obj.__module__` (so Textual's own `Input`/`Select`/`TextArea` paddings, imported into a screen's namespace, aren't attributed to the screen), and only inspects `margin*`/`padding*`.

---

## 2. Terminal-Size Contract & Breakpoints

### Supported Floor: 80×24
- **Decision**: The minimum supported terminal dimension for `bin/cockpit` is strictly **80×24** cells.
- **Rationale**: 80×24 is a deliberate project constraint matching standard VT100 console geometry for headless server recovery and base SSH administration without window management. Textual defines no framework floor (it only falls back to 80×24 when terminal detection fails).

### Breakpoints
Responsive layout reflow is driven by `App.HORIZONTAL_BREAKPOINTS`:
```python
HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (121, "-wide")]
```
- **Threshold Rationale (121 cells)**: The threshold is derived, not chosen. `ASCII_BANNER` is **115 columns** wide (all six lines, measured), it is rendered inside `CockpitHeader`, and `CockpitHeader` carries the side inset — so the banner needs `115 + $space-edge + $space-edge` = **115 + 3 + 3 = 121** cells of terminal to render unclipped. At or above 121 (`Screen.-wide`) the full banner (`#banner-art`) is shown; below it (`Screen.-narrow`) the header collapses to the single-line compact brand label (`#banner-compact`) rather than wrap or clip.
- **This number moved, and why.** It was 120, derived as `115 + 2 + 2 = 119` → round up to 120, back when `CockpitHeader` still carried `margin: 0 2 1 2`. That margin was the last thing left at the old inset: every screen's content had moved to 3 (`#main-tabs { margin: 0 $space-edge }`) while the banner and the `v… · Host: …` line stayed one cell left of it. Moving the header's side margin to `$space-edge` fixes the alignment and costs two cells of banner clearance, so the breakpoint moves with it: **121**. Measured live — at 120 cells with a 3-cell inset the header is 114 wide and the 115-column banner does not fit. The old arithmetic was correct for the old margin; keeping 120 after the margin change would have meant a one-column clip at exactly 120, which is precisely the failure the breakpoint exists to prevent. `#dashboard-columns` side-by-side metric panels get more clearance at 121 than they had at 120, so that secondary justification is unaffected.
- **Scrollbar Containment Contract**: To prevent outer canvas overflow and double vertical scrollbars:
  ```css
  Screen { overflow: hidden; }
  TabbedContent { height: 1fr; }
  ```
  `Screen { overflow: hidden; }` prevents the root `Screen` container from painting an outer vertical scrollbar. `TabbedContent { height: 1fr; }` ensures the tabbed interface cleanly fills the vertical viewport between `CockpitHeader` and `Footer`. This one stays a *type* selector: the nested `TabbedContent`s (LLM, Settings) must fill their pane too. All scrolling is strictly delegated to inner view containers (e.g. `VerticalScroll` within individual screens).
- **Enforcement**: Layout transitions must use CSS classes (`Screen.-narrow`, `Screen.-wide`). Screens must never implement manual `on_resize` geometry calculations.

### Side Inset

Every screen's content begins at exactly **3 cells** from the left terminal edge:

```css
#main-tabs    { margin: 0 $space-edge; }              /* every screen */
CockpitHeader { margin: 0 $space-edge $space-normal $space-edge; } /* the banner, outside the tabs */
```

Two rules, one value. `CockpitHeader` is a sibling of `#main-tabs` in `CockpitApp.compose`, not a descendant, so it cannot inherit the inset and has to restate `$space-edge`. That is the whole exemption — the header is **not** allowed a different value, and there is no third place. The header's side margin is load-bearing for the §2 breakpoint (see above): change it and the breakpoint changes with it.

- **Rule**: No screen, panel, or scroll container may add horizontal padding or margin of its own for the purpose of insetting content. A screen that wants breathing room already has it.
- **Rationale (the bug this replaces)**: the rule used to be `TabbedContent { margin: 0 2 }` — a *type* selector, so it applied a second time to the `TabbedContent` nested inside the LLM and Settings tabs, and `.panel` then added a third cell via `padding: 0 1`. Net left inset was 2 (Containers, Scripts), 3 (Dashboard), 4 (LLM > Models, LLM > HF Downloads) or 5 (LLM > Backends, Settings) depending on how deeply a screen happened to be nested and whether it used a panel. Scoping the margin to the outer container's id and dropping `.panel`'s horizontal padding (`.panel` paints no border and no background, so that padding was invisible indentation) makes all nine sub-views land at 3.
- **`.panel` contract**: `.panel` carries `height: auto` and `margin-bottom` only. It must never carry horizontal padding.

### Tab-Bar Gap

Content must never begin on the row immediately beneath a tab bar:

```css
TabbedContent > ContentSwitcher { margin-top: $space-normal; }
```

Textual's `Tabs` is `height: 2`, docked top, with no bottom margin, and `TabPane` has no padding — nothing in the framework provides this gap. The child combinator on the type selector is deliberate and matches **all** tab levels (outer, LLM, Settings): `TabbedContent.compose` yields exactly one `ContentSwitcher`, so there is no second element to catch. Verified live on all three levels.

---

## 3. Page Structure Rules

1. **Max Tables per Screen**:
   - **Ceiling**: Maximum of **3** `DataTable` widgets per screen.
   - **Precedent**: Established by [`cockpit/screens/builds.py`](file:///Users/nystrom/GitHub/llm-server-cockpit/cockpit/screens/builds.py) which renders 3 tables (`backends-table`, `builds-table`, and `history-table`, with `backends-table` added during upstream feature additions). Exceeding 3 tables requires splitting workflows into distinct sub-views or tab panes rather than stacking.

2. **Filter & Search Placement**:
   - A filter/search `Input` must reside in its own dedicated row immediately preceding the table it filters, left-aligned.
   - It must **never** be placed inside an action row.
   - **Precedent**: Established proactively to standardize upcoming model/artifact search inputs without polluting action trigger rows.

3. **Action-Row Placement**:
   - **Canonical names**: `.action-row-primary` and `.action-row-secondary` (defined in `SHARED_CSS`, see §8). These are the *only* container classes for a horizontal row of buttons.
   - `.button-row` **no longer exists**. It was this document's original name (§3.2/§3.3) and `.action-row-*` was introduced later by §8; both then coexisted in the code, with `.button-row` on modal and wizard rows and `.action-row-*` on main-screen rows. The class has been deleted from `SHARED_CSS` and its 11 call sites renamed to `.action-row-primary`. Do not reintroduce it.
   - Action rows must be positioned immediately beneath the specific table or form they operate on. Buttons must never float into a generic screen-level footer.
   - **Precedent**: Matches [`cockpit/screens/downloads.py`](file:///Users/nystrom/GitHub/llm-server-cockpit/cockpit/screens/downloads.py) (download actions directly under `model-table`) and [`cockpit/screens/deploy.py`](file:///Users/nystrom/GitHub/llm-server-cockpit/cockpit/screens/deploy.py) (model mutations directly under `models-table`).

4. **Modal Trigger Placement**:
   - The button initiating a destructive or mutating action must trigger the `ConfirmModal` directly upon press.
   - Intermediate dropdown menus or ellipsis "..." action selectors are forbidden.
   - **Precedent**: Followed consistently across existing screens (e.g. "Build" button in [`cockpit/screens/builds.py:126`](file:///Users/nystrom/GitHub/llm-server-cockpit/cockpit/screens/builds.py#L126)).

5. **Two-Column Layout Threshold**:
   - Multi-column layouts (such as `#dashboard-columns`) are permitted only when the `Screen.-wide` class is active (terminal width ≥ 121).
   - Under `Screen.-narrow` (< 121 cells), multi-column containers must collapse to a single vertical stack (`layout: vertical`) via `.columns-responsive`.
   - **Precedent**: Settings was refactored upstream into `TabbedContent` (`#settings-tabs`), removing the former hardcoded `#settings-columns` layout. The responsive multi-column pattern is standardized via `.columns-responsive` on `#dashboard-columns`.

---

## 4. DataTable Contract

Textual's `DataTable` is a scroll container (`overflow-x: auto`) that scrolls horizontally when contents exceed view width. To prevent clipped columns and preserve discoverability:

1. **Explicit Column Widths**:
   - Every `add_column()` invocation must specify an explicit integer `width=`. Dynamic automatic sizing without bounds causes layout instability.
   - **Enforcement**: [`CockpitDataTable`](file:///Users/nystrom/GitHub/llm-server-cockpit/cockpit/widgets.py#L336-L354) overrides `add_column` to raise `ValueError` if `width` is omitted or None.
2. **Pinned Identifier Columns**:
   - Any table whose cumulative width can exceed the usable width of an 80-column terminal must set `fixed_columns >= 1` so the primary key remains pinned during horizontal scrolling.
3. **No Resting Highlight**:
   - A table that has not been interacted with must paint **no** selection band. A freshly mounted `DataTable` otherwise shows two overlapping ones: the row-0 block cursor (`show_cursor` defaults `True`, `cursor_coordinate` defaults `(0, 0)`, and `add_row` force-posts a highlight when the first row lands) and an amber `$secondary-muted` band down every fixed column (`datatable--fixed`). Both read as "this is selected" when nothing is.
   - **Enforcement**: CSS in `SHARED_CSS` only — `datatable--cursor` and `datatable--fixed-cursor` are transparent at rest and repaint under `DataTable:focus`; `datatable--fixed` is pinned to `$surface`, the table's own background.
   - **Forbidden**: `show_cursor=False`. `DataTable._on_click` gates *every* selection message on `show_cursor`, so disabling it silently destroys all click handling and keyboard navigation while appearing to fix the cosmetics. `grep -rn "show_cursor" cockpit/` must never find a `False`.

4. **Audit & Enforced Contracts**:
   - Every table in `cockpit/screens/` derives from `CockpitDataTable` — directly, or via `SingleClickDataTable`, which was rebased onto it so the tick-able tables are covered by the same enforcement rather than bypassing it.
   - A **tick-able** table's column 0 is the `[ ]`/`[x]` `selection_marker`, so its identifier lives in column 1 and pinning it requires `fixed_columns=2`. Plain tables pin column 0 with `fixed_columns=1`.
   - All 9 tables across 6 screens (Dashboard renders no table):
     - `backends-table` (`builds.py`, tick-able, `fixed_columns=2`): 4 columns ("" w=3, "Component" w=22 pinned, "Pinned" w=8, "Update" w=34).
     - `builds-table` (`builds.py`, `fixed_columns=1`): 4 columns ("Backend" w=12 pinned, "Ref" w=14, "Status" w=12, "Integrity" w=12).
     - `history-table` (`builds.py` `BuildHistoryModal`, `fixed_columns=1`): 4 columns ("Backend" w=12 pinned, "Timestamp (UTC)" w=22, "Outcome" w=16, "Detail" w=40).
     - `models-table` (`deploy.py`, `fixed_columns=1`): 6 columns ("ID" w=24 pinned, "Engine" w=12, "GPU" w=10, "Backend" w=10, "Group" w=12, "TTL" w=8).
     - `import-table` (`deploy.py`, tick-able, `fixed_columns=2`): 4 columns ("" w=3, "ID" w=24 pinned, "Engine" w=12, "cmd" w=60).
     - `model-table` (`downloads.py`, tick-able, `fixed_columns=2`): 5 columns ("" w=3, "ID" w=24 pinned, "Repo ID" w=36, "Quant File" w=26, "Status" w=32).
     - `containers-table` (`containers.py`, `fixed_columns=1`): 5 columns ("Name" w=24 pinned, "Image" w=30, "Status" w=22, "Ports" w=24, "Compose" w=9).
     - `scripts-table` (`scripts.py`, tick-able, `fixed_columns=2`): 4 columns ("" w=3, "ID" w=20 pinned, "Path" w=40, "Status" w=30).
     - `gpu-table` (`settings.py`, `fixed_columns=1`): 3 columns ("GPU ID" w=16 pinned, "Vendor" w=16, "Backends" w=30). Composed in both the first-run wizard and the normal-mode GPU Topology sub-tab — only one branch is ever mounted, with identical columns.

---

## 5. Modal & Danger Tier Contract

All non-idempotent or mutating operations must be confirmed through `ConfirmModal`.

### Danger Tier Definition
An action is classified as **Dangerous** (`danger=True`, rendering the confirm button with `$error` styling) if it:
1. Mutates host-level system state outside declarative preview files (e.g. network wake flags, system drivers).
2. Restarts or alters background system services or systemd timers (e.g. `llama-swap`, scheduled reboots).
3. Executes irreversible state modifications (e.g. deleting downloaded weights, rolling back runtime binaries).

### Expressing the Tier
Two idioms, split on which fact the call site actually knows:
- An action meeting one of the three clauses above calls `self.confirm(..., mutates_system=True)` (`CockpitScreenBase.confirm`), which derives `danger=True`. The call site declares *what the action does*, not how the button should look.
- A write confined to this repo's declarative files (`models.yaml`, `scripts.yaml`, `hosts/<hostname>.yaml`) is not tier-1 by the clause above, but is still confirmed with `ConfirmModal(..., danger=True)` directly — established precedent across this project: a config write is the thing a later `Deploy` turns into a system change, and operators treat it as consequential.

### Call-Site Audit & Enforcement
All 20 modal confirmation call sites across the 6 mutating screens (Dashboard is read-only and opens no modal):
- **Builds** (`builds.py`):
  - `_handle_update_selected_press`: `mutates_system=True` (tier 3 — compiles and swaps the backend runtime binary `current` points at).
  - `_handle_rollback_press`: `mutates_system=True` (tier 3 — repoints `current` at a different runtime binary).
- **Deploy** (`deploy.py`):
  - `_confirm_and_save`: `ConfirmModal(danger=True)` (declarative `models.yaml` write).
  - `_delete_selected`: `ConfirmModal(danger=True)` (declarative `models.yaml` write).
  - `_confirm_and_import_selected`: `ConfirmModal(danger=True)` (declarative `models.yaml` write).
  - `_confirm_and_apply`: `mutates_system=True` (tier 2 — installs systemd units and restarts `llama-swap`).
- **Downloads** (`downloads.py`):
  - `_confirm_and_download_selected`: `ConfirmModal(danger=True)` (batch download; the single-model download this replaced was `danger=False`, but a batch consumes bandwidth/storage at the same order as download-all).
  - `_confirm_and_download_all`: `ConfirmModal(danger=True)` (bulk download consuming significant network/storage).
- **Containers** (`containers.py`):
  - `_handle_restart_press`: `mutates_system=True` (tier 2 — restarts a running background service on the host).
  - `_handle_restart_all_press`: `mutates_system=True` (tier 2 — restarts every container service at once).
  - Exec shell, view logs, view compose: no modal — read-only, or a terminal handover the operator initiated explicitly.
- **Scripts** (`scripts.py`):
  - `_handle_bulk_press` (start / stop / enable / disable — one call site, four verbs): `mutates_system=True` (tier 2 — all four act on generated systemd units).
  - `_confirm_and_save`: `ConfirmModal(danger=True)` (declarative `scripts.yaml` write).
  - `_confirm_and_remove`: `ConfirmModal(danger=True)` (declarative `scripts.yaml` write; the systemd unit is deliberately left in place).
- **Settings** (`settings.py`):
  - `_real_deploy` (first-setup): `ConfirmModal(danger=True)` (writes a new `hosts/<hostname>.yaml`).
  - `_confirm_and_save_profile`: `ConfirmModal(danger=True)` (mutates an existing `hosts/<hostname>.yaml`).
  - `_confirm_and_deploy_profile`: `mutates_system=True` (tier 2 — saves profile, restarts `llama-swap` and syncs timers).
  - `_confirm_and_check_wol`: `mutates_system=True` (tier 1+2 — NIC wake flags, TLP/NetworkManager config, a systemd persistence unit).
  - `_confirm_and_check_tailscale`: `mutates_system=True` (tier 1 — installs a system package if missing and joins the host to a tailnet).
  - `_confirm_and_check_drivers`: `ConfirmModal()` with no `danger` (read-only inspection; exits loudly on drift, never auto-corrects).
  - `_confirm_and_apply_service`: `mutates_system=True` (tier 2 — restarts `llama-swap` and (re)installs/removes systemd timers).

---

## 6. Status Feedback & Notifications

Operators frequently trigger long-running actions and switch tabs while operations execute. Feedback mechanisms are strictly decoupled by execution plane and trigger source:

1. **Operator-Initiated Mutating Operations**:
   - **Requirement**: Operations intentionally triggered by the operator (builds, rollbacks, service deployments, batch downloads, container restarts, bulk script operations, hardware checks) must emit a toast notification (`self.app.notify(...)` or `self.app.call_from_thread(self.app.notify, ...)`) upon completion (both success and failure).
   - **Rationale**: An operator often switches tabs while a long-running mutating operation runs; an inline label on an inactive tab will not be seen once navigated away.
   - **Compliance**:
     - `builds.py`: `_run_build`, `_run_rollback`, and manual `_run_update_check` notify on completion/error.
     - `deploy.py`: `_apply_in_background` emits toast notifications for both success and failure.
     - `downloads.py`: `_run_download_batch` and `_run_download_all` notify on completion/error.
     - `containers.py`: `_run_restart` and `_run_restart_all` notify on completion/error.
     - `scripts.py`: `_run_bulk` notifies on completion/error.
     - `settings.py`: `_run_wol`, `_run_tailscale_check`, `_run_drivers_check`, and `_apply_service_in_background` notify on completion/error.

2. **Passive Background Workers**:
   - **Requirement**: Background pollers and periodic refreshers that execute automatically on mount or global refresh (`r`) must toast on **error only**, never on normal success.
   - **Rationale**: Emitting success toasts on passive reads would cause toast flooding whenever the app launches or when the operator refreshes tabs. Normal status is rendered directly inline into panel widgets or table rows. However, unhandled worker thread exceptions must toast errors so failures do not silently leave panels or status indicators stale.
   - **Compliance**:
     - `dashboard.py`: `_refresh_all` catches worker exceptions and toasts on error only; normal readings populate gauges and labels inline.
     - `containers.py`: `_refresh_table` runs passively on mount/refresh without success toast, rendering errors into `#docker-banner`.
     - `scripts.py`: `_refresh_table` runs passively on mount/refresh without success toast, rendering status inline in table cells.
     - `settings.py`: `_refresh_wol_status` and `_refresh_tailscale_status` update inline labels with no toast on normal success.
     - `builds.py`: initial `_run_update_check(notify_result=False)` on mount suppresses success toasts, only notifying on manual button presses.

3. **Synchronous Validation**:
   - Synchronous input validation errors (e.g. invalid YAML, missing required fields) must update inline `.status-text` or `.error-text` labels only.
   - Synchronous validation must **never** trigger toast notifications.

---

## 7. Multi-Surface Architecture

The planned browser surface (`textual-serve` / `textual-web`) requires no separate design language or parallel styling system:
- Textual web drivers execute the identical Python codebase, widget tree, and TCSS stylesheets, rendering into the browser via an xterm.js terminal canvas.
- All tokens, breakpoints, and page structure rules in this document apply identically across both local TTY and web surfaces.
- **Future Adaptation Boundary**: When standing up `textual-serve`, file downloads, log exports, and external URLs must be routed through Textual's surface-agnostic primitives (`App.deliver_text()`, `App.deliver_binary()`, `App.open_url()`) rather than direct local filesystem or desktop browser calls.

---

## 8. Screen Archetypes

> These are the only A/B/C archetypes in this document. §9's button archetypes used to run A/B/C too, which made "archetype B" name two unrelated things (and put a §9 archetype-C button inside a §8 archetype-C screen); they are now named **action-row / in-table / inline**. A/B/C stays here because `plans/02-screen-archetypes.md` is committed history that references it.

Every screen in `llm-server-cockpit` conforms to one of three structural archetypes:

### Archetype A: Operational Dashboard (`DashboardScreen`)
- **Role**: Real-time telemetry, service health, and resource utilization monitoring.
- **Layout**: Single vertical scroll wrapping status panels and responsive multi-column containers (`.columns-responsive`). Side-by-side at 121 columns and above; stacked vertically below.
- **Resource Gauge Metrics**: Standardized inline gauge rows using shared classes in `SHARED_CSS`:
  - `.res-row`: Container row (`height: 1; align-vertical: middle; margin-bottom: 0;`).
  - `.res-label`: Fixed metric identifier (`width: 6; text-style: bold; color: $accent;`).
  - `.res-row ProgressBar`: Flexible bar indicator (`width: 1fr; height: 1;`).
  - `.res-val`: Numerical readout (`width: 18; text-align: right; color: $text-muted;`).
- **Interaction Contract**: Read-only display. Passive background workers update gauges and status panels inline; toasts emit on error only. Zero modal triggers.

### Archetype B: Table-Driven Inventory (`Builds`, `Deploy`, `Downloads`, `Containers`, `Scripts`)
- **Role**: Collection browsing, status inspection, and lifecycle operations over system items.
- **Layout**: Strict `CockpitDataTable` containment (maximum 3 tables per screen), with pinned primary identifier columns (`fixed_columns >= 1`, or `2` for tick-able selection tables). Dedicated filter/search input row placed immediately above table if present.
- **Action Rows**: Standardized button rows positioned directly beneath the table:
  - `.action-row-primary`: Primary lifecycle actions (`height: auto; align: left middle; margin-top: $space-normal; margin-bottom: 0;`).
  - `.action-row-secondary`: Subordinate/management actions (`height: auto; align: left middle; margin-top: $space-normal; margin-bottom: 0;`).
  - Every `Button` inside either row gets `margin-right: $space-normal` from one `SHARED_CSS` rule. Per-screen overrides of that gap are forbidden (`SettingsScreen .action-row-primary Button { margin-right: 1 }` used to be the only place action-row buttons didn't abut; it has been deleted).
- **Interaction Contract**: Mutating operations require `ConfirmModal` with explicit danger tier styling. Operator-initiated actions emit completion toasts on both success and error.

### Archetype C: Form / Inspector (`SettingsScreen`)
- **Role**: Declarative configuration, host profile management, and deep system inspection.
- **Layout**: Structured `.panel` containers organized via sub-tabs (`TabbedContent`) or vertical sections.
- **Form Rows**: Standardized key-value and field rows:
  - `.form-row`: Container row (`height: auto; align-vertical: middle; margin-bottom: 1;`).
  - `.form-label`: Fixed label header (`width: 24; text-style: bold; color: $text-muted;`).
  - `.form-field`: Flexible input, select, or display widget (`width: 1fr;`).
- **Interaction Contract**: Synchronous validation failures update inline `.status-text` or `.error-text` only (never toast). File saves and hardware probes prompt via `ConfirmModal`.

---

## 9. Button Archetypes

> Button archetypes are named, not lettered. §8's **screen** archetypes own A/B/C; naming these ones **action-row / in-table / inline** is what keeps "archetype B" from meaning two unrelated things.

The app has **exactly three** button archetypes. There is no fourth, and geometry is never decided at a call site.

Textual's default `Button` is `height: auto` with `border-top`/`border-bottom: tall` and `min-width: 16` — it renders **3 cells tall and at least 16 wide**. That is never what this app wants outside an input row.

### The action-row button
The only general-purpose button. Every button outside a table and outside an `.inline-row` is this one.

- **Mechanism**: `Button(label, variant=..., classes="thin-button")`.
- **Geometry**: `height: 1; min-width: 10; border: none; padding: 0 $space-normal; margin-right: $space-normal`.
- **Colour**: from `variant=` only (`primary`, `warning`, `error`, default). Never a bespoke CSS colour per button.
- **`Button(compact=True)` was evaluated and rejected as the mechanism.** Textual 8.2.8 implements `compact` as `-textual-compact { border: none !important }` nested inside `&.-style-default`, which drops the border (measured `height: 1`) but leaves `min-width: 16` and adds no right margin. Measured in a live app: bare `Button("Update selected")` → 17×3; `compact=True` → 17×1; `.thin-button` → 19×1 with `min-width: 10`. For a short label the difference is the point — `Button("Edit")` reserves 16 cells compact and 10 with the class. The class is retained; do not swap it for `compact=`.

### The in-table action cell
A per-row action, rendered as a **table cell in its own column**, e.g. `[ Update ]`.

- **Mechanism**: declare a `cockpit.widgets.TableAction` on a `SingleClickDataTable` via `add_action_column(...)`; splat `table.action_cells(row_key)` onto the end of each `add_row`. Rendering (`action_cell` → `rich.text.Text`), column width, single-click dispatch (from the cell's style meta) and confirmation all follow from the declaration. A click arrives at the screen as `handle_table_action(action_id, row_key, table)`, already confirmed. Never handle `TableActionInvoked` on a screen — that bypasses the confirm step.
- **Action columns go last**, after every data column, in declaration order. A row's action cells are refreshed in place with `table.refresh_action_cells(row_key)`; do not `clear()` + rebuild to repaint one cell.
- **Conditional actions**: `TableAction(..., available=predicate)` — the cell renders blank and is inert on rows where the predicate is False (e.g. "Update" only where an update exists).
- **This is not a `Button` and cannot be.** A Textual `Widget` has no `__rich_console__`, so `DataTable` — whose cells are `list[RenderableType]` rendered through `console.render_lines` — raises `NotRenderableError` on one. Nothing may ever be mounted into a `DataTable` cell.
- **Rendering contract**: `[ Label ]`, one space inside each bracket. Column width is `len(longest label in that column) + 4`.
- **Returns `Text`, never `str`.** The app console has markup enabled, so a raw `"[ Update ]"` is parsed as a Rich tag and renders as the empty string.
- **Destructive actions** (Remove, Delete) take the theme's `$error` colour and bold, not a different label shape — so danger reads at a glance down the column. They route through `confirm(..., mutates_system=)` per §5.

### The inline input-adjacent button
A button that acts on the `Input` immediately to its left (e.g. "ip a" resolving a VPN interface).

- **Mechanism**: a **bare** `Button(label)` inside `Horizontal(classes="inline-row")`. It must **not** also carry `.thin-button` — `.inline-row Button` outranks it, so the class would be a lie about how the button renders.
- **Geometry**: `height: 3` on purpose, matching the `Input` beside it; `min-width: 10`.
- **Colour**: `$secondary` background with explicitly matched `tall` borders, distinguishing it from an action-row page action that merely happens to be tall.

### Not an archetype
`.close-button` (the `×` glyph docked top-right of `InfoModal` / `BuildHistoryModal`) is a modal close affordance, not an action button: `width: 4; height: 1; border: none; dock: right`. It is exempt because it carries a glyph rather than a label, and it must not be used for anything else.

### Enforcement
- Every `Button(` in `cockpit/screens/` is action-row (`classes="thin-button"`), `.close-button` (not an archetype, see above), or inline — and an inline site is marked with a trailing `# inline archetype (DESIGN.md §9)` comment, because the `.inline-row` class sits on the enclosing `Horizontal` and is not visible on the `Button(` line itself. So this command must print **nothing**:
  ```bash
  grep -rn 'Button(' cockpit/screens/ | grep -vE 'thin-button|close-button|inline-row'
  ```
  The previous version of this check required every hit to carry `thin-button` or sit in `.inline-row`, and therefore failed on compliant code: `builds.py`'s `classes="close-button"` modal dismiss is exempted by this section's own "Not an archetype" paragraph, and the two inline sites can't be recognised from the `Button(` line without the marker comment.
- No screen `DEFAULT_CSS` may set `height`, `min-width`, `border` or `padding` on a `Button`.

