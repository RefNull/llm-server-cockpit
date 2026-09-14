"""Cockpit "Containers" tab: read-only observer over `docker ps` plus a handful of narrowly-
scoped actions (view the compose.yaml a container was started from, view recent logs, open an
interactive exec shell, restart). No compose management, no lifecycle beyond restart — see
provision/steps/docker.py's module docstring for the explicit scope boundary.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, DataTable, Label, Static

from provision.common import Runner
from provision.steps import docker

from cockpit.widgets import CockpitScreenBase, InfoModal, SingleClickDataTable


class ContainersScreen(CockpitScreenBase):
    """Mounted inside a TabPane by cockpit/app.py — not a Textual Screen.

    Single-select, not tick-able: every per-row action (view compose/logs, exec, restart)
    targets exactly one container, unlike the Installs/Downloads tables' genuine batch
    operations. The one action that spans every container — "Restart all" — is its own
    button below the table rather than requiring a multi-select UI for a single use case.
    """

    DEFAULT_CSS = """
    ContainersScreen {
        height: 1fr;
    }
    #docker-banner {
        padding: 1 2;
        margin-bottom: 1;
        background: $error 20%;
        color: $error;
        display: none;
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
        # name -> container dict, for the rows currently shown in the table.
        self._containers: dict[str, dict[str, Any]] = {}
        self._selected_name: str | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("Running and stopped Docker containers on this host.", classes="subtitle")
            yield Static("", id="docker-banner")
            # fixed_columns=1: Name is the identifier and the five columns together run well
            # past 80 cells, so it stays pinned while Image/Status/Ports scroll (DESIGN.md §4.2).
            table = SingleClickDataTable(
                id="containers-table", zebra_stripes=True, classes="data-table", fixed_columns=1
            )
            table.cursor_type = "row"
            yield table
            with Horizontal(classes="button-row"):
                yield Button("View compose.yaml", id="view-compose-btn", classes="thin-button")
                yield Button("View logs", id="view-logs-btn", classes="thin-button")
                yield Button("Exec shell", id="exec-btn", classes="thin-button")
                yield Button("Restart", id="restart-btn", variant="error", classes="thin-button")
            with Horizontal(classes="button-row"):
                yield Button("Restart all", id="restart-all-btn", variant="error", classes="thin-button")
            yield Label("", id="docker-status", classes="status-text")

    def on_mount(self) -> None:
        table = self.query_one("#containers-table", SingleClickDataTable)
        table.add_column("Name", width=24)
        table.add_column("Image", width=30)
        table.add_column("Status", width=22)
        table.add_column("Ports", width=24)
        table.add_column("Compose", width=9)
        self._refresh_table()

    def on_refresh_requested(self) -> None:
        self._refresh_table()

    # ------------------------------------------------------------------ rendering

    @work(thread=True)
    def _refresh_table(self) -> None:
        # Passive status read, no completion toast (DESIGN.md §6): a `docker ps` failure is
        # surfaced in the inline #docker-banner, and this runs on mount/refresh, not on an
        # operator action.
        try:
            containers = docker.list_containers()
            error = None
        except RuntimeError as e:
            containers = []
            error = str(e)
        self.app.call_from_thread(self._apply_containers, containers, error)

    def _apply_containers(self, containers: list[dict[str, Any]], error: str | None) -> None:
        if not self.is_mounted:
            return
        banner = self.query_one("#docker-banner", Static)
        table = self.query_one("#containers-table", SingleClickDataTable)
        self._containers = {c["name"]: c for c in containers}

        if error:
            banner.update(error)
            banner.display = True
        else:
            banner.display = False

        table.clear()
        for c in containers:
            compose_label = "yes" if c.get("compose_files") else "—"
            table.add_row(c["name"], c["image"], c["status"], c["ports"] or "", compose_label, key=c["name"])

        if self._selected_name not in self._containers:
            self._selected_name = None
        self._sync_button_state()

    def _sync_button_state(self) -> None:
        container = self._containers.get(self._selected_name) if self._selected_name else None
        has_selection = container is not None
        is_running = has_selection and container.get("state") == "running"
        has_compose = has_selection and bool(container.get("compose_files"))

        self.query_one("#view-compose-btn", Button).disabled = not has_compose
        self.query_one("#view-logs-btn", Button).disabled = not has_selection
        self.query_one("#exec-btn", Button).disabled = not is_running
        self.query_one("#restart-btn", Button).disabled = not has_selection
        self.query_one("#restart-all-btn", Button).disabled = not self._containers

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "containers-table":
            return
        if event.row_key is None or event.row_key.value is None:
            return
        self._selected_name = event.row_key.value
        self._sync_button_state()

    # ------------------------------------------------------------------ button dispatch

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "view-compose-btn":
            self._view_compose()
        elif button_id == "view-logs-btn":
            self._view_logs()
        elif button_id == "exec-btn":
            self._exec_shell()
        elif button_id == "restart-btn":
            await self._handle_restart_press()
        elif button_id == "restart-all-btn":
            await self._handle_restart_all_press()

    # ------------------------------------------------------------------ view compose / logs (off main thread)

    @work(thread=True)
    def _view_compose(self) -> None:
        container = self._containers.get(self._selected_name or "")
        if container is None:
            return
        try:
            content = docker.read_compose_file(container)
        except (ValueError, OSError) as e:
            self.app.call_from_thread(self.app.notify, f"couldn't read compose.yaml: {e}", severity="error")
            return
        self.app.call_from_thread(
            self.app.push_screen, InfoModal(f"compose.yaml — {container['name']}", content)
        )

    @work(thread=True)
    def _view_logs(self) -> None:
        name = self._selected_name
        if name is None:
            return
        try:
            content = docker.read_logs(name)
        except Exception as e:
            self.app.call_from_thread(self.app.notify, f"couldn't read logs: {e}", severity="error")
            return
        self.app.call_from_thread(self.app.push_screen, InfoModal(f"Logs — {name}", content))

    # ------------------------------------------------------------------ exec (blocks the whole app on purpose)

    def _exec_shell(self) -> None:
        name = self._selected_name
        if name is None:
            return
        # Suspending hands the real terminal to `docker exec` for an interactive session — an
        # InfoModal (captured, non-interactive output) can't represent a shell. This blocks the
        # Textual event loop for as long as the shell runs, which is the intended behavior: the
        # whole point of suspending is a full handover, not a backgrounded action.
        with self.app.suspend():
            subprocess.run(["docker", "exec", "-it", name, "sh"])

    # ------------------------------------------------------------------ restart (blocking, off main thread)

    async def _handle_restart_press(self) -> None:
        name = self._selected_name
        if name is None:
            return
        # DESIGN.md §5 tier 2: restarts a running background service on the host.
        confirmed = await self.confirm(
            f"Restart container {name!r}?", confirm_label="Restart", mutates_system=True
        )
        if confirmed:
            self._run_restart(name)

    @work(thread=True)
    def _run_restart(self, name: str) -> None:
        try:
            docker.restart_container(name, self.runner)
        except Exception as e:
            self.app.call_from_thread(self.app.notify, f"{name}: restart failed — {e}", severity="error")
            return
        self.app.call_from_thread(self.app.notify, f"{name}: restarted")
        self.app.call_from_thread(self._refresh_table)

    async def _handle_restart_all_press(self) -> None:
        names = list(self._containers.keys())
        if not names:
            return
        # DESIGN.md §5 tier 2: restarts every container service on the host at once.
        confirmed = await self.confirm(
            f"Restart all {len(names)} container(s)?\n{', '.join(names)}",
            confirm_label="Restart all",
            mutates_system=True,
        )
        if confirmed:
            self._run_restart_all(names)

    @work(thread=True)
    def _run_restart_all(self, names: list[str]) -> None:
        try:
            docker.restart_all(names, self.runner)
        except Exception as e:
            self.app.call_from_thread(self.app.notify, f"restart all failed — {e}", severity="error")
            return
        self.app.call_from_thread(self.app.notify, f"restarted {len(names)} container(s)")
        self.app.call_from_thread(self._refresh_table)
