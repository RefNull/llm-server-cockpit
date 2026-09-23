"""llama-swap install (pinned release binary) + config.yaml generation from models.yaml +
systemd unit install/refresh.
"""
from __future__ import annotations

import json
import logging
import platform
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

_ARCH_MAP = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
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

    machine = platform.machine()
    arch_suffix = _ARCH_MAP.get(machine)
    if arch_suffix is None:
        raise RuntimeError(f"swap: unsupported host architecture {machine!r} (supported: {sorted(_ARCH_MAP)})")

    asset = _release_asset_name(version, arch_suffix)
    repo = manifest["llama_swap"]["repo"].rstrip("/")
    url = f"{repo}/releases/download/{version}/{asset}"

    workdir = Path(host_profile["paths"]["state_dir"]) / "llama-swap-install"
    runner.mkdir(workdir)
    tarball = workdir / asset
    try:
        log.info("swap: downloading %s from %s", asset, url)
        runner.run(["curl", "-fsSL", "-o", str(tarball), url], capture=True)
        log.info("swap: extracting %s", asset)
        runner.run(["tar", "-xzf", str(tarball), "-C", str(workdir)], capture=True)
        log.info("swap: installing binary to %s", _BINARY_PATH)
        runner.run(["install", "-m", "0755", str(workdir / "llama-swap"), str(_BINARY_PATH)], capture=True)
    except subprocess.CalledProcessError as e:
        detail = (e.output or "").strip()
        cmd_str = " ".join(e.cmd) if isinstance(e.cmd, list) else str(e.cmd)
        raise RuntimeError(
            f"swap: install command {cmd_str} failed (exit {e.returncode})"
            + (f": {detail}" if detail else "")
        ) from e

    if not runner.dry_run:
        new_version = _current_installed_version(_BINARY_PATH)
        if new_version is None or version not in new_version:
            raise RuntimeError(
                f"swap: installed llama-swap at {_BINARY_PATH} but `-version` does not report pinned "
                f"{version!r} (got {new_version!r})"
            )
        log.info("swap: installed and verified llama-swap at %s (%s)", _BINARY_PATH, new_version.splitlines()[0])
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
    elif model["engine"] == "python":
        parts = [model["python"], model["script"], *model.get("args", [])]
        # Same quoting convention as the llama-cpp branch above (and for the same reason:
        # cmd is not run through a shell — SanitizeCommand shlex.splits it, so a
        # shlex.quote'd token round-trips whether or not it actually needed quoting).
        entry = {"cmd": " ".join(shlex.quote(p) for p in parts)}
    else:  # unmanaged
        entry = {"cmd": model["cmd"]}

    if model.get("env"):
        entry["env"] = list(model["env"])
    if model.get("check_endpoint"):
        entry["checkEndpoint"] = model["check_endpoint"]
    # Only written when models.yaml actually specifies it. `ttl` is optional in this repo's
    # schema, and it was defaulted to 0 here — but upstream llama-swap documents "a ttl of 0
    # will mean never unload", with its own default being "-1 (use global default)". So an
    # entry that simply omitted ttl was silently rewritten as "pin this model in VRAM forever",
    # which is the opposite of unspecified and the one field that costs VRAM at rest. Omitting
    # it hands the decision back to llama-swap's global default, where it belongs.
    if "ttl" in model:
        entry["ttl"] = model["ttl"]
    return entry


_KNOWN_PER_MODEL_KEYS = frozenset({"cmd", "env", "ttl", "checkEndpoint", "healthCheckTimeout"})
_PYTHON_BASENAME_RE = re.compile(r"^python3?(\.\d+)?$")
_BACKEND_DIR_RE = re.compile(r"/([^/]+)/current/bin/llama-server$")


def _collect_group_members(groups: Any, group_of: dict[str, str]) -> None:
    for group_name, group in (groups or {}).items():
        if not isinstance(group, dict):
            continue
        for member_id in group.get("members", []) or []:
            group_of[member_id] = group_name


def _sanitize_cmd(raw_cmd: str, notes: list[str]) -> tuple[list[str], str]:
    """Mirror llama-swap v255's SanitizeCommand (internal/config/commands.go:11-45) exactly:
    drop lines whose stripped text starts with '#', turn a trailing '\\' into a space, then
    shlex.split (POSIX) the result. `cmd` is never run through a shell (§0e) — this must match
    that mechanism, not approximate it.

    Returns `(tokens, cleaned_text)` — tokens for classifying/rewriting a recognised command
    (llama-server, python), cleaned_text (comments stripped, lines joined, NOT re-quoted) for
    an unrecognised one. An unrecognised command must be reproduced close to verbatim: running
    it through shlex.split then shlex.join re-quotes every token that shlex considers special
    (e.g. a bare `VAR=value` prefix, which contains `$`), which is needless since nothing here
    rewrites it — and it once produced literal `'` characters in the reconstructed cmd that
    were never in the operator's source (a real regression, caught against their own config)."""
    lines = raw_cmd.splitlines()
    kept: list[str] = []
    dropped: list[str] = []
    for line in lines:
        if line.strip().startswith("#"):
            dropped.append(line.strip())
        else:
            trimmed = line.strip()
            kept.append(trimmed[:-1] + " " if trimmed.endswith("\\") else line)
    if dropped:
        mmproj_lines = [d for d in dropped if "--mmproj" in d]
        note = f"{len(dropped)} comment line(s) dropped"
        if mmproj_lines:
            note += f" ({'; '.join(mmproj_lines)})"
        notes.append(note)
    cleaned_text = " ".join(line.strip() for line in kept if line.strip())
    return shlex.split("\n".join(kept)), cleaned_text


def _classify_llama_cpp(
    argv: list[str], host_profile: dict[str, Any], notes: list[str]
) -> tuple[dict[str, Any], str]:
    prefix_root = host_profile["paths"]["prefix_root"]
    models_dir = host_profile["paths"]["models_dir"]
    status = "ok"

    prog = argv[0]
    m = _BACKEND_DIR_RE.search(prog)
    backend: str | None = None
    if m is None:
        status = "review"
        notes.append("could not determine backend from binary path (expected .../<backend>/current/bin/llama-server)")
    else:
        backend = m.group(1)
        if not prog.startswith(prefix_root):
            notes.append("binary path rewritten to prefix_root")

    gpu: str | None = None
    if backend is not None:
        matches = sorted(g["id"] for g in host_profile.get("gpus", []) if backend in g.get("backends", []))
        if len(matches) == 1:
            gpu = matches[0]
        else:
            status = "review"
            if not matches:
                notes.append(f"no host GPU has backend {backend!r}")
            else:
                notes.append(f"backend {backend!r} matches multiple GPUs: {', '.join(matches)}")

    model: dict[str, Any] = {"engine": "llama-cpp", "repo_id": "local"}
    llama_server_args: list[str] = []
    quant_file: str | None = None
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok in ("--model", "--mmproj") and i + 1 < len(argv):
            p = argv[i + 1]
            basename = p.rsplit("/", 1)[-1]
            dirname = p.rsplit("/", 1)[0] if "/" in p else ""
            if dirname and dirname != models_dir:
                notes.append(f"relocated from {dirname}")
            if tok == "--model":
                quant_file = basename
            else:
                model["mmproj_file"] = basename
            i += 2
            continue
        if tok == "--port" and i + 1 < len(argv):
            i += 2
            continue
        llama_server_args.append(tok)
        i += 1

    if quant_file is None:
        status = "review"
        notes.append("no --model argument found")
    else:
        model["quant_file"] = quant_file
    if backend is not None and gpu is not None:
        model["bind"] = {"gpu": gpu, "backend": backend}
    if llama_server_args:
        model["llama_server_args"] = llama_server_args
    return model, status


def parse_config_for_import(
    yaml_text: str, host_profile: dict[str, Any]
) -> tuple[list[dict[str, Any]], int | None]:
    """Reverse of _generate_config(), for the cockpit's Models tab "Import from config.yaml"
    shortcut. Returns `(results, health_check_timeout)`: `results` entries are
    `{"model": <models.yaml entry>, "notes": [...], "status": "ok" | "review"}` — the operator
    reviews (and can edit) every proposed entry before anything is merged (see deploy.py);
    nothing here is written to disk. `health_check_timeout` is the pasted config's top-level
    value, or None if it had none — a plain tuple, not a `list` subclass carrying a sideband
    attribute, so every consumer gets it by unpacking rather than a defensive `getattr`.

    Classification is by argv[0]'s basename after `_sanitize_cmd` (llama-server -> llama-cpp,
    python/python3/python3.N -> python, anything else -> unmanaged, cmd reconstructed).
    Never stats a path — the cockpit may run off the host the config came from."""
    try:
        data = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"invalid YAML: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
        raise ValueError("expected a top-level 'models' mapping (a llama-swap config.yaml)")

    raw_timeout = data.get("healthCheckTimeout")
    health_check_timeout: int | None = None
    if raw_timeout is not None:
        try:
            health_check_timeout = int(raw_timeout)
        except (ValueError, TypeError):
            health_check_timeout = None

    group_of: dict[str, str] = {}
    _collect_group_members(data.get("groups"), group_of)
    routing_groups = (((data.get("routing") or {}).get("router") or {}).get("settings") or {}).get("groups")
    _collect_group_members(routing_groups, group_of)

    results: list[dict[str, Any]] = []
    for model_id, entry in data["models"].items():
        if not isinstance(entry, dict) or "cmd" not in entry:
            continue
        try:
            results.append(_import_one_model(model_id, entry, host_profile, group_of))
        except Exception as e:
            # One malformed entry (a non-string cmd, a field of the wrong shape, anything
            # this function didn't anticipate) must not abort the whole import — the docstring
            # above already promises that for the unbalanced-quote case; this covers the rest.
            # Caught against a real bug: `cmd: 123` in a pasted config.yaml raised AttributeError
            # out of _sanitize_cmd's raw_cmd.splitlines(), uncaught, and crashed the app.
            results.append({
                "model": {"id": model_id, "engine": "unmanaged", "cmd": str(entry.get("cmd", ""))},
                "notes": [f"could not import this entry ({type(e).__name__}: {e}) — needs manual review"],
                "status": "review",
            })
    return results, health_check_timeout


def _import_one_model(
    model_id: str, entry: dict[str, Any], host_profile: dict[str, Any], group_of: dict[str, str]
) -> dict[str, Any]:
    """One entry of `parse_config_for_import`'s per-model loop, split out so the caller can
    wrap it in one `try/except` per entry (see there) rather than scattering recovery logic
    through the loop body."""
    notes: list[str] = []
    status = "ok"
    try:
        argv, cleaned_text = _sanitize_cmd(entry["cmd"], notes)
    except ValueError as e:
        # Unbalanced quote — the one case _sanitize_cmd itself can raise on well-formed input.
        return {
            "model": {"id": model_id, "engine": "unmanaged", "cmd": entry["cmd"]},
            "notes": [f"cmd does not tokenize ({e}) — kept verbatim"],
            "status": "review",
        }

    if not argv:
        status = "review"
        notes.append("empty command after stripping comments")
        model: dict[str, Any] = {"engine": "unmanaged", "cmd": ""}
    else:
        base = argv[0].rsplit("/", 1)[-1]
        if base == "llama-server":
            model, engine_status = _classify_llama_cpp(argv, host_profile, notes)
            if engine_status == "review":
                status = "review"
        elif _PYTHON_BASENAME_RE.match(base):
            if len(argv) < 2:
                status = "review"
                notes.append("python interpreter with no script argument")
                model = {"engine": "unmanaged", "cmd": cleaned_text}
            else:
                model = {"engine": "python", "python": argv[0], "script": argv[1]}
                if argv[2:]:
                    model["args"] = argv[2:]
        else:
            model = {"engine": "unmanaged", "cmd": cleaned_text}

    model = {"id": model_id, **model}

    if entry.get("env"):
        model["env"] = list(entry["env"])
    # `in`, not truthiness: ttl 0 is meaningful ("never unload"), so a falsy check dropped
    # exactly the setting an operator was most deliberate about when importing a config.
    if "ttl" in entry:
        model["ttl"] = entry["ttl"]
    if "checkEndpoint" in entry:
        model["check_endpoint"] = entry["checkEndpoint"]
    if "healthCheckTimeout" in entry:
        notes.append(
            f"healthCheckTimeout {entry['healthCheckTimeout']} dropped — llama-swap only "
            "supports it globally (Settings > gateway)"
        )
    if model_id in group_of:
        group_name = group_of[model_id]
        model["group"] = group_name
        # If the model has a positive ttl, llama-swap will still idle-unload it after
        # that timeout despite group persistence (which only resists cross-group evictions).
        if entry.get("ttl", 0) > 0:
            notes.append(
                f"in group {group_name!r} with ttl {entry['ttl']}s: idle-unloads after "
                "timeout (set ttl 0 for permanent residency)"
            )
    for key in entry:
        if key not in _KNOWN_PER_MODEL_KEYS:
            notes.append(f"unsupported key {key!r} dropped")

    return {"model": model, "notes": notes, "status": status}


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

    # One group shape, and it means "pinned" (cockpit DeployScreen._is_pinned): upstream's
    # "forever" group. All three flags are load-bearing — v255 defaults a group to
    # swap: true, exclusive: true, persistent: false (internal/config/config.go:93-100), and
    # this used to emit only swap: false. That left exclusive: true, so loading a group member
    # evicted every ungrouped model, and persistent: false, so loading any ungrouped model
    # (default group, exclusive: true) evicted the whole "always-on" pool
    # (internal/router/group.go:84-96).
    if groups:
        config["groups"] = {
            name: {"swap": False, "exclusive": False, "persistent": True, "members": members}
            for name, members in groups.items()
        }

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
        log.info("swap: %s unit not installed on this host (deploy gateway to configure service)", _UNIT_NAME)
        return "no-unit"
    if state == "":
        return "unknown"
    if _is_active(_UNIT_NAME):
        log.info("swap: restarting %s", _UNIT_NAME)
        _systemctl(runner, "restart", _UNIT_NAME)
        return "restarted"
    log.info("swap: enabling and starting %s", _UNIT_NAME)
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
