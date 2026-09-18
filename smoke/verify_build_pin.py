#!/usr/bin/env python3
"""Phase 2 verification (plans/05-qa-remediation-pass.md), extended by the 2026-09-18
follow-up: manifest pin-write and cmake_flags-write safety, force=True/False build
semantics, and the shared cmake-composition function the follow-up introduced.

Things nothing else in the repo checked before this phase made manifest files writable from
the cockpit for the first time:

1. The header comment block and the `# bNNNNN` trailing comment on llama_cpp.ref must survive a
   pin write. `yaml.safe_dump` destroys both, which is exactly why `_write_manifest_ref`
   (cockpit/screens/builds.py) is a targeted regex line rewrite instead of a load/dump
   round-trip.
2. `build_step.run(..., force=True)` must not take the `_prefix_ready` early return that makes
   `force=False` skip a prefix that already exists — that early return is also the mechanism
   that made the old "Update" button silently do nothing on a second press (plans/05 §0a).
3. `_write_manifest_cmake_flags` (the list-block equivalent of `_write_manifest_ref`) must
   survive on fixtures where cmake_flags is not the backend's first child key, and where the
   target backend sits between two siblings — the two shapes that would break a rewrite that
   assumed cmake_flags's position rather than locating it inside the backend's own bounded
   block.
4. `build_step.resolve_cmake_argv`/`resolve_cmake_flags` — the functions BackendDetailModal
   and the Build confirm now call to display "what will run" — must produce the exact argv
   `_build_backend` passes to `Runner.run`, so the display is provably the same as the build,
   not a second, driftable description of it (coordinator correction, 2026-09-18: this used
   to be a planned "detect drift after the fact" test; extracting one pure function makes it
   true by construction instead, and this test locks that argv's shape as a regression guard).
5. `TableAction.confirm` accepting `str | Callable[[str], str]` (cockpit/widgets.py) must
   leave every existing static-string call site byte-identical — the Build confirm is the only
   caller that now passes a callable.

Run: .venv/bin/python smoke/verify_build_pin.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import bootstrap  # noqa: E402

bootstrap.add_venv_site_packages(_REPO_ROOT)

from cockpit import update_check  # noqa: E402
from cockpit.screens.builds import _short, _write_manifest_cmake_flags, _write_manifest_ref  # noqa: E402
from cockpit.widgets import TableAction  # noqa: E402
from provision import schema, smoke as smoke_step  # noqa: E402
from provision.common import Runner  # noqa: E402
from provision.steps import build as build_step, swap as swap_step  # noqa: E402
from textual._context import active_app  # noqa: E402


class CapturingRunner(Runner):
    """Records what would have been run instead of executing it — same pattern as
    smoke/verify_rendered_units.py's CapturingRunner. Deliberately not dry_run=True: dry-run
    short-circuits Runner.run before the CalledProcessError/return-code branches even matter,
    and more importantly here it is orthogonal to the thing under test — `force` gates a
    filesystem check (_prefix_ready) that runs regardless of dry_run.
    """

    def __init__(self) -> None:
        super().__init__()
        self.run_calls: list[list[str]] = []

    def run(self, cmd, **kwargs):
        self.run_calls.append(list(cmd))
        return None


def verify_manifest_roundtrip() -> None:
    """manifest.example.yaml's header comment block and the ref: line's trailing comment survive a pin
    write, and nothing else in the file changes."""
    original = (_REPO_ROOT / "manifest.example.yaml").read_text()
    header = original.split("llama_cpp:")[0]
    assert header.startswith("# Version pins"), "fixture assumption broken: manifest.example.yaml's own header changed"

    with tempfile.TemporaryDirectory() as td:
        # Case 1: a new ref with a known comment (the "Change version" / resolved-release path).
        new_ref = "b" * 40
        tmp1 = Path(td) / "manifest1.yaml"
        tmp1.write_text(original)
        _write_manifest_ref(tmp1, new_ref, new_comment="b99999")
        after1 = tmp1.read_text()
        assert after1.startswith(header), "header comment block was not preserved"
        assert f"ref: {new_ref}  # b99999" in after1, "new ref/comment line not found verbatim"
        _assert_only_ref_line_changed(original, after1)

        # Case 2: a new ref with no known comment (the "Update to latest" path) — the existing
        # trailing comment must survive untouched.
        tmp2 = Path(td) / "manifest2.yaml"
        tmp2.write_text(original)
        _write_manifest_ref(tmp2, new_ref)
        after2 = tmp2.read_text()
        assert after2.startswith(header), "header comment block was not preserved (no new comment case)"
        assert "# b10903" in after2, "trailing comment was not preserved when no new comment was given"
        assert new_ref in after2, "new ref was not written"
        _assert_only_ref_line_changed(original, after2)

    print("verify_build_pin: manifest header + trailing comment survive a pin write — OK")


def _assert_only_ref_line_changed(before: str, after: str) -> None:
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    assert len(before_lines) == len(after_lines), (
        f"line count changed ({len(before_lines)} -> {len(after_lines)}) — this must be a "
        "targeted line rewrite, not a re-dump"
    )
    diffs = [i for i, (b, a) in enumerate(zip(before_lines, after_lines)) if b != a]
    assert len(diffs) == 1, f"expected exactly one changed line, got {len(diffs)}: {diffs}"
    assert before_lines[diffs[0]].strip().startswith("ref:"), (
        f"the one changed line was not the ref: line: {before_lines[diffs[0]]!r}"
    )


def verify_write_manifest_ref_rejects_non_sha() -> None:
    """plans/06 §0b: a live bug, since Phase 1's build-directory scheme (`build3`, not a SHA)
    means a build id can now reach this function through a caller that forgot to resolve it to
    a real ref first. Before this guard, `_MANIFEST_REF_RE` only constrained the *existing*
    line being replaced — an incoming non-SHA `new_ref` substituted cleanly and reported
    success while writing garbage into manifest ref: line. This is exactly the class of
    bug the contract calls out as needing to have been seen failing, so it is asserted first."""
    original = (_REPO_ROOT / "manifest.example.yaml").read_text()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "manifest.example.yaml"
        tmp.write_text(original)
        before = tmp.read_text()

        raised = False
        try:
            _write_manifest_ref(tmp, "build7")
        except ValueError:
            raised = True
        assert raised, "_write_manifest_ref accepted 'build7' — a build directory id, not a SHA"
        assert tmp.read_text() == before, "a rejected write must not touch the file at all"

        sha = "c" * 40
        _write_manifest_ref(tmp, sha)
        assert f"ref: {sha}" in tmp.read_text(), "_write_manifest_ref rejected a valid 40-hex SHA"
    print("verify_build_pin: _write_manifest_ref rejects a build id, accepts a 40-hex SHA — OK")


def _fixture(tmp_root: Path) -> tuple[dict, dict, dict, str]:
    host_profile = {
        "paths": {
            "state_dir": str(tmp_root / "state"),
            "prefix_root": str(tmp_root / "builds"),
            "models_dir": str(tmp_root / "models"),
        },
        "retain_builds": 3,
        "gpus": [{"backends": ["cuda"]}],
    }
    manifest = {
        "llama_cpp": {"ref": "a" * 40, "repo": "https://github.com/example/llama.cpp"},
        "backends": {"cuda": {"cmake_flags": [], "apt_packages": []}},
    }
    models = {"models": []}
    return host_profile, manifest, models, manifest["llama_cpp"]["ref"]


def _write_fake_prefix(prefix: Path) -> None:
    (prefix / "bin").mkdir(parents=True, exist_ok=True)
    for name in ("llama-completion", "llama-server"):
        p = prefix / "bin" / name
        p.write_text("#!/bin/sh\necho fake\n")
        p.chmod(0o755)


def _run_with_force(tmp_root: Path, *, force: bool) -> list[str]:
    """Runs build_step.run() against a prefix that already exists and is sane, with the real
    cmake/smoke-test machinery replaced by recording stubs — the only thing this isolates is
    whether run() takes the _prefix_ready-and-not-force early return."""
    host_profile, manifest, models, ref = _fixture(tmp_root)
    prefix = Path(host_profile["paths"]["prefix_root"]) / "cuda" / ref
    _write_fake_prefix(prefix)  # prefix already built and sane before run() is even called

    model_path = tmp_root / "smoke-model.gguf"
    model_path.write_bytes(b"")

    build_calls: list[str] = []

    def fake_build_backend(backend, recipe, checkout_dir, px, runner):
        build_calls.append(backend)
        _write_fake_prefix(px)

    orig_build_backend = build_step._build_backend
    orig_ensure_model = build_step.ensure_smoke_model
    orig_run_smoke = build_step.run_smoke_test
    build_step._build_backend = fake_build_backend
    build_step.ensure_smoke_model = lambda *a, **k: model_path
    build_step.run_smoke_test = lambda *a, **k: (True, "ok")
    try:
        runner = CapturingRunner()
        build_step.run(host_profile, manifest, models, runner, _REPO_ROOT, backends=["cuda"], force=force)
    finally:
        build_step._build_backend = orig_build_backend
        build_step.ensure_smoke_model = orig_ensure_model
        build_step.run_smoke_test = orig_run_smoke
    return build_calls


def verify_force_flag() -> None:
    with tempfile.TemporaryDirectory() as td1:
        calls_false = _run_with_force(Path(td1), force=False)
        assert calls_false == [], (
            f"force=False took the build branch anyway on an already-sane prefix: {calls_false}"
        )
    with tempfile.TemporaryDirectory() as td2:
        calls_true = _run_with_force(Path(td2), force=True)
        assert calls_true == ["cuda"], (
            f"force=True did not rebuild an already-sane prefix (this is the Rebuild button's "
            f"whole point): {calls_true}"
        )
    print("verify_build_pin: force=True rebuilds, force=False still skips — OK")


# --------------------------------------------------------------------- Phase 1: per-build dirs


def _make_failed_dir(path: Path) -> None:
    """A build attempt that produced a record but no binaries — build_failed/smoke_failed."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "build-info.json").write_text('{"ref": "deadbeef", "outcome": "build_failed"}')


def verify_sequential_dirs_two_force_builds_first_unmodified() -> None:
    """Build directory naming: uses build-<tag> or build-<sha>. A force=True build of the same
    version updates in place as a rebuild, while a different ref creates a new directory."""
    with tempfile.TemporaryDirectory() as td:
        tmp_root = Path(td)
        host_profile, manifest, models, ref = _fixture(tmp_root)

        model_path = tmp_root / "smoke-model.gguf"
        model_path.write_bytes(b"")

        def fake_build_backend(backend, recipe, checkout_dir, px, runner):
            _write_fake_prefix(px)

        orig_build_backend = build_step._build_backend
        orig_ensure_model = build_step.ensure_smoke_model
        orig_run_smoke = build_step.run_smoke_test
        build_step._build_backend = fake_build_backend
        build_step.ensure_smoke_model = lambda *a, **k: model_path
        build_step.run_smoke_test = lambda *a, **k: (True, "ok")
        try:
            runner = CapturingRunner()
            build_step.run(host_profile, manifest, models, runner, _REPO_ROOT, backends=["cuda"], force=True)

            backend_dir = Path(host_profile["paths"]["prefix_root"]) / "cuda"
            dirs_after_first = sorted(p.name for p in backend_dir.iterdir() if p.is_dir() and not p.is_symlink())
            expected_id = f"build-{ref[:10]}"
            assert dirs_after_first == [expected_id], f"expected [{expected_id}], got {dirs_after_first}"

            # Rebuild at same ref updates in-place
            build_step.run(host_profile, manifest, models, runner, _REPO_ROOT, backends=["cuda"], force=True)
            dirs_after_second = sorted(p.name for p in backend_dir.iterdir() if p.is_dir() and not p.is_symlink())
            assert dirs_after_second == [expected_id], f"rebuild should update in place, got {dirs_after_second}"

            # Build at a different ref allocates a new build directory
            ref2 = "b" * 40
            manifest["llama_cpp"]["ref"] = ref2
            build_step.run(host_profile, manifest, models, runner, _REPO_ROOT, backends=["cuda"], force=True)
            dirs_after_third = sorted(p.name for p in backend_dir.iterdir() if p.is_dir() and not p.is_symlink())
            expected_id2 = f"build-{ref2[:10]}"
            assert dirs_after_third == sorted([expected_id, expected_id2]), (
                f"expected [{expected_id}, {expected_id2}], got {dirs_after_third}"
            )
        finally:
            build_step._build_backend = orig_build_backend
            build_step.ensure_smoke_model = orig_ensure_model
            build_step.run_smoke_test = orig_run_smoke
    print("verify_build_pin: build naming uses build-<version> and rebuilds in place — OK")


def verify_list_builds_shape_and_legacy() -> None:
    """list_builds() returns identity (id) and version as separate fields, backed by each
    build's own build-info.json; a pre-Phase-1 `<sha>`-named directory with no record is
    still listed, with its directory name reported as its version and legacy=True."""
    with tempfile.TemporaryDirectory() as td:
        # .resolve(): on macOS, tempfile's own tmp root has a /var -> /private/var symlink,
        # which would make an unresolved dir compare unequal to current_link.resolve() even
        # though they name the same file. Not a Phase 1 concern — resolving once up front
        # keeps every path built from tmp_root canonical, matching list_builds' own
        # current_link.resolve() comparison.
        tmp_root = Path(td).resolve()
        host_profile, manifest, models, ref = _fixture(tmp_root)
        backend_dir = Path(host_profile["paths"]["prefix_root"]) / "cuda"

        modern = backend_dir / "build1"
        _write_fake_prefix(modern)
        (modern / "build-info.json").write_text(json.dumps({
            "ref": ref, "cmake_flags": [], "argv": [], "timestamp": "2026-09-18T00:00:00+00:00",
            "outcome": "smoke_pass", "detail": "ok",
        }))
        (backend_dir / "current").symlink_to(modern)

        legacy = backend_dir / ("c" * 40)
        _write_fake_prefix(legacy)

        rows = build_step.list_builds(host_profile, "cuda")
        by_id = {r["id"]: r for r in rows}
        print(f"verify_build_pin: list_builds() row for a modern build: {by_id['build1']}")

        assert by_id["build1"]["version"] == ref, "modern build's version must come from its record, not its dir name"
        assert by_id["build1"]["id"] == "build1", "modern build's id must be its directory name"
        assert by_id["build1"]["legacy"] is False
        assert by_id["build1"]["current"] is True
        assert by_id["build1"]["sane"] is True
        assert by_id["build1"]["outcome"] == "smoke_pass"

        legacy_row = by_id["c" * 40]
        assert legacy_row["legacy"] is True, "a build with no build-info.json must be marked legacy"
        assert legacy_row["version"] == "c" * 40, "a legacy build's version must fall back to its directory name"
        assert legacy_row["current"] is False
        assert legacy_row["sane"] is True
        assert legacy_row["outcome"] is None
    print("verify_build_pin: list_builds() separates id/version, marks a recordless legacy dir — OK")


def verify_prune_disk_budget_is_sane_builds_only() -> None:
    """First-ever test for _prune_old_builds. retain_builds is a disk-space budget for
    installed (sane) builds only: `current` is never pruned even when it is the oldest
    directory, exactly `retain_builds` sane builds survive, and — the case that motivated
    the coordinator's correction — several failed builds in a row must NOT evict a sane,
    non-current build to make room for themselves."""
    with tempfile.TemporaryDirectory() as td:
        prefix_root = Path(td).resolve()  # see verify_list_builds_shape_and_legacy's comment
        backend_dir = prefix_root / "cuda"
        backend_dir.mkdir(parents=True)

        current_dir = backend_dir / "build1"  # current, oldest of all — must survive regardless
        _write_fake_prefix(current_dir)
        (backend_dir / "current").symlink_to(current_dir)

        prune_dir = backend_dir / "build2"  # sane, non-current, older of the two -> pruned
        _write_fake_prefix(prune_dir)

        keep_dir = backend_dir / "build3"  # sane, non-current, newer of the two -> kept
        _write_fake_prefix(keep_dir)

        failed_dirs = [backend_dir / f"build{i}" for i in range(4, 9)]  # 5 failed builds
        for d in failed_dirs:
            _make_failed_dir(d)

        now = time.time()
        for age, d in enumerate([current_dir, prune_dir, keep_dir, *failed_dirs]):
            os.utime(d, (now + age, now + age))  # each later in the list is newer

        runner = CapturingRunner()
        build_step._prune_old_builds(prefix_root, "cuda", retain=2, runner=runner)
        pruned = {Path(c[-1]) for c in runner.run_calls}

        assert current_dir not in pruned, "current was pruned even though it is oldest and must never be"
        assert keep_dir not in pruned, "a sane, non-current build within retain_builds was pruned"
        assert prune_dir in pruned, "the sane build exceeding retain_builds=2 should have been pruned"
        assert not (set(failed_dirs) & pruned), (
            "5 failed builds under the 20-build failed-build cap were pruned when they shouldn't be"
        )
        assert pruned == {prune_dir}, (
            f"expected exactly the one excess sane build pruned, got {[str(p) for p in pruned]} — "
            "a failed build must never count against retain_builds"
        )
    print("verify_build_pin: pruning keeps current + retain_builds sane builds; failed builds never evict them — OK")


def verify_rollback_rejects_traversal() -> None:
    """rollback() must reject a target_ref that escapes prefix_root/<backend>/ — checked on
    the raw string before any Path join, since Path(base) / "/etc/passwd" silently discards
    `base`."""
    with tempfile.TemporaryDirectory() as td:
        tmp_root = Path(td)
        host_profile, manifest, models, ref = _fixture(tmp_root)
        backend_dir = Path(host_profile["paths"]["prefix_root"]) / "cuda"
        sane = backend_dir / "build1"
        _write_fake_prefix(sane)

        for bad in ("../../etc", "/etc/passwd", "sub/dir", "..", "."):
            try:
                build_step.rollback(host_profile, "cuda", bad, CapturingRunner())
                raised = False
            except SystemExit:
                raised = True
            assert raised, f"rollback() accepted an invalid target_ref: {bad!r}"

        # A legitimate id is still accepted, to prove the guard isn't over-broad.
        build_step.rollback(host_profile, "cuda", "build1", CapturingRunner())
    print("verify_build_pin: rollback() rejects path traversal, absolute paths, and separators — OK")


def verify_remove_build_rejects_traversal_and_current() -> None:
    """remove_build() gets the same path hardening rollback() got in d925e91 (plans/06 Phase 3
    item — closing the HANDOVER.md deviation where the cockpit used to inline `rm -rf` with no
    validation at all), plus its own guard: it must refuse to delete whichever build `current`
    points at, since that would break the symlink swap.py's generated config depends on."""
    with tempfile.TemporaryDirectory() as td:
        tmp_root = Path(td)
        host_profile, manifest, models, ref = _fixture(tmp_root)
        backend_dir = Path(host_profile["paths"]["prefix_root"]) / "cuda"
        current = backend_dir / "build1"
        retained = backend_dir / "build2"
        _write_fake_prefix(current)
        _write_fake_prefix(retained)
        (backend_dir / "current").symlink_to(current)

        for bad in ("../../etc", "/etc/passwd", "sub/dir", "..", "."):
            try:
                build_step.remove_build(host_profile, "cuda", bad, CapturingRunner())
                raised = False
            except SystemExit:
                raised = True
            assert raised, f"remove_build() accepted an invalid build_id: {bad!r}"

        try:
            build_step.remove_build(host_profile, "cuda", "build1", CapturingRunner())
            raised = False
        except SystemExit:
            raised = True
        assert raised, "remove_build() deleted the build 'current' points at"
        assert current.is_dir(), "the current build was removed from disk despite the guard"

        runner = CapturingRunner()
        build_step.remove_build(host_profile, "cuda", "build2", runner)
        assert any(
            len(c) == 3 and c[0] == "rm" and c[1] == "-rf" and Path(c[2]).resolve() == retained.resolve()
            for c in runner.run_calls
        ), f"remove_build() did not rm -rf the retained build: {runner.run_calls}"
    print("verify_build_pin: remove_build() rejects traversal and refuses to remove the current build — OK")


def verify_metadata_roundtrip_and_dry_run_writes_nothing() -> None:
    """The per-build record round-trips (ref, cmake_flags, argv, outcome, detail all
    readable back exactly as written), and a dry_run=True Runner writes neither
    build-info.json nor build.log."""
    with tempfile.TemporaryDirectory() as td:
        tmp_root = Path(td)
        host_profile, manifest, models, ref = _fixture(tmp_root)
        backend_dir = Path(host_profile["paths"]["prefix_root"]) / "cuda"
        build_dir = backend_dir / "build1"
        recipe = manifest["backends"]["cuda"]
        checkout_dir = build_step.checkout_dir_for(host_profile)

        dry_runner = Runner(dry_run=True)
        build_step._write_build_record(
            build_dir, dry_runner, backend="cuda", ref=ref, recipe=recipe, checkout_dir=checkout_dir,
            outcome="smoke_pass", detail="ok", log_lines=["configuring...", "building..."],
        )
        assert not build_dir.exists(), "a dry_run=True Runner must write nothing at all"

        real_runner = Runner()
        build_step._write_build_record(
            build_dir, real_runner, backend="cuda", ref=ref, recipe=recipe, checkout_dir=checkout_dir,
            outcome="smoke_pass", detail="ok", log_lines=["configuring...", "building..."],
        )
        meta = build_step.read_build_metadata(host_profile, "cuda", "build1")
        assert meta is not None, "build-info.json was not written"
        assert meta["ref"] == ref
        assert meta["cmake_flags"] == build_step.resolve_cmake_flags(recipe)
        assert meta["argv"] == build_step.resolve_cmake_argv("cuda", recipe, checkout_dir, build_dir)
        assert meta["outcome"] == "smoke_pass"
        assert meta["detail"] == "ok"
        log_path = build_step.build_log_path(host_profile, "cuda", "build1")
        assert log_path.read_text() == "configuring...\nbuilding...\n"

        # The idempotent "already built, skipping" path must never blank out an existing
        # build's log: calling _write_build_record with no new log_lines must leave build.log
        # untouched while still refreshing build-info.json's outcome/timestamp.
        build_step._write_build_record(
            build_dir, real_runner, backend="cuda", ref=ref, recipe=recipe, checkout_dir=checkout_dir,
            outcome="smoke_pass", detail="ok (re-verified)", log_lines=[],
        )
        assert log_path.read_text() == "configuring...\nbuilding...\n", (
            "build.log was rewritten (or blanked) by a call with no new log_lines"
        )
        meta_after = build_step.read_build_metadata(host_profile, "cuda", "build1")
        assert meta_after["detail"] == "ok (re-verified)", "build-info.json was not refreshed"
    print("verify_build_pin: build-info.json round-trips; build.log only written when log_lines is non-empty; dry_run writes nothing — OK")


# --------------------------------------------------------------------- cmake_flags block rewrite

_FIXTURE_APT_FIRST = """\
backends:
  cuda:
    apt_packages: []  # CUDA toolkit presence is checked, not installed, by `provision drivers`
    cmake_flags:
      - "-DGGML_CUDA=ON"
      - "-DGGML_NATIVE=OFF"
  vulkan:
    cmake_flags:
      - "-DGGML_VULKAN=ON"
    apt_packages:
      - libvulkan-dev
"""

_FIXTURE_MIDDLE_BACKEND = """\
backends:
  cuda:
    cmake_flags:
      - "-DGGML_CUDA=ON"
  rocm:
    cmake_flags:
      - "-DGGML_HIP=ON"
    build_env:
      HIPCXX: "$(hipconfig -l)/clang"
  vulkan:
    cmake_flags:
      - "-DGGML_VULKAN=ON"
    apt_packages:
      - libvulkan-dev
"""


def verify_cmake_flags_roundtrip_order_independent() -> None:
    """The two shapes that would break a rewrite assuming cmake_flags's position: cmake_flags
    is not the backend's first child key, and the target backend is neither first nor last
    among its siblings. _write_manifest_cmake_flags locates the backend's block by its own key
    line (bounded by the next sibling at the same indent) and then locates cmake_flags inside
    that slice, so neither shape should matter — this proves it rather than asserting it."""
    with tempfile.TemporaryDirectory() as td:
        # cmake_flags is not the first key under cuda: (apt_packages is).
        tmp1 = Path(td) / "apt_first.yaml"
        tmp1.write_text(_FIXTURE_APT_FIRST)
        _write_manifest_cmake_flags(tmp1, "cuda", ["-DGGML_CUDA=ON", "-DGGML_NATIVE=OFF", "-DLLAMA_BUILD_TESTS=OFF"])
        after1 = tmp1.read_text()
        assert '#' in after1.splitlines()[2], "apt_packages' inline comment was not preserved"
        assert "libvulkan-dev" in after1, "vulkan's sibling block was disturbed"
        assert after1.count("cmake_flags:") == 2, "a sibling's cmake_flags key was duplicated or dropped"
        cuda_block = after1.split("vulkan:")[0]
        assert '"-DLLAMA_BUILD_TESTS=OFF"' in cuda_block, "new flag not written into cuda's block"
        vulkan_block = after1.split("vulkan:")[1]
        assert '"-DGGML_VULKAN=ON"' in vulkan_block and "-DGGML_CUDA" not in vulkan_block, (
            "vulkan's cmake_flags were touched by a rewrite targeting cuda"
        )

        # rocm sits between two siblings, and has a non-cmake_flags key (build_env) after it.
        tmp2 = Path(td) / "middle_backend.yaml"
        tmp2.write_text(_FIXTURE_MIDDLE_BACKEND)
        _write_manifest_cmake_flags(tmp2, "rocm", ["-DGGML_HIP=ON", "-DGGML_NATIVE=OFF"])
        after2 = tmp2.read_text()
        assert '"-DGGML_CUDA=ON"' in after2.split("rocm:")[0], "cuda's block (before rocm) was disturbed"
        assert '"-DGGML_VULKAN=ON"' in after2.split("vulkan:")[1], "vulkan's block (after rocm) was disturbed"
        assert "HIPCXX" in after2, "rocm's build_env sibling key was dropped"
        rocm_block = after2.split("rocm:")[1].split("vulkan:")[0]
        assert '"-DGGML_HIP=ON"' in rocm_block and '"-DGGML_NATIVE=OFF"' in rocm_block, (
            "new flags not written into rocm's (middle) block"
        )
    print("verify_build_pin: cmake_flags rewrite is order-independent (apt-first, middle-backend) — OK")


def verify_cmake_flags_roundtrip_real_manifest() -> None:
    """The manifest.example.yaml: header, every sibling backend, and cuda's own apt_packages
    comment must survive a cmake_flags rewrite targeting cuda. Also covers the 2026-09-18
    follow-up's `example: true` mark: a plain rewrite (clear_example=False, the default)
    must leave it in place — the mark means "still stock", and this call doesn't claim to
    know whether the new flags are still stock or not, so it must not touch the key."""
    original = (_REPO_ROOT / "manifest.example.yaml").read_text()
    header = original.split("llama_cpp:")[0]
    assert "example: true" in original.split("vulkan:")[0].split("cuda:")[1], (
        "fixture assumption broken: manifest.example.yaml's cuda recipe no longer has example: true"
    )
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "manifest.example.yaml"
        tmp.write_text(original)
        new_flags = ["-DGGML_CUDA=ON", "-DGGML_NATIVE=OFF", "-DLLAMA_BUILD_TESTS=OFF"]
        _write_manifest_cmake_flags(tmp, "cuda", new_flags)
        after = tmp.read_text()
        assert after.startswith(header), "header comment block was not preserved"
        assert "# CUDA toolkit presence is checked" in after, "cuda's apt_packages comment was lost"
        assert "# ROCm stack presence is checked" in after, "rocm's block/comment was disturbed"
        assert '"-DGGML_VULKAN=ON"' in after, "vulkan's cmake_flags were disturbed"
        assert '"-DGGML_SYCL=ON"' in after, "sycl's cmake_flags were disturbed"
        assert '"-DLLAMA_BUILD_TESTS=OFF"' in after, "new flag was not written"
        cuda_block_after = after.split("cuda:")[1].split("rocm:")[0]
        assert "example: true" in cuda_block_after, (
            "example: true was dropped by a plain rewrite (clear_example=False) — it must "
            "only be removed when explicitly requested"
        )
        # ref: line (a completely different rewrite path) must be untouched by this call.
        assert "481c65f091f74c5e7089dd0a3a1cc6b50cced31e  # b10903" in after, (
            "llama_cpp.ref was disturbed by a cmake_flags-only rewrite"
        )
    print("verify_build_pin: real manifest survives a cmake_flags rewrite, example: true intact — OK")


def verify_example_mark_cleared_on_request() -> None:
    """The same rewrite, with clear_example=True: `example: true` must be removed from
    cuda's block, and only cuda's — every sibling backend's own `example: true` (rocm,
    vulkan, sycl all ship one) must survive untouched, proving the removal is bounded to
    the target backend's block the same way the cmake_flags rewrite itself is."""
    original = (_REPO_ROOT / "manifest.example.yaml").read_text()
    header = original.split("llama_cpp:")[0]
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "manifest.example.yaml"
        tmp.write_text(original)
        new_flags = ["-DGGML_CUDA=ON", "-DGGML_NATIVE=OFF", "-DLLAMA_BUILD_TESTS=OFF"]
        _write_manifest_cmake_flags(tmp, "cuda", new_flags, clear_example=True)
        after = tmp.read_text()
        assert after.startswith(header), "header comment block was not preserved"
        # Scoped to the backends: block so the header comment's own mention of the literal
        # text `example: true` (explaining the key) isn't miscounted as a recipe's key.
        backends_after = after.split("backends:")[1]
        cuda_block_after = backends_after.split("cuda:")[1].split("rocm:")[0]
        assert "example: true" not in cuda_block_after, (
            "example: true was not cleared from cuda's block despite clear_example=True"
        )
        assert backends_after.count("example: true") == 3, (
            "clearing cuda's example: true must not touch rocm/vulkan/sycl's own — "
            f"expected 3 remaining, found {backends_after.count('example: true')}"
        )
    print("verify_build_pin: clear_example=True removes only the target backend's example: true — OK")


def _minimal_valid_host_profile(backends: list[str]) -> dict:
    """The smallest host profile that satisfies every _require() in
    validate_host_profile_dict — everything but gpus[0].backends is filler that only needs
    to be structurally valid, since backend-name validation is what's under test here."""
    return {
        "hostname": "test-host",
        "network": {
            "vpn": {"interface": "wg0"},
            "wol": {"interface": "eth0", "mac": "00:11:22:33:44:55"},
            "gateway": {"port": 8090},
        },
        "gpus": [{"id": "gpu0", "vendor": "nvidia", "backends": backends}],
        "paths": {"models_dir": "/models", "state_dir": "/state", "prefix_root": "/prefix"},
        "retain_builds": 3,
        "hf": {"token_env": "HF_TOKEN"},
    }


def verify_backend_names_derive_from_manifest() -> None:
    """2026-09-18 operator QA: a backend recipe added only to a manifest (not one of the
    historical cuda/rocm/vulkan/sycl four) must be immediately bindable — validate_host_
    profile_dict must accept it when given that manifest's backends, and still reject a name
    in no manifest at all. Also proves the documented no-manifest contract: known_backends=
    None skips the check entirely (deferred to validate_models_dict/build.run, per the
    docstring on validate_host_profile_dict), it does not fall back to a hardcoded list —
    that fallback was the reported bug, and a literal default here would silently reintroduce
    it under a different name."""
    manifest_with_custom_backend = {"backends": {"cuda": {}, "vulkan-igpu": {"cmake_flags": []}}}
    profile_custom = _minimal_valid_host_profile(["vulkan-igpu"])
    # Accepted: vulkan-igpu is a real key in this manifest's backends.
    schema.validate_host_profile_dict(
        profile_custom, known_backends=manifest_with_custom_backend["backends"].keys()
    )

    profile_bogus = _minimal_valid_host_profile(["not-a-real-backend"])
    try:
        schema.validate_host_profile_dict(
            profile_bogus, known_backends=manifest_with_custom_backend["backends"].keys()
        )
        raised = False
    except schema.ValidationError:
        raised = True
    assert raised, "a backend absent from every manifest must still be rejected"

    # No manifest at all: the check is skipped, not defaulted to a hardcoded set — this is
    # the documented contract, not an accident, so the same bogus name passes here.
    schema.validate_host_profile_dict(profile_bogus, known_backends=None)

    print(
        "verify_build_pin: backend names derive from manifest — a manifest-only backend "
        "is accepted, an unknown one is rejected, and known_backends=None defers (doesn't "
        "fall back) — OK"
    )


def verify_cmake_flags_write_aborts_on_bad_backend() -> None:
    """No block to find -> raise, never write a null/partial result."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "manifest.example.yaml"
        tmp.write_text(_FIXTURE_APT_FIRST)
        before = tmp.read_text()
        try:
            _write_manifest_cmake_flags(tmp, "does-not-exist", ["-DX=1"])
            raised = False
        except RuntimeError:
            raised = True
        assert raised, "rewriting a nonexistent backend must raise, not silently no-op"
        assert tmp.read_text() == before, "a failed rewrite must not modify the file"
    print("verify_build_pin: cmake_flags rewrite aborts (without writing) on an unknown backend — OK")


# --------------------------------------------------------------------- shared cmake composition

def verify_resolve_cmake_argv_matches_build() -> None:
    """resolve_cmake_argv/resolve_cmake_flags are what BackendDetailModal and the Build confirm
    now display — this locks their output for the manifest backends actually in use
    (cuda, vulkan) so a future change to the composition is a visible diff here, not silent
    drift between 'what is shown' and 'what runs' (there is only one function now; this test
    is a regression guard on its shape, not a drift detector between two copies)."""
    import yaml as _yaml

    manifest_dict = _yaml.safe_load((_REPO_ROOT / "manifest.example.yaml").read_text())
    host_profile = {"paths": {"state_dir": "/var/lib/llm-server", "prefix_root": "/opt/llm-server/builds"}}
    checkout_dir = build_step.checkout_dir_for(host_profile)
    assert checkout_dir == Path("/var/lib/llm-server/src/llama.cpp")

    for backend in ("cuda", "vulkan"):
        recipe = manifest_dict["backends"][backend]
        prefix = build_step.resolve_build_dir(host_profile, backend, "deadbeef")
        assert prefix == Path("/opt/llm-server/builds") / backend / "deadbeef"
        argv = build_step.resolve_cmake_argv(backend, recipe, checkout_dir, prefix)
        expected = [
            "cmake", "-B", str(checkout_dir / f"build-{backend}"),
            "-DCMAKE_BUILD_TYPE=Release",
            "-DCMAKE_INSTALL_RPATH=$ORIGIN/../lib:$ORIGIN/../lib64:$ORIGIN",
            "-DCMAKE_INSTALL_RPATH_USE_LINK_PATH=TRUE",
            *recipe["cmake_flags"],
            f"-DCMAKE_INSTALL_PREFIX={prefix}", str(checkout_dir),
        ]
        assert argv == expected, f"{backend}: resolve_cmake_argv() = {argv!r}, expected {expected!r}"

    build_calls: list[list[str]] = []

    class _RecordingRunner(Runner):
        def run(self, cmd, **kwargs):
            build_calls.append(list(cmd))
            return None

        def apt_install(self, packages):
            return None

    with tempfile.TemporaryDirectory() as td:
        real_checkout = Path(td) / "checkout"
        prefix = Path(td) / "prefix"
        build_step._build_backend("cuda", manifest_dict["backends"]["cuda"], real_checkout, prefix, _RecordingRunner())
        configure_call = build_calls[0]
        assert configure_call == build_step.resolve_cmake_argv("cuda", manifest_dict["backends"]["cuda"], real_checkout, prefix), (
            "the argv _build_backend actually ran diverged from resolve_cmake_argv()'s display value"
        )
    print("verify_build_pin: resolve_cmake_argv() is what _build_backend actually runs — OK")


def verify_build_cuda_env_sanitization() -> None:
    """Verifies that an invalid CUDACXX in os.environ is stripped and passed to unset_env so
    PAM/sudo cannot inject it into cmake under elevation."""
    import yaml as _yaml

    manifest_dict = _yaml.safe_load((_REPO_ROOT / "manifest.example.yaml").read_text())
    call_kwargs: list[dict] = []

    class _CaptureKwargsRunner(Runner):
        def run(self, cmd, **kwargs):
            call_kwargs.append(dict(kwargs))
            return None

        def apt_install(self, packages):
            return None

    orig_cudacxx = os.environ.get("CUDACXX")
    try:
        os.environ["CUDACXX"] = "/usr/local/cuda-nonexistent/bin/nvcc"
        with tempfile.TemporaryDirectory() as td:
            real_checkout = Path(td) / "checkout"
            prefix = Path(td) / "prefix"
            build_step._build_backend("cuda", manifest_dict["backends"]["cuda"], real_checkout, prefix, _CaptureKwargsRunner())
        assert "CUDACXX" not in os.environ, "invalid CUDACXX was not stripped from os.environ"
        assert len(call_kwargs) >= 1
        assert "CUDACXX" in call_kwargs[0].get("unset_env", []), (
            f"CUDACXX was not included in unset_env: {call_kwargs[0]}"
        )
    finally:
        if orig_cudacxx is not None:
            os.environ["CUDACXX"] = orig_cudacxx
        else:
            os.environ.pop("CUDACXX", None)
    print("verify_build_pin: CUDA CUDACXX sanitization and unset_env propagation — OK")


# --------------------------------------------------------------------- TableAction.confirm callable

def verify_table_action_confirm_callable() -> None:
    """cockpit/widgets.py: TableAction.confirm now accepts str | Callable[[str], str], the same
    treatment `label` already has. Every existing static-string call site must be untouched."""
    static = TableAction("restart", "Restart", confirm="Restart {row}?")
    assert static.confirm_message("cuda") == "Restart cuda?", "static confirm template regressed"

    templated = TableAction("build", lambda row: "Rebuild" if row == "cuda" else "Build", width=11, confirm="{action} llama.cpp ({row})?")
    assert templated.confirm_message("cuda") == "Rebuild llama.cpp (cuda)?"
    assert templated.confirm_message("vulkan") == "Build llama.cpp (vulkan)?"

    seen: list[str] = []

    def _compose(row_key: str) -> str:
        seen.append(row_key)
        return f"composed for {row_key}"

    callable_action = TableAction("build", "Build", confirm=_compose)
    assert callable_action.confirm_message("cuda") == "composed for cuda"
    assert seen == ["cuda"], "callable confirm was not invoked with the row key"

    destructive = TableAction("remove", "Remove", destructive=True)
    assert destructive.confirm_message("x") == "Remove x?", "destructive default prompt regressed"
    print("verify_build_pin: TableAction.confirm accepts str (unchanged) and callable (new) — OK")


# --------------------------------------------------------------------- Phase 1 (plans/07): Status

def _screen_fixture(tmp_root: Path):
    """A bare BuildsScreen — not mounted under a running app, since `_backend_status_cell` and
    `_active_build_cell` touch no DOM (no `query_one`, no CSS) and only need
    `self._building_backends`. Constructing it directly, the same way this file already
    constructs bare `host_profile`/`manifest` fixtures, is cheaper and more honest than
    spinning up a Pilot for two pure-function methods."""
    from cockpit.screens.builds import BuildsScreen

    host_profile, manifest, models, ref = _fixture(tmp_root)
    screen = BuildsScreen(host_profile, manifest, models, CapturingRunner(), _REPO_ROOT, app_ref=None)
    return screen, host_profile, ref


def _write_build(backend_dir: Path, build_id: str, version: str, *, sane: bool = True, current: bool = False) -> Path:
    d = backend_dir / build_id
    if sane:
        _write_fake_prefix(d)
    else:
        d.mkdir(parents=True, exist_ok=True)
    (d / "build-info.json").write_text(json.dumps({"ref": version, "outcome": "smoke_pass"}))
    if current:
        current_link = backend_dir / "current"
        if current_link.exists() or current_link.is_symlink():
            current_link.unlink()
        current_link.symlink_to(d)
    return d


def verify_four_status_states() -> None:
    """The exact table from plans/07 Phase 1 item 3, asserted by text AND colour — the Status
    column's whole reason for existing. `building…` precedence is checked separately below."""
    with tempfile.TemporaryDirectory() as td:
        tmp_root = Path(td).resolve()  # see verify_list_builds_shape_and_legacy's comment
        screen, host_profile, pin = _screen_fixture(tmp_root)
        backend_dir = Path(host_profile["paths"]["prefix_root"]) / "cuda"

        # 1. no build at all.
        builds = build_step.list_builds(host_profile, "cuda")
        current = screen._current_of(builds)
        cell = screen._backend_status_cell("cuda", builds, current, pin)
        assert cell.plain == "not built" and cell.style == "dim", f"not-built cell wrong: {cell.plain!r}/{cell.style!r}"

        # 2. active build's version == pin.
        _write_build(backend_dir, "build1", pin, current=True)
        builds = build_step.list_builds(host_profile, "cuda")
        current = screen._current_of(builds)
        cell = screen._backend_status_cell("cuda", builds, current, pin)
        assert cell.plain == "up to date" and cell.style == "green", f"up-to-date cell wrong: {cell.plain!r}/{cell.style!r}"
        # Active build is neutral now (coordinator correction) — it is an identity, not a
        # second colour-coding of the same up-to-date/out-of-date judgement Status just made.
        active_cell = screen._active_build_cell(current)
        assert not active_cell.style, f"Active build must be neutral, got style={active_cell.style!r}"
        assert active_cell.plain == f"build1 · {_short(pin)}"

        # 3. active != pin, but a sane build AT the pin exists (not active) — operator chose an
        # older build on purpose.
        old_ref = "d" * 40
        _write_build(backend_dir, "build1", old_ref, current=True)  # re-point current to old_ref
        _write_build(backend_dir, "build2", pin, current=False)  # sane, at the pin, not active
        builds = build_step.list_builds(host_profile, "cuda")
        current = screen._current_of(builds)
        cell = screen._backend_status_cell("cuda", builds, current, pin)
        # Names the ACTIVE build's version (old_ref), never the pin. A row reading
        # "pinned at <pin>" beside an Active build of "build1 · <old_ref>" contradicts itself,
        # and puts a global value in a per-row cell — the exact defect _backend_status_cell
        # replaced. This assertion originally encoded the pin and so passed the bug through.
        assert cell.plain == f"pinned at {_short(old_ref)}" and cell.style == "bold yellow", (
            f"pinned-at cell wrong: {cell.plain!r}/{cell.style!r}"
        )
        assert _short(pin) not in cell.plain, (
            f"pinned-at names the manifest pin {_short(pin)!r} instead of the active build"
        )
        active_cell = screen._active_build_cell(current)
        assert not active_cell.style, "Active build coloured a match/mismatch verdict again"

        # 4. active != pin, and NO sane build at the pin exists — genuinely out of date. Remove
        # build2 (the sane build at the pin) so only the stale build1 remains.
        import shutil as _shutil
        _shutil.rmtree(backend_dir / "build2")
        builds = build_step.list_builds(host_profile, "cuda")
        current = screen._current_of(builds)
        cell = screen._backend_status_cell("cuda", builds, current, pin)
        assert cell.plain == f"out of date, built on {_short(old_ref)}" and cell.style == "bold red", (
            f"out-of-date cell wrong: {cell.plain!r}/{cell.style!r}"
        )

        # building… keeps precedence over every other state.
        screen._building_backends.add("cuda")
        cell = screen._backend_status_cell("cuda", builds, current, pin)
        assert cell.plain == "building…" and cell.style == "dim", "building… lost precedence"
    print("verify_build_pin: all four Status states (text + colour), Active build neutral, building… precedence — OK")


def verify_update_does_not_make_stale_build_read_up_to_date() -> None:
    """The reported bug, asserted directly (plans/07 §0a): after 'Update to latest' moves the
    pin, a backend whose active build predates the new pin must read `out of date`, never `up
    to date` — the old code copied the global (now-true) 'pin == upstream latest' fact
    verbatim into every row, so a stale build read exactly what this asserts it must not."""
    with tempfile.TemporaryDirectory() as td:
        tmp_root = Path(td).resolve()
        screen, host_profile, old_ref = _screen_fixture(tmp_root)
        backend_dir = Path(host_profile["paths"]["prefix_root"]) / "cuda"
        _write_build(backend_dir, "build1", old_ref, current=True)

        # Simulate "Update to latest": the pin moves, nothing gets rebuilt.
        new_pin = "e" * 40
        builds = build_step.list_builds(host_profile, "cuda")
        current = screen._current_of(builds)
        cell = screen._backend_status_cell("cuda", builds, current, new_pin)
        assert cell.plain != "up to date", "a build at the OLD ref reads up to date after the pin moved — the reported bug"
        assert cell.plain == f"out of date, built on {_short(old_ref)}" and cell.style == "bold red", (
            f"expected out-of-date, got {cell.plain!r}/{cell.style!r}"
        )
    print("verify_build_pin: a build at the old ref reads 'out of date' (not 'up to date') after Update — OK")


def verify_fetch_checkout_idempotent() -> None:
    """`build_step.fetch_checkout` (the public wrapper `_confirm_and_update_to_latest` calls)
    must run no git command the second time it is called at the same ref — `_ensure_checkout`
    is already idempotent; this proves the wrapper doesn't reimplement or bypass that."""
    calls: list[list[str]] = []

    class _CapRunner(Runner):
        def run(self, cmd, **kwargs):
            calls.append(list(cmd))
            return None

        def mkdir(self, path):
            return None

    # _git_head shells out to real git; faking it is the same technique verify_build_pin
    # already uses for build_step._build_backend etc. — no checkout ever needs to exist on
    # disk to prove the *count* of git commands run.
    seen_head_calls = {"n": 0}

    def fake_git_head(checkout_dir):
        seen_head_calls["n"] += 1
        return None if seen_head_calls["n"] == 1 else "deadbeef" * 5

    orig_git_head = build_step._git_head
    build_step._git_head = fake_git_head
    try:
        runner = _CapRunner()
        checkout_dir = Path("/tmp/verify-fetch-checkout-idempotent")
        ref = "deadbeef" * 5
        build_step.fetch_checkout(runner, "https://github.com/example/llama.cpp", ref, checkout_dir)
        first_count = len(calls)
        assert first_count > 0, "first fetch (no checkout yet) ran no git command at all"
        build_step.fetch_checkout(runner, "https://github.com/example/llama.cpp", ref, checkout_dir)
        assert len(calls) == first_count, (
            f"second fetch at the same ref ran additional git commands: {calls[first_count:]}"
        )
    finally:
        build_step._git_head = orig_git_head
    print("verify_build_pin: fetch_checkout runs no git command on a second call at the same ref — OK")


def verify_fetch_checkout_existing_dir() -> None:
    """When checkout_dir already exists and has .git, fetch_checkout must fetch and checkout
    rather than attempt git clone, even if _git_head is None."""
    calls: list[list[str]] = []

    class _CapRunner(Runner):
        def run(self, cmd, **kwargs):
            calls.append(list(cmd))
            return None

        def mkdir(self, path):
            return None

    with tempfile.TemporaryDirectory() as td:
        checkout_dir = Path(td) / "src" / "llama.cpp"
        (checkout_dir / ".git").mkdir(parents=True)

        orig_git_head = build_step._git_head
        build_step._git_head = lambda d: None
        try:
            runner = _CapRunner()
            ref = "deadbeef" * 5
            build_step.fetch_checkout(runner, "https://github.com/example/llama.cpp", ref, checkout_dir)
            assert not any("clone" in c for c in calls), f"git clone was run on existing checkout: {calls}"
            assert any("fetch" in c for c in calls), f"fetch was not run on existing checkout: {calls}"
            assert any("checkout" in c for c in calls), f"checkout was not run on existing checkout: {calls}"
        finally:
            build_step._git_head = orig_git_head

    print("verify_build_pin: fetch_checkout on existing repo fetches without cloning — OK")


def verify_extract_manifest_tag() -> None:
    """extract_manifest_tag parses trailing '# bNNNN' comment tags from manifest.yaml ref lines."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "manifest.yaml"
        p.write_text("llama_cpp:\n  ref: 71dc5df04ecbc38d1844e1cba5fbfe42cce49bb3 # b4771\n")
        assert build_step.extract_manifest_tag(p, "71dc5df04ecbc38d1844e1cba5fbfe42cce49bb3") == "b4771"
        assert build_step.extract_manifest_tag(p, "unknown_ref") is None
    print("verify_build_pin: extract_manifest_tag parses comment tag correctly — OK")


def verify_ensure_smoke_model_fallback() -> None:
    """When venv-hf is absent, ensure_smoke_model falls back to direct curl download rather than aborting."""
    calls: list[list[str]] = []

    class _CapRunner(Runner):
        def run(self, cmd, **kwargs):
            calls.append(list(cmd))
            return None

        def mkdir(self, path, **kwargs):
            return None

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        hp = {"paths": {"models": str(root / "models"), "state_dir": str(root / "state")}}
        fixture = {"repo_id": "ggml-org/models", "quant_file": "smoke.gguf"}
        runner = _CapRunner()
        dest = smoke_step.ensure_smoke_model(hp, fixture, runner)
        assert dest == root / "state" / "smoke-model" / "smoke.gguf"
        assert any("curl" in c for c in calls), f"curl was not called when venv-hf is absent: {calls}"
        url = "https://huggingface.co/ggml-org/models/resolve/main/smoke.gguf"
        assert any(url in c for c in calls), f"expected {url} in curl call, got: {calls}"
    print("verify_build_pin: ensure_smoke_model falls back to curl when venv-hf missing — OK")


def verify_missing_manifest_fails_with_remedy() -> None:
    """A missing manifest.yaml must fail with ValidationError naming the remedy:
    copying manifest.example.yaml."""
    with tempfile.TemporaryDirectory() as td:
        missing_path = Path(td) / "manifest.yaml"
        raised = False
        try:
            schema.load_manifest(missing_path)
        except schema.ValidationError as e:
            raised = True
            msg = str(e)
            assert "manifest.example.yaml" in msg, f"remedy missing from error: {msg!r}"
        assert raised, "load_manifest succeeded for a non-existent manifest"
    print("verify_build_pin: missing manifest.yaml fails with remedy — OK")


def verify_first_run_creates_manifest_verbatim() -> None:
    """Phase 3: on first run with no manifest.yaml, setup produces one byte-identical to
    the template including comments. Running setup twice does not overwrite an edited
    manifest.yaml."""
    from cockpit.screens.settings import SettingsScreen

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        template = _REPO_ROOT / "manifest.example.yaml"
        shutil.copyfile(template, root / "manifest.example.yaml")
        (root / "hosts").mkdir()

        class _FakeSettingsScreen:
            def __init__(self, repo_root: Path):
                self.repo_root = repo_root
                self.app = None

            _ensure_manifest_copied = SettingsScreen._ensure_manifest_copied

        fake_screen = _FakeSettingsScreen(root)
        target = root / "manifest.yaml"
        assert not target.exists()

        # 1. First run creates manifest.yaml verbatim
        fake_screen._ensure_manifest_copied()
        assert target.exists()
        assert target.read_bytes() == template.read_bytes(), "copied manifest is not byte-identical to template"

        # 2. Edit manifest.yaml
        edited_content = target.read_text() + "\n# custom edit\n"
        target.write_text(edited_content)

        # 3. Second run does not overwrite edited manifest.yaml
        fake_screen._ensure_manifest_copied()
        assert target.read_text() == edited_content, "second setup run overwrote edited manifest.yaml"

    print("verify_build_pin: first run creates manifest verbatim, second run preserves edits — OK")


def verify_swap_install_captures_output_and_errors() -> None:
    """swap._install_llama_swap returns early when already installed, uses capture=True
    during curl/tar/install when updating, and raises descriptive RuntimeError on failure."""
    import subprocess

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        hp = {"paths": {"state_dir": str(root / "state")}}
        manifest = {"llama_swap": {"version": "v256", "repo": "https://github.com/mostlygeek/llama-swap"}}

        # 1. Early return if already installed
        orig_version_fn = swap_step._current_installed_version
        swap_step._current_installed_version = lambda bp: "llama-swap version v256 (amd64)"
        runner = CapturingRunner()
        try:
            res = swap_step._install_llama_swap(hp, manifest, runner)
            assert res == swap_step._BINARY_PATH
            assert runner.run_calls == [], "already installed binary ran install commands anyway"

            # 2. Captures output and raises descriptive RuntimeError on command failure
            swap_step._current_installed_version = lambda bp: "llama-swap version v255 (amd64)"

            class _FailingRunner(Runner):
                def __init__(self) -> None:
                    super().__init__()
                    self.captured_calls: list[tuple[list[str], bool]] = []

                def run(self, cmd, capture=False, **kwargs):
                    self.captured_calls.append((list(cmd), capture))
                    if "curl" in cmd:
                        raise subprocess.CalledProcessError(22, cmd, output="curl: (22) The requested URL returned error: 404")
                    return None

            failing_runner = _FailingRunner()
            try:
                swap_step._install_llama_swap(hp, manifest, failing_runner)
                assert False, "_install_llama_swap should have raised on curl failure"
            except RuntimeError as e:
                assert "curl: (22) The requested URL returned error: 404" in str(e)
                assert "failed (exit 22)" in str(e)

            # 3. Verify capture=True was passed to runner.run
            assert len(failing_runner.captured_calls) == 1
            cmd, capture = failing_runner.captured_calls[0]
            assert "curl" in cmd and capture is True, f"curl was not run with capture=True: {failing_runner.captured_calls}"
        finally:
            swap_step._current_installed_version = orig_version_fn

    print("verify_build_pin: swap install skips when current, captures output and raises on error — OK")


def verify_swap_update_transaction_and_state() -> None:
    """BuildsScreen._run_swap_update passes target_manifest to install_pinned_binary, writes
    manifest.yaml only on success, updates update_check view cache, and updates UI state."""
    from cockpit.screens.builds import BuildsScreen
    from cockpit import update_check

    class _FakeApp:
        def __init__(self) -> None:
            self.notifications: list[str] = []

        def call_from_thread(self, fn, *args, **kwargs):
            return fn(*args, **kwargs)

        def notify(self, msg, **kwargs):
            self.notifications.append(msg)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        manifest_path = root / "manifest.yaml"
        shutil.copyfile(_REPO_ROOT / "manifest.example.yaml", manifest_path)

        real_cache_path = update_check._CACHE_PATH
        update_check._CACHE_PATH = root / "upstream.update-cache.yaml"

        fake_app = _FakeApp()
        token = active_app.set(fake_app)
        try:
            screen = BuildsScreen(
                {"paths": {"state_dir": str(root / "state")}, "gpus": []},
                {"llama_swap": {"version": "v255"}},
                {"models": []},
                CapturingRunner(),
                root,
                app_ref=None,
            )

            # 1. Failure case: install fails -> manifest.yaml untouched
            orig_install = swap_step.install_pinned_binary
            orig_reconcile = swap_step.reconcile_service

            def _fail_install(hp, m, r):
                raise RuntimeError("network down")

            swap_step.install_pinned_binary = _fail_install
            check = {"ok": True, "pinned": "v255", "latest": "v256", "update_available": True}
            BuildsScreen._run_swap_update.__wrapped__(screen, "v256", check)

            disk_content = manifest_path.read_text()
            assert "version: v255" in disk_content, "failed install mutated manifest.yaml on disk!"
            assert screen.manifest.get("llama_swap", {}).get("version") == "v255"
            assert any("llama-swap update failed" in n for n in fake_app.notifications)

            # 2. Success case: install succeeds -> manifest.yaml updated, cache updated, check cleared
            received_manifests: list[dict] = []

            def _succ_install(hp, m, r):
                received_manifests.append(m)

            swap_step.install_pinned_binary = _succ_install
            swap_step.reconcile_service = lambda r: "restarted"

            fake_app.notifications.clear()
            BuildsScreen._run_swap_update.__wrapped__(screen, "v256", check)

            assert len(received_manifests) == 1
            target_manifest = received_manifests[0]
            assert target_manifest.get("llama_swap", {}).get("version") == "v256", (
                f"install_pinned_binary was called with {target_manifest.get('llama_swap', {}).get('version')} "
                "instead of new_version v256"
            )

            assert "version: v256" in manifest_path.read_text(), "manifest.yaml on disk was not updated to v256"
            assert screen.manifest.get("llama_swap", {}).get("version") == "v256", "screen.manifest was not updated"
            assert screen._llama_swap_check is not None
            assert screen._llama_swap_check["update_available"] is False
            assert screen._llama_swap_check["pinned"] == "v256"
            assert any("llama-swap updated to v256 — service restarted" in n for n in fake_app.notifications)
        finally:
            update_check._CACHE_PATH = real_cache_path
            swap_step.install_pinned_binary = orig_install
            swap_step.reconcile_service = orig_reconcile
            active_app.reset(token)

    print("verify_build_pin: swap update transaction installs target version, updates manifest/state/cache, atomic on failure — OK")


def verify_prebuilt_release_asset_discovery() -> None:
    sample_assets = [
        {"name": "llama-b11037-bin-ubuntu-x64.tar.gz", "size": 1000, "browser_download_url": "https://url/cpu"},
        {"name": "llama-b11037-bin-ubuntu-vulkan-x64.tar.gz", "size": 2000, "browser_download_url": "https://url/vulkan"},
        {"name": "llama-b11037-bin-ubuntu-cuda-12.8-x64.tar.gz", "size": 3000, "browser_download_url": "https://url/cuda12"},
        {"name": "cudart-llama-b11037-bin-ubuntu-cuda-12.8-x64.tar.gz", "size": 4000, "browser_download_url": "https://url/cudart12"},
        {"name": "llama-b11037-bin-win-x64.zip", "size": 5000, "browser_download_url": "https://url/win"},
        {"name": "llama-b11037-bin-macos-arm64.tar.gz", "size": 6000, "browser_download_url": "https://url/mac"},
    ]
    parsed = update_check.parse_release_assets(sample_assets, host_arch="x64")
    # Only linux x64 assets should be parsed, windows/mac ignored, cudart paired
    assert len(parsed) == 3, f"Expected 3 parsed assets, got {len(parsed)}"

    cuda = next(a for a in parsed if a["backend"] == "cuda")
    assert cuda["cuda_version"] == "12.8"
    assert cuda["companion_cudart"] is not None
    assert cuda["companion_cudart"]["browser_download_url"] == "https://url/cudart12"

    vulkan = next(a for a in parsed if a["backend"] == "vulkan")
    assert vulkan["companion_cudart"] is None

    rec_cuda = update_check.find_recommended_asset(parsed, target_backend="cuda")
    assert rec_cuda is not None and rec_cuda["backend"] == "cuda"

    rec_vulkan = update_check.find_recommended_asset(parsed, target_backend="vulkan")
    assert rec_vulkan is not None and rec_vulkan["backend"] == "vulkan"

    fallback = update_check.fallback_asset_url("https://github.com/ggml-org/llama.cpp", "b11037", "llama-b11037-bin-ubuntu-vulkan-x64.tar.gz")
    assert fallback == "https://github.com/ggml-org/llama.cpp/releases/download/b11037/llama-b11037-bin-ubuntu-vulkan-x64.tar.gz"
    print("verify_build_pin: prebuilt release asset discovery and recommendation — OK")


def verify_download_prebuilt_pipeline() -> None:
    import tarfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        prefix_root = root / "builds"
        state_dir = root / "state"
        host_profile = {
            "retain_builds": 3,
            "paths": {"prefix_root": str(prefix_root), "state_dir": str(state_dir)},
            "gpus": [{"vendor": "AMD"}],
        }
        manifest = {
            "backends": {
                "vulkan": {"cmake_flags": ["-DGGML_VULKAN=ON"]}
            }
        }

        # Create a mock source directory and tarball
        pkg_dir = root / "pkg"
        pkg_bin = pkg_dir / "bin"
        pkg_bin.mkdir(parents=True)
        (pkg_bin / "llama-completion").write_text("#!/bin/sh\necho llama-completion\n")
        (pkg_bin / "llama-completion").chmod(0o755)
        (pkg_bin / "llama-server").write_text("#!/bin/sh\necho llama-server\n")
        (pkg_bin / "llama-server").chmod(0o755)
        pkg_lib = pkg_dir / "lib"
        pkg_lib.mkdir(parents=True)
        (pkg_lib / "libllama.so").write_text("dummy shared library")

        tar_path = root / "llama-b11037-bin-ubuntu-vulkan-x64.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tar:
            tar.add(pkg_dir, arcname="llama-b11037")

        orig_smoke = build_step.run_smoke_test
        orig_ensure = build_step.ensure_smoke_model
        build_step.run_smoke_test = lambda *args, **kwargs: (True, "mock smoke test pass")
        build_step.ensure_smoke_model = lambda *args, **kwargs: root / "dummy_model.gguf"
        (root / "dummy_model.gguf").write_text("gguf")

        runner = Runner(dry_run=False)
        try:
            prefix = build_step.download_prebuilt_release(
                host_profile,
                manifest,
                "vulkan",
                "llama-b11037-bin-ubuntu-vulkan-x64.tar.gz",
                f"file://{tar_path}",
                runner=runner,
                repo_root=_REPO_ROOT,
            )
            assert prefix.exists()
            assert prefix.name == "build-b11037"
            assert (prefix / "bin" / "llama-completion").exists()
            assert (prefix / "bin" / "llama-server").exists()
            assert (prefix_root / "vulkan" / "current").resolve() == prefix.resolve()

            # Verify build-info.json
            info = build_step._read_build_metadata(prefix)
            assert info is not None
            assert info.get("prebuilt") is True
            assert info.get("ref") == "b11037"
            assert info.get("outcome") == "smoke_pass"

            # Verify list_builds() surfaces prebuilt
            builds = build_step.list_builds(host_profile, "vulkan")
            assert len(builds) == 1
            assert builds[0]["prebuilt"] is True
            assert builds[0]["current"] is True
            assert builds[0]["version"] == "b11037"
        finally:
            build_step.run_smoke_test = orig_smoke
            build_step.ensure_smoke_model = orig_ensure

    print("verify_build_pin: download_prebuilt_release pipeline, metadata and symlink — OK")


def main() -> None:
    verify_missing_manifest_fails_with_remedy()
    verify_first_run_creates_manifest_verbatim()
    verify_swap_install_captures_output_and_errors()
    verify_swap_update_transaction_and_state()
    verify_manifest_roundtrip()
    verify_write_manifest_ref_rejects_non_sha()
    verify_force_flag()
    verify_sequential_dirs_two_force_builds_first_unmodified()
    verify_list_builds_shape_and_legacy()
    verify_prune_disk_budget_is_sane_builds_only()
    verify_rollback_rejects_traversal()
    verify_remove_build_rejects_traversal_and_current()
    verify_metadata_roundtrip_and_dry_run_writes_nothing()
    verify_cmake_flags_roundtrip_order_independent()
    verify_cmake_flags_roundtrip_real_manifest()
    verify_example_mark_cleared_on_request()
    verify_backend_names_derive_from_manifest()
    verify_cmake_flags_write_aborts_on_bad_backend()
    verify_resolve_cmake_argv_matches_build()
    verify_build_cuda_env_sanitization()
    verify_table_action_confirm_callable()
    verify_four_status_states()
    verify_update_does_not_make_stale_build_read_up_to_date()
    verify_fetch_checkout_idempotent()
    verify_fetch_checkout_existing_dir()
    verify_extract_manifest_tag()
    verify_ensure_smoke_model_fallback()
    verify_prebuilt_release_asset_discovery()
    verify_download_prebuilt_pipeline()
    print("verify_build_pin: all checks passed")


if __name__ == "__main__":
    main()
