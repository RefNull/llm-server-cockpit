# Plan: `manifest.yaml` becomes per-deployment, like every other config file

Baseline: HEAD `cf96210`. Source: operator, 2026-09-18 — *"we ship a manifest.yaml with the
file that assumes key values, where we should ship a blank one that is only populated by the
setup."*

Correct, and this finally settles the canon question parked four times across `plans/05`,
`06` and `07`: `AGENTS.md` says backend recipes are repo-level while the cockpit edits them
per machine. That contradiction dissolves once **the file you ship and the file you edit are
two different files**.

**One refinement to the operator's framing.** Ship it *templated*, not blank — the way
`hosts/example.yaml` ships full structure with `TODO` in the machine-specific slots. Fully
blank would mean setup inventing cmake flags for four backends, and "how to build CUDA" is
genuinely repo knowledge. The template carries recipes and repo URLs; the copy carries pins.

---

## Phase 0: Discovery (DONE — cite it, do not redo)

Read from source at `cf96210`.

### 0a. `manifest.yaml` is the only config file that breaks the repo's own stated pattern

`.gitignore` states the principle outright:

> *Real per-deployment data — hostnames, MACs, home-dir paths, driver fingerprints. Never
> committed to a public repo. See hosts/example.yaml and models.example.yaml for the
> templates that ARE tracked; copy them and fill in your own machine's facts locally.*

| file | template (tracked) | live (gitignored) |
|---|---|---|
| host profile | `hosts/example.yaml` | `hosts/<hostname>.yaml` |
| model catalog | `models.example.yaml` | `models.yaml` |
| scripts | `scripts.example.yaml` | `scripts.yaml` |
| **manifest** | **— none —** | **`manifest.yaml`, tracked and live** |

### 0b. The smoke suite is half-fixture, half-operator-config

`smoke/verify_rendered_units.py:284` loads **`manifest.yaml`** (live) while the same file
loads `hosts/example.yaml` and `models.example.yaml` (fixtures). Since `plans/06` made
`backends.<name>.cmake_flags` editable from the TUI, **an operator tuning their Vulkan flags
changes what the test suite validates.** Introduced by this work; nothing catches it.

### 0c. A git-tracked file now mutates during normal operation

Three write paths added across `plans/05`-`07`, all to `manifest.yaml`:
`_write_manifest_ref` (the pin), `_write_manifest_cmake_flags` (recipes, and clearing
`example: true`), `_write_manifest_llama_swap_version`. So every operator using the cockpit
as designed ends up with a dirty working tree and conflicts on `git pull` — the exact
outcome the `.gitignore` comment exists to prevent.

The `example: true` marking added in `63e1c45` is a symptom of the same confusion: it exists
to annotate shipped values as *"not really yours yet"*, which is what a template file says by
being a template.

### 0d. The four pins are not four of a kind — **and one option in the operator's own decision was based on a false premise of mine**

| pin | reader | verdict |
|---|---|---|
| `llama_cpp.ref` | `build.py` (`run`, `fetch_checkout`) | per-deployment |
| `llama_swap.version` | `swap.py` (`_install_llama_swap`) | per-deployment |
| `backends.*` | `build.py` (`resolve_cmake_flags`), the TUI editor | template, tuned per machine |
| `huggingface_hub.version` | `hf.py` `_ensure_hub_pinned` | **stays in the manifest** |
| `textual.version` | **nothing** — only `schema.py:125` requiring it to exist | **delete** |

**The correction.** The operator was asked whether the toolchain pins should move to
`requirements.txt` and chose to move them. That was the right instinct for `textual` and
wrong for `huggingface_hub`, because the question mis-stated the fact:
`hf.py::_ensure_hub_pinned` installs it **into a separate venv on the managed host**
(`pip install huggingface_hub[cli]==<version>` into `venv_path`), and `requirements.txt`'s
own header says it covers *"Dependencies for provision/ and cockpit/ THEMSELVES — not the
inference stack they manage."* `huggingface_hub` is the stack being managed. It is not
"missing" from requirements; it is deliberately absent.

`textual.version` is the opposite: a dead duplicate of `requirements.txt`'s `textual==8.2.8`
with no reader anywhere in the tree.

### 0e. Anti-patterns

1. **Never `yaml.safe_dump` `manifest.yaml`** — the three targeted rewriters exist because a
   round-trip destroys the header block and every inline comment. The template must keep them.
2. **Do not delete an operator's existing `manifest.yaml`.** On an upgraded checkout it is
   tracked, populated and possibly edited. Migration is a copy, never a clobber.
3. Use `.venv/bin/python` for every gate.

---

## Phase 1: Split the file

**Files**: `manifest.example.yaml` (new), `manifest.yaml`, `.gitignore`, `provision/schema.py`,
`requirements.txt`.

1. **`manifest.example.yaml`, tracked** — today's `manifest.yaml` content, header comment
   block and inline comments intact, minus `textual`. It keeps working default pins: a
   template with no pin is useless, and `hosts/example.yaml`'s `TODO` convention exists for
   values only the operator can know, which a version pin is not.
2. **`manifest.yaml` becomes gitignored**, with `!manifest.example.yaml` alongside, matching
   the `hosts/` stanza. **`git rm --cached manifest.yaml`, never `rm`** — an existing
   checkout keeps its file and its edits.
3. **Delete `textual` from the manifest and its `schema.py` requirement** (§0d). Add nothing
   to `requirements.txt`: it already pins `textual==8.2.8`, and `huggingface_hub` stays in
   the manifest.
4. **Keep `huggingface_hub.version` in the manifest**, and say why in its comment — a future
   reader will otherwise "tidy" it into `requirements.txt`, which is what nearly happened here.

### Verification
- [ ] `python3 -m py_compile provision/schema.py`
- [ ] Schema gate loads `manifest.example.yaml` and validates.
- [ ] `git check-ignore manifest.yaml` matches; `manifest.example.yaml` does not.
- [ ] `git log --oneline -- manifest.yaml` still shows history — the file was untracked, not
      deleted.
- [ ] `grep -rn '"textual"' provision/ cockpit/` — empty.

---

## Phase 2: Everything that reads a manifest reads the right one

**Files**: `smoke/verify_rendered_units.py`, `smoke/verify_build_pin.py`,
`smoke/verify_screens.py`, `provision/cli.py`, `cockpit/app.py`, `bin/check-updates`.

1. **Smoke tests load `manifest.example.yaml`** — §0b. They already use `hosts/example.yaml`
   and `models.example.yaml`; this makes the fixture set consistent and stops an operator's
   tuned flags from changing what the suite validates.
2. **Runtime keeps loading `manifest.yaml`** (`cli.py`, `app.py`, `bin/check-updates`) — that
   is the point of the split.
3. **A missing `manifest.yaml` must fail with the remedy**, not a traceback: say to copy
   `manifest.example.yaml`. This is the first-run path on a fresh clone and the one an
   operator will actually hit.

### Verification
- [ ] Full sweep green with `manifest.yaml` present.
- [ ] **Full sweep green with `manifest.yaml` temporarily moved aside** — the smoke suite must
      no longer depend on a gitignored file. Restore from a copy you made yourself.
- [ ] `bin/provision --dry-run all` with no `manifest.yaml` prints the remedy and exits
      non-zero. `bin/cockpit` likewise.
- [ ] `grep -rn 'manifest.yaml' smoke/` — only the "missing file" test refers to the live one.

---

## Phase 3: Setup creates it

**Files**: `cockpit/screens/settings.py` (first-run wizard), `provision/cli.py`, `README.md`.

1. **First run creates `manifest.yaml` from the template.** The wizard already writes
   `hosts/<hostname>.yaml` when `host_profile is None`; the manifest copy belongs in the same
   moment. Copy the file verbatim — do **not** regenerate it through YAML, or the header block
   and inline comments die (§0e-1).
2. **`bin/provision` on a fresh checkout** does the same or tells the operator to, whichever
   fits `cli.py`'s existing shape. Report which you chose.
3. **README**: the install steps gain the copy, alongside the existing `hosts/` and
   `models.yaml` instructions, so all three read alike.

### Verification
- [ ] From a scratch clone with no `manifest.yaml`: first run produces one, byte-identical to
      the template including comments.
- [ ] Running setup twice does not overwrite an edited `manifest.yaml`.
- [ ] `verify_screens.py` green — the wizard path is covered there.

---

## Phase 4: Canon, and the contradiction this closes

**Files**: `AGENTS.md`, `cockpit/DESIGN.md`, `README.md`, `plans/06`, `plans/07`.

1. **`AGENTS.md`**: "Backend recipes are repo-level, not host-level" was true when there was
   one manifest. Now the *template* is repo-level and the *copy* is the host's. Say that, and
   say that editing recipes in your own manifest is expected — it is what the TUI editor is
   for. **This is the canon change parked four times; close it explicitly rather than letting
   the file quietly stop matching the code.**
2. **`AGENTS.md` §Repository Discipline 4** lists the gitignored deployment files; add
   `manifest.yaml`.
3. **`DESIGN.md` §5** lists `manifest.yaml` among the declarative files a write confirms
   against — still true, and now for the same reason as the others.
4. **`plans/06` and `plans/07`** each carry an "open, unanswered" item about this. Mark them
   resolved, pointing here.

---

## Open, carried forward

1. **`settings.py`'s `_host_profile_path()` keys on the `hostname:` field** while `app.py`
   loads `hosts/<host_name>.yaml`. If they disagree a save lands in a different file. Dormant
   on `haupe-server`; five call sites.
2. **Only `BuildsScreen` re-reads `app.host_profile`**, so other tabs need a restart to see a
   GPU binding.
3. **Legacy `<sha>`-named build directories** are listed and marked, never migrated.
