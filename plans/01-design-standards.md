# Plan: Cockpit Design Standards & Skeleton

Goal: stop "throwing stuff on the wall" in cockpit/screens/*.py by producing (1) a written
design-language standard, (2) an enforced code skeleton screens must inherit from so
violations fail structurally instead of by convention, and (3) all 4 existing screens
retrofitted onto it. Future screens attach to the skeleton; they don't reinvent layout.

Scope decisions locked in before this plan was written (see conversation, 2026-09-14):
- Deliverable: doc **+** code skeleton, not doc alone.
- Retrofit all 4 existing screens (Installs/Builds, Deploy, Downloads, Settings) in this plan.
- "Device" scope: a future web surface is planned but not built. Resolved below (Phase 1) —
  it does not need a separate design system.
- Doc lives at `cockpit/DESIGN.md`.

Each phase below is self-contained: it can be run as its own `Agent(model: "opus")` call in a
fresh context. Give the phase's own prompt plus this file's Phase 0 section — that's the
grounding it needs; it doesn't need this plan's other phases loaded.

---

## Phase 0: Documentation Discovery (already done — do not redo)

### 0a. Textual framework capabilities (verified against installed `textual==8.2.8`,
`.venv/lib/python3.14/site-packages/textual/`)

1. **Breakpoints are real.** `App.HORIZONTAL_BREAKPOINTS` / `App.VERTICAL_BREAKPOINTS`:
   `ClassVar[list[tuple[int, str]]]`, overridable per-`Screen`. Each tuple is
   `(minimum cells, CSS class name)`; Textual applies the matching class to the Screen on
   resize; target it with plain TCSS (`Screen.-wide { … }`). Source:
   `textual/app.py:488-523`, `textual/screen.py:224-227,1510-1562`;
   [App API](https://textual.textualize.io/api/app/), [Screen API](https://textual.textualize.io/api/screen/).
   Classes are mutually exclusive (highest match wins); include a `(0, "-narrow")`-style
   entry or nothing applies below your lowest threshold. This is the sanctioned mechanism —
   **do not hand-roll `on_resize` layout-switching**; that's the exception path, not the default.

2. **DataTable already degrades via horizontal scroll, not clipping.** `DataTable` is a
   `ScrollView` (`overflow-x: auto; overflow-y: auto` by default,
   `widgets/_data_table.py:268`, `scroll_view.py:28-32`). A too-wide table scrolls
   sideways today — the "columns cut off at the screen edge" problem visible in
   `screenshots/target-design/target-img1.png` and `target-img3.png` is a *discoverability*
   problem (nothing tells the operator to scroll), not a rendering bug. Levers:
   `add_column(width=…)` and `fixed_columns=` (pin leftmost columns while scrolling).
   [DataTable docs](https://textual.textualize.io/widgets/data_table/).

3. **No documented minimum terminal size.** Textual states none. The only `80×24` in the
   framework is a fallback default when size can't be detected
   (`drivers/web_driver.py:162`, `app.py:2137`) — not a supported-minimum guarantee. This
   standard must pick its own floor and say so as a project decision, not cite Textual for it.

4. **Layout primitives**: `Vertical`, `VerticalGroup`, `VerticalScroll`, `Horizontal`,
   `HorizontalGroup`, `HorizontalScroll`, `Grid`, `ItemGrid`, `Center`/`Right`/`Middle`,
   `dock`, `layers`; sizing via `fr` units, `%`, `min-width`/`max-width`/`min-height`/`max-height`.
   Two-column → one-column reflow = swap `layout: vertical` under the narrow breakpoint
   class, or change `grid-size`. [Layout guide](https://textual.textualize.io/guide/layout/),
   [styles reference](https://textual.textualize.io/styles/width/).

5. **Design tokens: two real mechanisms, not prose convention.** `Theme` (already in use —
   `cockpit/widgets.py:25-37`'s `AMBER_THEME`) auto-generates `$primary`, `$surface`,
   `$panel`, `$boost`, `$text-muted`, `$text-disabled`, plus `-lighten-N`/`-darken-N` shades,
   and re-resolves on theme switch — **colors belong here, not as new literal hex values in
   screen CSS**. Plain `$`-prefixed TCSS variables are values-only (cannot appear in a
   selector) — **spacing belongs here**, as a small fixed scale in `SHARED_CSS`.
   [Design guide](https://textual.textualize.io/guide/design/), [CSS guide](https://textual.textualize.io/guide/CSS/).

6. **The "planned web surface" is not a separate design problem.** `textual-serve` /
   `textual-web` run the *same app, same code, same CSS* in a browser via an xterm.js
   terminal emulation — not a web-native re-render. No new styling system, no new layout
   constraint. The only real adaptation needed if/when that surface is stood up: route
   file/link actions through `App.deliver_text` / `App.deliver_binary` / `App.open_url`
   instead of direct filesystem or `webbrowser` calls.
   [Towards Textual Web Applications](https://textual.textualize.io/blog/2024/09/08/towards-textual-web-applications/).
   **Consequence for this plan**: there is no Phase for "the web version of the standard."
   One standard, one skeleton, one future `textual-serve` deploy step when that's wanted.

### 0b. Existing repo conventions (verified by direct read, this session, commit 66ad5d4 +
this session's bootstrap.py dedup)

Full writeup: see the "existing conventions" note carried into Phase 1 below — summarized:

- **Already consistent** (formalize, don't redesign): one shared `SHARED_CSS` in
  `cockpit/widgets.py` cascaded via `CockpitApp.CSS`; `.panel`/`.panel-title`/`.section-title`/
  `.subtitle`/`.button-row`/`.thin-button`/`.status-text`/`.error-text`/`.inline-row` classes;
  every screen is a `Widget` in a `TabPane`, wrapped in `VerticalScroll`; every mutating
  action already routes through `ConfirmModal` before acting; `AMBER_THEME` is a real
  registered `Theme`.
- **Not yet codified** (Phase 1 must decide, Phase 2 must enforce): DataTable column
  widths/`fixed_columns` are never set anywhere today; no screen has ever needed a filter
  control so there's no placement precedent; `danger=True` on `ConfirmModal` is applied
  inconsistently (build/rollback/save/delete/deploy get it, "Apply service settings" and
  "Check/Enable WOL" — which mutate systemd units and NIC flags — don't); some background
  actions call both `self.notify()` and update a local status line, others only the status
  line, with no stated rule; only Settings uses a 2-column layout, with no stated width
  threshold for when a screen earns one; `on_refresh_requested()` is a duck-typed contract
  (`hasattr` check in `app.py:142-146`), not an enforced one.
- **Visual references in repo**: `screenshots/target-design/*.png` (aspirational — amber
  theme, ASCII banner, single active-tab underline, action-row directly above its table —
  but does *not* solve wide-table-on-narrow-terminal, see 0a.2 above, don't copy that gap);
  `screenshots/app-build-current/*.png` (stale, pre-amber-overhaul — blank Downloads tab,
  no scroll containers, table headers with no visible body — the "don't regress to this" case).

**Anti-pattern guard for every phase below**: do not invent Textual CSS properties, `Theme`
fields, or container types beyond what's listed in 0a. If a phase needs something not listed
here, it must re-verify against `textual/` source or the docs URLs above before using it.

---

## Phase 1: Author `cockpit/DESIGN.md`

**What to implement.** Write the standards document itself. Structure:

1. **Tokens** — colors: point at `AMBER_THEME` (`cockpit/widgets.py:25-37`) as the single
   source of truth; forbid new hex literals in screen `DEFAULT_CSS`, require `$primary` /
   `$accent` / `$text-muted` etc. Spacing: define a small fixed scale (e.g. `$space-tight: 1`,
   `$space-normal: 1`, `$space-section: 2` in cell units — match what's already used ad hoc
   across the 4 screens' `DEFAULT_CSS` blocks; don't invent new values, extract the ones
   already in use and name them) as new `$` variables in `SHARED_CSS`.
2. **Terminal-size contract** — state the supported floor explicitly as a project decision
   (recommend 80×24, matching Textual's own fallback default, but say "we chose this" not
   "Textual requires this"). Define the breakpoint(s) using `App.HORIZONTAL_BREAKPOINTS`
   (recommend one break, e.g. `[(0, "-narrow"), (110, "-wide")]` — tune the 110 against
   Settings' actual 2-column min-width today, `min-width: 45` per column ×2 +
   margins, cockpit/screens/settings.py:92-93).
3. **Page structure rules** — resolve, don't leave implicit:
   - Max tables per screen (current max is 2, Builds tab) — set as the ceiling; a 3rd table
     forces a design conversation, not a silent addition.
   - Filter placement (no precedent exists — set one: a filter/search `Input` lives in its
     own row immediately above the table it filters, left-aligned, never inside `.button-row`).
   - Button-row placement (already consistent — codify: directly under the table/form it
     acts on, never floated to a screen-level footer).
   - Popup/modal trigger placement (already consistent — codify: the button that opens a
     `ConfirmModal`/`InfoModal` is the same button that performs the action; no separate
     "..." menu pattern).
   - 2-column layout threshold: only above the `-wide` breakpoint from item 2; single column
     below it, always.
4. **DataTable contract** — every `add_column` call sets `width=`; every table that can
   plausibly exceed the `-narrow` breakpoint's usable width sets `fixed_columns >= 1`. State
   which existing tables need this (builds-table, history-table, models-table, model-table,
   gpu-table — check each against typical column count/content width).
5. **Confirm-modal danger tiers** — define what "danger" means (recommend: any action that
   writes outside a validated-then-atomic-write path, restarts a system service, or touches
   host-level state outside `models.yaml`/`hosts/*.yaml` preview = `danger=True`). Apply the
   rule to the two known violators: "Apply service settings" and "Check/Enable WOL"
   (settings.py:890, settings.py:800) — resolve in Phase 3, decide the rule here.
6. **Status feedback rule** — when is `self.app.notify()` (toast) required vs. an inline
   `.status-text` line sufficient. Recommend: any `@work(thread=True)` background action
   requires a toast on completion (success or failure) because the operator may have
   navigated to another tab by the time it finishes; a synchronous validation error is
   inline-only.
7. **Multi-surface note** — one paragraph, citing 0a.6: no separate web design system; if/when
   `textual-serve` is stood up, the same `DESIGN.md` applies unchanged, with the one adaptation
   (`deliver_*`/`open_url` for file/link actions) noted for whoever does that work later.

**Documentation references**: this plan's Phase 0 section (0a, 0b) in full — every rule
above must trace to a numbered finding there. Do not add a rule that isn't grounded in 0a/0b
or an explicit decision recorded in this section.

**Verification checklist**:
- [ ] Every color mentioned in the doc is a `$`-token name, zero hex literals.
- [ ] The terminal-size floor and breakpoint value are stated as *this project's* decision,
      with a one-line rationale each — not attributed to Textual as a requirement.
- [ ] Every rule in §3-6 has a concrete current-screen example or violation cited from 0b.
- [ ] Doc is committed at `cockpit/DESIGN.md`, referenced by one line added to `AGENTS.md`
      under Project (matching the existing convention that project facts live there — do not
      duplicate DESIGN.md's content into AGENTS.md, only a pointer, per AGENTS.md's own
      "duplicated rules drift" rule).

**Anti-pattern guards**: no invented Textual capability (recheck against 0a before writing
any implementation-specific claim); no rule with no current-code example to anchor it (that's
speculative design, not standards-from-evidence); doc must stay short enough to actually be
read — target under 300 lines.

---

## Phase 2: Build the enforced skeleton

**What to implement.** Turn Phase 1's rules that *can* be enforced by code into code, in
`cockpit/widgets.py` (extend, don't fork the existing shared layer):

1. Add the spacing `$` tokens from DESIGN.md §1 to `SHARED_CSS`.
2. Add `HORIZONTAL_BREAKPOINTS` (per DESIGN.md §2) to `CockpitApp` in `cockpit/app.py`, and
   the corresponding `Screen.-narrow`/`Screen.-wide` CSS hooks to `SHARED_CSS` for the
   2-column-threshold rule.
3. Add a `CockpitScreenBase(Widget)` class (name it what fits; it is the "skeleton" the user
   asked for) that:
   - Enforces the `VerticalScroll`-wrapped-content convention (either by providing a
     `compose()` template method screens fill in, or by asserting in `on_mount` that the
     first child is a scroll container — pick whichever is less invasive to the 4 existing
     screens' structure, since Phase 3 retrofits them).
   - Makes `on_refresh_requested()` an actual abstract method (raise `NotImplementedError`
     in the base, per DESIGN.md's resolution of the duck-typed-contract gap in 0b) instead of
     the current `hasattr` check in `cockpit/app.py:144-146`.
   - Provide a small helper for the confirm-modal danger-tier rule (e.g. a
     `confirm(message, *, mutates_system: bool)` wrapper around `push_screen_wait(ConfirmModal(...))`
     that sets `danger=True` automatically per DESIGN.md §5, so screens can't forget it).
4. Add a `DataTable` construction helper (or a documented required-kwargs pattern) that
   fails loudly (assert / raise) if `width=` is omitted on `add_column`, per DESIGN.md §4.

**Documentation references**: `cockpit/widgets.py` (existing file, extend in place);
DESIGN.md from Phase 1 (the rules being encoded); Textual `App.HORIZONTAL_BREAKPOINTS` per
Phase 0 finding 0a.1 (cite the exact source lines/doc URL again in code comments, since this
is exactly the kind of framework fact that should never be re-guessed later).

**Verification checklist**:
- [ ] `python3 -m py_compile cockpit/*.py cockpit/screens/*.py` passes.
- [ ] A throwaway screen that skips `on_refresh_requested()` fails to instantiate (or fails
      loudly at mount) — prove the enforcement isn't just documentation.
- [ ] A throwaway `add_column(...)` call without `width=` fails loudly under the new helper.
- [ ] Resizing the running app (via the `run` skill or `textual run --dev`) across the
      breakpoint threshold visibly changes which CSS class is applied (verify via Textual's
      dev console / `on_resize` logging, not just by eyeballing layout).

**Anti-pattern guards**: don't build a generic "responsive framework" beyond what DESIGN.md
actually calls for — one breakpoint, not a speculative N-breakpoint system; don't rewrite
`ConfirmModal`/`InfoModal` (they're already correct per 0b), only add the thin danger-tier
wrapper; don't touch `provision/` — this plan is cockpit-only.

---

## Phase 3: Retrofit the 4 existing screens

**What to implement.** For each of `cockpit/screens/{builds,deploy,downloads,settings}.py`:

1. Subclass the new base from Phase 2 instead of `Widget` directly.
2. Replace any `hasattr(widget, "on_refresh_requested")` reliance with the enforced contract
   (already each screen defines this method today — confirm each still does, now required).
3. Set `width=` on every `add_column(...)` call; add `fixed_columns=1` on any table identified
   in DESIGN.md §4 as exceeding the narrow-breakpoint width (builds-table, history-table,
   models-table likely candidates given their column counts — check against actual content).
4. Fix the two known danger-tier violations: `settings.py:890` ("Apply service settings")
   and `settings.py:800` ("Check/Enable WOL") — route through the Phase 2 `confirm()` helper
   with `mutates_system=True`.
5. Apply the status-feedback rule from DESIGN.md §6: audit every `@work(thread=True)` method
   in all 4 screens (there are ~8: builds.py's `_run_build`/`_run_rollback`/`_run_update_check`,
   downloads.py's `_run_download_one`/`_run_download_all`, settings.py's
   `_run_wol`/`_run_drivers_check`/`_apply_service_in_background`, deploy.py's
   `_apply_in_background`) and add `self.app.call_from_thread(self.app.notify, ...)` on
   completion anywhere it's currently missing (deploy.py's `_apply_in_background` is the one
   flagged in 0b).
6. Apply the 2-column breakpoint hook (Phase 2 item 2) to Settings' existing
   `#settings-columns` layout so it collapses to one column below the `-wide` breakpoint,
   since it's currently fixed-2-column regardless of terminal width (settings.py:236).

**Documentation references**: `cockpit/DESIGN.md` (Phase 1 output, the rules being applied);
the Phase 2 skeleton in `cockpit/widgets.py`/`cockpit/app.py`; the specific line numbers
above, already verified this session — don't re-derive them, use them.

**Verification checklist**:
- [ ] `python3 -m py_compile` clean across `cockpit/`.
- [ ] Each of the 4 screens instantiates without error (smoke-run via the `run` skill against
      `hosts/example.yaml`-shaped test data, or the real `models.yaml`/host profile if present).
- [ ] Grep confirms zero `add_column(` calls without `width=` remain in `cockpit/screens/`.
- [ ] Grep confirms `settings.py`'s WOL-check and service-settings confirms now pass
      `danger=True` (or use the Phase 2 `mutates_system=True` wrapper).
- [ ] Visual check (screenshot each tab, per the `run` skill, at both a ≥110-wide and an
      80-wide terminal) — Settings collapses to one column below 110; no table column runs
      off the visible edge without a `fixed_columns` pinned identifier column staying visible.

**Anti-pattern guards**: this is a retrofit, not a rewrite — don't restructure business logic
(model CRUD, build/rollback flows, WOL/driver checks) while touching layout; if a screen's
existing behavior and DESIGN.md conflict in a way that needs a product decision (not just a
mechanical fix), stop and flag it rather than guessing.

---

## Phase 4: Verification

1. Re-run every checklist item from Phases 1-3 in one pass, from a clean `git status`.
2. `python3 -m py_compile cockpit/*.py cockpit/screens/*.py provision/*.py provision/steps/*.py`
   (matches AGENTS.md's own documented syntax-verification command — reuse it, don't invent
   a new one).
3. Screenshot all 4 tabs at 80×24 and at ≥110 columns (use the `run` skill to launch
   `bin/cockpit` in the Browser/terminal pane; this repo has no existing test harness for the
   TUI beyond manual/visual checks — don't invent one here, that's a separate effort).
4. Grep-based anti-pattern sweep:
   - `grep -rn "add_column(" cockpit/screens/` → every call has `width=`.
   - `grep -rn "ConfirmModal(" cockpit/screens/` → cross-check each against DESIGN.md §5's
     danger-tier rule by hand (this one needs a human judgment call per call site, not a
     mechanical grep pass/fail).
   - `grep -rn "class.*Screen.*Widget" cockpit/screens/` → should return zero; all 4 should
     now read `class ...(CockpitScreenBase)` (or whatever Phase 2 named it).
5. Diff the running app's Installs/Deploy/Downloads/Settings tabs against
   `screenshots/target-design/*.png` for aesthetic drift (amber theme, banner, tab-underline
   style) — this is a sanity check against the original aspirational reference, not a
   pixel-match requirement.
6. Update `cockpit/DESIGN.md` if Phase 3 surfaced a rule that needed adjusting once applied
   to real screens (expected — the doc is not assumed perfect on first write).

**Report format**: pass/fail per checklist item above, plus a short list of any DESIGN.md
rules that changed during retrofit and why.
