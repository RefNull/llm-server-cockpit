"""Live host/GPU utilization reads for the cockpit's Dashboard tab — pure sampling, no
mutating actions, so unlike every other provision/steps/*.py module there's no run(). Every
function here is meant to be called as a one-shot read triggered by mounting or refreshing the
Dashboard tab (see cockpit/screens/dashboard.py's @work(thread=True) _refresh_all), never from
a recurring UI timer — commit 5c986a4 removed a 1Hz set_interval sampler after it kept an Intel
Arc GPU's sysman telemetry active continuously and was observed to ramp its fans to 100% within
moments of opening the app. Each function here is a single, fast, best-effort read that
degrades to "not available" rather than raising, since a transient failure here should never
interrupt a caller working through several of these in one pass.

GPU coverage: nvidia via `nvidia-smi` (full utilization+memory+power). Intel via `xpu-smi`:
memory/power/frequency only — as of the xpu-smi version this was written against (2.1.0,
2025-02-25), its utilization telemetry does not report real numbers, so utilization_pct is
always None for Intel. AMD is out of scope for now (no ROCm/amd-smi query implemented).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

_TIMEOUT_S = 5


# ---------------------------------------------------------------------------- CPU

def read_cpu_times() -> tuple[int, int]:
    """Raw (idle_jiffies, total_jiffies) from /proc/stat's aggregate 'cpu' line. Two
    consecutive readings are needed to compute a percentage — see cpu_percent_from_delta —
    which lines up naturally with a 1Hz sampling timer calling this once per tick."""
    with open("/proc/stat") as f:
        line = f.readline()
    fields = [int(x) for x in line.split()[1:]]
    # user, nice, system, idle, iowait, irq, softirq, steal, guest, guest_nice
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    total = sum(fields)
    return idle, total


def cpu_percent_from_delta(prev: tuple[int, int], curr: tuple[int, int]) -> float:
    prev_idle, prev_total = prev
    curr_idle, curr_total = curr
    total_delta = curr_total - prev_total
    idle_delta = curr_idle - prev_idle
    if total_delta <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * (1 - idle_delta / total_delta)))


# ---------------------------------------------------------------------------- Memory

def read_mem() -> dict[str, Any]:
    """MemAvailable (kernel's own "usable without swapping" estimate) rather than MemFree —
    matches what `free -h` calls "available" and is the number an operator actually cares
    about, not the free-but-reclaimable-cache number that makes a healthy host look starved."""
    values: dict[str, int] = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                values[key] = int(rest.strip().split()[0]) * 1024  # kB -> bytes
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    used = max(0, total - available)
    percent = (100.0 * used / total) if total else 0.0
    return {"used_bytes": used, "total_bytes": total, "percent": percent}


# ---------------------------------------------------------------------------- Disk

def read_disk(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {"used_bytes": 0, "total_bytes": 0, "percent": 0.0, "exists": False}
    usage = shutil.disk_usage(path)
    percent = (100.0 * usage.used / usage.total) if usage.total else 0.0
    return {"used_bytes": usage.used, "total_bytes": usage.total, "percent": percent, "exists": True}


# ---------------------------------------------------------------------------- GPU

def read_gpus() -> list[dict[str, Any]]:
    """Every nvidia/intel GPU currently visible to the respective vendor tool — independent
    of what host_profile declares, since this reports live hardware reality. Callers match
    these back to host_profile's declared GPU slots by vendor + ordinal position (there's no
    shared identifier between a hosts/*.yaml gpu entry and an nvidia-smi/xpu-smi device
    index)."""
    return _read_nvidia_gpus() + _read_intel_gpus()


def _read_nvidia_gpus() -> list[dict[str, Any]]:
    if shutil.which("nvidia-smi") is None:
        return []
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,power.draw",
                "--format=csv,noheader,nounits",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []

    gpus: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            gpus.append({
                "vendor": "nvidia",
                "index": int(parts[0]),
                "name": parts[1],
                "utilization_pct": float(parts[2]),
                "memory_used_mb": float(parts[3]),
                "memory_total_mb": float(parts[4]),
                "power_w": float(parts[5]) if parts[5] not in ("", "N/A") else None,
            })
        except ValueError:
            continue
    return gpus


_intel_device_ids_cache: list[str] | None = None


def _discover_intel_devices() -> list[str]:
    global _intel_device_ids_cache
    if _intel_device_ids_cache:
        return _intel_device_ids_cache
    try:
        result = subprocess.run(
            ["xpu-smi", "discovery", "-j"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=_TIMEOUT_S,
        )
        data = json.loads(result.stdout)
        device_list = data.get("device_list", data) if isinstance(data, dict) else data
        ids = [str(d.get("device_id", d.get("id"))) for d in device_list if isinstance(d, dict)]
        ids = [i for i in ids if i and i != "None"]
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError, AttributeError, TypeError):
        ids = []
    if ids:
        _intel_device_ids_cache = ids
    return ids


# Best-effort label matching over xpu-smi's stats output — deliberately not tied to one exact
# JSON schema or metric-ID numbering, both of which have shifted across xpu-smi versions.
# Verified against xpu-smi 2.1.0's *reported* behavior (memory/power/frequency real,
# utilization broken) secondhand, not against a live device — if this mismatches your actual
# output, the fix is a one-file change here, and every field degrades to None rather than
# crashing the sampler either way.
_INTEL_METRIC_PATTERNS = {
    "memory_used_mb": re.compile(r"memory\s*used[^0-9\-]*(-?[\d.]+)", re.IGNORECASE),
    "memory_total_mb": re.compile(r"memory\s*(?:physical\s*size|total)[^0-9\-]*(-?[\d.]+)", re.IGNORECASE),
    "power_w": re.compile(r"\bpower\b[^0-9\-]*(-?[\d.]+)", re.IGNORECASE),
    "frequency_mhz": re.compile(r"frequency[^0-9\-]*(-?[\d.]+)", re.IGNORECASE),
    "utilization_pct": re.compile(r"utilization[^0-9\-]*(-?[\d.]+)", re.IGNORECASE),
}


def _parse_intel_stats_text(text: str) -> dict[str, float | None]:
    values: dict[str, float | None] = {k: None for k in _INTEL_METRIC_PATTERNS}
    for line in text.splitlines():
        for key, pattern in _INTEL_METRIC_PATTERNS.items():
            if values[key] is not None:
                continue
            m = pattern.search(line)
            if m:
                try:
                    values[key] = float(m.group(1))
                except ValueError:
                    pass
    return values


# Keyword match against xpu-smi's JSON `metrics_type` enum labels (e.g.
# "XPUM_STATS_MEMORY_USED") — these carry no digits, so the numeric-capture regexes above (built
# for the text-table fallback, where label and number share a line) can never match them. This
# is a separate, purely lexical label->field mapping; the number itself always comes from the
# JSON entry's own "value" field, never from the label.
_INTEL_JSON_LABEL_KEYWORDS = {
    "memory_used_mb": ("memory_used",),
    "memory_total_mb": ("memory_physical_size", "memory_total"),
    "power_w": ("power",),
    "frequency_mhz": ("gpu_frequency", "frequency"),
    "utilization_pct": ("gpu_utilization", "utilization"),
}


def _parse_intel_stats_json(data: Any) -> dict[str, float | None]:
    values: dict[str, float | None] = {k: None for k in _INTEL_METRIC_PATTERNS}
    entries = data.get("device_level", data) if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return values
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("metrics_type") or entry.get("name") or "").lower()
        raw_value = entry.get("value")
        if raw_value is None or not label:
            continue
        for key, keywords in _INTEL_JSON_LABEL_KEYWORDS.items():
            if values[key] is None and any(kw in label for kw in keywords):
                try:
                    values[key] = float(raw_value)
                except (TypeError, ValueError):
                    pass
    return values


def _read_intel_gpu_stats(device_id: str) -> dict[str, float | None]:
    try:
        result = subprocess.run(
            ["xpu-smi", "stats", "-d", device_id, "-j"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=_TIMEOUT_S,
        )
        parsed = _parse_intel_stats_json(json.loads(result.stdout))
        if any(v is not None for v in parsed.values()):
            return parsed
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        pass
    # Fall back to the human-readable table if -j isn't supported or returned nothing useful.
    try:
        result = subprocess.run(
            ["xpu-smi", "stats", "-d", device_id],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=_TIMEOUT_S,
        )
        return _parse_intel_stats_text(result.stdout)
    except (subprocess.TimeoutExpired, OSError):
        return {k: None for k in _INTEL_METRIC_PATTERNS}


def _read_intel_gpus() -> list[dict[str, Any]]:
    if shutil.which("xpu-smi") is None:
        return []
    device_ids = _discover_intel_devices()
    gpus: list[dict[str, Any]] = []
    for i, device_id in enumerate(device_ids):
        stats = _read_intel_gpu_stats(device_id)
        gpus.append({
            "vendor": "intel",
            "index": i,
            "name": f"Intel GPU {device_id}",
            "utilization_pct": stats["utilization_pct"],  # expected None — see module docstring
            "memory_used_mb": stats["memory_used_mb"],
            "memory_total_mb": stats["memory_total_mb"],
            "power_w": stats["power_w"],
            "frequency_mhz": stats["frequency_mhz"],
        })
    return gpus
