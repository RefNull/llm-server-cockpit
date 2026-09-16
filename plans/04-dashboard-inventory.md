# Plan: Dashboard → Static System Inventory

Baseline: HEAD `0cb589f`. Source: operator scope decision, 2026-09-16, after two fan-ramp
incidents (`5c986a4`, `b799d83`) traced to GPU telemetry.

**Decisions taken 2026-09-16** (see the homestack/PiDeployment merge audit, same date):
- **x86-64 only. Raspberry Pi is out of scope — it always was, structurally.** See §0c.
- **No merge with homestack.** The two projects split by *workload*, not hardware: homestack
  deploys home services (DNS, media, Samba, backups), this deploys LLM inference. Both run on
  Debian. Nothing in this plan reaches for a shared abstraction with it.

**Goal**: delete live system measurement from the cockpit. This toolkit deploys LLM services;
monitoring the host belongs elsewhere. The Dashboard becomes a static inventory — left column
"what machine is this", right column "what services are deployed and are they on".

## Scope decisions locked before this plan was written

1. **All live utilization goes.** CPU%, RAM%, GPU utilization, GPU memory, power draw, the
   gauges, the `Sample GPUs` control and the `g` binding. `provision/steps/metrics.py` is
   deleted outright, not trimmed.
2. **Why deletion beats the on-demand button** (`b799d83`): a button still leaves a live
   telemetry path in a deployment tool. This class of defect has recurred twice. Removing the
   capability removes the thing that can regress.
3. **Left column = static identity.** Hardware model/revision, CPU, RAM size, OS, kernel,
   accelerator inventory with driver versions. Everything from files already on disk.
4. **Right column = services, standardized.** Service name first, then a status glyph, then
   optional detail. Today the glyph leads (`✔ llama-swap: running`); it moves after the name so
   the glyph column is scannable.
5. **What this deliberately gives up**: "is the GPU busy right now". `nvidia-smi`, `nvtop` and
   any real monitoring stack answer that better. Nothing in the deploy workflow reads it.
6. **Single platform target: x86-64 Debian with a discrete GPU.** The left column reads DMI,
   not device-tree. Adding a second platform is a schema change first (§0c), not a reader.

## Non-goals

- No new monitoring, history, sparklines, or alerting.
- No new subprocess on the Dashboard path — with one candidate exception, `lspci`, which is
  open question 4. Everything else the left column shows is a file read or already in the
  launch budget (§0b-bis).
- No change to Backends/Models/Downloads/Deployments/Settings beyond removing dead shared CSS.

---

## Phase 0: Hard constraints & allowed sources

> Read this before writing any code in later phases. Every source below was verified against
> this repo or this venv. Anything not on this list must be verified before use — **do not
> assume a `/proc` or `/sys` path exists because it usually does.**

### 0a. Sources that are FORBIDDEN on the Dashboard path

Every one of these spawns a vendor tool that can wake a GPU. This is the fan ramp.

| Tool | Where it lives today | Status |
|---|---|---|
| `nvidia-smi` | `provision/steps/metrics.py:_read_nvidia_gpus`, `provision/steps/drivers.py:36,37` | metrics.py deleted; drivers.py stays, behind Settings → Check Drivers only |
| `xpu-smi` | `provision/steps/metrics.py:_discover_intel_devices`, `_read_intel_gpu_stats` | deleted with metrics.py |
| `vulkaninfo` | `provision/steps/drivers.py:68` | stays, behind Check Drivers only |
| `modinfo amdgpu`, `dpkg-query` | `provision/steps/drivers.py:47,55,79` | stays, behind Check Drivers only |

`smoke/verify_no_gpu_wake.py` already asserts none of these spawn at launch. Phase 4 tightens
it from "not at launch" to "not ever, outside Check Drivers".

### 0b. Static identity sources — verified stdlib

Checked in this venv (Python 3.14.6):

- `platform.freedesktop_os_release() -> dict` — parses `/etc/os-release`, falls back to
  `/usr/lib/os-release`, **raises `OSError` when neither exists**. Python 3.10+. Keys of
  interest: `PRETTY_NAME`, `NAME`, `VERSION_ID`.
- `os.uname()` — fields `sysname`, `nodename`, `release`, `version`, `machine`. `release` is
  the kernel version string, `machine` the arch. Note: it has **no** `node` attribute and no
  `_fields`; use `nodename` and the documented attribute names.
- `os.cpu_count()` and `os.sysconf("SC_NPROCESSORS_ONLN")` — logical CPU count.

### 0b-bis. Facts ALREADY in the launch budget

These cost nothing new — the Dashboard already calls them today, and `00e6a76` measured launch
at 6 operations. Phases 2 and 3 must reuse these, not add readers.

| Fact | Source | Cost |
|---|---|---|
| llama-swap installed version, unit active/enabled | `swap.status()` — `provision/steps/swap.py:229-237` → `llama-swap -version` + 2× `systemctl` | already called |
| Installed build ref per backend | `build.list_builds()` — `provision/steps/build.py:184-197` | **pure filesystem**, no command |
| Driver versions | `drivers.read_lockfile_status()` — `provision/steps/drivers.py:96-103` | **pure YAML read** |
| Pinned versions | `manifest.yaml`, loaded once at `cockpit/app.py:180` | already in memory |
| Container list | `docker.list_containers()` — `provision/steps/docker.py:27-58` | already called. **Raises `RuntimeError`** when docker is absent — must be caught |
| tailscale state + IP | `tailscale.status()` — `provision/steps/tailscale.py:38-59` | already called |
| WOL unit enabled, wake flags, actual MAC | `wol.status()` — `provision/steps/wol.py:181-196` | already called |
| TLP active / power profile | inline in `cockpit/screens/dashboard.py:489-520` | already called; move into a service row |
| Script unit state | `scripts.status()` — `provision/steps/scripts.py:81-84` | per script |

### 0c. Static identity sources — files (VERIFY ON TARGET)

**Platform scope: x86-64 Debian only.** This is not a preference, it is what the code already
enforces. `provision/schema.py:69-80` *requires* a non-empty `gpus` list where every entry has
`vendor ∈ {nvidia, amd, intel}` and `backends ∈ {cuda, rocm, vulkan, sycl}`. There is no
CPU-only backend in `manifest.yaml`. **A Raspberry Pi cannot be expressed in a host profile at
all** — it would fail validation before any screen rendered. Supporting one is a schema and
manifest change (a CPU-only backend, a relaxed vendor enum), not a device-tree reader, and it
is not in this plan.

So: **no `/proc/device-tree`, no Pi revision parsing.** Drop them; they were speculative.

The dev machine is macOS; none of these could be read here. **Phase 1 must verify each on the
real host before relying on it**, and every reader must degrade to `None` rather than raise.

| Fact | Path | Notes |
|---|---|---|
| Board model | `/sys/class/dmi/id/product_name`, `sys_vendor`, `board_name` | World-readable, unlike `dmidecode`, which needs root — **do not use `dmidecode`**. |
| CPU model | `/proc/cpuinfo` | Key is `model name` on x86-64. |
| RAM total | `/proc/meminfo` `MemTotal` (kB) | Existing reader: `provision/steps/metrics.py:66-70` (`read_mem`) — copy the parse, drop `MemAvailable`/used/percent. |

**Negative result, verified by exhaustive repo-wide grep**: nothing in this repo reads
`/etc/os-release`, `/proc/cpuinfo`, device-tree, `dmidecode`, or `lscpu` today. Every row in
this table except `MemTotal` is a **new** reader with **no existing precedent to copy**. That is
exactly why Phase 1 must verify each one on haupe-server before the plan advances.

### 0c-bis. GPU product names without waking anything

`lspci` is **not** a GPU vendor tool and does not wake a device. The repo already has a working
one-liner: `_LSPCI_CMD` — `cockpit/screens/settings.py:64-68` — which emits `gpu0: <name>` lines
from `lspci | grep -E "VGA compatible controller|3D controller"`, run via
`run_shell_capture()` (`cockpit/widgets.py:394-402`, 5s timeout, never raises).

This is the **only** way to get a GPU's marketing name ("NVIDIA GeForce RTX 5090") without
`nvidia-smi`. `/sys/bus/pci/devices/*/` carries numeric vendor/device IDs but no names — that
lookup is what `lspci` does.

**Cost**: one subprocess at launch, taking the budget from 6 to 7. See open question 4.

### 0d. Facts the toolkit already has WITHOUT reading anything

- `cockpit/__init__.py:__version__` = `"2026.09.01"`.
- `manifest.yaml` — pinned `llama_cpp.ref`, `llama_swap.version`, `huggingface_hub.version`.
- `hosts/<hostname>.yaml` — declared `gpus[]` (`id`, `vendor`, `backends[]`), `paths`,
  `network`, `service`, `retain_builds`, `hf`.
- `hosts/<hostname>.lock.yaml` via `drivers.read_lockfile_status()`
  (`provision/steps/drivers.py:96`) — **a pure YAML file read, no device touch.** Returns
  `None` when no lockfile exists. Schema, from `drivers.run` (`:106-121`):
  ```yaml
  generated_at: <iso8601>
  gpus:
    <gpu_id>:
      vendor: nvidia|amd|intel
      # nvidia (drivers.py:41):  driver_version, cuda_version
      # amd    (drivers.py:51,61): amdgpu_version, rocm_version
      # intel  (drivers.py:76,80,85): vulkan_driver_version, vulkan_api_version,
      #                               intel_opencl_icd_version (optional)
  ```
  This is the **only** approved source of driver versions for the left column.

### 0e. Layout and CSS facts

- `.res-row`, `.res-label`, `.res-val`, `.res-row ProgressBar`, `.res-row Bar > .bar--bar`,
  `.res-row Bar > .bar--complete`, `.res-row PercentageStatus` are all in `SHARED_CSS`
  (`cockpit/widgets.py`) and used **only** by `DashboardScreen`. They are deleted in Phase 4.
- The two-column split (`#dashboard-columns`, `.columns-responsive`, the
  `Screen.-wide DashboardScreen #dashboard-left` gutter rule) **stays** — the new layout is
  still two columns at ≥121 cells, stacked below.
- `cockpit/DESIGN.md` §8 Archetype A is written entirely around gauges. It is rewritten in
  Phase 5, not patched.

### 0f. Anti-patterns to guard against in every phase

- **Inventing a `/proc` or `/sys` path.** If Phase 1 cannot verify it on the target host, the
  reader returns `None` and the row renders `unknown` — it does not guess.
- **Shelling out for a fact a file already carries.** No `lscpu`, `lsb_release`, `uname -a`,
  `free`, `dmidecode`, `hostnamectl`. `os.uname()` and file reads cover all of it.
- **Reading driver versions live.** The lockfile is the source; if it is absent the row says
  "not checked — run Settings → Check Drivers". **Assume absent is the normal case**: the
  lockfile is gitignored (`.gitignore:13`), none exists in this checkout, and it is unknown
  whether haupe-server has ever been baselined. The no-lockfile rendering is the default path,
  not the edge case.
- **Re-adding a percentage anywhere on this screen.**

---

## Phase 1: `provision/steps/sysinfo.py`

**What to implement.** A new module of static host-identity readers. Pure reads, no `Runner`,
no `run()` — the same "sampling module" shape `metrics.py` had, minus the devices.

```python
def read_os() -> dict          # pretty_name, version_id, kernel, arch  (0b)
def read_cpu() -> dict         # model, cores                          (0b, 0c)
def read_memory_total() -> int # bytes                                 (0c)
def read_board() -> dict       # model, vendor, revision  — any may be None (0c)
def read_all() -> dict         # the four above, one call for the screen
```

**Copy from, do not invent:**
- `/proc/meminfo` parse: copy `provision/steps/metrics.py:read_mem` before deleting it, keep
  only the `MemTotal` branch.
- Degrade-don't-raise idiom: copy the module docstring convention and per-function
  `try/except → None` shape from `metrics.py`'s header.
- `list_interfaces()` in `provision/steps/wol.py` is the reference for a sysfs reader that
  returns `[]` on a host without the path, and for the `_SYSFS_NET` module constant that makes
  it testable off-target. **Use the same constant trick** for `/proc` and `/sys` roots.

**Verification checklist:**
- [ ] `.venv/bin/python -c "from provision.steps import sysinfo; print(sysinfo.read_all())"`
      runs on macOS without raising, returning `None`/`unknown` for everything.
- [ ] A fixture-tree test (copy the shape of the `wol.list_interfaces` test written for
      `_SYSFS_NET`) covers: DMI files present, DMI files absent, `model name` present and
      absent in cpuinfo, a `MemTotal`-less meminfo, and `/etc/os-release` missing entirely
      (`platform.freedesktop_os_release()` raises `OSError` — §0b).
- [ ] Run it **on haupe-server** and paste the real output into the phase's completion note.
      This is the only way to confirm 0c.
- [ ] `grep -rn "subprocess\|shutil.which" provision/steps/sysinfo.py` returns nothing.

**Anti-pattern guards:** no subprocess in this file, at all. No `sys.exit`
(`AGENTS.md` repository discipline #2). No caching — these are read once per refresh.

---

## Phase 2: Dashboard left column — System

**What to implement.** Replace the `Hardware & Resources` panel with a static `System` panel.

```
System
  Model        ASUS System Product Name (PRIME Z790-P)
  CPU          24 × 13th Gen Intel(R) Core(TM) i9-13900K
  Memory       64.0 GB
  OS           Debian GNU/Linux 13 (trixie)
  Kernel       6.12.9-amd64 (x86_64)
  Cockpit      v2026.09.01

Accelerators
  gpu-nvidia   nvidia · cuda, vulkan · driver 550.127.05, CUDA 12.4
  gpu-intel    intel · vulkan, sycl · driver not checked
```

Values above are illustrative. Phase 1's completion note must paste the **real** output from
haupe-server — the DMI strings in particular are frequently vendor junk ("System Product Name"
is a real, common value), and the panel has to look sane when they are.

- Accelerator rows come from `host_profile["gpus"]` (declared) joined to
  `drivers.read_lockfile_status()` (captured). A GPU with no lockfile entry renders
  `driver not checked` — never a blank and never a live query.
- Reuse `.form-row` / `.form-label` / `.form-field` (`SHARED_CSS`) for the label-value rows —
  they already solve the alignment this needs (DESIGN.md §8 Archetype C). Do **not** invent a
  new row class.

**Verification checklist:**
- [ ] `smoke/verify_screens.py` passes at 80×24 and 121×30.
- [ ] Launch trace shows **zero** new subprocesses vs. `00e6a76`'s baseline of 6.
- [ ] With no `.lock.yaml` present, every accelerator row reads "not checked" and nothing raises.

**Anti-pattern guards:** no `ProgressBar` import in `dashboard.py` after this phase. No
percentage. No call into `drivers._capture_*`.

---

## Phase 3: Dashboard right column — Services

**What to implement.** One standardized row per managed service: **name, then glyph, then
detail.**

```
LLM
  llama-swap              ✔  running · v255
  Endpoint                   100.95.76.0:8090
Scripts
  whisper-api             ✔  active, enabled
  tts-proxy               ✖  stopped, disabled
Containers
  open-webui              ✔  healthy
Host
  tailscaled              ✔  100.95.76.0
  wol-enp6s0              ✖  not enabled
  llama-swap-restart      ●  not managed
```

**Add one shared helper** to `cockpit/widgets.py` so every row renders identically:

```python
def service_row(name: str, state: bool | None, detail: str = "", width: int = 22) -> str:
    """`name` padded to `width`, then ✔/✖/● , then muted detail. `None` = unknown/unmanaged."""
```

The three glyphs and their theme colours already exist and must be preserved exactly as used
today (`cockpit/screens/dashboard.py:439-576`): `✔` `$success`, `✖` `$error`, `●`
`$warning` for an error state and `$text-muted` for not-managed.

**Existing status functions to call — do not write new ones.** The Phase 0 sweep names each
one's return keys; call them, do not re-implement:
`swap.status`, `scripts_step.status`, `docker.list_containers`, `tailscale.status`,
`wol.status`.

**Verification checklist:**
- [ ] `grep -n "✔\|✖\|●" cockpit/screens/dashboard.py` shows every glyph coming from
      `service_row`, none hand-built.
- [ ] Glyph column is at the same x on every row — assert on widget/text geometry, not by eye.
- [ ] A host with no scripts, no containers and no tailscale renders empty sections, not errors.

**Anti-pattern guards:** the glyph never leads a line. No `f"✔ {name}"` anywhere.

---

## Phase 4: Delete the measurement machinery

**What to delete:**
1. `provision/steps/metrics.py` — the whole file.
2. `DashboardScreen`: `_compute_metrics`, `_apply_metrics`, `_apply_gpu_sample`,
   `_sample_gpus`, `_match_live_gpu`, `_compose_gpu_slot`, `action_sample_gpus`,
   `on_button_pressed`, the `BINDINGS = [("g", ...)]`, `_last_gpu_slots`/`_last_gpu_sample`,
   and the `#btn-sample-gpus` / `#gpu-sample-status` widgets.
3. `SHARED_CSS`: `.res-row`, `.res-label`, `.res-val`, `.res-row ProgressBar`,
   `.res-row Bar > .bar--bar`, `.res-row Bar > .bar--complete`, `.res-row PercentageStatus`,
   and `DashboardScreen .res-row*` / `.gpu-unavailable` / `.gpu-bar-row` in
   `dashboard.py`'s `DEFAULT_CSS`.

**Retarget the guard.** `smoke/verify_no_gpu_wake.py` currently asserts "no GPU tool at
launch" and then that `Sample GPUs` takes exactly one reading. Both halves change:
- The `Sample GPUs` half is deleted with the button.
- The assertion strengthens from *at launch* to **the cockpit never spawns a GPU vendor tool
  at all**, across launch, every tab, the global refresh and idle. The only sanctioned caller
  left is `drivers.run()` behind Settings → Check Drivers, which the check must not exercise.
- Rename to `smoke/verify_no_gpu_wake.py` (keep the name — `AGENTS.md` and two commit messages
  reference it).

**Verification checklist:**
- [ ] `grep -rn "metrics" cockpit/ provision/ --include=*.py` returns nothing but unrelated words.
- [ ] `grep -rn "res-row\|res-val\|res-label\|ProgressBar" cockpit/` returns nothing.
- [ ] `.venv/bin/python -m pyflakes cockpit/ provision/ smoke/` is clean.
- [ ] `smoke/verify_no_gpu_wake.py` passes, and **fails** when a `nvidia-smi` call is
      temporarily added to any screen's refresh path. Prove both.

---

## Phase 5: Docs & final verification

**What to update:**
- `cockpit/DESIGN.md` §8 **Archetype A — rewrite, don't patch.** Its role line ("Real-time
  telemetry ... resource utilization monitoring"), the whole Resource Gauge Metrics block, the
  gauge-row/bar-track bullets and the GPU-telemetry bullet all describe a screen that no longer
  exists. New contract: static inventory left, standardized service rows right, **zero live
  measurement, zero controls**, and the `service_row` glyph-after-name rule.
- `cockpit/DESIGN.md` §4/§9 — check whether `service_row` belongs beside the existing
  archetypes.
- `AGENTS.md` — Scope & Architecture gains one line: monitoring is explicitly out of scope.
- `README.md` — grep for Dashboard/monitoring claims and correct them.
- Delete the `metrics.py` references in `cockpit/screens/dashboard.py`'s module docstring.

**Final verification (all must pass):**
```bash
.venv/bin/python -m py_compile cockpit/*.py cockpit/screens/*.py provision/*.py provision/steps/*.py
.venv/bin/python -m pyflakes cockpit/ provision/ smoke/
.venv/bin/python smoke/verify_screens.py
.venv/bin/python smoke/verify_no_gpu_wake.py
.venv/bin/python smoke/verify_runner_sudo.py
.venv/bin/python smoke/verify_rendered_units.py
.venv/bin/python -c "from provision import schema; from pathlib import Path; r=Path('.'); m=schema.load_manifest(r/'manifest.yaml'); h=schema.load_host_profile(r/'hosts/example.yaml'); schema.load_models(r/'models.example.yaml',h,m); print('schema OK')"
```
- [ ] Launch subprocess trace: **6 or fewer** operations, none duplicated (the `00e6a76`
      baseline — this plan must not add any).
- [ ] Run `bin/cockpit` on haupe-server and confirm the fans stay quiet. This is the
      acceptance test the whole plan exists for.
- [ ] Paste the real rendered Dashboard from haupe-server into the completion note.

---

## Drive-by found while planning (not in scope, flagged so it isn't lost)

`provision/schema.py:16-18` exports `HOST_PROFILE_SCHEMA`, `MANIFEST_SCHEMA` and
`MODELS_SCHEMA` in `__all__`, but **none of those names exists anywhere in the repo** —
validation is imperative (`_require` calls), not a declarative schema object. `from
provision.schema import *` would raise `AttributeError`. This is the pyflakes warning that has
been showing up in every verification run this session. One-line fix (delete the three names),
but it belongs in its own commit, not this plan.

## Open questions for the operator

1. **Container rows**: one row per running container, or a single `Docker  ✔  4/4 healthy`
   summary? Per-container matches "list the services"; the summary stays short on a busy host.
   Plan currently assumes **per-container**.
2. **Endpoint line**: it is a fact, not a service, so it has no meaningful ✔/✖. Plan currently
   renders it in the Services panel with a blank glyph column. It could equally sit in the left
   column under System.
3. **Upstream update check**: the Dashboard's "Upstream Updates" line is the only network call
   at launch (2 GitHub requests). It is not a service and not system identity. Keep it on the
   Dashboard, move it to Backends (which already has its own check), or drop it?
4. **GPU product names — is one `lspci` worth it?** Without it the accelerator rows can only
   show what `hosts/<hostname>.yaml` declares (`gpu-nvidia · nvidia · cuda, vulkan`) plus
   whatever the lockfile holds. With it they can show "NVIDIA GeForce RTX 5090" — which is
   much closer to the "exact hardware revision" you asked for, and `lspci` does not wake a GPU.
   Cost is one subprocess at launch (6 → 7) and a `bash -c` pipeline whose output shape has
   never been asserted by a test. **Recommendation: include it**, behind a reader that degrades
   to the declared id, and add a fixture test for the parse. Your call.
5. ~~**Raspberry Pi — is it a real target?**~~ **RESOLVED 2026-09-16: no.** The host schema
   cannot represent a Pi (§0c), and the Pi keeps running homestack, which is the tool for it.
   Device-tree reading is dropped from Phase 1.
