"""Configuration validation for hosts/<hostname>.yaml, manifest.yaml, models.yaml.

Cross-file checks (model binds to a GPU/backend the host profile actually has)
happen here too — the point is a bad configuration should fail at load time,
not at first use on the live host.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

import yaml

__all__ = [
    "ValidationError",
    "HOST_PROFILE_SCHEMA",
    "MANIFEST_SCHEMA",
    "MODELS_SCHEMA",
    "load_host_profile",
    "try_load_host_profile",
    "validate_host_profile_dict",
    "validate_models_dict",
    "validate_scripts_dict",
    "load_manifest",
    "load_models",
    "load_scripts",
]


class ValidationError(Exception):
    """Raised when configuration fails schema or semantic validation."""
    pass

def _require(d: dict, keys: list[str], loc: str) -> None:
    if not isinstance(d, dict):
        raise ValidationError(f"{loc}: expected a dictionary, got {type(d).__name__}")
    for k in keys:
        if k not in d:
            raise ValidationError(f"{loc}: missing required key {k!r}")


def _load_yaml(path: Path) -> dict:
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
            if not isinstance(data, dict):
                raise ValidationError(f"{path}: expected a dictionary at root")
            return data
    except FileNotFoundError:
        raise ValidationError(f"{path}: file not found")
    except yaml.YAMLError as e:
        raise ValidationError(f"{path}: invalid YAML: {e}") from e


def validate_host_profile_dict(
    data: dict, source: str = "host profile", known_backends: Iterable[str] | None = None
) -> None:
    """Validate in-memory host profile dictionary.

    `known_backends` is the manifest's `backends.keys()` — backend recipes are repo-level
    (`manifest.yaml`, see AGENTS.md), so a host profile's `gpus[].backends` entries are only
    as valid as the recipes that exist. When the caller has no manifest to check against
    (`known_backends=None`), the unknown-backend check is skipped here rather than falling
    back to a hardcoded name list: a literal default would itself be an uncredited copy of
    `manifest["backends"].keys()` that silently drifts the moment a recipe is added or
    renamed — exactly the defect this parameter exists to remove. Deferring is not losing the
    check: `validate_models_dict` still rejects a model bound to a backend absent from the
    manifest, and `provision.steps.build.run` hard-exits at build time with "backend 'x' is
    used by a GPU in the host profile but has no recipe in manifest.yaml" — both of which run
    with a manifest in hand by construction. A host profile can therefore pass this function
    naming a backend with no recipe at all; it will not get past either of those.
    """
    _require(data, ["hostname", "network", "gpus", "paths", "retain_builds", "hf"], source)

    net = data["network"]
    _require(net, ["vpn", "wol", "gateway"], f"{source}.network")
    _require(net["vpn"], ["interface"], f"{source}.network.vpn")
    _require(net["wol"], ["interface", "mac"], f"{source}.network.wol")
    if not re.match(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$", str(net["wol"]["mac"])):
        raise ValidationError(f"{source}.network.wol.mac: invalid MAC address format {net['wol']['mac']!r}")
    _require(net["gateway"], ["port"], f"{source}.network.gateway")
    if not isinstance(net["gateway"]["port"], int) or not (1 <= net["gateway"]["port"] <= 65535):
        raise ValidationError(f"{source}.network.gateway.port: port must be integer between 1 and 65535")

    gpus = data["gpus"]
    if not isinstance(gpus, list) or len(gpus) == 0:
        raise ValidationError(f"{source}.gpus: must be a non-empty list")
    for i, g in enumerate(gpus):
        _require(g, ["id", "vendor", "backends"], f"{source}.gpus[{i}]")
        if g["vendor"] not in ("nvidia", "amd", "intel"):
            raise ValidationError(f"{source}.gpus[{i}].vendor: invalid vendor {g['vendor']!r}")
        if not isinstance(g["backends"], list):
            raise ValidationError(f"{source}.gpus[{i}].backends: must be a list")
        if known_backends is not None:
            kb = frozenset(known_backends)
            for b in g["backends"]:
                if b not in kb:
                    raise ValidationError(f"{source}.gpus[{i}].backends: unknown backend {b!r}")

    paths = data["paths"]
    _require(paths, ["models_dir", "state_dir", "prefix_root"], f"{source}.paths")

    if not isinstance(data["retain_builds"], int) or data["retain_builds"] < 1:
        raise ValidationError(f"{source}.retain_builds: must be an integer >= 1")
    # retain_builds is a disk-space budget for INSTALLED builds only (each is a multi-GB
    # compiled prefix) — it is not a count of every build directory. A build that failed to
    # compile or failed its smoke test has no binaries (a build-info.json + a log, kilobytes)
    # and is pruned separately, on its own generous cap — see build.py's
    # _MAX_FAILED_BUILDS_KEPT and _prune_old_builds. Do not "fix" this asymmetry: counting
    # failures against this budget lets a run of failed builds evict a working one a rollback
    # might need (provision/steps/build.py, coordinator correction 2026-09-18).

    _require(data["hf"], ["token_env"], f"{source}.hf")


def validate_manifest_dict(data: dict, source: str = "manifest.yaml") -> None:
    """Validate in-memory manifest dictionary."""
    _require(data, ["llama_cpp", "llama_swap", "huggingface_hub", "backends"], source)
    _require(data["llama_cpp"], ["ref", "repo"], f"{source}.llama_cpp")
    if not re.match(r"^[0-9a-f]{40}$", str(data["llama_cpp"]["ref"])):
        raise ValidationError(f"{source}.llama_cpp.ref: must be a 40-character hex commit SHA")
    _require(data["llama_swap"], ["version", "repo"], f"{source}.llama_swap")
    _require(data["huggingface_hub"], ["version"], f"{source}.huggingface_hub")

    backends = data["backends"]
    if not isinstance(backends, dict) or len(backends) == 0:
        raise ValidationError(f"{source}.backends: must be a non-empty mapping")
    for name, b in backends.items():
        _require(b, ["cmake_flags"], f"{source}.backends.{name}")
        if not isinstance(b["cmake_flags"], list):
            raise ValidationError(f"{source}.backends.{name}.cmake_flags: must be a list")


def validate_models_dict(data: dict, host_profile: dict, manifest: dict, source: str = "models.yaml") -> dict:
    """Validate an in-memory models dict without writing to disk."""
    if not isinstance(data, dict) or "models" not in data or not isinstance(data["models"], list):
        raise ValidationError(f"{source}: expected 'models' list at root")

    gpu_backends = {g["id"]: set(g["backends"]) for g in host_profile["gpus"]}
    known_backends = set(manifest["backends"].keys())
    seen_ids: set[str] = set()

    for i, m in enumerate(data["models"]):
        _require(m, ["id", "engine"], f"{source}.models[{i}]")
        mid = m["id"]
        if mid in seen_ids:
            raise ValidationError(f"{source}: duplicate model id {mid!r}")
        seen_ids.add(mid)

        engine = m["engine"]
        if engine == "llama-cpp":
            _require(m, ["repo_id", "quant_file", "bind"], f"{source}.models[{i}] ({mid})")
            bind = m["bind"]
            _require(bind, ["gpu", "backend"], f"{source}.models[{i}].bind")
            gpu, backend = bind["gpu"], bind["backend"]
            if gpu not in gpu_backends:
                raise ValidationError(
                    f"{source}: model {mid!r} binds to unknown gpu {gpu!r} "
                    f"(host profile defines: {sorted(gpu_backends)})"
                )
            if backend not in gpu_backends[gpu]:
                raise ValidationError(
                    f"{source}: model {mid!r} binds to backend {backend!r} not enabled for gpu {gpu!r} "
                    f"(gpu {gpu!r} allows: {sorted(gpu_backends[gpu])})"
                )
            if backend not in known_backends:
                raise ValidationError(
                    f"{source}: model {mid!r} uses backend {backend!r} not defined in manifest.yaml "
                    f"(manifest defines: {sorted(known_backends)})"
                )
        elif engine == "python":
            _require(m, ["python", "script"], f"{source}.models[{i}] ({mid})")
            if "args" in m:
                args = m["args"]
                if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                    raise ValidationError(f"{source}.models[{i}] ({mid}).args: must be a list of strings")
        elif engine == "unmanaged":
            _require(m, ["cmd"], f"{source}.models[{i}] ({mid})")
        else:
            raise ValidationError(f"{source}.models[{i}] ({mid}): unknown engine {engine!r} (allowed: 'llama-cpp', 'python', 'unmanaged')")

        # env is passed straight through to llama-swap's env: (provision/steps/swap.py
        # _build_model_entry) as a list of "KEY=VALUE" strings — a dict passes no check here
        # today and mis-serializes downstream (plans/05-qa-remediation-pass.md §0h).
        if "env" in m:
            env = m["env"]
            if not isinstance(env, list) or not all(isinstance(e, str) and "=" in e for e in env):
                raise ValidationError(
                    f"{source}.models[{i}] ({mid}).env: must be a list of 'KEY=VALUE' strings"
                )

        # check_endpoint applies to every engine (emitted as llama-swap's checkEndpoint,
        # provision/steps/swap.py _build_model_entry) — upstream expects a path, not a URL.
        if "check_endpoint" in m:
            check_endpoint = m["check_endpoint"]
            if not isinstance(check_endpoint, str) or not check_endpoint.startswith("/"):
                raise ValidationError(
                    f"{source}.models[{i}] ({mid}).check_endpoint: must be a string starting with '/'"
                )

    return data


def validate_scripts_dict(data: dict, source: str = "scripts.yaml") -> dict:
    """Validate an in-memory scripts dict without writing to disk. No cross-file checks against
    host_profile/manifest — unlike models, a registered script doesn't bind to a GPU/backend."""
    if not isinstance(data, dict) or "scripts" not in data or not isinstance(data["scripts"], list):
        raise ValidationError(f"{source}: expected 'scripts' list at root")

    seen_ids: set[str] = set()
    for i, s in enumerate(data["scripts"]):
        _require(s, ["id", "path"], f"{source}.scripts[{i}]")
        sid = s["id"]
        if sid in seen_ids:
            raise ValidationError(f"{source}: duplicate script id {sid!r}")
        seen_ids.add(sid)
        stype = s.get("type", "python")
        if stype not in ("python", "bash"):
            raise ValidationError(f"{source}.scripts[{i}] ({sid}).type: invalid value {stype!r} (allowed: 'python', 'bash')")
        if "args" in s and not isinstance(s["args"], list):
            raise ValidationError(f"{source}.scripts[{i}] ({sid}).args: must be a list")
        if "python" in s and not isinstance(s["python"], str):
            raise ValidationError(f"{source}.scripts[{i}] ({sid}).python: must be a string")
        if "interpreter" in s and not isinstance(s["interpreter"], str):
            raise ValidationError(f"{source}.scripts[{i}] ({sid}).interpreter: must be a string")
        restart_policy = s.get("restart_policy", "on-failure")
        if restart_policy not in ("on-failure", "always", "no"):
            raise ValidationError(f"{source}.scripts[{i}] ({sid}).restart_policy: invalid value {restart_policy!r}")

    return data


def load_host_profile(path: Path, manifest: dict | None = None) -> dict:
    if not path.exists():
        raise ValidationError(f"no host profile at {path} — create hosts/<hostname>.yaml for this machine")
    data = _load_yaml(path)
    known_backends = manifest["backends"].keys() if manifest is not None else None
    validate_host_profile_dict(data, source=str(path), known_backends=known_backends)
    return data


def try_load_host_profile(path: Path, manifest: dict | None = None) -> dict | None:
    if not path.exists():
        return None
    data = _load_yaml(path)
    known_backends = manifest["backends"].keys() if manifest is not None else None
    validate_host_profile_dict(data, source=str(path), known_backends=known_backends)
    return data


def load_manifest(path: Path) -> dict:
    if not path.exists():
        raise ValidationError(
            f"no manifest at {path} — create manifest.yaml for this machine "
            "(copy manifest.example.yaml to get started)"
        )
    data = _load_yaml(path)
    validate_manifest_dict(data, source=str(path))
    return data


def load_models(path: Path, host_profile: dict, manifest: dict) -> dict:
    if not path.exists():
        raise ValidationError(f"no models file at {path} — create models.yaml for this machine")
    data = _load_yaml(path)
    return validate_models_dict(data, host_profile, manifest, source=str(path))


def load_scripts(path: Path) -> dict:
    if not path.exists():
        raise ValidationError(
            f"no scripts file at {path} — create scripts.yaml for this machine "
            "(optional; copy scripts.example.yaml to get started)"
        )
    data = _load_yaml(path)
    return validate_scripts_dict(data, source=str(path))
