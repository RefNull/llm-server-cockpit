# Plan: Backends tab — compact the chrome, make the status mean something

Baseline: HEAD `00d20f3`. Source: operator QA, 2026-09-18, on the tab as shipped by
`plans/06`.

Two different problems wearing one coat. **(a)(b)(c)(d)(e)(g) are layout**: the sections
spend half a page on four lines, the buttons are in the wrong places, and one of them
should not exist. **(f) is a correctness bug, and it is mine twice over** — see §0a. Fix
(f) first; the layout work is easier once the table's Status column stops lying.

Each phase is self-contained and can run as its own `Agent` call in a fresh context. Give
the phase plus Phase 0.

**Dependency order is strict.** Phase 1 changes what Status *means*; Phases 2-3 then edit
`cockpit/screens/builds.py` one after another.

---

## Phase 0: Discovery (DONE — cite it, do not redo)

Read from source at `00d20f3`.

### 0a. The table's Status column is the global check, repeated

`_refresh_backends_table` renders the section line and every table row from **the same
object**:

```python
self.query_one("#cpp-update-status", Static).update(self._format_update_cell(self._llama_cpp_check))
...
for backend in self.backends:
    status_cell = Text("building…", style="dim") if backend in self._building_backends \
                  else self._format_update_cell(self._llama_cpp_check)
```

So the Status column is (i) a verbatim duplicate of the line directly above the table,
(ii) a **global value rendered per row** — the identical error that removing the Version
column fixed in `053ce59`, left standing in the next column along, and (iii) silent about
the only per-row question worth asking: *was this backend compiled from the selected
version?*

**And `_confirm_and_update_to_latest` fakes it:**

```python
self._llama_cpp_check = {**check, "pinned": new_ref, "update_available": False}
```

After writing the pin, it locally flips `update_available` to False, so the section line
**and every row** read "up to date" when nothing was fetched, compiled or installed. The
comparison is literally true — pin now equals upstream — but the operator reads "up to
date" as a claim about their machine, and it is not one.

The operator's second point is the sharper one: **even a fetched source does not make an
existing build current.** A build compiled from the old ref is stale the moment the pin
moves, and nothing on screen says so.

### 0b. The three facts the tab currently conflates

| Fact | Scope | Where it belongs |
|---|---|---|
| upstream HEAD vs the pin | **global** | the section line, once |
| local source checkout vs the pin | **global** | implicit today; Phase 1 makes it real |
| each backend's active build vs the pin | **per backend** | the table's Status column |

### 0c. Ground truth for the fix

- `_ensure_checkout(runner, repo, ref, checkout_dir)` (`build.py:57`) is **already
  idempotent** — it returns early when `_git_head(checkout_dir) == ref`, otherwise clones
  or fetches that exact SHA and checks it out. It goes through `Runner`, so it is
  sudo- and dry-run-safe. It is private; a screen must not call it directly (the same rule
  that made `swap.install_pinned_binary` a public wrapper in `053ce59`).
- `checkout_dir_for(host_profile)` (`build.py:156`) is public.
- `list_builds` returns `{"id", "version", "legacy", "current", "sane", "outcome",
  "timestamp"}` per build — enough to answer the per-row question without new I/O.
- `CockpitScreenBase.ensure_first_view` (`widgets.py:851-862`) latches on
  `_first_view_done`, so `on_first_view` fires **once per app run**. But
  `CockpitApp._load_visible_screens` (`app.py:253`) is called from
  `on_tabbed_content_tab_activated` (`:248`) on **every** tab switch. There is no
  per-view hook today; Phase 2 adds one.
- `update_check._CACHE_TTL_S` is 24h, cached at `hosts/upstream.update-cache.yaml`.
- `SystemdScreen` has `_view_unitfile(row_key)` and `_view_logs(row_key)` and keys rows by
  unit name, so (d) is reachable. Tab switching is `TabbedContent.active`.

### 0d. Operator decisions, 2026-09-18

1. **"Update to latest" bumps the pin AND fetches the source** to it, so the local checkout
   genuinely matches. Root + network, so it runs behind the build-log modal like a build.
   It does **not** rebuild — that stays a per-row decision.
2. **"Add build" is the existing Add-deployment button, renamed.** Same behaviour: bind a
   backend to a GPU, a row appears. Flagged and accepted: it sits beside a per-row
   `[ Build ]` that compiles, so two controls say "build" and mean different things.

### 0e. Anti-patterns (every phase)

1. **No explanatory prose on this tab.** Two such notes were deleted in `053ce59` and the
   habit is what the operator called "ballooned". Labels and values only.
2. **Never name a method `_render`** — collides with `Widget._render()`; Textual calls
   yours, gets `None`, crashes in the compositor, and both `py_compile` and the CSS
   self-check pass.
3. **Keep every `Button(` call on one line** — DESIGN.md §9's grep reads that line only and
   cannot classify a wrapped call.
4. **DESIGN.md §4.1** explicit `width=`; **§4.4** render ≤ 115 on screen, and a **modal**
   table is budgeted against its own dialog — measure it mounted, never compute it.
5. **§3.0** data reads in the refresh path. **§1** spacing tokens. **§9** button archetypes.
6. **Never execute a llama.cpp binary to read a version** — it wakes the GPU.
7. Use `.venv/bin/python` for every gate.

---

## Phase 1: Make Status a per-backend fact, and make Update fetch

**Files**: `provision/steps/build.py`, `cockpit/screens/builds.py`, `smoke/verify_build_pin.py`.

1. **A public fetch in `build.py`.** Wrap `_ensure_checkout` so the screen can bring the
   local checkout to the pinned ref without a build. It is already idempotent; do not
   reimplement it. Keep it `Runner`-driven so `--dry-run` stays honest.
2. **"Update to latest" writes the pin, then fetches**, behind the existing root gate and
   the build-log modal (`_open_log_modal`) so the operator sees a multi-second network
   operation rather than a frozen button. **Delete the
   `{**check, "update_available": False}` line** — re-derive the check from the new pin
   instead of asserting the answer. It does **not** rebuild.
3. **Status becomes per-backend build staleness**, derived from `list_builds` and the pin:

   | condition | text | style |
   |---|---|---|
   | no build for this backend | `not built` | dim |
   | active build's version == pin | `up to date` | green |
   | active build's version != pin, **and** a sane build at the pin exists | `pinned at <ver>` | **orange** — a deliberate choice, the operator activated an older build |
   | active build's version != pin, **and** no build at the pin exists | `out of date, built on <ver>` | **red** |

   The distinction between the last two is derivable: if a build at the current pin is
   available and not active, the operator chose this one. **The global upstream comparison
   leaves the table entirely** and lives only in the section line.
4. `building…` keeps its precedence over all of the above.

### Verification

- [ ] `python3 -m py_compile provision/steps/build.py cockpit/screens/builds.py`
- [ ] `.venv/bin/python smoke/verify_build_pin.py`
- [ ] `.venv/bin/python smoke/verify_rendered_units.py` — `config.yaml` byte-identical.
- [ ] All four Status states rendered from fixtures, asserted by text **and** colour.
- [ ] **After an Update, a backend built at the old ref reads `out of date`, not
      `up to date`.** This is the reported bug; assert it directly.
- [ ] The fetch is idempotent — calling it twice at the same ref runs no git command the
      second time.
- [ ] `grep -n "update_available.*False" cockpit/screens/builds.py` — empty.
- [ ] **Break the staleness derivation, watch the test fail, restore from your own copy.**

### Anti-pattern guards

- Do **not** call `_ensure_checkout` from the screen — wrap it.
- Do **not** make Update rebuild. Operator decision §0d-1.
- Do **not** leave any global value in a per-row column. That is the bug.

---

## Phase 2: Compact the sections, fix the buttons, check on view

**Depends on Phase 1.** **Files**: `cockpit/screens/builds.py`, `cockpit/widgets.py`,
`cockpit/app.py`, `smoke/verify_screens.py`.

1. **Compact both sections (a, e).** Llama-swap currently spends four `.form-row`s on four
   facts. Collapse to the operator's own shape — roughly:

   ```
   Llama-swap   v255 · update available (v256)   [ Update to latest ]
                /usr/local/bin/llama-swap · llama-swap.service (active, enabled)
   ```

   and llama.cpp to a single line of the same shape. Exact wording is yours; the
   requirement is **one or two lines per section, not a stack of label/value rows**, and no
   prose.
2. **The update button sits beside the status (b)**, and appears **only when an update is
   available** — not disabled-and-greyed, absent.
3. **Delete the "Check for Updates" button (c, g).** Replace with an automatic check **when
   the tab is shown**, served from cache within **15 minutes**. Two pieces:
   - `update_check` needs a shorter TTL for this path. Do not simply drop the 24h constant
     — `bin/check-updates` and the systemd timer share that module. Say what you chose.
   - `CockpitScreenBase` needs a **per-view hook**. `ensure_first_view` latches on
     `_first_view_done` and fires once; `CockpitApp._load_visible_screens` already runs on
     every `TabbedContent.TabActivated`. Add a hook that fires each time a screen becomes
     visible, distinct from `on_first_view`, and **leave `on_first_view`'s contract alone** —
     other screens rely on it (DESIGN.md §3.0, and `verify_screens.py` asserts launch does
     not repeat work).
   - **llama-swap gets its own check (c)** rather than piggybacking on llama.cpp's button,
     which is what made it counterintuitive.
4. **Table buttons become `Add build`, `Unmanaged builds`, `Build log` (g).** The first is
   the existing Add-deployment button renamed (§0d-2); the second is `Foreign builds`
   renamed. Behaviour unchanged for both.
5. **Version actions move out of the button row**: `Update to latest` beside its status per
   (b), and `Change version…` somewhere in the llama.cpp section rather than under the
   table. The table's buttons are about *builds*; the section's are about *versions*.

### Verification

- [ ] `py_compile`, `.venv/bin/python -m cockpit.widgets`
- [ ] `.venv/bin/python smoke/verify_screens.py` at 80×24 and 121×30
- [ ] **Print the measured vertical cells each section occupies, before and after.** The
      complaint is space; the number is the evidence.
- [ ] `grep -n "btn-check-updates" cockpit/screens/builds.py` — empty.
- [ ] Update buttons are **absent** when no update is available, not merely disabled.
- [ ] Switching away from the tab and back re-checks after the TTL and **does not** within
      it — assert both directions.
- [ ] `verify_screens.py`'s existing launch assertions still hold: no repeated work, no
      GitHub call at launch. The new per-view hook is the most likely thing to break them.

### Anti-pattern guards

- Do **not** change `on_first_view`'s semantics.
- Do **not** let the per-view hook fire network I/O on the global `r` refresh —
  `on_refresh_requested`'s no-network contract stands.
- Do **not** replace deleted rows with prose.

---

## Phase 3: "(see process)" jumps to the unit (d)

**Depends on Phase 2.** **Files**: `cockpit/screens/builds.py`, `cockpit/app.py`,
`cockpit/screens/systemd.py`, `smoke/verify_screens.py`.

After the systemd unit on the llama-swap line, an affordance that switches to
**System → Systemd** with `llama-swap.service` shown. `SystemdScreen` keys rows by unit
name and has `_view_unitfile(row_key)` / `_view_logs(row_key)`; tab switching is
`TabbedContent.active`. Decide and report whether it selects the row or opens the unit
file directly — the operator said "with the llama-swap.service info opened", so prefer
opening it if that is reachable without new machinery.

**If the target unit is not in the list** (hidden by the operator's hide-list, or systemd
absent), say so plainly rather than switching to a tab where nothing is selected.

### Verification

- [ ] `verify_screens.py` green; the jump works from a mounted app and lands on the unit.
- [ ] The not-found path is exercised, not just the happy one.
- [ ] No new `Button(` violates §9; if this is an inline affordance it carries the marker
      comment **with** the trailing `.inline-row` the grep keys on.

---

## Phase 4: Verification and docs

1. **Full sweep**: `py_compile`, `cockpit.widgets`, all six smoke scripts, the schema gate.
   `verify_no_gpu_wake.py` is not optional — Phase 1 touches the build path.
2. **Anti-pattern sweep**: `btn-check-updates`, `update_available.*False`, the §9 Button
   grep, and no global value in a per-row cell.
3. **Docs**:
   - `cockpit/DESIGN.md` §4.4's `backends-table` entry — the Status column's meaning
     changes completely, and the Installs layout contract recorded in `00d20f3` describes
     sections that this plan compacts.
   - If a per-view hook lands, **DESIGN.md §3.0 must describe it** alongside
     `on_first_view`, or the next reader will use the wrong one.
   - `README.md`'s Installs description if the button set changed.
4. **Host-only**: a real fetch under Update; the four Status states against real builds; the
   systemd jump; and whether the compacted sections read well at 80 columns.

---

## Open, carried forward and still unanswered

1. **`manifest.yaml` serves two purposes.** `AGENTS.md` says backend recipes are repo-level
   and a host profile only says which backends it uses — but the Installs tab edits those
   recipes per machine. Invisible with one deployment, wrong with two. **Raised three times,
   never answered.** A canon change, not a phase's business.
2. **`settings.py`'s `_host_profile_path()` keys on the `hostname:` field inside the file**
   while `app.py` loads `hosts/<host_name>.yaml` from `--host` or the machine hostname. If
   they disagree a save lands in a different file. Dormant on `haupe-server`; five call
   sites.
3. **Only `BuildsScreen` re-reads `app.host_profile`**, so other tabs still need a restart
   to see a GPU binding.
