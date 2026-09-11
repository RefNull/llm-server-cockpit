"""llama-swap install (pinned release binary) + config.yaml generation from models.yaml +
systemd unit install/refresh.
"""
from __future__ import annotations

import logging
import re
import shlex
import subprocess
import sys
from pathlib import Path
from string import Template
from typing import Any

import yaml

from provision.common import Runner

log = logging.getLogger("provision")

_ARCH_MAP = {"x86_64": "amd64", "aarch64": "arm64"}
_BINARY_PATH = Path("/usr/local/bin/llama-swap")
_UNIT_PATH = Path("/etc/systemd/system/llama-swap.service")
_UNIT_NAME = "llama-swap.service"


def _current_installed_version(binary_path: Path) -> str | None:
    """Read-only — safe under --dry-run."""
    if not binary_path.exists():
        return None
    result = subprocess.run([str(binary_path), "-version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def _release_asset_name(version: str, arch_suffix: str) -> str:
    # e.g. v255 -> llama-swap_255_linux_amd64.tar.gz (confirmed against the real release).
    return f"llama-swap_{version.lstrip('v')}_linux_{arch_suffix}.tar.gz"


def _install_llama_swap(host_profile: dict[str, Any], manifest: dict[str, Any], runner: Runner) -> Path:
    version = manifest["llama_swap"]["version"]
    installed = _current_installed_version(_BINARY_PATH)
    if installed is not None and version in installed:
        log.info("swap: llama-swap already installed at pinned %s (%s)", version, installed.splitlines()[0])
        return _BINARY_PATH

    machine = subprocess.run(["uname", "-m"], stdout=subprocess.PIPE, text=True).stdout.strip()
    arch_suffix = _ARCH_MAP.get(machine)
    if arch_suffix is None:
        sys.exit(f"swap: unsupported host architecture {machine!r} (supported: {sorted(_ARCH_MAP)})")

    asset = _release_asset_name(version, arch_suffix)
    repo = manifest["llama_swap"]["repo"].rstrip("/")
    url = f"{repo}/releases/download/{version}/{asset}"

    workdir = Path(host_profile["paths"]["state_dir"]) / "llama-swap-install"
    runner.mkdir(workdir)
    tarball = workdir / asset
    runner.run(["curl", "-fsSL", "-o", str(tarball), url])
    runner.run(["tar", "-xzf", str(tarball), "-C", str(workdir)])
    runner.run(["install", "-m", "0755", str(workdir / "llama-swap"), str(_BINARY_PATH)])

    if not runner.dry_run:
        new_version = _current_installed_version(_BINARY_PATH)
        if new_version is None or version not in new_version:
            sys.exit(
                f"swap: installed llama-swap at {_BINARY_PATH} but `-version` does not report pinned "
                f"{version!r} (got {new_version!r})"
            )
        log.info("swap: installed llama-swap %s", version)
    return _BINARY_PATH


def _resolve_listen_addr(host_profile: dict[str, Any]) -> str:
    iface = host_profile["network"]["vpn"]["interface"]
    port = host_profile["network"]["gateway"]["port"]
    # Resolved at generation time rather than stored in the host profile: the interface name
    # is stable, a DHCP/overlay-assigned IP is not. Binds to this address specifically (never
    # 0.0.0.0) per the project owner's explicit private-network-only decision.
    result = subprocess.run(["ip", "-4", "addr", "show", "dev", iface], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.returncode != 0:
        sys.exit(f"swap: interface {iface!r} (network.vpn.interface) not found on this host: {result.stdout.strip()}")
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/\d+", result.stdout)
    if not m:
        sys.exit(f"swap: interface {iface!r} has no IPv4 address yet — VPN not up, refusing to fall back to another bind address")
    return f"{m.group(1)}:{port}"


def _build_model_entry(model: dict[str, Any], host_profile: dict[str, Any]) -> dict[str, Any]:
    if model["engine"] == "llama-cpp":
        prefix_root = host_profile["paths"]["prefix_root"]
        models_dir = host_profile["paths"]["models_dir"]
        backend = model["bind"]["backend"]
        server_bin = f"{prefix_root}/{backend}/current/bin/llama-server"
        parts = [server_bin, "--model", f"{models_dir}/{model['quant_file']}", "--port", "${PORT}"]
        parts.extend(model.get("llama_server_args", []))
        if "mmproj_file" in model:
            parts.extend(["--mmproj", f"{models_dir}/{model['mmproj_file']}"])
        # llama-swap execs `cmd` through a shell (confirmed: config-schema.json describes it as
        # "full shell command string", and the original hand-written config single-quoted its
        # --chat-template-kwargs JSON blob for exactly this reason). shlex.quote every token so
        # values containing shell-special characters (that JSON blob's embedded double quotes)
        # survive instead of being silently stripped by the shell at launch. ${PORT} still
        # round-trips fine: llama-swap's macro substitution is a plain text replace over the
        # whole string, quoted or not, before the shell ever sees it.
        entry: dict[str, Any] = {"cmd": " ".join(shlex.quote(p) for p in parts)}
    else:  # unmanaged
        entry = {"cmd": model["cmd"]}

    if model.get("env"):
        entry["env"] = list(model["env"])
    entry["ttl"] = model.get("ttl", 0)  # ttl is optional in the schema; don't assume it's there
    return entry


def _generate_config(host_profile: dict[str, Any], models: dict[str, Any]) -> str:
    config: dict[str, Any] = {}
    timeout = host_profile["network"]["gateway"].get("health_check_timeout")
    if timeout is not None:
        config["healthCheckTimeout"] = timeout

    config["models"] = {}
    groups: dict[str, list[str]] = {}
    for model in models["models"]:
        config["models"][model["id"]] = _build_model_entry(model, host_profile)
        group = model.get("group")
        if group:
            groups.setdefault(group, []).append(model["id"])

    # Only the one precedent this repo has (an "always-on", never-evicted pool): swap: false
    # with a member list. No general group-swap-policy support beyond that single shape.
    if groups:
        config["groups"] = {name: {"swap": False, "members": members} for name, members in groups.items()}

    return yaml.safe_dump(config, sort_keys=False)


def _install_unit(repo_root: Path, binary_path: Path, config_path: Path, listen_addr: str, runner: Runner) -> bool:
    tmpl_path = repo_root / "systemd" / "llama-swap.service.tmpl"
    template = Template(tmpl_path.read_text())
    content = template.substitute(binary_path=str(binary_path), config_path=str(config_path), listen_addr=listen_addr)

    existing = _UNIT_PATH.read_text() if _UNIT_PATH.exists() else None
    unit_changed = existing != content
    runner.write_file(_UNIT_PATH, content)
    if unit_changed:
        runner.run(["systemctl", "daemon-reload"])
    return unit_changed


def _is_active(unit: str) -> bool:
    result = subprocess.run(["systemctl", "is-active", unit], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return result.returncode == 0 and result.stdout.strip() == "active"


def run(host_profile: dict[str, Any], manifest: dict[str, Any], models: dict[str, Any], runner: Runner, repo_root: Path) -> None:
    binary_path = _install_llama_swap(host_profile, manifest, runner)

    config_path = Path(host_profile["paths"]["state_dir"]) / "llama-swap" / "config.yaml"
    new_config = _generate_config(host_profile, models)
    existing_config = config_path.read_text() if config_path.exists() else None
    config_changed = existing_config != new_config

    # Validate BEFORE writing the live config or touching the running service — a bad
    # generated config must never take down an already-working llama-swap. Validate a
    # staged copy first; only overwrite the real file (and restart) once that passes.
    # (Previously this validated *after* writing+restarting, which meant a broken config
    # would restart a working service into a broken one with no path back.)
    if not runner.dry_run:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path = config_path.with_suffix(".yaml.staged")
        staging_path.write_text(new_config)
        try:
            result = subprocess.run(
                [str(binary_path), "-validate", "-config", str(staging_path)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
        finally:
            staging_path.unlink(missing_ok=True)
        if result.returncode != 0:
            sys.exit(
                "swap: generated config failed validation — refusing to write it or touch the "
                f"running service. `llama-swap -validate` exited {result.returncode}:\n{result.stdout}"
            )
        log.info("swap: generated config validates cleanly")

    runner.write_file(config_path, new_config)

    listen_addr = _resolve_listen_addr(host_profile)
    unit_changed = _install_unit(repo_root, binary_path, config_path, listen_addr, runner)

    was_active = _is_active(_UNIT_NAME)
    if was_active and (unit_changed or config_changed):
        log.info("swap: config/unit changed, restarting %s", _UNIT_NAME)
        runner.run(["systemctl", "restart", _UNIT_NAME])
    elif not was_active:
        log.info("swap: %s not active, enabling and starting", _UNIT_NAME)
        runner.run(["systemctl", "enable", "--now", _UNIT_NAME])
    else:
        log.info("swap: %s already active and up to date", _UNIT_NAME)

    # Evidence of absence, not just "the restart command returned 0" — confirm the service
    # is actually up after the fact too.
    if runner.dry_run:
        log.info("swap: --dry-run — nothing was actually installed/written/restarted")
        return
    if not _is_active(_UNIT_NAME):
        sys.exit(f"swap: {_UNIT_NAME} is not active after restart/start — check `systemctl status {_UNIT_NAME}`")
    log.info("swap: verified — %s is active, running a pre-validated config", _UNIT_NAME)
