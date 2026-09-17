#!/usr/bin/env python3
"""Phase 2 verification (plans/05-qa-remediation-pass.md): manifest.yaml pin-write safety and
force=True/False build semantics.

Two things nothing else in the repo checked before this phase made manifest.yaml writable from
the cockpit for the first time:

1. The header comment block and the `# bNNNNN` trailing comment on llama_cpp.ref must survive a
   pin write. `yaml.safe_dump` destroys both, which is exactly why `_write_manifest_ref`
   (cockpit/screens/builds.py) is a targeted regex line rewrite instead of a load/dump
   round-trip.
2. `build_step.run(..., force=True)` must not take the `_prefix_ready` early return that makes
   `force=False` skip a prefix that already exists — that early return is also the mechanism
   that made the old "Update" button silently do nothing on a second press (plans/05 §0a).

Run: .venv/bin/python smoke/verify_build_pin.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import bootstrap  # noqa: E402

bootstrap.add_venv_site_packages(_REPO_ROOT)

from cockpit.screens.builds import _write_manifest_ref  # noqa: E402
from provision.common import Runner  # noqa: E402
from provision.steps import build as build_step  # noqa: E402


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
    """manifest.yaml's header comment block and the ref: line's trailing comment survive a pin
    write, and nothing else in the file changes."""
    original = (_REPO_ROOT / "manifest.yaml").read_text()
    header = original.split("llama_cpp:")[0]
    assert header.startswith("# Version pins"), "fixture assumption broken: manifest.yaml's own header changed"

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

    print("verify_build_pin: manifest.yaml header + trailing comment survive a pin write — OK")


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
    for name in ("llama-cli", "llama-server"):
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


def main() -> None:
    verify_manifest_roundtrip()
    verify_force_flag()
    print("verify_build_pin: all checks passed")


if __name__ == "__main__":
    main()
