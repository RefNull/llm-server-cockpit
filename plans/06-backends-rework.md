# Plan: Backends tab rework — subtract the sprawl, make builds first-class

Baseline: HEAD `63e1c45`. Source: operator QA, 2026-09-18, on the tab as shipped by
`plans/05` Phase 2 and its two follow-ups.

**This is mostly a subtraction plan, and the thing being subtracted is mine.** Phase 2 of
`plans/05` answered every QA question by *adding* — a version line, a source note, a
foreign-builds note, a What/Where/How modal. Each was individually defensible and the sum
is a tab nobody can read. The operator's words: *"ballooned to adding long texts and making
the whole UX more convoluted; whereas it really should be very straightforward and simple."*

The correction is not more explanation. It is fewer, better-placed things: two sections,
one table, one editor, one builds list. Where this plan adds, it adds because something
genuinely has no home today — per-build identity (A7) and a way to add a deployment (A4).

Each phase is self-contained and can run as its own `Agent` call in a fresh context. Give
the phase plus Phase 0.

**Dependency order is strict.** Phase 1 changes what a build *is*; Phases 2-4 all edit
`cockpit/screens/builds.py` and must run one after another. Phase 5 is docs and gates.

---

## Phase 0: Discovery (DONE — cite it, do not redo)

Read from source at `63e1c45`. Negative claims were established by exhaustive grep over
`cockpit/`, `provision/`, `smoke/`, `bin/`, excluding `.venv`/`.git`/`__pycache__`.

### 0a. The root of A7 is one line

`list_builds` (`provision/steps/build.py:280-292`) ends:

```python
return [{"ref": d.name, "current": d == current_target, "sane": _prefix_ready(d)} for d in dirs]
```

**`ref` is the directory name reinterpreted as a version.** `backend_prefix` (`:148-150`)
builds `prefix_root/<backend>/<ref>`, and `run()` (`:356`) passes the manifest pin — which
is exactly why rebuilding the same ref reuses the same directory, the behaviour A7 removes.

Seven consumers inherit the assumption and **all break** when a directory is named `build7`:

| consumer | file:line | breaks how |
|---|---|---|
| `_build_label` | `builds.py:1090-1091` | `build7 != <sha>` → button says "Build" forever |
| `_installed_ref` | `builds.py:1115-1117` | returns `build7`, not a version |
| `_installed_cell` | `builds.py:1120-1127` | every row renders permanently yellow "differs from pin" |
| `_confirm_and_change_version` | `builds.py:1296-1300` | the "already built" hint silently never fires |
| `ChangeVersionModal._populate_local` | `builds.py:628-634` | see §0b — this one is a live bug |
| `RetainedBuildsModal._refresh` | `builds.py:438-452` | renders `build7` under a column labelled "Ref" |
| `dashboard._compute_llm_text` | `dashboard.py:281-291` | Dashboard reports `built · build7` |

Name-agnostic and therefore **safe**: `list_builds`'s own `iterdir`/mtime sort
(`:287-291`), `_prune_old_builds` (`:220-224`), `find_foreign_builds` (`:105-135`), and
`swap.py:176` — which embeds `f"{prefix_root}/{backend}/current/bin/llama-server"` and so
depends only on the `current` symlink, never on the directory name. **`swap.py` needs no
change.**

### 0b. A latent bug to fix while we are here

`ChangeVersionModal._populate_local` (`builds.py:628-634`) adds each on-disk build's
directory name to the version picker as a pinnable SHA, and `_on_table_action_invoked`
(`:669-678`) dismisses that row key unvalidated for `kind == "sha"`, which reaches
`_write_pin` (`:1315`) and `_write_manifest_ref`. **`_write_manifest_ref` has no assertion
that the incoming ref is a SHA** — `_MANIFEST_REF_RE` (`:52`) only constrains the line being
*replaced*. Today the directory names happen to be SHAs so nothing is wrong. Under A7 a
`build7` row would attempt to pin `build7`, the substitution would silently no-op, and the
operator would be told the pin changed when it did not.

**Fix the validation regardless of A7**: `_write_manifest_ref` must reject a non-SHA.

> **Correction, 2026-09-18.** Half of this was already fixed before Phase 3 reached it: the
> `_populate_local` line now reads `b.get("version")`, changed in `d925e91`'s compat pass
> when Phase 1 landed, which this section was written before and never updated. Phase 3's
> worker found that and declined to claim the no-op. Only the `_write_manifest_ref` guard
> was still outstanding; it landed in `cb34852`.

### 0c. Build logs are never persisted

In-memory only, and **wiped at the start of every build** (`builds.py:1228` resets
`_log_buffer` to `[]`). Trace: `Runner.on_output` (`common.py:94-112`) →
`_on_build_output` (`builds.py:1365-1368`) → `_append_build_log` (`:1376-1379`) → the
`_log_buffer` list and, when open, `BuildLogModal`. No `write_file`, no path under
`state_dir`, anywhere. The only compiler-adjacent text that survives the process is the
history JSONL's `detail` field — and that is the *smoke test's* output, truncated to 2000
chars (`build.py:254`), not the build's.

A7's "link to respective build's log" therefore requires persisting it, which does not
exist today.

### 0d. History and pruning

**`_record_history`** (`build.py:244-259`) writes five keys — `timestamp`, `backend`,
`ref`, `outcome` (`smoke_pass` | `smoke_failed` | `build_failed`), `detail[:2000]` — to
`state_dir/build-history.jsonl`. It bypasses `Runner`: plain `mkdir` + `open(...,"a")`
(`:257-259`), so **no sudo path and no dry-run announce**. Called at `:367`, `:393`, `:397`.

**Two readers, not one** — `BuildHistoryModal.on_mount` (`builds.py:312`) and, non-obviously,
`ChangeVersionModal._populate_local` (`builds.py:624-627`), which mines it for pinnable refs.

**`_prune_old_builds`** (`build.py:215-237`): keeps `current` plus the newest by mtime until
`retain_builds` entries, `rm -rf`s the rest through `Runner` (sudo- and dry-run-gated).
Sole call site `:400` — **only after a passing smoke test and the symlink flip.** Never on
failure, never on the already-built shortcut. `retain_builds` is validated at
`schema.py:104-105` (`int >= 1`; note `bool` passes `isinstance(True, int)`) and read by
`build.py:344` and `settings.py:467,671-672,714,907-911,919`.

### 0e. Prefix contents — a metadata file is safe

`_prefix_ready` (`build.py:60-68`) checks **only** `bin/llama-cli` and `bin/llama-server`
(`is_file()` + `os.access(X_OK)`). Nothing in this repo enumerates, whitelists or counts a
prefix's contents. `list_builds` and `_prune_old_builds` iterate the *parent*
(`backend_dir`), filtering `d.is_dir()`. `cmake --install` copies in and removes nothing
unknown; `rm -rf` on removal takes the whole directory, so no orphan.

**So extra files at the prefix root are invisible and safe.** One caveat: a file placed one
level up, *beside* `current` in `backend_dir`, is skipped by both iterators' `is_dir()`
filter — so a per-backend index file there would never be pruned. Keep metadata **inside**
each build directory.

**Gap:** upstream llama.cpp's full `install()` manifest was not verified — the pinned
checkout is a runtime path not present here. A dotfile name is very unlikely to collide;
nothing in this repo would detect it either way.

### 0f. Rollback

No function named `activate`. `rollback` (`build.py:295-302`) validates only
`_prefix_ready(target)` — **no check that `target` is inside `prefix_root`, and no
rejection of `..` or `/` in `target_ref`.** Deliberately skips re-smoke-testing. Exactly one
caller: `RetainedBuildsModal._run_rollback` (`builds.py:490-501`). `Runner.atomic_symlink`
(`common.py:167-185`) is create-tmp-then-`os.replace`, dry-run gated.

Sibling delete `RetainedBuildsModal._run_remove` (`builds.py:503-512`) inlines
`rm -rf` rather than calling into `build.py` — a known deviation (`HANDOVER.md:31-32`).

### 0g. Untested today

**Nothing in `smoke/` asserts `list_builds`'s return shape, reads or writes the history
JSONL, or exercises `_prune_old_builds` at all.** Phase 1 rewrites all three, so it is
writing their first tests, not adjusting existing ones.

Existing smoke that *does* encode the old convention:
`verify_build_pin.py:147` (fixture builds a ref-named prefix), `:176-187`
(`verify_force_flag` encodes "same ref ⇒ same directory ⇒ skippable"), **`:410-413` (hard
assertion on `backend_prefix`'s return path)**, `:414-421`.
`verify_sysinfo.py:298-334` uses `abc123` as a directory name but asserts name-agnostically,
so it passes either way.

### 0h. Operator decisions, 2026-09-18

1. **Keep auto-pruning; drop the name.** `retain_builds` keeps working silently. The
   "Retained Builds" label and its separate modal disappear; there is one "Builds" list.
2. **"Add deployment" binds an existing backend to an existing GPU.** It writes
   `hosts/<hostname>.yaml` `gpus[].backends`. It must **not** become a second GPU editor —
   it cannot add or rename GPUs, and Settings → GPU Topology stays the only place for that.
3. **The two sections are symmetric.** Llama-swap and Llama.cpp each carry version, status
   and an update action in the same shape.

### 0i. Display primitives to use (verified present)

- `.section-title` — `widgets.py:113-118`, `text-style: bold; color: $accent`. The yellow
  subtitle the operator referenced; precedent `deploy.py:807,812`.
- `.form-row` / `.form-label` (w=20, muted, bold) / `.form-field` (`width: 1fr; max-width: 40`)
  — `widgets.py:222-248`. This is the field:value shape A6 asks for; it is not "floating
  prose".
- `.panel`, `.action-row-primary` / `.action-row-secondary`, `.thin-button` — as used today.
- `TableAction` supports a callable `label` **and** a callable `confirm` (`widgets.py`).

### 0j. Anti-patterns (repo-wide, every phase)

1. **No widgets in `DataTable` cells, ever.**
2. **Never `yaml.safe_dump` `manifest.yaml`** — targeted rewrites only, with the existing
   re-parse-and-abort guard.
3. **Never execute a discovered or built llama.cpp binary** to read a version — it wakes the
   GPU. `smoke/verify_no_gpu_wake.py` enforces it.
4. **DESIGN.md §1**: spacing tokens, never bare `1`/`2`/`3`, in screen `DEFAULT_CSS`.
5. **DESIGN.md §9**: every `Button` is `thin-button`, `close-button`, or inline-with-marker.
   No `height`/`min-width`/`border`/`padding` on a Button in screen CSS.
6. **DESIGN.md §4.1**: explicit `width=` on every `add_column`. **§4.4**: render width ≤ 115.
7. **DESIGN.md §3.0**: every data read in the refresh path, never `__init__`/`compose`.
8. **Do not name a method `_render`** — it collides with `Widget._render()`, Textual calls
   yours, gets `None`, and crashes in the compositor. `py_compile` and the CSS self-check
   both pass with this bug in place; only `verify_screens.py`'s real mount catches it.
9. Use `.venv/bin/python` for every gate.

---

## Phase 1: A build gets its own directory and its own record

**Files**: `provision/steps/build.py`, `provision/schema.py` (only if needed), `smoke/verify_build_pin.py`.
**Nothing in `cockpit/` — Phase 2 consumes what this produces.**

### What to implement

1. **Sequential build directories.** `prefix_root/<backend>/build<N>/`, N chosen as
   max-existing + 1, robust to gaps and to the `current` symlink not matching. Replace
   `backend_prefix(host_profile, backend, ref)` with something that allocates a new
   directory for a build and resolves an existing one by id. **Its current signature is
   hard-asserted at `smoke/verify_build_pin.py:410-413`** — update that assertion
   deliberately, do not delete it.
2. **Per-build metadata, inside the build directory** (§0e: safe there, *not* one level up).
   One file recording at minimum: the `ref` built, the exact `cmake_flags` used, the
   resolved argv, timestamp, and outcome. This is what makes `list_builds` able to report a
   real version once the directory name stops being one, and it is A7's "config as it was
   used".
3. **Persist the build log** into the same directory. §0c: nothing writes it today. Route
   it through `Runner` so the sudo and dry-run paths stay honest.
4. **`list_builds` returns build identity and version as separate fields.** It must no
   longer conflate them. Keep `current` and `sane`. Read the version from metadata; a legacy
   `<sha>`-named directory with no metadata should still be listed — report its name as the
   version and mark it legacy rather than crashing or hiding it. **Do not migrate existing
   directories automatically.**
5. **Fold history into the same record.** §0d: `build-history.jsonl` exists because builds
   had no per-build home. Now they do. Keep a failed build's record too — a build that
   failed to compile has no directory, so decide and state where its record goes (the JSONL
   may legitimately survive for exactly that case). **Say what you chose and why.**
6. **Pruning keeps working** (operator decision §0h-1) and still never deletes `current`.
7. **Harden `rollback`** (§0f): reject a target that is not a direct child of
   `prefix_root/<backend>`, and reject `..` or a path separator in the id.

### Verification

- [ ] `python3 -m py_compile provision/steps/build.py provision/schema.py`
- [ ] `.venv/bin/python smoke/verify_build_pin.py`
- [ ] `.venv/bin/python smoke/verify_rendered_units.py` — proves `swap.py`'s generated
      `config.yaml` is **byte-identical**, since it goes through `current` only (§0a).
- [ ] **First-ever tests for three untested behaviours** (§0g): `list_builds`'s return
      shape; pruning (keeps `current`, keeps N, deletes the rest, never deletes `current`
      even when it is oldest); and the metadata round-trip.
- [ ] Two consecutive builds at the **same ref** produce **two** directories, and the first
      is not modified. This is A7's whole point — assert it directly.
- [ ] A legacy `<sha>`-named directory with no metadata is still listed.
- [ ] `rollback` rejects `../../etc` and an absolute path.

### Anti-pattern guards

- Do **not** put metadata or an index beside `current` in `backend_dir` — §0e: both
  iterators filter `d.is_dir()`, so it would be invisible and never pruned.
- Do **not** change `swap.py`. §0a: it depends only on `current`.
- Do **not** bypass `Runner` for the log write. `_record_history` already does that
  (§0d) and it is a defect, not a precedent.
- Do **not** re-smoke-test on rollback — `build.py:296-298` says why.

---

## Phase 2: Two sections, one table (A1, A2, A3, A5, A8)

**Depends on Phase 1.** **File**: `cockpit/screens/builds.py`.

### What to implement

1. **Section one — "Llama-swap"**, under a `.section-title`. **Not a table.** `.form-row`
   field:value lines: version, update status, binary path, and the systemd unit with its
   active state if resolvable. Then an update action. The llama-swap **row leaves the
   table** — it never belonged there; its Build and Info cells were always blank.
2. **Section two — "Llama.cpp"**, under a `.section-title`, mirroring section one
   (operator decision §0h-3): version, update status, and the version actions
   (`Update to latest`, `Change version…`, `Check for Updates`).
3. **Delete both floating notes (A3)**: the `Selected version: …` line and the
   `#backend-source-note`. Their content is now either in the section header (version) or
   nowhere (the source note — the "Add deployment" button in Phase 4 makes it unnecessary).
4. **Foreign builds behind a button (A2)**: `#foreign-builds-note` stops being an
   always-visible block and becomes a modal behind a button under the table. The scan
   still belongs in the refresh path (§0j-7).
5. **Table columns.** Component becomes just the backend — the section says llama.cpp:

   `Backend` 14, `Active build` 20, `Status` 32, `[ Build ]` 9, `[ Edit ]` 8
   → 83 content, render 83 + 2×5 = **93 ≤ 115**.

   **Corrected 2026-09-18**: this list originally included `[ Builds ]` 10, which contradicted
   Phase 3 — that action's modal does not exist until then, and Phase 2 item 6 says not to
   pre-empt the merge. Phase 3 adds the column (render becomes 105).

   `EX. cuda` is 8 cells; `build3 · 481c65f09` is 19; `update available (d1d3c33ab1)` is 29.
6. **Status drops the word "latest" (A5a)** — `update available (<id>)` is sufficient and
   was what truncated the id.
7. **`[ Build ]` is always "Build" (A8).** Delete `_build_label`'s callable entirely. The
   confirm dialog's header says whether it is a rebuild. **This also dissolves a real
   inconsistency**: `_build_label` asked "is any build at this ref *sane*" while
   `_installed_cell` asked "is one *current*" — two different definitions of "built" in
   adjacent cells, which is why the operator saw `Not built` next to `Rebuild`.
8. **`Active build` replaces `Installed`** and shows build identity *and* version, which
   Phase 1 now makes separable.

### Verification

- [ ] `py_compile`, `.venv/bin/python -m cockpit.widgets`
- [ ] `.venv/bin/python smoke/verify_screens.py` at 80×24 and 121×30
- [ ] Print the computed render width; assert ≤ 115
- [ ] `grep -n "Selected version\|backend-source-note" cockpit/screens/builds.py` — empty
- [ ] `grep -n "_build_label" cockpit/screens/builds.py` — empty
- [ ] No llama-swap row in the table; llama-swap's version still visible on screen
- [ ] §9 Button grep clean

### Anti-pattern guards

- Do **not** replace the removed notes with tooltips or longer labels. The instruction is
  subtraction; the information either has a home or it goes.
- Do **not** reintroduce a per-row Version column — that is what made "Update to latest"
  read as "changes every row".

---

## Phase 3: The Edit modal, and one Builds list (A6, A7-UI, A9)

**Depends on Phase 2.** **File**: `cockpit/screens/builds.py`.

### What to implement

1. **`[ Info ]` becomes `[ Edit ]`**, and `BackendDetailModal` is rewritten as a plain
   field:value form (`.form-row`/`.form-label`/`.form-field`, §0i). **Delete the What /
   Where / How headings and every editorial aside** — `(nothing built yet)`,
   `Defined in manifest`, and the rest. The operator's list, verbatim:

   ```
   Backend:         cuda
   GPU:             gpu-nvidia (nvidia)
   Build status:    not built   |   build3 active
   Build location:  /opt/llm-server/builds/cuda/
   ```

   then, **in this order, bottom-weighted**:

   ```
   cmake_flags      [editable TextArea]
   Build command    [read-only, updates live as the flags above change]
   ```

2. **The build command updates live** as `cmake_flags` is edited — `TextArea.Changed`
   recomputes through the same `resolve_cmake_argv` the build uses, so what is displayed
   cannot drift from what runs.
3. **One "Builds" list replaces Retained Builds and Build History** (A7). Per build: its
   directory, the version it was built at, the config as used, its log, whether it is
   active, and whether it is sane. **Remove the word "retained" from the UI** — pruning
   still happens (§0h-1), it is simply not a thing the operator manages.
4. **Selecting a previous build activates it (A9)** — this is the existing `rollback`,
   surfaced where it belongs instead of inside a modal called "Retained Builds".
5. **Reachable per row** via `[ Builds ]`, scoped to that backend.
6. **Fix the latent pin bug (§0b)**: `_write_manifest_ref` must reject a non-SHA, and
   `ChangeVersionModal` must not offer a build directory id as a pinnable version.

### Verification

- [ ] `py_compile`, `cockpit.widgets`, `verify_screens.py` at both sizes
- [ ] Modal containment at **80×24** — it carries paths and a multi-line command, and a
      110-cell dialog shipped once already by not checking this
- [ ] Editing a flag updates the displayed build command **without saving**
- [ ] The displayed command equals `resolve_cmake_argv`'s output for the same flags
- [ ] `_write_manifest_ref` rejects `build7` and accepts a 40-hex SHA — **watch it fail
      before the fix**, since §0b is a live bug
- [ ] Activating a previous build repoints `current`; the table's `Active build` follows
- [ ] `grep -rn "Retained\|retained" cockpit/screens/builds.py` — no operator-facing use

### Anti-pattern guards

- Do **not** name any method `_render` (§0j-8).
- Do **not** keep a second modal for history. One list.
- Do **not** let the Edit modal write anything but `cmake_flags`.

---

## Phase 4: Add deployment (A4)

**Depends on Phase 3.** **Files**: `cockpit/screens/builds.py`, and the host-profile write path.

### What to implement

A button under the table opening a small modal: pick a GPU **already in the host profile**,
pick a backend from `manifest.yaml`'s recipes, write that pairing into `gpus[].backends`.
A row appears.

Per operator decision §0h-2: it **cannot add or rename GPUs**. Settings → GPU Topology
remains the only editor for `gpus[]` itself. Reuse that screen's existing validation rather
than writing a second one — the backend-name check already derives from
`manifest["backends"].keys()` as of `63e1c45`, and must keep doing so.

### Verification

- [ ] Binding a backend already present on that GPU is rejected, not duplicated
- [ ] A backend with no `manifest.yaml` recipe cannot be selected
- [ ] The host profile round-trips: written, re-read, validates
- [ ] `verify_screens.py` green; modal contained at 80×24
- [ ] `grep` shows no second copy of the GPU-editing form

### Anti-pattern guards

- Do **not** duplicate the GPU editor.
- Do **not** write `manifest.yaml` here — this binds an existing recipe, it does not define
  one.

---

## Phase 5: Verification and docs

1. **Full sweep**:
   ```bash
   python3 -m py_compile cockpit/*.py cockpit/screens/*.py provision/*.py provision/steps/*.py
   .venv/bin/python -m cockpit.widgets
   .venv/bin/python smoke/verify_rendered_units.py
   .venv/bin/python smoke/verify_runner_sudo.py
   .venv/bin/python smoke/verify_sysinfo.py
   .venv/bin/python smoke/verify_build_pin.py
   .venv/bin/python smoke/verify_screens.py
   .venv/bin/python smoke/verify_no_gpu_wake.py
   ```
   `verify_no_gpu_wake.py` is not optional — Phase 1 touches build paths.

2. **Schema gate** (`AGENTS.md`'s one-liner).

3. **Anti-pattern sweep**: `_build_label`, `Selected version`, `backend-source-note`,
   operator-facing "retained", the §9 Button grep.

4. **Docs — these go stale the moment Phase 1 lands**:
   - `README.md:134`, `:229`, `:303` state the `<prefix_root>/<backend>/<ref>` convention.
   - `cockpit/DESIGN.md:151` (the `backends-table` entry — columns change twice in this
     plan), `:159-160` (history-table and retained-table specs, both of which cease to
     exist), `:104` and `:150` (max-3-tables precedent counts), `:206-208` (rollback tier,
     now reachable from a differently-named place), `:245-248` (toast-on-completion).
   - `plans/05-qa-remediation-pass.md` cites `build.py` line numbers that **already** no
     longer match. Do not repair them; note at the top of that file that it describes
     `af1985a`..`63e1c45` and that `plans/06` supersedes its Phase 2 UI decisions.

5. **Host-only, hand to the operator**: a real build producing two directories at the same
   ref; the persisted log and config being readable from the Builds list; activating a
   previous build and seeing llama-swap pick it up.

---

## Open, deliberately not decided here

1. **`manifest.yaml` is now serving two purposes.** [RESOLVED in `plans/08-manifest-per-deployment.md`:
   `manifest.example.yaml` ships repo-level recipes as a tracked template; `manifest.yaml` is
   per-deployment and gitignored, tuned per machine via the TUI editor.]
2. **Legacy `<sha>`-named build directories** are listed but never migrated (Phase 1 item 4).
   If they should be adopted or cleaned up, that is a separate decision.
3. `update_check.cache_age_seconds()` still has no callers; `state_dir/update-check-result.json`
   is still written and read by nothing. Both survived `plans/05` Phase 7 and remain dead.
