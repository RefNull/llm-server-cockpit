"""Cockpit "HF Downloads" tab: the weights on disk, and a way to fetch more.

**This tab owns files, not deployments.** It lists what is actually in `paths.models_dir` and
downloads new quants into it; it never writes `models.yaml`. Turning a file into something
llama-swap serves is the Models tab's job, where the file is picked from a dropdown and bound
to a GPU and backend.

That split is deliberate. `models.yaml` used to do double duty — "weights I want" and "routes
llama-swap serves" — so downloading a model silently created a route, and the table here had to
carry a whole HF-status machinery (tracked-but-missing, placeholder, needs-auth) to describe
the gap between the two. With the table built from the directory itself, that gap does not
exist and neither does the machinery.

HF metadata is fetched with plain urllib.request (huggingface_hub is not importable from .venv
— provision/steps/hf.py's module docstring).
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Static

from cockpit.widgets import CockpitScreenBase, SingleClickDataTable, TableAction
from provision.common import Runner
from provision.steps import hf


def _fmt_bytes(num_bytes: float) -> str:
    """Three ranges, not two. With only GB/MB a sub-megabyte file rendered as "0 MB", which
    reads as "empty" rather than "small"."""
    if num_bytes >= 1024**3:
        return f"{num_bytes / (1024 ** 3):.1f} GB"
    if num_bytes >= 1024**2:
        return f"{num_bytes / (1024 ** 2):.0f} MB"
    return f"{num_bytes / 1024:.0f} KB"


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


def list_model_files(models_dir: Path) -> list[dict[str, Any]]:
    """Every .gguf under models_dir, as [{"name","size","added"}] with name RELATIVE to it.

    Relative because that is what a models.yaml `quant_file` holds — hf.model_status joins it
    onto models_dir, so a nested file only resolves when stored that way.
    """
    if not models_dir.is_dir():
        return []
    files: list[dict[str, Any]] = []
    for path in sorted(models_dir.rglob("*.gguf")):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append(
            {
                "name": str(path.relative_to(models_dir)),
                "size": stat.st_size,
                "added": time.strftime("%Y-%m-%d", time.localtime(stat.st_mtime)),
            }
        )
    return files


class DownloadsScreen(CockpitScreenBase):
    """Not a Textual Screen — mounted inside a TabPane by cockpit/app.py."""

    DEFAULT_CSS = """
    DownloadsScreen {
        height: 1fr;
        padding: 0;
    }
    #fetch-row {
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
        self._files: dict[str, dict[str, Any]] = {}
        self._downloading = False

    def _models_dir(self) -> Path:
        return Path(self.host_profile["paths"]["models_dir"])

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            with Horizontal(classes="inline-row", id="fetch-row"):
                yield Input(
                    placeholder="Download: repo_id:quant_file (e.g. org/Model-GGUF:model.Q4_K_M.gguf)",
                    id="fetch-input",
                )
                yield Button("Download", id="btn-fetch", variant="primary")  # inline archetype (DESIGN.md §9): bare Button inside .inline-row

            table = SingleClickDataTable(id="files-table", zebra_stripes=True, classes="data-table")
            table.cursor_type = "row"
            yield table

            with Horizontal(classes="action-row-secondary"):
                yield Static("", id="disk-usage")
                yield Label("", id="download-status", classes="status-text")

    def on_mount(self) -> None:
        table = self.query_one("#files-table", SingleClickDataTable)
        # Content 76 + Delete 10 = 86, render 86 + 2*5 = 96 (DESIGN.md §4).
        table.add_column("File", width=34)
        table.add_column("Size", width=10)
        table.add_column("Added", width=10)
        table.add_column("Used by", width=22)
        table.add_action_column(
            TableAction(
                "delete",
                "Delete",
                destructive=True,
                confirm="Delete {row} from disk? This frees the space and cannot be undone.",
            )
        )
        # No data read here — ensure_first_view() does it when this tab is first shown.

    def on_refresh_requested(self) -> None:
        self.models = getattr(self.cockpit_app, "models", self.models)
        self._refresh_files()
        self._refresh_disk_usage()

    # ------------------------------------------------------------------ rendering

    def _users_of(self, name: str) -> list[str]:
        """Model ids whose quant_file or mmproj_file is this file — the bridge to the Models
        tab, and the warning before a delete takes a route's weights out from under it."""
        return [
            m["id"]
            for m in self.models.get("models", [])
            if name in (m.get("quant_file"), m.get("mmproj_file"))
        ]

    @work(thread=True)
    def _refresh_files(self) -> None:
        # Pure filesystem walk — no network, unlike the per-model HF status probe this replaced.
        files = list_model_files(self._models_dir())
        self.app.call_from_thread(self._apply_rows, files)

    def _apply_rows(self, files: list[dict[str, Any]]) -> None:
        if not self.is_mounted:
            return
        self._files = {f["name"]: f for f in files}
        table = self.query_one("#files-table", SingleClickDataTable)
        table.clear()
        for entry in files:
            users = self._users_of(entry["name"])
            # rich.text.Text, never str (DESIGN.md §4.6): a quant filename can carry brackets.
            table.add_row(
                Text(entry["name"]),
                Text(_fmt_bytes(entry["size"])),
                Text(entry["added"]),
                Text(", ".join(users) if users else "—", style="" if users else "dim"),
                *table.action_cells(entry["name"]),
                key=entry["name"],
            )
        if not files:
            self.query_one("#download-status", Label).update(
                f"no .gguf files in {self._models_dir()} — download one above, "
                "or check paths.models_dir in Settings > Host Profile"
            )

    def _refresh_disk_usage(self) -> None:
        widget = self.query_one("#disk-usage", Static)
        models_dir = self._models_dir()
        if not models_dir.exists():
            widget.update(
                f"Disk usage: {models_dir} does not exist yet — it is created on first download. "
                f"Wrong location? That is paths.models_dir, editable at "
                f"Settings > Host Profile > Models Directory."
            )
            return
        total_bytes = sum(f.stat().st_size for f in models_dir.rglob("*") if f.is_file())
        try:
            free_gb = shutil.disk_usage(models_dir).free / (1024 ** 3)
            widget.update(f"Disk usage: {_fmt_bytes(total_bytes)} in {models_dir}  |  {free_gb:.1f} GiB free")
        except OSError:
            widget.update(f"Disk usage: {_fmt_bytes(total_bytes)} in {models_dir}")

    # ------------------------------------------------------------------ delete

    async def handle_table_action(self, action_id: str, row_key: str, table: DataTable) -> None:
        if action_id != "delete":
            return
        users = self._users_of(row_key)
        if users and not await self.confirm(
            f"{row_key} is still used by {', '.join(users)}.\n"
            "Deleting the file leaves those models pointing at nothing until you re-download "
            "it or remove them on the Models tab.\n\nDelete anyway?",
            confirm_label="Delete anyway",
            mutates_system=True,
        ):
            return
        try:
            (self._models_dir() / row_key).unlink()
        except OSError as e:
            self.notify(f"could not delete {row_key}: {e}", severity="error")
            return
        self.notify(f"deleted {row_key}")
        self._refresh_files()
        self._refresh_disk_usage()

    # ------------------------------------------------------------------ download

    async def _ensure_hf_auth(self) -> bool:
        token_env = self.host_profile.get("hf", {}).get("token_env", "HF_TOKEN")
        if not os.environ.get(token_env):
            token = await self.app.push_screen_wait(HFTokenModal(token_env))
            if not token:
                self.notify("Hugging Face token required to download weights", severity="warning")
                return False
            os.environ[token_env] = token
        return True

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if (event.button.id or "") == "btn-fetch":
            self._on_fetch()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "fetch-input":
            self._on_fetch()

    @staticmethod
    def _parse_fetch_input(raw: str) -> tuple[str, str] | None:
        """Accepts `repo:file`, `repo file`, a full huggingface.co URL, or `org/repo/file.gguf`.
        Kept from the previous Track field — the input shapes an operator pastes did not change
        just because the destination did."""
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
            if repo_id.strip() and quant_file.strip():
                return repo_id.strip(), quant_file.strip()
        if " " in raw:
            repo_id, quant_file = raw.split(None, 1)
            if repo_id.strip() and quant_file.strip():
                return repo_id.strip(), quant_file.strip()
        parts = [p.strip() for p in raw.split("/") if p.strip()]
        if len(parts) >= 3 and parts[-1].endswith(".gguf"):
            return "/".join(parts[:-1]), parts[-1]
        if len(parts) == 2 and parts[1].endswith(".gguf"):
            return parts[0], parts[1]
        return None

    @work
    async def _on_fetch(self) -> None:
        if self._downloading:
            self.notify("a download is already running", severity="warning")
            return
        raw = self.query_one("#fetch-input", Input).value.strip()
        parsed = self._parse_fetch_input(raw)
        if not parsed:
            self.notify(
                "Could not parse input. Give a repo and filename "
                "(e.g. 'org/Model-GGUF:model.Q4_K_M.gguf')",
                severity="error",
            )
            return
        repo_id, quant_file = parsed
        if quant_file in self._files:
            self.notify(f"{quant_file} is already in the models folder", severity="warning")
            return
        if not await self._ensure_hf_auth():
            return
        if not await self.confirm(
            f"Download {quant_file}\nfrom {repo_id}\ninto {self._models_dir()}?\n\n"
            "This can take a while and uses bandwidth. It downloads the file only — bind it to "
            "a GPU and backend on the Models tab afterwards.",
            confirm_label="Download",
            mutates_system=True,
        ):
            return
        self._run_download(repo_id, quant_file)

    @work(thread=True)
    def _run_download(self, repo_id: str, quant_file: str) -> None:
        self.app.call_from_thread(self._set_downloading, True)
        status = self.query_one("#download-status", Label)
        try:
            venv_path = Path(self.host_profile["paths"]["state_dir"]) / "venv-hf"
            hf._ensure_venv(venv_path, self.runner)
            hf._ensure_hub_pinned(venv_path, self.manifest["huggingface_hub"]["version"], self.runner)
            self.runner.mkdir(str(self._models_dir()))
            self.app.call_from_thread(status.update, f"Downloading {quant_file}...")
            # An ad-hoc dict, not a models.yaml entry: downloading a file is no longer the same
            # act as declaring a route, which is the whole point of this split.
            hf.download_model(
                {"engine": "llama-cpp", "repo_id": repo_id, "quant_file": quant_file},
                self.host_profile,
                self.runner,
            )
        except BaseException as exc:  # noqa: BLE001 — a failed download must not kill the TUI
            self.app.call_from_thread(status.update, f"Download failed: {exc}")
            self.app.call_from_thread(self.app.notify, f"{quant_file}: download failed — {exc}", severity="error")
        else:
            self.app.call_from_thread(status.update, f"Downloaded {quant_file}.")
            self.app.call_from_thread(self.app.notify, f"{quant_file}: download complete")
            self.app.call_from_thread(self.query_one("#fetch-input", Input).clear)
        finally:
            self.app.call_from_thread(self._refresh_files)
            self.app.call_from_thread(self._refresh_disk_usage)
            self.app.call_from_thread(self._set_downloading, False)

    def _set_downloading(self, active: bool) -> None:
        self._downloading = active
        btn = self.query("#btn-fetch")
        if btn:
            btn.first(Button).disabled = active
