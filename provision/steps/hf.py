"""Hugging Face CLI venv, non-interactive auth, declarative model downloads."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from provision.common import Runner

log = logging.getLogger("provision")


def _venv_python(venv_path: Path) -> Path:
    return venv_path / "bin" / "python"


def _installed_hub_version(venv_path: Path) -> str | None:
    result = subprocess.run(
        [str(venv_path / "bin" / "pip"), "show", "huggingface_hub"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("Version:"):
            return line.split(":", 1)[1].strip()
    return None


def _ensure_venv(venv_path: Path, runner: Runner) -> None:
    if _venv_python(venv_path).exists():
        log.info("hf: venv already present at %s", venv_path)
        return
    log.info("hf: creating venv at %s", venv_path)
    runner.run(["python3", "-m", "venv", str(venv_path)])


def _ensure_hub_pinned(venv_path: Path, pinned_version: str, runner: Runner) -> None:
    if runner.dry_run and not _venv_python(venv_path).exists():
        log.info("hf: [dry-run] would install huggingface_hub[cli]==%s into new venv", pinned_version)
        return
    current = _installed_hub_version(venv_path)
    if current == pinned_version:
        log.info("hf: huggingface_hub already pinned at %s", pinned_version)
        return
    log.info("hf: installing huggingface_hub[cli]==%s (currently %s)", pinned_version, current)
    runner.run([str(venv_path / "bin" / "pip"), "install", f"huggingface_hub[cli]=={pinned_version}"])


def _check_auth(venv_path: Path, token: str, runner: Runner) -> None:
    # whoami(token=...) rather than login(token=...): login() persists the token to
    # $HF_HOME/token on disk (that's its whole job) — a real, if repo-external, violation of
    # "no secrets written to disk". whoami() takes the token as a plain call argument and
    # never writes anything; every download call below passes the same token explicitly
    # instead of relying on that persisted cache. Using the Python API at all (not the CLI)
    # is still deliberate: the CLI's subcommand/flags have moved across releases, same risk
    # class as llama-swap's `-config` default changing — the API is the stable surface.
    snippet = (
        "import os\n"
        "from huggingface_hub import whoami\n"
        "whoami(token=os.environ['HF_TOKEN_TO_USE'])\n"
    )
    env = {**os.environ, "HF_TOKEN_TO_USE": token}
    runner.run([str(_venv_python(venv_path)), "-c", snippet], env=env)


def _download(venv_path: Path, repo_id: str, filename: str, local_dir: str, token: str, runner: Runner) -> None:
    snippet = (
        "import os\n"
        "from huggingface_hub import hf_hub_download\n"
        f"hf_hub_download(repo_id={repo_id!r}, filename={filename!r}, local_dir={local_dir!r}, "
        "token=os.environ['HF_TOKEN_TO_USE'])\n"
    )
    env = {**os.environ, "HF_TOKEN_TO_USE": token}
    # hf_hub_download is itself idempotent (skips re-download of a matching local copy);
    # runner.run's own dry-run gating covers "log what would be downloaded, don't invoke it".
    runner.run([str(_venv_python(venv_path)), "-c", snippet], env=env)


def auth_configured(host_profile: dict[str, Any]) -> bool:
    """Read-only — for the cockpit's Downloads tab to show an auth warning without logging in."""
    return bool(os.environ.get(host_profile["hf"]["token_env"]))


def model_status(model: dict[str, Any], host_profile: dict[str, Any]) -> str:
    """One of: 'downloaded', 'missing', 'placeholder' (repo_id not filled in), 'unmanaged'."""
    if model.get("engine") != "llama-cpp":
        return "unmanaged"
    if model["repo_id"].startswith("TODO"):
        return "placeholder"
    path = Path(host_profile["paths"]["models_dir"]) / model["quant_file"]
    return "downloaded" if path.exists() else "missing"


def download_model(model: dict[str, Any], host_profile: dict[str, Any], runner: Runner) -> None:
    """Download a single llama-cpp model's quant (+ mmproj if present). Assumes the venv is
    already provisioned (run() does that); reads the token itself so this is independently
    callable — the cockpit's per-model download button calls this directly, not via run().
    Callers are responsible for calling model_status() first to skip placeholder repo_ids
    rather than attempting and failing loudly on every click."""
    token_env = host_profile["hf"]["token_env"]
    token = os.environ.get(token_env)
    if not token:
        sys.exit(f"hf: environment variable {token_env!r} is not set — export it with a Hugging Face token first")
    venv_path = Path(host_profile["paths"]["state_dir"]) / "venv-hf"
    models_dir = host_profile["paths"]["models_dir"]
    repo_id = model["repo_id"]
    _download(venv_path, repo_id, model["quant_file"], models_dir, token, runner)
    if "mmproj_file" in model:
        _download(venv_path, repo_id, model["mmproj_file"], models_dir, token, runner)


def run(host_profile: dict[str, Any], manifest: dict[str, Any], models: dict[str, Any], runner: Runner, repo_root: Path) -> None:
    venv_path = Path(host_profile["paths"]["state_dir"]) / "venv-hf"
    _ensure_venv(venv_path, runner)
    _ensure_hub_pinned(venv_path, manifest["huggingface_hub"]["version"], runner)

    token_env = host_profile["hf"]["token_env"]
    token = os.environ.get(token_env)
    if not token:
        sys.exit(f"hf: environment variable {token_env!r} is not set — export it with a Hugging Face token before running `provision hf`")

    _check_auth(venv_path, token, runner)

    models_dir = host_profile["paths"]["models_dir"]
    runner.mkdir(models_dir)

    skipped: list[str] = []
    for model in models["models"]:
        if model.get("engine") != "llama-cpp":
            continue
        status = model_status(model, host_profile)
        if status == "placeholder":
            log.warning("hf: model %r has a placeholder repo_id (%r) — skipping download", model["id"], model["repo_id"])
            skipped.append(model["id"])
            continue
        download_model(model, host_profile, runner)

    if skipped:
        sys.exit(
            "hf: the following models have placeholder repo_ids and were not downloaded — "
            f"fill in models.yaml before re-running: {', '.join(skipped)}"
        )
