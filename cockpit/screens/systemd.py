"""Cockpit "Deployments > Systemd" tab: every systemd unit loaded on the host.

Unlike the Scripts tab — which manages only the units this toolkit generates from scripts.yaml
— this lists what the host actually has running, whoever installed it. That makes it long
(a Debian/Ubuntu host loads well over a hundred services), which is what the hide list is for:
units the operator does not care about are hidden, and "Show hidden" brings them back to be
unhidden.

The hide list is a per-host UI preference, stored beside the host profile rather than inside
it: hosts/<hostname>.yaml is validated declarative configuration that provision/ consumes, and
a cockpit view preference has no business failing its schema.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, DataTable, Input, Static

from cockpit.widgets import (
    CockpitScreenBase,
    InfoModal,
    SingleClickDataTable,
    TableAction,
)
from provision.common import Runner
from provision.steps import scripts as scripts_step
from provision.steps import systemd


class SystemdScreen(CockpitScreenBase):
    """Mounted inside a TabPane by cockpit/app.py — not a Textual Screen."""

    DEFAULT_CSS = """
    SystemdScreen {
        height: 1fr;
    }
    SystemdScreen #systemd-table {
        height: 1fr;
        max-height: 100%;
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
        self.runner = runner
        self.repo_root = repo_root
        self.cockpit_app = app_ref
        self._units: dict[str, dict[str, Any]] = {}
        self._hidden: set[str] = self._load_hidden()
        self._show_hidden = False
        self._filter = ""
        # Last fetch, so typing re-filters in memory instead of re-running systemctl per keypress.
        self._last_units: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ hide list

    def _hidden_path(self) -> Path:
        hostname = self.host_profile.get("hostname") or getattr(self.cockpit_app, "host_name", "host")
        return self.repo_root / "hosts" / f"{hostname}.hidden-units.yaml"

    def _load_hidden(self) -> set[str]:
        path = self._hidden_path()
        if not path.exists():
            return set()
        try:
            data = yaml.safe_load(path.read_text()) or {}
            return set(data.get("hidden") or [])
        except Exception:
            # A corrupt preferences file must never stop the tab from rendering; the operator
            # loses their hide list, not the screen.
            return set()

    def _save_hidden(self) -> None:
        path = self._hidden_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump({"hidden": sorted(self._hidden)}, sort_keys=False), encoding="utf-8")

    # ------------------------------------------------------------------ compose

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            # Filter above the table it filters, never inside an action row (DESIGN.md §3.2).
            with Horizontal(classes="inline-row", id="systemd-filter-row"):
                yield Input(placeholder="Filter units (substring, case-insensitive)", id="systemd-filter")
            table = SingleClickDataTable(id="systemd-table", zebra_stripes=True, classes="data-table")
            table.cursor_type = "row"
            yield table
            with Horizontal(classes="action-row-secondary"):
                yield Button("Show hidden", id="btn-toggle-hidden", classes="thin-button")
                yield Button("Refresh", id="btn-refresh-units", classes="thin-button")
            yield Static("", id="systemd-status", classes="status-text")

    def on_mount(self) -> None:
        table = self.query_one("#systemd-table", SingleClickDataTable)
        # Column budget (DESIGN.md §4): content 99 + 2 padding x 8 columns = 115, exactly the
        # ceiling. Unit takes the width because socket/path/mount names are long
        # (systemd-fsck@dev-disk-by-uuid-...), and Active/Sub values are short words.
        table.add_column("Unit", width=36)
        table.add_column("Active", width=8)
        table.add_column("Sub", width=8)
        table.add_action_column(TableAction("logs", "Logs", width=9, requires_root=True))
        table.add_action_column(TableAction("unitfile", "Unit", width=8))
        table.add_action_column(
            TableAction(
                "run",
                lambda unit: "Stop" if self._units.get(unit, {}).get("active") == "active" else "Start",
                width=9,
                requires_root=True,
                confirm="{action} {row}?",
            )
        )
        table.add_action_column(
            TableAction("restart", "Restart", width=11, requires_root=True, confirm="Restart {row}?")
        )
        # One toggle column rather than two: a unit is either on the hide list or it is not, and
        # the two labels are never both applicable to the same row.
        table.add_action_column(
            TableAction("hide", lambda unit: "Unhide" if unit in self._hidden else "Hide", width=10)
        )
        # No data read here — ensure_first_view() does it when this tab is first shown.

    def on_refresh_requested(self) -> None:
        self._refresh_table()

    # ------------------------------------------------------------------ rendering

    @work(thread=True)
    def _refresh_table(self) -> None:
        # Passive status read, no completion toast (DESIGN.md §6).
        units = systemd.list_units()
        self.app.call_from_thread(self._apply_rows, units)

    def _apply_rows(self, units: list[dict[str, Any]]) -> None:
        if not self.is_mounted:
            return
        self._units = {u["unit"]: u for u in units}
        self._last_units = units
        table = self.query_one("#systemd-table", SingleClickDataTable)
        table.clear()
        needle = self._filter.strip().lower()
        visible = [
            u for u in units
            if (u["unit"] in self._hidden) == self._show_hidden
            and (not needle or needle in u["unit"].lower() or needle in u.get("description", "").lower())
        ]
        for unit in visible:
            name = unit["unit"]
            active = unit["active"]
            style = "green" if active == "active" else "bold red" if active == "failed" else ""
            # rich.text.Text, never str (DESIGN.md §4.6): a unit description or name can carry
            # brackets, which a markup-enabled console would eat.
            table.add_row(
                Text(name),
                Text(active, style=style),
                Text(unit["sub"]),
                *table.action_cells(name),
                key=name,
            )
        self._sync_controls(len(visible), len(units))

    def _sync_controls(self, shown: int, total: int) -> None:
        hidden_count = len(self._hidden)
        self.query_one("#btn-toggle-hidden", Button).label = (
            f"Show active ({total - hidden_count})" if self._show_hidden else f"Show hidden ({hidden_count})"
        )
        scope = "hidden" if self._show_hidden else "visible"
        self.query_one("#systemd-status", Static).update(
            f"{shown} {scope} unit(s)"
            + (f" matching {self._filter.strip()!r}" if self._filter.strip() else "")
            + f" · {hidden_count} hidden of {total} loaded"
            if total
            else "no units loaded — is systemd available on this host?"
        )

    # ------------------------------------------------------------------ actions

    async def handle_table_action(self, action_id: str, row_key: str, table: DataTable) -> None:
        # Already confirmed by CockpitScreenBase for every action declaring confirm=/destructive.
        if action_id == "hide":
            self._hidden.symmetric_difference_update({row_key})
            self._save_hidden()
            self._refresh_table()
        elif action_id == "logs":
            self._view_logs(row_key)
        elif action_id == "unitfile":
            self._view_unitfile(row_key)
        elif action_id == "restart":
            self._run_unit_action("restart", row_key)
        elif action_id == "run":
            active = self._units.get(row_key, {}).get("active") == "active"
            self._run_unit_action("stop" if active else "start", row_key)

    @work(thread=True)
    def _view_logs(self, unit: str) -> None:
        # privileged_runner: the action declares requires_root=True, so the operator has already
        # been prompted. A system unit's journal is root-readable only — running journalctl
        # unprivileged anyway returns an empty log that looks exactly like "never ran".
        content = scripts_step.journal_tail(unit, runner=self.privileged_runner)
        self.app.call_from_thread(self.app.push_screen, InfoModal(f"journalctl -u {unit}", content))

    @work(thread=True)
    def _view_unitfile(self, unit: str) -> None:
        content = systemd.unit_source(unit)
        self.app.call_from_thread(self.app.push_screen, InfoModal(f"systemctl cat {unit}", content))

    @work(thread=True)
    def _run_unit_action(self, verb: str, unit: str) -> None:
        try:
            self.privileged_runner.run(["systemctl", verb, unit])
        except Exception as e:
            self.app.call_from_thread(self.app.notify, f"{unit}: {verb} failed — {e}", severity="error")
        else:
            self.app.call_from_thread(self.app.notify, f"{unit}: {verb} complete")
        self.app.call_from_thread(self._refresh_table)

    # ------------------------------------------------------------------ buttons

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "systemd-filter":
            return
        # Re-render from the last fetch. `systemctl list-units` per keystroke would spawn a
        # subprocess per character on a list this long.
        self._filter = event.value
        self._apply_rows(self._last_units)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "btn-toggle-hidden":
            self._show_hidden = not self._show_hidden
            self._refresh_table()
        elif button_id == "btn-refresh-units":
            self._refresh_table()
