"""Real-inference smoke test helper, shared by steps/build.py (and steps/swap.py if it ever
needs to re-verify a binary post-swap — not currently exercised there).

Loads the pinned fixture in smoke/smoke.yaml, ensures the tiny smoke-test GGUF is cached
locally (reusing hf.py's venv-hf rather than a second HF client mechanism), and runs a real
one-shot completion through the freshly built backend binary, asserting a known substring
shows up in the output. "It loaded" is not a passing test — this actually generates text.
"""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from provision.common import Runner

log = logging.getLogger("provision")

SMOKE_TIMEOUT_S = 300


def load_smoke_fixture(repo_root: Path) -> dict[str, Any]:
    path = repo_root / "smoke" / "smoke.yaml"
    if not path.exists():
        sys.exit(f"build: no smoke-test fixture at {path}")
    with open(path) as f:
        fixture = yaml.safe_load(f)
    required = {"repo_id", "quant_file", "prompt", "expected_substring", "max_tokens"}
    missing = required - fixture.keys()
    if missing:
        sys.exit(f"{path}: missing required field(s): {sorted(missing)}")
    return fixture


def ensure_smoke_model(host_profile: dict[str, Any], fixture: dict[str, Any], runner: Runner) -> Path:
    """Download (once, cached) the smoke-test GGUF via the hf.py-owned venv. Never invents a
    second huggingface_hub install — build depends on hf in the step dependency order anyway.
    """
    state_dir = Path(host_profile["paths"]["state_dir"])
    dest_dir = state_dir / "smoke-model"
    dest = dest_dir / fixture["quant_file"]
    if dest.exists():
        return dest

    if runner.dry_run:
        log.info("build: smoke model not cached — nothing to download under --dry-run")
        return dest

    runner.mkdir(dest_dir)
    venv_python = state_dir / "venv-hf" / "bin" / "python"
    if venv_python.exists():
        download = (
            "from huggingface_hub import hf_hub_download; "
            f"hf_hub_download(repo_id={fixture['repo_id']!r}, filename={fixture['quant_file']!r}, "
            f"local_dir={str(dest_dir)!r})"
        )
        runner.run([str(venv_python), "-c", download])
    else:
        # Fallback to direct curl download from Hugging Face when provision hf has not been run
        url = f"https://huggingface.co/{fixture['repo_id']}/resolve/main/{fixture['quant_file']}"
        log.info("build: venv-hf not present; downloading smoke model directly from %s", url)
        runner.run(["curl", "-fsSL", "-o", str(dest), url])

    if dest.exists():
        runner.run(["chmod", "0644", str(dest)])
    return dest


def _smoke_argv(binary: Path, model: Path, fixture: dict[str, Any]) -> list[str]:
    # -no-cnv: llama-cli defaults to interactive chat mode for instruct models, which never
    # exits on its own — force plain one-shot completion instead. --temp 0: greedy decoding,
    # so the expected_substring check isn't at the mercy of sampling noise. -ngl 999: force
    # full GPU offload. Without this, a CUDA/Vulkan/SYCL-built binary can still default to
    # CPU-only inference — which would mean this test never touches the actual backend kernels
    # it exists to catch silently-wrong implementations of. Harmless on a backend with no GPU
    # layers to offload.
    return [
        str(binary),
        "-m", str(model),
        "-p", fixture["prompt"],
        "-n", str(fixture["max_tokens"]),
        "-no-cnv",
        "--temp", "0",
        "-ngl", "999",
    ]


def run_smoke_test(
    binary: Path,
    model: Path,
    fixture: dict[str, Any],
    *,
    source_script: str | None = None,
) -> tuple[bool, str]:
    """Real subprocess invocation of the built backend binary — always runs (not gated by
    --dry-run) since it mutates nothing; callers decide whether to skip it based on whether
    there's anything to test yet.
    """
    argv = _smoke_argv(binary, model, fixture)
    prefix = binary.parent.parent
    lib_dirs = [str(prefix / "lib"), str(prefix / "lib64"), str(binary.parent)]
    existing_ld = os.environ.get("LD_LIBRARY_PATH", "")
    combined_ld = ":".join(lib_dirs) + (f":{existing_ld}" if existing_ld else "")
    env = {**os.environ, "LD_LIBRARY_PATH": combined_ld}

    try:
        if source_script:
            # sycl backends need the oneAPI runtime env (LD_LIBRARY_PATH etc.) sourced to even
            # start the binary, same reason build.py's configure+build goes through a shell.
            script = f"export LD_LIBRARY_PATH={shlex.quote(combined_ld)} && source {source_script} && " + " ".join(shlex.quote(a) for a in argv)
            proc = subprocess.run(
                ["bash", "-c", script],
                env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=SMOKE_TIMEOUT_S,
            )
        else:
            proc = subprocess.run(
                argv,
                env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=SMOKE_TIMEOUT_S,
            )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {SMOKE_TIMEOUT_S}s"
    except OSError as e:
        return False, f"failed to execute {binary}: {e}"

    output = proc.stdout or ""
    if proc.returncode != 0:
        return False, f"exit code {proc.returncode}, output:\n{output}"
    if fixture["expected_substring"] not in output:
        return False, f"expected substring {fixture['expected_substring']!r} not found, output:\n{output}"
    return True, output
