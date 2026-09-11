"""jsonschema validation for hosts/<hostname>.yaml, manifest.yaml, models.yaml.

Cross-file checks (model binds to a GPU/backend the host profile actually has)
happen here too — the point is a bad configuration should fail at load time,
not at first use on the live host.
"""
from __future__ import annotations

import sys
from pathlib import Path

import jsonschema
import yaml

HOST_PROFILE_SCHEMA = {
    "type": "object",
    "required": ["hostname", "network", "gpus", "paths", "retain_builds", "hf"],
    "additionalProperties": False,
    "properties": {
        "hostname": {"type": "string"},
        "service": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "restart_policy": {"type": "string", "enum": ["on-failure", "always", "no"]},
                "restart_sec": {"type": "integer", "minimum": 0},
                "scheduled_restart": {
                    "type": "object",
                    "required": ["enabled"],
                    "additionalProperties": False,
                    "properties": {
                        "enabled": {"type": "boolean"},
                        "on_calendar": {"type": "string"},  # systemd OnCalendar= syntax, e.g. "daily"
                    },
                },
            },
        },
        "update_check": {
            "type": "object",
            "required": ["enabled"],
            "additionalProperties": False,
            "properties": {
                "enabled": {"type": "boolean"},
                "on_calendar": {"type": "string"},
            },
        },
        "network": {
            "type": "object",
            "required": ["vpn", "wol", "gateway"],
            "additionalProperties": False,
            "properties": {
                "vpn": {
                    "type": "object",
                    "required": ["interface"],
                    "additionalProperties": False,
                    "properties": {
                        # The overlay NIC name (e.g. tailscale0, wg0) — never an IP. The
                        # gateway binds to whatever address this interface currently has,
                        # resolved fresh on every apply (see swap.py's resolve_vpn_ip).
                        "interface": {"type": "string"},
                    },
                },
                "wol": {
                    "type": "object",
                    "required": ["interface", "mac"],
                    "additionalProperties": False,
                    "properties": {
                        "interface": {"type": "string"},
                        "mac": {"type": "string", "pattern": "^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$"},
                    },
                },
                "gateway": {
                    "type": "object",
                    "required": ["port"],
                    "additionalProperties": False,
                    "properties": {
                        "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                        "health_check_timeout": {"type": "integer", "minimum": 1},
                    },
                },
            },
        },
        "gpus": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "vendor", "backends"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "vendor": {"type": "string", "enum": ["nvidia", "amd", "intel"]},
                    "backends": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "enum": ["cuda", "rocm", "vulkan", "sycl"]},
                    },
                },
            },
        },
        "paths": {
            "type": "object",
            "required": ["models_dir", "state_dir", "prefix_root"],
            "additionalProperties": False,
            "properties": {
                "models_dir": {"type": "string"},
                "state_dir": {"type": "string"},
                "prefix_root": {"type": "string"},
            },
        },
        "retain_builds": {"type": "integer", "minimum": 1},
        "hf": {
            "type": "object",
            "required": ["token_env"],
            "additionalProperties": False,
            "properties": {"token_env": {"type": "string"}},
        },
    },
}

MANIFEST_SCHEMA = {
    "type": "object",
    "required": ["llama_cpp", "llama_swap", "huggingface_hub", "textual", "backends"],
    "additionalProperties": False,
    "properties": {
        "llama_cpp": {
            "type": "object",
            "required": ["ref", "repo"],
            "additionalProperties": False,
            "properties": {
                "ref": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
                "repo": {"type": "string"},
            },
        },
        "llama_swap": {
            "type": "object",
            "required": ["version", "repo"],
            "additionalProperties": False,
            "properties": {"version": {"type": "string"}, "repo": {"type": "string"}},
        },
        "huggingface_hub": {
            "type": "object",
            "required": ["version"],
            "additionalProperties": False,
            "properties": {"version": {"type": "string"}},
        },
        "textual": {
            "type": "object",
            "required": ["version"],
            "additionalProperties": False,
            "properties": {"version": {"type": "string"}},
        },
        "backends": {
            "type": "object",
            "minProperties": 1,
            "additionalProperties": {
                "type": "object",
                "required": ["cmake_flags"],
                "additionalProperties": False,
                "properties": {
                    "cmake_flags": {"type": "array", "items": {"type": "string"}},
                    "apt_packages": {"type": "array", "items": {"type": "string"}},
                    "build_env": {"type": "object", "additionalProperties": {"type": "string"}},
                    "source_script": {"type": "string"},
                },
            },
        },
    },
}

_LLAMA_CPP_MODEL = {
    "type": "object",
    "required": ["id", "engine", "repo_id", "quant_file", "bind"],
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string"},
        "engine": {"const": "llama-cpp"},
        "repo_id": {"type": "string"},
        "quant_file": {"type": "string"},
        "mmproj_file": {"type": "string"},
        "bind": {
            "type": "object",
            "required": ["gpu", "backend"],
            "additionalProperties": False,
            "properties": {"gpu": {"type": "string"}, "backend": {"type": "string"}},
        },
        "llama_server_args": {"type": "array", "items": {"type": "string"}},
        "env": {"type": "array", "items": {"type": "string"}},
        "ttl": {"type": "integer", "minimum": 0},
        "group": {"type": "string"},
    },
}

_UNMANAGED_MODEL = {
    "type": "object",
    "required": ["id", "engine", "cmd"],
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string"},
        "engine": {"const": "unmanaged"},
        "cmd": {"type": "string"},
        "env": {"type": "array", "items": {"type": "string"}},
        "ttl": {"type": "integer", "minimum": 0},
        "group": {"type": "string"},
    },
}

MODELS_SCHEMA = {
    "type": "object",
    "required": ["models"],
    "additionalProperties": False,
    "properties": {
        "models": {
            "type": "array",
            "items": {"oneOf": [_LLAMA_CPP_MODEL, _UNMANAGED_MODEL]},
        },
    },
}


def _load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _validate(path: Path, schema_obj: dict) -> dict:
    data = _load_yaml(path)
    try:
        jsonschema.validate(data, schema_obj)
    except jsonschema.ValidationError as e:
        loc = "/".join(str(p) for p in e.path) or "<root>"
        sys.exit(f"{path}: schema validation failed at {loc}: {e.message}")
    return data


def load_host_profile(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"no host profile at {path} — create hosts/<hostname>.yaml for this machine")
    return _validate(path, HOST_PROFILE_SCHEMA)


def try_load_host_profile(path: Path) -> dict | None:
    """Like load_host_profile, but returns None instead of exiting when the file is missing —
    for the cockpit's first-run flow, which offers to create one instead of refusing to start.
    A file that exists but fails validation still exits; that's a real error, not a first run."""
    if not path.exists():
        return None
    return _validate(path, HOST_PROFILE_SCHEMA)


def validate_host_profile_dict(data: dict) -> None:
    """Validate an in-memory host profile dict (e.g. built by the Settings tab's form) without
    touching disk — raises jsonschema.ValidationError on failure, doesn't sys.exit, so a caller
    building interactive UI can catch it and show the error inline."""
    jsonschema.validate(data, HOST_PROFILE_SCHEMA)


def load_manifest(path: Path) -> dict:
    return _validate(path, MANIFEST_SCHEMA)


def load_models(path: Path, host_profile: dict, manifest: dict) -> dict:
    data = _validate(path, MODELS_SCHEMA)
    gpu_backends = {g["id"]: set(g["backends"]) for g in host_profile["gpus"]}
    known_backends = set(manifest["backends"].keys())
    seen_ids: set[str] = set()
    for m in data["models"]:
        if m["id"] in seen_ids:
            sys.exit(f"{path}: duplicate model id {m['id']!r}")
        seen_ids.add(m["id"])
        if m["engine"] != "llama-cpp":
            continue
        gpu, backend = m["bind"]["gpu"], m["bind"]["backend"]
        if gpu not in gpu_backends:
            sys.exit(
                f"{path}: model {m['id']!r} binds to unknown gpu {gpu!r} "
                f"(host profile defines: {sorted(gpu_backends)})"
            )
        if backend not in gpu_backends[gpu]:
            sys.exit(
                f"{path}: model {m['id']!r} binds to backend {backend!r} not enabled for gpu {gpu!r} "
                f"(gpu {gpu!r} allows: {sorted(gpu_backends[gpu])})"
            )
        if backend not in known_backends:
            sys.exit(
                f"{path}: model {m['id']!r} uses backend {backend!r} not defined in manifest.yaml "
                f"(manifest defines: {sorted(known_backends)})"
            )
    return data
