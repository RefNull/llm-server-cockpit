"""Cockpit "Downloads" tab: view + confirm layer over provision.steps.hf — no parallel
download/auth logic, just a table of llama-cpp models, their HF download status, and
buttons that call the same hf.py functions the CLI uses.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

import yaml
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Static

from cockpit.widgets import CockpitScreenBase, SingleClickDataTable, selection_marker
from provision import schema
from provision.common import Runner
from provision.steps import hf

STATUS_ICONS = {
    "downloaded": "✅ downloaded",
    "missing": "⬜ missing",
    "placeholder": "⚠️  PLACEHOLDER — edit models.yaml",
}


class HFTokenModal(ModalScreen[str | None]):
    """Lightweight modal to enter a Hugging Face token on-demand."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    HFTokenModal {
        align: center middle;
    }
    #hf-token-dialog {
        width: 60;
        height: auto;
        border: thick $background 80%;
        background: $surface;
        padding: 1 2;
    }
    #hf-token-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #hf-token-msg {
        color: $text-muted;
        margin-bottom: 1;
    }
    #hf-token-input {
        margin-bottom: 1;
    }
    """

    def __init__(self, token_env: str) -> None:
        super().__init__()
        self.token_env = token_env

    def compose(self) -> ComposeResult:
        with Vertical(id="hf-token-dialog"):
            yield Static("Hugging Face Authentication", id="hf-token-title")
            yield Static(
                f"Export {self.token_env} or enter your token below for this session:",
                id="hf-token-msg",
            )
            yield Input(placeholder="hf_...", password=True, id="hf-token-input")
            with Horizontal(classes="button-row"):
                yield Button("Continue", id="btn-token-continue", variant="primary", classes="thin-button")
                yield Button("Cancel", id="btn-token-cancel", classes="thin-button")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-token-continue":
            val = self.query_one("#hf-token-input", Input).value.strip()
            self.dismiss(val if val else None)
        elif event.button.id == "btn-token-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class DownloadsScreen(CockpitScreenBase):
    """Not a Textual Screen — mounted inside a TabPane by cockpit/app.py."""

    DEFAULT_CSS = """
    DownloadsScreen {
        height: 1fr;
        padding: 0;
    }
    #disk-usage {
        margin-top: 1;
        color: $text-muted;
    }
    #download-status {
        margin-top: 1;
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
        # id -> model dict, for the llama-cpp models currently shown in the table.
        self._downloadable: dict[str, dict[str, Any]] = {}
        self._selected: set[str] = set()
        self._downloading = False

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            with Horizontal(classes="inline-row"):
                yield Input(
                    placeholder="Hugging Face repo/model or GGUF filename...",
                    id="adhoc-model-input",
                )
                yield Button("Download", id="adhoc-download-btn", variant="primary", classes="thin-button")

            # fixed_columns=2: column 0 is the tick marker, so keeping the model ID visible
            # while repo_id/quant_file/status scroll horizontally takes both (DESIGN.md §4.2).
            table = SingleClickDataTable(
                id="model-table", zebra_stripes=True, classes="data-table", fixed_columns=2
            )
            table.cursor_type = "row"
            yield table

            with Horizontal(classes="action-row-primary"):
                yield Button("Download selected", id="btn-download-selected", variant="primary", classes="thin-button")
                yield Button("Download all", id="btn-download-all", variant="error", classes="thin-button")

            with Horizontal(classes="action-row-secondary"):
                yield Static("", id="disk-usage")
                yield Label("", id="download-status", classes="status-text")

    def on_mount(self) -> None:
        table = self.query_one("#model-table", SingleClickDataTable)
        table.add_column("", width=3)
        table.add_column("ID", width=24)
        table.add_column("Repo ID", width=36)
        table.add_column("Quant File", width=26)
        table.add_column("Status", width=32)
        self._refresh_table()
        self._refresh_disk_usage()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _refresh_table(self) -> None:
        table = self.query_one("#model-table", SingleClickDataTable)
        table.clear()
        self._downloadable.clear()
        for model in self.models.get("models", []):
            if model.get("engine") != "llama-cpp":
                continue
            self._downloadable[model["id"]] = model
            status = hf.model_status(model, self.host_profile)
            repo_id = model["repo_id"]
            repo_display = "PLACEHOLDER — edit models.yaml" if status == "placeholder" else repo_id
            table.add_row(
                selection_marker(model["id"] in self._selected),
                model["id"],
                repo_display,
                model["quant_file"],
                STATUS_ICONS.get(status, status),
                key=model["id"],
            )
        self._selected &= self._downloadable.keys()

        btn_selected = self.query("#btn-download-selected")
        btn_all = self.query("#btn-download-all")

        if not self._downloadable:
            if btn_selected:
                btn_selected.first(Button).disabled = True
            if btn_all:
                btn_all.first(Button).disabled = True
        else:
            if btn_all:
                btn_all.first(Button).disabled = self._downloading
            self._sync_button_state()

    def _refresh_disk_usage(self) -> None:
        widget = self.query_one("#disk-usage", Static)
        models_dir = Path(self.host_profile["paths"]["models_dir"])
        if not models_dir.exists():
            widget.update(f"Disk usage: {models_dir} does not exist yet.")
            return
        total_bytes = sum(f.stat().st_size for f in models_dir.rglob("*") if f.is_file())
        total_gb = total_bytes / (1024 ** 3)
        try:
            free_bytes = shutil.disk_usage(models_dir).free
            free_gb = free_bytes / (1024 ** 3)
            widget.update(f"Disk usage: {total_gb:.1f} GiB in {models_dir}  |  {free_gb:.1f} GiB free")
        except OSError:
            widget.update(f"Disk usage: {total_gb:.1f} GiB in {models_dir}")

    def _sync_button_state(self) -> None:
        """Enable 'Download selected' only when at least one ticked model is still
        downloadable ('missing') and no download is already running."""
        btn = self.query("#btn-download-selected")
        if not btn:
            return
        button = btn.first(Button)
        if self._downloading:
            button.disabled = True
            return
        button.disabled = not any(
            hf.model_status(self._downloadable[model_id], self.host_profile) == "missing"
            for model_id in self._selected
            if model_id in self._downloadable
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "model-table" or event.row_key is None or event.row_key.value is None:
            return
        model_id = event.row_key.value
        if model_id not in self._downloadable:
            return
        if model_id in self._selected:
            self._selected.discard(model_id)
        else:
            self._selected.add(model_id)
        self._refresh_table()

    # ------------------------------------------------------------------
    # Refresh entry point (called by app.py's action_refresh_all)
    # ------------------------------------------------------------------

    def on_refresh_requested(self) -> None:
        self.models = getattr(self.cockpit_app, "models", self.models)
        self._refresh_table()
        self._refresh_disk_usage()

    # ------------------------------------------------------------------
    # Auth on-demand helper
    # ------------------------------------------------------------------

    async def _ensure_hf_auth(self) -> bool:
        token_env = self.host_profile.get("hf", {}).get("token_env", "HF_TOKEN")
        if not os.environ.get(token_env):
            token = await self.app.push_screen_wait(HFTokenModal(token_env))
            if not token:
                self.notify("Hugging Face token required to download weights", severity="warning")
                return False
            os.environ[token_env] = token
        return True

    # ------------------------------------------------------------------
    # Button handling & download actions
    # ------------------------------------------------------------------

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-download-selected":
            await self._on_download_selected()
        elif event.button.id == "btn-download-all":
            await self._on_download_all()
        elif event.button.id == "adhoc-download-btn":
            await self._on_adhoc_download()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "adhoc-model-input":
            await self._on_adhoc_download()

    @staticmethod
    def _parse_adhoc_input(raw: str) -> tuple[str, str] | None:
        raw = raw.strip()
        if raw.startswith("https://huggingface.co/"):
            raw = raw.removeprefix("https://huggingface.co/").strip()
        elif raw.startswith("hf.co/"):
            raw = raw.removeprefix("hf.co/").strip()
        for branch in ("/blob/main/", "/resolve/main/", "/blob/master/", "/resolve/master/"):
            if branch in raw:
                raw = raw.replace(branch, "/")
        if ":" in raw:
            repo_id, _, quant_file = raw.partition(":")
            repo_id, quant_file = repo_id.strip(), quant_file.strip()
            if repo_id and quant_file:
                return repo_id, quant_file
        if " " in raw:
            repo_id, quant_file = raw.split(None, 1)
            repo_id, quant_file = repo_id.strip(), quant_file.strip()
            if repo_id and quant_file:
                return repo_id, quant_file
        parts = [p.strip() for p in raw.split("/") if p.strip()]
        if len(parts) >= 3 and parts[-1].endswith(".gguf"):
            return "/".join(parts[:-1]), parts[-1]
        if len(parts) == 2 and parts[1].endswith(".gguf"):
            return parts[0], parts[1]
        return None

    @work
    async def _on_adhoc_download(self) -> None:
        raw = self.query_one("#adhoc-model-input", Input).value.strip()
        if not raw:
            self.notify("Enter a Hugging Face repo and model file", severity="warning")
            return
        parsed = self._parse_adhoc_input(raw)
        if not parsed:
            self.notify(
                "Could not parse input. Provide repo and filename (e.g. 'repo/model/file.gguf' or 'repo:file.gguf')",
                severity="error",
            )
            return
        repo_id, quant_file = parsed

        if not await self._ensure_hf_auth():
            return

        confirmed = await self.confirm(
            f"Download {quant_file} from {repo_id} and register in models.yaml?",
            confirm_label="Download",
            danger=True,
        )
        if not confirmed:
            return

        # Check if already present in models
        existing = next(
            (
                m for m in self.models.get("models", [])
                if m.get("engine") == "llama-cpp" and m.get("quant_file") == quant_file
            ),
            None,
        )
        if existing:
            target_model = existing
        else:
            stem = Path(quant_file).stem
            clean_id = re.sub(r"[^a-zA-Z0-9_\.\-]", "-", stem).lower().strip("-") or "model"
            candidate_id = clean_id
            existing_ids = {m.get("id") for m in self.models.get("models", [])}
            counter = 2
            while candidate_id in existing_ids:
                candidate_id = f"{clean_id}-{counter}"
                counter += 1

            gpus = self.host_profile.get("gpus", [])
            gpu_id = gpus[0]["id"] if gpus else "gpu-0"
            backend = gpus[0]["backends"][0] if gpus and gpus[0].get("backends") else "cuda"

            target_model = {
                "id": candidate_id,
                "engine": "llama-cpp",
                "repo_id": repo_id,
                "quant_file": quant_file,
                "bind": {"gpu": gpu_id, "backend": backend},
                "ttl": 300,
            }

            models_list = list(self.models.get("models", []))
            models_list.append(target_model)
            candidate_data = {"models": models_list}
            try:
                validated = schema.validate_models_dict(
                    candidate_data, self.host_profile, self.manifest, source="models.yaml"
                )
                models_yaml_path = self.repo_root / "models.yaml"
                models_yaml_path.write_text(yaml.safe_dump(validated, sort_keys=False), encoding="utf-8")
                if hasattr(self.cockpit_app, "reload_models"):
                    self.cockpit_app.reload_models()
                    self.models = self.cockpit_app.models
                else:
                    self.models = candidate_data
            except Exception as e:
                self.notify(f"Failed to register model in models.yaml: {e}", severity="error")
                return

        self.query_one("#adhoc-model-input", Input).value = ""
        self._refresh_table()
        self._run_download_batch([target_model])

    @work
    async def _on_download_selected(self) -> None:
        pending = [
            self._downloadable[model_id]
            for model_id in sorted(self._selected)
            if model_id in self._downloadable
            and hf.model_status(self._downloadable[model_id], self.host_profile) == "missing"
        ]
        if not pending:
            return
        if not await self._ensure_hf_auth():
            return
        names = ", ".join(m["id"] for m in pending)
        confirmed = await self.confirm(
            f"Download {len(pending)} selected model(s)?\n{names}\n"
            "This can take a while and uses bandwidth.",
            confirm_label="Download",
            danger=True,
        )
        if not confirmed:
            return
        self._run_download_batch(pending)

    @work
    async def _on_download_all(self) -> None:
        if not await self._ensure_hf_auth():
            return
        confirmed = await self.confirm(
            "Download ALL non-placeholder models?\n"
            "This provisions/updates the hf venv, logs in, and downloads every model "
            "not marked as a placeholder. Can take a long time and uses significant bandwidth.",
            confirm_label="Download all",
            danger=True,
        )
        if not confirmed:
            return
        self._run_download_all()

    @work(thread=True)
    def _run_download_batch(self, models: list[dict[str, Any]]) -> None:
        self.app.call_from_thread(self._set_downloading, True)
        status_label = self.query_one("#download-status", Label)
        failures: list[str] = []
        try:
            venv_path = Path(self.host_profile["paths"]["state_dir"]) / "venv-hf"
            hf._ensure_venv(venv_path, self.runner)
            hf._ensure_hub_pinned(venv_path, self.manifest["huggingface_hub"]["version"], self.runner)
            models_dir = self.host_profile["paths"]["models_dir"]
            self.runner.mkdir(models_dir)

            for model in models:
                model_id = model["id"]
                self.app.call_from_thread(status_label.update, f"Downloading {model_id}...")
                try:
                    hf.download_model(model, self.host_profile, self.runner)
                except BaseException as exc:  # noqa: BLE001
                    failures.append(model_id)
                    self.app.call_from_thread(
                        self.app.notify, f"{model_id}: download failed — {exc}", severity="error"
                    )
                    continue
                self.app.call_from_thread(self._selected.discard, model_id)
                self.app.call_from_thread(self.app.notify, f"{model_id}: download complete")
        finally:
            if failures:
                status_label_text = f"Download batch finished with {len(failures)} failure(s): {', '.join(failures)}"
            else:
                status_label_text = "Download batch complete."
            self.app.call_from_thread(status_label.update, status_label_text)
            self.app.call_from_thread(self._refresh_table)
            self.app.call_from_thread(self._refresh_disk_usage)
            self.app.call_from_thread(self._set_downloading, False)

    @work(thread=True)
    def _run_download_all(self) -> None:
        self.app.call_from_thread(self._set_downloading, True)
        status_label = self.query_one("#download-status", Label)
        self.app.call_from_thread(status_label.update, "Running full download (venv provision + login + all models)...")
        try:
            hf.run(self.host_profile, self.manifest, self.models, self.runner, self.repo_root)
        except BaseException as exc:  # noqa: BLE001
            message = str(exc) or repr(exc)
            self.app.call_from_thread(status_label.update, f"Download-all stopped: {message}")
            self.app.call_from_thread(self.app.notify, f"Download-all stopped: {message}", severity="error")
            self.app.call_from_thread(self._refresh_table)
            self.app.call_from_thread(self._refresh_disk_usage)
            self.app.call_from_thread(self._set_downloading, False)
            return
        self.app.call_from_thread(status_label.update, "Download-all complete.")
        self.app.call_from_thread(self.app.notify, "Download-all complete")
        self.app.call_from_thread(self._refresh_table)
        self.app.call_from_thread(self._refresh_disk_usage)
        self.app.call_from_thread(self._set_downloading, False)

    def _set_downloading(self, active: bool) -> None:
        self._downloading = active
        all_btn = self.query("#btn-download-all")
        if all_btn:
            all_btn.first(Button).disabled = active or not self._downloadable
        adhoc_btn = self.query("#adhoc-download-btn")
        if adhoc_btn:
            adhoc_btn.first(Button).disabled = active
        self._sync_button_state()
