#!/usr/bin/env python3
"""Verify `provision.steps.swap.parse_config_for_import` against a genericized llama-swap
config.yaml fixture that reproduces the operator's real entry shapes (4 llama-server, 2 Python
venv scripts, one commented-out block, an always-on group) — see
`plans/09-swap-import-and-python-engine.md` Phase 1.

This is a parser/generator round-trip check, not a UI check: parse the fixture, assert the
per-entry classification and notes, validate the "ok" entries against the schema, then run them
back through `_generate_config` and re-parse the result to confirm every llama-cpp cmd actually
lands under the *importing host's* prefix_root/models_dir rather than carrying over the
fixture's own (deliberately different) paths.

Run: .venv/bin/python smoke/verify_swap_import.py
"""
from __future__ import annotations

import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import bootstrap  # noqa: E402

bootstrap.add_venv_site_packages(_REPO_ROOT)

import yaml  # noqa: E402

from provision import schema  # noqa: E402
from provision.steps import swap  # noqa: E402

_FIXTURE = _REPO_ROOT / "smoke" / "fixtures" / "llama-swap-import.yaml"


def _load_results() -> tuple[dict, dict, list[dict], int | None]:
    manifest = schema.load_manifest(_REPO_ROOT / "manifest.example.yaml")
    host_profile = schema.load_host_profile(_REPO_ROOT / "hosts" / "example.yaml", manifest)
    text = _FIXTURE.read_text(encoding="utf-8")
    results, health_check_timeout = swap.parse_config_for_import(text, host_profile)
    return host_profile, manifest, results, health_check_timeout


def check_classification_and_status() -> list[dict]:
    host_profile, manifest, results, health_check_timeout = _load_results()
    by_id = {r["model"]["id"]: r for r in results}

    expected_llama_cpp = {
        "chat-main": ("chat-main-Q4_K_M.gguf", "vulkan"),
        "chat-vision": ("chat-vision-Q4_K_M.gguf", "cuda"),
        "embed-bge": ("bge-m3-Q8_0.gguf", "cuda"),
        "rerank-qwen": ("qwen3-reranker-4b-Q4_K_M.gguf", "cuda"),
    }
    expected_python = {"asr-python", "tts-python"}
    expected_unmanaged = {"tts-docker"}

    assert set(by_id) == set(expected_llama_cpp) | expected_python | expected_unmanaged, (
        f"unexpected set of imported ids: {sorted(by_id)}"
    )

    for model_id, (quant_file, backend) in expected_llama_cpp.items():
        r = by_id[model_id]
        model = r["model"]
        assert model["engine"] == "llama-cpp", f"{model_id}: expected engine llama-cpp, got {model['engine']!r}"
        assert model.get("quant_file") == quant_file, f"{model_id}: quant_file {model.get('quant_file')!r} != {quant_file!r}"
        assert model.get("bind", {}).get("backend") == backend, (
            f"{model_id}: backend {model.get('bind', {}).get('backend')!r} != {backend!r}"
        )
        assert r["status"] == "ok", f"{model_id}: expected status ok, got {r['status']!r} (notes={r['notes']})"
        assert any("relocated from" in n for n in r["notes"]), f"{model_id}: missing 'relocated from' note: {r['notes']}"
        assert any("binary path rewritten to prefix_root" in n for n in r["notes"]), (
            f"{model_id}: missing 'binary path rewritten to prefix_root' note: {r['notes']}"
        )

    assert by_id["chat-vision"]["model"].get("mmproj_file") is None, (
        "chat-vision: --mmproj was commented out in the fixture and must not survive import"
    )
    assert any("--mmproj" in n for n in by_id["chat-vision"]["notes"]), (
        f"chat-vision: comment-drop note must name the --mmproj line it dropped: {by_id['chat-vision']['notes']}"
    )

    for model_id in expected_python:
        r = by_id[model_id]
        model = r["model"]
        assert model["engine"] == "python", f"{model_id}: expected engine python, got {model['engine']!r}"
        assert model.get("check_endpoint") == "/health", f"{model_id}: check_endpoint {model.get('check_endpoint')!r} != '/health'"
        assert r["status"] == "ok", f"{model_id}: expected status ok, got {r['status']!r} (notes={r['notes']})"
        assert any("healthCheckTimeout" in n and "dropped" in n for n in r["notes"]), (
            f"{model_id}: missing dropped-healthCheckTimeout note: {r['notes']}"
        )

    assert by_id["asr-python"]["model"]["python"] == "/opt/services/asr/.venv/bin/python"
    assert by_id["asr-python"]["model"]["script"] == "/opt/services/asr/asr_server.py"
    # Python args are argv[2:] verbatim (plan Phase 1 item 3) — unlike llama-cpp, `--port
    # ${PORT}` is NOT stripped here: not every script takes --port, so the parser cannot assume
    # its presence/position the way it can for llama-server's own flag.
    assert by_id["asr-python"]["model"]["args"] == ["--port", "${PORT}", "--model", "Qwen3-ASR-0.6B", "--device", "cuda"], (
        f"asr-python: args {by_id['asr-python']['model']['args']!r} — expected argv[2:] verbatim"
    )
    assert by_id["tts-python"]["model"]["args"] == ["--port", "${PORT}"], (
        f"tts-python: args {by_id['tts-python']['model'].get('args')!r} — expected ['--port', '${{PORT}}']"
    )

    assert by_id["embed-bge"]["model"].get("group") == "always-on", "embed-bge: top-level groups.always-on.members not applied"
    assert by_id["rerank-qwen"]["model"].get("group") == "always-on", "rerank-qwen: top-level groups.always-on.members not applied"

    # tts-docker: argv[0] is `PORT=${PORT}`, not llama-server or python -> unmanaged fallback.
    # Regression coverage for a real bug (caught against the operator's own config): the
    # fallback used to shlex.split+shlex.join the command, which quoted `PORT=${PORT}` into
    # `'PORT=${PORT}'` — literal quote characters that were never in the source, and that
    # llama-swap's own shlex-based exec (no shell; process_command.go:505 execs argv[0]
    # directly) would treat as PART of the token, not stripped protection.
    docker_entry = by_id["tts-docker"]
    assert docker_entry["model"]["engine"] == "unmanaged", (
        f"tts-docker: expected engine unmanaged, got {docker_entry['model']['engine']!r}"
    )
    expected_cmd = "PORT=${PORT} docker compose -f /srv/services/tts-docker/docker-compose.yml up --build"
    assert docker_entry["model"]["cmd"] == expected_cmd, (
        f"tts-docker: cmd was re-quoted on import: {docker_entry['model']['cmd']!r} != {expected_cmd!r}"
    )
    assert "'" not in docker_entry["model"]["cmd"], (
        f"tts-docker: cmd carries a quote character not present in the source: {docker_entry['model']['cmd']!r}"
    )

    assert health_check_timeout == 120, (
        f"expected health_check_timeout 120 from fixture, got {health_check_timeout}"
    )

    print(f"  {len(by_id)} entries classified correctly (4 llama-cpp, 2 python, 1 unmanaged), notes present for relocated paths and dropped healthCheckTimeout, top-level healthCheckTimeout preserved, unmanaged cmd not re-quoted")
    return results


def check_args_roundtrip(results: list[dict]) -> None:
    """plans/09-swap-import-and-python-engine.md Phase 2 item 6 (Decision 4): the form's
    tokens<->lines helpers must round-trip losslessly. Uses chat-main's llama_server_args,
    which carries the fixture's quoted --chat-template-kwargs JSON blob (braces, colons,
    embedded double quotes) — the token most likely to break a naive per-line join/split.
    """
    from cockpit.screens.deploy import _args_lines_to_tokens, _args_tokens_to_lines

    by_id = {r["model"]["id"]: r for r in results}
    tokens = by_id["chat-main"]["model"]["llama_server_args"]
    assert any("chat-template-kwargs" in t for t in tokens), (
        f"chat-main: fixture no longer carries a chat-template-kwargs token to round-trip: {tokens!r}"
    )
    lines = _args_tokens_to_lines(tokens)
    round_tripped = _args_lines_to_tokens(lines)
    assert round_tripped == tokens, (
        f"args round-trip lossy over {len(tokens)} tokens:\n  in:  {tokens!r}\n  lines: {lines!r}\n  out: {round_tripped!r}"
    )
    print(f"  args round-trip lossless over {len(tokens)} tokens, including the quoted chat-template-kwargs JSON")


def check_validate_and_regenerate(results: list[dict]) -> None:
    manifest = schema.load_manifest(_REPO_ROOT / "manifest.example.yaml")
    host_profile = schema.load_host_profile(_REPO_ROOT / "hosts" / "example.yaml", manifest)

    ok_models = [r["model"] for r in results if r["status"] == "ok"]
    assert ok_models, "no 'ok' entries — nothing below checked anything"
    candidate = {"models": ok_models}
    validated = schema.validate_models_dict(candidate, host_profile, manifest, source="smoke/verify_swap_import.py")

    text = swap._generate_config(host_profile, validated)
    parsed = yaml.safe_load(text)
    assert isinstance(parsed, dict) and isinstance(parsed.get("models"), dict), "_generate_config output does not parse as a mapping with a models key"

    prefix_root = host_profile["paths"]["prefix_root"]
    models_dir = host_profile["paths"]["models_dir"]
    llama_cpp_ids = [m["id"] for m in ok_models if m["engine"] == "llama-cpp"]
    assert llama_cpp_ids, "no llama-cpp entries survived validation — the assertions below are vacuous"
    for model_id in llama_cpp_ids:
        cmd = parsed["models"][model_id]["cmd"]
        assert cmd.startswith(prefix_root), f"{model_id}: regenerated cmd does not start with prefix_root {prefix_root!r}: {cmd!r}"
        assert models_dir in cmd, f"{model_id}: regenerated cmd does not contain models_dir {models_dir!r}: {cmd!r}"
        assert "${PORT}" in cmd, f"{model_id}: regenerated cmd lost ${{PORT}}: {cmd!r}"

    print(f"  {len(ok_models)} 'ok' entries validate; regenerated config rebinds every llama-cpp cmd under this host's prefix_root/models_dir and keeps ${{PORT}}")


def check_self_reimport(results: list[dict]) -> None:
    """plans/09-swap-import-and-python-engine.md Phase 4 item 5: 'Our own generated config
    re-imports as llama-cpp/python — confirm.' Regenerates config.yaml from the fixture's own
    'ok' entries, then re-runs parse_config_for_import on THAT output (not just inspecting its
    text, the way check_validate_and_regenerate does) and asserts every entry keeps its engine.
    This is the loop ImportModelsModal.on_mount's own discovery would go through against a
    real state_dir/llama-swap/config.yaml this cockpit generated.
    """
    manifest = schema.load_manifest(_REPO_ROOT / "manifest.example.yaml")
    host_profile = schema.load_host_profile(_REPO_ROOT / "hosts" / "example.yaml", manifest)

    ok_models = [r["model"] for r in results if r["status"] == "ok"]
    validated = schema.validate_models_dict({"models": ok_models}, host_profile, manifest, source="smoke/verify_swap_import.py")
    text = swap._generate_config(host_profile, validated)

    reimported, reimported_timeout = swap.parse_config_for_import(text, host_profile)
    reimported_by_id = {r["model"]["id"]: r for r in reimported}
    original_engine = {m["id"]: m["engine"] for m in ok_models}

    assert set(reimported_by_id) == set(original_engine), (
        f"self-reimport lost or gained ids: had {sorted(original_engine)}, got {sorted(reimported_by_id)}"
    )
    for model_id, engine in original_engine.items():
        got = reimported_by_id[model_id]["model"]["engine"]
        assert got == engine, f"{model_id}: self-reimport changed engine {engine!r} -> {got!r}"

    assert reimported_timeout == 120, f"expected reimported health_check_timeout 120, got {reimported_timeout}"

    print(f"  {len(original_engine)} 'ok' entries round-trip through _generate_config -> parse_config_for_import with the same engine and healthCheckTimeout")


def check_residency_and_ttl_behavior() -> None:
    # plans/11 removed DeployScreen._is_pinned along with the Pinned/Swappable table split —
    # the Deploy tab's one table now just prints ttl literally (0 or a number), no predicate
    # to test here any more. What's left: the import-time advisory note below, which is
    # independent of the screen and still real (llama-swap idle-unloads a grouped model with
    # a positive ttl regardless of what any UI calls it).

    # Check that grouped model with ttl > 0 gets an informative note on import
    manifest = schema.load_manifest(_REPO_ROOT / "manifest.example.yaml")
    host_profile = schema.load_host_profile(_REPO_ROOT / "hosts" / "example.yaml", manifest)
    grouped_ttl_fixture = """
healthCheckTimeout: 90
models:
  bge-grouped:
    cmd: /srv/llm/builds/cuda/current/bin/llama-server --model /srv/llm/models/gguf/bge-m3-Q8_0.gguf --port ${PORT}
    ttl: 600
groups:
  always-on:
    members:
      - bge-grouped
"""
    parsed, parsed_timeout = swap.parse_config_for_import(grouped_ttl_fixture, host_profile)
    assert parsed_timeout == 90
    m = parsed[0]
    assert m["model"]["group"] == "always-on"
    assert m["model"]["ttl"] == 600
    assert any("idle-unloads after timeout" in n for n in m["notes"]), (
        f"expected idle-unload note for grouped model with ttl 600, got notes: {m['notes']}"
    )

    print("  import note warns when a grouped model's positive ttl still means it idle-unloads")


def check_malformed_entry_does_not_abort_import() -> None:
    """A single malformed entry (here, `cmd:` holding a non-string) must not crash the whole
    parse — the rest of the config still comes back, and the bad one lands as `review` with a
    note, instead of an uncaught AttributeError from `_sanitize_cmd`'s `raw_cmd.splitlines()`.

    Regression: caught live against a pasted config where a stray YAML value (`cmd: 123`)
    took the whole "Import from llama-swap" dialog down with the app.
    """
    manifest = schema.load_manifest(_REPO_ROOT / "manifest.example.yaml")
    host_profile = schema.load_host_profile(_REPO_ROOT / "hosts" / "example.yaml", manifest)
    text = """
models:
  bad:
    cmd: 123
  good:
    cmd: |
      /srv/llm/builds/cuda/current/bin/llama-server
      --model /srv/models/gguf/x.gguf
      --port ${PORT}
"""
    results, _ = swap.parse_config_for_import(text, host_profile)
    by_id = {r["model"]["id"]: r for r in results}
    assert set(by_id) == {"bad", "good"}, f"malformed entry took a sibling entry down with it: {sorted(by_id)}"
    assert by_id["bad"]["status"] == "review", f"malformed entry should be 'review', got {by_id['bad']['status']!r}"
    assert by_id["good"]["status"] == "ok", f"sibling entry should still parse fine, got {by_id['good']['status']!r}"
    print("  a malformed entry (non-string cmd) degrades to 'review' instead of crashing the whole import")


def main() -> None:
    results = check_classification_and_status()
    check_args_roundtrip(results)
    check_validate_and_regenerate(results)
    check_self_reimport(results)
    check_residency_and_ttl_behavior()
    check_malformed_entry_does_not_abort_import()
    print("Swap import verification PASSED.")


if __name__ == "__main__":
    main()
