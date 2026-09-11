# llm-server-cockpit

Provisioning + a Textual TUI for a bare-metal local LLM inference stack —
llama.cpp (one build per GPU backend) and llama-swap — on a Debian-family
Linux host. Runs on the target machine itself, from a clean or
partially-configured state.

Bare metal, not containers, for anything GPU-adjacent — containers bundle
their own GPU userspace and fight the host driver stack. Everything is
version-pinned in `manifest.yaml`; there's no "install latest" anywhere.
Every mutating action is idempotent and runs through a dry-run gate.

## Install

```
git clone https://github.com/RefNull/llm-server-cockpit
cd llm-server-cockpit
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`bin/provision` and `bin/cockpit` auto-detect `.venv` and re-exec themselves
under it, so once it exists you run them directly — no need to activate it
or prefix with `.venv/bin/python`.

Then set up your host:

```
cp hosts/example.yaml hosts/$(hostname).yaml
cp models.example.yaml models.yaml
bin/cockpit   # Settings tab walks you through the rest, or edit the YAML directly
```

## Requirements

- Debian-family, apt-based — no assumptions about a specific release.
- Root, for everything except `--dry-run` (installs apt packages, writes
  under `/opt`, `/etc`, `/var/lib`).
- One or more GPUs from any mix of vendors — CUDA, ROCm, Vulkan, SYCL are
  all supported backends, bound per-model, per-device, in `models.yaml`.
- A Hugging Face token in an env var (named in your host profile) for model
  downloads. Never written to disk, never in the repo.
- Reachable only over your own VPN — the gateway binds to that interface's
  address specifically, never `0.0.0.0`.

## CLI

```
bin/provision --dry-run all   # preview every action, take none
bin/provision wol              # wake-on-LAN: enable, persist across reboot, verify
bin/provision drivers           # capture/verify GPU driver version lockfile — detects drift, never fixes it
bin/provision hf                 # HF auth + declarative model downloads
bin/provision build               # llama.cpp build per backend: smoke-tested, then atomic symlink swap
bin/provision swap                 # llama-swap install, config generation, systemd unit
bin/provision all                   # all of the above, in dependency order
```

`--host` defaults to the machine's own hostname; pass it to target a
different host profile.

## Cockpit

`bin/cockpit` — starts in dry-run. Toggle with `d`, refresh with `r`.

- **Installs** — per-backend build status, retained builds with one-click
  rollback, real build/smoke-test history, and a manual upstream-version
  check against the pinned llama.cpp/llama-swap versions (surfaces
  "update available", never applies one — bumping a pin stays your call).
- **Deploy** — add/edit/delete `models.yaml` entries with GPU/backend
  dropdowns constrained to what the host profile actually has, a
  config.yaml preview, and apply (install + restart llama-swap).
- **Downloads** — per-model Hugging Face download status and triggers, plus
  disk usage.
- **Settings** — host profile fields (network, paths, HF token env var
  name), WoL status and enable, driver-drift status, llama-swap's restart
  policy, and two optional systemd timers: scheduled restart and scheduled
  update checks (detect-only, same rule as the Installs tab). On first run,
  with no host profile yet, this is the only tab shown, and it creates one.

## Layout

```
bin/provision, bin/cockpit    entrypoints
provision/                     CLI + schema validation + steps (wol, drivers, hf, build, swap)
cockpit/                        Textual TUI, one screen per tab, calling into provision/steps
hosts/example.yaml               tracked template — no real facts
hosts/<hostname>.yaml            your real host profile — gitignored
models.example.yaml               tracked template
models.yaml                        your real model list — gitignored
manifest.yaml                       version pins + per-backend build recipes
smoke/smoke.yaml                     pinned model + prompt, smoke-tests every backend build
systemd/                             unit templates: llama-swap, scheduled restart, update-check timer
```

`hosts/<hostname>.yaml` and `models.yaml` hold real per-deployment facts —
hostnames, MACs, filesystem paths — so they're gitignored. Copy the
`.example` files to get your own.

## Scope

In scope: wake-on-LAN, driver drift detection, Hugging Face auth/downloads,
bare-metal multi-backend llama.cpp builds, llama-swap deployment. Out of
scope: other machines on the network, model selection/tuning, anything
above the model server. STT/TTS isn't built by this repo — `models.yaml`'s
`engine: unmanaged` type exists so an external service (docker or otherwise)
can still be listed in the generated config without this repo owning its
lifecycle; it's the seam a future bare-metal STT/TTS backend would slot
into.
