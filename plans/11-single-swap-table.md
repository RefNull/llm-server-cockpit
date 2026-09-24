# Plan: collapse Deploy's two llama-swap tables into one

Baseline: HEAD `66b9f85`. Source: operator, 2026-09-24 — *"This page in the app is now
exclusively for llama-swap, whereas my idea was to have one table be for llama-swap (the
bottom one) and the top one is for directly - self managed - deployments against a
backend. I don't need to list the models that llama-swap unloads vs keeps loaded
indefinitely in two different tables, they can be in the same with ttl stated as 0."*

Confirmed back to the operator and correct: **Pinned/Swappable is a display split this
session invented on top of `plans/09`, not something the operator asked for.** Every model
in `models.yaml` is already one flat list; `_is_pinned` (`group` + `ttl==0`) only decides
which of two tables a row renders in. Collapsing that back into one table, with TTL as an
ordinary column showing `0` for a pinned model, loses no information — DESIGN.md's own
Archetype B rationale for the split ("what is pinned in my VRAM right now?") is answered by
a column, not a second table.

This plan is **scoping only** — nothing here is implemented. It stands alone and does not
depend on `plans/10-direct-launch-inventory.md` (the "top table" the operator describes);
it only needs `plans/10` amended once this lands (Phase 3 below), since `plans/10` was
scoped against a two-table Deploy tab that no longer exists after this plan runs.

No new review pass is needed before this plan: the `e62b2e8..HEAD` review already covered
this exact code and its 8 findings are fixed (`66b9f85`). This restructuring touches the
same functions again, so Phase 2's verification re-runs are not optional.

---

## Phase 0: Discovery

### 0a. Every consumer of the Pinned/Swappable split (complete list, grepped at `66b9f85`)

`cockpit/screens/deploy.py`:
- `compose()` (`:1247-1284`): two `Static` section titles, two `SingleClickDataTable`s
  (`#models-pinned`, `#models-swappable`), two Add buttons (`btn-add-pinned`,
  `btn-add-swappable`), one explanatory subtitle Static that exists only to disambiguate the
  two tables from each other.
- `on_mount()` (`:1286-1325`): builds each table's columns separately — **Pinned has no TTL
  column and has a `run` (Start/Stop) action column; Swappable has a TTL column and no `run`
  action.** This asymmetry is not print budget — `swap.load_model`/`unload_model`
  (`provision/steps/swap.py`) don't check pinned status at all, so a Swappable model simply
  has no manual Start/Stop today. Decision 1 below.
- `_is_pinned()` (`:1327-1344`): the two-condition predicate (`group` and `ttl in (0, None)`)
  that decides which table a row goes in. Nothing else needs this *predicate* once there is
  one table — see 0b.
- `_populate_table()` (`:1367-1402`): the only place that reads `_is_pinned`, branches the
  row into one table or the other, and — because Pinned drops the TTL column — omits the TTL
  cell entirely for a pinned row.
- `EditModelModal.__init__`/`compose()` (`:391, 579, 587`): `pinned_seed`, used only to
  prefill `ttl=0` and `group="always-on"` when the modal was opened from "Add Pinned Model".
- `_on_add_model(pinned: bool)` (`:1418-1434`) and the button dispatch (`:1580-1585`): the
  only reason `EditModelModal` takes a `pinned` param at all.

`smoke/verify_swap_import.py:213-227`: a truth table asserting `_is_pinned` against six
cases — the only test coverage of the predicate itself.

`smoke/verify_screens.py`: **no direct references** to `models-pinned`/`models-swappable`
ids — the generic per-screen table-width-budget and geometry sweep (`_assert_table_widths_in_budget`,
the `("deploy", "llm", "#llm-tabs", "models")` navigation entry at `:995`) applies to
whatever tables `DeployScreen` composes, so it needs no changes, only a clean re-run.

### 0b. What still needs the pinned/not-pinned *concept*, versus what only needed the split

- **`provision/steps/swap.py::parse_config_for_import`**'s advisory note (*"in group X with
  ttl Ns: idle-unloads after timeout"*) inlines the same `ttl > 0` condition, but it is
  independent of table routing — it fires on import regardless of what UI renders the
  result afterward. **Not in scope, not touched.**
- **Nothing else in the codebase calls `_is_pinned`.** Once `_populate_table` no longer
  branches on it, it has no callers left. Per `contract.md` "Cost of existing", it is
  deleted, not kept "for later."

### 0c. Allowed APIs / patterns to copy

- `SingleClickDataTable`, `TableAction`, `add_action_column` (`cockpit/widgets.py`) — same
  APIs already in use, just declared once instead of twice.
- `rich.text.Text` for every cell (DESIGN.md §4.6, already the convention in this file).
- DESIGN.md §4's 115-cell on-screen budget: merged column set is
  ID(22) + Engine(10) + GPU(9) + Backend(9) + Group(10) + TTL(10) + Status(9) = 79, plus
  three action columns (`run`/`edit`/`delete`, each ≤10 wide) ≈ 109 — inside budget with
  room to spare, so no column needs to shrink or drop.

---

## Decisions

**Decision 1 — Start/Stop becomes available on every row, not just formerly-Pinned ones.**
Recommended, and the natural consequence of merging: `swap.load_model`/`unload_model` never
checked pinned status (`provision/steps/swap.py:150-166`), so the old omission on Swappable
was an artifact of the two-table split, not a deliberate restriction. One table needs one
action-column set; giving every row Start/Stop is strictly more capable, not a behavior
change an operator could be relying on the absence of. **Flagging for override**: if the
operator specifically wanted Swappable models to stay pure "loads on request only", say so
and Phase 1 keeps `run` conditional on `_is_pinned`'s old condition (kept as a private
column-visibility helper only, not a routing decision).

**Decision 2 — the TTL column shows the literal configured value, not a "never" euphemism.**
The operator's own words: *"ttl stated as 0"*. Today's Swappable-table TTL cell prints
`"never"` for `ttl == 0` (`deploy.py:1396`) — drop that; show `"0"` (or the actual number, or
blank when unset) for every row. This also removes the one remaining place that quietly
implied "0 means permanently safe", which is only true in combination with group membership
(`_is_pinned`'s own docstring) — showing the raw number next to the Group column is more
honest than a label that depends on a column the operator has to cross-reference anyway.

**Decision 3 — one "Add Model" button, no ttl/group prefill.** The two-button convenience
(`pinned_seed` prefilling `ttl=0` + `group="always-on"`) only existed because there were two
destinations to seed toward. With one table there is one button; the ttl/group fields stay
ordinary, operator-filled fields, blank by default (llama-swap's own default). This is a
minor convenience loss for the "I always want this pinned" case — acceptable given the
operator asked for less structure here, not more; revisit only if it turns out to be missed
in practice.

---

## Phase 1: Merge the tables (`cockpit/screens/deploy.py`)

1. `compose()`: one `Static("Models", classes="section-title")` (or similar — the operator
   may want a different label than "Models"; "llama-swap" is redundant given the subtitle
   below already says so), one `SingleClickDataTable(id="models-swap")`, one
   `Button("Add Model", id="btn-add-model")`. Delete the two-table CSS/markup and the
   explanatory subtitle Static entirely — with one table there is nothing left to
   disambiguate. Keep `#swap-missing` and the `Apply & Restart` / `Import` / `Preview` row
   unchanged.
2. `on_mount()`: one column set — `ID, Engine, GPU, Backend, Group, TTL, Status` — plus
   action columns `run` (per Decision 1: unconditional now), `edit`, `delete`. Delete the
   two separate column-building blocks.
3. `_populate_table()`: one loop, one `table.add_row(...)` call per model, TTL cell always
   present (`Text(str(m["ttl"]) if "ttl" in m else "")` per Decision 2 — no `_is_pinned`
   branch, no "never"/"default" strings).
4. Delete `_is_pinned` entirely (0b).
5. `EditModelModal`: remove the `pinned`/`pinned_seed` parameter and its two prefill
   branches (`:391, 579, 587`) per Decision 3. `_on_add_model` drops its `pinned` parameter;
   `_edit_model` is unaffected (it never used it).
6. Button dispatch (`:1580-1585`): `bid == "btn-add-model"` → `self._on_add_model()`, no
   `pinned=` argument.
7. `_apply_running`/`_refresh_running`/`_run_model_action`: unchanged — they already key by
   `model_id` and don't know about tables.

## Phase 2: Verification

1. Remove the `_is_pinned` truth table (`smoke/verify_swap_import.py:213-227`) — the
   predicate it tests no longer exists. Keep the rest of `check_residency_and_ttl_behavior`
   (the grouped-ttl-600 import-note assertion, `:228-244`) — unaffected, tests the parser
   note, not the screen.
2. `python3 -m py_compile cockpit/screens/deploy.py smoke/verify_swap_import.py`
3. `.venv/bin/python -m cockpit.widgets` — no bare margins introduced.
4. `.venv/bin/python smoke/verify_swap_import.py`
5. `.venv/bin/python smoke/verify_screens.py` — full sweep; this is the one that proves the
   merged table still fits the 115-cell budget at both breakpoints and that
   `_assert_import_models_modal`'s staged-Edit flow (unaffected by this plan, but shares
   `EditModelModal`) still passes with the `pinned` param removed.
6. Manual/real-host: Add a model with `ttl` left blank and no group (shows blank TTL,
   Swappable-shaped today), one with `ttl: 0` + a group (shows `0`, Pinned-shaped today) —
   confirm both appear in the one table, Start/Stop works on both, Apply & Restart still
   generates the same `config.yaml` as before (this plan touches display only,
   `provision/steps/swap.py::_generate_config` is untouched).

## Phase 3: Update `plans/10-direct-launch-inventory.md`

Once Phase 1 lands, `plans/10`'s framing is stale in two places — fix both, don't re-scope
the rest of it:
- Its opening line *"Both existing Deploy tables (Pinned, Swappable) stay exactly as they
  are; this is a third one"* → the Deploy tab has one llama-swap table (bottom) after this
  plan; the direct-launch table is the second, placed **above** it, not a third.
- Phase 3's *"a third ('Direct'...) section, same pattern as Pinned/Swappable"* → same
  pattern as the merged table; update the GPU-reservation note in Decision 3 (*"removes that
  GPU from the options offered... for both Pinned/Swappable"*) to reference the one table.

Nothing else in `plans/10` changes — its Decisions 1-3 (reuse Scripts tab, fixed port,
GPU-reservation-by-removal) are independent of table count.
