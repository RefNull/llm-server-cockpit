"""Cockpit "Downloads" tab: track models against models.yaml, see their live Hugging Face
status (downloaded / ready for download / needs auth to verify / not found), and download —
view + confirm layer over provision.steps.hf, no parallel download/auth logic.

HF metadata (size, dates, existence) is read straight from the public HF Hub API via plain
urllib.request (huggingface_hub is not importable from .venv — provision/steps/hf.py's module
docstring; cockpit/update_check.py already sets this precedent for cockpit-side HF calls).
Unauthenticated, a nonexistent repo returns 401 (anti-enumeration), not 404 — so "not found" is
only ever reported once a token is available to disambiguate it from "private/gated".
"""
from __future__ import annotations

import json
import os
import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Static

from cockpit.widgets import CockpitScreenBase, SingleClickDataTable, TableAction
from provision import schema
from provision.common import Runner
from provision.steps import hf

_HF_TIMEOUT_S = 10
_LOCAL_STATUS_LABELS = {
    "placeholder": "PLACEHOLDER — edit models.yaml",
    "unmanaged": "unmanaged",
    "downloaded": "downloaded",
}


def _hf_api_lookup(repo_id: str, token: str | None) -> tuple[int, dict | None]:
    """Live HF Hub metadata probe. Returns (http_status, json_or_None). Never raises — a
    network failure degrades to status 0 like cockpit/update_check.py's GitHub calls do."""
    url = f"https://huggingface.co/api/models/{repo_id}?blobs=true"
    headers = {"User-Agent": "llm-server-cockpit"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_HF_TIMEOUT_S) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def _fmt_bytes(num_bytes: float) -> str:
    return f"{num_bytes / (1024 ** 3):.1f} GB" if num_bytes >= 1024**3 else f"{num_bytes / (1024 ** 2):.0f} MB"


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
        padding: $space-normal $space-section;
    }
    #hf-token-title {
        text-style: bold;
        margin-bottom: $space-normal;
    }
    #hf-token-msg {
        color: $text-muted;
        margin-bottom: $space-normal;
    }
    #hf-token-input {
        margin-bottom: $space-normal;
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
            with Horizontal(classes="action-row-primary"):
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
    #track-row {
        margin-bottom: $space-section;
    }
    #disk-usage {
        margin-top: $space-normal;
        color: $text-muted;
    }
    #download-status {
        margin-top: $space-normal;
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
        # id -> {"status": str, "size": int|None, "date": str|None} from the last refresh.
        self._row_info: dict[str, dict[str, Any]] = {}
        self._downloading = False

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            with Horizontal(classes="inline-row", id="track-row"):
                yield Input(
                    placeholder="Track a model: repo_id:quant_file (e.g. org/Model-GGUF:model.Q4_K_M.gguf)",
                    id="track-model-input",
                )
                yield Button("Track", id="track-btn", variant="primary")  # inline archetype (DESIGN.md §9): bare Button inside .inline-row

            # No fixed_columns: a pinned column is painted from datatable--fixed, which
            # REPLACES the row style (_data_table.py _render_line_in_row) rather than
            # compositing with it, so it cut a flat band down column 0 through the zebra
            # stripes. Every column here carries an explicit width that fits the 115-cell
            # usable viewport, so nothing scrolls horizontally for it to pin.
            table = SingleClickDataTable(id="model-table", zebra_stripes=True, classes="data-table")
            table.cursor_type = "row"
            yield table

            with Horizontal(classes="action-row-primary"):
                # Per-model download is the in-table [ Download ] action column. This one is
                # the genuine bulk case, not a second way to act on a selection.
                yield Button("Download all missing", id="btn-download-all", variant="primary", classes="thin-button")

            with Horizontal(classes="action-row-secondary"):
                yield Static("", id="disk-usage")
                yield Label("", id="download-status", classes="status-text")

    def on_mount(self) -> None:
        table = self.query_one("#model-table", SingleClickDataTable)
        # Column budget (DESIGN.md §4): render width is content width + 2*cell_padding per
        # column (Textual _data_table.py Column.get_render_width) against a 115-cell usable
        # viewport at 121x30 (121 - 2*$space-edge). Dropping the tick column returned its 3
        # content cells + 2 padding to the data columns and put ID flush left. Content sum
        # 88, render sum 88 + 2*6 = 100 <= 115.
        table.add_column("ID", width=20)
        table.add_column("Repo ID", width=26)
        table.add_column("Status", width=20)
        table.add_column("Size", width=10)
        table.add_column("Release Date", width=12)
        table.add_action_column(TableAction("download", "Download", confirm="Download {row}?", available=self._can_download))
        # No data read here — ensure_first_view() does it when this tab is first shown.

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _can_download(self, model_id: str) -> bool:
        info = self._row_info.get(model_id)
        return bool(info) and info["status"] == "ready for download" and not self._downloading

    @work(thread=True)
    def _refresh_table(self) -> None:
        """Batched on refresh (mount / 'r' / after a track/download), never per-row on render
        and never per keystroke — every HF lookup below is a live network call."""
        token_env = self.host_profile.get("hf", {}).get("token_env", "HF_TOKEN")
        token = os.environ.get(token_env)

        models = [m for m in self.models.get("models", []) if m.get("engine") == "llama-cpp"]
        rows: list[tuple[dict, dict]] = []
        for model in models:
            rows.append((model, self._compute_row_info(model, token)))
        self.app.call_from_thread(self._apply_rows, models, rows)

    def _compute_row_info(self, model: dict, token: str | None) -> dict:
        local = hf.model_status(model, self.host_profile)
        if local in ("downloaded", "placeholder", "unmanaged"):
            return {"status": local, "size": None, "date": None}

        # local == "missing" — ask HF whether it actually exists / is downloadable.
        code, data = _hf_api_lookup(model["repo_id"], token)
        if code == 200:
            size = date = None
            if data:
                sibling = next(
                    (s for s in data.get("siblings", []) if s.get("rfilename") == model.get("quant_file")),
                    None,
                )
                size = sibling.get("size") if sibling else None
                date = data.get("createdAt")
            return {"status": "ready for download", "size": size, "date": date}
        if code == 404:
            # Only reachable with a token — unauthenticated HF returns 401 for a nonexistent
            # repo too (anti-enumeration), so "not found" is never reported without one.
            return {"status": "not found", "size": None, "date": None}
        if code == 401:
            return {"status": "needs auth to verify", "size": None, "date": None}
        return {"status": "check failed", "size": None, "date": None}

    def _apply_rows(self, models: list[dict], rows: list[tuple[dict, dict]]) -> None:
        if not self.is_mounted:
            return
        table = self.query_one("#model-table", SingleClickDataTable)
        table.clear()
        self._downloadable = {m["id"]: m for m in models}
        self._row_info = {m["id"]: info for m, info in rows}

        for model, info in rows:
            status = info["status"]
            status_label = _LOCAL_STATUS_LABELS.get(status, status)
            size_label = _fmt_bytes(info["size"]) if info.get("size") else "—"
            date_label = (info.get("date") or "—")[:10]
            repo_display = "PLACEHOLDER — edit models.yaml" if status == "placeholder" else model["repo_id"]
            # rich.text.Text, not raw str (DESIGN.md §4.6 / Phase 0a.6): the app console has
            # markup=True, so an operator-chosen repo_id containing brackets would have that
            # span silently eaten by Rich as a markup tag.
            table.add_row(
                Text(model["id"]),
                Text(repo_display),
                Text(status_label),
                Text(size_label),
                Text(date_label),
                *table.action_cells(model["id"]),
                key=model["id"],
            )

        self._sync_button_state()

    def _refresh_disk_usage(self) -> None:
        widget = self.query_one("#disk-usage", Static)
        models_dir = Path(self.host_profile["paths"]["models_dir"])
        if not models_dir.exists():
            # Name the setting, not just the path. This reads as a bug when the operator has a
            # models directory somewhere else — the app is right that THIS path is absent, but
            # says nothing about which config decides it or where to change it.
            widget.update(
                f"Disk usage: {models_dir} does not exist yet — it is created on first download. "
                f"Wrong location? That is paths.models_dir, editable at "
                f"Settings > Host Profile > Models Directory."
            )
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
        """'Download all missing' enables only when some model's live status is 'ready for
        download' and no download is running."""
        btn_all = self.query("#btn-download-all")
        if not btn_all:
            return
        ready = any(info["status"] == "ready for download" for info in self._row_info.values())
        btn_all.first(Button).disabled = self._downloading or not ready

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
    # Per-row table action
    # ------------------------------------------------------------------

    async def handle_table_action(self, action_id: str, row_key: str, table: DataTable) -> None:
        if action_id != "download":
            return
        model = self._downloadable.get(row_key)
        if model is None:
            return
        if not await self._ensure_hf_auth():
            return
        self._run_download_batch([model])

    # ------------------------------------------------------------------
    # Button handling & download actions
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # No `await` on a @work method: the decorator returns a Worker, which is not
        # awaitable — awaiting one raises TypeError and takes down the app. The worker is
        # already running by the time the call returns; there is nothing to wait for here.
        if event.button.id == "btn-download-all":
            self._on_download_all_missing()
        elif event.button.id == "track-btn":
            self._on_track_model()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "track-model-input":
            self._on_track_model()

    @staticmethod
    def _parse_track_input(raw: str) -> tuple[str, str] | None:
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
    async def _on_track_model(self) -> None:
        """Register a model in models.yaml without downloading it — download stays an explicit
        per-row action once its live status comes back 'ready for download'. Tracking-without-
        downloading already exists in the data model as hf.model_status() == 'missing'."""
        raw = self.query_one("#track-model-input", Input).value.strip()
        if not raw:
            self.notify("Enter a Hugging Face repo and model file", severity="warning")
            return
        parsed = self._parse_track_input(raw)
        if not parsed:
            self.notify(
                "Could not parse input. Provide repo and filename (e.g. 'repo/model/file.gguf' or 'repo:file.gguf')",
                severity="error",
            )
            return
        repo_id, quant_file = parsed

        existing = next(
            (
                m for m in self.models.get("models", [])
                if m.get("engine") == "llama-cpp" and m.get("quant_file") == quant_file
            ),
            None,
        )
        if existing:
            self.notify(f"already tracked as {existing['id']!r}", severity="warning")
            return

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

        self.query_one("#track-model-input", Input).value = ""
        self.notify(f"tracking {candidate_id!r} — checking Hugging Face status...")
        self._refresh_table()

    @work
    async def _on_download_all_missing(self) -> None:
        pending = [
            model
            for model_id, model in self._downloadable.items()
            if self._row_info.get(model_id, {}).get("status") == "ready for download"
        ]
        if not pending:
            return
        if not await self._ensure_hf_auth():
            return
        names = ", ".join(m["id"] for m in pending)
        confirmed = await self.confirm(
            f"Download all {len(pending)} model(s) currently ready for download?\n{names}\n"
            "This can take a long time and uses significant bandwidth.",
            confirm_label="Download all",
            mutates_system=True,
        )
        if not confirmed:
            return
        self._run_download_batch(pending)

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

    def _set_downloading(self, active: bool) -> None:
        self._downloading = active
        track_btn = self.query("#track-btn")
        if track_btn:
            track_btn.first(Button).disabled = active
        self._sync_button_state()
