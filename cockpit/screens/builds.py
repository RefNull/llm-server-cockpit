"""Installs tab: per-backend llama.cpp build state, retained-build/rollback controls, and a
manual upstream-version check. A thin view + confirm layer over provision.steps.build and
cockpit.update_check — no build/rollback/version-check logic lives here, only rendering and
the ConfirmModal gate in front of every mutating call.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, RichLog, Static

from cockpit import update_check
from cockpit.widgets import (
    CockpitDataTable,
    CockpitScreenBase,
    ConfirmModal,
    SingleClickDataTable,
    TableAction,
    TableActionInvoked,
    acquire_sudo,
    root_note,
)
from provision.common import Runner
from provision.steps import build as build_step

log = logging.getLogger("provision")


def _short(ref: str | None) -> str:
    if not ref:
        return ""
    return ref[:10]


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
            table = CockpitDataTable(id="history-table", zebra_stripes=True)
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


class RetainedBuildsModal(ModalScreen[None]):
    """Per-backend retained build inventory — previously an always-visible third DataTable on
    the Installs tab (the DESIGN.md §3.1 "max 2 tables" violation named in the QA pass), now
    behind a button so BuildsScreen itself composes only backends-table directly.

    Rollback and Remove are per-row actions here rather than a cursor-select + action-row
    button pair: each row already identifies one specific (backend, ref) build, which is
    exactly the (backend, target_ref) pair build_step.rollback() needs — no separate selection
    step to get wrong. Not a CockpitScreenBase (it's a ModalScreen, a different widget-tree
    root), so it can't inherit CockpitScreenBase's TableActionInvoked handling — this modal
    implements the same confirm-then-dispatch shape itself, same as every other modal in this
    codebase that calls ConfirmModal directly (DESIGN.md §5's declarative-write idiom).
    """

    BINDINGS = [("escape", "dismiss_modal", "Close")]

    DEFAULT_CSS = """
    RetainedBuildsModal {
        align: center middle;
    }
    #retained-dialog {
        width: 90%;
        height: 80%;
        border: thick $background 80%;
        background: $surface;
        padding: $space-normal $space-section;
    }
    #retained-header {
        height: auto;
        margin-bottom: $space-normal;
    }
    #retained-title {
        width: 1fr;
        text-style: bold;
    }
    #retained-table {
        height: 1fr;
        margin-bottom: $space-normal;
    }
    """

    def __init__(self, host_profile: dict, backends: list[str], runner: Runner) -> None:
        super().__init__()
        self.host_profile = host_profile
        self.backends = backends
        self.runner = runner
        # "backend:ref" -> build dict (+ "backend"), refreshed on every _refresh() call.
        self._builds: dict[str, dict] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="retained-dialog"):
            with Horizontal(id="retained-header"):
                yield Static("Retained Builds", id="retained-title")
                yield Button("×", id="retained-close", classes="close-button", variant="error")
            table = SingleClickDataTable(id="retained-table", zebra_stripes=True)
            table.cursor_type = "row"
            yield table
            with Horizontal(classes="action-row-secondary"):
                yield Button("Close", id="btn-retained-close-bottom", classes="thin-button")

    def on_mount(self) -> None:
        table = self.query_one("#retained-table", SingleClickDataTable)
        table.add_column("Backend", width=12)
        table.add_column("Ref", width=14)
        table.add_column("Status", width=12)
        table.add_column("Integrity", width=12)
        table.add_action_column(
            TableAction(
                "rollback",
                "Rollback",
                confirm="Roll back {row}? Points 'current' at this already-built prefix — "
                "no rebuild, no re-smoke-test.",
                available=self._can_rollback,
            )
        )
        table.add_action_column(
            TableAction(
                "remove",
                "Remove",
                destructive=True,
                confirm="Delete retained build {row}? Frees disk space; cannot be undone.",
                available=self._can_remove,
            )
        )
        self._refresh()

    def _can_rollback(self, row_key: str) -> bool:
        build = self._builds.get(row_key)
        return bool(build) and not build["current"] and build["sane"]

    def _can_remove(self, row_key: str) -> bool:
        build = self._builds.get(row_key)
        return bool(build) and not build["current"]

    def _refresh(self) -> None:
        if not self.is_mounted:
            return
        table = self.query_one("#retained-table", SingleClickDataTable)
        table.clear()
        self._builds.clear()
        for backend in self.backends:
            for b in build_step.list_builds(self.host_profile, backend):
                key = f"{backend}:{b.get('ref', '')}"
                self._builds[key] = {**b, "backend": backend}
                current_cell = Text("current", style="bold green") if b.get("current") else Text("retained", style="dim")
                sane_cell = Text("ok", style="green") if b.get("sane") else Text("NOT SANE", style="bold red")
                table.add_row(
                    Text(backend),
                    Text(_short(b.get("ref", ""))),
                    current_cell,
                    sane_cell,
                    *table.action_cells(key),
                    key=key,
                )

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id in ("retained-close", "btn-retained-close-bottom"):
            self.dismiss(None)

    @work
    async def _on_table_action_invoked(self, event: TableActionInvoked) -> None:
        event.stop()
        message = event.action.confirm_message(event.row_key)
        if message is not None:
            # Both actions write under prefix_root (/opt/...), so this modal repeats
            # CockpitScreenBase.confirm's root gate — it can't inherit it, being a ModalScreen.
            needs_sudo = os.geteuid() != 0
            confirmed = await self.app.push_screen_wait(
                ConfirmModal(
                    root_note(message) if needs_sudo else message,
                    confirm_label=event.action.resolve_label(event.row_key),
                    danger=True,
                )
            )
            if not confirmed:
                return
            if needs_sudo:
                elevated = await acquire_sudo(self.app)
                if elevated is None:
                    return
                self.runner = elevated
        build = self._builds.get(event.row_key)
        if build is None:
            return
        if event.action.id == "rollback":
            self._run_rollback(build["backend"], build["ref"])
        elif event.action.id == "remove":
            self._run_remove(build["backend"], build["ref"])

    @work(thread=True)
    def _run_rollback(self, backend: str, ref: str) -> None:
        try:
            build_step.rollback(self.host_profile, backend, ref, self.runner)
        except SystemExit as e:
            self.app.call_from_thread(self.app.notify, f"rollback failed: {e}", severity="error")
            return
        except Exception as e:
            self.app.call_from_thread(self.app.notify, f"rollback failed: {e}", severity="error")
            return
        self.app.call_from_thread(self.app.notify, f"{backend}: current now points at {_short(ref)}")
        self.app.call_from_thread(self._refresh)

    @work(thread=True)
    def _run_remove(self, backend: str, ref: str) -> None:
        target = Path(self.host_profile["paths"]["prefix_root"]) / backend / ref
        try:
            self.runner.run(["rm", "-rf", str(target)])
        except Exception as e:
            self.app.call_from_thread(self.app.notify, f"remove failed: {e}", severity="error")
            return
        self.app.call_from_thread(self.app.notify, f"{backend}: removed retained build {_short(ref)}")
        self.app.call_from_thread(self._refresh)


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

        self._build_in_progress = False
        self._llama_cpp_check: dict | None = None
        self._llama_swap_check: dict | None = None

    def _compute_backends(self) -> list[str]:
        seen: list[str] = []
        for gpu in self.host_profile.get("gpus", []):
            for backend in gpu.get("backends", []):
                if backend not in seen:
                    seen.append(backend)
        return seen

    # ------------------------------------------------------------------ compose / mount

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            with Vertical(id="update-panel", classes="panel"):
                # No fixed_columns (DESIGN.md §4.2): datatable--fixed REPLACES the row
                # style rather than compositing with it, so a pinned column cut a flat band
                # down column 0 through the zebra stripes — the "first column is always
                # highlighted" defect. Every column has an explicit width that fits, so
                # nothing scrolls horizontally for it to pin.
                backends_table = SingleClickDataTable(
                    id="backends-table", zebra_stripes=True, classes="data-table"
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

            with Horizontal(classes="action-row-secondary"):
                yield Button("Retained Builds", id="btn-retained-builds", classes="thin-button")
                yield Button("Build History", id="btn-build-history", classes="thin-button")
                yield Button("Check for Updates", id="btn-check-updates", classes="thin-button")

            yield Static("", id="build-status")
            yield RichLog(id="build-log", highlight=False, markup=False, max_lines=400)

    def on_mount(self) -> None:
        backends_table = self.query_one("#backends-table", SingleClickDataTable)
        backends_table.cursor_type = "row"
        backends_table.zebra_stripes = True
        backends_table.add_column("Component", width=22)
        backends_table.add_column("Pinned ref", width=14)
        backends_table.add_column("Status", width=34)
        backends_table.add_action_column(
            TableAction(
                "update",
                "Update",
                confirm="Build/update {row}? This can take several minutes.",
                requires_root=True,
                available=self._backend_has_update,
            )
        )

        # No data read here — ensure_first_view() does it when this tab is first shown.

    def on_refresh_requested(self) -> None:
        """Called by the app's global 'r' binding. Re-reads local (manifest/check) state —
        never re-triggers the network update check."""
        self._refresh_backends_table()

    def on_first_view(self) -> None:
        """The one screen whose first view does more than a refresh: the upstream version
        check is a network call, so it runs once when the tab is first opened and then only on
        the explicit button — never on 'r'."""
        self._refresh_backends_table()
        self._run_update_check(notify_result=False)

    # ------------------------------------------------------------------ rendering

    def _backend_has_update(self, row_key: str) -> bool:
        return (
            row_key in self.backends
            and self._llama_cpp_check is not None
            and self._llama_cpp_check.get("ok")
            and self._llama_cpp_check.get("update_available")
        )

    def _refresh_backends_table(self) -> None:
        table = self.query_one("#backends-table", SingleClickDataTable)
        table.clear()
        cpp_ref = self.manifest.get("llama_cpp", {}).get("ref", "")
        for backend in self.backends:
            table.add_row(
                Text(f"llama.cpp ({backend})"),
                Text(_short(cpp_ref)),
                self._format_update_cell(self._llama_cpp_check, shorten=True),
                *table.action_cells(backend),
                key=backend,
            )
        swap_version = self.manifest.get("llama_swap", {}).get("version", "")
        table.add_row(
            Text("llama-swap"),
            Text(swap_version),
            self._format_update_cell(self._llama_swap_check, shorten=False),
            # The llama-swap row is informational only — updated from the Deploy tab's deploy
            # action, not from here — so its Update cell is always blank (_backend_has_update
            # is False for any key not in self.backends).
            *table.action_cells("llama-swap"),
            key="llama-swap",
        )

    def _format_update_cell(self, result: dict | None, *, shorten: bool) -> Text:
        if result is None:
            return Text("not checked yet", style="dim")
        if not result.get("ok"):
            return Text(f"couldn't check ({result.get('error')})", style="dim")
        if not result["update_available"]:
            return Text("up to date", style="green")
        latest = _short(result["latest"]) if shorten else result["latest"]
        return Text(f"update available (latest {latest})", style="bold yellow")

    # ------------------------------------------------------------------ per-row action dispatch

    async def handle_table_action(self, action_id: str, row_key: str, table: DataTable) -> None:
        if action_id != "update":
            return
        if self._build_in_progress:
            self.notify("a build is already in progress", severity="warning")
            return
        self._run_build([row_key])

    # ------------------------------------------------------------------ button dispatch

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "btn-check-updates":
            self._run_update_check()
        elif button_id == "btn-build-history":
            self.app.push_screen(BuildHistoryModal(self.host_profile, self.backends))
        elif button_id == "btn-retained-builds":
            self.app.push_screen(RetainedBuildsModal(self.host_profile, self.backends, self.runner))

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
            build_step.run(self.host_profile, self.manifest, self.models, self.privileged_runner, self.repo_root, backends=backends)
        except SystemExit as e:
            self.app.call_from_thread(self.app.notify, f"build failed: {e}", severity="error")
        except Exception as e:  # never let a build-time exception crash the whole TUI
            self.app.call_from_thread(self.app.notify, f"build failed: {e}", severity="error")
        else:
            self.app.call_from_thread(self.app.notify, "build finished")
        finally:
            provision_logger.removeHandler(handler)
            provision_logger.setLevel(prior_level)
            self.app.call_from_thread(self._set_building, False)
            self.app.call_from_thread(self._refresh_backends_table)

    def _set_building(self, active: bool) -> None:
        self._build_in_progress = active
        self.query_one("#build-status", Static).update("building... (see log below)" if active else "")
        if active:
            log_widget = self.query_one("#build-log", RichLog)
            log_widget.clear()
            log_widget.display = True

    def _append_build_log(self, msg: str) -> None:
        self.query_one("#build-log", RichLog).write(msg)

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
