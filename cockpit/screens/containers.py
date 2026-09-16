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

from rich.text import Text

from provision.common import Runner
from provision.steps import docker

from cockpit.widgets import CockpitScreenBase, InfoModal, SingleClickDataTable, TableAction


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
        padding: $space-normal $space-section;
        margin-bottom: $space-normal;
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

    BINDINGS = [
        ("c", "view_compose", "Compose"),
        ("s", "exec_shell", "Shell"),
    ]

    def action_view_compose(self) -> None:
        self._view_compose()

    def action_exec_shell(self) -> None:
        self._exec_shell()

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("", id="docker-banner")
            # No fixed_columns (DESIGN.md §4.2): datatable--fixed REPLACES the row style
            # rather than compositing with it, so pinning Name cut a flat band down column 0
            # through the zebra stripes. Below 121 cells the row scrolls horizontally without
            # a pinned identifier — the banding cost more than the pin was worth.
            table = SingleClickDataTable(
                id="containers-table", zebra_stripes=True, classes="data-table"
            )
            table.cursor_type = "row"
            yield table
            with Horizontal(classes="action-row-primary"):
                yield Button("Restart all", id="btn-restart-all", variant="error", classes="thin-button")
                yield Button("View Logs", id="btn-view-logs", classes="thin-button")
            yield Label("", id="docker-status", classes="status-text")

    def on_mount(self) -> None:
        table = self.query_one("#containers-table", SingleClickDataTable)
        # Column budget (DESIGN.md §4): content + 2 padding per column against the 115-cell
        # usable viewport at 121x30. The old widths summed to 132 render cells, which put the
        # "[ Restart ]" action column off-screen with no horizontal-scroll affordance to reach
        # it — the same defect the Downloads budget comment records. 22+24+20+18+8+11 = 103,
        # render 103 + 2*6 = 115.
        table.add_column("Name", width=22)
        table.add_column("Image", width=24)
        table.add_column("Status", width=20)
        table.add_column("Ports", width=18)
        table.add_column("Compose", width=8)
        # Not destructive (that's $error-styled, reserved for Remove/Delete, DESIGN.md §9) —
        # restarting a container is tier 2 (mutates_system), confirmed, but reversible.
        table.add_action_column(TableAction("restart", "Restart", confirm="Restart {row}?"))
        # No data read here — ensure_first_view() does it when this tab is first shown.

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
            # rich.text.Text, not raw str (DESIGN.md §4.6 / Phase 0a.6): the app console has
            # markup=True, so a raw port mapping like "[::]:8080->8080/tcp" gets parsed as a
            # Rich tag and silently eaten instead of rendered.
            table.add_row(
                Text(c["name"]),
                Text(c["image"]),
                Text(c["status"]),
                Text(c["ports"] or ""),
                Text(compose_label),
                *table.action_cells(c["name"]),
                key=c["name"],
            )

        if self._selected_name not in self._containers:
            self._selected_name = None
        self._sync_button_state()

    def _sync_button_state(self) -> None:
        container = self._containers.get(self._selected_name) if self._selected_name else None
        has_selection = container is not None

        self.query_one("#btn-view-logs", Button).disabled = not has_selection
        self.query_one("#btn-restart-all", Button).disabled = not self._containers

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
        if button_id == "btn-view-logs":
            self._view_logs()
        elif button_id == "btn-restart-all":
            await self._handle_restart_all_press()

    async def handle_table_action(self, action_id: str, row_key: str, table) -> None:
        if action_id == "restart":
            self._run_restart(row_key)

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
