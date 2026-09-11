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
import urllib.request
from typing import Any

_TIMEOUT_S = 10
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


def check_all(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {"llama_cpp": check_llama_cpp(manifest), "llama_swap": check_llama_swap(manifest)}
