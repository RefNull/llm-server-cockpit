# Agent Entry Point

This file defines the operating conventions, repository discipline, and architectural rules for AI agents working in `llm-server-cockpit`.

---

## Conventions

Root holds only entry points, uppercase, because harnesses look for them by exact
name. Everything inside `agents/` is lowercase. `CLAUDE.md` is a one-line pointer to
this file; add equivalents for other tools the same way rather than copying content —
duplicated rules drift, see `agents/rationale.md` §9. **`handlers.md` records the exact
wiring per harness**, including which have a real import and which need a symlink.

**Size budget: keep any file a harness loads under 12,000 characters.** That is
Antigravity's hard per-rule-file limit. Other tools are more permissive, so 12,000 is the
binding number for anything meant to be portable.

Keep this file short. An entry point that grows into a second rules document becomes
a duplicate of `agents/contract.md`, and one of them will go stale.

Project-specific facts — build commands, deployment steps, domain constraints — go
below, never mixed into the routing above.

---

## Project

### Scope & Architecture
- **Bare-metal execution**: Bare metal on Linux (Debian-family), not containers, for anything GPU-adjacent (CUDA, ROCm, Vulkan, SYCL). Containers bundle driver runtimes that conflict with the host driver stack.
- **Dual planes**:
  - Headless CLI (`bin/provision`) for automated, declarative setup and CI/CD.
  - Interactive Textual TUI (`bin/cockpit`) for manual operator inspection, model rollout, and live compilation.
- **Not a monitor**: the cockpit deploys and supervises; it does not measure host utilization. No CPU/RAM/GPU telemetry, live or on demand — reading GPU telemetry wakes the device and cost two fan-ramp incidents. Host monitoring belongs to a monitoring stack. Home-server services (DNS, media, shares, backups) are out of scope too; that is a separate toolkit's job, split by workload rather than by hardware.
- **Inference supervision**: `llama.cpp` builds per backend and `llama-swap` reverse proxy managed via `systemd`. Unmanaged external services (e.g. containerized STT/TTS) route via `engine: unmanaged`.

### Repository Discipline
1. **Idempotence & dry-run safety**: Every mutating operation in `provision/` must support dry-run (`--dry-run`) and be strictly idempotent. Preview actions before executing.
2. **Layer decoupling**: Never call `sys.exit()` inside reusable core or schema modules (`provision/schema.py`). Raise descriptive exceptions (e.g. `ValidationError`) so CLI entrypoints can format exit codes while UI callers (`cockpit/`) catch and display errors gracefully.
3. **Declarative configuration & zero drift**: Upstream component versions and compilation recipes ship templated in `manifest.example.yaml`. Backend recipes in the tracked template are repo-level; the per-deployment copy (`manifest.yaml`) is the host's, and editing recipes there via the TUI editor is expected. Deployment configurations belong in `manifest.yaml`, `hosts/` and `models.yaml`.
4. **Secret & environmental hygiene**: Never commit deployment facts, tokens, or private endpoints. `hosts/<hostname>.yaml`, `models.yaml` and `manifest.yaml` must remain gitignored; use `hosts/example.yaml`, `models.example.yaml` and `manifest.example.yaml` as templates. Hugging Face tokens are resolved via environment variables and never persisted to disk.
5. **Character budget**: All harness rule files must adhere to Antigravity's strict `< 12,000` character limit.
6. **TUI design standards**: Interactive screens (`cockpit/screens/*.py`) must adhere to layout, token, breakpoint, and modal contracts defined in `cockpit/DESIGN.md`.

### Key Commands
- **Environment setup**:
  ```bash
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
  ```
- **CLI provisioning (dry-run preview)**:
  ```bash
  bin/provision --dry-run all
  ```
- **Interactive TUI Cockpit**:
  ```bash
  bin/cockpit
  ```
- **Syntax verification**:
  ```bash
  python3 -m py_compile cockpit/*.py cockpit/screens/*.py provision/*.py provision/steps/*.py
  ```
- **Rendered-artifact verification** (every generated systemd unit + llama-swap config.yaml):
  ```bash
  .venv/bin/python smoke/verify_rendered_units.py
  ```
- **GPU fan-ramp non-regression** (launch must not wake any GPU):
  ```bash
  .venv/bin/python smoke/verify_no_gpu_wake.py
  ```
- **Runner privilege-escalation verification**:
  ```bash
  .venv/bin/python smoke/verify_runner_sudo.py
  ```
- **Schema & configuration validation**:
  ```bash
  python3 -c "from provision import schema; from pathlib import Path; r = Path('.'); m = schema.load_manifest(r/'manifest.example.yaml'); h = schema.load_host_profile(r/'hosts/example.yaml'); schema.load_models(r/'models.example.yaml', h, m); print('OK')"
  ```
