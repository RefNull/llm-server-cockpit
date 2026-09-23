# Plan: a third Deploy table for models launched directly, not via llama-swap

Baseline: HEAD `e584c34`. Source: operator, 2026-09-23 — clarifying what "Resident" was
supposed to mean: *"the first table is models that we load via directly calling for
example llama.cpp or ollama or whatever (this is not built)."*

`plans/09`'s "Resident" table was renamed to **Pinned** (`e584c34`) precisely to free this
word: the table this plan describes is what "Resident" should mean going forward — a model
process the cockpit itself starts and supervises, with **no llama-swap in front of it at
all**. Both existing Deploy tables (Pinned, Swappable) stay exactly as they are; this is a
third one, fed by a model that was never in `models.yaml`.

**This plan is scoping only.** Nothing here is implemented. Phase 0's open decision must be
confirmed with the operator before Phase 1 starts.

---

## Phase 0: Discovery

### 0a. The closest thing that already exists is the Scripts tab — and it is close

`provision/steps/scripts.py` + `systemd/python-script.service.tmpl` already do most of what
"launch a binary directly, supervise it with systemd" needs:

- One generated unit per entry (`cockpit-script-<id>.service`), `Restart=`, `RestartSec=`,
  `WorkingDirectory=` (`scripts.py:_build_exec_start`, wraps the argv in `sh -c` +
  `shlex.quote` — one quoting pass, not systemd's own `ExecStart=` word-splitting).
- Auto-install-on-start, in-table `[ Logs ]` via `journalctl` (`cockpit/screens/scripts.py`,
  landed in `28e8d46`).
- Both `python` and `bash` script types (`provision/schema.py::validate_scripts_dict`).

What it does **not** have, and what a "launch llama-server/ollama directly" entry needs on
top:

1. **GPU/backend binding**, validated against `hosts/<hostname>.yaml` the way
   `EditModelModal`'s llama-cpp fields already do (`cockpit/screens/deploy.py:_gpu_options`,
   `_backend_options_for_gpu`) — a script today is not GPU-aware at all.
2. **A model-file picker** over `models_dir` (`cockpit/screens/downloads.py::list_model_files`,
   already used by `EditModelModal`), instead of a free-text `path`.
3. **A fixed, explicit port**, and a check that it doesn't collide with anything else the
   cockpit manages. Nothing today checks this even for scripts —
   `scripts.example.yaml`'s `--port 9099` is entirely the operator's own bookkeeping,
   unchecked against `hosts/<hostname>.yaml`'s `network.gateway.port` (llama-swap's own
   port) or any other script's `--port`. A directly-launched model raises the stakes on the
   same gap, because a silent port collision here means two processes fighting over one
   socket rather than a script that just doesn't start.

### 0b. Why this can't just be "add `engine: unmanaged` to models.yaml and skip the swap step"

Every existing `models.yaml` entry, regardless of engine, is rendered into llama-swap's
`config.yaml` and started **by llama-swap** (`provision/steps/swap.py::_generate_config`,
`_build_model_entry`) — `${PORT}` is llama-swap's own macro, assigned per model at load
time, never a fixed port the operator picks. A model with no llama-swap in front of it has
no `${PORT}` to receive; it needs a real, fixed port in its own launch command, and nothing
proxies or health-gates it the way llama-swap does for everything else. So this genuinely is
a second supervision path, not a third `engine:` value on the existing one — folding it into
`models.yaml`/`_build_model_entry` would teach that function to sometimes mean "written into
llama-swap's config" and sometimes "not," which is exactly the kind of ambiguity
`_is_pinned`'s docstring already warns against for the group/ttl axis.

### 0c. The VRAM-collision risk this introduces

AGENTS.md is explicit: *"the cockpit deploys and supervises; it does not measure host
utilization... reading GPU telemetry wakes the device."* llama-swap's own group/ttl
accounting only sees what it manages — a directly-launched model on the same GPU is
invisible to it. Two independent supervisors can schedule two models onto one GPU with
neither aware of the other, and this repo cannot poll VRAM to catch it (that rule is
load-bearing, not a gap to route around here). The only honest mitigation available is a
**declared, static reservation** — the operator says "this GPU is spoken for by a direct
launch," and the Deploy tab's `bind.gpu` picker for *both* other tables refuses that GPU
(or warns) while the reservation stands. This needs a decision, not an assumption (Decision
3 below).

### 0d. Allowed APIs / patterns to copy, not reinvent

- Unit generation: `provision/steps/scripts.py::install_unit`, `_build_exec_start`,
  `systemd/python-script.service.tmpl` — copy the pattern, don't duplicate the module if
  Decision 1 lands on "extend scripts," don't rewrite it if it lands on "new module."
  `scripts.py:_UNIT_DIR`, `unit_name()` — naming convention to extend
  (`cockpit-direct-<id>.service` or fold into `cockpit-script-<id>.service`, per Decision 1).
- GPU/backend selection: `EditModelModal._gpu_options`, `_backend_options_for_gpu`,
  `_refresh_backend_options` (`cockpit/screens/deploy.py`).
- Model file picker: `cockpit.screens.downloads.list_model_files`.
- Schema pattern: `provision/schema.py::validate_scripts_dict` (no cross-file GPU/backend
  check today) vs `validate_models_dict` (has one, via `host_profile`/`manifest` — a direct
  entry needs the latter's shape, not the former's).
- Table/tab pattern: `cockpit/DESIGN.md` Archetype B (Table-Driven Inventory) — this is
  exactly that archetype a third time, not a new one.

---

## Decisions needed before Phase 1

**Decision 1 — reuse Scripts, or build a parallel mechanism?** Recommended: **extend
`scripts.yaml`/`provision/steps/scripts.py`** with optional `bind: {gpu, backend}` and a
required `port` when `bind` is present, rather than a fourth storage file and a near-copy of
unit-generation code (`contract.md` "Cost of existing" — reuse before adding). The Deploy
tab's third table would then be a *filtered view* of `scripts.yaml` (entries with `bind`
set), the same way Pinned/Swappable are two filtered views of `models.yaml` — not a new
data file. **Needs operator confirmation**: this measurably changes what "the Scripts tab"
is for (general ad-hoc process supervision) by teaching it about GPUs, which a change of
that shape to `AGENTS.md`'s stated scope deserves sign-off on.

**Decision 2 — port allocation.** Recommended: operator types a fixed port per entry (like
`scripts.example.yaml` does today), validated at save time against: every other direct-entry
port, `hosts/<hostname>.yaml`'s `network.gateway.port`, and (new) every `--port`/`-p` token
already appearing in any `scripts.yaml` entry's `args` — closing the pre-existing gap named
in §0a.3 as part of this work, since the validator has to exist either way once a direct
entry is in scope. No auto-assignment: a fixed, operator-visible port is the point (a client
calls it directly, with no proxy to discover it through).

**Decision 3 — GPU reservation enforcement.** Recommended: a direct entry's `bind.gpu`
removes that GPU from the options offered by `EditModelModal`'s own `_gpu_options()` (Deploy
tab, for both Pinned/Swappable), with a form error if an operator forces it by hand-editing
YAML and then tries to save from the UI. Not a hard block on multiple direct entries sharing
one GPU — that's a legitimate topology (two small models, one big GPU) and not this
mechanism's business to police; the reservation is against llama-swap's own scheduling,
which has no per-GPU concept at all today and would happily co-load anything.

---

## Phase 1: Schema + validation (blocked on Decision 1/2)

Extend `provision/schema.py::validate_scripts_dict` (or a new `validate_direct_models_dict`
if Decision 1 goes the other way) to accept optional `bind: {gpu, backend}` + required `port`
when `bind` is present, cross-checked against `host_profile`/`manifest` the way
`validate_models_dict` already does. Add the port-collision check from Decision 2. New smoke
fixture + `smoke/verify_*` script proving: a colliding port is rejected, a GPU already bound
elsewhere is rejected, a valid entry round-trips.

## Phase 2: Unit generation + start/stop

Generalize `provision/steps/scripts.py::_build_exec_start`/`install_unit` (or the new
module) to build the launch command from `bind`-resolved binary path (same
`prefix_root/<backend>/current/bin/llama-server` shape as `_build_model_entry`) + model file
+ fixed `--port`. Reuse `cockpit/screens/scripts.py`'s Start/Stop/Logs table actions rather
than re-implementing them.

## Phase 3: Deploy tab — third table

`DeployScreen` gains a "Direct" (or similar — bikeshed at execution time) section, same
`SingleClickDataTable` pattern as Pinned/Swappable, sourced from the entries Decision 1
selects. `EditModelModal`'s `_gpu_options()` filters out any GPU reserved by a direct entry
(Decision 3). Update the subtitle static added in `182a23d` once this exists — it currently
says the direct path "isn't built yet."

## Phase 4: Verification

Full smoke sweep; a real host test starting one direct-launch model and confirming its port
answers directly (no llama-swap route for it) while `Apply & Restart llama-swap` on the
other two tables is unaffected.
