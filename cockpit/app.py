"""Cockpit: a Textual TUI over the same provision.steps.* functions the CLI uses — no
parallel implementation of build/config-gen/download logic, just a view + confirm layer.

Interactive actions require operator confirmation via modals, and YAML configuration
previews are available before deployment.
"""
from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Footer, Header, Static, TabbedContent, TabPane

from provision import schema
from provision.common import Runner

from cockpit import __version__
from cockpit.screens.builds import BuildsScreen
from cockpit.screens.containers import ContainersScreen
from cockpit.screens.dashboard import DashboardScreen
from cockpit.screens.deploy import DeployScreen
from cockpit.screens.downloads import DownloadsScreen
from cockpit.screens.scripts import ScriptsScreen
from cockpit.screens.settings import SettingsScreen
from cockpit.screens.systemd import SystemdScreen
from cockpit.widgets import AMBER_THEME, SHARED_CSS, CockpitScreenBase

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
        /* Side margin is the app's single side inset (DESIGN.md §2) — the header is not
           inside #main-tabs, so it has to restate the value rather than inherit it. It used
           to be a bare 2, which left the banner and the version/host line one cell left of
           every screen's content once #main-tabs moved to 3. */
        margin: 0 $space-edge $space-normal $space-edge;
    }
    """

    def __init__(self, host_name: str) -> None:
        super().__init__(id="cockpit-header")
        self.host_name = host_name

    def compose(self) -> ComposeResult:
        yield Static(ASCII_BANNER, id="banner-art")
        yield Static("[bold #f5a623]LLM-SERVER-COCKPIT[/]", id="banner-compact")
        # The badge reflects the process's ACTUAL euid, not a flag that was asked for: with
        # --sudo the re-exec has already happened by the time this renders, so a badge driven
        # by the flag would claim rights the process might not have if sudo failed.
        sudo_badge = " [dim]·[/] [bold #000000 on #e5a93c] SUDO MODE [/]" if os.geteuid() == 0 else ""
        yield Static(
            f"[bold #f5a623]v{__version__}[/] [dim]·[/] [dim]Host:[/] [bold #e5a93c]{self.host_name}[/]{sudo_badge}",
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

    Normal mode's tabs are grouped two levels deep via nested TabbedContent — Dashboard is the
    default/landing tab, "LLM" groups Backends/Models/HF Downloads (BuildsScreen/DeployScreen/
    DownloadsScreen — renamed labels only, same screen classes), "System" groups
    Containers/Scripts/Systemd, Settings groups its own sub-tabs internally (see
    SettingsScreen._compose_normal). Nesting doesn't change how
    action_refresh_all's DOM query below finds screens: Textual mounts every TabPane's content
    up front (no lazy-mount), so a query for a screen class matches regardless of nesting depth.

    CSS = SHARED_CSS (cockpit/widgets.py): panel/action-row/status-text/error-text, the three
    button archetypes and the scroll-container convention are defined once there and cascade
    to every screen — see that file's module docstring for the convention every screen
    composes against. Spacing tokens are SPACE_TOKENS below, not SHARED_CSS (see the comment
    there for why that distinction is load-bearing).
    """

    CSS = (
        SHARED_CSS
        + """
    Screen.-narrow #banner-art {
        display: none;
    }
    Screen.-narrow #banner-compact {
        display: block;
        text-align: center;
        margin-bottom: 0;
    }
    Screen.-wide #banner-art {
        display: block;
        content-align: center middle;
        text-align: center;
        width: 100%;
        color: #f5a623;
    }
    Screen.-wide #banner-compact {
        display: none;
    }
    #banner-meta {
        content-align: center middle;
        text-align: center;
        width: 100%;
        color: $text-muted;
    }
    Screen {
        overflow: hidden;
    }
    /* Type selector on purpose: both the outer tab bar and the nested ones (LLM, Settings)
       must fill the viewport vertically. The side inset deliberately is NOT here — see
       #main-tabs below. */
    TabbedContent {
        height: 1fr;
    }
    /* The single side inset for the whole app (DESIGN.md §2). This used to be
       `TabbedContent { margin: 0 2 }`, a type selector, so it applied again to the nested
       TabbedContents inside the LLM and Settings tabs and pushed those screens 2 cells
       further in than the flat ones. Scoped to the outer container, every screen sits at 3. */
    #main-tabs {
        margin: 0 $space-edge;
    }
    /* Textual's Tabs is `height: 2` docked top with no bottom margin, so content started on
       the row immediately under the tab labels. Matches both levels: the selector is a child
       combinator on the type, and TabbedContent.compose yields exactly one ContentSwitcher. */
    TabbedContent > ContentSwitcher {
        margin-top: $space-normal;
    }
    """
    )

    # DESIGN.md §1 spacing scale, in cells. These live here rather than as `$name: value`
    # lines in SHARED_CSS because Textual parses each CSS source (App.CSS and every widget's
    # DEFAULT_CSS) against get_css_variables() alone — a variable declared inside App.CSS is
    # invisible to a screen's DEFAULT_CSS, and referencing it there raises
    # UnresolvedVariableError at mount. Routed through get_css_variables below, they resolve
    # in every source. Values must stay distinct: $space-tight used to be a second name for 1.
    SPACE_TOKENS: ClassVar[dict[str, str]] = {
        "space-normal": "1",   # between consecutive rows/controls; the default gap
        "space-section": "2",  # between distinct regions of a screen
        "space-edge": "3",     # the app's side inset (#main-tabs and CockpitHeader)
    }

    TITLE = "llm-server-cockpit"
    BINDINGS = [
        ("r", "refresh_all", "Refresh"),
        ("q", "quit", "Quit"),
    ]

    # DESIGN.md §2. Textual stamps exactly one of these classes onto the active Screen on every
    # resize (width < 121 -> Screen.-narrow, >= 121 -> Screen.-wide); the reflow itself lives in
    # SHARED_CSS / screen DEFAULT_CSS, so no screen implements on_resize geometry by hand.
    # 121 = ASCII_BANNER's 115 columns + the $space-edge inset on both sides (3 + 3). It was
    # 120 while CockpitHeader's side margin was 2; moving that margin to $space-edge moved this.
    HORIZONTAL_BREAKPOINTS: ClassVar[list[tuple[int, str]]] = [(0, "-narrow"), (121, "-wide")]

    def __init__(self, host: str | None = None, manifest_path: Path | None = None) -> None:
        super().__init__()
        self.register_theme(AMBER_THEME)
        self.theme = "cockpit-amber"
        self.host_name = host or socket.gethostname()
        self.repo_root = REPO_ROOT
        if manifest_path is not None:
            self.manifest_path = Path(manifest_path)
        elif self.host_name == "example":
            self.manifest_path = REPO_ROOT / "manifest.example.yaml"
        else:
            self.manifest_path = REPO_ROOT / "manifest.yaml"
        # Manifest first: try_load_host_profile validates gpus[].backends against
        # manifest["backends"] (provision/schema.py), so it needs the manifest already loaded.
        # This does not change the first-run case — try_load_host_profile returns None before
        # ever consulting the manifest when hosts/<host>.yaml does not exist yet.
        self.manifest = schema.load_manifest(self.manifest_path)
        self.host_profile = schema.try_load_host_profile(REPO_ROOT / "hosts" / f"{self.host_name}.yaml", self.manifest)
        if self.host_profile is not None:
            try:
                self.models = schema.load_models(REPO_ROOT / "models.yaml", self.host_profile, self.manifest)
            except schema.ValidationError:
                self.models = {"models": []}
            try:
                self.scripts = schema.load_scripts(REPO_ROOT / "scripts.yaml")
            except schema.ValidationError:
                self.scripts = {"scripts": []}
        else:
            self.models = {"models": []}
            self.scripts = {"scripts": []}
        self.runner = Runner(dry_run=False)

    def get_css_variables(self) -> dict[str, str]:
        return {**super().get_css_variables(), **self.SPACE_TOKENS}

    def compose(self) -> ComposeResult:
        yield Header()
        yield CockpitHeader(self.host_name)
        if self.host_profile is None:
            with TabbedContent(initial="settings", id="main-tabs"):
                with TabPane("First setup", id="settings"):
                    yield SettingsScreen(self.host_profile, self.manifest, self.models, self.runner, self.repo_root, self)
        else:
            with TabbedContent(initial="dashboard", id="main-tabs"):
                with TabPane("Dashboard", id="dashboard"):
                    yield DashboardScreen(self.host_profile, self.manifest, self.models, self.runner, self.repo_root, self)
                with TabPane("LLM", id="llm"):
                    # Deployments first: it is the tab an operator opens to see what is served.
                    with TabbedContent(initial="models", id="llm-tabs"):
                        with TabPane("Deployments", id="models"):
                            yield DeployScreen(self.host_profile, self.manifest, self.models, self.runner, self.repo_root, self)
                        with TabPane("Backends", id="backends"):
                            yield BuildsScreen(self.host_profile, self.manifest, self.models, self.runner, self.repo_root, self)
                        with TabPane("Model Files", id="hf-downloads"):
                            yield DownloadsScreen(self.host_profile, self.manifest, self.models, self.runner, self.repo_root, self)
                with TabPane("System", id="system"):
                    # Host-level surfaces: containers, the toolkit's own script units, and
                    # every systemd unit. Named "System" because "Deployments" now belongs to
                    # the LLM sub-tab that lists what llama-swap serves — one name, one place.
                    with TabbedContent(initial="containers", id="system-tabs"):
                        with TabPane("Containers", id="containers"):
                            yield ContainersScreen(self.host_profile, self.manifest, self.models, self.runner, self.repo_root, self)
                        with TabPane("Scripts", id="scripts"):
                            yield ScriptsScreen(self.host_profile, self.manifest, self.models, self.runner, self.repo_root, self)
                        with TabPane("Systemd", id="systemd"):
                            yield SystemdScreen(self.host_profile, self.manifest, self.models, self.runner, self.repo_root, self)
                with TabPane("Settings", id="settings"):
                    yield SettingsScreen(self.host_profile, self.manifest, self.models, self.runner, self.repo_root, self)
        yield Footer()

    def on_mount(self) -> None:
        # After the first layout pass, so is_on_screen is meaningful.
        self.call_after_refresh(self._load_visible_screens)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Bubbles up from the outer tab bar and from every nested one, which is what makes a
        two-level tab (LLM > Backends) load when both levels finally point at it."""
        self.call_after_refresh(self._load_visible_screens)

    def _load_visible_screens(self) -> None:
        """Give every screen the operator can currently see its one-time initial load.

        `is_on_screen` is false for anything in an inactive TabPane, so this is what defers the
        other tabs' work instead of doing all seven at launch (see
        CockpitScreenBase.ensure_first_view).
        """
        for screen in self.query(CockpitScreenBase):
            if screen.is_on_screen:
                screen.ensure_first_view()
                # Distinct from ensure_first_view above: on_tab_shown is not latched and fires
                # on every activation, including this same first one (DESIGN.md §3.0 /
                # cockpit/widgets.py CockpitScreenBase.on_tab_shown).
                screen.on_tab_shown()

    def action_refresh_all(self) -> None:
        self.reload_manifest()
        self.reload_models()
        self.reload_scripts()
        # Every tab subclasses CockpitScreenBase, which makes on_refresh_requested() abstract —
        # so this is a type query, not a hand-maintained list of screen names plus a hasattr
        # guard that silently skipped any tab that forgot to implement it.
        for screen in self.query(CockpitScreenBase):
            screen.on_refresh_requested()

    def reload_manifest(self) -> None:
        """Re-read manifest.yaml — the third sibling to reload_models()/reload_scripts().

        A screen that writes the llama_cpp pin (BuildsScreen's "Update to latest"/"Change
        version…") calls this instead of loading its own copy: self.manifest here is the one
        object every screen's constructor was handed (cockpit/app.py compose()), so rebinding
        it only on the writing screen would leave every other tab holding the pre-write dict
        until the app restarts.
        """
        try:
            self.manifest = schema.load_manifest(self.manifest_path)
        except schema.ValidationError as e:
            self.notify(f"manifest.yaml error: {e}", severity="error")

    def reload_host_profile(self) -> None:
        """Re-read hosts/<hostname>.yaml — the fourth sibling to reload_manifest()/
        reload_models()/reload_scripts(). BuildsScreen's "Add deployment" (plans/06 Phase 4) is
        the first screen-initiated host-profile write outside Settings, and this keeps
        self.host_profile here the one object every screen's constructor was handed, the same
        reasoning reload_manifest already gives for self.manifest.

        Keyed on self.host_name — the file __init__ actually loaded (--host, or the machine's
        own hostname) — never on host_profile["hostname"], which is the field inside that file
        and can name a different host entirely (settings.py._host_profile_path() does that; see
        its own caller for why this phase does not follow it).

        This does not, by itself, make an already-mounted screen redraw with the new value:
        only BuildsScreen calls it today and then re-reads self.host_profile from here itself.
        No other screen's on_refresh_requested rereads self.app_ref.host_profile (only manifest
        gets that treatment, via dashboard.py's on_refresh_requested) — so Settings, Deploy,
        Dashboard etc. still need a restart to see a change made here, exactly as
        _confirm_and_save_profile's own "restart the cockpit for other tabs to see the change"
        already says. Wiring that up everywhere is a separate change, not this one.
        """
        if self.host_profile is None:
            return
        try:
            self.host_profile = schema.try_load_host_profile(
                self.repo_root / "hosts" / f"{self.host_name}.yaml", self.manifest
            )
        except schema.ValidationError as e:
            self.notify(f"host profile error: {e}", severity="error")

    def reload_models(self) -> None:
        if self.host_profile is None:
            return
        try:
            self.models = schema.load_models(self.repo_root / "models.yaml", self.host_profile, self.manifest)
        except schema.ValidationError as e:
            self.notify(f"models.yaml error: {e}", severity="error")

    def reload_scripts(self) -> None:
        if self.host_profile is None:
            return
        scripts_path = self.repo_root / "scripts.yaml"
        if not scripts_path.exists():
            # scripts.yaml is optional (unlike models.yaml) — no scripts registered is the
            # normal case, not something to notify about.
            self.scripts = {"scripts": []}
            return
        try:
            self.scripts = schema.load_scripts(scripts_path)
        except schema.ValidationError as e:
            self.notify(f"scripts.yaml error: {e}", severity="error")


def main() -> None:
    CockpitApp().run()


if __name__ == "__main__":
    main()
