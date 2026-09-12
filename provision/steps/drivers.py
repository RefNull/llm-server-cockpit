"""GPU userspace driver lockfile + drift detection. Detection only — never upgrades/fixes a driver."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from provision.common import Runner

log = logging.getLogger("provision")

# These are read-only diagnostic queries (nvidia-smi/modinfo/vulkaninfo/dpkg-query), not builds
# or downloads — 30s is generous headroom for a hung driver/kernel module query without letting
# one wedge the cockpit's synchronous drift-check worker indefinitely.
_TIMEOUT_S = 30


def _run_ro(cmd: list[str]) -> str:
    """Read-only query helper — safe under --dry-run, output is used for comparison only."""
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=_TIMEOUT_S)
    if result.returncode != 0:
        raise RuntimeError(f"command failed ({' '.join(cmd)}): {result.stdout}")
    return result.stdout


def _capture_nvidia(gpu_id: str) -> dict[str, str]:
    if shutil.which("nvidia-smi") is None:
        sys.exit(f"drivers: gpu {gpu_id!r} is vendor=nvidia but nvidia-smi is not on PATH — cannot verify, refusing to skip")
    driver_version = _run_ro(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"]).strip()
    header = _run_ro(["nvidia-smi"])
    m = re.search(r"CUDA Version:\s*([\d.]+)", header)
    if not m:
        sys.exit(f"drivers: gpu {gpu_id!r} — could not parse CUDA Version out of `nvidia-smi` output")
    return {"driver_version": driver_version, "cuda_version": m.group(1)}


def _capture_amd(gpu_id: str) -> dict[str, str]:
    if shutil.which("modinfo") is None:
        sys.exit(f"drivers: gpu {gpu_id!r} is vendor=amd but modinfo is not on PATH — cannot verify, refusing to skip")
    modinfo_out = _run_ro(["modinfo", "amdgpu"])
    m = re.search(r"^version:\s*(\S+)", modinfo_out, re.MULTILINE)
    if not m:
        sys.exit(f"drivers: gpu {gpu_id!r} — could not parse `version:` out of `modinfo amdgpu` output")
    facts = {"amdgpu_version": m.group(1)}
    # rocm-libs chosen as the representative package for the installed ROCm stack version.
    if shutil.which("dpkg-query") is None:
        sys.exit(f"drivers: gpu {gpu_id!r} is vendor=amd but dpkg-query is not on PATH — cannot verify, refusing to skip")
    rocm_result = subprocess.run(
        ["dpkg-query", "-W", "-f=${Version}", "rocm-libs"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=_TIMEOUT_S,
    )
    if rocm_result.returncode != 0:
        sys.exit(f"drivers: gpu {gpu_id!r} — rocm-libs package not found via dpkg-query, cannot verify ROCm stack version")
    facts["rocm_version"] = rocm_result.stdout.strip()
    return facts


def _capture_intel(gpu_id: str) -> dict[str, str]:
    if shutil.which("vulkaninfo") is None:
        sys.exit(f"drivers: gpu {gpu_id!r} is vendor=intel and uses the Vulkan backend but vulkaninfo is not on PATH — apt install vulkan-tools")
    vk_out = _run_ro(["vulkaninfo", "--summary"])
    # Narrow fields only (matches nvidia/amd's pattern) — the full dump includes things like
    # device enumeration order that can vary run-to-run without any real driver change,
    # which would trip false-positive drift on cosmetic output differences.
    driver_m = re.search(r"driverVersion\s*=\s*(\S+)", vk_out)
    api_m = re.search(r"apiVersion\s*=\s*(\S+)", vk_out)
    if not driver_m:
        sys.exit(f"drivers: gpu {gpu_id!r} — could not parse `driverVersion` out of `vulkaninfo --summary` output")
    facts = {"vulkan_driver_version": driver_m.group(1)}
    if api_m:
        facts["vulkan_api_version"] = api_m.group(1)
    # intel-opencl-icd is optional — not every Intel GPU setup uses the compute runtime.
    icd_result = subprocess.run(
        ["dpkg-query", "-W", "-f=${Version}", "intel-opencl-icd"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=_TIMEOUT_S,
    )
    if icd_result.returncode == 0:
        facts["intel_opencl_icd_version"] = icd_result.stdout.strip()
    return facts


_CAPTURE = {"nvidia": _capture_nvidia, "amd": _capture_amd, "intel": _capture_intel}


def run(host_profile: dict[str, Any], manifest: dict[str, Any], models: dict[str, Any], runner: Runner, repo_root: Path) -> None:
    lock_path = repo_root / "hosts" / f"{host_profile['hostname']}.lock.yaml"

    captured: dict[str, dict[str, str]] = {}
    for gpu in host_profile["gpus"]:
        gpu_id, vendor = gpu["id"], gpu["vendor"]
        captured[gpu_id] = {"vendor": vendor, **_CAPTURE[vendor](gpu_id)}

    if not lock_path.exists():
        content = yaml.safe_dump(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "gpus": captured,
            },
            sort_keys=False,
        )
        log.info("drivers: no lockfile at %s — establishing new baseline", lock_path)
        runner.write_file(lock_path, content)
        return

    existing = yaml.safe_load(lock_path.read_text()) or {}
    existing_gpus = existing.get("gpus", {})

    drift: list[str] = []
    for gpu_id, facts in captured.items():
        old_facts = existing_gpus.get(gpu_id)
        if old_facts is None:
            drift.append(f"{gpu_id}: no prior entry in lockfile (new GPU?) — captured {facts}")
            continue
        for field, new_value in facts.items():
            old_value = old_facts.get(field)
            if old_value != new_value:
                drift.append(f"{gpu_id}.{field}: locked={old_value!r} actual={new_value!r}")

    if drift:
        sys.exit(
            "drivers: DRIFT DETECTED — driver/runtime versions no longer match "
            f"{lock_path}:\n  " + "\n  ".join(drift) +
            f"\nThis step never auto-corrects. If this drift is expected, hand-edit {lock_path} "
            "(a git-diffable, auditable act) and re-run."
        )

    log.info("drivers: verified — all captured GPU driver/runtime versions match %s", lock_path)
