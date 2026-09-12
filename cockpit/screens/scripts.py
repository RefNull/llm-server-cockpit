"""Cockpit "Scripts" tab: register ad-hoc `python serve.py`-style scripts and supervise them
via one generated systemd unit each (see provision/steps/scripts.py) — cockpit never holds a
live subprocess handle itself. Every write to scripts.yaml goes through the same
build-candidate -> validate -> write -> reload pattern deploy.py uses for models.yaml.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Button, DataTable, Input, Label, Select, Static, Switch, TextArea

from provision import schema
from provision.common import Runner
from provision.steps import scripts as scripts_step

from cockpit.widgets import ConfirmModal, SingleClickDataTable, selection_marker

_RESTART_POLICY_OPTIONS = [("on-failure", "on-failure"), ("always", "always"), ("no", "no")]


class ScriptsScreen(Widget):
    """Mounted inside a TabPane by cockpit/app.py — not a Textual Screen.

    Two independent selection mechanisms on the one table, matching what each needs: the
    ticked set (SingleClickDataTable + selection_marker, same convention as Installs/Downloads)
    drives the genuinely batch actions (Start/Stop/Enable/Disable selected); the plain
    highlighted cursor row (RowHighlighted, always up to date on arrow-key navigation too)
    drives Edit/Remove, which only ever make sense for exactly one entry — mirrors deploy.py's
    single-row model-editing convention.
    """

    DEFAULT_CSS = """
    ScriptsScreen {
        height: 1fr;
    }
    ScriptsScreen #edit-form {
        height: auto;
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
        self._selected_ids: set[str] = set()
        self._cursor_script_id: str | None = None
        self._editing_id: str | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("Ad-hoc Python scripts, supervised via one generated systemd unit each.", classes="subtitle")
            table = SingleClickDataTable(id="scripts-table", zebra_stripes=True, classes="data-table")
            table.cursor_type = "row"
            yield table
            with Horizontal(classes="button-row"):
                yield Button("Start selected", id="start-btn", variant="primary", classes="thin-button")
                yield Button("Stop selected", id="stop-btn", variant="error", classes="thin-button")
                yield Button("Enable on boot", id="enable-btn", classes="thin-button")
                yield Button("Disable", id="disable-btn", classes="thin-button")
            with Horizontal(classes="button-row"):
                yield Button("Add", id="btn-add", variant="primary", classes="thin-button")
                yield Button("Edit", id="btn-edit", classes="thin-button")
                yield Button("Remove", id="btn-remove", variant="error", classes="thin-button")
            yield Static("", id="status-message", classes="status-text")

            with Vertical(id="edit-form", classes="panel"):
                yield Static("", id="form-title", classes="panel-title")
                yield Label("id")
                yield Input(id="f-id", placeholder="script id")
                yield Label("path (absolute path to the .py file)")
                yield Input(id="f-path", placeholder="/opt/myservice/serve.py")
                yield Label("working_dir (optional — defaults to path's directory)")
                yield Input(id="f-working-dir", placeholder="")
                yield Label("python (optional — defaults to python3)")
                yield Input(id="f-python", placeholder="")
                yield Label("args (one per line)")
                yield TextArea(id="f-args")
                yield Label("restart_policy")
                yield Select(_RESTART_POLICY_OPTIONS, id="f-restart-policy", allow_blank=False, value="on-failure")
                with Horizontal(classes="switch-row"):
                    yield Switch(id="f-enabled")
                    yield Label("Enable on boot")
                yield Static("", id="form-error", classes="error-text")
                with Horizontal(classes="button-row"):
                    yield Button("Save", id="btn-save", variant="primary", classes="thin-button")
                    yield Button("Cancel", id="btn-cancel", classes="thin-button")

    def on_mount(self) -> None:
        table = self.query_one("#scripts-table", DataTable)
        table.add_columns("", "ID", "Path", "Status")
        self.query_one("#edit-form").display = False
        self._refresh_table()

    def on_refresh_requested(self) -> None:
        self.scripts = getattr(self.cockpit_app, "scripts", self.scripts)
        self._refresh_table()

    # ------------------------------------------------------------------ rendering

    @work(thread=True)
    def _refresh_table(self) -> None:
        rows = []
        for script in self.scripts.get("scripts", []):
            try:
                st = scripts_step.status(script["id"])
                status_text = f"{'active' if st['unit_active'] else 'stopped'}, {'enabled' if st['unit_enabled'] else 'disabled'}"
            except Exception as e:
                status_text = f"error checking status ({e})"
            rows.append((script, status_text))
        self.app.call_from_thread(self._apply_rows, rows)

    def _apply_rows(self, rows: list[tuple[dict, str]]) -> None:
        if not self.is_mounted:
            return
        table = self.query_one("#scripts-table", DataTable)
        table.clear()
        known_ids = {script["id"] for script, _ in rows}
        self._selected_ids &= known_ids
        if self._cursor_script_id not in known_ids:
            self._cursor_script_id = None
        for script, status_text in rows:
            sid = script["id"]
            table.add_row(
                selection_marker(sid in self._selected_ids),
                sid,
                script["path"],
                status_text,
                key=sid,
            )
        if self._cursor_script_id is None and table.row_count > 0:
            # DataTable defaults its cursor to row 0 without firing RowHighlighted for it, so
            # sync explicitly here — otherwise Edit/Remove stay disabled after the very first
            # load (or any refresh that lost the previous selection) until the user manually
            # moves the cursor once.
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            self._cursor_script_id = row_key.value if row_key is not None else None
        self._sync_button_state()

    def _sync_button_state(self) -> None:
        has_ticked = bool(self._selected_ids)
        has_cursor = self._cursor_script_id is not None
        self.query_one("#start-btn", Button).disabled = not has_ticked
        self.query_one("#stop-btn", Button).disabled = not has_ticked
        self.query_one("#enable-btn", Button).disabled = not has_ticked
        self.query_one("#disable-btn", Button).disabled = not has_ticked
        self.query_one("#btn-edit", Button).disabled = not has_cursor
        self.query_one("#btn-remove", Button).disabled = not has_cursor

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "scripts-table":
            return
        self._cursor_script_id = event.row_key.value if event.row_key is not None else None
        self._sync_button_state()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "scripts-table" or event.row_key is None or event.row_key.value is None:
            return
        sid = event.row_key.value
        if sid in self._selected_ids:
            self._selected_ids.discard(sid)
        else:
            self._selected_ids.add(sid)
        self._refresh_table()

    def _script_by_id(self, script_id: str) -> dict[str, Any] | None:
        return next((s for s in self.scripts.get("scripts", []) if s["id"] == script_id), None)

    # ------------------------------------------------------------------ button dispatch

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "start-btn":
            await self._handle_bulk_press("start", scripts_step.start)
        elif button_id == "stop-btn":
            await self._handle_bulk_press("stop", scripts_step.stop)
        elif button_id == "enable-btn":
            await self._handle_bulk_press("enable", scripts_step.enable)
        elif button_id == "disable-btn":
            await self._handle_bulk_press("disable", scripts_step.disable)
        elif button_id == "btn-add":
            self._open_form_for_add()
        elif button_id == "btn-edit":
            self._open_form_for_edit()
        elif button_id == "btn-remove":
            await self._confirm_and_remove()
        elif button_id == "btn-save":
            await self._confirm_and_save()
        elif button_id == "btn-cancel":
            self._hide_form()

    # ------------------------------------------------------------------ bulk start/stop/enable/disable

    async def _handle_bulk_press(self, verb: str, action) -> None:
        ids = sorted(self._selected_ids)
        if not ids:
            return
        confirmed = await self.app.push_screen_wait(
            ConfirmModal(f"{verb.capitalize()} {len(ids)} script(s)?\n{', '.join(ids)}", confirm_label=verb.capitalize(), danger=True)
        )
        if confirmed:
            self._run_bulk(verb, action, ids)

    @work(thread=True)
    def _run_bulk(self, verb: str, action, ids: list[str]) -> None:
        failures: list[str] = []
        for sid in ids:
            try:
                action(sid, self.runner)
            except Exception as e:
                failures.append(sid)
                self.app.call_from_thread(self.app.notify, f"{sid}: {verb} failed — {e}", severity="error")
        if not failures:
            self.app.call_from_thread(self.app.notify, f"{verb}ed {len(ids)} script(s)")
        self.app.call_from_thread(self._refresh_table)

    # ------------------------------------------------------------------ add / edit / remove form

    def _set_form_error(self, text: str) -> None:
        self.query_one("#form-error", Static).update(text)

    def _set_status(self, text: str) -> None:
        self.query_one("#status-message", Static).update(text)

    def _show_form(self) -> None:
        self.query_one("#edit-form").display = True

    def _hide_form(self) -> None:
        self.query_one("#edit-form").display = False
        self._set_form_error("")

    def _open_form_for_add(self) -> None:
        self._editing_id = None
        self.query_one("#form-title", Static).update("Add script")
        self.query_one("#f-id", Input).value = ""
        self.query_one("#f-path", Input).value = ""
        self.query_one("#f-working-dir", Input).value = ""
        self.query_one("#f-python", Input).value = ""
        self.query_one("#f-args", TextArea).text = ""
        self.query_one("#f-restart-policy", Select).value = "on-failure"
        self.query_one("#f-enabled", Switch).value = False
        self._set_form_error("")
        self._show_form()

    def _open_form_for_edit(self) -> None:
        if self._cursor_script_id is None:
            self._set_status("select a script row first")
            return
        script = self._script_by_id(self._cursor_script_id)
        if script is None:
            self._set_status("selected script is no longer configured — refresh and try again")
            return
        self._editing_id = script["id"]
        self.query_one("#form-title", Static).update(f"Edit script — {script['id']}")
        self.query_one("#f-id", Input).value = script["id"]
        self.query_one("#f-path", Input).value = script["path"]
        self.query_one("#f-working-dir", Input).value = script.get("working_dir", "")
        self.query_one("#f-python", Input).value = script.get("python", "")
        self.query_one("#f-args", TextArea).text = "\n".join(script.get("args", []))
        self.query_one("#f-restart-policy", Select).value = script.get("restart_policy", "on-failure")
        self.query_one("#f-enabled", Switch).value = bool(script.get("enabled", False))
        self._set_form_error("")
        self._show_form()

    def _build_script_from_form(self) -> tuple[dict | None, str | None]:
        script_id = self.query_one("#f-id", Input).value.strip()
        path = self.query_one("#f-path", Input).value.strip()
        if not script_id:
            return None, "id is required"
        if not path:
            return None, "path is required"
        working_dir = self.query_one("#f-working-dir", Input).value.strip()
        python = self.query_one("#f-python", Input).value.strip()
        args = [line.strip() for line in self.query_one("#f-args", TextArea).text.splitlines() if line.strip()]
        restart_policy = self.query_one("#f-restart-policy", Select).value
        enabled = self.query_one("#f-enabled", Switch).value

        script: dict[str, Any] = {"id": script_id, "path": path, "restart_policy": restart_policy, "enabled": enabled}
        if working_dir:
            script["working_dir"] = working_dir
        if python:
            script["python"] = python
        if args:
            script["args"] = args
        return script, None

    def _validate_candidate(self, candidate: dict) -> tuple[bool, str, dict | None]:
        try:
            validated = schema.validate_scripts_dict(candidate, source="scripts.yaml")
            return True, "", validated
        except schema.ValidationError as e:
            return False, str(e), None

    def _write_scripts_yaml(self, data: dict) -> None:
        target = self.repo_root / "scripts.yaml"
        target.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    def _after_write(self, action_past_tense: str) -> None:
        self.cockpit_app.reload_scripts()
        self.scripts = self.cockpit_app.scripts
        self._refresh_table()
        self._set_status(f"{action_past_tense} — scripts.yaml written")

    @work
    async def _confirm_and_save(self) -> None:
        script, error = self._build_script_from_form()
        if error:
            self._set_form_error(error)
            return

        others = [s for s in self.scripts.get("scripts", []) if s["id"] != self._editing_id] if self._editing_id else list(self.scripts.get("scripts", []))
        if any(s["id"] == script["id"] for s in others):
            self._set_form_error(f"duplicate script id {script['id']!r}")
            return
        candidate = {"scripts": others + [script]}

        ok, err, validated = self._validate_candidate(candidate)
        if not ok:
            self._set_form_error(err)
            return

        verb = "Save changes to" if self._editing_id else "Add"
        confirmed = await self.app.push_screen_wait(
            ConfirmModal(f"{verb} script {script['id']!r} in scripts.yaml?", confirm_label="Save", danger=True)
        )
        if not confirmed:
            return

        self._write_scripts_yaml(validated)
        self._after_write(f"saved script {script['id']!r}")
        self._hide_form()

    @work
    async def _confirm_and_remove(self) -> None:
        script_id = self._cursor_script_id
        if script_id is None:
            self._set_status("select a script row first")
            return
        confirmed = await self.app.push_screen_wait(
            ConfirmModal(
                f"Remove script {script_id!r} from scripts.yaml? Its systemd unit is left in place "
                "(stop it first if it's running) — only the registration is removed.",
                confirm_label="Remove",
                danger=True,
            )
        )
        if not confirmed:
            return

        new_list = [s for s in self.scripts.get("scripts", []) if s["id"] != script_id]
        candidate = {"scripts": new_list}
        ok, err, validated = self._validate_candidate(candidate)
        if not ok:
            self._set_status(f"remove blocked by validation: {err}")
            return

        self._write_scripts_yaml(validated)
        self._after_write(f"removed script {script_id!r}")
