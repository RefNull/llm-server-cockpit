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
import time
import urllib.request
from pathlib import Path
from typing import Any

import yaml

_TIMEOUT_S = 10
_CACHE_TTL_S = 24 * 60 * 60
_CACHE_PATH = Path(__file__).resolve().parent.parent / "hosts" / "upstream.update-cache.yaml"


def _read_cache() -> dict[str, Any] | None:
    """Last result, if it is still fresh. Unauthenticated GitHub allows 60 requests/hour/IP and
    this repo makes two calls per check — opening the Backends tab a few times in a session was
    enough to start getting 403s, which then rendered as "couldn't check" and looked like a
    network fault rather than a self-inflicted rate limit."""
    try:
        data = yaml.safe_load(_CACHE_PATH.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    checked_at = data.get("checked_at")
    if not isinstance(checked_at, (int, float)) or time.time() - checked_at > _CACHE_TTL_S:
        return None
    results = data.get("results")
    return results if isinstance(results, dict) else None


def _write_cache(results: dict[str, Any]) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(yaml.safe_dump({"checked_at": time.time(), "results": results}, sort_keys=False))
    except OSError:
        pass  # a cache that cannot be written is a slower app, not a broken one


def cache_age_seconds() -> float | None:
    """How old the stored result is, for the UI to say so. None when there is none."""
    try:
        data = yaml.safe_load(_CACHE_PATH.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    checked_at = data.get("checked_at")
    return time.time() - checked_at if isinstance(checked_at, (int, float)) else None


_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "llm-server-cockpit"}


def _get_json(url: str) -> Any:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        return json.load(resp)


def _repo_path(repo_url: str) -> str:
    return repo_url.rstrip("/").removeprefix("https://github.com/")


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
