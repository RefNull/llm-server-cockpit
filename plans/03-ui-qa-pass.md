# Plan: UI QA Remediation Pass

Baseline: HEAD `114a6c3` (after Antigravity's `edf23e4` design-standards + `114a6c3` archetypes).
Source: operator QA review of the running build, 2026-09-14, plus two annotated screenshots
(LLM > Backends, Settings > GPU Topology).

Goal: resolve a 10-item QA backlog whose through-line is inconsistency — two button geometries
fighting each other, three different side insets, zero gap under the tab bar, and per-row
actions that don't exist. Fix the shared chrome first, build the missing primitive second,
then apply it screen by screen.

Each phase is self-contained and can run as its own `Agent(model: "opus")` call in a fresh
context. Give the phase plus Phase 0 — that's the grounding it needs.

---

## Phase 0: Documentation Discovery (DONE — do not redo, cite it)

Three parallel discovery agents, 2026-09-14. Everything below was read from installed source
or executed live; nothing is inferred.

### 0a. Textual 8.2.8 hard constraints — read this before designing any table interaction

Installed at `.venv/lib/python3.14/site-packages/textual/`.

1. **Widgets CANNOT go in `DataTable` cells. Verified empirically.** Cells are typed
   `list[RenderableType]` (`widgets/_data_table.py:265`) and rendered via
   `self.app.console.render_lines(...)` (`:2176-2181`). A Textual `Widget` has no
   `__rich_console__`; `Console().render_lines(Button("Hi"), ...)` raises
   `rich.errors.NotRenderableError`. `DataTable` subclasses `ScrollView`, which "doesn't rely
   on the compositor to render children" (`scroll_view.py:15-18`) — anything mounted into it
   is CSS-positioned over the rendered strips with zero relationship to cell geometry.
   **No Button, no Checkbox, no Switch in a cell. Ever.**
2. **`[@click=...]` markup in cell text is also dead.** `DataTable._on_click` (`:2669`) never
   calls `super()._on_click`, so `Widget.broker_event` (`widget.py:4695-4703`) never runs.
3. **The working mechanism**: every cell's style carries meta `{"row": row_index,
   "column": column_index}` (`:2136`), and `_on_click` reads `event.style.meta` (`:2671`).
   Subclass `DataTable`, override `_on_click`, dispatch on `meta["column"]`. Subclass handlers
   run first down the MRO (`message_pump.py:758,794-799`); `event.prevent_default()` suppresses
   the base handler. **This repo already does exactly this** — `cockpit/widgets.py:255-276`
   (`SingleClickDataTable`). That is the copy-ready pattern.
4. `CellSelected` (`:472-504`, carries `coordinate`, `cell_key`) and `RowSelected` (`:536-563`,
   carries `cursor_row`, `row_key`) both require **two clicks** — `_on_click` only posts a
   selection when `new_coordinate == self.cursor_coordinate` (`:2696-2700`). Single-click
   targeting must go through the `_on_click` override, not these events. `HeaderSelected`
   (`:623`) and `RowLabelSelected` (`:653`) do fire on a single click.
5. **The double highlight band in both screenshots is two separate things.**
   - Row-0 band = the cursor: `show_cursor` defaults `True` (`:416`), `cursor_coordinate`
     defaults `Coordinate(0,0)` (`:422`), and `add_row` force-posts a highlight when the first
     row lands (`:1728-1732`). Blurred styling is `$block-cursor-blurred-background` (`:384-388`).
   - First-column band = **`fixed_columns`**: `_render_cell` marks `column_index <
     self.fixed_columns` as `is_fixed_style_cell` (`:2112-2116`), styled `datatable--fixed` →
     `background: $secondary-muted` (`:371-374`). That is the amber column. Set at
     `containers.py:82`, `downloads.py:139`, `scripts.py:256`, `deploy.py:391`.
   - Full component-class list: `datatable--cursor`, `--hover`, `--fixed`, `--fixed-cursor`,
     `--header`, `--header-cursor`, `--header-hover`, `--odd-row`, `--even-row` (`:302-312`).
   - **Do NOT fix this with `show_cursor=False`** — `:2691` gates all selection messages on it,
     so you'd lose every click event and all keyboard nav. Fix with CSS (Phase 1).
   - `cursor_coordinate` cannot be set null; `validate_cursor_coordinate` clamps (`:1314-1322`).
6. **Cell updates**: `update_cell(row_key, column_key, value, *, update_width=False)`
   (`:871-910`) and `update_cell_at(coordinate, value, *, update_width=False)` (`:915`).
   **Markup gotcha**: the app console has `markup=True` (`app.py:623-634`), so a raw `str`
   cell `"[x] foo"` renders as `" foo"` — Rich eats the tag. **Always use `rich.text.Text`
   for cell content.** The repo's `selection_marker` (`widgets.py:220`) already does.
7. **Meters**: `ProgressBar(total=None, *, show_bar=True, show_percentage=True, show_eta=True,
   ...)` (`widgets/_progress_bar.py:238-285`). Layout is horizontal (`:200-207`) and
   `PercentageStatus` is `width: 5; content-align-horizontal: right` sitting **immediately
   right of the bar** (`:152-157`) — this is precisely the bar/value adjacency the QA asks
   for. `Bar` defaults to `width: 32`; set `Bar { width: 1fr; }`. Copy-ready composition at
   `_progress_bar.py:293-302`. Also available: `Sparkline`, `Digits`.
8. `ListView`/`ListItem` (`_list_view.py:19`, `_list_item.py:11`) can host real Buttons, but
   `ListView` sets `can_focus_children=False` so they're mouse-only, and you lose `sort()`,
   fixed columns, cursor nav, zebra stripes and per-line render caching. **Rejected** for this
   plan (decision recorded below) — noted only so nobody re-proposes it.

### 0b. Button and spacing inventory (current code)

- Textual `Button` default (`_button.py:116-123,188-194`): `min-width: 16`, `height: auto` +
  `border-top/bottom: tall` → **renders 3 cells tall, 16 wide**. That is the "massive ugly
  button", at **27 call sites**: `containers.py:87,88,90`; `deploy.py:543-546,548,549`;
  `scripts.py:261-264,266,267`; `settings.py:206,215,216,262,263,275,276,318-320`;
  `widgets.py:366,367`.
- `.thin-button` (`widgets.py:94-100`): `height: 1; min-width: 10; border: none; padding: 0 1;
  margin-right: 1` → the compact look the operator wants, at **29 call sites** including
  `builds.py:220` (`"Update selected"` — the reference button).
- `variant=` only sets colour (`_button.py:147-186`), never geometry.
- **Textual ships `Button(compact=True)`** (`_button.py:305,342`) → `-textual-compact
  { border: none !important }` (`:197-199`), i.e. the native equivalent of `.thin-button`.
  **Currently unused anywhere in this repo.**
- `.inline-row Button` (`widgets.py:117-120`): `min-width: 10; height: 3` — deliberately tall
  to match an `Input`. This is the operator's "button type #3" and it already exists.
- `SettingsScreen .action-row-primary Button { margin-right: 1 }` (`settings.py:96-98`) is the
  **only** screen giving action-row buttons a gap; Containers/Scripts/Deploy buttons abut.

**Spacing cascade, terminal edge → table content:**

| Layer | file:line | Value |
|---|---|---|
| `Screen` | `app.py:117-119` | `overflow: hidden`, no padding |
| `CockpitHeader` | `app.py:46-50` | `margin: 0 2 1 2` |
| `TabbedContent` — **type selector, hits outer AND nested LLM tabs** | `app.py:120-123` | `height: 1fr; margin: 0 2` |
| `Tabs` | textual `_tabs.py:217-219` | `height: 2`, docked top, **no bottom margin** |
| `TabPane` | textual `_tabbed_content.py:172-173` | `height: auto`, no padding |
| screen root `VerticalScroll` | `builds.py:196`, `containers.py:77`, `dashboard.py:106`, `deploy.py:539`, `downloads.py:128`, `scripts.py:252` | no CSS → 0 |
| `.panel` | `widgets.py:70-79` | `padding: 0 $space-normal` (=1) |

**Net left inset: 2 cells** (Containers/Scripts/Deploy/Downloads — bare tables, no `.panel`),
**5 cells** (Builds — 2 outer + 2 nested `TabbedContent` + 1 `.panel`), **3** (Dashboard).
That double-application of the type selector is the root cause of the inset discrepancy.
**Vertical gap below the sub-tab bar: 0 cells** — nothing in repo or framework adds one.

- Tokens `$space-tight: 1; $space-normal: 1; $space-section: 2` (`widgets.py:45-47`) are used
  at **8 sites, all inside SHARED_CSS**. Zero screen `DEFAULT_CSS` uses a token; all hardcode
  `1`/`2`. `$space-tight` and `$space-normal` are both `1` — the scale has two real values.
- **`DESIGN.md` has no rule about button height, min-width, border, or when `.thin-button`
  applies — anywhere.** QA item 0 is a *doc gap*, not a compliance gap. DESIGN.md also
  contradicts itself: §3.3/§3.2 mandate `.button-row`, §8 introduced `.action-row-*`, and both
  are now in use. Likewise no rule fixes a side inset or tab-bar gap, so item (b) is a doc gap too.
- `plans/02-screen-archetypes.md:26` asked for secondary rows as "thin, low-contrast ghost
  buttons" — implemented in `builds.py` only; `containers.py:90` and `scripts.py:266-267` use
  bare defaults. That part *is* a compliance gap.

### 0c. Data availability

- **Per-GPU metrics already exist.** `metrics.read_gpus() -> list[dict]` (`metrics.py:81`)
  returns one dict per device: `vendor, index, name, utilization_pct, memory_used_mb,
  memory_total_mb, power_w` (+ `frequency_mhz` for Intel). The aggregation is a UI choice:
  `dashboard.py:263` takes `primary = gpus[0]`, `:273-274` sums VRAM. **No new collection code
  needed for nvidia/intel.**
  - nvidia: fully obtainable — `nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,
    memory.total,power.draw --format=csv,noheader,nounits` (`metrics.py:94-101`).
  - intel: `utilization_pct` is **expected `None`** — xpu-smi 2.1.0 "does not report real
    numbers" (`metrics.py:7-10`, `:247`); **no product name** — `:246` hardcodes
    `f"Intel GPU {device_id}"`. VRAM is real.
  - amd: **zero telemetry.** `read_gpus()` is literally `_read_nvidia_gpus() +
    _read_intel_gpus()` (`:87`); `metrics.py:10` says AMD is out of scope. AMD is declarable in
    a host profile, so treat it as unimplemented, not broken.
- **Sampling is a hard constraint.** `5c986a4` removed `set_interval(1.0, ...)` because
  "continuously polling nvidia-smi/xpu-smi every second … kept an Intel Arc GPU's sysman
  telemetry active and was observed to ramp its fans to 100% within moments of opening the
  app." Current safe pattern: **one-shot only**, inside `@work(thread=True) _refresh_all()`
  (`dashboard.py:157`), from `on_mount` (`:147`) and `on_refresh_requested` (`:151`).
  `metrics.py:1-5`'s docstring still claims "roughly once a second" — **stale, fix it.**
  The `time.sleep(0.1)` at `:189` is a `/proc` CPU delta, not a GPU poll — leave it.
- **Copy-ready**: the pre-`5c986a4` dashboard rendered per-slot GPU gauges keyed off
  `self._gpu_slots` with ids `res-gpu-{slot_idx}-util-bar` — recover verbatim via
  `git show 5c986a4^:cockpit/screens/dashboard.py`.
- **"Pinned" encodes nothing.** `builds.py:292` renders `[★]` iff
  `manifest["llama_cpp"]["ref"]` is truthy, and `schema.py` *requires* that key for the
  manifest to load — so the cell is a constant on every row. Repo-wide grep finds no pin
  boolean; nothing consumes one. Update-flagging is independent (`update_check.py:45,62`
  compares manifest ref vs GitHub API). A functional unpin toggle would have to make
  git-tracked `manifest.yaml` TUI-mutable or add an untracked override — contradicting
  `AGENTS.md:39` zero-drift and `manifest.yaml:1-3` "no 'install latest' path anywhere in this
  repo". **Decision (operator, 2026-09-14): show the actual pinned value instead.**
- **HF metadata, probed live and unauthenticated**: `GET
  https://huggingface.co/api/models/{repo_id}?blobs=true` → 200 with `lastModified`,
  `createdAt`, `gated`, `private`, `sha`, `downloads`, `gguf.total`, and
  `siblings: [{rfilename, blobId, size}]`. File existence and per-file byte size are obtainable
  **without downloading weights**. **Blocker**: a nonexistent repo returns **401, not 404**
  (anti-enumeration), and a gated repo returns 200 — so unauthenticated, "not found" is
  indistinguishable from "private". With a token, missing repos return 404.
- `hf.model_status()` (`hf.py:88-95`) returns exactly `"unmanaged" | "placeholder" |
  "downloaded" | "missing"` — **purely local filesystem, no network.** The model list is
  `models.yaml`, read at `downloads.py:170`. "Tracked but not downloaded" already exists: it is
  `"missing"`. Ad-hoc input already registers into `models.yaml` before downloading
  (`downloads.py:359-376`), validating via `schema.validate_models_dict`. `schema.py:128-131`
  requires `repo_id`, `quant_file` and a valid `bind.{gpu,backend}` — a tracked entry needs a
  synthesized bind, which `downloads.py:355-357` already does.
- **`huggingface_hub` is NOT importable from `.venv`** — it installs only into
  `state_dir/venv-hf` on the target host (`hf.py:41-50`). Cockpit-side HF calls must use plain
  `urllib.request`, following the precedent in `update_check.py`.

### 0d. Decisions taken by the operator (2026-09-14) — do not relitigate

| # | Decision |
|---|---|
| 1 | Per-row actions = **click-dispatch text columns**, not ListView, not real buttons. |
| 2 | "Pinned" column → **show the actual pinned ref/version**. No toggle. |
| 3 | HF validity column → **three-state**; token upgrades "needs auth to verify" → definitive "not found". |
| 4 | GPU gaps → **show the section, mark gaps explicitly** ("not reported by xpu-smi" / "no AMD telemetry"), never a 0% bar. |

### Anti-pattern guards — apply to EVERY phase

- Never mount a Widget into a `DataTable` cell (0a.1). Never use `[@click=...]` in cell text (0a.2).
- Never kill the highlight with `show_cursor=False` (0a.5) — CSS only.
- Never use a raw `str` for cell content containing brackets — `rich.text.Text` only (0a.6).
- Never add `set_interval`/timer polling of GPU metrics (0c) — one-shot only.
- Never `import huggingface_hub` from `cockpit/` (0c) — `urllib.request` only.
- Never make `manifest.yaml` writable from the TUI (0c).
- Do not invent Textual APIs. If something isn't in 0a, verify against installed source before using it.
- `provision/` is out of scope except the one stale docstring fix named in Phase 6.

---

## Phase 1: Global chrome — spacing, button archetypes, table highlight

Fixes: the opening complaint (side margins, tab-bar gap) + item 0 + items 2g/8a (highlight).
Everything downstream depends on this, so it goes first.

**What to implement**

1. **Uniform side inset.** `app.py:120-123`'s `TabbedContent { margin: 0 2 }` is a type
   selector and double-applies to the nested LLM tabs. Scope it to the outer container (give
   the outer `TabbedContent` an id and target that, or use a child combinator from `Screen`),
   then raise the inset to `0 3` so every screen sits at a uniform 3 cells instead of today's
   2 / 3 / 5. Verify `.panel`'s extra `padding: 0 1` (`widgets.py:70-79`) doesn't reintroduce
   a mismatch between panelled (Builds) and bare-table (Containers) screens.
2. **Gap below the tab/sub-tab bar.** Nothing provides one today. Add
   `TabbedContent > ContentSwitcher { margin-top: 1 }` near `app.py:120-123`. Confirm it
   applies to both tab levels or add the nested rule explicitly.
3. **Exactly three button archetypes**, codified in `widgets.py` and documented in DESIGN.md:
   - **A — action-row button** (outside tables, the only general-purpose type). Geometry of
     today's `.thin-button` (h1, min-width 10, no border). Prefer Textual's native
     `Button(compact=True)` (0b) as the mechanism and keep a thin class only for
     `min-width`/`margin-right`; if `compact` doesn't reproduce the exact geometry, keep the
     CSS class — verify, don't assume. Colour comes from `variant=` only.
   - **B — in-table action cell.** Not a Button at all: `Text("[ Update ]")` in a dedicated
     column, dispatched by Phase 2's framework. Codify the exact rendering (bracket style,
     column width, and `$error`-styled text for destructive actions like Remove).
   - **C — inline input-adjacent button.** Today's `.inline-row Button` (h3, matches `Input`
     height). Give it a distinct colour per item 7.
   Convert all **27 bare-button sites** listed in 0b to archetype A. Give `.action-row-*
   Button` a uniform `margin-right` in SHARED_CSS and delete the one-off
   `settings.py:96-98`.
4. **Kill the resting highlight** (items 2g, 8a) with CSS in SHARED_CSS:
   ```css
   DataTable > .datatable--cursor { background: transparent; color: $foreground; text-style: none; }
   DataTable > .datatable--fixed  { background: $surface; color: $foreground; }
   ```
   Blurred table → no visible band; focus/click repaints via `&:focus > .datatable--cursor`.
   This removes both the row-0 band and the amber fixed-column band (0a.5) while keeping every
   selection message alive.
5. **DESIGN.md**: add the button-geometry rule (absent today), add the side-inset and
   tab-gap rules, and resolve the `.button-row` vs `.action-row-*` naming contradiction by
   picking one and updating every reference.
6. **Token realism** (`widgets.py:45-47`): `$space-tight` and `$space-normal` are both `1`.
   Either collapse to a real scale or give them distinct values, then convert the hardcoded
   `1`/`2` values in screen `DEFAULT_CSS` (`dashboard.py:53,57,66,70`; `settings.py:97,107,112,119`;
   `builds.py:143,148`; `containers.py:37,38`) to tokens.

**Verification**: `python3 -m py_compile cockpit/*.py cockpit/screens/*.py`;
`grep -rn "Button(" cockpit/screens/ | grep -v "compact\|thin-button\|inline-row"` → only
intentional archetype-C sites; screenshot every tab at ≥110 cols and confirm identical left
inset and a visible gap under both tab rows; open Backends and GPU Topology and confirm no
band appears before interaction, and that clicking a row still selects it.

**Anti-pattern guards**: don't fix inset by adding per-screen padding (that recreates the
divergence); don't use `show_cursor=False`; don't introduce a fourth button style.

---

## Phase 2: Table action-column framework (the shared primitive)

**What to implement.** Extend `SingleClickDataTable` (`cockpit/widgets.py:255-276` — read it
first, it already does meta-based click dispatch) into a reusable action-column mechanism:

- Declare action columns per table (column key → action id + label + style + optional
  per-row predicate, e.g. "Update" only renders when an update is available).
- Override `_on_click`, read `event.style.meta["column"]` and `["row"]` (0a.3), map to the
  action, and `event.prevent_default()` so the base handler doesn't also move the cursor.
- Destructive actions route through the existing `confirm(..., mutates_system=)` helper from
  the design-standards work (`CockpitScreenBase`) — the Y/N pop-up item 2f asks for comes free.
- Render via `Text` only (0a.6); use `update_cell`/`update_cell_at` (0a.6) for in-place
  refresh after an action rather than rebuilding the whole table.
- **Fix the broken tick-boxes** (item 2h): 0a diagnosed the likely cause as the click path
  (`widgets.py:262-276`) versus the full-table rebuild in the `RowSelected` handlers
  (`downloads.py:231`, `scripts.py:339`) — one clobbers the other's state. Read both, fix the
  root cause once here rather than per screen. Tick-boxes stay on Downloads/Scripts/Containers
  even though Backends drops them in Phase 3.

**Verification**: add an `assert`-based self-check (the repo already has this convention —
`python3 -m cockpit.widgets`) proving action dispatch fires for the right (row, column) and
that a destructive action requires confirmation. Manually click a tick-box twice and confirm
the state survives.

**Anti-pattern guards**: no widgets in cells; don't use `CellSelected`/`RowSelected` for action
dispatch (two-click requirement, 0a.4); don't rebuild the entire table on every cell toggle.

---

## Phase 3: LLM > Backends (item 2)

**What to implement** — `cockpit/screens/builds.py`, using Phase 2's framework:

- (a) Remove the "Components" and "Retained Builds" subtitle headings.
- (b) Move the Retained Builds table into a modal, opened by a new button placed **left of**
  "Build History". This also resolves the DESIGN.md §3.1 "max 2 tables per screen" violation
  flagged in the previous handover (Backends currently has 3).
- (c) Add an **Update** action column, rendered only on rows where an update is available.
- (d) Add a **Rollback** action column.
- (e) Replace the constant `[★]` Pinned cell (`builds.py:292,301`) with the **actual pinned
  value** — `ref[:10]` for llama.cpp, `version` for llama-swap (decision 0d.2). Rename the
  column accordingly (e.g. "Pinned ref").
- (f) Add a **Remove** action column as the last column, `$error`-styled, with a Y/N confirm.
- (g) Resting highlight — already fixed by Phase 1; verify on this screen specifically since
  it's the screenshot the operator flagged.
- (h) Drop the tick-box column here (per-row actions supersede it) and re-check whether
  `fixed_columns` is still wanted once column 0 is no longer a marker.
- Delete the now-redundant "Update selected" and "Rollback" action-row buttons. Keep "Build
  History", "Check for Updates", and the new Retained Builds button, all archetype A.

**Verification**: `py_compile`; screenshot the tab — no subtitles, no resting band, 2 tables
max (one now modal); confirm Update appears only on updatable rows; confirm Remove prompts
before acting; confirm the pinned column shows a real ref matching `manifest.yaml`.

**Anti-pattern guards**: don't add a pin/unpin toggle (0c, 0d.2); don't let Remove act without
confirmation; don't reintroduce a third inline table.

---

## Phase 4: LLM > Models (item 3)

**What to implement** — `cockpit/screens/deploy.py`:

- (a) Convert the bare buttons at `deploy.py:543-546,548,549` to archetype A.
- (b) Move **Edit** and **Delete** into action columns via Phase 2's framework; Delete is
  `$error`-styled and confirmed.
- (c) Put the remaining buttons on a single row with **"Apply and restart" first**, separated
  from the rest by extra spacing to mark it as the distinct, consequential action.

**Verification**: `py_compile`; screenshot — one button row, Apply-and-restart visually
separated; Edit/Delete work per-row; Delete confirms.

**Anti-pattern guards**: don't change model CRUD or `models.yaml` write/validate logic — this
is presentation only. `deploy.py`'s validate-then-write path stays exactly as-is.

---

## Phase 5: Containers and Scripts (items 5, 6)

**What to implement**

- **Containers** (`cockpit/screens/containers.py`): (a) add a per-row **Restart** action column
  (Phase 2 framework, archetype B); (b) collapse the current wrapped button block
  (`containers.py:87,88,90` — all three bare default buttons) into a single row of archetype-A
  buttons.
- **Scripts** (`cockpit/screens/scripts.py`): the operator asked for "a similar pass based on
  the above logic". Read the screen first and **enumerate what you find** — every button
  (`scripts.py:261-264,266,267` are bare defaults), every table, every bulk action — then apply
  the same treatment: per-row actions into columns where they're row-scoped, remaining buttons
  onto one archetype-A row, tick-boxes retained for genuine bulk operations. If a Scripts
  action has no obvious per-row analogue, leave it in the action row and say so in the report
  rather than inventing one.

**Verification**: `py_compile`; screenshot both tabs — no 3-cell buttons anywhere, no wrapped
button rows; per-row Restart works and confirms (it restarts a service → `mutates_system=True`).

**Anti-pattern guards**: don't alter container lifecycle or script execution logic; don't
remove bulk tick-boxes from these two screens (unlike Backends, bulk operations are the point).

---

## Phase 6: Dashboard — per-GPU sections (item 1)

**What to implement** — `cockpit/screens/dashboard.py`:

- Replace the aggregate "GPU"/"VRAM" bars with **one section per device**, iterating
  `metrics.read_gpus()` (already per-device, 0c) instead of `primary = gpus[0]`
  (`dashboard.py:263`) and the VRAM `sum(...)` (`:273-274`). Each section headed by the device
  name and index.
- **Remove the DISK bar entirely.**
- Fix the disconnected value text: use Textual's `ProgressBar` with `show_eta=False` and
  `Bar { width: 1fr; }` — `PercentageStatus` is width-5 right-aligned immediately right of the
  bar (0a.7), which is exactly the requested adjacency. Copy the composition from
  `.venv/.../textual/widgets/_progress_bar.py:293-302`.
- **Mark unavailable stats explicitly** (decision 0d.4): Intel → `utilization_pct is None`
  renders "not reported by xpu-smi", never a 0% bar; AMD → section renders with a "no AMD
  telemetry" note. VRAM still renders for Intel (real data).
- Structural reference: `git show 5c986a4^:cockpit/screens/dashboard.py` has the pre-regression
  per-slot gauge layout (`self._gpu_slots`, ids `res-gpu-{slot_idx}-util-bar`) — copy the
  structure, **not** the sampling.
- Fix the stale module docstring at `metrics.py:1-5` claiming metrics are "called roughly once
  a second from a UI timer" — it contradicts the `5c986a4` fix. (This is the one sanctioned
  `provision/` edit in this plan; docstring only.)

**Verification**: `py_compile`; launch and confirm one section per configured GPU with correct
names; confirm bar values sit immediately right of their bars; **confirm no timer exists** —
`grep -rn "set_interval" cockpit/screens/dashboard.py` must be empty; watch GPU fans/telemetry
for at least a minute with the tab open to confirm no ramp.

**Anti-pattern guards**: **never** reintroduce `set_interval` polling (0c — this caused a real
hardware fan-ramp bug); never render a 0% bar for a stat that is `None`; don't add AMD
collection here (out of scope, decided in 0d.4).

---

## Phase 7: LLM > HF Downloads — reframe to tracked models (item 4)

The operator called this screen purposeless and was explicit that they're open to design
suggestions. Target shape: the top bar becomes **"track a model"**, and the table below is the
list of tracked models with real, useful columns.

**What to implement** — `cockpit/screens/downloads.py`:

- (a) Add real separation between the top input section and the table (uses Phase 1's tokens).
- (b) Reframe the top bar from a download trigger into a **track** input. Tracking already
  exists in the data model — a tracked-not-downloaded model is `hf.model_status() == "missing"`
  (0c), and `downloads.py:359-376` already registers ad-hoc input into `models.yaml` with a
  synthesized bind (`:355-357`). So this is mostly re-labelling plus deferring the download.
- Table columns: model id / repo, **status (three-state, decision 0d.3)**, size, release date.
  - Status values: `downloaded` (local file exists) / `ready for download` (HF API 200) /
    `needs auth to verify` (401 without token) / `not found` (404, token present only).
  - Size and dates come from `GET https://huggingface.co/api/models/{repo_id}?blobs=true` →
    `siblings[].size`, `lastModified`, `createdAt` (verified live, 0c).
- Use **plain `urllib.request`**, following `update_check.py`'s precedent — `huggingface_hub`
  is not importable from `.venv` (0c).
- Download becomes a **per-row action column** (Phase 2 framework), replacing the "Download
  selected"/"Download all" buttons the operator flagged as nonsensical. Keep a bulk
  "Download all missing" on the action row only if tick-boxes are retained.
- **Rate limits**: validate on refresh or explicit action, not per-row on every render —
  follow `update_check.py:8-9`'s "not a tight poll loop" convention. Do the lookups in the
  existing `@work(thread=True)` path and toast on completion per DESIGN.md §6.

**Verification**: `py_compile`; track a known-good repo → "ready for download" with a real size
and date; track a nonsense repo without a token → "needs auth to verify" (NOT "not found");
with `HF_TOKEN` exported, the same nonsense repo → "not found"; a downloaded model still reads
"downloaded"; confirm no HF call happens on every keystroke.

**Anti-pattern guards**: don't import `huggingface_hub`; don't report "not found" on a 401
(0c — it's indistinguishable from private); don't bypass `schema.validate_models_dict` when
writing `models.yaml`; don't fire network calls per-row on render.

---

## Phase 8: Settings (items 7, 8, 9)

**What to implement** — `cockpit/screens/settings.py`:

- **Host Profile (item 7)**: give the "ip a" button archetype C (h3, matching the `Input` row —
  `.inline-row Button` already sets `height: 3`, so verify whether it's actually applying or
  losing to specificity) plus the requested distinct colour. **Move the HF token field out** to
  a new **Connectors** sub-tab — a deliberate catch-all for third-party integrations, built to
  be extended (Tailscale arguably belongs there later; don't move it in this pass unless it
  falls out naturally).
- **GPU Topology (item 8)**: (a) resting-highlight fix lands via Phase 1 — verify on this exact
  screen (it's the second screenshot); (b) move the printed Driver Status block into a
  **column in the table** rather than a text dump below it; (c) convert the bare buttons
  (`settings.py:318-320` etc.) to archetype A; (d) **add an add-GPU capability** — the
  first-run wizard already has `_show_add_gpu_form`/`_save_gpu_form` in this same file; reuse
  that form rather than writing a second one.
- **System Services (item 9)**: (a) the operator finds many fields purposeless. **Audit and
  propose, do not delete unilaterally** — produce a list of each field, what writes/reads it,
  and a recommendation (keep / merge / drop), and leave the removals for approval. Fields that
  are genuinely load-bearing in `provision/steps/swap.py`'s unit generation must stay. (b) Fix
  the bottom buttons to archetype A.

**Verification**: `py_compile`; HF token no longer on Host Profile and the Connectors tab
saves it correctly; GPU table shows driver info as a column with no resting band; adding a GPU
persists to `hosts/<hostname>.yaml` and passes `schema.validate_host_profile_dict`; screenshot
all three Settings sub-tabs.

**Anti-pattern guards**: don't delete a System Services field without confirming nothing in
`provision/` consumes it; don't write a second add-GPU form; don't break the first-run wizard
while refactoring shared form code.

---

## Phase 9: Verification

1. Re-run every phase's checklist from a clean `git status`.
2. `python3 -m py_compile cockpit/*.py cockpit/screens/*.py provision/*.py provision/steps/*.py`
   (AGENTS.md's own documented command).
3. `python3 -m cockpit.widgets` — the existing self-check convention, now also covering Phase 2.
4. Run `smoke/verify_screens.py` (exists in-repo, currently unexecuted — check what it does and
   whether it needs updating for the new screens).
5. Grep sweep:
   - `grep -rn "set_interval" cockpit/screens/dashboard.py` → empty.
   - `grep -rn "Button(" cockpit/screens/` → every site is archetype A (compact) or a
     deliberate archetype-C inline button.
   - `grep -rn "huggingface_hub" cockpit/` → empty.
   - `grep -rn "show_cursor" cockpit/` → no `False`.
   - Every `add_column(` still carries `width=` (the existing DESIGN.md §4 contract).
6. Screenshot all tabs at 80×24 and ≥110 columns; confirm uniform side inset, tab-bar gap, no
   resting highlight, no 3-cell buttons outside inline rows.
7. Update `cockpit/DESIGN.md` to match what actually shipped, and reconcile it with
   `plans/02-screen-archetypes.md` (which currently describes archetypes the code only
   partially implements).

**Report**: pass/fail per item, plus the System Services field audit from Phase 8 awaiting
approval, plus any QA item that turned out to need a product decision rather than a fix.
