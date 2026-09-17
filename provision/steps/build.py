"""Per-backend llama.cpp build: one checkout, one build dir + one versioned prefix per
distinct backend a host GPU actually uses, verified by real inference before the `current`
symlink is ever moved.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from provision.common import Runner, resolve_env_value
from provision.smoke import ensure_smoke_model, load_smoke_fixture, run_smoke_test

log = logging.getLogger("provision")


def _needed_backends(host_profile: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    for gpu in host_profile["gpus"]:
        for backend in gpu["backends"]:
            if backend not in seen:
                seen.append(backend)
    return seen


def _git_head(checkout_dir: Path) -> str | None:
    """Read-only — safe to call regardless of --dry-run."""
    if not (checkout_dir / ".git").is_dir():
        return None
    result = subprocess.run(
        ["git", "-C", str(checkout_dir), "rev-parse", "HEAD"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _ensure_checkout(runner: Runner, repo: str, ref: str, checkout_dir: Path) -> None:
    current = _git_head(checkout_dir)
    if current == ref:
        log.info("build: checkout at %s already at pinned ref %s, skipping clone/fetch", checkout_dir, ref)
        return
    if current is None:
        runner.mkdir(checkout_dir.parent)
        runner.run(["git", "clone", repo, str(checkout_dir)])
    else:
        log.info("build: checkout at %s is at %s, fetching pinned ref %s", checkout_dir, current, ref)
        # GitHub serves arbitrary commit SHAs directly, so this reaches `ref` even if it
        # isn't the tip of any branch — no need to fetch full history/refs.
        runner.run(["git", "-C", str(checkout_dir), "fetch", "origin", ref])
    runner.run(["git", "-C", str(checkout_dir), "checkout", ref])


def _binary_sane(prefix: Path, name: str) -> bool:
    p = prefix / "bin" / name
    return p.is_file() and os.access(p, os.X_OK)


def _prefix_ready(prefix: Path) -> bool:
    # Both binaries matter: llama-cli is what the smoke test drives, llama-server is what
    # swap.py's generated config actually runs at serve time.
    return _binary_sane(prefix, "llama-cli") and _binary_sane(prefix, "llama-server")


def _sh(value: str) -> str:
    return shlex.quote(value)


def _build_backend(
    backend: str,
    recipe: dict[str, Any],
    checkout_dir: Path,
    prefix: Path,
    runner: Runner,
) -> None:
    builddir = checkout_dir / f"build-{backend}"
    runner.apt_install(recipe.get("apt_packages", []))

    build_env = {k: resolve_env_value(v) for k, v in recipe.get("build_env", {}).items()}
    # -DCMAKE_BUILD_TYPE=Release must be set at configure time, not just `--config Release` at
    # build time — that flag only matters for multi-config generators (Xcode/MSVC); the default
    # Linux Makefile/Ninja generator is single-config and would otherwise build unoptimized.
    cmake_flags = ["-DCMAKE_BUILD_TYPE=Release", *recipe["cmake_flags"]]
    nproc = str(os.cpu_count() or 4)
    source_script = recipe.get("source_script")

    # cmake --install over hand-copying build-<backend>/bin/: llama.cpp's CMakeLists.txt
    # ships standard install() targets for llama-cli/llama-server/libllama, so this gives a
    # complete, correct prefix layout regardless of which targets a given backend produces.
    if source_script:
        # `source` only works inside a shell — the whole configure+build+install has to be
        # one runner.shell call, not separate runner.run() direct-exec calls.
        exports = "".join(f"export {k}={_sh(v)}\n" for k, v in build_env.items())
        flags = " ".join(_sh(f) for f in cmake_flags)
        script = (
            f"{exports}"
            f"source {source_script} && "
            f"cmake -B {_sh(str(builddir))} {flags} -DCMAKE_INSTALL_PREFIX={_sh(str(prefix))} "
            f"{_sh(str(checkout_dir))} && "
            f"cmake --build {_sh(str(builddir))} --config Release -j{nproc} && "
            f"cmake --install {_sh(str(builddir))}"
        )
        runner.shell(script)
    else:
        env = {**os.environ, **build_env} if build_env else None
        runner.run(
            ["cmake", "-B", str(builddir), *cmake_flags, f"-DCMAKE_INSTALL_PREFIX={prefix}", str(checkout_dir)],
            env=env,
        )
        runner.run(["cmake", "--build", str(builddir), "--config", "Release", "-j", nproc], env=env)
        runner.run(["cmake", "--install", str(builddir)], env=env)


def _prune_old_builds(prefix_root: Path, backend: str, retain: int, runner: Runner) -> None:
    backend_dir = prefix_root / backend
    current_link = backend_dir / "current"
    current_target = current_link.resolve() if current_link.is_symlink() else None

    dirs = sorted(
        (d for d in backend_dir.iterdir() if d.is_dir() and not d.is_symlink()),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )

    keep: set[Path] = set()
    if current_target is not None:
        keep.add(current_target)
    for d in dirs:
        if len(keep) >= retain:
            break
        keep.add(d)

    for d in dirs:
        if d not in keep:
            log.info("build[%s]: pruning old build %s (retain_builds=%d)", backend, d, retain)
            runner.run(["rm", "-rf", str(d)])


def _history_path(host_profile: dict[str, Any]) -> Path:
    return Path(host_profile["paths"]["state_dir"]) / "build-history.jsonl"


def _record_history(host_profile: dict[str, Any], runner: Runner, *, backend: str, ref: str, outcome: str, detail: str = "") -> None:
    """Append-only evidence log for the cockpit's Installs tab — real smoke-test history, not
    just "the binary currently exists". Never written under --dry-run (nothing real happened)."""
    if runner.dry_run:
        return
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "backend": backend,
        "ref": ref,
        "outcome": outcome,  # "smoke_pass" | "smoke_failed" | "build_failed"
        "detail": detail[:2000],
    }
    path = _history_path(host_profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def read_build_history(host_profile: dict[str, Any], backend: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Most-recent-first tail of the build history log, optionally filtered to one backend."""
    path = _history_path(host_profile)
    if not path.exists():
        return []
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if backend is None or entry["backend"] == backend:
                entries.append(entry)
    entries.reverse()
    return entries[:limit]


def list_builds(host_profile: dict[str, Any], backend: str) -> list[dict[str, Any]]:
    """Retained build dirs for a backend, most recent first — for the cockpit's Installs tab."""
    backend_dir = Path(host_profile["paths"]["prefix_root"]) / backend
    if not backend_dir.is_dir():
        return []
    current_link = backend_dir / "current"
    current_target = current_link.resolve() if current_link.is_symlink() else None
    dirs = sorted(
        (d for d in backend_dir.iterdir() if d.is_dir() and not d.is_symlink()),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    return [{"ref": d.name, "current": d == current_target, "sane": _prefix_ready(d)} for d in dirs]


def rollback(host_profile: dict[str, Any], backend: str, target_ref: str, runner: Runner) -> None:
    """Point `current` at an already-built, retained prefix — no rebuild. Skips re-smoke-testing:
    this build already passed its smoke test to get built in the first place, and the binary
    hasn't changed since; if that's not good enough, rebuild that ref instead of rolling back."""
    target = Path(host_profile["paths"]["prefix_root"]) / backend / target_ref
    if not _prefix_ready(target):
        sys.exit(f"build: cannot roll back {backend!r} to {target_ref!r} — {target} is missing or not a sane build")
    runner.atomic_symlink(Path(host_profile["paths"]["prefix_root"]) / backend / "current", target)


def run(
    host_profile: dict[str, Any],
    manifest: dict[str, Any],
    models: dict[str, Any],
    runner: Runner,
    repo_root: Path,
    backends: list[str] | None = None,
    force: bool = False,
) -> None:
    """`backends`, when given, restricts the build to that subset (e.g. the cockpit's Installs
    tab building only the ticked rows) instead of every backend the host needs — everything
    else (checkout, smoke test, retained-build pruning, history) is unchanged. Defaults to
    every needed backend, matching every existing caller (CLI, bin/provision).

    `force=True` skips the "prefix already built and sane" shortcut and rebuilds even a
    prefix that already exists — the cockpit's "Rebuild" action (plans/05 Phase 2 item 5).
    Every existing caller defaults to False, so the already-pinned-SHA idempotence CLI callers
    rely on is unchanged.
    """
    needed = _needed_backends(host_profile)
    if backends is None:
        backends = needed
    else:
        unknown = [b for b in backends if b not in needed]
        if unknown:
            sys.exit(
                f"build: backend(s) {unknown} were requested but this host profile doesn't need "
                f"them (needed: {needed})"
            )
    if not backends:
        log.info("build: no GPU in host profile declares any backend — nothing to build")
        return

    checkout_dir = Path(host_profile["paths"]["state_dir"]) / "src" / "llama.cpp"
    _ensure_checkout(runner, manifest["llama_cpp"]["repo"], manifest["llama_cpp"]["ref"], checkout_dir)

    fixture = load_smoke_fixture(repo_root)
    prefix_root = Path(host_profile["paths"]["prefix_root"])
    ref = manifest["llama_cpp"]["ref"]
    retain = host_profile["retain_builds"]

    failed: list[str] = []
    for backend in backends:
        log.info("build: === backend %s ===", backend)
        recipe = manifest["backends"].get(backend)
        if recipe is None:
            sys.exit(
                f"build: backend {backend!r} is used by a GPU in the host profile but has no recipe in "
                f"manifest.yaml backends (defined: {sorted(manifest['backends'])})"
            )

        prefix = prefix_root / backend / ref
        try:
            if _prefix_ready(prefix) and not force:
                log.info("build[%s]: prefix %s already built and sane, skipping build", backend, prefix)
            else:
                _build_backend(backend, recipe, checkout_dir, prefix, runner)
        except (subprocess.CalledProcessError, OSError) as e:
            # A build failure for one backend must not abort the others (constraint: one
            # backend's failure shouldn't block the rest) — record it and move on.
            failed.append(backend)
            log.error("build[%s]: BUILD FAILED — %s — leaving 'current' symlink untouched", backend, e)
            _record_history(host_profile, runner, backend=backend, ref=ref, outcome="build_failed", detail=str(e))
            continue

        cli_binary = prefix / "bin" / "llama-cli"
        if not cli_binary.exists():
            if runner.dry_run:
                log.info("build[%s]: --dry-run, nothing built yet at %s — skipping smoke test", backend, prefix)
                continue
            failed.append(backend)
            log.error("build[%s]: expected binary %s not present after build — cannot smoke test", backend, cli_binary)
            continue

        model_path = ensure_smoke_model(host_profile, fixture, runner)
        if not model_path.exists():
            # Only reachable under --dry-run (ensure_smoke_model hard-fails otherwise).
            log.info("build[%s]: --dry-run, smoke model not downloaded — skipping smoke test", backend)
            continue

        log.info(
            "build[%s]: running real-inference smoke test against %s%s",
            backend, cli_binary, " (read-only diagnostic under --dry-run)" if runner.dry_run else "",
        )
        ok, detail = run_smoke_test(cli_binary, model_path, fixture, source_script=recipe.get("source_script"))
        if not ok:
            failed.append(backend)
            log.error("build[%s]: SMOKE TEST FAILED — %s — leaving 'current' symlink untouched", backend, detail)
            _record_history(host_profile, runner, backend=backend, ref=ref, outcome="smoke_failed", detail=detail)
            continue

        log.info("build[%s]: smoke test passed", backend)
        _record_history(host_profile, runner, backend=backend, ref=ref, outcome="smoke_pass", detail=detail)
        # atomic_symlink / runner.run are dry-run-gated internally, same as every other step.
        runner.atomic_symlink(prefix_root / backend / "current", prefix)
        _prune_old_builds(prefix_root, backend, retain, runner)

    if failed:
        sys.exit(f"build: FAILED for backend(s): {', '.join(failed)} — see log above; 'current' left untouched for each")
