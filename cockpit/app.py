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
from textual.containers import Vertical
from textual.widgets import Footer, Header, Static, TabbedContent, TabPane

from provision import schema
from provision.common import Runner

from cockpit import __version__
from cockpit.screens.builds import BuildsScreen
from cockpit.screens.deploy import DeployScreen
from cockpit.screens.downloads import DownloadsScreen
from cockpit.screens.settings import SettingsScreen
from cockpit.widgets import AMBER_THEME, SHARED_CSS

REPO_ROOT = Path(__file__).resolve().parent.parent

ASCII_BANNER = (
    r" _      _     ___  ___       _____ ___________ _   _ ___________       _____ _____ _____  _   ________ _____ _____ " "\n"
    r"| |    | |    |  \/  |      /  ___|  ___| ___ \ | | |  ___| ___ \     /  __ \  _  /  __ \| | / /| ___ \_   _|_   _|" "\n"
    r"| |    | |    | .  . |______\ `--.| |__ | |_/ / | | | |__ | |_/ /_____| /  \/ | | | /  \/| |/ / | |_/ / | |   | |  " "\n"
    r"| |    | |    | |\/| |______|`--. \  __||    /| | | |  __||    /______| |   | | | | |    |    \ |  __/  | |   | |  " "\n"
    r"| |____| |____| |  | |      /\__/ / |___| |\ \\ \_/ / |___| |\ \      | \__/\ \_/ / \__/\| |\  \| |    _| |_  | |  " "\n"
    r"\_____/\_____/\_|  |_/      \____/\____/\_| \_|\___/\____/\_| \_|      \____/\___/ \____/\_| \_/\_|    \___/  \_/  "
)


class CockpitHeader(Vertical):
    """Top header widget displaying ASCII art banner, version, and host info."""

    DEFAULT_CSS = """
    CockpitHeader {
        height: auto;
        align: center middle;
        margin: 0 2 1 2;
    }
    CockpitHeader #banner-art {
        color: #f5a623;
        content-align: center middle;
        text-align: center;
        width: 100%;
        overflow-x: hidden;
    }
    CockpitHeader #banner-meta {
        content-align: center middle;
        text-align: center;
        width: 100%;
        color: $text-muted;
    }
    """

    def __init__(self, host_name: str) -> None:
        super().__init__(id="cockpit-header")
        self.host_name = host_name

    def compose(self) -> ComposeResult:
        yield Static(ASCII_BANNER, id="banner-art")
        yield Static(
            f"[bold #f5a623]Local LLM server cockpit · v{__version__}[/]  [dim]·[/]  [dim]Host:[/] [bold #e5a93c]{self.host_name}[/]",
            id="banner-meta",
        )


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

    CSS = (
        SHARED_CSS
        + """
    TabbedContent {
        margin: 0 2;
    }
    """
    )

    TITLE = "llm-server-cockpit"
    BINDINGS = [
        ("r", "refresh_all", "Refresh"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, host: str | None = None) -> None:
        super().__init__()
        self.register_theme(AMBER_THEME)
        self.theme = "cockpit-amber"
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
        yield CockpitHeader(self.host_name)
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
