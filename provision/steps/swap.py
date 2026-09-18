"""llama-swap install (pinned release binary) + config.yaml generation from models.yaml +
systemd unit install/refresh.
"""
from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
import subprocess
import urllib.request
import sys
from pathlib import Path
from string import Template
from typing import Any

import yaml

from provision.common import Runner, unit_command, unit_value

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


def resolve_vpn_ip(interface: str) -> str | None:
    """Read-only: the interface's current IPv4 address, or None if it doesn't exist / has
    none yet. Public so the cockpit's Settings tab can show a live preview of what an
    interface name actually resolves to — the field takes a NIC name, not an IP, precisely
    because that address can change (DHCP/overlay-assigned) while the interface name doesn't."""
    # Guarded like wol._read_iface_mac: iproute2 is not guaranteed present, and this is
    # documented as returning None rather than raising — it did raise FileNotFoundError, which
    # every caller then had to wrap.
    if shutil.which("ip") is None:
        return None
    result = subprocess.run(["ip", "-4", "addr", "show", "dev", interface], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.returncode != 0:
        return None
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/\d+", result.stdout)
    return m.group(1) if m else None


def endpoint(host_profile: dict[str, Any]) -> str | None:
    """`host:port` llama-swap is listening on, or None when the VPN address has no IP yet."""
    iface = host_profile["network"]["vpn"]["interface"]
    port = host_profile["network"]["gateway"]["port"]
    ip = resolve_vpn_ip(iface)
    return f"{ip}:{port}" if ip else None


def _api(host_profile: dict[str, Any], path: str, *, method: str = "GET", timeout: float = 5.0):
    """One request against llama-swap's own HTTP API. Never raises — this feeds a status column
    and a pair of buttons, and llama-swap being down is a normal state for both."""
    base = endpoint(host_profile)
    if base is None:
        return None, "llama-swap endpoint not resolvable (VPN interface has no IP)"
    req = urllib.request.Request(f"http://{base}{path}", method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), None
    except Exception as e:
        return None, str(e)


def running_models(host_profile: dict[str, Any]) -> dict[str, str]:
    """model id -> state, from `GET /running`.

    Response shape is `{"running": [{"model": ..., "state": ..., ...}]}` — read from
    llama-swap's own `runningModel` struct tags (internal/server/api.go), not guessed. A model
    absent from the list is simply not loaded.
    """
    body, error = _api(host_profile, "/running")
    if body is None:
        return {}
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return {}
    return {
        entry.get("model"): entry.get("state", "")
        for entry in data.get("running", [])
        if isinstance(entry, dict) and entry.get("model")
    }


def load_model(host_profile: dict[str, Any], model_id: str) -> str | None:
    """Warm a model. Returns an error string, or None on success.

    `GET /upstream/<id>/` — llama-swap has NO load endpoint; loading is a side effect of a
    request arriving for a model. This is the same trick llama-swap's own startup preload uses
    ("fires a background GET / at every model named in Hooks.OnStartup.Preload so they are warm
    before the first real request", internal/server/api.go::startPreload), so it costs no
    tokens and runs no inference. Generous timeout: loading a large quant into VRAM is slow.
    """
    _, error = _api(host_profile, f"/upstream/{model_id}/", timeout=300.0)
    return error


def unload_model(host_profile: dict[str, Any], model_id: str) -> str | None:
    """`POST /api/models/unload/<id>`. Returns an error string, or None on success."""
    _, error = _api(host_profile, f"/api/models/unload/{model_id}", method="POST", timeout=30.0)
    return error


def _resolve_listen_addr(host_profile: dict[str, Any]) -> str:
    iface = host_profile["network"]["vpn"]["interface"]
    port = host_profile["network"]["gateway"]["port"]
    # Resolved at generation time rather than stored in the host profile: the interface name
    # is stable, a DHCP/overlay-assigned IP is not. Binds to this address specifically (never
    # 0.0.0.0) per the project owner's explicit private-network-only decision.
    ip = resolve_vpn_ip(iface)
    if ip is None:
        sys.exit(
            f"swap: interface {iface!r} (network.vpn.interface) not found, or has no IPv4 "
            "address yet (VPN not up) — refusing to fall back to another bind address"
        )
    return f"{ip}:{port}"


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
    # Only written when models.yaml actually specifies it. `ttl` is optional in this repo's
    # schema, and it was defaulted to 0 here — but upstream llama-swap documents "a ttl of 0
    # will mean never unload", with its own default being "-1 (use global default)". So an
    # entry that simply omitted ttl was silently rewritten as "pin this model in VRAM forever",
    # which is the opposite of unspecified and the one field that costs VRAM at rest. Omitting
    # it hands the decision back to llama-swap's global default, where it belongs.
    if "ttl" in model:
        entry["ttl"] = model["ttl"]
    return entry


def parse_config_for_import(yaml_text: str) -> list[dict[str, Any]]:
    """Best-effort reverse of _generate_config(), for the cockpit's Models tab "Import from
    config.yaml" shortcut. Every entry comes back as engine: "unmanaged" with its cmd preserved
    verbatim, never as a reconstructed engine: "llama-cpp" entry — a llama-swap config's cmd
    string never carries the repo_id a real llama-cpp models.yaml entry requires (repo_id only
    matters at download time; by the time a model is running, the served file is already local
    and nothing in the command line says what Hugging Face repo it came from), so a confident
    llama-cpp/GPU reconstruction from this alone isn't possible. The operator reviews every
    proposed entry before anything is merged (see deploy.py) and can hand-convert one to
    engine: "llama-cpp" afterward if they want that, using the imported cmd as a reference."""
    try:
        data = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"invalid YAML: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
        raise ValueError("expected a top-level 'models' mapping (a llama-swap config.yaml)")

    group_of: dict[str, str] = {}
    for group_name, group in (data.get("groups") or {}).items():
        if not isinstance(group, dict):
            continue
        for member_id in group.get("members", []) or []:
            group_of[member_id] = group_name

    proposed: list[dict[str, Any]] = []
    for model_id, entry in data["models"].items():
        if not isinstance(entry, dict) or "cmd" not in entry:
            continue
        model: dict[str, Any] = {"id": model_id, "engine": "unmanaged", "cmd": entry["cmd"]}
        if entry.get("env"):
            model["env"] = list(entry["env"])
        # `in`, not truthiness: ttl 0 is meaningful ("never unload"), so a falsy check dropped
        # exactly the setting an operator was most deliberate about when importing a config.
        if "ttl" in entry:
            model["ttl"] = entry["ttl"]
        if model_id in group_of:
            model["group"] = group_of[model_id]
        proposed.append(model)
    return proposed


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


def _install_unit(repo_root: Path, binary_path: Path, config_path: Path, listen_addr: str, host_profile: dict[str, Any], runner: Runner) -> bool:
    service = host_profile.get("service", {})
    restart_policy = service.get("restart_policy", "on-failure")
    restart_sec = service.get("restart_sec", 5)

    tmpl_path = repo_root / "systemd" / "llama-swap.service.tmpl"
    template = Template(tmpl_path.read_text())
    content = template.substitute(
        binary_path=unit_command(str(binary_path)),
        config_path=unit_command(str(config_path)),
        listen_addr=unit_command(listen_addr),
        restart_policy=restart_policy,
        restart_sec=str(restart_sec),
    )

    existing = _UNIT_PATH.read_text() if _UNIT_PATH.exists() else None
    unit_changed = existing != content
    runner.write_file(_UNIT_PATH, content)
    if unit_changed:
        runner.run(["systemctl", "daemon-reload"])
    return unit_changed


_TIMEOUT_S = 10


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


def status(host_profile: dict[str, Any]) -> dict[str, Any]:
    """Read-only llama-swap facts for display (the cockpit's Dashboard/Backends tabs) — reuses
    run()'s own _is_active/_is_enabled/_current_installed_version rather than a parallel check.

    `binary_path`/`unit_name` are the module's own `_BINARY_PATH`/`_UNIT_NAME` constants,
    surfaced here rather than as separate exported names — this is already the one function
    both cockpit call sites use for llama-swap facts, so it stays the single public surface
    instead of growing a second and third module-level name."""
    return {
        "installed_version": _current_installed_version(_BINARY_PATH),
        "unit_active": _is_active(_UNIT_NAME),
        "unit_enabled": _is_enabled(_UNIT_NAME),
        "binary_path": str(_BINARY_PATH),
        "unit_name": _UNIT_NAME,
    }


def install_pinned_binary(host_profile: dict[str, Any], manifest: dict[str, Any], runner: Runner) -> Path:
    """Install/refresh the llama-swap binary at manifest.yaml's pinned version only — no
    config.yaml regeneration, no unit install, no restart. Narrow counterpart to run(), for a
    caller that must not turn "update llama-swap" into a full redeploy of the gateway (the
    cockpit's Backends tab "Update to latest" button, which bumps the pin and installs the new
    binary but must not rewrite config.yaml or the systemd unit as a side effect)."""
    return _install_llama_swap(host_profile, manifest, runner)


def unit_load_state() -> str:
    """What systemd itself says about llama-swap.service: "loaded", "not-found", "masked", …

    Asked of systemd rather than by probing `_UNIT_PATH`, because a unit can equally live in
    /lib/systemd/system or /usr/lib/systemd/system — a path check would call a perfectly good
    packaged unit missing. Returns "" when systemctl cannot be reached at all (no systemd,
    e.g. a dev machine), which callers must treat as "unknown", not as "not-found"."""
    try:
        result = subprocess.run(
            ["systemctl", "show", "-p", "LoadState", "--value", _UNIT_NAME],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _systemctl(runner: Runner, *args: str) -> None:
    """systemctl through Runner with its output captured, so a failure carries systemd's own
    reason instead of just an exit code. Without capture=True the child inherits stdout/stderr,
    which under the TUI means the message is painted over the screen and lost, leaving the
    operator with "returned non-zero exit status 1" and nothing to act on."""
    try:
        runner.run(["systemctl", *args], capture=True)
    except subprocess.CalledProcessError as e:
        detail = (e.output or "").strip()
        raise RuntimeError(
            f"systemctl {' '.join(args)} failed (exit {e.returncode})"
            + (f": {detail}" if detail else " with no output")
        ) from e


def reconcile_service(runner: Runner) -> str:
    """Bring the running service in line with the binary on disk, and say what was done.

    Returns "restarted" | "started" | "no-unit" | "unknown". Never touches config.yaml or the
    unit file — only run() does that, because writing them is a deploy, not an update.

    Exists because `_current_installed_version` reads the binary: after install_pinned_binary()
    swaps it, the version on screen would report the new pin while the running process still
    executes the old inode. Restarting closes that gap.

    **A missing unit is not a failure.** The binary is a system-wide install at _BINARY_PATH
    and updating it is complete in itself; whether systemd supervises it is a separate
    question. An earlier version of this raised on a missing unit, which made a binary swap
    depend on a service and refused updates on hosts where llama-swap runs perfectly well
    without one."""
    state = unit_load_state()
    if state == "not-found":
        # Not an error. The binary at _BINARY_PATH is a system-wide install and updating it is
        # complete on its own; whether a systemd unit happens to supervise it is a separate
        # question. Refusing the update because no unit exists made a binary swap depend on a
        # service, which is backwards — reported by the operator on 2026-09-18.
        return "no-unit"
    if state == "":
        return "unknown"
    if _is_active(_UNIT_NAME):
        _systemctl(runner, "restart", _UNIT_NAME)
        return "restarted"
    _systemctl(runner, "enable", "--now", _UNIT_NAME)
    return "started"


def _sync_timer_pair(
    name: str,
    service_content: str,
    timer_content: str,
    enabled: bool,
    runner: Runner,
) -> None:
    """Install/remove a <name>.service + <name>.timer pair as one unit, driven by a single
    `enabled` flag — used for both scheduled restart and scheduled update-check, which are
    identical in shape (a oneshot service triggered by a timer, toggled on/off as a pair).
    """
    service_path = Path(f"/etc/systemd/system/{name}.service")
    timer_path = Path(f"/etc/systemd/system/{name}.timer")
    timer_unit = f"{name}.timer"

    if not enabled:
        if _is_enabled(timer_unit) or _is_active(timer_unit):
            log.info("swap: disabling %s (scheduled feature turned off)", timer_unit)
            runner.run(["systemctl", "disable", "--now", timer_unit], check=False)
        return

    existing_service = service_path.read_text() if service_path.exists() else None
    existing_timer = timer_path.read_text() if timer_path.exists() else None
    changed = existing_service != service_content or existing_timer != timer_content

    runner.write_file(service_path, service_content)
    runner.write_file(timer_path, timer_content)
    if changed:
        runner.run(["systemctl", "daemon-reload"])
    if not _is_enabled(timer_unit):
        log.info("swap: enabling %s", timer_unit)
        runner.run(["systemctl", "enable", "--now", timer_unit])
    elif changed:
        runner.run(["systemctl", "restart", timer_unit])


def sync_scheduled_restart(host_profile: dict[str, Any], repo_root: Path, runner: Runner) -> None:
    cfg = host_profile.get("service", {}).get("scheduled_restart", {})
    enabled = cfg.get("enabled", False)
    on_calendar = cfg.get("on_calendar", "daily")
    service_content = (repo_root / "systemd" / "llama-swap-restart.service.tmpl").read_text()
    timer_content = Template((repo_root / "systemd" / "llama-swap-restart.timer.tmpl").read_text()).substitute(on_calendar=unit_value(on_calendar))
    _sync_timer_pair("llama-swap-restart", service_content, timer_content, enabled, runner)


def sync_update_check_timer(host_profile: dict[str, Any], repo_root: Path, runner: Runner) -> None:
    cfg = host_profile.get("update_check", {})
    enabled = cfg.get("enabled", False)
    on_calendar = cfg.get("on_calendar", "daily")
    check_updates_path = repo_root / "bin" / "check-updates"
    service_content = Template((repo_root / "systemd" / "update-check.service.tmpl").read_text()).substitute(
        check_updates_path=unit_command(str(check_updates_path)),
        hostname=unit_command(host_profile["hostname"]),
    )
    timer_content = Template((repo_root / "systemd" / "update-check.timer.tmpl").read_text()).substitute(on_calendar=unit_value(on_calendar))
    _sync_timer_pair("llm-server-cockpit-update-check", service_content, timer_content, enabled, runner)


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
    unit_changed = _install_unit(repo_root, binary_path, config_path, listen_addr, host_profile, runner)
    sync_scheduled_restart(host_profile, repo_root, runner)
    sync_update_check_timer(host_profile, repo_root, runner)

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
