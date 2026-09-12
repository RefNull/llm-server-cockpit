"""Cockpit "Dashboard" tab: read-only status board over the rest of the app — no new
provisioning logic, only provision.steps.* read paths already used by the other screens
(list_builds, model_status, swap.status, wol.status, drivers.read_lockfile_status). Landing tab;
every mutating/checking action stays on its own tab, this only answers "what's the state of my
stack right now".
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Static

from provision.common import Runner
from provision.steps import build as build_step
from provision.steps import drivers, hf, swap, wol


class DashboardScreen(Widget):
    """Mounted as the app's default tab by cockpit/app.py — not a Textual Screen."""

    # .panel / .panel-title / .subtitle come from cockpit/widgets.py's SHARED_CSS.
    DEFAULT_CSS = """
    DashboardScreen {
        height: 1fr;
    }
    DashboardScreen #dashboard-columns {
        height: auto;
    }
    DashboardScreen #dashboard-left, DashboardScreen #dashboard-right {
        width: 1fr;
    }
    DashboardScreen #dashboard-left {
        margin-right: 1;
    }
    DashboardScreen .panel Static {
        margin-top: 1;
    }
    """

    def __init__(
        self,
        host_profile: dict,
        manifest: dict,
        models: dict,
        runner: Runner,
        repo_root: Path,
        app_ref,
    ) -> None:
        super().__init__()
        self.host_profile = host_profile
        self.manifest = manifest
        self.models = models
        self.runner = runner
        self.repo_root = repo_root
        self.cockpit_app = app_ref
        self.backends = self._compute_backends()

    def _compute_backends(self) -> list[str]:
        seen: list[str] = []
        for gpu in self.host_profile.get("gpus", []):
            for backend in gpu.get("backends", []):
                if backend not in seen:
                    seen.append(backend)
        return seen

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("At-a-glance status across the LLM stack.", classes="subtitle")
            with Horizontal(id="dashboard-columns"):
                with Vertical(id="dashboard-left"):
                    with Vertical(classes="panel"):
                        yield Static("LLM Backends", classes="panel-title")
                        yield Static("", id="db-backends")
                    with Vertical(classes="panel"):
                        yield Static("Models & llama-swap", classes="panel-title")
                        yield Static("", id="db-models")
                with Vertical(id="dashboard-right"):
                    with Vertical(classes="panel"):
                        yield Static("HF Downloads", classes="panel-title")
                        yield Static("", id="db-downloads")
                    with Vertical(classes="panel"):
                        yield Static("Hardware & Networking", classes="panel-title")
                        yield Static("", id="db-hardware")

    def on_mount(self) -> None:
        self._refresh_all()

    def on_refresh_requested(self) -> None:
        """Called by CockpitApp.action_refresh_all — same pattern every other screen follows."""
        self.models = getattr(self.cockpit_app, "models", self.models)
        self._refresh_all()

    def _refresh_all(self) -> None:
        self._refresh_backends()
        self._refresh_models()
        self._refresh_downloads()
        self._refresh_hardware()

    # ------------------------------------------------------------------ panels

    def _refresh_backends(self) -> None:
        """Current installed state per backend — deliberately not an upstream-version check
        (that stays a manual action on the Backends tab); this answers "what's running now"."""
        widget = self.query_one("#db-backends", Static)
        if not self.backends:
            widget.update("no GPU backends declared in host profile")
            return
        lines: list[str] = []
        for backend in self.backends:
            builds = build_step.list_builds(self.host_profile, backend)
            current = next((b for b in builds if b.get("current")), None)
            if current is None:
                lines.append(f"{backend}: not built yet")
            else:
                ref = (current.get("ref") or "")[:10]
                sane = "ok" if current.get("sane") else "NOT SANE"
                lines.append(f"{backend}: {ref} ({sane})")
        widget.update("\n".join(lines))

    def _refresh_models(self) -> None:
        widget = self.query_one("#db-models", Static)
        counts: dict[str, int] = {}
        for model in self.models.get("models", []):
            engine = model.get("engine", "unknown")
            counts[engine] = counts.get(engine, 0) + 1
        count_line = ", ".join(f"{n} {engine}" for engine, n in sorted(counts.items())) or "no models configured"
        try:
            swap_status = swap.status(self.host_profile)
        except Exception as e:
            widget.update(f"{count_line}\nllama-swap: error checking status ({e})")
            return
        swap_line = (
            f"llama-swap: {'running' if swap_status['unit_active'] else 'stopped'} "
            f"({'enabled' if swap_status['unit_enabled'] else 'disabled'} on boot)"
        )
        if swap_status["installed_version"]:
            swap_line += f" — {swap_status['installed_version']}"
        widget.update(f"{count_line}\n{swap_line}")

    def _refresh_downloads(self) -> None:
        widget = self.query_one("#db-downloads", Static)
        downloaded = missing = placeholder = 0
        for model in self.models.get("models", []):
            if model.get("engine") != "llama-cpp":
                continue
            status = hf.model_status(model, self.host_profile)
            if status == "downloaded":
                downloaded += 1
            elif status == "missing":
                missing += 1
            elif status == "placeholder":
                placeholder += 1
        total = downloaded + missing + placeholder
        if total == 0:
            widget.update("no llama-cpp models configured")
            return
        widget.update(f"{downloaded}/{total} downloaded, {missing} missing, {placeholder} placeholder")

    def _refresh_hardware(self) -> None:
        widget = self.query_one("#db-hardware", Static)
        gpu_count = len(self.host_profile.get("gpus", []))
        lines = [f"{gpu_count} GPU(s) configured"]

        try:
            lock_data = drivers.read_lockfile_status(self.host_profile, self.repo_root)
        except Exception as e:
            lines.append(f"driver lockfile: error reading ({e})")
        else:
            lines.append(
                "driver lockfile: not checked yet" if lock_data is None
                else f"driver lockfile: last checked {lock_data.get('generated_at', 'unknown')}"
            )

        try:
            wol_status = wol.status(self.host_profile)
        except Exception as e:
            lines.append(f"Wake-on-LAN: error checking ({e})")
        else:
            lines.append(
                f"Wake-on-LAN: {wol_status['wake_flags'] or 'unknown'} flags, "
                f"persistence {'enabled' if wol_status['unit_enabled'] else 'disabled'}"
            )
        widget.update("\n".join(lines))
