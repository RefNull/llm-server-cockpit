"""Cockpit "Scripts" tab: register ad-hoc `python serve.py`-style scripts and supervise them
via one generated systemd unit each (see provision/steps/scripts.py) — cockpit never holds a
live subprocess handle itself. Every write to scripts.yaml goes through the same
build-candidate -> validate -> write -> reload pattern deploy.py uses for models.yaml.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Select, Static, Switch, TextArea

from provision import schema
from provision.common import Runner
from provision.steps import scripts as scripts_step

from cockpit.widgets import CockpitScreenBase, ConfirmModal, SingleClickDataTable, TableAction

_RESTART_POLICY_OPTIONS = [("on-failure", "on-failure"), ("always", "always"), ("no", "no")]


class EditScriptModal(ModalScreen[bool]):
    """Modal dialog for adding or editing a script in scripts.yaml.

    Validates candidate configuration against schema.validate_scripts_dict before writing.
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    EditScriptModal {
        align: center middle;
    }
    #edit-script-dialog {
        width: 75;
        height: 85%;
        border: thick $background 80%;
        background: $surface;
        padding: $space-normal $space-section;
    }
    #edit-script-title {
        text-style: bold;
        color: $accent;
        margin-bottom: $space-normal;
    }
    #edit-script-scroll {
        height: 1fr;
    }
    #edit-script-scroll Label {
        margin-top: $space-normal;
        color: $text-muted;
    }
    #f-script-args {
        height: 5;
    }
    #script-form-error {
        color: $error;
        margin-top: $space-normal;
    }
    .switch-row {
        height: auto;
        align-vertical: middle;
        margin-top: $space-normal;
    }
    .switch-row Label {
        margin-left: $space-normal;
    }
    """

    def __init__(
        self,
        host_profile: dict,
        manifest: dict,
        scripts: dict,
        editing_id: str | None,
        repo_root: Path,
        cockpit_app: Any,
    ) -> None:
        super().__init__()
        self.host_profile = host_profile
        self.manifest = manifest
        self.scripts = scripts
        self.editing_id = editing_id
        self.repo_root = repo_root
        self.cockpit_app = cockpit_app
        self._script = (
            next((s for s in self.scripts.get("scripts", []) if s["id"] == editing_id), None)
            if editing_id
            else None
        )

    def compose(self) -> ComposeResult:
        title = f"Edit Script — {self.editing_id}" if self.editing_id else "Add Script"
        s = self._script
        restart_policy = s.get("restart_policy", "on-failure") if s else "on-failure"
        enabled_val = bool(s.get("enabled", False)) if s else False

        with Vertical(id="edit-script-dialog"):
            yield Static(title, id="edit-script-title")
            with VerticalScroll(id="edit-script-scroll"):
                yield Label("id")
                yield Input(
                    id="f-script-id",
                    placeholder="script id",
                    value=s["id"] if s else "",
                    disabled=bool(self.editing_id),
                )
                yield Label("path (absolute path to the .py file)")
                yield Input(
                    id="f-script-path",
                    placeholder="/opt/myservice/serve.py",
                    value=s["path"] if s else "",
                )
                yield Label("working_dir (optional — defaults to path's directory)")
                yield Input(
                    id="f-script-working-dir",
                    placeholder="",
                    value=s.get("working_dir", "") if s else "",
                )
                yield Label("python (optional — defaults to python3)")
                yield Input(
                    id="f-script-python",
                    placeholder="",
                    value=s.get("python", "") if s else "",
                )
                yield Label("args (one per line)")
                yield TextArea(
                    "\n".join(s.get("args", [])) if s else "",
                    id="f-script-args",
                )
                yield Label("restart_policy")
                yield Select(
                    _RESTART_POLICY_OPTIONS,
                    id="f-script-restart-policy",
                    allow_blank=False,
                    value=restart_policy,
                )
                with Horizontal(classes="switch-row"):
                    yield Switch(value=enabled_val, id="f-script-enabled")
                    yield Label("Enable on boot")
                yield Static("", id="script-form-error", classes="error-text")
            with Horizontal(classes="action-row-primary"):
                yield Button("Save", id="btn-save", variant="primary", classes="thin-button")
                yield Button("Cancel", id="btn-cancel", classes="thin-button")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def _set_form_error(self, text: str) -> None:
        self.query_one("#script-form-error", Static).update(text)

    def _build_script_from_form(self) -> tuple[dict | None, str | None]:
        script_id = self.query_one("#f-script-id", Input).value.strip()
        path = self.query_one("#f-script-path", Input).value.strip()
        if not script_id:
            return None, "id is required"
        if not path:
            return None, "path is required"
        working_dir = self.query_one("#f-script-working-dir", Input).value.strip()
        python = self.query_one("#f-script-python", Input).value.strip()
        args = [line.strip() for line in self.query_one("#f-script-args", TextArea).text.splitlines() if line.strip()]
        restart_policy = str(self.query_one("#f-script-restart-policy", Select).value)
        enabled = self.query_one("#f-script-enabled", Switch).value

        script: dict[str, Any] = {"id": script_id, "path": path, "restart_policy": restart_policy, "enabled": enabled}
        if working_dir:
            script["working_dir"] = working_dir
        if python:
            script["python"] = python
        if args:
            script["args"] = args
        return script, None

    @work
    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(False)
            return
        if event.button.id != "btn-save":
            return

        script, error = self._build_script_from_form()
        if error:
            self._set_form_error(error)
            return

        existing = self.scripts.get("scripts", [])
        others = [s for s in existing if s["id"] != self.editing_id] if self.editing_id else list(existing)
        if any(s["id"] == script["id"] for s in others):
            self._set_form_error(f"duplicate script id {script['id']!r}")
            return
        candidate = {"scripts": others + [script]}

        try:
            validated = schema.validate_scripts_dict(candidate, source="scripts.yaml")
        except schema.ValidationError as e:
            self._set_form_error(str(e))
            return

        verb = "Save changes to" if self.editing_id else "Add"
        confirmed = await self.app.push_screen_wait(
            ConfirmModal(f"{verb} script {script['id']!r} in scripts.yaml?", confirm_label="Save", danger=True)
        )
        if not confirmed:
            return

        target = self.repo_root / "scripts.yaml"
        target.write_text(yaml.safe_dump(validated, sort_keys=False), encoding="utf-8")
        self.cockpit_app.reload_scripts()
        self.app.notify(f"saved script {script['id']!r}")
        self.dismiss(True)


class ScriptsScreen(CockpitScreenBase):
    """Cockpit "Scripts" tab conforming to Archetype B (Table-Driven Inventory)."""

    BINDINGS = [
        ("e", "edit_script", "Edit"),
    ]

    DEFAULT_CSS = """
    ScriptsScreen {
        height: 1fr;
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
        self.scripts = getattr(app_ref, "scripts", {"scripts": []})
        # id -> {"unit_active": bool, "unit_enabled": bool} from the last refresh. Drives the
        # Run/Boot toggle columns' labels, so it must be written before action_cells() is
        # called for a row (see _apply_rows).
        self._status_by_id: dict[str, dict[str, bool]] = {}
        self._cursor_script_id: str | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            # No fixed_columns: a pinned column is painted from datatable--fixed, which
            # REPLACES the row style (_data_table.py _render_line_in_row) rather than
            # compositing with it, so it cut a flat band down the pinned columns through the
            # zebra stripes. The widths below fit the 115-cell usable viewport, so there is no
            # horizontal scroll for it to pin against.
            table = SingleClickDataTable(id="scripts-table", zebra_stripes=True, classes="data-table")
            table.cursor_type = "row"
            yield table
            with Horizontal(classes="action-row-primary"):
                # The only screen-level action left: every per-script operation is an in-table
                # action column on its own row, so there is no selection to act on.
                yield Button("New Script", id="btn-new-script", variant="primary", classes="thin-button")
            yield Static("", id="status-message", classes="status-text")

    def on_mount(self) -> None:
        table = self.query_one("#scripts-table", SingleClickDataTable)
        # Column budget (DESIGN.md §4): content + 2 padding per column against the 115-cell
        # usable viewport at 121x30. Data 16+27+20 = 63, actions 9+11+8+10 = 38, 7 columns →
        # 101 + 14 = 115.
        table.add_column("ID", width=16)
        table.add_column("Path", width=27)
        table.add_column("Status", width=20)
        # Start/Stop and Enable/Disable are one toggle column each, not two static ones: six
        # static action columns need 56 content cells and blow the budget above, and only one
        # of each pair is ever applicable to a given row anyway.
        table.add_action_column(
            TableAction(
                "run",
                lambda sid: "Stop" if self._status_by_id.get(sid, {}).get("unit_active") else "Start",
                width=9,
                confirm="{action} script {row}?",
            )
        )
        table.add_action_column(
            TableAction(
                "boot",
                lambda sid: "Disable" if self._status_by_id.get(sid, {}).get("unit_enabled") else "Enable",
                width=11,
                confirm="{action} script {row} at boot?",
            )
        )
        # Edit already existed as a keybinding ('e') with no clickable affordance — exposing it
        # as a per-row action column makes it discoverable without a mouse-only regression.
        table.add_action_column(TableAction("edit", "Edit"))
        table.add_action_column(
            TableAction("remove", "Remove", destructive=True, confirm="Remove script {row} from scripts.yaml?")
        )
        self._refresh_table()

    def on_refresh_requested(self) -> None:
        self.scripts = getattr(self.cockpit_app, "scripts", self.scripts)
        self._refresh_table()

    # ------------------------------------------------------------------ rendering

    @work(thread=True)
    def _refresh_table(self) -> None:
        # Passive status read, no completion toast (DESIGN.md §6): per-script systemctl errors
        # are rendered into the row's own Status cell, and this runs on mount/refresh rather
        # than on an operator action.
        rows = []
        for script in self.scripts.get("scripts", []):
            try:
                st = scripts_step.status(script["id"])
                status_text = f"{'active' if st['unit_active'] else 'stopped'}, {'enabled' if st['unit_enabled'] else 'disabled'}"
            except Exception as e:
                st = {"unit_active": False, "unit_enabled": False}
                status_text = f"error checking status ({e})"
            rows.append((script, st, status_text))
        self.app.call_from_thread(self._apply_rows, rows)

    def _apply_rows(self, rows: list[tuple[dict, dict[str, bool], str]]) -> None:
        if not self.is_mounted:
            return
        table = self.query_one("#scripts-table", SingleClickDataTable)
        table.clear()
        # Before any action_cells() call below: the Run/Boot columns read their labels here.
        self._status_by_id = {script["id"]: st for script, st, _ in rows}
        if self._cursor_script_id not in self._status_by_id:
            self._cursor_script_id = None
        for script, _st, status_text in rows:
            sid = script["id"]
            # rich.text.Text, not raw str (DESIGN.md §4.6 / Phase 0a.6): the app console has
            # markup=True, so an operator-chosen script path, or a status string embedding an
            # exception message, containing brackets would have that span silently eaten by
            # Rich as a markup tag.
            table.add_row(
                Text(sid),
                Text(script["path"]),
                Text(status_text),
                *table.action_cells(sid),
                key=sid,
            )
        if self._cursor_script_id is None and table.row_count > 0:
            # DataTable defaults its cursor to row 0 without firing RowHighlighted for it, so
            # sync explicitly here.
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            self._cursor_script_id = row_key.value if row_key is not None else None

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "scripts-table":
            return
        self._cursor_script_id = event.row_key.value if event.row_key is not None else None

    def _set_status(self, text: str) -> None:
        self.query_one("#status-message", Static).update(text)

    # ------------------------------------------------------------------ modal open

    @work
    async def _open_modal_for_add(self) -> None:
        saved = await self.app.push_screen_wait(
            EditScriptModal(
                host_profile=self.host_profile,
                manifest=self.manifest,
                scripts=self.scripts,
                editing_id=None,
                repo_root=self.repo_root,
                cockpit_app=self.cockpit_app,
            )
        )
        if saved:
            self.scripts = getattr(self.cockpit_app, "scripts", self.scripts)
            self._refresh_table()
            self._set_status("script added — scripts.yaml written")

    @work
    async def _open_modal_for_edit(self, script_id: str) -> None:
        saved = await self.app.push_screen_wait(
            EditScriptModal(
                host_profile=self.host_profile,
                manifest=self.manifest,
                scripts=self.scripts,
                editing_id=script_id,
                repo_root=self.repo_root,
                cockpit_app=self.cockpit_app,
            )
        )
        if saved:
            self.scripts = getattr(self.cockpit_app, "scripts", self.scripts)
            self._refresh_table()
            self._set_status(f"script {script_id!r} updated — scripts.yaml written")

    def action_edit_script(self) -> None:
        if self._cursor_script_id:
            self._open_modal_for_edit(self._cursor_script_id)
        else:
            self._set_status("select a script row first")

    # ------------------------------------------------------------------ per-row table actions

    async def handle_table_action(self, action_id: str, row_key: str, table) -> None:
        # Already confirmed by CockpitScreenBase._on_table_action_invoked for every action
        # that declared a confirm= / destructive=True.
        if action_id == "edit":
            self._open_modal_for_edit(row_key)
        elif action_id == "remove":
            await self._remove_ids([row_key])
        elif action_id == "run":
            active = self._status_by_id.get(row_key, {}).get("unit_active")
            self._run_unit_action("stop" if active else "start", row_key)
        elif action_id == "boot":
            enabled = self._status_by_id.get(row_key, {}).get("unit_enabled")
            self._run_unit_action("disable" if enabled else "enable", row_key)

    @work(thread=True)
    def _run_unit_action(self, verb: str, script_id: str) -> None:
        action = {
            "start": scripts_step.start,
            "stop": scripts_step.stop,
            "enable": scripts_step.enable,
            "disable": scripts_step.disable,
        }[verb]
        try:
            action(script_id, self.runner)
        except Exception as e:
            self.app.call_from_thread(
                self.app.notify, f"{script_id}: {verb} failed — {e}", severity="error"
            )
        else:
            # Not f"{verb}ed" — that produced "stoped"/"enableed". The verb is already the
            # right word; it just isn't a regular past tense.
            self.app.call_from_thread(self.app.notify, f"{script_id}: {verb} complete")
        self.app.call_from_thread(self._refresh_table)

    # ------------------------------------------------------------------ button dispatch

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # No `await` on a @work method: the decorator returns a Worker, which is not
        # awaitable — awaiting one raises TypeError and takes down the app. The worker is
        # already running by the time the call returns; there is nothing to wait for here.
        if (event.button.id or "") == "btn-new-script":
            self._open_modal_for_add()

    # ------------------------------------------------------------------ remove

    async def _remove_ids(self, ids_to_remove: list[str]) -> None:
        remove_set = set(ids_to_remove)
        new_list = [s for s in self.scripts.get("scripts", []) if s["id"] not in remove_set]
        candidate = {"scripts": new_list}
        try:
            validated = schema.validate_scripts_dict(candidate, source="scripts.yaml")
        except schema.ValidationError as e:
            self._set_status(f"remove blocked by validation: {e}")
            return

        target = self.repo_root / "scripts.yaml"
        target.write_text(yaml.safe_dump(validated, sort_keys=False), encoding="utf-8")
        self.cockpit_app.reload_scripts()
        self.scripts = self.cockpit_app.scripts
        self._selected_ids -= remove_set
        self._refresh_table()
        self._set_status(f"removed {len(ids_to_remove)} script(s) — scripts.yaml written")
        self.app.notify(f"removed {len(ids_to_remove)} script(s)")
