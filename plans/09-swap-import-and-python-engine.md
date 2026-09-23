# Plan: Deploy tab — real config import, a Python engine, and a form that works

**Status: Phases 1–4 done** (`c083534`, `9f28073`, `0a76e39`). Phase 5 item 6 (live host) open.

Baseline: HEAD `e62b2e8`. Source: operator, 2026-09-23, before first live llama-swap tests.
Reference config: the operator's own llama-swap `config.yaml` (4 × llama-server, 2 × Python
venv scripts, one `always-on` group). It is private and not in the repo — Phase 1 turns its
*shapes* into a fixture with genericized paths.

The operator's five points (a–e) reduce to **three defects and two layout fixes**. Two of
the defects were not in the report and are worse than what was reported (§0a, §0b).

Each phase is self-contained: hand an executor the phase plus Phase 0.

**Order is strict.** Phase 1 (schema + generator + parser) is the contract every UI phase
builds on. Phases 2–4 all edit `cockpit/screens/deploy.py`, so they run one after another,
never in parallel.

---

## Phase 0: Discovery (DONE — cite it, do not redo)

Read from source at `e62b2e8`.

### 0a. Adding an unmanaged model from the form is impossible (the real reason (e) "does not work")

`EditModelModal._build_model_from_form` (`cockpit/screens/deploy.py:489`):

```python
model_id = self.editing_id or self._derived_id()
if not model_id:
    return None, "could not derive an id — pick a model file"
```

`_derived_id` (`:293`) reads `#f-quant-file`, which `_toggle_engine_fields` **hides** for
`engine: unmanaged`. So a new unmanaged model always fails with "pick a model file" for a
field the operator cannot see. The id field was removed in an earlier pass; the removal only
ever thought about llama-cpp.

### 0b. A two-token example line becomes one argv token — silently broken commands

- `ExamplesModal.ARG_EXAMPLES` snippets are `"--ctx-size 262144"`, `"--embedding --pooling cls"`
  (`deploy.py:90-96`); `_insert_example` inserts each as **one line** (`:528-533`).
- `_build_model_from_form` stores each non-blank line as one list element (`:511`).
- `swap._build_model_entry` `shlex.quote`s each element (`provision/steps/swap.py:200`) →
  `'--ctx-size 262144'` reaches llama-server as a single unknown argument.

Any operator who types a flag and its value on one line — the natural way — gets a model
that fails at load. The label ("one flag or value per line") is the only thing standing
between them and it, and that label is the text (b) reports as clipped.

### 0c. Import is a verbatim copy, not a parse

`swap.parse_config_for_import` (`swap.py:217-255`) turns **every** entry into
`engine: unmanaged` with `cmd` copied verbatim. Against the operator's config that means:

| config content | what import does today |
|---|---|
| `/home/…/builds/vulkan/current/bin/llama-server` | kept verbatim — wrong prefix on this host, never rewritten |
| `--model /home/…/gguf/X.gguf` | kept verbatim — not mapped to `quant_file`, `models_dir` ignored |
| `# --mmproj …` comment lines inside `cmd: \|` | kept (llama-swap strips them itself — harmless, but noise) |
| per-model `checkEndpoint: /health` | **dropped** — no field exists in the schema |
| per-model `healthCheckTimeout: 120/180` | dropped — correct by accident (§0e), but silently |
| Python `…/.venv/bin/python script.py --port ${PORT}` | unmanaged; no structure |
| `ttl`, `env`, top-level `groups.*.members` | read correctly |

The review table (`ImportModelsModal`, `deploy.py:581-754`) shows id/engine/cmd and a tick
box — no way to inspect or edit an entry before it is written.

### 0d. `EditModelModal` writes to disk on Save — no staging mode

`on_button_pressed` → `schema.validate_models_dict` → `ConfirmModal` → `models.yaml.write_text`
→ `dismiss(True)` (`deploy.py:549-578`). The entry being edited is looked up from
`self.models` by `editing_id` (`:281`). Neither supports editing a not-yet-saved candidate,
which is what import review (a) needs.

### 0e. Upstream llama-swap per-model keys (verified)

`docs/config.example.yaml` on llama-swap `main`: per-model keys include `cmd`, `env`, `ttl`,
`checkEndpoint` (default `/health`, expects HTTP 200), `proxy`, `aliases`, `name`,
`description`, `unlisted`, `useModelName`, `cmdStop`, `concurrencyLimit`, … .
**`healthCheckTimeout` is global only** — not a per-model key. `${PORT}` and `${MODEL_ID}`
are documented macros. Comments inside `cmd: |` "will be parsed out".

**Checked against the `v255` source** (the pin, `manifest.example.yaml:14`), 2026-09-23:
- Top-level `groups:` is the *legacy* form, still accepted and normalized into
  `routing.router` (`internal/config/load.go:189-197`; error only if both forms are used).
  Group emission stays as-is; the parser reads top-level `groups:` and, if present,
  `routing.router.settings.groups` too.
- Per-model `healthCheckTimeout` parses but is **overwritten by the global** for every model
  (`internal/config/load.go:130`). Dropping it on import loses nothing.
- `cmd` is **not run through a shell**: `SanitizeCommand` (`internal/config/commands.go:11-45`)
  drops lines whose trimmed text starts with `#`, turns a trailing `\` into a space, then
  `shlex.Posix.Split`s. The import parser must mirror exactly that. (`swap.py:193`'s
  "execs through a shell" comment is wrong in mechanism, right in effect — `shlex.quote`
  output is valid POSIX-shlex input. Not in scope to rewrite.)

### 0f. Form geometry (b)

- `#f-args, #f-cmd, #f-env { height: 5 }` and then `#f-env { min-height: 18 }`
  (`deploy.py:220-222, 244-246`) — env gets 18 rows, args gets 5. Inverted from real use:
  the operator's biggest model has 19 arg tokens and 1 env line.
- The args label is a `Label` in a `1fr` column; at 80 cols its ~110-char text clips.
- `smoke/verify_screens.py:378-458` (`_assert_edit_model_modal`) checks dialog containment,
  horizontal field containment, scroll reachability of `#f-group`/`#f-env`, and that the
  field-group Verticals do not clip. **Any new field id must be added to its `field_ids`.**

### 0g. Other engine consumers

`provision/schema.py:151-175` (validator), `provision/steps/swap.py:183-214`
(`_build_model_entry`), `provision/steps/hf.py:89-91,133` (treats any non-`llama-cpp` as
"unmanaged" for download purposes — a new engine needs **no** change there),
`smoke/verify_rendered_units.py:286-309` (renders `models.example.yaml`).
`DeployScreen._populate_table` (`deploy.py:910-914`) blanks GPU/Backend for non-llama-cpp — fine.

### 0h. The Scripts tab is not the answer to (e)

`scripts.yaml` + `systemd/python-script.service.tmpl` run a script **permanently** as its own
systemd unit on a fixed port. The operator's Python servers are **llama-swap routes**: started
on demand, on `${PORT}`, health-checked, swapped or grouped. Different lifecycle, different
owner. Do not route (e) through `scripts.yaml`; do say so in the form's help line, because
the two will be confused.

### 0i. Allowed APIs (use these, invent nothing)

- Textual (pinned in `requirements.txt`): `DataTable`, `Select`, `Input`, `TextArea`,
  `ModalScreen[T]`, `push_screen_wait`. **Correction found in Phase 4:** an unset `Select`
  holds `Select.NULL` (truthy); `Select.BLANK` is `Widget.BLANK` (`False`) and never equals
  a Select's value. Compare against `Select.NULL`. Fixed in `deploy.py` (`0a76e39`); still
  wrong in `builds.py` and `settings.py`.
- Repo: `SingleClickDataTable`, `TableAction`, `add_action_column`, `action_cells`,
  `selection_marker` (`cockpit/widgets.py`); `ConfirmModal`, `InfoModal`.
- `schema.validate_models_dict(data, host_profile, manifest, source=)` — the only validator.
- `shlex.split` / `shlex.join` / `shlex.quote` — stdlib.

---

## Decisions (made here; do not reopen in execution)

1. **Third engine `python`**, chosen with the existing **engine `Select`**, not a third
   button. The Add buttons pick *residency* (ttl seed); engine is the orthogonal axis —
   the operator's Python servers are resident, so a "Python" button would need to exist
   under both tables. Select labels: `llama-server (GGUF)`, `Python script`,
   `Custom command`; values stay `llama-cpp`, `python`, `unmanaged`.
2. **`python` schema:** required `id`, `engine`, `python` (interpreter path), `script`;
   optional `args` (list of strings), `check_endpoint`, `env`, `ttl`, `group`.
   No `working_dir` — llama-swap has no per-model cwd and the operator's scripts do not need
   one. Rendered `cmd` = `shlex.join([python, script, *args])`; `--port ${PORT}` lives in
   `args` and is **prefilled** by the form, not hardcoded (not every script takes `--port`).
3. **`check_endpoint`** is a new optional key on **all** engines, emitted as
   `checkEndpoint`. Default stays upstream's (omit when unset).
4. **Args are parsed per line with `shlex.split`**, not one line = one token. Storage in
   `models.yaml` stays a flat token list (no schema change). On load, the form re-pairs
   `--flag value` onto one line via `shlex.join` for readability. Round-trip is lossless.
5. **An explicit `id` Input** on the form. llama-cpp auto-fills it from the chosen file
   (current `_derived_id` logic) until the operator types in it; python/unmanaged require it.
   Disabled when editing an existing model (renames are out of scope).
6. **Import is a real parser, then a staged review.** Nothing reaches `models.yaml` until the
   operator has seen each entry's status and could have edited it.
7. **Per-model `healthCheckTimeout` is dropped on import with a visible note**, not carried —
   v255 overwrites it with the global for every model (§0e).

---

## Phase 1: Schema, generator, parser (no UI)

Files: `provision/schema.py`, `provision/steps/swap.py`, `models.example.yaml`,
new fixture `smoke/fixtures/llama-swap-import.yaml`, new `smoke/verify_swap_import.py`.

**Implement**

1. `schema.validate_models_dict`: add `elif engine == "python": _require(m, ["python", "script"], …)`,
   `args` must be a list of str; `check_endpoint`, if present, a str starting with `/`.
   Update the unknown-engine message. Copy the shape of the existing `unmanaged` branch.
2. `swap._build_model_entry`: `python` branch per Decision 2; emit `checkEndpoint` for any
   engine when `check_endpoint` is set. Keep the existing `shlex.quote` rationale comment
   applicable — `${PORT}` survives quoting (see `swap.py:193-199`).
3. `swap.parse_config_for_import(yaml_text, host_profile) -> list[dict]` — each result is
   `{"model": <models.yaml entry>, "notes": [str, …], "status": "ok" | "review"}`.
   Per entry:
   - Strip lines whose first non-blank char is `#` from `cmd` (upstream does the same, §0e);
     record "N comment line(s) dropped" in notes — include the stripped text if it names
     `--mmproj` (the operator's comments carry exactly that intent).
   - `shlex.split` the joined cmd. Classify on `argv[0]`:
     - basename `llama-server` → **llama-cpp**. `backend` = the path segment before
       `/current/bin/llama-server` if it matches; else status `review`. `gpu` = the single
       host GPU whose `backends` contain that backend; zero or several → `review`.
       `--model P` → `quant_file = basename(P)`; if `dirname(P) != models_dir`, note
       "relocated from <dir>" (this is (a)'s folder mismatch, resolved by construction since
       the generator always prefixes `models_dir`). `--mmproj P` → `mmproj_file` likewise.
       `--port ${PORT}` dropped. Everything else → `llama_server_args` tokens.
       `repo_id: "local"` (same convention as the form, `deploy.py:506`). If the backend dir
       differs from `prefix_root`, note "binary path rewritten to prefix_root".
     - basename matches `python`, `python3`, `python3.N` → **python**: `python = argv[0]`,
       `script = argv[1]`, `args = argv[2:]`. Paths kept as-is and never stat'd — the
       cockpit may run off-host. Status `ok`; the operator edits paths in review if needed.
     - anything else → **unmanaged**, cmd (comments stripped) kept.
   - Carry `env`, `ttl` (`in`, not truthiness — keep the existing comment), `checkEndpoint`
     → `check_endpoint`, top-level `groups.*.members` → `group`.
   - Per-model `healthCheckTimeout` → note "healthCheckTimeout N dropped — llama-swap only
     supports it globally (Settings > gateway)". Any other unknown per-model key → note
     "unsupported key K dropped".
   - Keep raising `ValueError` for invalid YAML / missing `models` mapping.
4. Fixture: the operator's config's **structure**, paths genericized to `/srv/…` and
   `/opt/…`, including the comment lines, both Python entries, per-model
   `healthCheckTimeout`, and the commented-out model block (YAML comments — must be ignored).
5. `smoke/verify_swap_import.py`: parse the fixture against `hosts/example.yaml`; assert
   engine per id (4 llama-cpp, 2 python), `quant_file` basenames, `backend` values,
   `check_endpoint == "/health"` on both Python entries, notes present for dropped
   `healthCheckTimeout`, then `validate_models_dict` the `ok` entries, then
   `_generate_config` and re-parse: every llama-cpp cmd starts with `prefix_root`, contains
   `models_dir`, and keeps `${PORT}`. Add it to AGENTS.md's Key Commands.
6. `models.example.yaml`: keep the docker `speaches-stt-tts` entry (it is the unmanaged
   example); **add** one `python` entry mirroring the ASR shape with generic paths, so
   `verify_rendered_units.py` exercises the new branch.

**Verify**
- `.venv/bin/python smoke/verify_swap_import.py`
- `.venv/bin/python smoke/verify_rendered_units.py`
- AGENTS.md schema one-liner.

**Anti-patterns**
- Do not add `healthCheckTimeout` per model to the schema or generator.
- Do not stat paths during parsing (off-host operation).
- Do not change group emission (§0e gap).

---

## Phase 2: Model form — id, python engine, args parsing, geometry (b, e, 0a, 0b)

File: `cockpit/screens/deploy.py` (`EditModelModal`), `smoke/verify_screens.py`.

**Implement**

1. `_ENGINE_OPTIONS` → three entries with the labels from Decision 1.
2. Add `#f-id` Input at the top of the left column (Decision 5). Replace the
   `self.editing_id or self._derived_id()` line with the Input's value; on `#f-quant-file`
   change, fill `#f-id` from `_derived_id()` only if the operator has not edited it.
   Validate id against `^[a-zA-Z0-9_.\-]+$` (the same class `_derived_id` sanitizes to).
3. New `#f-python-group` (Vertical, `height: auto` — same c.3 rule as its siblings, see the
   CSS comment at `:247-253`): `python` Input, `script` Input, one help line:
   "Runs on demand under llama-swap on ${PORT}. For an always-on systemd service use the
   Scripts tab." `args` for python reuses the args TextArea (same slot, same parser); for a
   new python model prefill it with `--port ${PORT}`.
4. `#f-check-endpoint` Input (optional, placeholder `/health`) visible for all engines.
5. `_toggle_engine_fields`: three-way. llama-cpp: file/gpu/backend + args. python: python
   group + args. unmanaged: cmd.
6. Args parsing per Decision 4 — `shlex.split` per line inside `_build_model_from_form`; on
   `ValueError` (unbalanced quote) set the form error naming the line. On load, pair
   tokens for display. Label text becomes short enough not to clip:
   `llama-server flags — one flag (and its value) per line` — and for python:
   `script arguments — one per line`. Make the label wrap (`width: 1fr` on the right
   column's Labels) rather than rely on length alone.
7. Geometry: `#f-args { height: 1fr; min-height: 12 }`, `#f-env { height: 5 }`, drop the
   `min-height: 18` on env. Rewrite the CSS comment at `:209-215` — its reasoning (env can
   be 15–20 lines) is contradicted by real configs; say so in one line.
8. `smoke/verify_screens.py::_assert_edit_model_modal`: add `#f-id`, `#f-check-endpoint`,
   the python inputs and `#f-python-group` to `field_ids` / the group list; run the check
   once per engine value (set the Select, pause, assert).

**Verify**
- `python3 -m py_compile cockpit/screens/deploy.py`
- `python3 -m cockpit.widgets` (spacing tokens — no bare margins in the new CSS)
- `.venv/bin/python smoke/verify_screens.py` at every size it already runs (80×24 floor).
- Manual: add an unmanaged model with no file selected → saves. Add llama-cpp with the line
  `--ctx-size 262144` → Preview YAML shows `--ctx-size 262144` as two tokens.

**Anti-patterns**
- No per-button CSS (DESIGN.md §9). No hex colours, no bare spacing ints (DESIGN.md §1).
- Do not change `models.yaml` storage format for args.

---

## Phase 3: Examples as tables (c)

File: `cockpit/screens/deploy.py` (`ExamplesModal`).

**Implement**

1. Replace the Button+Static rows with one `SingleClickDataTable`, two columns:
   `Purpose` | `Snippet`. Row select dismisses with the snippet. All cells `rich.text.Text`
   (DESIGN.md §4.6 — the JSON snippet has braces/brackets).
2. Intro text → one line: `llama-server CLI flags (with --). Not llama.cpp preset keys, not a full command.`
3. `ARG_EXAMPLES` becomes `(purpose, snippet)` — no description column. Full set from the
   operator's config (every flag it uses, `--port`/`--model`/`--mmproj` excluded — the form
   owns those):

   | Purpose | Snippet |
   |---|---|
   | Context window | `--ctx-size 262144` |
   | KV cache K type | `--cache-type-k q4_0` |
   | KV cache V type | `--cache-type-v q4_0` |
   | Flash attention | `--flash-attn` |
   | Jinja chat template | `--jinja` |
   | Chat template kwargs | `--chat-template-kwargs '{"reasoning_effort":"medium","preserve_thinking":true}'` |
   | Reasoning budget | `--reasoning-budget 5000` |
   | Speculative decoding | `--spec-type draft-mtp` |
   | Max draft tokens | `--spec-draft-n-max 2` |
   | Auto-fit off | `--fit off` |
   | Context checkpoints | `--ctx-checkpoints 0` |
   | Embedding | `--embedding` |
   | Pooling (embedding) | `--pooling cls` |
   | Reranking | `--reranking` |
   | Pooling (reranker) | `--pooling rank` |

   `ENV_EXAMPLES`: `Intel Vulkan ICD` | `VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/intel_icd.x86_64.json`;
   `Pin CUDA device` | `CUDA_VISIBLE_DEVICES=0`.
   Update the class docstring's provenance line to cite the operator config + `models.example.yaml`.
4. Insertion stays append-as-new-line; Phase 2's `shlex.split` makes one-line
   `--flag value` snippets correct (§0b). Add a python-args variant only if trivially
   shared — otherwise the Examples button is hidden for `python`. Prefer hiding.

**Verify**
- `py_compile`; `smoke/verify_screens.py`; manual: insert "Chat template kwargs", Preview
  YAML — the JSON is one argv token, single-quoted.

**Anti-patterns**
- Do not reintroduce a per-row description; purpose + snippet is the whole row.

---

## Phase 4: Import review — staged, per-entry edit (a, d)

File: `cockpit/screens/deploy.py` (`EditModelModal`, `ImportModelsModal`).

**Implement**

1. `EditModelModal`: add `staged: dict | None = None`. When set, the form seeds from that
   dict instead of looking up `editing_id` in `self.models`, the id stays editable, and Save
   **validates then dismisses with the model dict** — no ConfirmModal, no write. Change the
   type to `ModalScreen[dict | None]`; non-staged Save dismisses with the saved dict (callers
   only test truthiness — `deploy.py:965, 981` — so they keep working).
   Validation in staged mode: `validate_models_dict` of `{"models": [model]}` only — cross-
   entry collisions are checked at import time.
2. For a staged llama-cpp entry whose `quant_file`/`mmproj_file` is not in `models_dir`,
   show a form-error line "X not found in models_dir — download it or pick another file"
   and allow Save anyway (the schema does not require the file; the operator may download
   next). Same rule as today's on_mount comment (`:393-395`) — just surface it.
3. `ImportModelsModal`: candidates are Phase 1's `{model, notes, status}`. Columns:
   tick | ID | Engine | Status | Notes (first note, truncated). Add
   `TableAction("edit", "Edit")` → opens staged `EditModelModal`; the result replaces the
   candidate and sets status `ok`. Widen the dialog from `width: 85` to fit the column
   budget per DESIGN.md §4 (≤115 incl. action cells) — use `90%` like the other modals.
4. Import button: refuse while any **ticked** row is `review` ("edit N entr(y/ies) first");
   id collisions with `models.yaml` keep today's message. Ticked-by-default: all `ok` rows.
5. `on_mount` discovery of `state_dir/llama-swap/config.yaml` passes `host_profile` to the
   new parser signature. Our own generated config re-imports as llama-cpp/python — confirm.

**Verify**
- `py_compile`; `smoke/verify_screens.py`; `smoke/verify_swap_import.py`.
- Manual against the operator's real config (paste it): 4 llama-cpp rows (`ok` if the host
  has exactly one GPU per backend), 2 python rows `ok` with `/health`, notes show dropped
  comment lines and healthCheckTimeout. Edit one, change a path, Import → `models.yaml`
  diff shows the edit. Preview YAML: all binaries under `prefix_root`, all weights under
  `models_dir`.

**Anti-patterns**
- Never write `models.yaml` from staged mode.
- Do not auto-resolve an ambiguous GPU — mark `review`.

---

## Phase 5: Verification

1. All commands in AGENTS.md Key Commands, plus `smoke/verify_swap_import.py`.
2. `grep -n '_derived_id()' cockpit/screens/deploy.py` — only used to *seed* `#f-id`.
3. `grep -n 'min-height: 18' cockpit/screens/deploy.py` — gone.
4. `grep -rn '"unmanaged", "cmd": entry' provision/` — gone (old verbatim import).
5. `git diff --stat` touches no `hosts/*.yaml`, `models.yaml`, `manifest.yaml`.
6. Live-host dry run: Apply on the operator's server with the imported catalog, one
   llama-cpp + one python model loaded via the Start action, `/running` shows both.
