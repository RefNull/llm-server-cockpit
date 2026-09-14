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
from textual.widgets import Button, DataTable, Input, Label, Select, Static, TextArea

from cockpit.widgets import (
    CockpitScreenBase,
    ConfirmModal,
    InfoModal,
    SingleClickDataTable,
    TableAction,
    selection_marker,
)
from provision import schema
from provision.common import Runner
from provision.steps import swap

_ENGINE_OPTIONS = [("llama-cpp", "llama-cpp"), ("unmanaged", "unmanaged")]


class ConfigPasteModal(ModalScreen[str | None]):
    """Paste-a-llama-swap-config.yaml modal for importing models."""

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
        padding: $space-normal $space-section;
    }
    #paste-title {
        text-style: bold;
        color: $accent;
        margin-bottom: $space-normal;
    }
    #paste-text {
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="paste-dialog"):
            yield Static("Paste a llama-swap config.yaml below", id="paste-title")
            yield TextArea(id="paste-text")
            with Horizontal(classes="action-row-primary"):
                yield Button("Parse", id="btn-parse", variant="primary", classes="thin-button")
                yield Button("Cancel", id="btn-paste-cancel", classes="thin-button")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-parse":
            self.dismiss(self.query_one("#paste-text", TextArea).text)
        elif event.button.id == "btn-paste-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class EditModelModal(ModalScreen[bool]):
    """Modal dialog for adding or editing a model in models.yaml.

    Validates candidate configuration against schema.validate_models_dict before writing.
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    EditModelModal {
        align: center middle;
    }
    #edit-model-dialog {
        width: 80;
        height: 85%;
        border: thick $background 80%;
        background: $surface;
        padding: $space-normal $space-section;
    }
    #edit-model-title {
        text-style: bold;
        color: $accent;
        margin-bottom: $space-normal;
    }
    #edit-model-scroll {
        height: 1fr;
    }
    #edit-model-scroll Label {
        margin-top: $space-normal;
        color: $text-muted;
    }
    #f-args, #f-cmd, #f-env {
        height: 5;
    }
    #form-error {
        color: $error;
        margin-top: $space-normal;
    }
    """

    def __init__(
        self,
        host_profile: dict,
        manifest: dict,
        models: dict,
        editing_id: str | None,
        repo_root: Path,
        app_ref: Any,
    ) -> None:
        super().__init__()
        self.host_profile = host_profile
        self.manifest = manifest
        self.models = models
        self.editing_id = editing_id
        self.repo_root = repo_root
        self.app_ref = app_ref
        self._model = (
            next((m for m in self.models.get("models", []) if m["id"] == editing_id), None)
            if editing_id
            else None
        )

    def compose(self) -> ComposeResult:
        title = f"Edit Model: {self.editing_id}" if self.editing_id else "Add Model"
        m = self._model
        engine_val = m.get("engine", "llama-cpp") if m else "llama-cpp"

        with Vertical(id="edit-model-dialog"):
            yield Static(title, id="edit-model-title")
            with VerticalScroll(id="edit-model-scroll"):
                yield Label("id")
                yield Input(
                    id="f-id",
                    placeholder="model id",
                    value=m["id"] if m else "",
                    disabled=bool(self.editing_id),
                )
                yield Label("engine")
                yield Select(_ENGINE_OPTIONS, id="f-engine", allow_blank=False, value=engine_val)

                with Vertical(id="f-llamacpp-fields"):
                    yield Label("repo_id")
                    yield Input(id="f-repo-id", placeholder="huggingface repo id", value=m.get("repo_id", "") if m else "")
                    yield Label("quant_file")
                    yield Input(id="f-quant-file", placeholder="quant filename", value=m.get("quant_file", "") if m else "")
                    yield Label("mmproj_file (optional)")
                    yield Input(id="f-mmproj-file", placeholder="", value=m.get("mmproj_file", "") if m else "")
                    yield Label("bind.gpu")
                    yield Select(self._gpu_options(), id="f-gpu", allow_blank=False)
                    yield Label("bind.backend")
                    yield Select([], id="f-backend", allow_blank=True)
                    yield Label("llama_server_args (one flag/value per line)")
                    yield TextArea(
                        "\n".join(m.get("llama_server_args", [])) if m else "",
                        id="f-args",
                    )

                with Vertical(id="f-unmanaged-fields"):
                    yield Label("cmd (raw shell command, ${PORT} available)")
                    yield TextArea(m.get("cmd", "") if m else "", id="f-cmd")

                yield Label("env (one KEY=VALUE per line)")
                yield TextArea("\n".join(m.get("env", [])) if m else "", id="f-env")
                yield Label("ttl (seconds, 0 = never evict)")
                yield Input(id="f-ttl", value=str(m.get("ttl", 0)) if m else "0")
                yield Label("group (optional)")
                yield Input(id="f-group", placeholder="", value=m.get("group", "") if m else "")

                yield Static("", id="form-error", classes="error-text")

            with Horizontal(classes="action-row-primary"):
                yield Button("Save", id="btn-save", variant="primary", classes="thin-button")
                yield Button("Cancel", id="btn-cancel", classes="thin-button")

    def on_mount(self) -> None:
        m = self._model
        engine_val = m.get("engine", "llama-cpp") if m else "llama-cpp"
        self._toggle_engine_fields(engine_val)

        gpus = self.host_profile.get("gpus", [])
        if gpus:
            selected_gpu = m.get("bind", {}).get("gpu") if m else gpus[0]["id"]
            if any(g["id"] == selected_gpu for g in gpus):
                self.query_one("#f-gpu", Select).value = selected_gpu
            else:
                self.query_one("#f-gpu", Select).value = gpus[0]["id"]
            selected_backend = m.get("bind", {}).get("backend") if m else None
            self._refresh_backend_options(str(self.query_one("#f-gpu", Select).value), selected=selected_backend)

    def _gpu_options(self) -> list[tuple[str, str]]:
        return [(g["id"], g["id"]) for g in self.host_profile.get("gpus", [])]

    def _backend_options_for_gpu(self, gpu_id: str) -> list[tuple[str, str]]:
        for g in self.host_profile.get("gpus", []):
            if g["id"] == gpu_id:
                return [(b, b) for b in g.get("backends", [])]
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

    def _toggle_engine_fields(self, engine: str) -> None:
        is_llama = engine == "llama-cpp"
        self.query_one("#f-llamacpp-fields").display = is_llama
        self.query_one("#f-unmanaged-fields").display = not is_llama

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "f-engine":
            self._toggle_engine_fields(str(event.value))
        elif event.select.id == "f-gpu":
            self._refresh_backend_options(str(event.value))

    def _set_form_error(self, text: str) -> None:
        self.query_one("#form-error", Static).update(text)

    def action_cancel(self) -> None:
        self.dismiss(False)

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

        model: dict[str, Any] = {"id": model_id, "engine": str(engine)}

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
            model["bind"] = {"gpu": str(gpu), "backend": str(backend)}
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

    @work
    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(False)
            return
        if event.button.id != "btn-save":
            return

        model, error = self._build_model_from_form()
        if error:
            self._set_form_error(error)
            return

        existing_models = self.models.get("models", [])
        others = [m for m in existing_models if m["id"] != self.editing_id] if self.editing_id else list(existing_models)
        if any(m["id"] == model["id"] for m in others):
            self._set_form_error(f"duplicate model id {model['id']!r}")
            return
        candidate = {"models": others + [model]}

        try:
            validated = schema.validate_models_dict(candidate, self.host_profile, self.manifest, source="models.yaml")
        except schema.ValidationError as e:
            self._set_form_error(str(e))
            return

        verb = "Save changes to" if self.editing_id else "Add"
        confirmed = await self.app.push_screen_wait(
            ConfirmModal(f"{verb} model {model['id']!r} in models.yaml?", confirm_label="Save", danger=True)
        )
        if not confirmed:
            return

        target = self.repo_root / "models.yaml"
        target.write_text(yaml.safe_dump(validated, sort_keys=False), encoding="utf-8")
        self.app_ref.reload_models()
        self.app.notify(f"saved model {model['id']!r}")
        self.dismiss(True)


class ImportModelsModal(ModalScreen[bool]):
    """Modal dialog for importing discovered or pasted models into models.yaml."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    ImportModelsModal {
        align: center middle;
    }
    #import-dialog {
        width: 85;
        height: 85%;
        border: thick $background 80%;
        background: $surface;
        padding: $space-normal $space-section;
    }
    #import-title {
        text-style: bold;
        color: $accent;
        margin-bottom: $space-normal;
    }
    #import-scroll {
        height: 1fr;
    }
    #import-error {
        color: $error;
        margin-top: $space-normal;
    }
    """

    def __init__(
        self,
        host_profile: dict,
        manifest: dict,
        models: dict,
        repo_root: Path,
        app_ref: Any,
    ) -> None:
        super().__init__()
        self.host_profile = host_profile
        self.manifest = manifest
        self.models = models
        self.repo_root = repo_root
        self.app_ref = app_ref
        self._import_candidates: dict[str, dict[str, Any]] = {}
        self._import_selected: set[str] = set()

    def compose(self) -> ComposeResult:
        with Vertical(id="import-dialog"):
            yield Static("Import Models from Llama-Swap", id="import-title")
            with VerticalScroll(id="import-scroll"):
                table = SingleClickDataTable(
                    id="import-table", zebra_stripes=True, classes="data-table", fixed_columns=2
                )
                table.cursor_type = "row"
                yield table
                yield Static("", id="import-error", classes="error-text")
            with Horizontal(classes="action-row-primary"):
                yield Button("Import selected", id="btn-import-selected", variant="primary", classes="thin-button")
                yield Button("Paste config.yaml", id="btn-import-paste", classes="thin-button")
                yield Button("Cancel", id="btn-import-close", classes="thin-button")

    def on_mount(self) -> None:
        table = self.query_one("#import-table", SingleClickDataTable)
        table.add_column("", width=3)
        table.add_column("ID", width=24)
        table.add_column("Engine", width=12)
        table.add_column("cmd", width=60)

        # Discover from state_dir config.yaml if present
        state_dir = self.host_profile.get("paths", {}).get("state_dir")
        if state_dir:
            config_path = Path(state_dir) / "llama-swap" / "config.yaml"
            if config_path.exists():
                try:
                    content = config_path.read_text(encoding="utf-8")
                    proposed = swap.parse_config_for_import(content)
                    self._import_candidates = {m["id"]: m for m in proposed}
                except Exception:
                    pass
        self._refresh_import_table()

    def _refresh_import_table(self) -> None:
        table = self.query_one("#import-table", SingleClickDataTable)
        table.clear()
        for model_id, model in self._import_candidates.items():
            cmd_preview = model.get("cmd", "")[:80] + ("…" if len(model.get("cmd", "")) > 80 else "")
            table.add_row(
                selection_marker(model_id in self._import_selected),
                model_id,
                model.get("engine", "unmanaged"),
                cmd_preview,
                key=model_id,
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "import-table" or event.row_key is None or event.row_key.value is None:
            return
        model_id = str(event.row_key.value)
        if model_id in self._import_selected:
            self._import_selected.discard(model_id)
        else:
            self._import_selected.add(model_id)
        self._refresh_import_table()

    def action_cancel(self) -> None:
        self.dismiss(False)

    @work
    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-import-close":
            self.dismiss(False)
        elif bid == "btn-import-paste":
            await self._paste_config()
        elif bid == "btn-import-selected":
            await self._confirm_and_import()

    async def _paste_config(self) -> None:
        text = await self.app.push_screen_wait(ConfigPasteModal())
        if text is None or not text.strip():
            return
        try:
            proposed = swap.parse_config_for_import(text)
        except ValueError as e:
            self.query_one("#import-error", Static).update(f"parse failed: {e}")
            return
        if not proposed:
            self.query_one("#import-error", Static).update("no models found in pasted config.yaml")
            return
        self.query_one("#import-error", Static).update("")
        self._import_candidates = {m["id"]: m for m in proposed}
        self._import_selected = set()
        self._refresh_import_table()

    async def _confirm_and_import(self) -> None:
        error_widget = self.query_one("#import-error", Static)
        error_widget.update("")
        if not self._import_selected:
            error_widget.update("tick at least one row to import")
            return

        existing_ids = {m["id"] for m in self.models.get("models", [])}
        colliding = sorted(self._import_selected & existing_ids)
        if colliding:
            error_widget.update(f"id(s) already exist in models.yaml: {', '.join(colliding)}")
            return

        to_import = [self._import_candidates[mid] for mid in sorted(self._import_selected)]
        candidate = {"models": list(self.models.get("models", [])) + to_import}
        try:
            validated = schema.validate_models_dict(candidate, self.host_profile, self.manifest, source="models.yaml")
        except schema.ValidationError as e:
            error_widget.update(str(e))
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

        target = self.repo_root / "models.yaml"
        target.write_text(yaml.safe_dump(validated, sort_keys=False), encoding="utf-8")
        self.app_ref.reload_models()
        self.app.notify(f"imported {len(to_import)} model(s)")
        self.dismiss(True)


class DeployScreen(CockpitScreenBase):
    """Cockpit "Deploy" tab conforming to Archetype B (Table-Driven Inventory)."""

    DEFAULT_CSS = """
    DeployScreen {
        height: 1fr;
    }
    /* Not a Button — a plain rule character marking off "Apply & Restart" (the consequential
       action) from the rest of the row, without touching any Button's own geometry/margin
       (DESIGN.md §9 forbids per-screen Button CSS overrides). */
    DeployScreen .action-row-divider {
        width: 3;
        color: $text-muted;
        text-align: center;
    }
    """

    def __init__(
        self,
        host_profile: dict,
        manifest: dict,
        models: dict,
        runner: Runner,
        repo_root: Path,
        app_ref: Any,
    ) -> None:
        super().__init__()
        self.host_profile = host_profile
        self.manifest = manifest
        self.models = models
        self.runner = runner
        self.repo_root = repo_root
        self.app_ref = app_ref

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            table = SingleClickDataTable(id="models-table", classes="data-table", fixed_columns=1)
            yield table
            with Horizontal(classes="action-row-primary"):
                yield Button("Apply & Restart Service", id="btn-apply", variant="primary", classes="thin-button")
                yield Static("│", classes="action-row-divider")
                yield Button("Add Model", id="btn-add-model", classes="thin-button")
                yield Button("Import from Llama-Swap", id="btn-import-toggle", classes="thin-button")
                yield Button("Preview YAML", id="btn-preview-yaml", classes="thin-button")
            yield Static("", id="status-message", classes="status-text")

    def on_mount(self) -> None:
        table = self.query_one("#models-table", SingleClickDataTable)
        table.cursor_type = "row"
        table.add_column("ID", width=24)
        table.add_column("Engine", width=12)
        table.add_column("GPU", width=10)
        table.add_column("Backend", width=10)
        table.add_column("Group", width=12)
        table.add_column("TTL", width=8)
        table.add_action_column(TableAction("edit", "Edit"))
        table.add_action_column(
            TableAction("delete", "Delete", destructive=True, confirm="Delete model {row} from models.yaml?")
        )
        self._populate_table()

    def _populate_table(self) -> None:
        table = self.query_one("#models-table", SingleClickDataTable)
        table.clear()
        for m in self.models.get("models", []):
            if m["engine"] == "llama-cpp":
                gpu = m.get("bind", {}).get("gpu", "")
                backend = m.get("bind", {}).get("backend", "")
            else:
                gpu, backend = "", ""
            table.add_row(
                m["id"],
                m["engine"],
                gpu,
                backend,
                m.get("group", ""),
                str(m.get("ttl", "")),
                *table.action_cells(m["id"]),
                key=m["id"],
            )

    def on_refresh_requested(self) -> None:
        """Called by CockpitApp.action_refresh_all — pick up fresh models.yaml."""
        self.models = self.app_ref.models
        self._populate_table()

    def _set_status(self, text: str) -> None:
        self.query_one("#status-message", Static).update(text)

    # -- Actions ---------------------------------------------------------------

    @work
    async def _on_add_model(self) -> None:
        saved = await self.app.push_screen_wait(
            EditModelModal(
                host_profile=self.host_profile,
                manifest=self.manifest,
                models=self.models,
                editing_id=None,
                repo_root=self.repo_root,
                app_ref=self.app_ref,
            )
        )
        if saved:
            self.models = self.app_ref.models
            self._populate_table()
            self._set_status("model added — models.yaml written")

    async def _edit_model(self, model_id: str) -> None:
        saved = await self.app.push_screen_wait(
            EditModelModal(
                host_profile=self.host_profile,
                manifest=self.manifest,
                models=self.models,
                editing_id=model_id,
                repo_root=self.repo_root,
                app_ref=self.app_ref,
            )
        )
        if saved:
            self.models = self.app_ref.models
            self._populate_table()
            self._set_status(f"model {model_id!r} updated — models.yaml written")

    async def _delete_model(self, model_id: str) -> None:
        # Already confirmed: this is a TableAction(destructive=True), so CockpitScreenBase's
        # _on_table_action_invoked has already run the ConfirmModal before calling here.
        new_list = [m for m in self.models.get("models", []) if m["id"] != model_id]
        candidate = {"models": new_list}
        try:
            validated = schema.validate_models_dict(candidate, self.host_profile, self.manifest, source="models.yaml")
        except schema.ValidationError as e:
            self._set_status(f"delete blocked by validation: {e}")
            return

        target = self.repo_root / "models.yaml"
        target.write_text(yaml.safe_dump(validated, sort_keys=False), encoding="utf-8")
        self.app_ref.reload_models()
        self.models = self.app_ref.models
        self._populate_table()
        self._set_status(f"deleted model {model_id!r} — models.yaml written")
        self.app.notify(f"deleted model {model_id!r}")

    @work
    async def _on_import_toggle(self) -> None:
        imported = await self.app.push_screen_wait(
            ImportModelsModal(
                host_profile=self.host_profile,
                manifest=self.manifest,
                models=self.models,
                repo_root=self.repo_root,
                app_ref=self.app_ref,
            )
        )
        if imported:
            self.models = self.app_ref.models
            self._populate_table()
            self._set_status("imported models — models.yaml written")

    def _on_preview_yaml(self) -> None:
        try:
            text = swap._generate_config(self.host_profile, self.models)
        except Exception as e:
            self._set_status(f"failed to generate config preview: {e}")
            return
        self.app.push_screen(InfoModal("Generated config.yaml preview", text))

    @work
    async def _confirm_and_apply(self) -> None:
        # DESIGN.md §5 tier 2: installs units and restarts llama-swap.
        confirmed = await self.confirm(
            "Deploy llama-swap service and apply configuration to systemd?",
            confirm_label="Deploy",
            mutates_system=True,
        )
        if not confirmed:
            return
        self._set_status("deploying configuration...")
        self._apply_in_background()

    @work(thread=True)
    def _apply_in_background(self) -> None:
        # DESIGN.md §6: toasts on both success and failure for background operations.
        try:
            swap.run(self.host_profile, self.manifest, self.models, self.runner, self.repo_root)
        except SystemExit as e:
            msg = f"deploy failed: {e.code}"
        except Exception as e:
            msg = f"deploy failed: {e}"
        else:
            msg = "deployed configuration and restarted llama-swap"
            self.app.call_from_thread(self._set_status, msg)
            self.app.call_from_thread(self.app.notify, msg)
            return
        self.app.call_from_thread(self._set_status, msg)
        self.app.call_from_thread(self.app.notify, msg, severity="error")

    # -- Per-row table actions --------------------------------------------------

    async def handle_table_action(self, action_id: str, row_key: str, table) -> None:
        if action_id == "edit":
            await self._edit_model(row_key)
        elif action_id == "delete":
            await self._delete_model(row_key)

    # -- Button Dispatch -------------------------------------------------------

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-apply":
            await self._confirm_and_apply()
        elif bid == "btn-add-model":
            await self._on_add_model()
        elif bid == "btn-import-toggle":
            await self._on_import_toggle()
        elif bid == "btn-preview-yaml":
            self._on_preview_yaml()
