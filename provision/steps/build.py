"""Per-backend llama.cpp build: one checkout, one build dir + one versioned prefix per
distinct backend a host GPU actually uses, verified by real inference before the `current`
symlink is ever moved.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from provision.common import Runner, resolve_env_value
from provision.smoke import ensure_smoke_model, load_smoke_fixture, run_smoke_test

log = logging.getLogger("provision")

# A failed build has no binaries — a build-info.json plus a build.log, kilobytes — so it
# never competes with retain_builds' disk-space budget for installed (multi-GB) prefixes.
# It gets its own generous, fixed cap instead: high enough that a real flag-tuning session
# (the reason this whole phase exists) never loses the log it is actively iterating on, low
# enough that the directory can't grow without bound. (Coordinator correction, 2026-09-18:
# an earlier version of _prune_old_builds counted every directory, including failures,
# against retain_builds — a run of failed builds could evict a working, non-current build a
# rollback might need.)
_MAX_FAILED_BUILDS_KEPT = 20

_BUILD_DIR_RE = re.compile(r"^build(\d+)$")


def needed_backends(host_profile: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(b for g in host_profile.get("gpus", []) for b in g.get("backends", [])))


def _git_head(checkout_dir: Path) -> str | None:
    """Read-only — safe to call regardless of --dry-run."""
    git_dir = checkout_dir / ".git"
    if not git_dir.exists():
        return None
    cmd = ["git", "-c", "safe.directory=*", "-C", str(checkout_dir), "rev-parse", "HEAD"]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, OSError):
        pass
    return None


def ensure_checkout(runner: Runner, repo: str, ref: str, checkout_dir: Path) -> None:
    current = _git_head(checkout_dir)
    if current == ref:
        log.info("build: checkout at %s already at pinned ref %s, skipping clone/fetch", checkout_dir, ref)
        return
    git_dir = checkout_dir / ".git"
    is_git_repo = git_dir.exists()
    if not is_git_repo:
        if checkout_dir.is_dir() and any(checkout_dir.iterdir()):
            log.warning("build: %s exists without .git; initializing git in place", checkout_dir)
            runner.run(["git", "-c", "safe.directory=*", "-C", str(checkout_dir), "init"])
            runner.run(["git", "-c", "safe.directory=*", "-C", str(checkout_dir), "remote", "add", "origin", repo], check=False)
            runner.run(["git", "-c", "safe.directory=*", "-C", str(checkout_dir), "remote", "set-url", "origin", repo])
        else:
            runner.mkdir(checkout_dir.parent)
            runner.run(["git", "-c", "safe.directory=*", "clone", repo, str(checkout_dir)])
            runner.run(["git", "-c", "safe.directory=*", "-C", str(checkout_dir), "checkout", ref])
            return

    log.info("build: checkout at %s is at %s, fetching pinned ref %s", checkout_dir, current or "unknown", ref)
    # GitHub serves arbitrary commit SHAs directly, so this reaches `ref` even if it
    # isn't the tip of any branch — no need to fetch full history/refs.
    runner.run(["git", "-c", "safe.directory=*", "-C", str(checkout_dir), "fetch", "origin", ref])
    runner.run(["git", "-c", "safe.directory=*", "-C", str(checkout_dir), "checkout", ref])


fetch_checkout = ensure_checkout


def _binary_sane(prefix: Path, name: str) -> bool:
    p = prefix / "bin" / name
    return p.is_file() and os.access(p, os.X_OK)


def _prefix_ready(prefix: Path) -> bool:
    # Both binaries matter: llama-cli is what the smoke test drives, llama-server is what
    # swap.py's generated config actually runs at serve time.
    return _binary_sane(prefix, "llama-cli") and _binary_sane(prefix, "llama-server")


# Fixed, documented candidate roots for find_foreign_builds() — never a filesystem walk.
# `/opt/llama.cpp` and `/opt/llama.cpp/build-*` are upstream llama.cpp's own in-tree cmake
# layout (a plain `cmake -B build-<backend> && cmake --build build-<backend>`, with no
# `--install`, leaves the binaries at `build-<backend>/bin/`); `/usr/local` is the other
# common hand-install prefix. Module-level, not inlined, for two reasons: it is the one
# readable place to audit what this toolkit looks at outside prefix_root, and it lets a
# fixture test point the scan at a tmp tree the same way smoke/verify_sysinfo.py points
# wol._SYSFS_NET at one — a hermetic test has no permission to create files under the real
# /opt on the machine running it. Production callers never override this.
_FOREIGN_BUILD_ROOTS: tuple[Path, ...] = (Path("/opt/llama.cpp"), Path("/usr/local"))


def find_foreign_builds(host_profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Read-only scan for llama.cpp binaries this toolkit did not install and cannot manage.

    Modelled on wol.find_persistence_unit (wol.py:298-323): a fixed, documented candidate
    list plus a behaviour match on what is actually there, never a recursive walk. Operator
    decision, 2026-09-17 (plans/05-qa-remediation-pass.md Phase 3): detect and surface,
    never write, never adopt, never execute. `prefix_root` stays the single authoritative
    install root.

    Deliberately takes no `Runner` — this function cannot write, symlink, remove, or run
    anything it finds, and the missing parameter makes that structural rather than a
    promise. It also never executes a discovered binary: `sane` is `_binary_sane`, the same
    exec-bit check `_prefix_ready` uses, not an invocation. Running a foreign build to learn
    its version would initialise a GPU backend and wake the device (AGENTS.md → Scope, "Not
    a monitor"; smoke/verify_no_gpu_wake.py) — path and exec-bit only, never a version.

    Returns `[{"path", "kind", "sane"}, ...]` for whatever was found. `kind` names which
    candidate matched, for display. Excludes anything under `prefix_root` (resolved, so a
    `current` symlink — including the one `shutil.which` legitimately resolves to on a
    provisioned host — that points back into prefix_root is excluded too); those builds are
    managed, not foreign.
    """
    prefix_root = Path(host_profile["paths"]["prefix_root"]).resolve()

    candidates: list[tuple[Path, str]] = [(root, str(root)) for root in _FOREIGN_BUILD_ROOTS]
    llama_cpp_root = _FOREIGN_BUILD_ROOTS[0]
    try:
        candidates.extend(
            (p, f"{llama_cpp_root}/build-*") for p in sorted(llama_cpp_root.glob("build-*"))
        )
    except OSError:
        pass
    which = shutil.which("llama-server")
    if which:
        # <bin>/llama-server -> its grandparent is the prefix/build-dir root, matching the
        # other two layouts below.
        candidates.append((Path(which).parent.parent, "PATH (llama-server)"))

    found: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for path, kind in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved == prefix_root or prefix_root in resolved.parents:
            continue  # managed, not foreign
        if not (resolved / "bin" / "llama-server").exists():
            continue  # keeps /usr/local silent on every ordinary host
        found.append({"path": str(resolved), "kind": kind, "sane": _binary_sane(resolved, "llama-server")})
    return found


def _sh(value: str) -> str:
    return shlex.quote(value)


def checkout_dir_for(host_profile: dict[str, Any]) -> Path:
    """Where the pinned llama.cpp source lives — one checkout shared by every backend."""
    return Path(host_profile["paths"]["state_dir"]) / "src" / "llama.cpp"


def resolve_build_dir(host_profile: dict[str, Any], backend: str, build_id: str) -> Path:
    """The install prefix for one already-known build, addressed by its directory name
    (e.g. "build3", or a legacy `<sha>` directory predating this scheme). Purely mechanical —
    callers that need to *allocate a new* build use allocate_build_dir() instead."""
    return Path(host_profile["paths"]["prefix_root"]) / backend / build_id


def extract_manifest_tag(manifest_path: Path, ref: str) -> str | None:
    """Extract trailing comment tag like `# b10903` from manifest.yaml for this ref."""
    if not manifest_path.is_file():
        return None
    try:
        text = manifest_path.read_text()
        m = re.search(r"ref:\s*([0-9a-fA-F]{7,40})\s*#\s*(b\d+)", text)
        if m and (m.group(1).startswith(ref) or ref.startswith(m.group(1))):
            return m.group(2)
    except OSError:
        pass
    return None


def git_tag_for_ref(checkout_dir: Path, ref: str) -> str | None:
    """Check git tags pointing at ref if local repo checkout exists."""
    git_dir = checkout_dir / ".git"
    if not git_dir.exists():
        return None
    try:
        res = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", str(checkout_dir), "tag", "--points-at", ref],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=5,
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                line = line.strip()
                if re.match(r"^b\d+$", line):
                    return line
    except Exception:
        pass
    return None


def allocate_build_dir(
    host_profile: dict[str, Any],
    backend: str,
    ref: str = "",
    tag: str | None = None,
) -> Path:
    """Install directory for a build of this backend: prefix_root/<backend>/build-<tag_or_ref>.
    Uses the release tag (e.g. build-b11037) or short commit SHA (e.g. build-481c65f091).
    Rebuilding the same version updates in place rather than creating build1, build2, etc.
    """
    backend_dir = Path(host_profile["paths"]["prefix_root"]) / backend
    if tag:
        clean_tag = tag if not tag.startswith("build-") else tag.removeprefix("build-")
        build_id = f"build-{clean_tag}"
    elif ref:
        if re.match(r"^b\d+$", ref):
            build_id = f"build-{ref}"
        else:
            build_id = f"build-{ref[:10]}"
    else:
        max_n = 0
        if backend_dir.is_dir():
            for d in backend_dir.iterdir():
                if not d.is_dir() or d.is_symlink():
                    continue
                m = _BUILD_DIR_RE.match(d.name)
                if m:
                    max_n = max(max_n, int(m.group(1)))
        build_id = f"build{max_n + 1}"
    return backend_dir / build_id


def _read_build_metadata(build_dir: Path) -> dict[str, Any] | None:
    """The per-build record written by _write_build_record(), or None for a directory that
    predates it (legacy `<sha>` builds) or whose record is unreadable."""
    path = build_dir / "build-info.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def read_build_metadata(host_profile: dict[str, Any], backend: str, build_id: str) -> dict[str, Any] | None:
    """Public accessor for the full per-build record (ref, cmake_flags, argv, timestamp,
    outcome, detail) — for a cockpit detail view that wants more than list_builds()'s row
    shape. Returns None for a legacy build with no build-info.json."""
    return _read_build_metadata(resolve_build_dir(host_profile, backend, build_id))


def build_log_path(host_profile: dict[str, Any], backend: str, build_id: str) -> Path:
    """Where _write_build_record() puts this build's persisted compile log. The file may not
    exist — e.g. a legacy build, or a build that was skipped because it was already sane."""
    return resolve_build_dir(host_profile, backend, build_id) / "build.log"


def _write_build_record(
    build_dir: Path,
    runner: Runner,
    *,
    backend: str,
    ref: str,
    recipe: dict[str, Any],
    checkout_dir: Path,
    outcome: str,
    detail: str,
    log_lines: list[str],
    argv: list[str] | None = None,
    prebuilt: bool = False,
    asset: str | None = None,
) -> None:
    """Writes build-info.json (always — this is what makes an outcome, including a failure,
    readable) and build.log (only when log_lines is non-empty, i.e. only when a build actually
    ran this call — the idempotent "already built, skipping" path must never blank out an
    existing build's real compile transcript). Both go through Runner so sudo and --dry-run
    stay honest; a dry_run=True Runner writes neither."""
    record: dict[str, Any] = {
        "ref": ref,
        "cmake_flags": resolve_cmake_flags(recipe) if not prebuilt else [],
        "argv": argv if argv is not None else resolve_cmake_argv(backend, recipe, checkout_dir, build_dir),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,  # "smoke_pass" | "smoke_failed" | "build_failed"
        "detail": detail[:2000],
    }
    if prebuilt:
        record["prebuilt"] = True
    if asset:
        record["asset"] = asset
    runner.write_file(build_dir / "build-info.json", json.dumps(record, indent=2) + "\n", mode=0o644)
    if log_lines:
        runner.write_file(build_dir / "build.log", "\n".join(log_lines) + "\n", mode=0o644)
    if not runner.dry_run:
        runner.run(["chmod", "-R", "a+rX", str(build_dir)], check=False)


def _find_matching_build(host_profile: dict[str, Any], backend: str, ref: str) -> Path | None:
    """The newest sane, already-built directory for this backend that was built at `ref` —
    or None. Preserves the old "same ref ⇒ same directory ⇒ skippable" idempotence
    (verify_build_pin.py's verify_force_flag) without directory identity doing the work:
    a build with a record matches by its recorded ref; a legacy `<sha>` directory with no
    record matches the old way, by its own name."""
    backend_dir = Path(host_profile["paths"]["prefix_root"]) / backend
    if not backend_dir.is_dir():
        return None
    dirs = sorted(
        (d for d in backend_dir.iterdir() if d.is_dir() and not d.is_symlink()),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    for d in dirs:
        if not _prefix_ready(d):
            continue
        meta = _read_build_metadata(d)
        built_ref = meta["ref"] if meta is not None else d.name
        if built_ref == ref:
            return d
    return None


def resolve_cmake_flags(recipe: dict[str, Any]) -> list[str]:
    """The `-D...` flags passed to `cmake -B` for this backend's recipe, before the
    positional -B/-DCMAKE_INSTALL_PREFIX/source-dir arguments are added around them.

    Pure — no filesystem access, no Runner — so a display caller (the cockpit's per-backend
    detail view and Build confirm) and the code that actually runs the build
    (`_build_backend`) share one source of truth instead of a display copy that can drift
    from what the compiler receives (plans/05-qa-remediation-pass.md Phase 2 follow-up).

    -DCMAKE_BUILD_TYPE=Release must be set at configure time, not just `--config Release` at
    build time — that flag only matters for multi-config generators (Xcode/MSVC); the default
    Linux Makefile/Ninja generator is single-config and would otherwise build unoptimized.

    -DCMAKE_INSTALL_RPATH embeds relative paths ($ORIGIN/../lib) so installed binaries can
    load companion shared libraries (libllama-cli-impl.so, etc.) without system-wide ld.so.conf.
    """
    flags = [
        "-DCMAKE_BUILD_TYPE=Release",
        "-DCMAKE_INSTALL_RPATH=$ORIGIN/../lib:$ORIGIN/../lib64:$ORIGIN",
        "-DCMAKE_INSTALL_RPATH_USE_LINK_PATH=TRUE",
    ]
    flags.extend(recipe.get("cmake_flags", []))
    return flags


def resolve_cmake_argv(backend: str, recipe: dict[str, Any], checkout_dir: Path, prefix: Path) -> list[str]:
    """The full `cmake -B ...` configure command line for this backend — the exact argv
    `_build_backend` passes to `Runner.run` on the (non-source_script) direct-exec path, so
    anything that shows this to the operator is showing what actually executes."""
    builddir = checkout_dir / f"build-{backend}"
    return ["cmake", "-B", str(builddir), *resolve_cmake_flags(recipe), f"-DCMAKE_INSTALL_PREFIX={prefix}", str(checkout_dir)]


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
    nproc = str(os.cpu_count() or 4)
    source_script = recipe.get("source_script")

    unset_vars: list[str] = []
    # Sanitize CUDACXX: CMake's CMakeDetermineCUDACompiler fails with:
    # "Could not find compiler set in environment variable CUDACXX: <path>"
    # if CUDACXX points to a compiler binary that does not exist on disk.
    for target in (build_env, os.environ):
        val = target.get("CUDACXX")
        if val and not (Path(val.strip()).is_file() or shutil.which(val.strip())):
            log.warning("build[%s]: ignoring invalid CUDACXX=%r (binary not found)", backend, val)
            target.pop("CUDACXX", None)
            if "CUDACXX" not in unset_vars:
                unset_vars.append("CUDACXX")

    if backend == "cuda":
        # Always ensure CUDACXX is unset if it was invalid or not explicitly given in build_env,
        # so host /etc/environment or PAM cannot inject a stale CUDACXX under sudo.
        if "CUDACXX" not in build_env:
            if "CUDACXX" not in unset_vars:
                unset_vars.append("CUDACXX")
            default_nvcc = Path("/usr/local/cuda/bin/nvcc")
            if default_nvcc.is_file():
                build_env["CUDACXX"] = str(default_nvcc)
            elif shutil.which("nvcc"):
                build_env["CUDACXX"] = shutil.which("nvcc")  # type: ignore[assignment]
        cuda_dir = Path("/usr/local/cuda/bin")
        if cuda_dir.is_dir():
            cur_path = build_env.get("PATH") or os.environ.get("PATH", "")
            if str(cuda_dir) not in cur_path.split(os.pathsep):
                build_env["PATH"] = f"{cuda_dir}{os.pathsep}{cur_path}"

    runner.mkdir(prefix.parent, mode=0o755)

    # cmake --install over hand-copying build-<backend>/bin/: llama.cpp's CMakeLists.txt
    # ships standard install() targets for llama-cli/llama-server/libllama, so this gives a
    # complete, correct prefix layout regardless of which targets a given backend produces.
    if source_script:
        # `source` only works inside a shell — the whole configure+build+install has to be
        # one runner.shell call, not separate runner.run() direct-exec calls.
        unsets = "".join(f"unset {u}\n" for u in unset_vars)
        exports = "".join(f"export {k}={_sh(v)}\n" for k, v in build_env.items())
        flags = " ".join(_sh(f) for f in resolve_cmake_flags(recipe))
        script = (
            f"{unsets}"
            f"{exports}"
            f"source {source_script} && "
            f"cmake -B {_sh(str(builddir))} {flags} -DCMAKE_INSTALL_PREFIX={_sh(str(prefix))} "
            f"{_sh(str(checkout_dir))} && "
            f"cmake --build {_sh(str(builddir))} --config Release -j{nproc} && "
            f"cmake --install {_sh(str(builddir))}"
        )
        runner.shell(script)
    else:
        runner.run(
            resolve_cmake_argv(backend, recipe, checkout_dir, prefix),
            env=build_env,
            unset_env=unset_vars,
        )
        runner.run(
            ["cmake", "--build", str(builddir), "--config", "Release", "-j", nproc],
            env=build_env,
            unset_env=unset_vars,
        )
        runner.run(
            ["cmake", "--install", str(builddir)],
            env=build_env,
            unset_env=unset_vars,
        )

    if not runner.dry_run:
        prefix_root = prefix.parent.parent
        backend_dir = prefix.parent
        runner.run(["chmod", "a+rx", str(prefix_root), str(backend_dir)], check=False)
        runner.run(["chmod", "-R", "a+rX", str(prefix)], check=False)


def _prune_old_builds(prefix_root: Path, backend: str, retain: int, runner: Runner) -> None:
    """retain (host_profile['retain_builds']) is a disk-space budget for INSTALLED builds
    only — see schema.py's comment on retain_builds for why. Sane builds (installed,
    multi-GB) and failed builds (a log + a JSON record, kilobytes) are pruned against two
    separate quotas so a run of failed builds can never evict a working, non-current build.
    `current` is never pruned by either path."""
    backend_dir = prefix_root / backend
    current_link = backend_dir / "current"
    current_target = current_link.resolve() if current_link.is_symlink() else None

    dirs = sorted(
        (d for d in backend_dir.iterdir() if d.is_dir() and not d.is_symlink()),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    sane_dirs = [d for d in dirs if d != current_target and _prefix_ready(d)]
    failed_dirs = [d for d in dirs if d != current_target and not _prefix_ready(d)]

    keep: set[Path] = set()
    if current_target is not None:
        keep.add(current_target)
    for d in sane_dirs:
        if len(keep) >= retain:
            break
        keep.add(d)
    for d in sane_dirs:
        if d not in keep:
            log.info("build[%s]: pruning old build %s (retain_builds=%d)", backend, d, retain)
            runner.run(["rm", "-rf", str(d)])

    for d in failed_dirs[_MAX_FAILED_BUILDS_KEPT:]:
        log.info("build[%s]: pruning old failed build %s (cap=%d)", backend, d, _MAX_FAILED_BUILDS_KEPT)
        runner.run(["rm", "-rf", str(d)])


def _history_path(host_profile: dict[str, Any]) -> Path:
    return Path(host_profile["paths"]["state_dir"]) / "build-history.jsonl"


def read_build_history(host_profile: dict[str, Any], backend: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Most-recent-first tail of a pre-Phase-1 build-history.jsonl, optionally filtered to one
    backend. Nothing writes this file any more — Phase 1 folded per-run history into each
    build's own build-info.json (_write_build_record) — so this only ever returns whatever a
    host accumulated before that change. Kept (rather than deleted) so a host with real
    pre-migration history in this file, and the two existing cockpit readers of it, don't
    hard-break; it is expected to be retired once the cockpit's Build History UI is."""
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
    """Every build directory for a backend, most recent first — for the cockpit's Builds list.

    Returns identity and version as separate fields, since a build's directory name (`id`,
    e.g. "build3") is no longer a version the way it was pre-Phase-1: `version` is read from
    the build's own record (build-info.json's `ref`) when one exists. A directory with no
    record — a `<sha>`-named build from before this scheme — is still listed, with its
    directory name reported as its version and `legacy: True`, rather than crashing or being
    hidden.

    Shape: [{"id": str, "version": str, "legacy": bool, "current": bool, "sane": bool,
             "outcome": str | None, "timestamp": str | None}, ...]
    `outcome`/`timestamp` come straight from the record (None for a legacy build) — the
    cockpit's Builds list needs them per row; the full record (cmake_flags, argv, detail) is
    available separately via read_build_metadata() for a detail view.
    """
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
    result = []
    for d in dirs:
        meta = _read_build_metadata(d)
        legacy = meta is None
        result.append({
            "id": d.name,
            "version": meta["ref"] if meta is not None else d.name,
            "legacy": legacy,
            "current": d.resolve() == current_target,
            "sane": _prefix_ready(d),
            "outcome": None if meta is None else meta.get("outcome"),
            "timestamp": None if meta is None else meta.get("timestamp"),
            "prebuilt": False if meta is None else meta.get("prebuilt", False),
        })
    return result


def _resolve_build_dir_strict(host_profile: dict[str, Any], backend: str, build_id: str, *, verb: str) -> Path:
    """Shared path validation for every action that takes an operator-supplied build id and
    touches disk with it (`rollback`, `remove_build`) — one place to get this right rather than
    two copies that could drift. `build_id` must be a bare directory name, one level directly
    under prefix_root/<backend>/ — never an absolute path or one containing a path separator,
    and never "." or "..". Checked on the raw string before any Path join: Path(base) /
    "/etc/passwd" silently discards `base` and evaluates to "/etc/passwd", so the join itself
    cannot be trusted to enforce this. `verb` only shapes the error message (e.g. "roll back",
    "remove").
    """
    if os.path.isabs(build_id) or "/" in build_id or "\\" in build_id or build_id in (".", ".."):
        sys.exit(f"build: invalid build id {build_id!r} — must be a bare directory name")
    prefix_root = Path(host_profile["paths"]["prefix_root"]).resolve()
    backend_dir = prefix_root / backend
    target = (backend_dir / build_id).resolve()
    if target.parent != backend_dir:
        sys.exit(f"build: {build_id!r} does not resolve to a build directly under {backend_dir}")
    return target


def rollback(host_profile: dict[str, Any], backend: str, target_ref: str, runner: Runner) -> None:
    """Point `current` at an already-built, retained prefix — no rebuild. Skips re-smoke-testing:
    this build already passed its smoke test to get built in the first place, and the binary
    hasn't changed since; if that's not good enough, rebuild that ref instead of rolling back.

    target_ref must be a bare directory name — see _resolve_build_dir_strict for the exact
    validation.
    """
    target = _resolve_build_dir_strict(host_profile, backend, target_ref, verb="roll back")
    if not _prefix_ready(target):
        sys.exit(f"build: cannot roll back {backend!r} to {target_ref!r} — {target} is missing or not a sane build")
    backend_dir = target.parent
    runner.atomic_symlink(backend_dir / "current", target)


def remove_build(host_profile: dict[str, Any], backend: str, build_id: str, runner: Runner) -> None:
    """Delete one retained build directory outright — the cockpit's manual "this build is
    known-bad, remove it now" action, distinct from _prune_old_builds' automatic disk-space
    budget. Closes a known deviation (HANDOVER.md): the cockpit used to inline `rm -rf` at the
    call site with no argument validation at all; this gives it the same path hardening
    `rollback` got in d925e91 rather than carrying that gap into a second modal.

    Refuses to remove the build `current` points at — deleting it out from under a running
    llama-swap would break the symlink swap.py's generated config depends on. Rolling back (or
    rebuilding) first is the only way to remove it; that ordering is enforced by the caller
    (BuildsListModal._can_remove) but re-checked here too, since this function is the one thing
    that actually deletes data and must not trust a UI guard alone.
    """
    target = _resolve_build_dir_strict(host_profile, backend, build_id, verb="remove")
    backend_dir = target.parent
    current_link = backend_dir / "current"
    current_target = current_link.resolve() if current_link.is_symlink() else None
    if target == current_target:
        sys.exit(f"build: refusing to remove {backend!r} build {build_id!r} — it is the current build")
    if not target.is_dir():
        sys.exit(f"build: cannot remove {backend!r} build {build_id!r} — {target} does not exist")
    runner.run(["rm", "-rf", str(target)])


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
    needed = needed_backends(host_profile)
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

    checkout_dir = checkout_dir_for(host_profile)
    ensure_checkout(runner, manifest["llama_cpp"]["repo"], manifest["llama_cpp"]["ref"], checkout_dir)

    fixture = load_smoke_fixture(repo_root)
    prefix_root = Path(host_profile["paths"]["prefix_root"])
    ref = manifest["llama_cpp"]["ref"]
    retain = host_profile["retain_builds"]

    manifest_path = repo_root / "manifest.yaml"
    tag = (
        extract_manifest_tag(manifest_path, ref)
        or git_tag_for_ref(checkout_dir, ref)
        or (ref if re.match(r"^b\d+$", ref) else None)
    )

    failed: list[str] = []
    for backend in backends:
        log.info("build: === backend %s ===", backend)
        recipe = manifest["backends"].get(backend)
        if recipe is None:
            sys.exit(
                f"build: backend {backend!r} is used by a GPU in the host profile but has no recipe in "
                f"manifest.yaml backends (defined: {sorted(manifest['backends'])})"
            )

        matched = None if force else _find_matching_build(host_profile, backend, ref)
        log_lines: list[str] = []
        if matched is not None:
            prefix = matched
            log.info("build[%s]: %s already built and sane at %s, skipping build", backend, ref, prefix)
        else:
            prefix = allocate_build_dir(host_profile, backend, ref=ref, tag=tag)
            original_on_output = runner.on_output

            def _capture(line: str, _orig=original_on_output) -> None:
                log_lines.append(line)
                if _orig is not None:
                    _orig(line)

            runner.on_output = _capture
            try:
                _build_backend(backend, recipe, checkout_dir, prefix, runner)
            except (subprocess.CalledProcessError, OSError) as e:
                # A build failure for one backend must not abort the others (constraint: one
                # backend's failure shouldn't block the rest) — record it and move on.
                failed.append(backend)
                log.error("build[%s]: BUILD FAILED — %s — leaving 'current' symlink untouched", backend, e)
                _write_build_record(
                    prefix, runner, backend=backend, ref=ref, recipe=recipe, checkout_dir=checkout_dir,
                    outcome="build_failed", detail=str(e), log_lines=log_lines,
                )
                continue
            finally:
                runner.on_output = original_on_output

        cli_binary = prefix / "bin" / "llama-cli"
        if not cli_binary.exists():
            if runner.dry_run:
                log.info("build[%s]: --dry-run, nothing built yet at %s — skipping smoke test", backend, prefix)
                continue
            failed.append(backend)
            log.error("build[%s]: expected binary %s not present after build — cannot smoke test", backend, cli_binary)
            _write_build_record(
                prefix, runner, backend=backend, ref=ref, recipe=recipe, checkout_dir=checkout_dir,
                outcome="build_failed", detail=f"expected binary {cli_binary} not present after build",
                log_lines=log_lines,
            )
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
            _write_build_record(
                prefix, runner, backend=backend, ref=ref, recipe=recipe, checkout_dir=checkout_dir,
                outcome="smoke_failed", detail=detail, log_lines=log_lines,
            )
            continue

        log.info("build[%s]: smoke test passed", backend)
        _write_build_record(
            prefix, runner, backend=backend, ref=ref, recipe=recipe, checkout_dir=checkout_dir,
            outcome="smoke_pass", detail=detail, log_lines=log_lines,
        )
        # atomic_symlink / runner.run are dry-run-gated internally, same as every other step.
        runner.atomic_symlink(prefix_root / backend / "current", prefix)
        _prune_old_builds(prefix_root, backend, retain, runner)

    if failed:
        sys.exit(f"build: FAILED for backend(s): {', '.join(failed)} — see log above; 'current' left untouched for each")


def download_prebuilt_release(
    host_profile: dict[str, Any],
    manifest: dict[str, Any],
    backend: str,
    asset_name: str,
    asset_url: str,
    *,
    cudart_url: str | None = None,
    runner: Runner,
    repo_root: Path,
) -> Path:
    """Download, extract, and activate an upstream prebuilt release binary for `backend`.

    1. Downloads archive (+ optional companion cudart runtime libraries) via runner (curl).
    2. Extracts into prefix_root/<backend>/build-<tag>.
    3. Copies binaries to bin/ and shared libraries to lib/ and bin/ with proper permissions (chmod 0755/0644).
    4. Writes build-info.json and build.log.
    5. Runs real-inference smoke test against the downloaded binary.
    6. On pass: atomically links current symlink and prunes retained builds.
    7. On fail: leaves current symlink untouched and raises RuntimeError.
    """
    clean = asset_name.removeprefix("cudart-")
    m = re.match(r"^llama-([^-]+)-bin-", clean)
    tag = m.group(1) if m else "prebuilt"

    prefix_root = Path(host_profile["paths"]["prefix_root"])
    backend_dir = prefix_root / backend
    prefix = allocate_build_dir(host_profile, backend, ref=tag, tag=tag)

    workdir = Path(host_profile["paths"]["state_dir"]) / "prebuilt-download" / f"{backend}-{tag}"
    runner.mkdir(workdir, mode=0o755)
    tarball = workdir / asset_name

    log_lines: list[str] = []

    def _log(msg: str) -> None:
        log.info(msg)
        log_lines.append(msg)
        if runner.on_output:
            runner.on_output(msg)

    _log(f"build[{backend}]: downloading {asset_name} from {asset_url}")
    runner.run(["curl", "-fsSL", "-o", str(tarball), asset_url])

    cudart_tarball: Path | None = None
    if cudart_url:
        cudart_name = Path(urllib.parse.urlsplit(cudart_url).path).name or f"cudart-{asset_name}"
        cudart_tarball = workdir / cudart_name
        _log(f"build[{backend}]: downloading companion CUDA runtime from {cudart_url}")
        runner.run(["curl", "-fsSL", "-o", str(cudart_tarball), cudart_url])

    # Extraction
    extract_dir = workdir / "extracted"
    runner.mkdir(extract_dir, mode=0o755)
    _log(f"build[{backend}]: extracting {asset_name}")
    runner.run(["tar", "-xzf", str(tarball), "-C", str(extract_dir)])
    if cudart_tarball:
        _log(f"build[{backend}]: extracting {cudart_tarball.name}")
        runner.run(["tar", "-xzf", str(cudart_tarball), "-C", str(extract_dir)])

    # Target directory structure
    runner.mkdir(prefix / "bin", mode=0o755)
    runner.mkdir(prefix / "lib", mode=0o755)

    if not runner.dry_run:
        # Move/copy files from extracted tree
        copy_script = (
            f"find {_sh(str(extract_dir))} -type f -exec cp -f {{}} {_sh(str(prefix / 'bin'))}/ \\; && "
            f"find {_sh(str(extract_dir))} -type f -name '*.so*' -exec cp -f {{}} {_sh(str(prefix / 'lib'))}/ \\;"
        )
        runner.shell(copy_script)

        # Ensure executable bits on binaries
        for bin_name in ("llama-cli", "llama-server", "llama-bench"):
            p = prefix / "bin" / bin_name
            if p.exists():
                runner.run(["chmod", "0755", str(p)], check=False)

        runner.run(["chmod", "a+rx", str(prefix_root), str(backend_dir)], check=False)
        runner.run(["chmod", "-R", "a+rX", str(prefix)], check=False)

    cli_binary = prefix / "bin" / "llama-cli"
    if not cli_binary.exists() and not runner.dry_run:
        err = f"expected binary {cli_binary} not found after extraction"
        _log(f"build[{backend}]: {err}")
        _write_build_record(
            prefix, runner, backend=backend, ref=tag, recipe={"cmake_flags": []},
            checkout_dir=workdir, outcome="build_failed", detail=err, log_lines=log_lines,
            argv=["download", asset_url], prebuilt=True, asset=asset_name,
        )
        raise RuntimeError(err)

    fixture = load_smoke_fixture(repo_root)
    model_path = ensure_smoke_model(host_profile, fixture, runner)

    _log(f"build[{backend}]: running real-inference smoke test against {cli_binary}")
    recipe = manifest.get("backends", {}).get(backend, {})
    ok, detail = run_smoke_test(cli_binary, model_path, fixture, source_script=recipe.get("source_script"))

    if not ok:
        _log(f"build[{backend}]: SMOKE TEST FAILED — {detail}")
        _write_build_record(
            prefix, runner, backend=backend, ref=tag, recipe={"cmake_flags": []},
            checkout_dir=workdir, outcome="smoke_failed", detail=detail, log_lines=log_lines,
            argv=["download", asset_url], prebuilt=True, asset=asset_name,
        )
        raise RuntimeError(f"smoke test failed for {asset_name}: {detail}")

    _log(f"build[{backend}]: prebuilt smoke test passed")
    _write_build_record(
        prefix, runner, backend=backend, ref=tag, recipe={"cmake_flags": []},
        checkout_dir=workdir, outcome="smoke_pass", detail=detail, log_lines=log_lines,
        argv=["download", asset_url], prebuilt=True, asset=asset_name,
    )
    runner.atomic_symlink(backend_dir / "current", prefix)
    _prune_old_builds(prefix_root, backend, host_profile.get("retain_builds", 3), runner)
    return prefix
