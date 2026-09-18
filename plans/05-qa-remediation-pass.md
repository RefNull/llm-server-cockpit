# Plan: QA Remediation Pass — build pipeline, WOL regression, add-model form

Baseline: HEAD `af1985a` (`feat(deploy): llama-swap running status, and Start/Stop on Resident`).

> **Superseded in part.** This plan describes `af1985a`..`63e1c45`. Its Phase 2 UI
> decisions — the Version column, the floating version/source notes, the What/Where/How
> detail modal, and the Retained Builds / Build History split — were reworked by
> `plans/06-backends-rework.md` after operator QA on 2026-09-18 found the tab had
> "ballooned". Its `build.py` line citations no longer match the file. The Phase 0
> findings and the WOL, add-model-form and Dashboard work all still stand.
Source: operator QA review of the running build on `haupe-server`, 2026-09-17, plus the
`wol-fix-steps` repair transcript.

Five defect areas, one through-line: **the TUI reports intent, not reality.** The Installs
table shows a manifest pin and calls it a version. The Update button builds that same pin and
calls it an update. The WOL step enables a unit and never starts it. The add-model form lays
out a field group with no height and calls the result a form. In every case the widget is
telling the truth about what the code was asked to do and lying about what the host now is.

Two of the five QA hypotheses were false premises. They are corrected in Phase 0 §0a before
any phase acts on them. Read Phase 0 first.

Each phase is self-contained and can run as its own `Agent` call in a fresh context. Give the
phase plus Phase 0 — that is the grounding it needs.

**Dependency order**: Phase 1 → Phase 2 (Build needs the contained log first). Phases 3, 4, 5,
6 are independent of everything and of each other. Phase 4 is a live hardware regression and
should go first in wall-clock terms even though nothing blocks on it.

---

## Phase 0: Documentation Discovery (DONE — do not redo, cite it)

Three parallel discovery agents plus orchestrator spot-verification, 2026-09-17. Every claim
below was read from source at `af1985a`, executed live, or fetched from upstream. Negative
claims ("nothing does X") were each established by an exhaustive grep over the tree excluding
`.venv`/`.git`/`__pycache__`, not by absence of memory.

### 0a. Two QA hypotheses are false premises — correct them before acting

**FALSE: "the software finds the `/opt/llama.cpp` files but installs new builds elsewhere."**

Nothing in this toolkit has ever looked at `/opt/llama.cpp`. A repo-wide grep for `/opt/llama`
returns zero hits. The only `build-<backend>` string in the codebase is a *cmake scratch
directory inside the source checkout* — `provision/steps/build.py:81`:

```python
builddir = checkout_dir / f"build-{backend}"
```

What QA saw as "both builds were detected" is `_refresh_backends_table`
(`cockpit/screens/builds.py:452-474`) synthesising **one row per backend** from
`host_profile["gpus"][*]["backends"]` — `cuda` and `vulkan` — joined to a single
`manifest.yaml` value. It is not a filesystem scan. That the host also happens to hold
`/opt/llama.cpp/build-cuda` and `/build-vulkan` is a naming coincidence.

The real install root on the QA machine is `hosts/haupe-server.yaml:23`,
`prefix_root: /opt/llm-server/builds`. Layout is `<prefix_root>/<backend>/<sha>/` plus a
`current` symlink — see §0b. An operator watching `/opt/llama.cpp` would correctly observe
nothing changing, forever.

**FALSE: "the table and the backend never get updated as a function."**

The table *does* refresh — `builds.py:532`, in the `finally` of `_run_build`. The refresh is a
no-op because **the Update action cannot change anything it could refresh from.**
`build_step.run` reads `provision/steps/build.py:240`:

```python
ref = manifest["llama_cpp"]["ref"]
```

It builds the **already-pinned** SHA. Nothing anywhere bumps that pin — `manifest.yaml` is
read-only to this codebase. So after a successful multi-minute build:

- "Pinned ref" is the same string it was (it is `self.manifest`, loaded once at app start,
  `builds.py:346-362`, never re-read).
- "update available (latest …)" is still on screen, because `_refresh_backends_table` renders
  from `self._llama_cpp_check`, a stale in-memory dict from the *previous* network call.
- A second press hits `build.py:255-256` `_prefix_ready(prefix)` and is **silently skipped**.

This is not a refresh bug. `manifest.yaml`'s own header and `README.md:165` both state that
bumping the pin is the only sanctioned update path and that "there is no 'install latest' path
anywhere in this repo". The button labelled **Update** contradicts the repository's own
contract. Phase 2 resolves that contradiction; do not paper over it with a table refresh.

### 0b. Build pipeline ground truth

| Stage | Value | Citation |
|---|---|---|
| Source checkout | `state_dir/src/llama.cpp` | `build.py:235` |
| cmake build dir | `<checkout>/build-<backend>` | `build.py:81` |
| Staging dir | **none — does not exist** | — |
| Install prefix | `prefix_root/<backend>/<ref>` | `build.py:253` |
| Activation | atomic symlink `prefix_root/<backend>/current` | `build.py:296` |
| Consumer | `f"{prefix_root}/{backend}/current/bin/llama-server"` | `swap.py:176` |

`cmake --install` writes **directly into the final versioned prefix** (`build.py:111-116`) —
no staging, no `DESTDIR`. The symlink flip is gated on a passing real-inference smoke test
(`build.py:286-296`); on build or smoke failure the log says `leaving 'current' symlink
untouched`. `Runner.atomic_symlink` is `provision/common.py:119-137` (create-tmp-then-
`os.replace`). Pruning to `retain_builds` happens only after a successful flip
(`build.py:297` → `_prune_old_builds`, `build.py:119-141`).

Rollback already exists: `build.py:199-206`, driven from `RetainedBuildsModal._run_rollback`
(`builds.py:302`). **Rollback flips the symlink; it does not touch the pin.** Keep those two
operations distinct in Phase 2 or the confusion returns.

Discovery of installed builds: `list_builds()`, `build.py:184-196` — a single directory
listing under `prefix_root/<backend>/`, returning `{"ref", "current", "sane"}`. `sane` is
`_prefix_ready` (`build.py:64-67`), an existence + exec-bit check on `bin/llama-cli` and
`bin/llama-server`, never an execution. Consumers: `builds.py:248` (Retained Builds modal)
and `dashboard.py:280-290`. **The main Installs table does not call it at all.**

Persisted state after a build: one file, `state_dir/build-history.jsonl`, appended by
`_record_history` (`build.py:148-163`, dry-run-gated at `:151-152`) at `:264` `build_failed`,
`:290` `smoke_failed`, `:294` `smoke_pass`. There is no manifest of installed builds — the
filesystem layout *is* the manifest. Nothing ever runs `llama-server --version`; the only
`-version` call in the repo is for llama-swap (`swap.py:34` → `dashboard.py:271`).

### 0c. Why build output "spewed all over the screen"

`Runner.run` — `provision/common.py:52-72` — takes `capture: bool = False`, and when False
passes `stdout=None, stderr=None`, which children **inherit**. Textual owns the same tty, so
the bytes land on top of the rendered TUI. The build runs in-process on a
`@work(thread=True)` worker (`builds.py:509-521`) — not `app.suspend()`, not a detached
subprocess — so nothing is insulating the terminal.

Leaking call sites, in execution order:

1. `build.py:50,55,56` — `git clone` / `fetch` / `checkout`
2. `common.py:149` — `apt-get install -y` in `Runner.apt_install`; **bypasses `Runner.run`
   entirely and has no `capture` option at all**
3. `build.py:112,115,116` — `cmake` configure / `--build -j<nproc>` / `--install`.
   **`cmake --build` is the multi-minute firehose and the primary source of the report.**
4. `common.py:79` — `Runner.shell`; **no `capture` parameter exists on this method**, so the
   `source_script`/sycl path (`build.py:100-108`) is structurally uncapturable today.

Meanwhile `#build-log` (`builds.py:406`) receives **only Python `logging` records**, via
`_BuildLogHandler` (`builds.py:42-65`). Step narration is inside the widget; the actual output
is on the terminal. That split *is* the defect. `#build-log` is also an inline `RichLog` with
`height: 10` (`builds.py:333-338`), `display: none` until `_set_building(True)` — not a modal.

**There is no incremental pipe reader anywhere in this repo.** No `Popen`, no `readline` loop,
no `bufsize` in `cockpit/`. Every existing "show output" path runs to completion and then
displays a finished string.

### 0d. Update-check ground truth

`cockpit/update_check.py`. `check_llama_cpp` (`:75-89`) GETs
`api.github.com/repos/<repo>/commits/master` and compares `latest["sha"] !=
manifest["llama_cpp"]["ref"]` — **manifest pin vs upstream master HEAD. It never looks at what
is installed on the host.** `check_llama_swap` (`:92-106`) compares against
`/releases/latest` `tag_name`. Result shape:
`{"ok", "pinned", "latest", "update_available", "latest_date"}` or `{"ok": False, "error"}`.

Cache: `hosts/upstream.update-cache.yaml` (`update_check.py:25`) — **in the repo, not in
`state_dir`** — TTL 24h (`:24`), written only when both results are `ok` (`:124-125`).
Network timeout 10s (`:23`).

Callers: `builds.py:440` (`on_first_view`, cache-served, once per app run) and `builds.py:501`
(the "Check for Updates" button, `force=True`). `on_refresh_requested` (`builds.py:431-432`)
deliberately never re-checks the network.

**Two dead code paths, confirmed by grep for callers/readers:** `cache_age_seconds()`
(`update_check.py:52-59`) has no callers; `state_dir/update-check-result.json`
(`bin/check-updates:59`) is written and never read by anything.

**The "version notice setting" QA referred to is `update_check.enabled` /
`update_check.on_calendar`** — `settings.py:355,358`; read `:759,765-766`; collected
`:936-937,944`; persisted `:1256`; applied `:1295` → `swap.sync_update_check_timer`
(`swap.py:363-372`). **It governs only the systemd timer that runs `bin/check-updates`.** It
does not control any in-TUI notice. The Installs tab's own check (`builds.py:440`) ignores it
entirely. There is no "show version notice" toggle anywhere in the repo. If QA expected the
setting to silence the in-app banner, that expectation has never been implementable.

### 0e. WOL ground truth — the unit is right, the sequence is wrong

`provision/steps/wol.py:248-310`. Both the CLI (`cli.py:19`) and the TUI
(`settings.py:1119`) call `wol.run` **in-process**; only the `Runner` differs.

Execution order:

1. `:253` read-only `ip link show <iface>`; `sys.exit` on missing iface or MAC mismatch.
2. `:263` `_fix_tlp` (`:126-138`) — **gated on `shutil.which("tlp")`**. Writes
   `/etc/tlp.d/01-wol.conf` and runs `tlp start`.
3. `:264` `_arm_networkmanager` (`:155-178`) — gated on `shutil.which("nmcli")` and
   `systemctl is-active NetworkManager`. Runs `nmcli con mod … wake-on-lan magic` then
   **`nmcli con up <conn>`**.
4. `:266` `_read_wake_flags` (`:74-88`) — **gated on `shutil.which("ethtool")`**; returns
   `None` if absent → `:268` **`sys.exit`**.
5. `:271` `ethtool -s <iface> wol g`, **only if `"g" not in wake_flags`**.
6. `:275-282` write `/etc/systemd/system/wol-<iface>.service` + `systemctl daemon-reload`.
7. `:286-288` `systemctl enable` — **never `enable --now`, never `start`, never `restart`**.

**The unit template is not the bug.** `_unit_content` (`wol.py:102-123`) is byte-for-byte the
shape the operator hand-built in repair step 3 — `Type=oneshot`, `RemainAfterExit=yes`, both
`ExecStart` and `ExecStop` running `/usr/sbin/ethtool -s <iface> wol g`,
`DefaultDependencies=no`, `Before=`/`Conflicts=shutdown.target`. It installs as
`wol-<iface>.service`, a different path from the operator's `wol.service`, so it neither
overwrote nor disabled theirs.

Ruled out by grep, each one explicitly: no `systemctl disable`, no `mask`, no
`ethtool -s … wol d` anywhere in the repo, no toggle-off path, no dry-run-only disarm branch.
The only `ethtool -s` calls are `wol.py:271,118,119`, all `wol g`.

**The regression is three composing defects:**

**(i) PATH-gated guards, sudo-executed mutations.** `shutil.which` at `wol.py:76,128,158`
runs in the *caller's* process. The TUI runs unprivileged (`app.py:200`
`Runner(dry_run=False)`; `widgets.py:683` `Runner(sudo=True)`), and `wol.run` is called
in-process from `settings.py:1119`. `tlp` and `ethtool` live in `/usr/sbin`, which Debian
keeps off a non-root user's PATH — the repo's own unit hardcodes `/usr/sbin/ethtool`
(`wol.py:118`), so it already knows this. On such a host `_fix_tlp` returns at `wol.py:129`
without writing anything, and `WOL_DISABLE=N` — the file the operator identified as **"the
real cause"** — is never created.

**(ii) Link-disturbing mutations run before any verification, and the run can abort between
them.** Order is `_fix_tlp` → `_arm_networkmanager` → *then* the first ethtool read.
`nmcli con up <conn>` (`:178`) is a NetworkManager link-up event — the exact thing this
module's own docstring at `wol.py:127` names as what "clobbers WOL on every
boot/resume/NM event". If `ethtool` is then not on PATH, `:268` `sys.exit`s **after** the NIC
has been cycled and after `tlp start` re-applied TLP policy. Nothing re-arms. The host is left
disturbed, unverified, with no persistence unit, and the error message blames a missing
package rather than naming what was just done to the link.

**(iii) Check-then-act on one sample, plus a unit enabled but never started.** `wake_flags` is
sampled once at `:266`; the in-band re-arm at `:271` is skipped entirely if that one sample
contains `g`. TLP's and NM's re-application of policy is not synchronous with `tlp start` /
`nmcli con up` returning, so a stale `g` suppresses the only re-arm in the run. And the new
unit is only `enable`d (`:288`) — contrast `swap.py:349,420`, which use `enable --now`. The
unit's `ExecStart` therefore first fires at the *next boot*. Within this run there is no
second chance.

Net: the feature performs the two WOL-clobbering operations, skips the guard meant to make
them safe, skips its own re-arm, and never starts the unit that would have fixed it.

**Detection** (`status()`, `wol.py:223-245`, after `55fd139`): `armed` = sysfs
`/sys/class/net/<iface>/device/power/wakeup == "enabled"` when readable, else `"g" in
wake_flags` (`:236`). `find_persistence_unit` (`:195-220`) searches `wol-<iface>.service`,
then `wol.service`, then any enabled `/etc/systemd/system/*.service` matching
`ethtool\s+-s\s+<iface>\s+wol` (`:209`). **Detection never reads TLP or NetworkManager.** The
only TLP read in the repo is `dashboard.py:364-392`, an unlinked informational row.

**Escaping and template gaps.** `_unit_content` interpolates `iface` raw, bypassing
`unit_value()`/`unit_command()` (`common.py:152-173`) — the escaping contract that
`smoke/verify_rendered_units.py:139-184` exists to enforce. And `wol-<iface>.service` is
absent from the `expected` dict at `verify_rendered_units.py:114-121`, so **the WOL unit is
rendered by code no smoke test ever parses.** Unit rendering elsewhere is
`string.Template(...).substitute(...)` — `swap.py:273-288` (read + substitute + content-compare
+ conditional `daemon-reload`) and `scripts.py:43-66`. Placeholders in `systemd/*.tmpl` are
bare `$name`.

**Privilege.** `Runner._elevate` (`common.py:46-50`) prefixes `sudo -n`. `write_file` under
sudo (`common.py:86-101`) stages to a temp file then `mkdir -p` + `install -m`, never
`sudo tee`. CLI root is `require_root()` (`common.py:194-196`, `cli.py:50`). TUI root is
`acquire_sudo()` (`widgets.py:650-683`). `smoke/verify_runner_sudo.py` asserts every elevated
call starts with `-n ` (`:73`) and that a `sudo=False` Runner invokes `sudo` zero times
(`:79-85`).

**Contract gap.** `--dry-run` exists only on the CLI. `app.py:200` hardcodes `dry_run=False`
and `widgets.py:683` defaults it False, so the operator's preview path and apply path are not
the same button — against `AGENTS.md:38`. No test exercises `wol.run`'s mutating path at all.

### 0f. Add-model modal ground truth — measured, not inferred

`EditModelModal(ModalScreen[bool])`, `cockpit/screens/deploy.py:85-440`. **One shared modal**;
Resident vs Swappable is the `resident: bool` flag (`:151`), which only seeds `ttl` with `"0"`
vs blank (`:243-246`). Residency is *derived* on read, not stored — `_is_resident`
(`deploy.py:727-740`): `ttl == 0 or bool(group)`. Opened from `deploy.py:672,677` →
`:970-973` → `_on_add_model` (`:814-830`).

Compose structure, `deploy.py:200-258`:

```
Vertical(id="edit-model-dialog")           width: 110  height: 90%
├─ Static(id="edit-model-title")
├─ Horizontal(id="edit-model-columns")     height: 1fr
│   ├─ VerticalScroll(id="edit-model-scroll")   width: 3fr   ← fields 1-11
│   │   ├─ Vertical(id="f-llamacpp-fields")     NO HEIGHT RULE
│   │   └─ Vertical(id="f-unmanaged-fields")    NO HEIGHT RULE
│   └─ Vertical(id="edit-model-right")          width: 2fr   ← env only
└─ Horizontal(classes="action-row-primary")
```

**The c.3 clipping root cause, measured live under Textual 8.2.8:** `#f-llamacpp-fields` and
`#f-unmanaged-fields` are plain `textual.containers.Vertical`, whose framework default is
`height: 1fr; overflow: hidden hidden`, and **neither id is given a height anywhere in
`EditModelModal.DEFAULT_CSS`** (`deploy.py:93-141`). Inside a scroll container that `1fr`
resolves against the *viewport*, not the content, so the box takes leftover space and
hard-clips — and the outer `VerticalScroll` never learns the content is taller.

| terminal | `#f-llamacpp-fields` region height | its content height | outer scroll |
|---|---|---|---|
| 80×24 | **1** | 27 | virtual 22 vs viewport 10, `max_scroll_y = 3` |
| 121×30 | **1** | 27 | virtual 22 vs viewport 19, `max_scroll_y = 3` |
| 160×50 | 16 | 27 | no scrollbar at all |

At normal sizes the llama-cpp group renders **1 cell tall holding 27 cells of content**;
`#f-args` is laid out at absolute y=36, far below the viewport. `ttl` and `group` are siblings
*outside* that box, so they paint immediately after the collapsed 1-cell block — exactly the
reported "cut off after `llama_server_args`, `ttl` shown next". The outer scrollbar exists but
moves 3 rows, which is why it reads as "there is nothing more to see".

**Two further layout facts:**

- `#edit-model-dialog { width: 110 }` (`deploy.py:116`) is a hard literal. DESIGN.md §2:48-50
  sets the supported floor at **80×24**. At 80 columns, 30 cells hang off the right edge and
  **the entire right column (x≈65..107) is unreachable.** Every other dialog in the repo uses
  a percentage or a smaller literal: 90% (`deploy.py:51`, `builds.py:77`, `builds.py:168`),
  80% (`widgets.py:707`), 85 (`deploy.py:453`), 75 (`scripts.py:41`), 60 (`widgets.py:759`,
  `downloads.py:57`).
- `#edit-model-columns` carries **no `.columns-responsive` class**, so it never collapses to a
  vertical stack under `Screen.-narrow` — a direct miss against DESIGN.md §3.5:122-125.
- Columns are **3fr / 2fr** (`deploy.py:101,108`), i.e. 60/40, measured 62 vs 42 cells.
- `#f-env` is matched by two same-specificity id rules; the later `height: 5`
  (`deploy.py:135`) wins over `height: 1fr` (`:112`), so the surviving `min-height: 18`
  (`:113`) is what actually sizes it. **The `1fr` on line 112 is inert.**

**`smoke/verify_screens.py` has zero modal coverage.** Grep for `Modal` / `push_screen` in it
returns nothing. That is how a 110-cell dialog survived on an 80-cell floor and how a 1-cell
field group shipped. Any phase touching a modal must close this.

**The `id` field (c.1) is already not an input.** `deploy.py:206-207` renders a read-only
`Static #f-id-preview`; the comment at `:204-205` records that the input was deliberately
removed. Derivation is `_derived_id()`, `deploy.py:176-193` — `Path(quant_file).stem`,
non-alphanumerics → `-`, lowercased, de-duplicated with a numeric suffix. Live preview at
`:332-335` (add-mode only); applied at `:364-366`; **editing never re-derives — the stored id
wins.** `id` is load-bearing: `schema.py:120` requires it, `schema.py:121-124` rejects
duplicates, and `swap.py:255` uses it as the llama-swap YAML mapping key, i.e. the HTTP route
name. Removing the *display* is safe; removing the *derivation* breaks both.

**Not rendered anywhere: `repo_id`.** Still schema-required for `llama-cpp`
(`schema.py:128`), silently supplied at `deploy.py:379-381` — preserved on edit, literal
`"local"` for a new binding.

**Field → llama-swap mapping**, `_build_model_entry` (`swap.py:171-202`):

- **`llama_server_args` is not emitted as a key at all.** It is spliced into the middle of one
  `cmd:` string (`swap.py:178`), after `--model <path> --port ${PORT}` and before `--mmproj`,
  each token `shlex.quote`d (`:188`).
- `env` → `env:`, verbatim list of `"K=V"` strings (`:192-193`); omitted when falsy.
- `ttl` → `ttl:`, only when present (`:200-201`; the comment at `:194-199` records that
  defaulting it to 0 used to silently pin every model in VRAM).
- `group` → not per-model; collected into top-level `groups:` (`:256-263`).
- `repo_id` and `bind.gpu` never reach the output; `bind.backend` appears only as the
  `prefix_root/<backend>/current/bin/llama-server` path segment.

### 0g. Upstream ground truth (fetched 2026-09-17)

**llama-swap** `docs/config.example.yaml` (785 lines). Model keys: `cmd`, `cmdStop`, `env`,
`ttl`, `proxy`, `aliases`, `checkEndpoint`, `unlisted`, `filters`, `useModelName`,
`concurrencyLimit`, `name`, `description`, `macros`, `metadata`, `capabilities`, `timeouts`,
`unloadTimeout`, `compat`, `sendLoadingState`.

- `env` (`:303-308`): *"define an array of environment variables to inject into cmd's
  environment / each value is a single string / in the format: ENV_NAME=value"*. Example:
  `env: ["CUDA_VISIBLE_DEVICES=0,1,2"]`. **This is exactly the repo's shape.**
- `ttl` (`:323-329`): seconds. `-1` = use global default. **`0` = never unload.**
- `cmd` (`:284-289`, `:503-507`): multi-line block scalar, `${PORT}` / `${MODEL_ID}` macros.

**llama.cpp** `docs/preset.md`: a **third** naming space — INI preset files with hyphenated,
`--`-less keys (`ctx-size`, `temp`, `batch-size`, `top-p`, `hf`, `chat-template-kwargs`) under
`[*]` and `[model-name]` sections. These are neither CLI args nor llama-swap keys.

**So three naming spaces collide inside one form — this is QA item c.4 precisely:**

| # | Name | Meaning | Shape |
|---|---|---|---|
| 1 | `llama_server_args` (this repo) | tokens appended to `llama-server` | YAML list of CLI tokens |
| 2 | `cmd` (llama-swap) | the whole command | one string |
| 3 | `ctx-size`, `temp` (llama.cpp presets) | preset keys | INI, hyphenated, no `--` |

And the repo **already has a literal `cmd` field of its own** for `engine: unmanaged` models
(`deploy.py:235-237`, `README.md:249`). So inside this single form "cmd" means two different
things depending on the engine. Name that collision in the labels; do not hide it.

### 0h. `VK_ICD_FILENAMES` (QA item d) — already supported end to end

| Scope | Exists? | Citation |
|---|---|---|
| Per-model `env` | **Yes** | form `deploy.py:253-254,362,395-396`; emit `swap.py:192-193`; import round-trip `swap.py:234-235`; example `models.example.yaml:47-48`; README `:245-246` |
| Per-backend runtime env | **No** | `manifest.yaml:38` `backends.rocm.build_env` is compile-time only (`build.py:84,98,110`) |
| Host-level env defaults | **No** | `schema.py:55-88` requires only `hostname/network/gpus/paths/retain_builds/hf` |
| llama-swap global env | **No** | `_generate_config` emits only `healthCheckTimeout`, `models`, `groups` (`swap.py:246-265`) |

`README.md:246` already ships the exact line QA asked about. **This is a surfacing problem,
not a capability gap** — and `env` is completely unvalidated by `schema.py:110-152` (a dict
would pass validation and then mis-serialize downstream). **Operator decision, 2026-09-17:
surface it in the form only. Do not add a host-level or per-backend env section.**

### 0i. Separator style (QA item e) — the CSS is identical; the height is not

Both rules live in `SHARED_CSS` (`cockpit/widgets.py`) and their three declarations are
byte-identical:

```css
/* :287-291 */  Screen.-wide DashboardScreen #dashboard-left
/* :308-312 */  Screen.-wide SettingsScreen .settings-columns > .settings-column:first-of-type
                { margin-right: $space-section; padding-right: $space-section;
                  border-right: solid $surface-lighten-2; }
```

The visible difference is **column height**:

- `widgets.py:283-286` — `Screen.-wide DashboardScreen #dashboard-left, #dashboard-right
  { height: 1fr; }`, which overrides `height: auto` from `dashboard.py:50-53`.
- `widgets.py:304-307` — `SettingsScreen .settings-column { height: auto; }`, reinforced by
  `settings.py:121-123`.

So the Dashboard rule stretches to the full row (the taller column), while the Settings rule
stops at its own column's content. The operator prefers the Settings look.

The `1fr` was deliberate — the comment at `widgets.py:278-282` reads *"Both columns fill the
row, so the divider below runs its full length."* Reverting it means editing that comment
too, not deleting it silently. DESIGN.md references the gutter at `:125` (§3.5 precedent) and
`:286` (Dashboard archetype).

### 0j. Copy-ready snippet locations

| Need | Copy from |
|---|---|
| Modal shell: scroll body, `escape` binding, close button | `InfoModal`, `cockpit/widgets.py:694-745` |
| Smallest complete `ModalScreen` to clone | `ConfigPasteModal`, `deploy.py:41-82` (42 lines) |
| Modal opened from inside a modal, awaited, result used | `deploy.py:564-579` (`@work` + `push_screen_wait`) |
| Modal that *does work* (worker + `call_from_thread`) | `RetainedBuildsModal`, `builds.py:269-321` |
| Worker-thread → widget append bridge | `_BuildLogHandler` `builds.py:42-65` + `_append_build_log` `:542-543` |
| `RichLog` construction | `builds.py:406` |
| Responsive two-column pattern | `.columns-responsive` `widgets.py:255-261`; usage `settings.py:246` |
| Unit template render + compare + `daemon-reload` | `swap.py:273-288` |
| Dynamic per-row action label + confirm | `TableAction` `widgets.py:450-516`; `label` may be a callable of the row key, and `confirm` substitutes `{action}` with the resolved label |
| Validate → confirm → write pipeline | `deploy.py:411-440` |
| Emitter to extend for any new model key | `swap._build_model_entry`, `swap.py:171-202` |
| Validator to extend in lockstep | `schema.validate_models_dict`, `schema.py:110-152` |

### 0k. Anti-patterns — repo-wide, apply to every phase

1. **Never `sys.exit()` in `provision/` core or schema modules** (`AGENTS.md`, layer
   decoupling). `wol.run` and `build.run` already violate the spirit of this by calling
   `sys.exit` where a UI caller must catch `SystemExit` (`builds.py:522`). Do not add more.
2. **Every mutating operation in `provision/` must be dry-run-safe and idempotent**
   (`AGENTS.md:38`).
3. **No hex literals** in screen-level `DEFAULT_CSS` or `SHARED_CSS` (DESIGN.md §1:11).
4. **Spacing tokens, never bare `1`/`2`/`3`**, for margin/padding. `0` and `auto` stay
   literal; non-spacing integers (`height: 10`, `width: 1fr`) are not spacing (DESIGN.md
   §1:36).
5. **No widgets in `DataTable` cells, ever** (plans/03 Phase 0 §0a, verified empirically).
6. **No screen `DEFAULT_CSS` may set `height`, `min-width`, `border` or `padding` on a
   `Button`** (DESIGN.md §9:372).
7. **A rule whose selector starts with an ancestor (`Screen…`) must live in `SHARED_CSS`, not
   a widget's `DEFAULT_CSS`** — scoped parsing silently rewrites it into a selector that can
   never match (`widgets.py:263-277`).
8. **`on_mount` is structure only; every data read belongs in `on_first_view()`**
   (DESIGN.md §3.0).
9. Use `.venv/bin/python` for every gate — system `python3` has no textual.

---

## Phase 1: Contain build output in a streaming log modal

**Problem**: §0c. Build subprocess output inherits the TUI's stdout and paints over it.

**What to implement**

1. `provision/common.py` — teach `Runner` to stream instead of inherit.
   - Add an optional `on_output: Callable[[str], None] | None = None` to `Runner.__init__`.
   - In `Runner.run` (`common.py:52-72`): when `on_output` is set, replace `subprocess.run`
     with a `Popen(..., stdout=PIPE, stderr=STDOUT, text=True, bufsize=1)` plus a
     `for line in proc.stdout:` loop that calls `on_output(line.rstrip())`, then `wait()` and
     raise `CalledProcessError` when `check` and `returncode`. Preserve the existing
     `capture=True` return contract by also accumulating into a
     `CompletedProcess`. When `on_output` is None, behaviour must be **byte-identical to
     today** — do not change the default path.
   - Give `Runner.shell` (`common.py:74-79`) the same treatment. It currently has **no**
     capture parameter, which is why the sycl path is uncapturable (§0c item 4).
   - Route `Runner.apt_install` (`common.py:149`) through `self.run` instead of its own bare
     `subprocess.run`, so it stops being the one call site with no capture option at all.
2. `cockpit/screens/builds.py` — a `BuildLogModal(ModalScreen[None])`.
   - Copy the modal shell from `InfoModal` (`widgets.py:694-745`): `ModalScreen`, `escape`
     binding, `align: center middle`, a percentage width (90%, matching
     `#history-dialog`/`#retained-dialog`), `.close-button`.
   - Body is the `RichLog` from `builds.py:406`, `height: 1fr`.
   - Feed it from both sources: keep `_BuildLogHandler` (`builds.py:42-65`) for step
     narration, and pass `on_output=` into the `Runner` so subprocess lines land in the same
     widget. Prefix subprocess lines so the two streams are distinguishable.
   - The modal must stay open after the build finishes (the operator needs to read the tail)
     and only dismiss on explicit close. Disable `escape`-to-close *while* a build is running,
     or the worker outlives its own log view.
   - Delete the inline `#build-log` / `#build-status` (`builds.py:406,405`, CSS `:333-338`)
     once the modal replaces them.
3. Thread discipline: the worker is `@work(thread=True)` (`builds.py:509`), so every widget
   touch from `on_output` must go through `self.app.call_from_thread` — exactly as
   `_BuildLogHandler` already does at `builds.py:62`. A direct write from the worker thread is
   the failure mode to guard against.

**Documentation references**: §0c, §0j. `InfoModal` `widgets.py:694-745`; `_BuildLogHandler`
`builds.py:42-65`; `_append_build_log` `builds.py:542-543`; `RetainedBuildsModal`
`builds.py:269-321` for the worker+`call_from_thread` shape.

**Verification checklist**

- [ ] `python3 -m py_compile cockpit/*.py cockpit/screens/*.py provision/*.py provision/steps/*.py`
- [ ] `.venv/bin/python smoke/verify_runner_sudo.py` — still green. This is the gate that
      proves `_elevate`/`-n`/`install -m` semantics survived the `Popen` rewrite.
- [ ] `.venv/bin/python smoke/verify_rendered_units.py` — green (it subclasses `Runner`).
- [ ] `.venv/bin/python smoke/verify_screens.py` — green.
- [ ] New assertion, in `smoke/verify_runner_sudo.py`: a `Runner(on_output=collector)` running
      a command that prints N lines calls `collector` N times, and the same `Runner` with
      `on_output=None` leaves `stdout`/`stderr` as `None` in the `subprocess` call.
- [ ] `grep -n "stdout=subprocess.PIPE if capture else None" provision/common.py` — the
      default path is unchanged.
- [ ] Manual, on the target host: start a build, confirm no raw text appears outside the
      modal, and that `cmake --build` lines appear *inside* it.

**Anti-pattern guards**

- Do **not** use `app.suspend()`. It exists at `widgets.py:668`, `containers.py:229`,
  `settings.py:1186` for interactive commands; a build is not interactive and suspending the
  TUI for ten minutes is the defect wearing a hat.
- Do **not** write to a widget from the worker thread without `call_from_thread`.
- Do **not** change `Runner.run`'s behaviour when `on_output` is None.
- Do **not** invent a Textual streaming API. There is no `Popen`/`readline` precedent in this
  repo (§0c) — write the loop explicitly.

---

## Phase 2: Installs tab — separate version selection from building

**Depends on Phase 1** (Build opens the log modal).

**Problem**: §0a second half. The Update action builds the already-pinned SHA and cannot
change anything. The table shows a pin and calls it a version, and never shows what is
actually installed.

**Operator decision, 2026-09-17** — the Rebuild/Bump-pin split was rejected. The model is:

> *"the column should call out version and the Version ID can be bumped to latest (but we call
> this update version, not bump pin), then we have a build button. Just pressing build on
> current version would equal a rebuild. … we need 'Update to latest', 'Select other version'
> (… to go back so to say; seeing as we have the historical builds button), and 'Build'."*

**The data model that makes this coherent.** Three distinct facts are currently collapsed into
one column. Separate them, because conflating them is what produced the QA report:

| Fact | Source | Scope |
|---|---|---|
| **Version** — what is selected | `manifest.yaml` `llama_cpp.ref` | **global** (one value, all backends) |
| **Installed** — what is on disk and active | `current` symlink target, `list_builds()` `build.py:184-196` | **per backend** |
| **Upstream** — what exists remotely | `update_check.check_llama_cpp` `:75-89` | global |

Because the pin is global and the build is per-backend, version actions are **screen-level
buttons** and Build is a **per-row action**. That split is not cosmetic — it is the data model
made visible.

**What to implement**

1. **Columns** (`builds.py:412-418`, `_refresh_backends_table` `:452-474`). Replace
   `Component | Pinned ref | Status | [Update]` with:

   | Column | Width | Content |
   |---|---|---|
   | Component | 22 | `llama.cpp (vulkan)` |
   | Version | 14 | selected ref, short — from `manifest` |
   | Installed | 14 | `current` symlink's ref, short, or `—` / `not built` |
   | Status | 35 | `_format_update_cell` output |
   | `[ Build ]` | 10 | per-row action |

   Budget: 22+14+14+35+10 = 95 content, render 95 + 2×5 = **105 ≤ 115**
   (DESIGN.md §4.2; `_assert_table_widths_in_budget` `verify_screens.py:39-56`).

   Style the Installed cell to make drift legible: green when it equals Version, yellow when
   it differs, dim when absent. **This is the cell that would have told QA the truth
   immediately.**

2. **Action rows** (`builds.py:400-403`). Two rows, **for grouping — not for width.**
   `SHARED_CSS` already stacks action rows one button per line under `Screen.-narrow`
   (`widgets.py:211-217`, DESIGN.md §9 "Action-row overflow at narrow widths"), so five
   buttons in one row would already fit the 80-cell floor. The split is semantic:

   ```
   row 1 (version):   [ Update to latest ]  [ Change version… ]  [ Check for Updates ]
   row 2 (artefacts): [ Retained Builds ]   [ Build History ]
   ```

   Ordering rationale: row 1 is the decision (which version), row 2 is the record (what has
   been built). Build is deliberately *not* here — it is per-backend, so it lives in the table.

3. **`Update to latest`** — writes `llama_cpp.ref` in `manifest.yaml` to the SHA from
   `self._llama_cpp_check["latest"]`. Enabled only when that check is `ok` and
   `update_available`; otherwise disabled with a tooltip/notify saying the check has not run.
   Gate behind `ConfirmModal(danger=True)` showing **old → new** and the `latest_date`
   (DESIGN.md §5:201 — a write to a declarative repo file is still confirmed).
   After writing, re-read `manifest.yaml` into `self.manifest` and refresh the table.
   **It does not build.**

4. **`Change version…`** — a `ModalScreen[str | None]` version picker. Clone
   `ConfigPasteModal` (`deploy.py:41-82`) for the shell; open with `push_screen_wait` from a
   `@work` method (`deploy.py:564-579`). Sources, in this order:
   - refs already present in `state_dir/build-history.jsonl` (`read_build_history`
     `build.py:166-181`), annotated with their recorded outcome — this is the "go back" path
     the operator asked for;
   - refs currently on disk from `list_builds()` per backend, annotated `installed`;
   - upstream releases (GitHub `/releases`, cached the way `update_check.py:25-49` caches);
   - a manual SHA entry field.

   Selecting a version writes the pin, same confirm as §3. If the chosen version is already
   built and sane for a backend, say so in the confirm and point at **Retained Builds** for
   activation — because activating an existing build is a symlink flip, not a build (§0b).
   **It does not build.**

5. **`[ Build ]` per-row action** — replaces `[ Update ]` (`builds.py:418-426`). Builds the
   currently selected version for that backend.
   - `available` predicate: always true for a row in `self.backends` (drop
     `_backend_has_update`, `builds.py:444-450` — gating Build on an update being available is
     what made the action unreachable and the intent unclear).
   - **Dynamic label**, per `TableAction`'s contract (`widgets.py:458-463`): the callable
     returns `"Rebuild"` when `_prefix_ready(prefix_root/<backend>/<ref>)` and `"Build"`
     otherwise. A callable label **must** pass `width=`. `confirm` substitutes `{action}` with
     the resolved label (`widgets.py:466-469`), so one prompt covers both:
     `"{action} llama.cpp ({row}) at version <short-ref>? This can take several minutes."`
   - `requires_root=True` (unchanged).
   - Add `force: bool = False` to `build_step.run` and thread it to the `_prefix_ready`
     skip at `build.py:255-256`. The Rebuild path passes `force=True`, which makes the
     operator's *"pressing build on current version would equal a rebuild"* literally true
     instead of a silent no-op.

6. **Make the post-build refresh mean something** (`builds.py:528-532`). After a build,
   re-read `list_builds()` for the affected backend so the **Installed** column changes. Do
   **not** re-run the network check — `on_refresh_requested`'s no-network contract
   (`builds.py:431-432`) stands. Status legitimately still reads "update available" when the
   pin is behind upstream; with Version and Installed both visible that is now informative
   rather than confusing.

7. **Update DESIGN.md §4.4 and §5 in lockstep — both are already stale.** Verified
   2026-09-17:
   - §4.4:143 records `backends-table` as *4 columns ("Component" w=22, "Pinned ref" w=14,
     "Status" w=34, `Update` action w=10). Render 88.* The code has been `width=55` on Status
     since the QA pass (`builds.py:417`), so the real render is **109**, not 88. The entry is
     wrong today and this phase changes it again — rewrite it to the 5-column, render-105
     shape.
   - §5:206-207 lists `_handle_update_selected_press` and `_handle_rollback_press` as the
     Builds confirm call sites. **Neither identifier exists anywhere in the code** — grep
     returns only those two DESIGN.md lines. The real sites are `handle_table_action`
     (`builds.py:488`) → `_run_build` (`builds.py:509`) and `RetainedBuildsModal._run_rollback`
     (`builds.py:302`). Correct them, and add the two new version actions with their tier.
   - §5 "Expressing the Tier" lists the declarative files as `models.yaml`, `scripts.yaml`,
     `hosts/<hostname>.yaml`. This phase makes `manifest.yaml` writable from the TUI for the
     first time, so add it to that list — noting it differs from the others in being **tracked
     by git**, which is what makes the pin auditable and is the reason the confirm shows
     old → new.

8. **Write path for `manifest.yaml`.** `manifest.yaml` is tracked by git and is currently
   read-only to this codebase — this phase makes it writable for the first time. Preserve the
   header comment block (`manifest.yaml:1-7`) and the `# b10903` build-number trailing comment
   on `ref`, updating the latter from the release tag when known. `yaml.safe_dump` will
   destroy both. Prefer a targeted line rewrite (regex on the `ref:` line) over a
   load/dump round-trip, and assert in the smoke test that the header survives.

**Documentation references**: §0a, §0b, §0d, §0j. `TableAction` `widgets.py:450-516`.
`ConfirmModal` `widgets.py:747-793`. Existing declarative-write confirm precedent
`settings.py:1249-1290`.

**Verification checklist**

- [ ] `py_compile` gate (all four globs).
- [ ] `.venv/bin/python smoke/verify_screens.py` — green, including
      `_assert_table_widths_in_budget` and `_assert_buttons_in_bounds` at both 80×24 and
      121×30.
- [ ] New smoke assertion: the Installs table renders **5** columns and the computed render
      width is ≤ 115.
- [ ] New smoke assertion: `manifest.yaml`'s header comment and the `# bNNNNN` trailing
      comment survive a pin write. Round-trip a temp copy, assert `startswith("# Version pins")`
      and that the `ref:` line still carries its `#` comment.
- [ ] New smoke assertion: `build_step.run(..., force=True)` does not take the
      `_prefix_ready` early return. Use the `CapturingRunner` pattern from
      `verify_rendered_units.py:44-59` — deliberately not `dry_run=True`, which short-circuits
      before the branch is reached.
- [ ] `grep -n "_backend_has_update" cockpit/screens/builds.py` — no hits (removed).
- [ ] `grep -rn "Pinned ref" cockpit/` — no hits.
- [ ] Manual: press Build on an unbuilt version → label reads `Build`, Installed changes on
      completion. Press again → label reads `Rebuild`, confirm says Rebuild, and it actually
      rebuilds.

**Anti-pattern guards**

- Do **not** merge version selection into Build. The whole point of the rework is that
  choosing a version and materialising it are separate acts.
- Do **not** conflate **Change version…** (rewrites the pin) with **Retained Builds**
  (flips the `current` symlink, `build.py:199-206`). Two different operations on two different
  pieces of state; the existing modal keeps its job.
- Do **not** `yaml.safe_dump` `manifest.yaml`.
- Do **not** re-trigger the network check on `r` — `builds.py:431-432` is deliberate.
- Do **not** reintroduce a per-row action gated on `update_available`.

---

## Phase 3: Detect and surface foreign llama.cpp builds

**Operator decision, 2026-09-17**: mirror what `55fd139` already did for WOL units — detect,
surface, never write. `prefix_root` stays the single authoritative install root.

**Problem**: §0a. A hand-built `/opt/llama.cpp/build-vulkan` is completely invisible to the
toolkit, so an operator who has one cannot tell from the TUI why nothing they do affects it.

**What to implement**

1. `provision/steps/build.py` — `find_foreign_builds(host_profile) -> list[dict]`, modelled
   directly on `wol.find_persistence_unit` (`wol.py:195-220`), which is the repo's existing
   precedent for "something this toolkit did not install is nonetheless doing the job".
   - Scan a **fixed, documented** candidate list — `/opt/llama.cpp`, `/usr/local`,
     `/opt/llama.cpp/build-*` — never a recursive filesystem walk.
   - For each, report `{"path", "kind", "sane"}` where `sane` reuses `_binary_sane`
     (`build.py:59-61`) on `bin/llama-server` (and the in-tree `build-*/bin/` layout, which
     upstream's own cmake produces).
   - Exclude anything under `prefix_root` — those are managed, not foreign.
   - **Read-only. No writes, no symlinks, no `rm`.** This function must have no `Runner` at
     all, which makes that guarantee structural rather than a promise.
2. `cockpit/screens/builds.py` — surface them. Either extra rows in the Installs table styled
   dim with Version `—` and Installed `<path> (unmanaged)`, or a line under the table. Prefer
   rows only if the width budget still holds; a `Static` line is acceptable and cheaper.
3. `README.md` §Architecture or a new note under "Operational Lifecycle": state plainly that
   builds live under `paths.prefix_root`, that `/opt/llama.cpp` is neither read nor written,
   and that the per-backend rows are **backends declared in the host profile**, not filesystem
   discoveries. This sentence is the cheapest part of the whole plan and would have prevented
   the QA report.

**Documentation references**: §0a, §0b. `wol.find_persistence_unit` `wol.py:195-220` is the
copy-source for the three-stage "look in the obvious places, then match on behaviour" search.

**Verification checklist**

- [ ] `py_compile` gate.
- [ ] `.venv/bin/python smoke/verify_screens.py` — green; table budget still ≤ 115 if rows
      were used.
- [ ] New fixture test in `smoke/verify_sysinfo.py`, alongside
      `check_wol_detects_foreign_unit()` (`:186-209`) which is the exact precedent: build a
      tmp tree with a fake `/opt/llama.cpp/build-vulkan/bin/llama-server`, assert
      `find_foreign_builds` reports it, and assert a path under `prefix_root` is **not**
      reported.
- [ ] `grep -n "Runner" provision/steps/build.py` within `find_foreign_builds` — no hits.
- [ ] `grep -rn "/opt/llama" README.md` — the new note exists.

**Anti-pattern guards**

- Do **not** write to, adopt, symlink, or prune a foreign tree. Detection only.
- Do **not** recursively walk the filesystem. A fixed candidate list is auditable; a walk on a
  host with a large `/opt` is not.
- Do **not** change `prefix_root`'s default or semantics.

---

## Phase 4: Fix the Wake-on-LAN regression

**Highest operator impact — this one broke working hardware.** Run it first in wall-clock
terms; nothing else depends on it.

**Problem**: §0e. Three composing defects: PATH-gated guards under a sudo-executed mutation
path; link-disturbing operations before any verification, with a `sys.exit` between them; and
a unit that is enabled but never started.

**What to implement**

1. **Resolve tool paths absolutely, once** (`wol.py:74-88,126-138,155-178`). Replace every
   `shutil.which("tlp"|"ethtool"|"nmcli")` with a helper that checks the standard sbin
   locations explicitly — `/usr/sbin`, `/sbin`, `/usr/local/sbin` — before falling back to
   `shutil.which`. The repo already hardcodes `/usr/sbin/ethtool` at `wol.py:118`, so it
   already knows where these live; the guards just did not. Use the resolved absolute path in
   every `runner.run([...])` call so the sudo `secure_path` and the caller's PATH cannot
   disagree.
   **This alone is the fix for "the real cause"** — it is what stops `_fix_tlp` from
   silently returning without writing `WOL_DISABLE=N`.
2. **Fail before touching the link, not after** (`wol.py:263-268`). Move the `ethtool`
   availability check **above** `_fix_tlp` and `_arm_networkmanager`. If `ethtool` cannot be
   found, exit having changed nothing. Today the exit at `:268` fires *after* `tlp start` and
   `nmcli con up` have already cycled the NIC.
3. **Re-arm unconditionally after the disturbing steps** (`wol.py:269-273`). Drop the
   `if "g" not in wake_flags` guard around `ethtool -s <iface> wol g`. The command is
   idempotent by nature, and the current check-then-act reads one sample *before* TLP and NM
   have finished re-applying policy asynchronously. Keep the log line distinguishing "was
   already armed" from "armed it now" — that is the useful half of the guard, and it costs
   nothing to keep while still issuing the command.
4. **Start the unit, do not merely enable it** (`wol.py:286-288`). Use
   `systemctl enable --now wol-<iface>.service`, matching `swap.py:349,420`. `Type=oneshot`
   + `RemainAfterExit=yes` means starting it runs `ExecStart` immediately, which is the
   in-band re-arm the run currently lacks entirely.
5. **Verify the end state and report it honestly** (`wol.py:295-310`). Re-read sysfs
   `power/wakeup` *and* `ethtool` after all mutations and report both. If the host ends
   disarmed, say **what was changed** (TLP conf written, NM connection modified, link cycled)
   rather than blaming a missing package. An operator who knows the link was just cycled can
   act; one told "is ethtool installed?" cannot.
6. **Teach detection about TLP** (`status()`, `wol.py:223-245`). Add a `tlp_disables_wol`
   signal: TLP present and `/etc/tlp.d/01-wol.conf` absent or not `WOL_DISABLE=N`. Surface it
   in the Settings WOL panel (`settings.py:1035-1038`) as the reason a host that looks armed
   now will not be after the next suspend. Also widen `_fix_tlp`'s inspection beyond
   `/etc/tlp.d/01-wol.conf` (`wol.py:130`) — it currently misses `/etc/tlp.conf` and any
   later-sorting `/etc/tlp.d/*.conf` that overrides `01-`.
7. **Move the unit into `systemd/wol.service.tmpl`** and render it the way every other unit is
   rendered — `string.Template(...).substitute(...)`, `swap.py:273-288`. Escape `iface`
   through `unit_value()` (`common.py:152-173`); `_unit_content` (`wol.py:102-123`)
   interpolates it raw today. **Keep the unit's content byte-identical** — §0e establishes it
   is already correct and matches the operator's own repair. This is a relocation for
   testability, not a redesign.
8. **Register it with the smoke suite**: add `wol-<iface>.service` to the `expected` dict at
   `verify_rendered_units.py:114-121`, with its call site alongside `:99-112`. It is currently
   the only generated unit no test ever parses.

**Documentation references**: §0e, §0j. `55fd139` for the detection precedent.
`swap.py:273-288` for render+compare+`daemon-reload`. `swap.py:349,420` for `enable --now`.
The operator's own repair transcript (`wol-fix-steps`) is the acceptance criterion.

**Verification checklist**

- [ ] `py_compile` gate.
- [ ] `.venv/bin/python smoke/verify_rendered_units.py` — green, **and now covering
      `wol-<iface>.service`**: `[Unit]`/`[Service]`/`[Install]` present, `ExecStart` and
      `ExecStop` both present, no unexpanded `$placeholder`
      (`assert_no_unexpanded_placeholder` `:77-90`).
- [ ] `.venv/bin/python smoke/verify_screens.py` — green, including
      `_assert_wol_form_wiring` (`:246-290`) and `_assert_apply_service_ignores_wol` (`:231`).
- [ ] `.venv/bin/python smoke/verify_sysinfo.py` — green, including
      `check_wol_detects_foreign_unit()` (`:186-209`).
- [ ] New test: `wol.run` under a `CapturingRunner` with `ethtool` unresolvable performs
      **zero** mutations — specifically no `tlp start`, no `nmcli con up`, no file writes.
      This is the regression test for defect (ii) and it is the most important one here.
- [ ] New test: the rendered unit equals the operator's repair unit modulo the interface name.
- [ ] `grep -n "shutil.which" provision/steps/wol.py` — no remaining bare `which` on a guard
      that precedes a mutation.
- [ ] `grep -n "systemctl.*enable" provision/steps/wol.py` — uses `enable --now`.
- [ ] `bin/provision --dry-run wol` — prints every intended action, mutates nothing.
- [ ] **Manual, on `haupe-server`, and this is the real gate**: after running the feature,
      `sudo ethtool enp6s0 | grep Wake-on` → `Wake-on: g`, and
      `cat /sys/class/net/enp6s0/device/power/wakeup` → `enabled`. Then a full poweroff and a
      magic packet.

**Anti-pattern guards**

- Do **not** change the unit's content. §0e establishes it is correct.
- Do **not** touch or remove the operator's own `wol.service`. `find_persistence_unit`
  (`wol.py:195-220`) finds it; that is detection, not ownership. Two units both running
  `ethtool -s enp6s0 wol g` is harmless and idempotent.
- Do **not** add an `ethtool -s … wol d` path. There is none in the repo today (§0e) and
  there must not be one.
- Do **not** run any mutation before the availability checks pass.
- Do **not** report success from the mere fact that a step ran. The module's existing
  "evidence-of-absence verification" comment at `wol.py:294` is the right instinct; extend it,
  do not weaken it.

---

## Phase 5: Rework the add-model modal

Covers QA items c.1–c.4 and d.

**Problem**: §0f. A field group with no height rule clips 27 cells of content into 1. A hard
110-cell dialog overflows the 80-cell supported floor. Three naming spaces collide in one form
with no explanation.

**What to implement**

1. **c.3 — fix the clipping, which is the actual defect.** Give `#f-llamacpp-fields` and
   `#f-unmanaged-fields` `height: auto` in `EditModelModal.DEFAULT_CSS` (`deploy.py:93-141`).
   They are plain `Vertical`s inheriting Textual's `height: 1fr; overflow: hidden hidden`, and
   `1fr` inside a `VerticalScroll` resolves against the viewport, so the group collapses and
   the outer scroll never learns the content is taller. With `auto`, the outer
   `#edit-model-scroll` gets a real virtual size and a real scrollbar.
   **Fix this before anything cosmetic** — everything else in this phase is layout polish on
   top of a form whose fields are currently unreachable.
2. **c.2 — width and equal columns.** Replace `width: 110` (`deploy.py:116`) with a
   percentage, matching `#history-dialog`/`#retained-dialog` (`builds.py:77,168`). Set
   `#edit-model-scroll` and `#edit-model-right` both to `width: 1fr` (from 3fr/2fr at
   `deploy.py:101,108`). Add `.columns-responsive` to `#edit-model-columns`
   (`deploy.py:200-202`) so it stacks under `Screen.-narrow` per DESIGN.md §3.5:122-125 — it
   is the only two-column container in the repo that does not. A percentage width is *wider in
   effect* at 121+ than the current literal while finally fitting the 80-cell floor.
3. **c.1 — the `id` field.** It is already a read-only `Static`, not an input
   (`deploy.py:206-207`). The operator asks for it to be hidden. Remove the `Label` + `Static`
   pair from `compose`. **Keep `_derived_id()` (`deploy.py:176-193`) exactly as it is** — it
   is load-bearing for `schema.py:120` and for `swap.py:255`, where the id *is* the HTTP route
   name. Because the id is the route, do not make it invisible everywhere: surface it in the
   save confirmation, or as a dim one-line note near the model-file Select, so the operator
   still learns the route name before committing. Also drop the now-dead `on_select_changed`
   preview refresh at `deploy.py:332-335`.
4. **c.4 — naming, order, and the `cmd` collision.** Move `llama_server_args` from the left
   column to the **right column, above `env`** — both are multi-line and both deserve the
   width, and this is what the operator asked for. Relabel with the mapping made explicit
   rather than hidden:

   | Field | Current label | New label |
   |---|---|---|
   | `llama_server_args` | `llama_server_args (one flag/value per line)` | `llama-server arguments — one flag or value per line. Appended to the generated llama-swap `cmd:` after `--model` and `--port`.` |
   | `cmd` (unmanaged) | `cmd (raw shell command, ${PORT} available)` | `cmd — the complete command llama-swap runs. `${PORT}` and `${MODEL_ID}` are substituted.` |
   | `env` | `env (one KEY=VALUE per line)` | `env — one `KEY=VALUE` per line. Injected into the command's environment.` |
   | `ttl` | `ttl (seconds; 0 = never unload, blank = llama-swap default)` | keep — it is already correct per §0g `:323-329`, and it is the only label in the form that already explains a footgun |

   Order in the right column: `llama-server arguments` → `env`, chronologically as the
   operator asked. The two are mutually exclusive by engine (`_toggle_engine_fields`,
   `deploy.py:322-325`), so `cmd` takes the same slot when `engine: unmanaged`.

5. **c.4 + d — an `Examples` nested modal.** A button beside the arguments field and beside
   `env`, opening a `ModalScreen[str | None]` of copy-in snippets. Nested modals are
   well-precedented here — `deploy.py:564-579` is a `@work` + `push_screen_wait` pair, and
   `ConfigPasteModal` (`deploy.py:41-82`, 42 lines) is the shell to clone. Content, all
   drawn from §0g and `models.example.yaml`:
   - **Arguments**: `--ctx-size 262144`, `--cache-type-k q4_0`, `--flash-attn`, `--jinja`,
     `--embedding --pooling cls` — copied from the real entries at `models.example.yaml:26-46`
     and `:57-62`, not invented.
   - **env**: `VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/intel_icd.x86_64.json` labelled
     *"Intel GPU via Vulkan — required on hosts where the Intel ICD is not the default"*, and
     `CUDA_VISIBLE_DEVICES=0`. Both already ship in `models.example.yaml:47-48,63-64`.
   - A pointer line naming the three naming spaces (§0g) so the operator knows that llama.cpp
     preset keys (`ctx-size`) are **not** what this field takes.

   **This is the whole of QA item d.** Per-model `env` already works end to end (§0h) and
   `README.md:246` already ships that exact line. Operator decision: surface only; no
   host-level or per-backend env section.

6. **Validate `env`.** `schema.validate_models_dict` (`schema.py:110-152`) never mentions
   `env` — a dict passes validation and mis-serializes downstream. Add: must be a list, every
   element a string containing `=`. Cheap, and it closes a real hole while this file is open.

7. **Close the modal smoke gap.** `smoke/verify_screens.py` has **zero** modal coverage
   (§0f) — that is how both the 110-cell dialog and the 1-cell field group shipped. Add a
   modal pass: push `EditModelModal` at 80×24 and 121×30, assert every field widget's region
   is within the dialog's region, assert no field group has a region height smaller than its
   `virtual_size` height, and run `_assert_buttons_in_bounds` against it.

**Documentation references**: §0f, §0g, §0h, §0j. DESIGN.md §2:48-50 (80×24 floor), §2:59-65
(scrollbar containment), §3.5:122-125 (`.columns-responsive`), §8:306-311 (form archetype),
§5:165-167 (modal confirm). `ConfigPasteModal` `deploy.py:41-82`. `_build_model_entry`
`swap.py:171-202`.

**Verification checklist**

- [ ] `py_compile` gate.
- [ ] `.venv/bin/python -m cockpit.widgets` (the CSS parse gate used in the last QA pass).
- [ ] `.venv/bin/python smoke/verify_screens.py` — green, **including the new modal pass**.
- [ ] New assertion: at 80×24, `EditModelModal`'s dialog region is fully inside the screen
      region. This is the one that fails today.
- [ ] New assertion: `#f-llamacpp-fields.region.height >= its virtual_size.height`, or the
      outer scroll's `max_scroll_y` covers the full content. This is the c.3 regression test.
- [ ] Schema validation gate from `AGENTS.md` (the one-liner loading `manifest.yaml`,
      `hosts/example.yaml`, `models.example.yaml`) — green with the new `env` rule.
- [ ] New schema test: `env: {"A": "B"}` is rejected; `env: ["A=B"]` is accepted.
- [ ] `grep -n "f-id-preview" cockpit/screens/deploy.py` — no hits.
- [ ] `grep -n "_derived_id" cockpit/screens/deploy.py` — still present and still called at
      the save path.
- [ ] `grep -n "width: 110" cockpit/screens/deploy.py` — no hits.
- [ ] Manual at 80×24 and 121×30: every field is reachable, the arguments field and `env` are
      both in the right column with arguments above, and Examples opens and inserts.

**Anti-pattern guards**

- Do **not** remove `_derived_id()`. §0f: `schema.py:120` requires `id` and `swap.py:255` uses
  it as the route name.
- Do **not** rename the `llama_server_args` **schema key**. Relabel the form field only —
  renaming breaks every existing `models.yaml`, which is gitignored and unrecoverable from
  this repo.
- Do **not** add a `repo_id` input. It is deliberately supplied at `deploy.py:379-381`.
- Do **not** set `height` or `padding` on a `Button` in screen CSS (DESIGN.md §9:372).
- Do **not** re-add the inert `#f-env { height: 1fr }` at `deploy.py:112` — it is overridden
  by `:135` and has never done anything. Delete it or make it win, but do not leave both.
- Do **not** invent llama-server flags for the Examples modal. Every snippet must be copied
  from `models.example.yaml` or from the upstream docs cited in §0g.

---

## Phase 6: Match the Dashboard separator to the Settings separator

**Problem**: §0i. The CSS is identical; the Dashboard's columns are `height: 1fr` and the
Settings' are `height: auto`, so the Dashboard's rule runs the full row while the Settings'
stops at its content. The operator prefers the Settings look.

**What to implement**

1. Remove `Screen.-wide DashboardScreen #dashboard-left, #dashboard-right { height: 1fr; }`
   (`widgets.py:283-286`). `dashboard.py:50-53` already sets `height: auto`, which then takes
   effect.
2. **Rewrite the comment at `widgets.py:278-282`**, which currently justifies the rule being
   removed (*"Both columns fill the row, so the divider below runs its full length"*). Replace
   it with the reason it is gone: the gutter is content-height on both screens, matching
   Settings, per operator preference 2026-09-17. This repo's CSS comments carry measured
   findings — leaving a stale justification behind is worse than leaving the rule.
3. **Better still: unify.** Both selectors now carry three identical declarations
   (`widgets.py:287-291` and `:308-312`). Fold them into one rule with a grouped selector so
   there is one definition of "column gutter" and the two screens cannot drift again. Keep the
   `Screen.-narrow` reset (`widgets.py:292-297`) — Settings has no narrow reset today because
   `.columns-responsive` handles its stacking, so check whether the Dashboard's explicit reset
   is still needed once the heights match, and delete it if not.
4. Update DESIGN.md §3.5:125 and the Dashboard archetype at `:286` if the wording implies a
   full-height rule.

**Documentation references**: §0i. `widgets.py:263-277` explains why these rules must live in
`SHARED_CSS` and not in a screen's `DEFAULT_CSS` — do not move them while tidying.

**Verification checklist**

- [ ] `.venv/bin/python -m cockpit.widgets` — CSS parses.
- [ ] `.venv/bin/python smoke/verify_screens.py` — green at 80×24 and 121×30, including the
      phantom-scrollbar assertion (`verify_screens.py:417`).
- [ ] `grep -n "height: 1fr" cockpit/widgets.py` — no hit on the dashboard column rule.
- [ ] Visual: at 121×30 the Dashboard rule ends with the left column's content, as Settings
      does. Compare the exported SVGs — `verify_screens.py` writes them.

**Anti-pattern guards**

- Do **not** move these rules into `DashboardScreen.DEFAULT_CSS`. A selector starting with
  `Screen…` is silently rewritten by Textual's scoped parser into one that can never match —
  `widgets.py:263-277` records this being measured live.
- Do **not** delete the explanatory comment block wholesale. Rewrite it.
- Do **not** change the border colour, style, or the `$space-section` padding. Only the height
  differs.

---

## Phase 7: Final verification

1. **Full gate sweep**, in this order:
   ```bash
   python3 -m py_compile cockpit/*.py cockpit/screens/*.py provision/*.py provision/steps/*.py
   .venv/bin/python -m cockpit.widgets
   .venv/bin/python smoke/verify_rendered_units.py
   .venv/bin/python smoke/verify_runner_sudo.py
   .venv/bin/python smoke/verify_sysinfo.py
   .venv/bin/python smoke/verify_screens.py
   .venv/bin/python smoke/verify_no_gpu_wake.py
   ```
   `verify_no_gpu_wake.py` is not optional. `AGENTS.md` records that reading GPU telemetry
   cost two fan-ramp incidents; Phase 3's foreign-build scan is the change in this plan most
   capable of regressing it.

2. **Schema gate**:
   ```bash
   python3 -c "from provision import schema; from pathlib import Path; r = Path('.'); m = schema.load_manifest(r/'manifest.yaml'); h = schema.load_host_profile(r/'hosts/example.yaml'); schema.load_models(r/'models.example.yaml', h, m); print('OK')"
   ```

3. **Dry-run gate**: `bin/provision --dry-run all` mutates nothing.

4. **Anti-pattern grep sweep**:
   ```bash
   grep -rn "Pinned ref\|_backend_has_update" cockpit/          # Phase 2: empty
   grep -rn "width: 110\|f-id-preview" cockpit/screens/deploy.py # Phase 5: empty
   grep -n  "shutil.which" provision/steps/wol.py               # Phase 4: no pre-mutation guards
   grep -rn "ethtool.*wol.*d\b" provision/                      # always empty
   grep -rn "#[0-9a-fA-F]\{6\}" cockpit/widgets.py cockpit/screens/  # DESIGN.md §1: no hex
   ```

5. **Character budget** (`AGENTS.md`): every harness-loaded rule file stays under 12,000
   characters. Check `AGENTS.md` and `cockpit/DESIGN.md` if either was edited.

6. **Documentation is part of done.** Confirm each landed:
   - README: builds live under `prefix_root`; `/opt/llama.cpp` is neither read nor written;
     Installs rows are declared backends, not filesystem discoveries (Phase 3).
   - README §Declarative Configuration: `manifest.yaml` is now writable from the TUI via
     **Update to latest** / **Change version…**, and the pin remains the source of truth
     (Phase 2). `README.md:165` currently asserts the opposite and must be corrected.
   - README §Operational Lifecycle item 4: the WOL description already claims TLP and
     NetworkManager handling — after Phase 4 it will finally be true. Verify the wording
     matches the shipped behaviour rather than assuming it does.
   - DESIGN.md: a **modal width rule**. §4.4:149 says modal tables are budgeted "against their
     dialog width" without ever saying what a dialog width may be, and the codebase carries
     eight ad-hoc values (90%, 85%, 80%, 110, 85, 75, 60, 60). Phase 5 fixes one dialog; the
     contract gap is what allowed it.

7. **Host-dependent items that cannot be closed here** — hand to the operator on
   `haupe-server`:
   - WOL: full poweroff, then a magic packet. The only real proof (Phase 4).
   - A real llama.cpp build end to end: output containment (Phase 1), Installed column
     changing (Phase 2), Build → Rebuild label flip (Phase 2).
   - Foreign-build detection against the actual `/opt/llama.cpp` tree (Phase 3).
   - Terminal colour judgement on the Installed cell's green/yellow drift styling (Phase 2).

---

## Open questions deliberately left unresolved

1. **`state_dir/update-check-result.json` is written and never read** (`bin/check-updates:59`),
   and **`cache_age_seconds()` has no callers** (`update_check.py:52-59`). Both confirmed dead
   by grep (§0d). Phase 2 touches this area; either wire them to something or delete them, but
   do not leave a third party writing a file nobody reads.
2. **The TUI has no dry-run.** `app.py:200` hardcodes `dry_run=False`, so the operator's
   preview and apply paths are different buttons — against `AGENTS.md:38` (§0e). Out of scope
   here; it is a design change, not a QA fix. It is also what made the WOL regression
   unpreviewable.
3. **`hosts/upstream.update-cache.yaml` lives in the repo, not `state_dir`**
   (`update_check.py:25`). A machine-specific cache inside a git working tree sits oddly
   beside the repository-discipline rule that deployment facts stay out of the repo
   (`AGENTS.md`, §Repository Discipline 4). It is gitignored, so this is untidiness rather
   than a leak.
4. **`build.run` and `wol.run` call `sys.exit`** and UI callers catch `SystemExit`
   (`builds.py:522`). `AGENTS.md` §Repository Discipline 2 asks for descriptive exceptions
   instead. Pre-existing; both phases touch these functions and should not make it worse.
