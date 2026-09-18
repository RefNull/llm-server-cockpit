# llm-server-cockpit

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Textual](https://img.shields.io/badge/Textual-8.2%2B-teal.svg)](https://textual.textualize.io/)
[![llama.cpp](https://img.shields.io/badge/llama.cpp-b10903-orange.svg)](https://github.com/ggml-org/llama.cpp)
[![llama-swap](https://img.shields.io/badge/llama--swap-v255-purple.svg)](https://github.com/mostlygeek/llama-swap)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

`llm-server-cockpit` is a declarative provisioning toolchain and Textual TUI cockpit for managing bare-metal LLM inference stacks on Debian-family Linux hosts.

It orchestrates multi-backend `llama.cpp` compilation (CUDA, ROCm, Vulkan, SYCL), dynamic model routing and VRAM management via `llama-swap`, hardware Wake-on-LAN persistence, and GPU driver drift detection across a unified control plane.

## Architecture Overview

`llm-server-cockpit` decouples high-performance inference execution on bare metal from operational management, providing dual headless CLI and interactive terminal cockpit control planes:

- **Dynamic Inference Gateway (`llama-swap`)**: Systemd-supervised reverse proxy that loads model backends into VRAM on demand and evicts idle instances based on inactivity timeouts (`ttl`), routing inference traffic over a private VPN interface (`port 8090`).
- **Multi-Backend Compilations (`llama.cpp`)**: Dedicated, version-pinned `llama-server` binaries compiled from source for each target GPU architecture (CUDA, ROCm, Vulkan, SYCL) and verified via real-inference smoke tests before atomic symlink activation.
- **Unmanaged Services Seam**: Gateway routing for pre-existing or containerized auxiliary services (e.g. STT/TTS engines via Docker) without imposing compilation lifecycle ownership.
- **Dual Management Plane**:
  - **Headless CLI (`bin/provision`)**: Non-interactive command-line runner for automated setup, CI/CD pipelines, and scriptable provisioning with mandatory dry-run safety gates.
  - **Interactive TUI Cockpit (`bin/cockpit`)**: Full-screen Textual operator cockpit for visual hardware inspection, live model catalog deployment, one-click compilation rollback, and download monitoring.

```
                              +---------------------------------------------------+
                              |             Clients (Private Network)             |
                              |    Tailscale / WireGuard VPN  •  API Gateways     |
                              +-------------------------+-------------------------+
                                                        |
                                                        |  HTTP Requests (:8090)
                                                        v
                              +---------------------------------------------------+
                              |            llama-swap (systemd daemon)            |
                              |    Dynamic Model Swapping  •  Process Supervisor  |
                              |    VRAM Eviction / TTL     •  Port Orchestration  |
                              +----+--------------------+--------------------+----+
                                   |                    |                    |
                +------------------+                    |                    +------------------+
                |                                       |                                       |
                v                                       v                                       v
     +--------------------+                  +--------------------+                  +--------------------+
     | llama-server (CUDA)|                  |llama-server(Vulkan)|                  | Unmanaged Service  |
     |   /opt/.../cuda    |                  |  /opt/.../vulkan   |                  |  Speaches STT/TTS  |
     | NVIDIA RTX / Hopper|                  |  Intel Arc / iGPU  |                  |  Docker Container  |
     +---------+----------+                  +---------+----------+                  +---------+----------+
               |                                        |                                       |
===============|========================================|=======================================|==================
[Hardware]     v                                        v                                       v
       +---------------+                        +---------------+                       +---------------+
       |  NVIDIA GPU   |                        |   Intel GPU   |                       |  Docker Host  |
       | (Driver Lock) |                        | (Vulkan/OpenCL|                       | (Socket Mgmt) |
       +---------------+                        +---------------+                       +---------------+
===================================================================================================================
[Management]
               +----------------------------------------+---------------------------------------+
               |                                        |                                       |
               v                                        v                                       v
     +-------------------+                   +--------------------+                  +--------------------+
     |    Declarative    |                   |    Headless CLI    |                  |  Interactive TUI   |
     |  Configurations   |                   |  (bin/provision)   |                  |   (bin/cockpit)    |
     |  manifest.yaml    | <---------------> |  Dry-run / CI/CD   | <--------------> |  Textual Cockpit   |
     |  hosts/*.yaml     |                   |  Idempotent steps  |                  |  Live compilation  |
     |  models.yaml      |                   |  Systemd & builds  |                  |  Rollback & deploy |
     +-------------------+                   +--------------------+                  +--------------------+
```

## Prerequisites

- **Operating System**: Bare-metal Linux (Debian 12 "Bookworm", Ubuntu 24.04+ LTS, or apt-based derivatives).
  - *Bare-Metal Rationale*: GPU-accelerated inference requires direct, unmediated communication with host kernel modules and GPU userspace runtimes. Containerizing GPU compute stacks bundles driver userspaces that collide with the host kernel driver. Non-GPU auxiliary workloads (such as STT/TTS) route cleanly via Docker using the unmanaged engine seam.
- **Privileges**: Root / `sudo` required for live mutations (`/opt`, `/etc/systemd/system`, `/var/lib`, `apt-get`). Dry-run previews (`--dry-run`) run safely unprivileged.
- **System Utilities**:
  - `build-essential`, `cmake`, `git`, `curl` (compilation and asset acquisition)
  - `ethtool`, `iproute2` (`ip`) (Wake-on-LAN verification and persistent arming)
  - `pciutils` (`lspci`) (PCIe hardware bus device enumeration)

### Hardware Acceleration Matrix

| Backend | Manifest Key | CMake Compilation Flags | Host Dependencies & Utilities | Target Hardware |
| :--- | :--- | :--- | :--- | :--- |
| **CUDA** | `cuda` | `-DGGML_CUDA=ON`, `-DGGML_VULKAN=OFF`, `-DGGML_NATIVE=OFF`, `-DLLAMA_BUILD_TESTS=OFF` | NVIDIA Driver, CUDA Toolkit (`nvidia-smi`) | NVIDIA GeForce, RTX, Tesla, Hopper |
| **ROCm** | `rocm` | `-DGGML_HIP=ON`, `-DLLAMA_BUILD_TESTS=OFF` | AMD ROCm stack, `rocm-libs`, `amdgpu` (`hipconfig`) | AMD Radeon, Radeon Pro, Instinct |
| **Vulkan** | `vulkan` | `-DGGML_VULKAN=ON`, `-DGGML_NATIVE=ON`, `-DLLAMA_BUILD_TESTS=OFF` | `libvulkan-dev`, `glslc`, `spirv-headers` (`vulkaninfo`) | Intel Arc / iGPU, cross-vendor GPUs |
| **SYCL** | `sycl` | `-DGGML_SYCL=ON`, `-DCMAKE_C_COMPILER=icx`, `-DLLAMA_BUILD_TESTS=OFF` | Intel oneAPI Base Toolkit (`setvars.sh`, `icx`/`icpx`) | Intel Data Center GPU Flex/Max, Arc |

`-DCMAKE_BUILD_TYPE=Release` is injected by the build step and is not repeated in any recipe. `-DGGML_NATIVE` is deliberately asymmetric — `ON` bakes `-march=native` into the binary, which is free performance on a fixed bare-metal host and wrong if that binary is ever copied to different hardware.

**These recipes ship marked `example: true`.** The Installs tab renders such a backend as `EX. llama.cpp (<backend>)` — "still stock, nobody has tailored this" — and editing a recipe's `cmake_flags` from that tab's detail view clears the mark. Adding a recipe here makes that backend immediately bindable in `hosts/<hostname>.yaml` `gpus[].backends` and in Settings → GPU Topology; both validate against these keys rather than a hardcoded list.

## Installation

```bash
git clone https://github.com/RefNull/llm-server-cockpit.git
cd llm-server-cockpit
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`bin/provision` and `bin/cockpit` automatically inspect `.venv/lib/python3.*/site-packages` and inject it into `sys.path`. This bypasses Debian's patched virtual environment symlink behaviors, allowing entrypoints to be invoked directly without explicit `source .venv/bin/activate`.

Initialize local deployment configurations from templates:

```bash
cp hosts/example.yaml hosts/$(hostname).yaml
cp models.example.yaml models.yaml
```

## Quickstart & Operating Modes

### 1. Interactive TUI Cockpit (`bin/cockpit`)

Launch the Textual management cockpit:

```bash
bin/cockpit
# Or target a specific host profile:
bin/cockpit --host llm-host

# Run the whole cockpit as root instead of elevating per action. The header then shows
# a SUDO MODE badge. Default (no flag) keeps unprivileged work unprivileged — an HF
# download under sudo leaves root-owned files in models_dir.
sudo bin/cockpit          # or: bin/cockpit --sudo
```

Cockpit starts in **dry-run mode** by default to prevent accidental mutations. Key controls:
- `d`: Toggle dry-run safety gate (`DRY RUN` vs `LIVE`).
- `r`: Reload model catalog and refresh all screen data.
- `q`: Quit cockpit.

#### Cockpit Screens

- **Dashboard**: Static system inventory — board, CPU, memory, OS, kernel and the accelerators declared in the host profile with their recorded driver versions — beside a standardised list of deployed services and whether each is running. Read-only, no controls. Deliberately **not** a monitor: no CPU/RAM/GPU utilization, live or on demand. Host monitoring belongs to a monitoring stack.
- **First Setup / Settings**: Bootstraps a new host profile if missing. Configures network bindings, displays live VPN IPv4 resolution, inspects Wake-on-LAN hardware state, audits GPU driver drift against lockfiles, and configures systemd restart policies and scheduled timers.
- **Installs**: Two sections, each one or two lines. *Llama-swap* shows its version, update status, binary path and systemd unit — with an update action beside the status, and "(see process)" jumping to System → Systemd. Upstream is re-checked when the tab is shown, cached 15 minutes; there is no manual check button. *Llama.cpp* shows the pinned version and one row per declared backend — its active build, and a **per-backend** status — `up to date`, `pinned at <ver>`, or `out of date, built on <ver>` — which answers whether *that* build was compiled from the selected version, not whether upstream has moved. Per-row Build / Edit / Builds actions. Each build gets its own directory (`<prefix_root>/<backend>/build<N>`) carrying the config it was built with and its log, so a rebuild never overwrites its predecessor; the Builds list activates, inspects or removes them, and `current` is an atomic symlink flip.
- **Deploy**: Model catalog management (`models.yaml`). Form inputs constrain GPU and backend selection strictly to hardware declared in the host profile. Features real-time `config.yaml` syntax preview, staging validation, and atomic service restart.
- **Downloads**: View Hugging Face download status, trigger asynchronous GGUF snapshot downloads, verify checksums, and monitor storage utilization under `paths.models_dir`.

### 2. Headless CLI Provisioning (`bin/provision`)

Execute idempotent provisioning steps non-interactively for automated deployments:

```bash
# Preview every action without executing mutations
bin/provision --dry-run all

# Execute the complete pipeline in dependency order
sudo bin/provision all

# Execute individual lifecycle steps
sudo bin/provision wol       # Wake-on-LAN: configure MAC, persist systemd unit, override TLP
sudo bin/provision drivers   # GPU driver lockfile baseline or drift audit
sudo bin/provision hf        # Dedicated HF venv, token verification, model downloads
sudo bin/provision build     # Multi-backend compilation, smoke verification, symlink swap
sudo bin/provision swap      # llama-swap install, config generation, systemd unit deployment
```

#### Provisioning Steps

| Step | Dependency | Description |
| :--- | :--- | :--- |
| `wol` | *None* | Verifies configured MAC, arms Wake-on-LAN via `ethtool`, installs `wol-<iface>.service`, and suppresses TLP/NetworkManager sleep overrides. |
| `drivers` | `wol` | Captures baseline GPU driver versions into `hosts/<host>.lock.yaml` or checks for unvetted system driver drift (aborts on mismatch; never auto-upgrades). |
| `hf` | `drivers` | Establishes dedicated isolated virtualenv (`venv-hf`), verifies non-persisting HF token authentication, and downloads declared GGUF models. |
| `build` | `hf` | Clones pinned `llama.cpp` ref, builds versioned binary prefixes per backend, executes real-inference smoke tests, and atomically pivots the `current` symlink. |
| `swap` | `build` | Installs pinned `llama-swap` binary, generates and stages `config.yaml`, validates via `llama-swap -validate`, deploys systemd unit, and triggers service restart. |

## Declarative Configuration

### 1. Software Manifest (`manifest.yaml`)

Defines upstream release pins and per-backend compilation recipes. Bumping a version here is the only sanctioned path to update inference binaries — by hand, or from the cockpit's Installs tab ("Update to latest" / "Change version…"), which write this same file through a targeted, comment-preserving line rewrite (never a full YAML re-dump) and are themselves confirmed before they touch it. Either way the pin stays tracked by git, so a version change has an author and a diff:

```yaml
llama_cpp:
  ref: 481c65f091f74c5e7089dd0a3a1cc6b50cced31e  # b10903
  repo: https://github.com/ggml-org/llama.cpp

llama_swap:
  version: v255
  repo: https://github.com/mostlygeek/llama-swap

backends:
  cuda:
    cmake_flags:
      - "-DGGML_CUDA=ON"
      - "-DGGML_NATIVE=OFF"
    apt_packages: []
  vulkan:
    cmake_flags:
      - "-DGGML_VULKAN=ON"
    apt_packages: [libvulkan-dev, glslc, spirv-headers]
```

### 2. Host Profile (`hosts/<hostname>.yaml`)

Gitignored machine profile specifying hardware topology, network bindings, and local filesystem layout:

```yaml
hostname: llm-host

network:
  vpn:
    interface: tailscale0     # Gateway binds to dynamic IP resolved from this NIC
  wol:
    interface: enp5s0         # Physical interface receiving magic packets
    mac: "d8:43:ae:01:23:45"
  gateway:
    port: 8090
    health_check_timeout: 120

gpus:
  - id: gpu-nvidia
    vendor: nvidia
    backends: [cuda]
  - id: gpu-intel
    vendor: intel
    backends: [vulkan]

paths:
  models_dir: /srv/models/gguf
  state_dir: /var/lib/llm-server-cockpit
  prefix_root: /opt/llm-server-cockpit/builds

retain_builds: 3
hf:
  token_env: HF_TOKEN
```

**Where builds actually live.** Compiled backends go under `paths.prefix_root` only — `<prefix_root>/<backend>/build<N>`, one directory per build, activated by an atomic `current` symlink once a real-inference smoke test passes. Each directory holds `build-info.json` (the ref built, the exact cmake flags and resolved argv used, outcome) and `build.log`, so what a build was made of survives the build. `retain_builds` is a disk budget over *installed* builds — failed attempts are kept separately and cheaply, and never evict a working build. This toolkit never reads or writes `/opt/llama.cpp`, and the per-backend rows in the Installs tab are the backends declared in `gpus[].backends` above, **not filesystem discoveries**. A hand-built tree elsewhere on disk is reported read-only in a note beneath that table; it is never adopted, built, pruned, or executed.

### 3. Model Catalog (`models.yaml`)

Gitignored model registry mapping identifiers to engines, quant files, GPU backends, and invocation parameters:

```yaml
models:
  # Managed llama.cpp backend
  - id: qwen3.8
    engine: llama-cpp
    repo_id: "ISTA-DASLab/Qwen3.8-27B-GSQ-RCO-GGUF"
    quant_file: Qwen3.8-27B-GSQ-RCO-IQ3_S-mtp.gguf
    bind:
      gpu: gpu-intel
      backend: vulkan
    llama_server_args:
      - "--ctx-size"
      - "262144"
      - "--cache-type-k"
      - "q4_0"
      - "--flash-attn"
    env:
      - "VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/intel_icd.x86_64.json"
    ttl: 0

  # Unmanaged Docker container service
  - id: speaches-stt-tts
    engine: unmanaged
    cmd: |
      docker run --rm -p ${PORT}:8000 --gpus device=0 \
      -v /srv/models/speaches-cache:/home/ubuntu/.cache/huggingface/hub \
      speaches-ai/speaches:latest-cuda
    ttl: 0
    group: always-on
```

## Operational Lifecycle & Systemd Services

The provisioning toolchain generates and supervises four systemd units:

1. **Inference Gateway (`llama-swap.service`)**:
   Main reverse proxy daemon. The listening address is resolved dynamically from `network.vpn.interface` (e.g. `100.x.y.z:8090`), strictly preventing exposure to public interfaces. Configured with `Restart=on-failure`, `RestartSec=5s`, and `LimitNOFILE=65536`.
2. **Scheduled Restart (`llama-swap-restart.timer` & `.service`)**:
   Optional periodic timer executing `systemctl restart llama-swap.service` (e.g. `OnCalendar=daily`) to recycle memory allocations, clear GPU fragmentation, and re-initialize drivers.
3. **Scheduled Update Checker (`llm-server-cockpit-update-check.timer` & `.service`)**:
   Periodic timer executing `bin/check-updates` (e.g. `OnCalendar=daily`). Queries GitHub APIs for upstream `llama.cpp` and `llama-swap` releases and records results to `update-check-result.json`. Detect-and-surface only: never modifies pins or triggers builds.
4. **Wake-on-LAN Persistence (`wol-<interface>.service`)**:
   Oneshot unit executing `/usr/sbin/ethtool -s <iface> wol g` on both startup and shutdown (`RemainAfterExit=yes`, `ExecStop=...`). Ensures NICs (such as Realtek RTL8126A) that lose WOL register states during cold ACPI S5 poweroff remain wakeable. Overrides TLP power profiles (`/etc/tlp.d/01-wol.conf`) and NetworkManager connection profiles.

## Testing & Smoke Verification

### Syntax & Schema Validation

Verify Python syntax and declarative configuration schemas offline:

```bash
# Verify Python bytecode syntax across all modules
python3 -m py_compile cockpit/*.py cockpit/screens/*.py provision/*.py provision/steps/*.py

# Validate declarative schemas against example profiles
python3 -c "from provision import schema; from pathlib import Path; r = Path('.'); m = schema.load_manifest(r/'manifest.yaml'); h = schema.load_host_profile(r/'hosts/example.yaml'); schema.load_models(r/'models.example.yaml', h, m); print('OK')"
```

### Real-Inference Smoke Verification (`smoke/smoke.yaml`)

Every compiled `llama.cpp` backend binary must pass a live inference smoke test before its prefix is activated:

- **Fixture Model**: Lightweight official instruction model (`Qwen/Qwen2.5-0.5B-Instruct-GGUF`, file `qwen2.5-0.5b-instruct-q4_k_m.gguf`). Downloaded once per host and cached in `state_dir/smoke-model/`.
- **Execution Test**: Invokes the freshly built `llama-cli` with greedy decoding (`--temp 0`), one-shot prompt evaluation (`-no-cnv`), and full GPU offload (`-ngl 999`) to force execution through the compiled backend compute kernels:
  ```bash
  llama-cli -m <model> -p "Q: What is the capital of France?\nA:" -n 32 -no-cnv --temp 0 -ngl 999
  ```
- **Gate Criterion**: The test asserts exit code `0` and validates that `Paris` appears in the generated text.
- **Safety Guarantee**: If inference fails or times out (300s), the `current` symlink is left pointing to the previous working build, the failure is recorded in that build's own `build-info.json` alongside its `build.log`, and deployment terminates before touching the running service.

## Security & Privilege Model

- **Sudo / Root Boundaries**:
  - `bin/provision` requires root privileges for mutating actions to write to `/opt/llm-server-cockpit`, `/var/lib/llm-server-cockpit`, `/etc/systemd/system`, and manage systemd daemons.
  - `--dry-run` operations run unprivileged, executing read-only commands (`git rev-parse`, `ip addr`, `ethtool`, `vulkaninfo`) to format output without taking mutating actions.
- **Daemon Privilege Rationale**:
  - `llama-swap.service` runs as `root` by design. This allows `llama-swap` to manage child process execution limits, bind to system ports, and interface directly with the Docker daemon socket (`/var/run/docker.sock`) to launch and terminate containerized STT/TTS auxiliary models (`engine: unmanaged`) without nested `sudo` friction.
- **Network Isolation**:
  - The inference gateway binds exclusively to the IPv4 address assigned to the private VPN interface (`network.vpn.interface`, e.g. Tailscale or WireGuard). It strictly refuses to bind to `0.0.0.0` or fall back to public NICs.
- **Secret & Token Hygiene**:
  - Hugging Face API tokens are resolved strictly from the environment variable named in `hf.token_env` (e.g. `$HF_TOKEN`).
  - Non-interactive validation calls `whoami(token=...)` via the Python API, deliberately bypassing `huggingface-cli login` to avoid persisting credentials to `$HF_HOME/token` on disk. Tokens are never committed, logged, or written to systemd service definitions.

## Upstream Lineage & Attributions

### `kyuz0/ai-toolbox-cockpit`
- **Repository**: [https://github.com/kyuz0/ai-toolbox-cockpit](https://github.com/kyuz0/ai-toolbox-cockpit)
- **Author**: Jesper (`kyuz0`)
- **Attribution Notice**:
  `llm-server-cockpit` draws its core TUI architecture concept, Textual screen workflow patterns, and hardware cockpit operational paradigms from `kyuz0/ai-toolbox-cockpit` by Jesper (`kyuz0`). While `llm-server-cockpit` is a clean-room, declarative implementation engineered for bare-metal Linux hosts with multi-backend hardware acceleration, the user interface structure, screen workflow paradigms, and operator cockpit concept are directly inspired by Jesper's work. Sincere appreciation is extended to the author for pioneering this interactive approach to local LLM server management.

### Inference Infrastructure & Tooling
- **[`llama.cpp`](https://github.com/ggml-org/llama.cpp)** (Georgi Gerganov and contributors) - High-performance LLM inference engine.
- **[`llama-swap`](https://github.com/mostlygeek/llama-swap)** (mostlygeek) - Dynamic model swapping reverse proxy.
- **[`textual`](https://github.com/Textualize/textual)** (Textualize) - Terminal application framework.

## License

This project is licensed under the [MIT License](LICENSE).  
For third-party dependencies, upstream licenses, and system utility notices, see [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
