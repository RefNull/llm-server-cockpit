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
Spacing tokens are defined in cell units and declared as TCSS variables in `SHARED_CSS`:
- `$space-tight: 1`: Compact separation between related controls (e.g. button right margins, field label bottom margin).
- `$space-normal: 1`: Standard element padding and margin between consecutive input rows or panel boundaries.
- `$space-section: 2`: Boundary separation between distinct screen regions, button-row spacing, and dialog container padding.

---

## 2. Terminal-Size Contract & Breakpoints

### Supported Floor: 80×24
- **Decision**: The minimum supported terminal dimension for `bin/cockpit` is strictly **80×24** cells.
- **Rationale**: 80×24 is a deliberate project constraint matching standard VT100 console geometry for headless server recovery and base SSH administration without window management. Textual defines no framework floor (it only falls back to 80×24 when terminal detection fails).

### Breakpoints
Responsive layout reflow is driven by `App.HORIZONTAL_BREAKPOINTS`:
```python
HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (120, "-wide")]
```
- **Threshold Rationale (120 cells)**: Grounded in the ASCII art banner width (115 characters + 2-cell margins on each side = 119 cells). At or above 120 cells (`Screen.-wide`), the full ASCII banner (`#banner-art`) is displayed; below 120 cells (`Screen.-narrow`), the header collapses to a single-line compact brand label (`#banner-compact`) to prevent wrapping or horizontal clipping on standard 80-column consoles. The 120-cell threshold also guarantees clearance for `#dashboard-columns` side-by-side metric panels without wrapping gauge readouts or clipping panel titles.
- **Scrollbar Containment Contract**: To prevent outer canvas overflow and double vertical scrollbars:
  ```css
  Screen { overflow: hidden; }
  TabbedContent { height: 1fr; margin: 0 2; }
  ```
  `Screen { overflow: hidden; }` prevents the root `Screen` container from painting an outer vertical scrollbar. `TabbedContent { height: 1fr; }` ensures the tabbed interface cleanly fills the vertical viewport between `CockpitHeader` and `Footer`. All scrolling is strictly delegated to inner view containers (e.g. `VerticalScroll` within individual screens).
- **Enforcement**: Layout transitions must use CSS classes (`Screen.-narrow`, `Screen.-wide`). Screens must never implement manual `on_resize` geometry calculations.

---

## 3. Page Structure Rules

1. **Max Tables per Screen**:
   - **Ceiling**: Maximum of **3** `DataTable` widgets per screen.
   - **Precedent**: Established by [`cockpit/screens/builds.py`](file:///Users/nystrom/GitHub/llm-server-cockpit/cockpit/screens/builds.py) which renders 3 tables (`backends-table`, `builds-table`, and `history-table`, with `backends-table` added during upstream feature additions). Exceeding 3 tables requires splitting workflows into distinct sub-views or tab panes rather than stacking.

2. **Filter & Search Placement**:
   - A filter/search `Input` must reside in its own dedicated row immediately preceding the table it filters, left-aligned.
   - It must **never** be placed inside an action `.button-row`.
   - **Precedent**: Established proactively to standardize upcoming model/artifact search inputs without polluting action trigger rows.

3. **Button-Row Placement**:
   - Action buttons (`.button-row`) must be positioned immediately beneath the specific table or form they operate on.
   - Buttons must never float into a generic screen-level footer.
   - **Precedent**: Matches [`cockpit/screens/downloads.py`](file:///Users/nystrom/GitHub/llm-server-cockpit/cockpit/screens/downloads.py) (download actions directly under `model-table`) and [`cockpit/screens/deploy.py`](file:///Users/nystrom/GitHub/llm-server-cockpit/cockpit/screens/deploy.py) (model mutations directly under `models-table`).

4. **Modal Trigger Placement**:
   - The button initiating a destructive or mutating action must trigger the `ConfirmModal` directly upon press.
   - Intermediate dropdown menus or ellipsis "..." action selectors are forbidden.
   - **Precedent**: Followed consistently across existing screens (e.g. "Build" button in [`cockpit/screens/builds.py:126`](file:///Users/nystrom/GitHub/llm-server-cockpit/cockpit/screens/builds.py#L126)).

5. **Two-Column Layout Threshold**:
   - Multi-column layouts (such as `#dashboard-columns`) are permitted only when the `Screen.-wide` class is active (terminal width ≥ 120).
   - Under `Screen.-narrow` (< 120 cells), multi-column containers must collapse to a single vertical stack (`layout: vertical`) via `.columns-responsive`.
   - **Precedent**: Settings was refactored upstream into `TabbedContent` (`#settings-tabs`), removing the former hardcoded `#settings-columns` layout. The responsive multi-column pattern is standardized via `.columns-responsive` on `#dashboard-columns`.

---

## 4. DataTable Contract

Textual's `DataTable` is a scroll container (`overflow-x: auto`) that scrolls horizontally when contents exceed view width. To prevent clipped columns and preserve discoverability:

1. **Explicit Column Widths**:
   - Every `add_column()` invocation must specify an explicit integer `width=`. Dynamic automatic sizing without bounds causes layout instability.
   - **Enforcement**: [`CockpitDataTable`](file:///Users/nystrom/GitHub/llm-server-cockpit/cockpit/widgets.py#L336-L354) overrides `add_column` to raise `ValueError` if `width` is omitted or None.
2. **Pinned Identifier Columns**:
   - Any table whose cumulative width can exceed the usable width of an 80-column terminal must set `fixed_columns >= 1` so the primary key remains pinned during horizontal scrolling.
3. **Audit & Enforced Contracts**:
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

Every screen in `llm-server-cockpit` conforms to one of three structural archetypes:

### Archetype A: Operational Dashboard (`DashboardScreen`)
- **Role**: Real-time telemetry, service health, and resource utilization monitoring.
- **Layout**: Single vertical scroll wrapping status panels and responsive multi-column containers (`.columns-responsive`). Side-by-side above 120 columns; stacked vertically below 120 columns.
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
  - `.action-row-primary`: Primary lifecycle actions (`height: auto; align: left middle; margin-top: 1; margin-bottom: 0;`).
  - `.action-row-secondary`: Subordinate/management actions (`height: auto; align: left middle; margin-top: 1; margin-bottom: 0;`).
- **Interaction Contract**: Mutating operations require `ConfirmModal` with explicit danger tier styling. Operator-initiated actions emit completion toasts on both success and error.

### Archetype C: Form / Inspector (`SettingsScreen`)
- **Role**: Declarative configuration, host profile management, and deep system inspection.
- **Layout**: Structured `.panel` containers organized via sub-tabs (`TabbedContent`) or vertical sections.
- **Form Rows**: Standardized key-value and field rows:
  - `.form-row`: Container row (`height: auto; align-vertical: middle; margin-bottom: 1;`).
  - `.form-label`: Fixed label header (`width: 24; text-style: bold; color: $text-muted;`).
  - `.form-field`: Flexible input, select, or display widget (`width: 1fr;`).
- **Interaction Contract**: Synchronous validation failures update inline `.status-text` or `.error-text` only (never toast). File saves and hardware probes prompt via `ConfirmModal`.

