"""Ad-hoc `python serve.py`-style script supervision: one generated systemd unit per script
registered in scripts.yaml, mirroring provision/steps/swap.py's own unit-install pattern —
cockpit never holds a live subprocess handle itself, so a script survives a cockpit restart and
gets restart-on-failure / `journalctl` logs for free, the same as llama-swap.
"""
from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path
from string import Template
from typing import Any

from provision.common import Runner, unit_command, unit_value

log = logging.getLogger("provision")

_UNIT_DIR = Path("/etc/systemd/system")
_TIMEOUT_S = 10
_JOURNAL_TIMEOUT_S = 15
"""journalctl -n 200 outruns the 10s budget used for the quick is-active/is-enabled probes —
matches provision/steps/docker.py::read_logs's own 15s for the same reason (a bigger read)."""


def unit_name(script_id: str) -> str:
    """The systemd service unit name for `script_id`."""
    return f"cockpit-script-{script_id}.service"


_unit_name = unit_name


def _unit_path(script_id: str) -> Path:
    return _UNIT_DIR / unit_name(script_id)


def unit_installed(script_id: str) -> bool:
    """Check if the systemd unit file exists on disk."""
    return _unit_path(script_id).exists()


def _build_exec_start(script: dict[str, Any]) -> str:
    """The single, already-quoted argument that follows `sh -c` in the unit's ExecStart= line
    (see systemd/python-script.service.tmpl) — one POSIX-shell quoting pass here rather than
    trusting systemd's own ExecStart= word-splitting to agree with it."""
    stype = script.get("type", "python")
    if stype == "bash":
        binary = script.get("interpreter") or script.get("bash") or "/bin/bash"
    else:
        binary = script.get("python") or "python3"
    argv = [binary, script["path"], *script.get("args", [])]
    return shlex.quote(shlex.join(argv))


def install_unit(script: dict[str, Any], host_profile: dict[str, Any], repo_root: Path, runner: Runner) -> bool:
    """Write (or refresh) this script's unit file. Returns True if the unit content changed
    (caller should `systemctl daemon-reload`)."""
    service = host_profile.get("service", {})
    restart_policy = script.get("restart_policy") or service.get("restart_policy", "on-failure")
    restart_sec = service.get("restart_sec", 5)
    working_dir = script.get("working_dir") or str(Path(script["path"]).parent)

    tmpl_path = repo_root / "systemd" / "python-script.service.tmpl"
    template = Template(tmpl_path.read_text())
    content = template.substitute(
        script_id=unit_value(script["id"]),
        working_dir=unit_value(working_dir),
        exec_start=unit_command(_build_exec_start(script)),
        restart_policy=restart_policy,
        restart_sec=str(restart_sec),
    )

    unit_path = _unit_path(script["id"])
    try:
        existing = unit_path.read_text() if unit_path.exists() else None
    except OSError:
        existing = None
    changed = existing != content
    runner.write_file(unit_path, content)
    if changed:
        runner.run(["systemctl", "daemon-reload"])
    return changed


def _is_active(unit: str) -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", unit], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=_TIMEOUT_S,
    )
    return result.returncode == 0 and result.stdout.strip() == "active"


def _is_enabled(unit: str) -> bool:
    result = subprocess.run(
        ["systemctl", "is-enabled", unit], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=_TIMEOUT_S,
    )
    return result.returncode == 0 and result.stdout.strip() == "enabled"


def unit_status(unit: str) -> dict[str, bool]:
    """Read-only active/enabled state for any systemd unit — not script-specific. The cockpit's
    Units tab uses this directly for llama-swap.service, its timers and the WOL unit; `status`
    below is the script-specific wrapper kept for callers that only have a script id."""
    return {"unit_active": _is_active(unit), "unit_enabled": _is_enabled(unit)}


def status(script_id: str) -> dict[str, Any]:
    """Read-only status for display (the cockpit's Units/Dashboard tabs)."""
    return unit_status(_unit_name(script_id))


def journal_tail(unit: str, lines: int = 200, runner: Runner | None = None) -> str:
    """Recent journal for `unit` — feeds the cockpit Units tab's "Logs" action for llama-swap,
    its timers, the WOL unit and every script unit alike. Scoped like
    provision/steps/docker.py::read_logs (a snapshot, not a live tail), but unlike it this
    never raises: callers here have no docker_available()-style precheck to lean on first, and
    a unit that doesn't exist yet is a normal state for this tab, not an error to propagate."""
    cmd = ["journalctl", "-u", unit, "-n", str(lines), "--no-pager"]
    try:
        if runner is not None:
            # Routed through Runner so an elevated one actually applies: a system unit's journal
            # is root-readable only, so an unprivileged read returns an empty log that looks
            # exactly like "this unit has never run". check=False — journalctl exits non-zero
            # for a unit that does not exist, which is a normal state for this tab.
            result = runner.run(cmd, check=False, capture=True)
            return (result.stdout if result is not None else "") or "(no output)"
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=_JOURNAL_TIMEOUT_S,
        )
    except Exception as e:
        return f"error running journalctl: {e}"
    return result.stdout or "(no output)"


def start(script_id: str, runner: Runner) -> None:
    runner.run(["systemctl", "start", _unit_name(script_id)])


def stop(script_id: str, runner: Runner) -> None:
    runner.run(["systemctl", "stop", _unit_name(script_id)])


def enable(script_id: str, runner: Runner) -> None:
    runner.run(["systemctl", "enable", _unit_name(script_id)])


def disable(script_id: str, runner: Runner) -> None:
    runner.run(["systemctl", "disable", _unit_name(script_id)])


def run(host_profile: dict[str, Any], scripts: dict[str, Any], runner: Runner, repo_root: Path) -> None:
    """Idempotent install-all: (re)write every registered script's unit, enabling/starting per
    its own `enabled` flag. Cockpit's Scripts tab calls this same function — no parallel
    install logic between CLI and TUI, matching every other provision.steps.*::run()."""
    for script in scripts.get("scripts", []):
        install_unit(script, host_profile, repo_root, runner)
        unit = _unit_name(script["id"])
        if script.get("enabled", False):
            if not _is_enabled(unit):
                runner.run(["systemctl", "enable", unit])
            if not _is_active(unit):
                runner.run(["systemctl", "start", unit])
        log.info("scripts[%s]: unit installed", script["id"])
