"""Static host-identity reads for the cockpit's Dashboard tab — pure sampling, no mutating
actions, no `run()`, same shape `provision/steps/metrics.py` had before its live-utilization
readers were deleted (see plans/04-dashboard-inventory.md). Every function here answers "what
machine is this", never "what is it doing right now": a file read or a stdlib call, nothing
that can wake a device.

Each function degrades to `None` (or `[]` for a list) rather than raising, including on a host
with no `/proc` or `/sys` at all — the dev machine is macOS, and `read_all()` must run there
cleanly. No caching: these are read once per refresh, by the caller, not by this module.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

_TIMEOUT_S = 5

# Module-level so a test can point these at a fixture tree; this host's real /proc and /sys are
# the only values they ever take in production. Same trick as wol.py's _SYSFS_NET.
_PROC = Path("/proc")
_SYS = Path("/sys")


def read_os() -> dict[str, Any]:
    """PRETTY_NAME from /etc/os-release (falls back to /usr/lib/os-release), kernel release
    and machine arch from uname. `freedesktop_os_release()` raises OSError when neither file
    exists — the normal case on this dev box (macOS) — and isn't parameterizable by a module
    constant the way a file path is, so it's covered by the real run on macOS rather than a
    fixture."""
    pretty_name = None
    try:
        pretty_name = platform.freedesktop_os_release().get("PRETTY_NAME")
    except OSError:
        pass
    kernel = None
    arch = None
    try:
        uname = os.uname()
        kernel = uname.release
        arch = uname.machine
    except (AttributeError, OSError):
        pass
    return {"pretty_name": pretty_name, "kernel": kernel, "arch": arch}


# AMD puts the physical core count inside the model string ("AMD Ryzen 7 7800X3D 8-Core
# Processor"), Intel does not. Left in, the Dashboard renders "16 x AMD Ryzen 7 7800X3D 8-Core
# Processor" — the 16 is threads and the 8 is cores, so the line states two different counts
# and looks like a bug. Observed on llm-host, 2026-09-16.
_CORE_SUFFIX_RE = re.compile(r"\s+\d+-Core Processor\s*$", re.IGNORECASE)


def read_cpu() -> dict[str, Any]:
    """`model name` from /proc/cpuinfo (x86-64 only — see plan §0c) and logical core count
    from os.cpu_count(), which works on every platform including macOS."""
    model = None
    try:
        with open(_PROC / "cpuinfo") as f:
            for line in f:
                key, sep, rest = line.partition(":")
                if sep and key.strip() == "model name":
                    model = _CORE_SUFFIX_RE.sub("", rest.strip()) or None
                    break
    except OSError:
        pass
    return {"model": model, "cores": os.cpu_count()}


def read_memory_total() -> int | None:
    """MemTotal from /proc/meminfo, kB -> bytes. Copied from metrics.py:read_mem's parse —
    that reader also kept MemAvailable for a used/percent computation; this one drops both,
    the Dashboard is static identity now, not a live gauge."""
    try:
        with open(_PROC / "meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key == "MemTotal":
                    return int(rest.strip().split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def read_board() -> dict[str, Any]:
    """Board identity from sysfs DMI — world-readable, unlike dmidecode, which needs root and
    is never used here (plan §0c)."""

    def _read(name: str) -> str | None:
        try:
            value = (_SYS / "class" / "dmi" / "id" / name).read_text().strip()
        except OSError:
            return None
        return value or None

    return {"model": _read("product_name"), "vendor": _read("sys_vendor")}


# Anchored on the class name and everything after its colon, e.g.
# "00:02.0 VGA compatible controller: NVIDIA Corporation ... (rev a1)" -> the NVIDIA text.
# NOT a split on the Nth colon: lspci prints a domain prefix ("0000:00:02.0") whenever the host
# has more than one PCI domain, which shifts the colon count and would otherwise return
# "00.0 VGA compatible controller: NVIDIA Corporation ..." as the product name. Deliberately
# just the two class names the plan calls for, not every PCI display class lspci knows about.
_GPU_LINE_RE = re.compile(r"(?:VGA compatible controller|3D controller)\s*:\s*(.+)$")
_REV_SUFFIX_RE = re.compile(r"\s*\(rev [0-9a-fA-F]+\)\s*$")


def read_pci_gpus() -> list[str]:
    """GPU marketing names via `lspci` — not a GPU vendor tool, does not wake a device (plan
    §0c-bis). argv-list subprocess, no shell pipeline; parsing happens here in Python so it's
    fixture-testable, unlike the bash one-liner this replaces
    (cockpit/screens/settings.py:_LSPCI_CMD). Returns [] whenever lspci is absent, times out,
    or exits non-zero — never raises."""
    if shutil.which("lspci") is None:
        return []
    try:
        result = subprocess.run(
            ["lspci"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []

    names: list[str] = []
    for line in result.stdout.splitlines():
        match = _GPU_LINE_RE.search(line)
        if match is None:
            continue
        name = _REV_SUFFIX_RE.sub("", match.group(1).strip())
        if name:
            names.append(name)
    return names


def read_all() -> dict[str, Any]:
    """One call for the Dashboard screen — the four static-identity readers plus GPU product
    names, all degrading independently so one missing fact never blocks the rest."""
    return {
        "os": read_os(),
        "cpu": read_cpu(),
        "memory_total_bytes": read_memory_total(),
        "board": read_board(),
        "pci_gpus": read_pci_gpus(),
    }
