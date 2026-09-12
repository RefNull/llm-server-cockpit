"""Docker container observer: read-only listing/inspection of whatever's already running, plus
two narrowly-scoped mutating calls (restart one/all). Cockpit never owns or edits a compose.yaml
— it only points at the one each running container was already started from. Building a Docker
management suite is explicitly out of scope; this stays a thin, honest view over `docker ps`/
`docker inspect`/`docker logs`/`docker restart`.
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

_TIMEOUT_S = 15


def docker_available() -> bool:
    return shutil.which("docker") is not None


def list_containers() -> list[dict[str, Any]]:
    """All containers docker knows about (running or stopped), most-recently-created first.
    Each entry also carries compose_files/compose_working_dir when the container was started
    via `docker compose` (Docker Compose stamps every container it creates with
    com.docker.compose.project.config_files/.working_dir labels) — absent (None) for a
    container started via a bare `docker run`; callers disable compose-specific actions for
    those rows rather than erroring. Raises RuntimeError if docker itself isn't installed."""
    if not docker_available():
        raise RuntimeError("docker not found on this host — install Docker to use this tab")
    result = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{json .}}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=_TIMEOUT_S,
    )
    if result.returncode != 0:
        raise RuntimeError(f"`docker ps` failed: {result.stdout}")

    containers: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        containers.append({
            "name": row.get("Names", ""),
            "image": row.get("Image", ""),
            "status": row.get("Status", ""),
            "state": row.get("State", ""),
            "ports": row.get("Ports", ""),
        })
    for container in containers:
        container.update(_compose_labels(container["name"]))
    return containers


def _compose_labels(name: str) -> dict[str, Any]:
    """`index` is required (not plain dot access) because the label keys themselves contain
    dots — the standard way to pull a Compose project's labels via `docker inspect --format`."""
    result = subprocess.run(
        [
            "docker", "inspect", "--format",
            '{{index .Config.Labels "com.docker.compose.project.config_files"}}||'
            '{{index .Config.Labels "com.docker.compose.project.working_dir"}}',
            name,
        ],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=_TIMEOUT_S,
    )
    if result.returncode != 0:
        return {"compose_files": None, "compose_working_dir": None}
    config_files, _, working_dir = result.stdout.strip().partition("||")
    return {"compose_files": config_files or None, "compose_working_dir": working_dir or None}


def read_compose_file(container: dict[str, Any]) -> str:
    """Contents of the compose file(s) this container was started from. Raises if the
    container has no compose labels — callers should check compose_files first and disable
    the "View compose.yaml" action rather than calling this and catching the error."""
    config_files = container.get("compose_files")
    if not config_files:
        raise ValueError(f"{container.get('name')!r} wasn't started via docker compose — no compose file to show")
    paths = [p for p in config_files.split(",") if p]
    content = Path(paths[0]).read_text()
    if len(paths) > 1:
        content = f"# {paths[0]} (1 of {len(paths)} compose files for this project: {', '.join(paths)})\n\n{content}"
    return content


def read_logs(name: str, tail: int = 200) -> str:
    """Snapshot of recent logs, not a live tail — matches the InfoModal popup this feeds into
    (a fixed-content read-only view, same as the lspci/ip a popups already in the app)."""
    result = subprocess.run(
        ["docker", "logs", "--tail", str(tail), name],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=_TIMEOUT_S,
    )
    return result.stdout or "(no output)"


def restart_container(name: str, runner: Runner) -> None:
    runner.run(["docker", "restart", name])


def restart_all(names: list[str], runner: Runner) -> None:
    if not names:
        return
    runner.run(["docker", "restart", *names])
