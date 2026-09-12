"""Tailscale: install the client and bring it up. `tailscale up` is inherently interactive the
first time (it prints a login URL and blocks until the operator authenticates in a browser) —
callers running from a real terminal (bin/provision) get that for free; the cockpit TUI instead
suspends itself and hands the real terminal to the subprocess for exactly this step (see
cockpit/screens/settings.py) rather than trying to mediate an interactive login through widgets.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from provision.common import Runner

log = logging.getLogger("provision")

_TIMEOUT_S = 10


def is_installed() -> bool:
    return shutil.which("tailscale") is not None


def ensure_installed(runner: Runner) -> None:
    """Idempotent: no-op if the client is already on PATH. Installs via Tailscale's own
    install script rather than hand-rolling per-distro apt repo setup — matches this repo's
    default of using upstream's own supported installer where one exists (see swap.py's own
    binary-release install for the same reasoning applied to llama-swap)."""
    if is_installed():
        log.info("tailscale: already installed")
        return
    runner.shell("curl -fsSL https://tailscale.com/install.sh | sh")


def status(host_profile: dict[str, Any]) -> dict[str, Any]:
    """Read-only Tailscale facts for display (the cockpit's Settings/Dashboard tabs)."""
    if not is_installed():
        return {"installed": False, "logged_in": False, "backend_state": None, "ip": None}
    result = subprocess.run(
        ["tailscale", "status", "--json"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=_TIMEOUT_S,
    )
    if result.returncode != 0:
        return {"installed": True, "logged_in": False, "backend_state": None, "ip": None, "error": result.stdout.strip()}
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        return {"installed": True, "logged_in": False, "backend_state": None, "ip": None, "error": f"couldn't parse status: {e}"}
    backend_state = data.get("BackendState")
    ips = (data.get("Self") or {}).get("TailscaleIPs") or []
    return {
        "installed": True,
        "backend_state": backend_state,
        "logged_in": backend_state == "Running",
        "ip": ips[0] if ips else None,
    }


def run(host_profile: dict[str, Any], manifest: dict[str, Any], models: dict[str, Any], runner: Runner, repo_root: Path) -> None:
    """CLI entry point (bin/provision) — a real terminal is already available here, so
    `tailscale up`'s interactive login prompt (if not already authenticated) just works."""
    ensure_installed(runner)
    st = status(host_profile)
    if st["logged_in"]:
        log.info("tailscale: already logged in (state=%s, ip=%s)", st["backend_state"], st["ip"])
        return
    runner.run(["tailscale", "up"])
