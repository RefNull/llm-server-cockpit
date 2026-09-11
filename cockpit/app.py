"""Cockpit: a Textual TUI over the same provision.steps.* functions the CLI uses — no
parallel implementation of build/config-gen/download logic, just a view + confirm layer.

Starts in dry-run by default (real builds/downloads/systemd restarts are one keystroke away
in an interactive UI — default to the safe mode, make the operator opt in to real actions).
Screens share a single Runner instance by reference, so toggling dry-run here is
instantly visible to every tab without any extra plumbing.
"""
from __future__ import annotations

import socket
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, TabbedContent, TabPane

from provision import schema
from provision.common import Runner

from cockpit.screens.builds import BuildsScreen
from cockpit.screens.deploy import DeployScreen
from cockpit.screens.downloads import DownloadsScreen
from cockpit.screens.settings import SettingsScreen
from cockpit.widgets import SHARED_CSS

REPO_ROOT = Path(__file__).resolve().parent.parent


class CockpitApp(App):
    """Every screen widget is constructed as Screen(host_profile, manifest, models, runner,
    repo_root, app_ref) — see cockpit/screens/*.py. `models` is reloaded via reload_models()
    after any edit to models.yaml so every tab observes fresh state; screens should call
    self.cockpit_app.reload_models() (not re-implement their own loader) after a config edit.

    First run (no hosts/<hostname>.yaml yet): host_profile is None, and only the Settings tab
    is shown — it offers to create one. The other tabs all assume a real host_profile (gpus,
    paths, etc.), so rather than pushing None-handling into every screen, first-run just asks
    for a restart once the profile is created (see SettingsScreen) — a one-time step, not
    something that needs to be seamless.

    CSS = SHARED_CSS (cockpit/widgets.py): panel/button-row/status-text/error-text and the
    scroll-container convention are defined once there and cascade to every screen — see that
    file's module docstring for the convention every screen composes against.
    """

    CSS = SHARED_CSS

    TITLE = "llm-server-cockpit"
    BINDINGS = [
        ("d", "toggle_dry_run", "Toggle dry-run"),
        ("r", "refresh_all", "Refresh"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, host: str | None = None) -> None:
        super().__init__()
        self.host_name = host or socket.gethostname()
        self.repo_root = REPO_ROOT
        self.host_profile = schema.try_load_host_profile(REPO_ROOT / "hosts" / f"{self.host_name}.yaml")
        self.manifest = schema.load_manifest(REPO_ROOT / "manifest.yaml")
        if self.host_profile is not None:
            try:
                self.models = schema.load_models(REPO_ROOT / "models.yaml", self.host_profile, self.manifest)
            except schema.ValidationError:
                self.models = {"models": []}
        else:
            self.models = {"models": []}
        self.runner = Runner(dry_run=True)

    def compose(self) -> ComposeResult:
        yield Header()
        if self.host_profile is None:
            with TabbedContent(initial="settings"):
                with TabPane("First setup", id="settings"):
                    yield SettingsScreen(self.host_profile, self.manifest, self.models, self.runner, self.repo_root, self)
        else:
            with TabbedContent(initial="installs"):
                with TabPane("Installs", id="installs"):
                    yield BuildsScreen(self.host_profile, self.manifest, self.models, self.runner, self.repo_root, self)
                with TabPane("Deploy", id="deploy"):
                    yield DeployScreen(self.host_profile, self.manifest, self.models, self.runner, self.repo_root, self)
                with TabPane("Downloads", id="downloads"):
                    yield DownloadsScreen(self.host_profile, self.manifest, self.models, self.runner, self.repo_root, self)
                with TabPane("Settings", id="settings"):
                    yield SettingsScreen(self.host_profile, self.manifest, self.models, self.runner, self.repo_root, self)
        yield Footer()

    def on_mount(self) -> None:
        self._update_dry_run_subtitle()

    def _update_dry_run_subtitle(self) -> None:
        self.sub_title = "DRY RUN — no action will actually execute" if self.runner.dry_run else "LIVE — actions execute for real"

    def action_toggle_dry_run(self) -> None:
        self.runner.dry_run = not self.runner.dry_run
        self._update_dry_run_subtitle()
        self.notify(f"dry-run {'ON' if self.runner.dry_run else 'OFF'}", severity="warning" if not self.runner.dry_run else "information")

    def action_refresh_all(self) -> None:
        self.reload_models()
        for widget in self.query("BuildsScreen, DeployScreen, DownloadsScreen, SettingsScreen"):
            if hasattr(widget, "on_refresh_requested"):
                widget.on_refresh_requested()

    def reload_models(self) -> None:
        if self.host_profile is None:
            return
        try:
            self.models = schema.load_models(self.repo_root / "models.yaml", self.host_profile, self.manifest)
        except schema.ValidationError as e:
            self.notify(f"models.yaml error: {e}", severity="error")


def main() -> None:
    CockpitApp().run()


if __name__ == "__main__":
    main()
