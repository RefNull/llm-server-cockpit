"""Read-only check of upstream llama.cpp/llama-swap versions against manifest.yaml's pins.

Never writes anything, never bumps a pin — same philosophy as driver-drift detection in
provision/steps/drivers.py: this is detection only. Bumping a pin and rebuilding stays a
manual, reviewed act the operator triggers from the Installs tab (or `provision build`
directly) after looking at what this reports.

Unauthenticated GitHub API calls (60 req/hr/IP) — fine for a check triggered by opening the
cockpit or hitting refresh, not a tight poll loop. Must never crash the app: any failure
(no network, rate-limited, VPN-only host with no general internet egress) degrades to an
{"ok": False, "error": ...} result the UI can render as "couldn't check" rather than blow up.
"""
from __future__ import annotations

import json
import os
import platform
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

import yaml

_TIMEOUT_S = 10
_CACHE_TTL_S = 24 * 60 * 60
_CACHE_PATH = Path(__file__).resolve().parent.parent / "hosts" / "upstream.update-cache.yaml"

# A second, independent TTL for the Backends tab's automatic per-view check (plans/07 Phase 2)
# — deliberately NOT `_CACHE_TTL_S`. That constant is shared with `bin/check-updates` and its
# systemd timer (see check_all's own docstring); shortening it globally would make the
# scheduled checker hit GitHub 96x more often for no reason a human ever asked for. 15 minutes
# is short enough that switching tabs away and back within a session reads as "fresh", long
# enough that repeatedly re-opening the tab in the same few minutes costs nothing extra.
_VIEW_CACHE_TTL_S = 15 * 60


def _read_full_cache() -> dict[str, Any]:
    """The whole cache file as a dict — check_all's "checked_at"/"results" plus, since Phase 2,
    a "releases" section keyed by repo path. One file, one TTL constant, shared by both;
    _write_cache used to overwrite the file wholesale, which would have silently dropped
    whichever section it didn't know about."""
    try:
        data = yaml.safe_load(_CACHE_PATH.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_full_cache(data: dict[str, Any]) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(yaml.safe_dump(data, sort_keys=False))
    except OSError:
        pass  # a cache that cannot be written is a slower app, not a broken one


def _read_cache() -> dict[str, Any] | None:
    """Last result, if it is still fresh. Unauthenticated GitHub allows 60 requests/hour/IP and
    this repo makes two calls per check — opening the Backends tab a few times in a session was
    enough to start getting 403s, which then rendered as "couldn't check" and looked like a
    network fault rather than a self-inflicted rate limit."""
    data = _read_full_cache()
    checked_at = data.get("checked_at")
    if not isinstance(checked_at, (int, float)) or time.time() - checked_at > _CACHE_TTL_S:
        return None
    results = data.get("results")
    return results if isinstance(results, dict) else None


def _write_cache(results: dict[str, Any]) -> None:
    # Merge, not overwrite: a bare {"checked_at", "results"} write would clobber the
    # "releases" section on the very next version check.
    data = _read_full_cache()
    data["checked_at"] = time.time()
    data["results"] = results
    _write_full_cache(data)


def cache_age_seconds() -> float | None:
    """How old the stored result is, for the UI to say so. None when there is none."""
    checked_at = _read_full_cache().get("checked_at")
    return time.time() - checked_at if isinstance(checked_at, (int, float)) else None


_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "llm-server-cockpit"}


def _get_json(url: str) -> Any:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        return json.load(resp)


def _repo_path(repo_url: str) -> str:
    return repo_url.rstrip("/").removeprefix("https://github.com/")


def _read_releases_cache(repo_path: str) -> list[dict[str, Any]] | None:
    entry = (_read_full_cache().get("releases") or {}).get(repo_path)
    if not isinstance(entry, dict):
        return None
    checked_at = entry.get("checked_at")
    if not isinstance(checked_at, (int, float)) or time.time() - checked_at > _CACHE_TTL_S:
        return None
    items = entry.get("items")
    return items if isinstance(items, list) else None


def _write_releases_cache(repo_path: str, items: list[dict[str, Any]]) -> None:
    data = _read_full_cache()
    releases = data.get("releases")
    if not isinstance(releases, dict):
        releases = {}
    releases[repo_path] = {"checked_at": time.time(), "items": items}
    data["releases"] = releases
    _write_full_cache(data)


def check_releases(repo_url: str, *, force: bool = False, limit: int = 20) -> dict[str, Any]:
    """Recent GitHub releases for `repo_url` — the "upstream releases" source for BuildsScreen's
    Change version… picker (plans/05 Phase 2 item 4). Same 24h cache and TTL as check_all,
    same never-raise contract: {"ok": True, "releases": [...]} or {"ok": False, "error": ...}.
    Each release item is {"tag_name", "published_at"}.
    """
    repo = _repo_path(repo_url)
    if not force:
        cached = _read_releases_cache(repo)
        if cached is not None:
            return {"ok": True, "releases": cached}
    try:
        data = _get_json(f"https://api.github.com/repos/{repo}/releases?per_page={limit}")
    except Exception as e:  # network down, rate-limited, VPN-only host — never crash the UI
        return {"ok": False, "error": str(e)}
    releases = [
        {
            "tag_name": r.get("tag_name"),
            "published_at": r.get("published_at"),
            "name": r.get("name") or r.get("tag_name"),
            "assets": [
                {
                    "name": a.get("name"),
                    "size": a.get("size", 0),
                    "browser_download_url": a.get("browser_download_url"),
                }
                for a in r.get("assets", [])
                if isinstance(a, dict) and a.get("name")
            ],
        }
        for r in data if isinstance(r, dict)
    ]
    _write_releases_cache(repo, releases)
    return {"ok": True, "releases": releases}


def fetch_release_by_tag(repo_url: str, tag: str) -> dict[str, Any]:
    """Fetch metadata for a single specific GitHub release tag.
    Returns {"ok": True, "release": {...}} or {"ok": False, "error": str}.
    """
    repo = _repo_path(repo_url)
    clean_tag = tag.strip()
    try:
        r = _get_json(f"https://api.github.com/repos/{repo}/releases/tags/{clean_tag}")
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if not isinstance(r, dict):
        return {"ok": False, "error": "invalid response from GitHub API"}
    release = {
        "tag_name": r.get("tag_name") or clean_tag,
        "published_at": r.get("published_at"),
        "name": r.get("name") or r.get("tag_name") or clean_tag,
        "assets": [
            {
                "name": a.get("name"),
                "size": a.get("size", 0),
                "browser_download_url": a.get("browser_download_url"),
            }
            for a in r.get("assets", [])
            if isinstance(a, dict) and a.get("name")
        ],
    }
    return {"ok": True, "release": release}


_LINUX_ARCH_MAP = {
    "x86_64": "x64",
    "amd64": "x64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


def detect_host_arch() -> str:
    m = platform.machine().lower()
    return _LINUX_ARCH_MAP.get(m, m)


def detect_host_cuda_version() -> str | None:
    """Safely detect installed CUDA version from /usr/local/cuda* paths without waking GPU."""
    for p in (Path("/usr/local/cuda"), Path("/usr/local/cuda-12"), Path("/usr/local/cuda-13")):
        try:
            if p.is_symlink():
                target = os.readlink(p)
                m = re.search(r"cuda-([0-9.]+)", target)
                if m:
                    return m.group(1)
        except OSError:
            pass
    try:
        cand: list[str] = []
        for d in Path("/usr/local").glob("cuda-*"):
            if d.is_dir() and not d.is_symlink():
                m = re.match(r"^cuda-([0-9.]+)$", d.name)
                if m:
                    cand.append(m.group(1))
        if cand:
            cand.sort(key=lambda s: [int(x) for x in s.split(".") if x.isdigit()], reverse=True)
            return cand[0]
    except OSError:
        pass
    return None


def parse_asset_info(name: str) -> dict[str, Any] | None:
    """Parse release asset filename into metadata (backend, arch, cuda_version, is_cudart)."""
    if not name.endswith(".tar.gz"):
        return None
    if any(k in name for k in ("win", "macos", "android")):
        return None

    is_cudart = name.startswith("cudart-")
    clean = name.removeprefix("cudart-")

    m = re.match(r"^llama-([^-]+)-bin-(?:ubuntu-)?(.+)\.tar\.gz$", clean)
    if not m:
        m = re.match(r"^llama-([^-]+)-bin-(.+)\.tar\.gz$", clean)
        if not m:
            return None

    tag = m.group(1)
    rest = m.group(2)

    arch = "x64" if "x64" in rest else ("arm64" if "arm64" in rest else ("s390x" if "s390x" in rest else "unknown"))

    backend = "cpu"
    cuda_ver = None
    if "cuda" in rest:
        backend = "cuda"
        cm = re.search(r"cuda-([0-9.]+)", rest)
        if cm:
            cuda_ver = cm.group(1)
    elif "vulkan" in rest:
        backend = "vulkan"
    elif "rocm" in rest:
        backend = "rocm"
    elif "sycl" in rest:
        backend = "sycl"
    elif "openvino" in rest:
        backend = "openvino"

    label = f"CUDA {cuda_ver}" if cuda_ver else backend.upper()
    return {
        "name": name,
        "tag": tag,
        "arch": arch,
        "backend": backend,
        "cuda_version": cuda_ver,
        "is_cudart": is_cudart,
        "label": label,
    }


def parse_release_assets(assets: list[dict[str, Any]], host_arch: str | None = None) -> list[dict[str, Any]]:
    """Filter and parse assets for Linux, pairing CUDA assets with companion cudart."""
    target_arch = host_arch or detect_host_arch()
    cudart_map: dict[str, dict[str, Any]] = {}
    runnable_assets: list[dict[str, Any]] = []

    for a in assets:
        name = a.get("name", "")
        info = parse_asset_info(name)
        if not info:
            continue
        entry = {**a, **info}
        if entry["is_cudart"]:
            # e.g. key by base name (without "cudart-")
            cudart_map[name.removeprefix("cudart-")] = entry
        else:
            runnable_assets.append(entry)

    # Attach companion cudart
    for r in runnable_assets:
        r["companion_cudart"] = cudart_map.get(r["name"])

    # Prioritize matching host architecture
    runnable_assets.sort(
        key=lambda x: (
            0 if x.get("arch") == target_arch else 1,
            0 if x.get("backend") == "cuda" else (1 if x.get("backend") == "vulkan" else 2),
            x.get("name", ""),
        )
    )
    return runnable_assets


def find_recommended_asset(
    assets: list[dict[str, Any]],
    host_profile: dict[str, Any] | None = None,
    target_backend: str | None = None,
) -> dict[str, Any] | None:
    """Identify the recommended asset for this host hardware & architecture."""
    host_arch = detect_host_arch()
    matching_arch = [a for a in assets if a.get("arch") == host_arch]
    if not matching_arch:
        matching_arch = assets

    detected_cuda = detect_host_cuda_version() or "12.8"

    if target_backend:
        if target_backend == "cuda":
            for a in matching_arch:
                if a.get("backend") == "cuda" and a.get("cuda_version") == detected_cuda:
                    return a
            for a in matching_arch:
                if a.get("backend") == "cuda":
                    return a
        else:
            for a in matching_arch:
                if a.get("backend") == target_backend:
                    return a
        return matching_arch[0] if matching_arch else None

    # No explicit target backend: inspect host GPUs
    gpus = (host_profile or {}).get("gpus", [])
    has_nvidia = any(
        "nvidia" in (g.get("vendor", "") + " " + g.get("model", "")).lower()
        for g in gpus
    )
    if has_nvidia:
        for a in matching_arch:
            if a.get("backend") == "cuda" and a.get("cuda_version") == detected_cuda:
                return a
        for a in matching_arch:
            if a.get("backend") == "cuda":
                return a

    # Try vulkan
    for a in matching_arch:
        if a.get("backend") == "vulkan":
            return a

    # Fallback to cpu
    for a in matching_arch:
        if a.get("backend") == "cpu":
            return a

    return matching_arch[0] if matching_arch else None


def fallback_asset_url(repo_url: str, tag: str, asset_name: str) -> str:
    """Generate predictable GitHub release download URL when API is rate-limited."""
    repo = repo_url.rstrip("/").removeprefix("https://github.com/")
    return f"https://github.com/{repo}/releases/download/{tag}/{asset_name}"


def resolve_ref_sha(repo_url: str, ref: str) -> str:
    """Resolve any git ref (branch, tag, short/long SHA) to its full commit SHA.

    Raises on failure, unlike check_llama_cpp/check_llama_swap/check_releases — those back a
    passive background check that must never crash the UI, this backs one operator-initiated
    lookup (BuildsScreen's Change version… picker resolving a chosen release tag) whose caller
    is already inside a try/except and reports the failure itself.
    """
    repo = _repo_path(repo_url)
    data = _get_json(f"https://api.github.com/repos/{repo}/commits/{ref}")
    return data["sha"]


def check_llama_cpp(manifest: dict[str, Any]) -> dict[str, Any]:
    pinned = manifest["llama_cpp"]["ref"]
    repo = _repo_path(manifest["llama_cpp"]["repo"])
    try:
        latest = _get_json(f"https://api.github.com/repos/{repo}/commits/master")
        latest_sha = latest["sha"]
    except Exception as e:  # network down, rate-limited, VPN-only host — never crash the UI over this
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "pinned": pinned,
        "latest": latest_sha,
        "update_available": latest_sha != pinned,
        "latest_date": latest.get("commit", {}).get("author", {}).get("date"),
    }


def check_llama_swap(manifest: dict[str, Any]) -> dict[str, Any]:
    pinned = manifest["llama_swap"]["version"]
    repo = _repo_path(manifest["llama_swap"]["repo"])
    try:
        latest = _get_json(f"https://api.github.com/repos/{repo}/releases/latest")
        latest_tag = latest["tag_name"]
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "pinned": pinned,
        "latest": latest_tag,
        "update_available": latest_tag != pinned,
        "latest_date": latest.get("published_at"),
    }


def _read_view_cache(component: str) -> dict[str, Any] | None:
    """Last per-view-scoped result for `component` ("llama_cpp" or "llama_swap"), if still
    fresh under `_VIEW_CACHE_TTL_S`. A separate section of the same cache file — `"view"`,
    keyed per component — never `check_all`'s own `"results"`/`"checked_at"`, so the two TTLs
    (15m here, 24h there) can never collide or overwrite one another."""
    entry = (_read_full_cache().get("view") or {}).get(component)
    if not isinstance(entry, dict):
        return None
    checked_at = entry.get("checked_at")
    if not isinstance(checked_at, (int, float)) or time.time() - checked_at > _VIEW_CACHE_TTL_S:
        return None
    result = entry.get("result")
    return result if isinstance(result, dict) else None


def _write_view_cache(component: str, result: dict[str, Any]) -> None:
    data = _read_full_cache()
    view = data.get("view")
    if not isinstance(view, dict):
        view = {}
    view[component] = {"checked_at": time.time(), "result": result}
    data["view"] = view
    _write_full_cache(data)


def check_llama_cpp_for_view(manifest: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    """`check_llama_cpp`, cache-served within `_VIEW_CACHE_TTL_S` — the Backends tab's
    automatic per-view check (plans/07 Phase 2), called from `on_tab_shown`. Independent of
    `check_all`'s 24h cache and of llama-swap's own view check: each upstream component gets
    its own check now, rather than one button firing both (which is what made the old
    "Check for Updates" button counterintuitive sitting under the llama.cpp section)."""
    expected_pin = (manifest.get("llama_cpp") or {}).get("ref")
    if not force:
        cached = _read_view_cache("llama_cpp")
        if cached is not None and (expected_pin is None or cached.get("pinned") == expected_pin):
            return cached
    result = check_llama_cpp(manifest)
    if result.get("ok"):
        _write_view_cache("llama_cpp", result)
    return result


def check_llama_swap_for_view(manifest: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    """`check_llama_swap`'s own-component counterpart to `check_llama_cpp_for_view` — see that
    function's docstring."""
    expected_pin = (manifest.get("llama_swap") or {}).get("version")
    if not force:
        cached = _read_view_cache("llama_swap")
        if cached is not None and (expected_pin is None or cached.get("pinned") == expected_pin):
            return cached
    result = check_llama_swap(manifest)
    if result.get("ok"):
        _write_view_cache("llama_swap", result)
    return result


def check_all(manifest: dict[str, Any], *, force: bool = False) -> dict[str, dict[str, Any]]:
    """Both upstream checks, served from a 24h cache unless `force`.

    The cockpit calls this whenever the Backends tab is first opened; without the cache that is
    two GitHub calls per open, against a 60/hour unauthenticated budget shared by everything
    else on the same IP. `force=True` is the explicit "Check for Updates" button, which must
    always hit the network — that is the whole point of pressing it.
    """
    if not force:
        cached = _read_cache()
        if cached is not None:
            return cached
    results = {"llama_cpp": check_llama_cpp(manifest), "llama_swap": check_llama_swap(manifest)}
    # Only cache a clean result: caching a rate-limited failure would keep reporting the
    # failure for a day after the limit cleared.
    if all(r.get("ok") for r in results.values()):
        _write_cache(results)
    return results
