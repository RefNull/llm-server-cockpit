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
from textual.widgets import Button, DataTable, Label, RichLog, Static

from cockpit import update_check
from cockpit.widgets import CockpitScreenBase, SingleClickDataTable, selection_marker
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


class BuildsScreen(CockpitScreenBase):
    """The 'Installs' tab. One panel per backend the host's GPUs actually use (cuda, vulkan,
    etc. per hosts/<hostname>.yaml), plus one upstream-version-check panel.
    """

    # .panel / .panel-title / .button-row come from cockpit/widgets.py's SHARED_CSS
    # (CockpitApp.CSS) — only this screen's own rules live here.
    DEFAULT_CSS = """
    BuildsScreen {
        height: 1fr;
    }
    BuildsScreen #build-log {
        height: 10;
        border: round $accent;
        margin-top: 1;
        display: none;
    }
    BuildsScreen #build-status {
        text-style: italic;
        margin-bottom: 1;
    }
    BuildsScreen #selected-build {
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
        self.app_ref = app_ref
        self.backends = self._compute_backends()

        self._selected_backend: str | None = None
        self._selected_ref: str | None = None
        self._builds_cache: dict[str, list[dict]] = {backend: [] for backend in self.backends}
        self._build_in_progress = False
        # Ticked backends in the tick-able table below — same convention as the Downloads tab
        # (see cockpit/widgets.py's selection_marker()/SingleClickDataTable): plain DataTable +
        # a "[ ]"/"[x]" text marker, rebuilt on every toggle rather than mutated in place.
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
            yield Static("Manage llama.cpp builds, versions, and rollback controls.", classes="subtitle")

            with Vertical(id="update-panel", classes="panel"):
                yield Label("Components", classes="panel-title")
                ref = self.manifest.get("llama_cpp", {}).get("ref", "")
                yield Static(f"pinned llama.cpp ref: {self._short(ref)}", id="pinned-ref")
                yield Label("Tick a llama.cpp backend, then Update selected — llama-swap is informational here (updated from the Deploy tab):")
                # fixed_columns=2: column 0 is the tick marker, so pinning the identifier
                # ("Component") during horizontal scroll takes both (DESIGN.md §4.2).
                backends_table = SingleClickDataTable(
                    id="backends-table", zebra_stripes=True, classes="data-table", fixed_columns=2
                )
                backends_table.cursor_type = "row"
                yield backends_table
                with Horizontal(classes="button-row"):
                    yield Button("Check for updates", id="check-updates-btn", classes="thin-button")
                    yield Button("Update selected", id="update-selected-btn", variant="primary", classes="thin-button")

            if not self.backends:
                yield Static(
                    "host profile declares no GPU backends (hosts/*.yaml gpus[].backends) — "
                    "nothing to build.",
                    id="no-backends",
                    classes="panel",
                )
            else:
                with Vertical(id="builds-panel", classes="panel"):
                    with Horizontal(classes="button-row"):
                        yield Button("Roll back to selected", id="rollback-btn", variant="error", classes="thin-button")

                    yield Label("Retained builds (click a row to select it for rollback):")
                    yield SingleClickDataTable(id="builds-table", classes="data-table", fixed_columns=1)
                    yield Static("selected for rollback: (none — click a row above)", id="selected-build")

                    yield Label("Recent build history:")
                    yield SingleClickDataTable(id="history-table", classes="data-table", fixed_columns=1)

            yield Static("", id="build-status")
            yield RichLog(id="build-log", highlight=False, markup=False, max_lines=400)

    def on_mount(self) -> None:
        backends_table = self.query_one("#backends-table", SingleClickDataTable)
        backends_table.cursor_type = "row"
        backends_table.zebra_stripes = True
        backends_table.add_column("", width=3)
        backends_table.add_column("Component", width=22)
        backends_table.add_column("Pinned", width=12)
        backends_table.add_column("Update", width=34)

        if self.backends:
            builds_table = self.query_one("#builds-table", SingleClickDataTable)
            builds_table.cursor_type = "row"
            builds_table.zebra_stripes = True
            builds_table.add_column("Backend", width=12)
            builds_table.add_column("Ref", width=14)
            builds_table.add_column("Status", width=12)
            builds_table.add_column("Integrity", width=12)

            history_table = self.query_one("#history-table", SingleClickDataTable)
            history_table.zebra_stripes = True
            history_table.add_column("Backend", width=12)
            history_table.add_column("Timestamp (UTC)", width=22)
            history_table.add_column("Outcome", width=16)
            history_table.add_column("Detail", width=40)

        self._refresh_builds_and_history()
        self._refresh_backends_table()
        # Initial check on mount so the panel isn't blank; the button below is for
        # re-checking on demand afterwards — refresh (the 'r' binding) never re-hits it.
        self._run_update_check(notify_result=False)

    def on_refresh_requested(self) -> None:
        """Called by the app's global 'r' binding. Re-reads local build state only — never
        re-triggers the network update check (that stays a manual action, see point 2)."""
        self._refresh_builds_and_history()

    # ------------------------------------------------------------------ rendering

    def _refresh_builds_and_history(self) -> None:
        if not self.backends:
            return
        ref = self.manifest.get("llama_cpp", {}).get("ref", "")
        if self.query("#pinned-ref"):
            pinned_widget = self.query_one("#pinned-ref", Static)
            pinned_widget.update(f"pinned llama.cpp ref: {self._short(ref)}")
            pinned_widget.tooltip = ref

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
        self._update_selected_label()

        all_history: list[tuple[str, dict]] = []
        for backend in self.backends:
            history = build_step.read_build_history(self.host_profile, backend, limit=5)
            for h in history:
                all_history.append((backend, h))
        all_history.sort(key=lambda item: item[1].get("timestamp") or "", reverse=True)

        htable = self.query_one("#history-table", DataTable)
        htable.clear()
        for backend, h in all_history[:10]:
            detail = h.get("detail") or ""
            first_line = detail.splitlines()[0] if detail else ""
            ts = (h.get("timestamp") or "")[:19].replace("T", " ")
            outcome = h.get("outcome", "")
            style = (
                "green" if outcome == "smoke_pass"
                else "bold red" if outcome in ("smoke_failed", "build_failed")
                else ""
            )
            htable.add_row(Text(backend), Text(ts), Text(outcome, style=style), Text(first_line))

    def _refresh_backends_table(self) -> None:
        table = self.query_one("#backends-table", DataTable)
        table.clear()
        cpp_ref = self.manifest.get("llama_cpp", {}).get("ref", "")
        for backend in self.backends:
            table.add_row(
                selection_marker(backend in self._selected_backends),
                Text(f"llama.cpp ({backend})"),
                Text(self._short(cpp_ref)),
                self._format_update_cell(self._llama_cpp_check, shorten=True),
                key=backend,
            )
        swap_version = self.manifest.get("llama_swap", {}).get("version", "")
        table.add_row(
            Text("—", style="dim"),
            Text("llama-swap"),
            Text(swap_version),
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

    def _update_selected_label(self) -> None:
        if not self.query("#selected-build"):
            return
        label = self.query_one("#selected-build", Static)
        if self._selected_backend and self._selected_ref:
            label.update(
                f"selected for rollback: [{self._selected_backend}] {self._short(self._selected_ref)} (full: {self._selected_ref})"
            )
        else:
            label.update("selected for rollback: (none — click a row above)")

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
        self._update_selected_label()

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
            self._update_selected_label()

    # ------------------------------------------------------------------ button dispatch

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "check-updates-btn":
            self._run_update_check()
        elif button_id == "update-selected-btn":
            await self._handle_update_selected_press()
        elif button_id == "rollback-btn":
            await self._handle_rollback_press()

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
            self.app.call_from_thread(self._refresh_builds_and_history)
            self.app.call_from_thread(self._refresh_backends_table)

    def _set_building(self, active: bool) -> None:
        self._build_in_progress = active
        self.query_one("#build-status", Static).update("building... (see log below)" if active else "")
        update_btn = self.query("#update-selected-btn")
        if update_btn:
            update_btn.first(Button).disabled = active
        rollback_btn = self.query("#rollback-btn")
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
        self.app.call_from_thread(self._refresh_builds_and_history)

    # ------------------------------------------------------------------ upstream version check (network, off main thread)

    @work(thread=True)
    def _run_update_check(self, *, notify_result: bool = True) -> None:
        # DESIGN.md §6: a network check the operator can navigate away from reports its outcome
        # as a toast, not only in the table cell they may no longer be looking at. The one
        # exception is the automatic check on mount (notify_result=False) — nobody triggered it.
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
        btn = self.query("#check-updates-btn")
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
