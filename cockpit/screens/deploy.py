"""Cockpit "Deploy" tab: list/add/edit/delete entries in models.yaml, preview the
llama-swap config.yaml that would be generated from them, and apply it (install +
restart llama-swap) via provision.steps.swap.run — the same function the CLI uses.

Every write to the real models.yaml goes: build candidate in memory -> write to a
temp file -> schema.load_models(temp_path, ...) (the authoritative jsonschema +
cross-file validator) -> only on success, overwrite models.yaml and tell the app to
reload. A failed validation never touches the real file.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, DataTable, Input, Label, Select, Static, TextArea

from cockpit.widgets import ConfirmModal, SingleClickDataTable, selection_marker
from provision import schema
from provision.common import Runner
from provision.steps import swap

_ENGINE_OPTIONS = [("llama-cpp", "llama-cpp"), ("unmanaged", "unmanaged")]


class ConfigPasteModal(ModalScreen[str | None]):
    """Paste-a-llama-swap-config.yaml modal for the "Import from config.yaml" shortcut.
    Returns the pasted text on Parse, None on Cancel — deploy.py does the actual parsing
    (swap.parse_config_for_import) after the modal closes, so parse errors can be shown
    inline on the main screen's status line rather than re-opening this modal."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    ConfigPasteModal {
        align: center middle;
    }
    #paste-dialog {
        width: 90%;
        height: 80%;
        border: thick $background 80%;
        background: $surface;
        padding: 1 2;
    }
    #paste-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #paste-text {
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="paste-dialog"):
            yield Static("Paste a llama-swap config.yaml below", id="paste-title")
            yield TextArea(id="paste-text")
            with Horizontal(classes="button-row"):
                yield Button("Parse", id="btn-parse", variant="primary", classes="thin-button")
                yield Button("Cancel", id="btn-paste-cancel", classes="thin-button")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-parse":
            self.dismiss(self.query_one("#paste-text", TextArea).text)
        elif event.button.id == "btn-paste-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class DeployScreen(Widget):
    """Mounted inside a TabPane by cockpit/app.py — not a Textual Screen."""

    # .panel / .button-row / .status-text / .error-text come from cockpit/widgets.py's
    # SHARED_CSS (CockpitApp.CSS) — only this screen's own rules live here. Root content is
    # wrapped in a VerticalScroll (see compose()) so a tall form/preview doesn't get clipped
    # in a short terminal.
    DEFAULT_CSS = """
    DeployScreen {
        height: 1fr;
    }
    #edit-form, #preview-area {
        height: auto;
        max-height: 32;
    }
    #edit-form Label {
        margin-top: 1;
    }
    #f-args, #f-cmd, #f-env {
        height: 5;
    }
    #preview-text {
        height: 22;
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
        self.app_ref = app_ref
        self._editing_id: str | None = None  # None while the form is in "add" mode
        # id -> proposed model dict, for the current "Import from config.yaml" review table.
        self._import_candidates: dict[str, dict[str, Any]] = {}
        self._import_selected: set[str] = set()

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("Configure models.yaml and deploy the llama-swap inference routing service", classes="subtitle")
            yield Static("Models (models.yaml)", classes="section-title")
            yield DataTable(id="models-table", classes="data-table")
            with Horizontal(classes="button-row"):
                yield Button("Add", id="btn-add", variant="primary", classes="thin-button")
                yield Button("Edit", id="btn-edit", classes="thin-button")
                yield Button("Delete", id="btn-delete", variant="error", classes="thin-button")
                yield Button("Preview config.yaml", id="btn-preview", classes="thin-button")
                yield Button("Import from config.yaml", id="btn-import", classes="thin-button")
            yield Static("", id="status-message", classes="status-text")

            with Vertical(id="import-review", classes="panel"):
                yield Static("Proposed models from pasted config.yaml", classes="panel-title")
                yield Static(
                    "Every entry imports as engine: unmanaged with its original cmd preserved — a "
                    "llama-swap config never carries the repo_id a real llama-cpp entry needs, so "
                    "there's no confident way to reconstruct one. Tick the ones to keep, then hand-"
                    "convert any to llama-cpp afterward via Edit if you want that.",
                    classes="subtitle",
                )
                table = SingleClickDataTable(id="import-table", zebra_stripes=True, classes="data-table")
                table.cursor_type = "row"
                yield table
                yield Static("", id="import-error", classes="error-text")
                with Horizontal(classes="button-row"):
                    yield Button("Import selected", id="btn-import-selected", variant="primary", classes="thin-button")
                    yield Button("Close", id="btn-import-close", classes="thin-button")

            with Vertical(id="edit-form", classes="panel"):
                yield Static("", id="form-title", classes="panel-title")
                yield Label("id")
                yield Input(id="f-id", placeholder="model id")
                yield Label("engine")
                yield Select(_ENGINE_OPTIONS, id="f-engine", allow_blank=False, value="llama-cpp")

                with Vertical(id="f-llamacpp-fields"):
                    yield Label("repo_id")
                    yield Input(id="f-repo-id", placeholder="huggingface repo id")
                    yield Label("quant_file")
                    yield Input(id="f-quant-file", placeholder="quant filename")
                    yield Label("mmproj_file (optional)")
                    yield Input(id="f-mmproj-file", placeholder="")
                    yield Label("bind.gpu")
                    yield Select(self._gpu_options(), id="f-gpu", allow_blank=False)
                    yield Label("bind.backend")
                    yield Select([], id="f-backend", allow_blank=True)
                    yield Label("llama_server_args (one flag/value per line)")
                    yield TextArea(id="f-args")

                with Vertical(id="f-unmanaged-fields"):
                    yield Label("cmd (raw shell command, ${PORT} available)")
                    yield TextArea(id="f-cmd")

                yield Label("env (one KEY=VALUE per line)")
                yield TextArea(id="f-env")
                yield Label("ttl (seconds, 0 = never evict)")
                yield Input(id="f-ttl", value="0")
                yield Label("group (optional)")
                yield Input(id="f-group", placeholder="")

                yield Static("", id="form-error", classes="error-text")
                with Horizontal(classes="button-row"):
                    yield Button("Save", id="btn-save", variant="primary", classes="thin-button")
                    yield Button("Cancel", id="btn-cancel", classes="thin-button")

            with Vertical(id="preview-area", classes="panel"):
                yield Static("Generated config.yaml preview", classes="panel-title")
                yield TextArea(id="preview-text")
                with Horizontal(classes="button-row"):
                    yield Button(
                        "Deploy Host Configuration (llama-swap & systemd)",
                        id="btn-confirm-apply",
                        variant="primary",
                        classes="thin-button",
                    )
                    yield Button("Close preview", id="btn-close-preview", classes="thin-button")

    def on_mount(self) -> None:
        table = self.query_one("#models-table", DataTable)
        table.cursor_type = "row"
        self.query_one("#preview-text", TextArea).read_only = True
        self.query_one("#edit-form").display = False
        self.query_one("#preview-area").display = False
        self.query_one("#import-review").display = False
        self.query_one("#import-table", DataTable).add_columns("", "ID", "Engine", "cmd")
        self._populate_table()

    # -- host-profile-derived option lists -------------------------------------

    def _gpu_options(self) -> list[tuple[str, str]]:
        return [(g["id"], g["id"]) for g in self.host_profile["gpus"]]

    def _backend_options_for_gpu(self, gpu_id: str) -> list[tuple[str, str]]:
        for g in self.host_profile["gpus"]:
            if g["id"] == gpu_id:
                return [(b, b) for b in g["backends"]]
        return []

    def _refresh_backend_options(self, gpu_id: str, selected: str | None = None) -> None:
        backend_select = self.query_one("#f-backend", Select)
        options = self._backend_options_for_gpu(gpu_id)
        backend_select.set_options(options)
        if selected is not None and any(value == selected for _, value in options):
            backend_select.value = selected
        elif options:
            backend_select.value = options[0][1]
        else:
            backend_select.value = Select.BLANK

    # -- table ------------------------------------------------------------------

    def _populate_table(self) -> None:
        table = self.query_one("#models-table", DataTable)
        table.clear(columns=True)
        table.add_columns("ID", "Engine", "GPU", "Backend", "Group", "TTL")
        for m in self.models["models"]:
            if m["engine"] == "llama-cpp":
                gpu = m.get("bind", {}).get("gpu", "")
                backend = m.get("bind", {}).get("backend", "")
            else:
                gpu, backend = "", ""
            table.add_row(
                m["id"], m["engine"], gpu, backend, m.get("group", ""), str(m.get("ttl", "")),
                key=m["id"],
            )

    def _selected_model_id(self) -> str | None:
        table = self.query_one("#models-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        row_key = cell_key.row_key
        return str(row_key.value) if row_key is not None and row_key.value is not None else None

    def on_refresh_requested(self) -> None:
        """Called by CockpitApp.action_refresh_all — pick up models.yaml as reloaded by
        app_ref.reload_models(), in case another tab or an external edit changed it."""
        self.models = self.app_ref.models
        self._populate_table()

    # -- status / error helpers --------------------------------------------------

    def _set_status(self, text: str) -> None:
        self.query_one("#status-message", Static).update(text)

    def _set_form_error(self, text: str) -> None:
        self.query_one("#form-error", Static).update(text)

    def _show_form(self) -> None:
        self.query_one("#preview-area").display = False
        self.query_one("#import-review").display = False
        self.query_one("#edit-form").display = True

    def _hide_form(self) -> None:
        self.query_one("#edit-form").display = False

    def _show_preview(self) -> None:
        self.query_one("#edit-form").display = False
        self.query_one("#import-review").display = False
        self.query_one("#preview-area").display = True

    def _hide_preview(self) -> None:
        self.query_one("#preview-area").display = False

    def _show_import_review(self) -> None:
        self.query_one("#edit-form").display = False
        self.query_one("#preview-area").display = False
        self.query_one("#import-review").display = True

    def _hide_import_review(self) -> None:
        self.query_one("#import-review").display = False

    def _toggle_engine_fields(self, engine: str) -> None:
        is_llama = engine == "llama-cpp"
        self.query_one("#f-llamacpp-fields").display = is_llama
        self.query_one("#f-unmanaged-fields").display = not is_llama

    # -- form open/populate --------------------------------------------------

    def _open_form_for_add(self) -> None:
        self._editing_id = None
        self.query_one("#form-title", Static).update("Add model")
        self.query_one("#f-id", Input).value = ""
        self.query_one("#f-id", Input).disabled = False
        engine_select = self.query_one("#f-engine", Select)
        engine_select.value = "llama-cpp"
        self._toggle_engine_fields("llama-cpp")
        self.query_one("#f-repo-id", Input).value = ""
        self.query_one("#f-quant-file", Input).value = ""
        self.query_one("#f-mmproj-file", Input).value = ""
        gpus = self.host_profile["gpus"]
        if gpus:
            self.query_one("#f-gpu", Select).value = gpus[0]["id"]
            self._refresh_backend_options(gpus[0]["id"])
        self.query_one("#f-args", TextArea).text = ""
        self.query_one("#f-cmd", TextArea).text = ""
        self.query_one("#f-env", TextArea).text = ""
        self.query_one("#f-ttl", Input).value = "0"
        self.query_one("#f-group", Input).value = ""
        self._set_form_error("")
        self._show_form()

    def _on_edit_pressed(self) -> None:
        model_id = self._selected_model_id()
        if model_id is None:
            self._set_status("select a model row first")
            return
        model = next((m for m in self.models["models"] if m["id"] == model_id), None)
        if model is None:
            self._set_status(f"model {model_id!r} not found — list may be stale, press r to refresh")
            return
        self._editing_id = model_id
        self.query_one("#form-title", Static).update(f"Edit model: {model_id}")
        self.query_one("#f-id", Input).value = model["id"]
        engine = model["engine"]
        self.query_one("#f-engine", Select).value = engine
        self._toggle_engine_fields(engine)
        self.query_one("#f-group", Input).value = model.get("group", "")
        self.query_one("#f-ttl", Input).value = str(model.get("ttl", 0))
        self.query_one("#f-env", TextArea).text = "\n".join(model.get("env", []))
        if engine == "llama-cpp":
            self.query_one("#f-repo-id", Input).value = model.get("repo_id", "")
            self.query_one("#f-quant-file", Input).value = model.get("quant_file", "")
            self.query_one("#f-mmproj-file", Input).value = model.get("mmproj_file", "")
            gpu = model.get("bind", {}).get("gpu", "")
            backend = model.get("bind", {}).get("backend", "")
            self.query_one("#f-gpu", Select).value = gpu
            self._refresh_backend_options(gpu, selected=backend)
            self.query_one("#f-args", TextArea).text = "\n".join(model.get("llama_server_args", []))
            self.query_one("#f-cmd", TextArea).text = ""
        else:
            self.query_one("#f-repo-id", Input).value = ""
            self.query_one("#f-quant-file", Input).value = ""
            self.query_one("#f-mmproj-file", Input).value = ""
            self.query_one("#f-cmd", TextArea).text = model.get("cmd", "")
        self._set_form_error("")
        self._show_form()

    # -- build model dict from form fields --------------------------------------

    def _build_model_from_form(self) -> tuple[dict | None, str | None]:
        model_id = self.query_one("#f-id", Input).value.strip()
        if not model_id:
            return None, "id is required"
        engine = self.query_one("#f-engine", Select).value
        if engine is Select.BLANK:
            return None, "engine is required"

        ttl_raw = self.query_one("#f-ttl", Input).value.strip()
        try:
            ttl = int(ttl_raw) if ttl_raw else 0
        except ValueError:
            return None, "ttl must be an integer"
        if ttl < 0:
            return None, "ttl must be >= 0"

        group = self.query_one("#f-group", Input).value.strip()
        env_lines = [line.strip() for line in self.query_one("#f-env", TextArea).text.splitlines() if line.strip()]

        model: dict = {"id": model_id, "engine": engine}

        if engine == "llama-cpp":
            repo_id = self.query_one("#f-repo-id", Input).value.strip()
            quant_file = self.query_one("#f-quant-file", Input).value.strip()
            mmproj_file = self.query_one("#f-mmproj-file", Input).value.strip()
            gpu = self.query_one("#f-gpu", Select).value
            backend = self.query_one("#f-backend", Select).value
            if not repo_id or not quant_file:
                return None, "repo_id and quant_file are required for llama-cpp models"
            if gpu is Select.BLANK or backend is Select.BLANK:
                return None, "bind.gpu and bind.backend are required for llama-cpp models"
            model["repo_id"] = repo_id
            model["quant_file"] = quant_file
            if mmproj_file:
                model["mmproj_file"] = mmproj_file
            model["bind"] = {"gpu": gpu, "backend": backend}
            args_lines = [line for line in self.query_one("#f-args", TextArea).text.splitlines() if line.strip() != ""]
            if args_lines:
                model["llama_server_args"] = args_lines
        else:
            cmd = self.query_one("#f-cmd", TextArea).text
            if not cmd.strip():
                return None, "cmd is required for unmanaged models"
            model["cmd"] = cmd

        if env_lines:
            model["env"] = env_lines
        model["ttl"] = ttl
        if group:
            model["group"] = group
        return model, None

    # -- validation (authoritative: reuses schema.load_models) -------------------

    def _validate_candidate(self, candidate: dict) -> tuple[bool, str, dict | None]:
        """Run schema.validate_models_dict directly in memory against candidate dict."""
        try:
            validated = schema.validate_models_dict(candidate, self.host_profile, self.manifest, source="models.yaml")
            return True, "", validated
        except schema.ValidationError as e:
            return False, str(e), None

    def _write_models_yaml(self, data: dict) -> None:
        target = self.repo_root / "models.yaml"
        content = yaml.safe_dump(data, sort_keys=False)
        target.write_text(content, encoding="utf-8")

    def _after_write(self, action_past_tense: str) -> None:
        self.app_ref.reload_models()
        self.models = self.app_ref.models
        self._populate_table()
        self._set_status(f"{action_past_tense} — models.yaml written")

    # -- save / delete ------------------------------------------------------------

    def _on_save_pressed(self) -> None:
        self._confirm_and_save()

    @work
    async def _confirm_and_save(self) -> None:
        model, error = self._build_model_from_form()
        if error:
            self._set_form_error(error)
            return

        others = [m for m in self.models["models"] if m["id"] != self._editing_id] if self._editing_id else list(self.models["models"])
        if any(m["id"] == model["id"] for m in others):
            self._set_form_error(f"duplicate model id {model['id']!r}")
            return
        candidate = {"models": others + [model]}

        ok, err, validated = self._validate_candidate(candidate)
        if not ok:
            self._set_form_error(err)
            return

        verb = "Save changes to" if self._editing_id else "Add"
        message = f"{verb} model {model['id']!r} in models.yaml?"
        confirmed = await self.app.push_screen_wait(ConfirmModal(message, confirm_label="Save", danger=True))
        if not confirmed:
            return

        self._write_models_yaml(validated)
        self._after_write(f"saved model {model['id']!r}")
        self._hide_form()

    @work
    async def _delete_selected(self) -> None:
        model_id = self._selected_model_id()
        if model_id is None:
            self._set_status("select a model row first")
            return
        message = f"Delete model {model_id!r} from models.yaml?"
        confirmed = await self.app.push_screen_wait(ConfirmModal(message, confirm_label="Delete", danger=True))
        if not confirmed:
            return

        new_list = [m for m in self.models["models"] if m["id"] != model_id]
        candidate = {"models": new_list}
        ok, err, validated = self._validate_candidate(candidate)
        if not ok:
            self._set_status(f"delete blocked by validation: {err}")
            return

        self._write_models_yaml(validated)
        self._after_write(f"deleted model {model_id!r}")

    # -- import from config.yaml ------------------------------------------------------------

    @work
    async def _open_import_modal(self) -> None:
        text = await self.app.push_screen_wait(ConfigPasteModal())
        if text is None or not text.strip():
            return
        try:
            proposed = swap.parse_config_for_import(text)
        except ValueError as e:
            self._set_status(f"import failed: {e}")
            return
        if not proposed:
            self._set_status("no models found in that config.yaml")
            return
        self._import_candidates = {m["id"]: m for m in proposed}
        self._import_selected = set()
        self._refresh_import_table()
        self._show_import_review()

    def _refresh_import_table(self) -> None:
        table = self.query_one("#import-table", DataTable)
        table.clear()
        for model_id, model in self._import_candidates.items():
            cmd_preview = model["cmd"][:80] + ("…" if len(model["cmd"]) > 80 else "")
            table.add_row(
                selection_marker(model_id in self._import_selected),
                model_id,
                model["engine"],
                cmd_preview,
                key=model_id,
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "import-table" or event.row_key is None or event.row_key.value is None:
            return
        model_id = event.row_key.value
        if model_id in self._import_selected:
            self._import_selected.discard(model_id)
        else:
            self._import_selected.add(model_id)
        self._refresh_import_table()

    @work
    async def _confirm_and_import_selected(self) -> None:
        error_widget = self.query_one("#import-error", Static)
        error_widget.update("")
        if not self._import_selected:
            error_widget.update("tick at least one row to import")
            return

        existing_ids = {m["id"] for m in self.models.get("models", [])}
        colliding = sorted(self._import_selected & existing_ids)
        if colliding:
            error_widget.update(f"id(s) already exist in models.yaml, deselect or remove first: {', '.join(colliding)}")
            return

        to_import = [self._import_candidates[mid] for mid in sorted(self._import_selected)]
        candidate = {"models": list(self.models.get("models", [])) + to_import}
        ok, err, validated = self._validate_candidate(candidate)
        if not ok:
            error_widget.update(err)
            return

        confirmed = await self.app.push_screen_wait(
            ConfirmModal(
                f"Import {len(to_import)} model(s) into models.yaml?\n{', '.join(m['id'] for m in to_import)}",
                confirm_label="Import",
                danger=True,
            )
        )
        if not confirmed:
            return

        self._write_models_yaml(validated)
        self._after_write(f"imported {len(to_import)} model(s)")
        self._import_candidates = {}
        self._import_selected = set()
        self._hide_import_review()

    # -- preview + apply ------------------------------------------------------------

    def _on_preview_pressed(self) -> None:
        try:
            text = swap._generate_config(self.host_profile, self.models)
        except Exception as e:
            self._set_status(f"failed to generate config preview: {e}")
            return
        self.query_one("#preview-text", TextArea).text = text
        self._show_preview()

    @work
    async def _confirm_and_apply(self) -> None:
        message = "Deploy llama-swap service and apply configuration to systemd?"
        confirmed = await self.app.push_screen_wait(
            ConfirmModal(message, confirm_label="Deploy", danger=True)
        )
        if not confirmed:
            return
        self._set_status("deploying configuration...")
        self._apply_in_background()

    @work(thread=True)
    def _apply_in_background(self) -> None:
        try:
            swap.run(self.host_profile, self.manifest, self.models, self.runner, self.repo_root)
        except SystemExit as e:
            self.app.call_from_thread(self._set_status, f"deploy failed: {e.code}")
            return
        except Exception as e:
            self.app.call_from_thread(self._set_status, f"deploy failed: {e}")
            return
        self.app.call_from_thread(self._set_status, "deployed configuration and restarted llama-swap")

    # -- button dispatch ------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-add":
            self._open_form_for_add()
        elif bid == "btn-edit":
            self._on_edit_pressed()
        elif bid == "btn-delete":
            self._delete_selected()
        elif bid == "btn-preview":
            self._on_preview_pressed()
        elif bid == "btn-save":
            self._on_save_pressed()
        elif bid == "btn-cancel":
            self._hide_form()
        elif bid == "btn-confirm-apply":
            self._confirm_and_apply()
        elif bid == "btn-close-preview":
            self._hide_preview()
        elif bid == "btn-import":
            self._open_import_modal()
        elif bid == "btn-import-selected":
            self._confirm_and_import_selected()
        elif bid == "btn-import-close":
            self._import_candidates = {}
            self._import_selected = set()
            self._hide_import_review()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "f-engine":
            self._toggle_engine_fields(event.value)
        elif event.select.id == "f-gpu":
            self._refresh_backend_options(event.value)
