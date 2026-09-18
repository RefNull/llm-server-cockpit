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
- **Known exception, in `SHARED_CSS` only**: `Tab { padding: 0 2 }`. That is a widget's internal label metric (the same category as `.form-label { width: 20 }`), not separation between elements, and calling it `$space-section` — "boundary separation between distinct screen regions" — would be a naming fiction of exactly the kind the distinct-values rule exists to prevent. It stays literal and stays out of the check's scope.
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
- **Rationale (the bug this replaces)**: the rule used to be `TabbedContent { margin: 0 2 }` — a *type* selector, so it applied a second time to the `TabbedContent` nested inside the LLM and Settings tabs, and `.panel` then added a third cell via `padding: 0 1`. Net left inset was 2 (Containers, Scripts), 3 (Dashboard), 4 (LLM > Models, LLM > HF Downloads) or 5 (LLM > Backends, Settings) depending on how deeply a screen happened to be nested and whether it used a panel. Scoping the margin to the outer container's id and dropping `.panel`'s horizontal padding (`.panel` paints no border and no background, so that padding was invisible indentation) makes every sub-view land at 3.
- **`.panel` contract**: `.panel` carries `height: auto` and `margin-bottom` only. It must never carry horizontal padding.

### Tab-Bar Gap

Content must never begin on the row immediately beneath a tab bar:

```css
TabbedContent > ContentSwitcher { margin-top: $space-normal; }
```

Textual's `Tabs` is `height: 2`, docked top, with no bottom margin, and `TabPane` has no padding — nothing in the framework provides this gap. The child combinator on the type selector is deliberate and matches **all** tab levels (outer, LLM, Settings): `TabbedContent.compose` yields exactly one `ContentSwitcher`, so there is no second element to catch. Verified live on all three levels.

---

## 3. Page Structure Rules

0. **A hidden tab does no I/O**:
   - Textual mounts every `TabPane`'s content up front, so every screen's `on_mount` fires at launch even though the operator can only see one tab. Left unmanaged that meant seven screens' initial loads to render the Dashboard — the same facts fetched two and three times over (`ip -4 addr` ×3, `docker ps` ×2, four GitHub calls).
   - **`on_mount` is for structure only** — table columns, form population, anything free. **Every data read belongs in `on_first_view()`**, which `CockpitScreenBase.ensure_first_view` runs the first time the tab is actually visible (`CockpitApp._load_visible_screens`, driven by `TabbedContent.TabActivated` and keyed on `is_on_screen`). It defaults to `on_refresh_requested()`, which is what most tabs want; override it only when first view legitimately does more than a refresh (`BuildsScreen`'s upstream version check, which `r` deliberately never repeats).
   - Beware self-inflicted reads: assigning to an `Input`'s `value` while populating a form fires `Input.Changed`, and a handler on it can shell out. Wrap form population in `self.prevent(Input.Changed)`.
   - **Enforcement**: `smoke/verify_screens.py` asserts launch spawns no command twice and makes at most the Dashboard's own 2 GitHub calls, then that opening Backends adds its own — so the check can tell deferral from deletion.

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
   - **Precedent**: The responsive multi-column pattern is standardized via `.columns-responsive` — on `#dashboard-columns`, and on the paired `.panel`s inside the Settings `Host Profile` and `System Services` sub-tabs (`.settings-columns`).

---

## 4. DataTable Contract

Textual's `DataTable` is a scroll container (`overflow-x: auto`) that scrolls horizontally when contents exceed view width. To prevent clipped columns and preserve discoverability:

1. **Explicit Column Widths**:
   - Every `add_column()` invocation must specify an explicit integer `width=`. Dynamic automatic sizing without bounds causes layout instability.
   - **Enforcement**: [`CockpitDataTable`](file:///Users/nystrom/GitHub/llm-server-cockpit/cockpit/widgets.py#L336-L354) overrides `add_column` to raise `ValueError` if `width` is omitted or None.
2. **No Pinned Columns — fit the viewport instead**:
   - `fixed_columns` is **forbidden** in `cockpit/screens/`. A pinned cell is painted with `datatable--fixed`, and `_render_line_in_row` uses that style *instead of* the row style rather than compositing with it — so a pinned column cuts a flat, unstriped band down the left of every zebra table, which reads as "this column is selected". No CSS fixes it; the style replacement is structural.
   - The rule it replaces (`fixed_columns >= 1` for any table wider than 80 cells) also never delivered: the columns that actually scroll out of reach are the per-row action columns on the *right*, and pinning the identifier on the left does nothing to reach them.
   - **Instead**: every table's render width (§4.4) must fit the 115-cell usable viewport at 121×30, so no horizontal scroll happens at or above the breakpoint. Below it, the table scrolls and no column is pinned.
3. **No Resting Highlight**:
   - A table that has not been interacted with must paint **no** selection band, and its zebra stripes must alternate correctly from the first row.
   - **Cursor**: `show_cursor` defaults `True` and `cursor_coordinate` defaults `(0, 0)`, so a freshly mounted table paints a block cursor on row 0. `datatable--cursor` / `datatable--fixed-cursor` are therefore transparent at rest and repaint only under `DataTable:focus`.
   - **Zebra phase**: Textual tints the *even* rows (0, 2, 4…). Row 0 is even, and the resting cursor repaints it `$surface` — so rows 0 and 1 came out identical and the alternation started a row late ("the first two lines are always highlighted"). `SHARED_CSS` swaps the phase: `datatable--even-row` is `$surface` and `datatable--odd-row` carries the tint, which makes the row-0 cursor repaint a no-op.
   - **Forbidden**: `show_cursor=False`. `DataTable._on_click` gates *every* selection message on `show_cursor`, so disabling it silently destroys all click handling and keyboard navigation while appearing to fix the cosmetics. `grep -rn "show_cursor" cockpit/` must never find a `False`.

4. **Audit & Enforced Contracts**:
   - Every table in `cockpit/screens/` derives from `CockpitDataTable` — directly, or via `SingleClickDataTable`, which was rebased onto it so the tick-able tables are covered by the same enforcement rather than bypassing it.
   - A **tick-able** table's column 0 is the `[ ]`/`[x]` `selection_marker`. Only `import-table` is still tick-able; `backends-table`, `model-table` and `scripts-table` dropped theirs when their bulk "act on selected" buttons became per-row action columns, which is also what put their `ID` flush against the left edge.
   - **Render width** = content width + 2 cells of padding per column (`Column.get_render_width`). Budget: **≤ 115**, the usable viewport at 121×30 (121 − 2 × `$space-edge`). Modal-hosted tables are budgeted against their dialog width, not this.
   - The 8 tables (Dashboard renders no table; `builds-table` no longer exists — retained builds moved into `RetainedBuildsModal`):
     - `backends-table` (`builds.py`): 5 columns ("Backend" w=14, "Active build" w=20, "Status" w=32, `Build` action w=9, `Edit` action w=8). Render 93. `Backend` is the bare backend name (`EX. ` prefix for a stock recipe) — the `.section-title` above the table already says "Llama.cpp", and llama-swap has its own section rather than a row here, which is why its permanently-blank Build and Edit cells are gone. `Active build` shows identity and version together (`build3 · 481c65f09`, or `not built`), green when it matches the pin and yellow when it does not — identity and version are separate fields as of `d925e91`, because `list_builds` used to return the directory name *as* the version. `Status` reads `update available (<sha>)`; the word "latest" was dropped because it truncated the sha it was describing. **`Build` is always literally "Build"** — the confirm dialog says "Rebuild" when `_is_rebuild` is true. That is one shared definition of "already built" (the current build's version equals the pin), used by both the confirm wording and `Active build`'s colour: two different definitions in adjacent cells is what made a row read `Not built` beside `[ Rebuild ]` (QA 2026-09-18). The pin is not a column and must not become one — see the note above.
     - `models-table` (`deploy.py`): 8 columns ("ID" w=24, "Engine" w=12, "GPU" w=10, "Backend" w=10, "Group" w=12, "TTL" w=8, `Edit` action w=8, `Remove` action w=10). Render 110.
     - `model-table` (`downloads.py`): 6 columns ("ID" w=20, "Repo ID" w=26, "Status" w=20, "Size" w=10, "Release Date" w=12, `Download` action w=12). Render 112.
     - `containers-table` (`containers.py`): 6 columns ("Name" w=22, "Image" w=24, "Status" w=20, "Ports" w=18, "Compose" w=8, `Restart` action w=11). Render 115.
     - `scripts-table` (`scripts.py`): 7 columns ("ID" w=16, "Path" w=27, "Status" w=20, `run` toggle w=9, `boot` toggle w=11, `Edit` action w=8, `Remove` action w=10). Render 115.
     - `files-table` (`downloads.py`): 5 columns ("File" w=34, "Size" w=10, "Added" w=10, "Used by" w=22, `Delete` action w=10). Render 96. Built from the directory, not from `models.yaml` — this tab owns files, the Models tab owns deployments.
     - `systemd-table` (`systemd.py`): 8 columns ("Unit" w=30, "Active" w=9, "Sub" w=9, `Logs` w=9, `Unit` w=8, `run` toggle w=9, `Restart` w=11, `hide` toggle w=10). Render 111. The only host-wide table in the app — it lists every loaded unit, not just the toolkit's, which is why it carries a hide list.
     - `gpu-table` (`settings.py`): 4 columns ("GPU ID" w=16, "Vendor" w=16, "Backends" w=30, "Driver Status" w=40). Render 110. Composed in both the first-run wizard and the normal-mode GPU Topology sub-tab — only one branch is ever mounted, with identical columns.
     - `history-table` (`builds.py` `BuildHistoryModal`, 90%-width dialog): 4 columns ("Backend" w=12, "Timestamp (UTC)" w=22, "Outcome" w=16, "Detail" w=40). Render 98.
     - `retained-table` (`builds.py` `RetainedBuildsModal`, 90%-width dialog): 6 columns ("Backend" w=12, "Ref" w=14, "Status" w=12, "Integrity" w=12, `Rollback` action w=12, `Remove` action w=10). Render 84.
     - `import-table` (`deploy.py` import modal, tick-able): 4 columns ("" w=3, "ID" w=24, "Engine" w=12, "cmd" w=60). Render 107.

---

## 5. Modal & Danger Tier Contract

All non-idempotent or mutating operations must be confirmed through `ConfirmModal`.

### Danger Tier Definition
An action is classified as **Dangerous** (`danger=True`, rendering the confirm button with `$error` styling) if it:
1. Mutates host-level system state outside declarative preview files (e.g. network wake flags, system drivers).
2. Restarts or alters background system services or systemd timers (e.g. `llama-swap`, scheduled reboots).
3. Executes irreversible state modifications (e.g. deleting downloaded weights, rolling back runtime binaries).

### Root-Requiring Actions

The CLI demands root up front (`cli.py`'s `require_root`: "every step writes under /opt, /etc,
/var/lib or installs apt packages"). The cockpit is normally launched unprivileged, and used to
apply that rule nowhere at all — a root-needing action just failed deep inside a worker with a
raw `install ... returned non-zero exit status 1`.

- **`confirm(..., requires_root=True)`** (and `TableAction(requires_root=True)`) marks an action
  that writes under `/etc`, `/opt` or `/usr/local`, or drives `systemctl`/`apt`. The prompt then
  says root is needed, and confirming authenticates before the action runs: `sudo -n -v` first,
  so a live credential cache costs no password and no suspend, and only a cache miss drops out of
  the TUI for a real prompt. Failing or declining that aborts the action.
- **Handlers behind the flag must use `self.privileged_runner`, never `self.runner`.** The shared
  `Runner` is app-wide; elevating it in place would silently put unrelated later actions through
  sudo — an HF download under sudo leaves root-owned files in the operator's `models_dir`.
- **`requires_root` is not `mutates_system`.** They overlap but are not the same set: downloading
  a model mutates host state and must *not* be elevated; restarting a container goes through the
  docker group, not root. Decide it from what the step actually writes.
- **Elevation is threaded through `Runner`, not applied per call site** — the same argument
  `dry_run` makes. Wrapping only `run()` is worse than not wrapping at all: subprocesses go as
  root while `write_file`/`mkdir`/`atomic_symlink` still fail with EACCES, so a step half-succeeds.
  `smoke/verify_runner_sudo.py` covers every branch.

### Expressing the Tier
Two idioms, split on which fact the call site actually knows:
- An action meeting one of the three clauses above calls `self.confirm(..., mutates_system=True)` (`CockpitScreenBase.confirm`), which derives `danger=True`. The call site declares *what the action does*, not how the button should look.
- A write confined to this repo's declarative files (`models.yaml`, `scripts.yaml`, `hosts/<hostname>.yaml`, `manifest.yaml`) is not tier-1 by the clause above, but is still confirmed with `ConfirmModal(..., danger=True)` directly — established precedent across this project: a config write is the thing a later `Deploy` turns into a system change, and operators treat it as consequential.
  - **`manifest.yaml` differs from the other three in being tracked by git.** That is why its confirm shows the old ref → the new one rather than just naming the file: the pin is auditable in history, and the confirm is the operator's last look before it moves. It is written by a targeted line rewrite, never `yaml.safe_dump`, which would destroy the header comment block and the `# bNNNNN` build-number comment on the `ref:` line.

### Call-Site Audit & Enforcement
All 20 modal confirmation call sites across the 6 mutating screens (Dashboard is read-only and opens no modal):
- **Builds** (`builds.py`):
  - `handle_table_action` → `_run_build`: `mutates_system=True`, `requires_root=True` via the `build` `TableAction` (tier 3 — compiles and swaps the backend runtime binary `current` points at). The prompt names the resolved verb through `{action}`, so a row whose prefix already exists reads "Rebuild".
  - `RetainedBuildsModal._run_rollback`: `mutates_system=True` (tier 3 — repoints `current` at a different runtime binary).
  - `_confirm_and_update_to_latest`: `ConfirmModal(danger=True)` (declarative `manifest.yaml` write, showing old → new).
  - `_confirm_and_change_version`: `ConfirmModal(danger=True)` (same write, from the `ChangeVersionModal` picker).
  - **Selecting a version and building it are separate acts**, and the two confirms above are the version half — neither compiles anything. Rollback is a third thing again: it repoints `current` without touching the pin. Do not merge them.
- **Deploy** (`deploy.py`):
  - `_confirm_and_save`: `ConfirmModal(danger=True)` (declarative `models.yaml` write).
  - `_delete_selected`: `ConfirmModal(danger=True)` (declarative `models.yaml` write).
  - `_confirm_and_import_selected`: `ConfirmModal(danger=True)` (declarative `models.yaml` write).
  - `_confirm_and_apply`: `mutates_system=True`, `requires_root=True` (tier 2 — installs systemd units and restarts `llama-swap`).
- **Downloads** (`downloads.py`):
  - `download` `TableAction` (`confirm="Download {row}?"`): `mutates_system=True` via `CockpitScreenBase._on_table_action_invoked` — per-model download.
  - `_on_download_all_missing`: `confirm(..., mutates_system=True)` (bulk download consuming significant network/storage).
- **Containers** (`containers.py`):
  - `_handle_restart_press`: `mutates_system=True` (tier 2 — restarts a running background service on the host).
  - `_handle_restart_all_press`: `mutates_system=True` (tier 2 — restarts every container service at once).
  - Exec shell, view logs, view compose: no modal — read-only, or a terminal handover the operator initiated explicitly.
- **Scripts** (`scripts.py`):
  - `run` / `boot` `TableAction`s (start↔stop, enable↔disable — two toggle columns, four verbs): confirmed with `mutates_system=True` by `CockpitScreenBase._on_table_action_invoked` (tier 2 — all four act on generated systemd units). Each prompt names the verb via the `{action}` substitution, so the toggle's current meaning reaches the modal.
  - `EditScriptModal.on_button_pressed`: `ConfirmModal(danger=True)` (declarative `scripts.yaml` write).
  - `remove` `TableAction` (`destructive=True`): confirmed by the same gate (declarative `scripts.yaml` write; the systemd unit is deliberately left in place).
- **Settings** (`settings.py`):
  - `_real_deploy` (first-setup): `ConfirmModal(danger=True)` (writes a new `hosts/<hostname>.yaml`).
  - `_confirm_and_save_profile`: `ConfirmModal(danger=True)` (mutates an existing `hosts/<hostname>.yaml`).
  - `_confirm_and_deploy_profile`: `mutates_system=True`, `requires_root=True` (tier 2 — saves profile, restarts `llama-swap` and syncs timers).
  - `_confirm_and_save_wol`: `ConfirmModal()` with no `danger` (declarative `hosts/<hostname>.yaml` write; no NIC, systemd or binary is touched, so it works unprivileged).
  - `_confirm_and_check_wol`: `mutates_system=True`, `requires_root=True` (tier 1+2 — NIC wake flags, TLP/NetworkManager config, a systemd persistence unit).
  - `_confirm_and_check_tailscale`: `mutates_system=True`, `requires_root=True` (tier 1 — installs a system package if missing and joins the host to a tailnet).
  - `_confirm_and_check_drivers`: `ConfirmModal()` with no `danger` (read-only inspection; exits loudly on drift, never auto-corrects).
  - `_confirm_and_apply_service`: `mutates_system=True`, `requires_root=True` (tier 2 — restarts `llama-swap` and (re)installs/removes systemd timers).

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
     - `dashboard.py`: `_refresh_all` catches worker exceptions and toasts on error only; normal readings populate the inventory and service rows inline.
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

### Archetype A: Static Inventory (`DashboardScreen`)
- **Role**: answer "what machine is this" and "what is deployed, and is it on". **Not a monitor.**
- **Zero live measurement, and this is load-bearing.** No CPU%, no RAM%, no GPU utilization, no gauges, no sampling control. The toolkit deploys LLM services; monitoring the host belongs to a monitoring stack (`AGENTS.md` → Scope). This was settled after the measurement cost two fan-ramp incidents — `5c986a4` removed a 1Hz sampler, `b799d83` removed the mount-time GPU read and put the last one behind a button — and a button still leaves a live telemetry path in a deployment tool. Deleting the capability deletes the thing that can regress. See `plans/04-dashboard-inventory.md`.
- **Never spawn a GPU vendor tool** (`nvidia-smi`, `xpu-smi`, `rocm-smi`, `amd-smi`, `vulkaninfo`, `clinfo`) from anywhere the operator can reach without asking. The only sanctioned caller in the repo is `drivers.run()` behind Settings → Check Drivers. Enforced by `smoke/verify_no_gpu_wake.py` at the subprocess boundary, so a new caller reaching one some other way is caught too. `lspci` is **not** on that list — it reads the PCI ID database to name a device and does not open the interfaces that wake one.
- **Layout**: `Horizontal(classes="columns-responsive")` — side by side at 121 columns and above, stacked below, with the rule-plus-`$space-section` gutter on `#dashboard-left` (§3.5).
- **Left column — identity, all of it static**: board, CPU, memory size, OS, kernel, cockpit version via `provision/steps/sysinfo.py` (file reads and stdlib only); then one line per *declared* accelerator from `hosts/<hostname>.yaml`, joined to whatever `drivers.read_lockfile_status()` recorded — a pure YAML read. **Declared, never probed.** A GPU with no lockfile entry reads "driver not checked"; it does not trigger a check.
- **Right column — services, one standardised row each** via `cockpit.widgets.service_row`: **name first, then the glyph, then muted detail.** The glyph used to lead (`✔ llama-swap: running`), which put it in a different column on every line and made the single ✖ in a list of ✔ hard to find. Name-then-glyph gives one scannable column, which is the only thing the mark is for.
- **The glyph is three-valued.** `True` → `✔ $success` (running/enabled), `False` → `✖ $error` (not), `None` → `● $text-muted` (**not managed, or not knowable**). An error renders as `None` with the error as detail — never `✖`, because `✖` asserts "this is off" and an unreachable daemon is not that claim. Build every row through `service_row`; never hand-assemble a glyph.
- **Per-service rows, not roll-ups.** One row per container, per script, per backend. A `4/4 healthy` summary hides which one is down, which is the only thing the row exists to show.
- **Interaction Contract**: read-only display, **zero controls**. Passive background workers fill the panels; toasts on error only. Zero modal triggers. A control on this screen is the smell that measurement is creeping back.

### Archetype B: Table-Driven Inventory (`Builds`, `Deploy`, `Downloads`, `Containers`, `Scripts`)
- **Role**: Collection browsing, status inspection, and lifecycle operations over system items.
- **Layout**: Strict `CockpitDataTable` containment (maximum 3 tables per screen), no `fixed_columns` (§4.2), every table budgeted to fit the 115-cell usable viewport. Dedicated filter/search input row placed immediately above table if present.
- **Per-row before bulk**: an operation that acts on *one* row is an in-table action column (§9), never a tick-box column plus a "… selected" button. An action row holds only operations that have no single-row meaning (`New Script`, `Download all missing`). This is what keeps column 0 the identifier, flush left.
- **Action Rows**: Standardized button rows positioned directly beneath the table:
  - `.action-row-primary`: Primary lifecycle actions (`height: auto; align: left middle; margin-top: $space-normal; margin-bottom: 0;`).
  - `.action-row-secondary`: Subordinate/management actions (`height: auto; align: left middle; margin-top: $space-normal; margin-bottom: 0;`).
  - Every `Button` inside either row gets `margin-right: $space-normal` from one `SHARED_CSS` rule. Per-screen overrides of that gap are forbidden (`SettingsScreen .action-row-primary Button { margin-right: 1 }` used to be the only place action-row buttons didn't abut; it has been deleted).
- **Interaction Contract**: Mutating operations require `ConfirmModal` with explicit danger tier styling. Operator-initiated actions emit completion toasts on both success and error.

### Archetype C: Form / Inspector (`SettingsScreen`)
- **Role**: Declarative configuration, host profile management, and deep system inspection.
- **Layout**: Structured `.panel` containers organized via sub-tabs (`TabbedContent`) or vertical sections.
- **Form Rows**: Standardized key-value and field rows:
  - `.form-row`: Container row (`height: auto; margin-bottom: 1;`).
  - `.form-label`: Fixed label header (`width: 20; margin-top: 1; text-style: bold; color: $text-muted;`).
  - `.form-field`: Flexible input, select, or display widget (`width: 1fr; max-width: 40;`); a `Static` one also takes `margin-top: 1`.
- **Row alignment is two margins, not `align-vertical`.** `.form-row` used to declare `align-vertical: middle`, which silently did nothing — Textual does not offset a short child inside a `height: auto` `Horizontal`, so a 1-line label sat on row 0 of a 3-line `Input`, i.e. on its top border. An `Input`/`Select`/`Switch` is 3 cells tall with its text on row 1, so `.form-label` drops one row to meet it, and a plain-text `Static` value takes the same offset. Before this, a multi-line status rendered a full line *below* its own label (most visible on WOL Status). Do not reintroduce `align-vertical` here expecting it to do this job.
- **Paired panels**: 20 + 40 = 60 cells per form row, so two columns fit side by side inside the 115-cell usable viewport. A two-column sub-tab wraps each side in `Vertical(classes="settings-column")` inside a `Horizontal(classes="columns-responsive settings-columns")` — side by side above the breakpoint, stacked below it — with the same rule-plus-`$space-section` gutter the Dashboard uses. A column wraps *panels*, not fields, so one side can stack several (System Services puts Wake-on-LAN above Tailscale). The cap on `.form-field` exists for this: full-width inputs made every Settings sub-tab one tall column that had to be scrolled to reach its own save button.
- **One concern per panel, and its buttons inside it.** A panel's action row belongs to that panel (§3.3), not to the sub-tab. Wake-on-LAN and Tailscale used to share a "Host & Network Services" panel and a screen-level action row with the llama-swap settings, which is how saving a WOL interface came to run `swap.run()` — a root-only reinstall — and fail. If two groups of fields do not save together, they are two panels.
- **Offer what the host already knows.** A field whose valid values are enumerable from the host is a `Select` over those values, not an `Input` the operator has to type exactly (`f-wol-interface` over `wol.list_interfaces()`); a field derivable from another is prefilled from it (`f-wol-mac` from the chosen NIC) and stays editable, because the profile is the source of truth and can legitimately disagree with the current hardware. Prefilling from a `Select` must be wrapped in `self.prevent(Select.Changed)` when repopulating the form, or reloading clobbers the operator's override.
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
- **Rendering contract**: `[ Label ]`, one space inside each bracket. Column width is `len(label) + 4`.
- **State toggles**: `TableAction(id, callable, width=N)` — a callable label resolves per row, so one column reads `[ Start ]` on a stopped row and `[ Stop ]` on a running one. Two static columns would reserve both widths on every row forever; `scripts-table` only fits its six operations inside the 115-cell budget because start/stop and enable/disable are one toggle column each. A callable label must pass an explicit `width=` (there is no single label to measure), and `confirm="…{action}…"` substitutes the resolved label so the prompt names the verb the operator clicked.
- **Returns `Text`, never `str`.** The app console has markup enabled, so a raw `"[ Update ]"` is parsed as a Rich tag and renders as the empty string.
- **For a markup `Static` — not a table cell — use `cockpit.widgets.escape_markup`, and NOT `rich.markup.escape`.** Textual 8.2.8 parses its own content markup and accepts tags Rich does not: Rich's tag regex requires `[a-z#/@]` after the bracket, Textual's does not. So a vendor string like `Intel Corporation DG2 [Arc A770]` passes through **both** `rich.markup.escape` and `textual.markup.escape` untouched — each is built around the Rich-era rule — and is then eaten by Textual's parser, truncating the value at the bracket with no error and no exception. Only a literal `\[` survives. Measured in a live app: the GPU name rendered as `gpu-intel · Intel Corporation DG2`. Anything an operator or a vendor supplies goes through `escape_markup`: a container name, a script id, a device name. `smoke/verify_screens.py` holds the line.
- **Destructive actions** (Remove, Delete) take the theme's `$error` colour and bold, not a different label shape — so danger reads at a glance down the column. They route through `confirm(..., mutates_system=)` per §5.

### The inline input-adjacent button
A button that acts on the `Input` immediately to its left (e.g. "ip a" resolving a VPN interface).

- **Mechanism**: a **bare** `Button(label)` inside `Horizontal(classes="inline-row")`. It must **not** also carry `.thin-button` — `.inline-row Button` outranks it, so the class would be a lie about how the button renders.
- **Geometry**: `height: 3` on purpose, matching the `Input` beside it; `min-width: 10`.
- **Colour**: `$secondary` background with explicitly matched `tall` borders, distinguishing it from an action-row page action that merely happens to be tall.

### Action-row overflow at narrow widths
An `.action-row-primary`/`.action-row-secondary` with enough buttons can extend past
`screen.region.right` at the 80×24 floor while fitting comfortably at normal widths — the
single-row layout is the QA 3c/5b fix and stays right at typical widths, so the fix is not to
revert to always-wrapping.
- **Mechanism**: `Screen.-narrow .action-row-primary, Screen.-narrow .action-row-secondary { layout: vertical; height: auto; }` in `SHARED_CSS` (`cockpit/widgets.py`) — the same `CockpitApp.HORIZONTAL_BREAKPOINTS` hook `.columns-responsive` already uses (§2). Under `-narrow` the row stacks one button per line, each still reachable; under `-wide` it is untouched and stays the single row.
- **Scope**: applies to every `.action-row-*` in the app, not one screen at a time — any future screen with enough buttons in one row can hit this same overflow at 80 columns.
- **Enforcement**: `smoke/verify_screens.py` asserts, at both 80×24 and 121×30 for every screen, that no mounted and enabled `Button` has `region.right > screen.region.right`.

### Not an archetype
`.close-button` (the `×` glyph docked top-right of `InfoModal` / `BuildHistoryModal`) is a modal close affordance, not an action button: `width: 4; height: 1; border: none; dock: right`. It is exempt because it carries a glyph rather than a label, and it must not be used for anything else.

### Enforcement
- Every `Button(` in `cockpit/screens/` is action-row (`classes="thin-button"`), `.close-button` (not an archetype, see above), or inline — and an inline site is marked with a trailing `# inline archetype (DESIGN.md §9): bare Button inside .inline-row` comment, because the `.inline-row` class sits on the enclosing `Horizontal` and is not visible on the `Button(` line itself. **The trailing `.inline-row` in that comment is load-bearing, not decoration**: the check below filters on `inline-row`, so the shorter `# inline archetype (DESIGN.md §9)` does not satisfy it. This paragraph used to give exactly that shorter form, and a compliant button at `downloads.py:178` failed the check for years because of it — the marker and the grep must be read as one contract. So this command must print **nothing**:
  ```bash
  grep -rn 'Button(' cockpit/screens/ | grep -vE 'thin-button|close-button|inline-row'
  ```
  The previous version of this check required every hit to carry `thin-button` or sit in `.inline-row`, and therefore failed on compliant code: `builds.py`'s `classes="close-button"` modal dismiss is exempted by this section's own "Not an archetype" paragraph, and the two inline sites can't be recognised from the `Button(` line without the marker comment.
- No screen `DEFAULT_CSS` may set `height`, `min-width`, `border` or `padding` on a `Button`.

