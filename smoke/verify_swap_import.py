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


def _load_results() -> tuple[dict, dict, list[dict]]:
    manifest = schema.load_manifest(_REPO_ROOT / "manifest.example.yaml")
    host_profile = schema.load_host_profile(_REPO_ROOT / "hosts" / "example.yaml", manifest)
    text = _FIXTURE.read_text(encoding="utf-8")
    results = swap.parse_config_for_import(text, host_profile)
    return host_profile, manifest, results


def check_classification_and_status() -> list[dict]:
    host_profile, manifest, results = _load_results()
    by_id = {r["model"]["id"]: r for r in results}

    expected_llama_cpp = {
        "chat-main": ("chat-main-Q4_K_M.gguf", "vulkan"),
        "chat-vision": ("chat-vision-Q4_K_M.gguf", "cuda"),
        "embed-bge": ("bge-m3-Q8_0.gguf", "cuda"),
        "rerank-qwen": ("qwen3-reranker-4b-Q4_K_M.gguf", "cuda"),
    }
    expected_python = {"asr-python", "tts-python"}

    assert set(by_id) == set(expected_llama_cpp) | expected_python, (
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

    print(f"  {len(by_id)} entries classified correctly (4 llama-cpp, 2 python), notes present for relocated paths and dropped healthCheckTimeout")
    return results


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


def main() -> None:
    results = check_classification_and_status()
    check_validate_and_regenerate(results)
    print("Swap import verification PASSED.")


if __name__ == "__main__":
    main()
