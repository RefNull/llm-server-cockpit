"""Installs tab: per-backend llama.cpp build state, retained-build/rollback controls, and a
manual upstream-version check. A thin view + confirm layer over provision.steps.build and
cockpit.update_check — no build/rollback/version-check logic lives here, only rendering and
the ConfirmModal gate in front of every mutating call.
"""
from __future__ import annotations

import logging
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Label, RichLog, Static

from cockpit import update_check
from cockpit.widgets import CockpitDataTable, CockpitScreenBase, SingleClickDataTable, selection_marker
from provision.common import Runner
from provision.steps import build as build_step

log = logging.getLogger("provision")


class _BuildLogHandler(logging.Handler):
    """Forwards provision.* log records into the build-log RichLog while a build runs.

    Installed only for the duration of a build (see BuildsScreen._run_build) so it doesn't
    leak records from unrelated screens/tabs into this one's log widget.
    """

    def __init__(self, screen: "BuildsScreen") -> None:
        super().__init__()
        self._screen = screen
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            return
        try:
            # emit() runs on the worker thread doing the build — call_from_thread is the
            # correct (and only safe) way to touch widget state from here.
            self._screen.app.call_from_thread(self._screen._append_build_log, msg)
        except Exception:
            pass  # app shutting down or no longer running in a thread context — drop it


class BuildHistoryModal(ModalScreen[None]):
    """Modal displaying recent build history entries."""

    BINDINGS = [("escape", "dismiss_modal", "Close")]

    DEFAULT_CSS = """
    BuildHistoryModal {
        align: center middle;
    }
    #history-dialog {
        width: 90%;
        height: 80%;
        border: thick $background 80%;
        background: $surface;
        padding: $space-normal $space-section;
    }
    #history-header {
        height: auto;
        margin-bottom: $space-normal;
    }
    #history-title {
        width: 1fr;
        text-style: bold;
    }
    #history-table {
        height: 1fr;
        margin-bottom: $space-normal;
    }
    """

    def __init__(self, host_profile: dict, backends: list[str]) -> None:
        super().__init__()
        self.host_profile = host_profile
        self.backends = backends

    def compose(self) -> ComposeResult:
        with Vertical(id="history-dialog"):
            with Horizontal(id="history-header"):
                yield Static("Recent Build History", id="history-title")
                yield Button("×", id="history-close", classes="close-button", variant="error")
            table = CockpitDataTable(id="history-table", zebra_stripes=True, fixed_columns=1)
            table.cursor_type = "row"
            yield table
            with Horizontal(classes="action-row-secondary"):
                yield Button("Close", id="btn-history-close-bottom", classes="thin-button")

    def on_mount(self) -> None:
        table = self.query_one("#history-table", CockpitDataTable)
        table.add_column("Backend", width=12)
        table.add_column("Timestamp (UTC)", width=22)
        table.add_column("Outcome", width=16)
        table.add_column("Detail", width=40)

        all_history: list[tuple[str, dict]] = []
        for backend in self.backends:
            history = build_step.read_build_history(self.host_profile, backend, limit=10)
            for h in history:
                all_history.append((backend, h))
        all_history.sort(key=lambda item: item[1].get("timestamp") or "", reverse=True)

        for backend, h in all_history[:20]:
            detail = h.get("detail") or ""
            first_line = detail.splitlines()[0] if detail else ""
            ts = (h.get("timestamp") or "")[:19].replace("T", " ")
            outcome = h.get("outcome", "")
            style = (
                "green" if outcome == "smoke_pass"
                else "bold red" if outcome in ("smoke_failed", "build_failed")
                else ""
            )
            table.add_row(Text(backend), Text(ts), Text(outcome, style=style), Text(first_line))

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id in ("history-close", "btn-history-close-bottom"):
            self.dismiss(None)


class BuildsScreen(CockpitScreenBase):
    """The 'Installs' tab. One panel per backend the host's GPUs actually use (cuda, vulkan,
    etc. per hosts/<hostname>.yaml), plus one upstream-version-check panel.
    """

    DEFAULT_CSS = """
    BuildsScreen {
        height: 1fr;
    }
    BuildsScreen #build-log {
        height: 10;
        border: round $accent;
        margin-top: $space-normal;
        display: none;
    }
    BuildsScreen #build-status {
        text-style: italic;
        margin-top: $space-normal;
        margin-bottom: $space-normal;
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
        self.backends = self._compute_backends()

        self._selected_backend: str | None = None
        self._selected_ref: str | None = None
        self._builds_cache: dict[str, list[dict]] = {backend: [] for backend in self.backends}
        self._build_in_progress = False
        self._selected_backends: set[str] = set()
        self._llama_cpp_check: dict | None = None
        self._llama_swap_check: dict | None = None

    def _compute_backends(self) -> list[str]:
        seen: list[str] = []
        for gpu in self.host_profile.get("gpus", []):
            for backend in gpu.get("backends", []):
                if backend not in seen:
                    seen.append(backend)
        return seen

    @staticmethod
    def _short(ref: str | None) -> str:
        if not ref:
            return ""
        return ref[:10]

    # ------------------------------------------------------------------ compose / mount

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            with Vertical(id="update-panel", classes="panel"):
                yield Static("Components", classes="section-title")
                backends_table = SingleClickDataTable(
                    id="backends-table", zebra_stripes=True, classes="data-table", fixed_columns=2
                )
                backends_table.cursor_type = "row"
                yield backends_table

            if not self.backends:
                yield Static(
                    "host profile declares no GPU backends (hosts/*.yaml gpus[].backends) — "
                    "nothing to build.",
                    id="no-backends",
                    classes="panel",
                )
            else:
                with Vertical(id="builds-panel", classes="panel"):
                    yield Static("Retained Builds", classes="section-title")
                    builds_table = SingleClickDataTable(id="builds-table", classes="data-table", fixed_columns=1)
                    builds_table.cursor_type = "row"
                    yield builds_table

            with Horizontal(classes="action-row-primary"):
                yield Button("Update selected", id="btn-update-selected", variant="primary", classes="thin-button")
                yield Button("Rollback", id="btn-rollback", variant="warning", classes="thin-button")

            with Horizontal(classes="action-row-secondary"):
                yield Button("Build History", id="btn-build-history", classes="thin-button")
                yield Button("Check for Updates", id="btn-check-updates", classes="thin-button")

            yield Static("", id="build-status")
            yield RichLog(id="build-log", highlight=False, markup=False, max_lines=400)

    def on_mount(self) -> None:
        backends_table = self.query_one("#backends-table", SingleClickDataTable)
        backends_table.cursor_type = "row"
        backends_table.zebra_stripes = True
        backends_table.add_column("", width=3)
        backends_table.add_column("Component", width=22)
        backends_table.add_column("Pinned", width=8)
        backends_table.add_column("Update", width=34)

        if self.backends:
            builds_table = self.query_one("#builds-table", SingleClickDataTable)
            builds_table.cursor_type = "row"
            builds_table.zebra_stripes = True
            builds_table.add_column("Backend", width=12)
            builds_table.add_column("Ref", width=14)
            builds_table.add_column("Status", width=12)
            builds_table.add_column("Integrity", width=12)

        self._refresh_builds()
        self._refresh_backends_table()
        # Initial check on mount so the panel isn't blank; the button below is for
        # re-checking on demand afterwards — refresh (the 'r' binding) never re-hits it.
        self._run_update_check(notify_result=False)

    def on_refresh_requested(self) -> None:
        """Called by the app's global 'r' binding. Re-reads local build state only — never
        re-triggers the network update check."""
        self._refresh_builds()

    # ------------------------------------------------------------------ rendering

    def _refresh_builds(self) -> None:
        if not self.backends:
            return

        table = self.query_one("#builds-table", DataTable)
        table.clear()
        for backend in self.backends:
            builds = build_step.list_builds(self.host_profile, backend)
            self._builds_cache[backend] = builds
            for b in builds:
                current_cell = Text("current", style="bold green") if b.get("current") else Text("retained", style="dim")
                sane_cell = Text("ok", style="green") if b.get("sane") else Text("NOT SANE", style="bold red")
                table.add_row(
                    Text(backend),
                    Text(self._short(b.get("ref", ""))),
                    current_cell,
                    sane_cell,
                    key=f"{backend}:{b.get('ref', '')}",
                )

        if self._selected_backend and self._selected_ref:
            backend_builds = self._builds_cache.get(self._selected_backend, [])
            if not any(b.get("ref") == self._selected_ref for b in backend_builds):
                self._selected_backend = None
                self._selected_ref = None

    def _refresh_backends_table(self) -> None:
        table = self.query_one("#backends-table", DataTable)
        table.clear()
        cpp_ref = self.manifest.get("llama_cpp", {}).get("ref", "")
        for backend in self.backends:
            pinned_cell = Text("[★]", style="bold green") if cpp_ref else Text("[-]", style="dim")
            table.add_row(
                selection_marker(backend in self._selected_backends),
                Text(f"llama.cpp ({backend})"),
                pinned_cell,
                self._format_update_cell(self._llama_cpp_check, shorten=True),
                key=backend,
            )
        swap_version = self.manifest.get("llama_swap", {}).get("version", "")
        swap_pinned = Text("[★]", style="bold green") if swap_version else Text("[-]", style="dim")
        table.add_row(
            Text("—", style="dim"),
            Text("llama-swap"),
            swap_pinned,
            self._format_update_cell(self._llama_swap_check, shorten=False),
            key="llama-swap",
        )

    def _format_update_cell(self, result: dict | None, *, shorten: bool) -> Text:
        if result is None:
            return Text("not checked yet", style="dim")
        if not result.get("ok"):
            return Text(f"couldn't check ({result.get('error')})", style="dim")
        if not result["update_available"]:
            return Text("up to date", style="green")
        latest = self._short(result["latest"]) if shorten else result["latest"]
        return Text(f"update available (latest {latest})", style="bold yellow")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "builds-table":
            return
        if event.row_key is None or event.row_key.value is None:
            self._selected_backend = None
            self._selected_ref = None
        else:
            key_str = str(event.row_key.value)
            backend, _, ref = key_str.partition(":")
            self._selected_backend = backend
            self._selected_ref = ref

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "backends-table":
            if event.row_key is None or event.row_key.value is None:
                return
            backend = event.row_key.value
            if backend not in self.backends:
                # The llama-swap row is informational only — updated from the Deploy tab's
                # deploy action, not from here — so it isn't a valid tick target.
                return
            if backend in self._selected_backends:
                self._selected_backends.discard(backend)
            else:
                self._selected_backends.add(backend)
            self._refresh_backends_table()
            return
        if event.data_table.id != "builds-table":
            return
        if event.row_key is not None and event.row_key.value is not None:
            key_str = str(event.row_key.value)
            backend, _, ref = key_str.partition(":")
            self._selected_backend = backend
            self._selected_ref = ref

    # ------------------------------------------------------------------ button dispatch

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "btn-check-updates":
            self._run_update_check()
        elif button_id == "btn-update-selected":
            await self._handle_update_selected_press()
        elif button_id == "btn-rollback":
            await self._handle_rollback_press()
        elif button_id == "btn-build-history":
            self.app.push_screen(BuildHistoryModal(self.host_profile, self.backends))

    async def _handle_update_selected_press(self) -> None:
        if self._build_in_progress:
            self.notify("a build is already in progress", severity="warning")
            return
        if not self._selected_backends:
            self.notify("tick at least one backend in the table first", severity="warning")
            return

        backends = sorted(self._selected_backends)
        ref = self.manifest["llama_cpp"]["ref"]
        message = f"Build/update {', '.join(backends)} at pinned ref {ref}? This can take several minutes."

        # DESIGN.md §5 tier 3: compiles and swaps the backend runtime binary `current` points at.
        confirmed = await self.confirm(message, confirm_label="Update", mutates_system=True)
        if confirmed:
            self._run_build(backends)

    async def _handle_rollback_press(self) -> None:
        if not self._selected_backend or not self._selected_ref:
            self.notify("select a build in the table first", severity="warning")
            return

        backend = self._selected_backend
        target_ref = self._selected_ref

        matching = next((b for b in self._builds_cache.get(backend, []) if b.get("ref") == target_ref), None)
        if matching is None:
            self.notify("selected build is no longer available — refresh and try again", severity="warning")
            return
        if matching.get("current"):
            self.notify(f"{backend}: {self._short(target_ref)} is already current", severity="information")
            return

        message = (
            f"Roll back {backend} to {target_ref}? This points 'current' at an "
            f"already-built, retained prefix — no rebuild, no re-smoke-test."
        )

        # DESIGN.md §5 tier 3: repoints `current` at a different runtime binary.
        confirmed = await self.confirm(message, confirm_label="Roll back", mutates_system=True)
        if confirmed:
            self._run_rollback(backend, target_ref)

    # ------------------------------------------------------------------ build (blocking, off main thread)

    @work(thread=True)
    def _run_build(self, backends: list[str]) -> None:
        self.app.call_from_thread(self._set_building, True)

        handler = _BuildLogHandler(self)
        provision_logger = logging.getLogger("provision")
        prior_level = provision_logger.level
        if provision_logger.level == logging.NOTSET or provision_logger.level > logging.INFO:
            provision_logger.setLevel(logging.INFO)
        provision_logger.addHandler(handler)

        try:
            build_step.run(self.host_profile, self.manifest, self.models, self.runner, self.repo_root, backends=backends)
        except SystemExit as e:
            self.app.call_from_thread(self.app.notify, f"build failed: {e}", severity="error")
        except Exception as e:  # never let a build-time exception crash the whole TUI
            self.app.call_from_thread(self.app.notify, f"build failed: {e}", severity="error")
        else:
            self.app.call_from_thread(self.app.notify, "build finished")
        finally:
            provision_logger.removeHandler(handler)
            provision_logger.setLevel(prior_level)
            self.app.call_from_thread(self._selected_backends.clear)
            self.app.call_from_thread(self._set_building, False)
            self.app.call_from_thread(self._refresh_builds)
            self.app.call_from_thread(self._refresh_backends_table)

    def _set_building(self, active: bool) -> None:
        self._build_in_progress = active
        self.query_one("#build-status", Static).update("building... (see log below)" if active else "")
        update_btn = self.query("#btn-update-selected")
        if update_btn:
            update_btn.first(Button).disabled = active
        rollback_btn = self.query("#btn-rollback")
        if rollback_btn:
            rollback_btn.first(Button).disabled = active
        if active:
            log_widget = self.query_one("#build-log", RichLog)
            log_widget.clear()
            log_widget.display = True

    def _append_build_log(self, msg: str) -> None:
        self.query_one("#build-log", RichLog).write(msg)

    # ------------------------------------------------------------------ rollback (blocking, off main thread)

    @work(thread=True)
    def _run_rollback(self, backend: str, target_ref: str) -> None:
        try:
            build_step.rollback(self.host_profile, backend, target_ref, self.runner)
        except SystemExit as e:
            self.app.call_from_thread(self.app.notify, f"rollback failed: {e}", severity="error")
            return
        except Exception as e:
            self.app.call_from_thread(self.app.notify, f"rollback failed: {e}", severity="error")
            return

        msg = f"{backend}: current now points at {self._short(target_ref)}"
        self.app.call_from_thread(self.app.notify, msg)
        self.app.call_from_thread(self._refresh_builds)

    # ------------------------------------------------------------------ upstream version check (network, off main thread)

    @work(thread=True)
    def _run_update_check(self, *, notify_result: bool = True) -> None:
        self.app.call_from_thread(self._set_checking_status, True)
        try:
            result_cpp = update_check.check_llama_cpp(self.manifest)
            result_swap = update_check.check_llama_swap(self.manifest)
        except Exception as e:
            self.app.call_from_thread(self.app.notify, f"version check failed: {e}", severity="error")
            self.app.call_from_thread(self._set_checking_status, False)
            return
        self.app.call_from_thread(self._apply_update_check_results, result_cpp, result_swap)
        if notify_result:
            if not result_cpp.get("ok") or not result_swap.get("ok"):
                self.app.call_from_thread(
                    self.app.notify, "version check completed with errors", severity="warning"
                )
            elif result_cpp.get("update_available") or result_swap.get("update_available"):
                self.app.call_from_thread(self.app.notify, "updates available for upstream components")
            else:
                self.app.call_from_thread(self.app.notify, "upstream components are up to date")
        self.app.call_from_thread(self._set_checking_status, False)

    def _set_checking_status(self, checking: bool) -> None:
        if not self.is_mounted:
            return
        btn = self.query("#btn-check-updates")
        if btn:
            btn.first(Button).disabled = checking
        if checking:
            self._llama_cpp_check = None
            self._llama_swap_check = None
            self._refresh_backends_table()

    def _apply_update_check_results(self, result_cpp: dict, result_swap: dict) -> None:
        if not self.is_mounted:
            return
        self._llama_cpp_check = result_cpp
        self._llama_swap_check = result_swap
        self._refresh_backends_table()
