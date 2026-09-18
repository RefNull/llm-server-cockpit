#!/usr/bin/env python3
"""Phase 2 verification (plans/05-qa-remediation-pass.md), extended by the 2026-09-18
follow-up: manifest.yaml pin-write and cmake_flags-write safety, force=True/False build
semantics, and the shared cmake-composition function the follow-up introduced.

Things nothing else in the repo checked before this phase made manifest.yaml writable from
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

import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import bootstrap  # noqa: E402

bootstrap.add_venv_site_packages(_REPO_ROOT)

from cockpit.screens.builds import _write_manifest_cmake_flags, _write_manifest_ref  # noqa: E402
from cockpit.widgets import TableAction  # noqa: E402
from provision import schema  # noqa: E402
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
    """The real manifest.yaml: header, every sibling backend, and cuda's own apt_packages
    comment must survive a cmake_flags rewrite targeting cuda. Also covers the 2026-09-18
    follow-up's `example: true` mark: a plain rewrite (clear_example=False, the default)
    must leave it in place — the mark means "still stock", and this call doesn't claim to
    know whether the new flags are still stock or not, so it must not touch the key."""
    original = (_REPO_ROOT / "manifest.yaml").read_text()
    header = original.split("llama_cpp:")[0]
    assert "example: true" in original.split("vulkan:")[0].split("cuda:")[1], (
        "fixture assumption broken: manifest.yaml's cuda recipe no longer has example: true"
    )
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "manifest.yaml"
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
    print("verify_build_pin: real manifest.yaml survives a cmake_flags rewrite, example: true intact — OK")


def verify_example_mark_cleared_on_request() -> None:
    """The same rewrite, with clear_example=True: `example: true` must be removed from
    cuda's block, and only cuda's — every sibling backend's own `example: true` (rocm,
    vulkan, sycl all ship one) must survive untouched, proving the removal is bounded to
    the target backend's block the same way the cmake_flags rewrite itself is."""
    original = (_REPO_ROOT / "manifest.yaml").read_text()
    header = original.split("llama_cpp:")[0]
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "manifest.yaml"
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
    """2026-09-18 operator QA: a backend recipe added only to manifest.yaml (not one of the
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
        "verify_build_pin: backend names derive from manifest.yaml — a manifest-only backend "
        "is accepted, an unknown one is rejected, and known_backends=None defers (doesn't "
        "fall back) — OK"
    )


def verify_cmake_flags_write_aborts_on_bad_backend() -> None:
    """No block to find -> raise, never write a null/partial result."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "manifest.yaml"
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
    now display — this locks their output for the manifest.yaml backends actually in use
    (cuda, vulkan) so a future change to the composition is a visible diff here, not silent
    drift between 'what is shown' and 'what runs' (there is only one function now; this test
    is a regression guard on its shape, not a drift detector between two copies)."""
    import yaml as _yaml

    manifest_dict = _yaml.safe_load((_REPO_ROOT / "manifest.yaml").read_text())
    host_profile = {"paths": {"state_dir": "/var/lib/llm-server", "prefix_root": "/opt/llm-server/builds"}}
    checkout_dir = build_step.checkout_dir_for(host_profile)
    assert checkout_dir == Path("/var/lib/llm-server/src/llama.cpp")

    for backend in ("cuda", "vulkan"):
        recipe = manifest_dict["backends"][backend]
        prefix = build_step.backend_prefix(host_profile, backend, "deadbeef")
        assert prefix == Path("/opt/llm-server/builds") / backend / "deadbeef"
        argv = build_step.resolve_cmake_argv(backend, recipe, checkout_dir, prefix)
        expected = [
            "cmake", "-B", str(checkout_dir / f"build-{backend}"),
            "-DCMAKE_BUILD_TYPE=Release", *recipe["cmake_flags"],
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


def main() -> None:
    verify_manifest_roundtrip()
    verify_force_flag()
    verify_cmake_flags_roundtrip_order_independent()
    verify_cmake_flags_roundtrip_real_manifest()
    verify_example_mark_cleared_on_request()
    verify_backend_names_derive_from_manifest()
    verify_cmake_flags_write_aborts_on_bad_backend()
    verify_resolve_cmake_argv_matches_build()
    verify_table_action_confirm_callable()
    print("verify_build_pin: all checks passed")


if __name__ == "__main__":
    main()
