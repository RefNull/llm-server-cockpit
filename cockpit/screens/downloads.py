"""Cockpit "Downloads" tab: view + confirm layer over provision.steps.hf — no parallel
download/auth logic, just a table of llama-cpp models, their HF download status, and
buttons that call the same hf.py functions the CLI uses.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widget import Widget
from textual.widgets import Button, DataTable, Label, Static

from provision.common import Runner
from provision.steps import hf

from cockpit.widgets import ConfirmModal

STATUS_ICONS = {
    "downloaded": "✅ downloaded",   # ✅
    "missing": "⬜ missing",         # ⬜
    "placeholder": "⚠️  PLACEHOLDER — edit models.yaml",  # ⚠️
}


class DownloadsScreen(Widget):
    """Not a Textual Screen — mounted inside a TabPane by cockpit/app.py.

    Judgment call: per-model download buttons are DISABLED (not just left to fail) for
    "downloaded" and "placeholder" status, matching the project's stated bias toward making
    a bad action structurally impossible to express rather than catching it after the fact.
    The "Download all" button stays enabled regardless — hf.run() already has its own
    skip-placeholder-and-report logic, so there's nothing to pre-empt there.
    """

    # .button-row / .status-text come from cockpit/widgets.py's SHARED_CSS (CockpitApp.CSS).
    # Root content is wrapped in a VerticalScroll (see compose()); the table gets a bounded
    # max-height rather than 1fr, which doesn't have a well-defined meaning inside a
    # content-sized scroll container (matches builds.py's DataTable convention).
    DEFAULT_CSS = """
    DownloadsScreen {
        padding: 0;
    }
    #auth-banner {
        padding: 1 2;
        margin-bottom: 1;
    }
    #auth-banner.ok {
        background: $success 20%;
        color: $success;
    }
    #auth-banner.bad {
        background: $error 20%;
        color: $error;
    }
    #disk-usage {
        margin: 1 0;
    }
    #model-table {
        height: auto;
        max-height: 15;
        margin-bottom: 1;
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

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("", id="auth-banner")
            yield Static("", id="disk-usage")
            table = DataTable(id="model-table", zebra_stripes=True)
            table.cursor_type = "row"
            yield table
            with Horizontal(classes="button-row"):
                yield Button("Download selected", id="download-selected", variant="primary")
                yield Button("Download all", id="download-all", variant="warning")
            yield Label("", id="download-status", classes="status-text")

    def on_mount(self) -> None:
        table = self.query_one("#model-table", DataTable)
        table.add_columns("id", "repo_id", "quant_file", "status")
        self._refresh_auth_banner()
        self._refresh_table()
        self._refresh_disk_usage()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _refresh_auth_banner(self) -> None:
        banner = self.query_one("#auth-banner", Static)
        token_env = self.host_profile["hf"]["token_env"]
        if hf.auth_configured(self.host_profile):
            banner.remove_class("bad")
            banner.add_class("ok")
            banner.update(f"HF auth OK — {token_env} is set.")
        else:
            banner.remove_class("ok")
            banner.add_class("bad")
            banner.update(
                f"HF auth NOT configured — export {token_env} with a Hugging Face token "
                "before downloading anything here (login happens inside 'Download all'; "
                "a per-model download will fail without it too)."
            )

    def _refresh_table(self) -> None:
        table = self.query_one("#model-table", DataTable)
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
                model["id"],
                repo_display,
                model["quant_file"],
                STATUS_ICONS.get(status, status),
                key=model["id"],
            )
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
        """Enable 'Download selected' only when the highlighted row's status is 'missing'."""
        button = self.query_one("#download-selected", Button)
        table = self.query_one("#model-table", DataTable)
        model_id = self._selected_model_id(table)
        if model_id is None:
            button.disabled = True
            return
        model = self._downloadable.get(model_id)
        if model is None:
            button.disabled = True
            return
        status = hf.model_status(model, self.host_profile)
        button.disabled = status != "missing"

    def _selected_model_id(self, table: DataTable) -> str | None:
        if table.row_count == 0:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        return row_key.value if row_key is not None else None

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._sync_button_state()

    # ------------------------------------------------------------------
    # Refresh entry point (called by app.py's action_refresh_all)
    # ------------------------------------------------------------------

    def on_refresh_requested(self) -> None:
        self._refresh_auth_banner()
        self._refresh_table()
        self._refresh_disk_usage()

    # ------------------------------------------------------------------
    # Button handling
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "download-selected":
            self._on_download_selected()
        elif event.button.id == "download-all":
            self._on_download_all()

    def _on_download_selected(self) -> None:
        table = self.query_one("#model-table", DataTable)
        model_id = self._selected_model_id(table)
        if model_id is None:
            return
        model = self._downloadable.get(model_id)
        if model is None:
            return
        status = hf.model_status(model, self.host_profile)
        if status != "missing":
            # Button should already be disabled in this case; belt-and-braces guard.
            return
        self._confirm_and_download_one(model_id, model)

    @work
    async def _confirm_and_download_one(self, model_id: str, model: dict[str, Any]) -> None:
        confirmed = await self.app.push_screen_wait(
            ConfirmModal(
                f"Download {model_id!r} ({model['quant_file']})?\n"
                "This can take a while and uses bandwidth."
            )
        )
        if not confirmed:
            return
        self._run_download_one(model_id, model)

    @work(thread=True)
    def _run_download_one(self, model_id: str, model: dict[str, Any]) -> None:
        status_label = self.query_one("#download-status", Label)
        self.app.call_from_thread(status_label.update, f"Downloading {model_id}...")
        try:
            hf.download_model(model, self.host_profile, self.runner)
        except BaseException as exc:  # noqa: BLE001 — surface any failure instead of a dead thread
            # download_model() itself doesn't call sys.exit() (only run() does — see hf.py),
            # but it shells out via Runner.run(check=True), so a bad repo_id/network failure
            # raises CalledProcessError/OSError. Catch broadly anyway: better an ugly message
            # in the UI than a silently dead worker thread.
            self.app.call_from_thread(
                status_label.update, f"Download failed for {model_id}: {exc}"
            )
            self.app.call_from_thread(self.app.notify, f"{model_id}: download failed — {exc}", severity="error")
            return
        self.app.call_from_thread(status_label.update, f"Downloaded {model_id}.")
        self.app.call_from_thread(self.app.notify, f"{model_id}: download complete")
        self.app.call_from_thread(self._refresh_table)
        self.app.call_from_thread(self._refresh_disk_usage)

    def _on_download_all(self) -> None:
        self._confirm_and_download_all()

    @work
    async def _confirm_and_download_all(self) -> None:
        confirmed = await self.app.push_screen_wait(
            ConfirmModal(
                "Download ALL non-placeholder models?\n"
                "This provisions/updates the hf venv, logs in, and downloads every model "
                "not marked as a placeholder. Can take a long time and uses significant bandwidth.",
                confirm_label="Download all",
                danger=True,
            )
        )
        if not confirmed:
            return
        self._run_download_all()

    @work(thread=True)
    def _run_download_all(self) -> None:
        status_label = self.query_one("#download-status", Label)
        self.app.call_from_thread(status_label.update, "Running full download (venv provision + login + all models)...")
        try:
            hf.run(self.host_profile, self.manifest, self.models, self.runner, self.repo_root)
        except BaseException as exc:  # noqa: BLE001
            # hf.run() calls sys.exit(...) both for a missing HF token and for any models
            # left with placeholder repo_ids after attempting the rest. sys.exit() raises
            # SystemExit, which is a BaseException, not Exception — and because this runs in
            # a @work(thread=True) worker, an uncaught SystemExit here would just kill this
            # background thread silently; it will NOT propagate up and terminate the TUI the
            # way it would in a single-threaded CLI run. Catch BaseException broadly (sys.exit
            # is the designed failure signal for this step) and surface exc's message in the
            # UI instead of letting it vanish.
            message = str(exc) or repr(exc)
            self.app.call_from_thread(status_label.update, f"Download-all stopped: {message}")
            self.app.call_from_thread(self.app.notify, f"Download-all stopped: {message}", severity="error")
            self.app.call_from_thread(self._refresh_table)
            self.app.call_from_thread(self._refresh_disk_usage)
            return
        self.app.call_from_thread(status_label.update, "Download-all complete.")
        self.app.call_from_thread(self.app.notify, "Download-all complete")
        self.app.call_from_thread(self._refresh_auth_banner)
        self.app.call_from_thread(self._refresh_table)
        self.app.call_from_thread(self._refresh_disk_usage)
