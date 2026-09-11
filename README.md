# llm-server-cockpit

Provisions and updates a local LLM inference stack — llama.cpp (bare metal,
one build per GPU backend) + llama-swap — on a Debian-family Linux host, from
clean or partially-configured state. Runs on the target machine itself.

## Design

- **Bare metal, not containers**, for anything GPU-adjacent. Containers
  bundle their own GPU userspace and fight the host driver stack.
- **Everything version-pinned** in `manifest.yaml`. No "install latest."
  Updating means editing a pin.
- **Idempotent, `--dry-run` everywhere.** Safe to re-run on a live machine.
  One deliberate exception: `provision build --dry-run` still runs the real
  smoke test (real GPU inference, up to a few minutes) against any backend
  that was already built in a prior real run — it's genuinely read-only, and
  more useful as a live diagnostic than skipped, but it does mean dry-run
  isn't always fast.
- **Atomic switchover with rollback.** Build into a versioned prefix per
  backend, smoke-test it, then flip a symlink. Previous builds are retained.
- **Driver drift is detected, never auto-corrected.** `provision drivers`
  fails loudly on drift against a committed lockfile; it never upgrades or
  fixes anything.
- **No secrets in the repo.** Hugging Face auth comes from an environment
  variable named in the host profile, never a file.
- **Private-network only.** The gateway binds to the host's VPN interface
  address, resolved at config-generation time — never `0.0.0.0`.

## Layout

```
bin/provision                executable entrypoint (CLI)
bin/cockpit                   executable entrypoint (TUI)
provision/                    python package (cli, schema validation, steps)
cockpit/                        python package (Textual TUI over provision/steps/*)
hosts/example.yaml               template host profile — TRACKED by git, contains no real facts
hosts/<hostname>.yaml            your real host profile — gitignored, never committed
hosts/<hostname>.lock.yaml       generated: observed GPU driver versions — gitignored
manifest.yaml                     version pins + per-backend build recipes
models.example.yaml                template model list — TRACKED by git
models.yaml                         your real model list — gitignored, never committed
smoke/smoke.yaml                     pinned tiny model + prompt used to smoke-test every backend build
systemd/                             unit template for llama-swap
```

`hosts/<hostname>.yaml` and `models.yaml` hold real, per-deployment facts —
hostnames, MACs, filesystem paths — and are gitignored on purpose (see
`.gitignore`). Copy the `.example` files to get started; git will never see
your copies.

## Setup

`provision/` and `cockpit/` need `PyYAML`, `jsonschema`, and (for the TUI) `textual` —
pinned in `requirements.txt`, matching `manifest.yaml`'s `textual` pin. One dedicated venv
at `.venv` in the repo root (separate from `venv-hf`, which `provision hf` creates on its
own for the Hugging Face side):

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`bin/provision` and `bin/cockpit` auto-detect `.venv` at the repo root and re-exec
themselves under it — once it exists, run them directly (`bin/provision ...`,
`bin/cockpit`), no need to prefix with `.venv/bin/python` or activate it.

## Usage

```
cp hosts/example.yaml hosts/$(hostname).yaml   # fill in the TODOs — see below
cp models.example.yaml models.yaml             # fill in the TODOs — see below

bin/provision --dry-run all      # preview every action, take none
bin/provision wol                 # wake-on-LAN: enable + persist + verify
bin/provision drivers              # capture/verify GPU driver version lockfile
bin/provision hf                    # HF CLI + auth + declarative model downloads
bin/provision build                  # llama.cpp build per backend, smoke-tested, atomic swap
bin/provision swap                    # llama-swap install + config + systemd unit
bin/provision all                      # all of the above, in dependency order

bin/cockpit                             # interactive TUI over the same steps — starts in dry-run
```

Requires root (installs apt packages, writes under `/opt`, `/etc`,
`/var/lib`). `--host` (both `provision` and `cockpit`) defaults to the
machine's own hostname — pass it explicitly to target a different host
profile.

## Before first run

These are real gaps in the example files, not oversights — left as
placeholders rather than guessed values. Fill in on your own copies:

- `hosts/<hostname>.yaml`: VPN provider/interface, WoL NIC/MAC — all `TODO`.
- `models.yaml`: any model whose `repo_id` you haven't sourced yet.
- `manifest.yaml`: `huggingface_hub` version pin is a `TODO` (not researched
  — check PyPI before first run).
- Export the token named by your host profile's `hf.token_env`
  (`HF_TOKEN`) before running `provision hf` or the cockpit's Downloads tab.

## Out of scope

Other machines on the network. Model selection and tuning. Orchestration
and frontend layers above the model server. STT/TTS (`speaches-stt-tts` in
`models.yaml` is carried over as an unmanaged passthrough entry only, so
config generation doesn't drop it — see the `engine: unmanaged` note in
`provision/schema.py`) — scoped so it can slot in later as pinned, bare-metal
components (Piper, faster-whisper, Chatterbox) without restructuring.
