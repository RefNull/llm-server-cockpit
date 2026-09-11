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
from textual.widget import Widget
from textual.widgets import Button, DataTable, Label, RichLog, Static

from cockpit import update_check
from cockpit.widgets import ConfirmModal
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


class BuildsScreen(Widget):
    """The 'Installs' tab. One panel per backend the host's GPUs actually use (cuda, vulkan,
    etc. per hosts/<hostname>.yaml), plus one upstream-version-check panel.
    """

    # .panel / .panel-title / .button-row come from cockpit/widgets.py's SHARED_CSS
    # (CockpitApp.CSS) — only this screen's own rules live here.
    DEFAULT_CSS = """
    BuildsScreen {
        height: 1fr;
    }
    BuildsScreen DataTable {
        height: auto;
        max-height: 10;
        margin-bottom: 1;
    }
    BuildsScreen #build-log {
        height: 10;
        border: round $accent;
        margin-top: 1;
    }
    BuildsScreen #build-status {
        text-style: italic;
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

        # ref -> None once we know it's stale; populated by _refresh_builds_and_history.
        self._selected_build: dict[str, str | None] = {backend: None for backend in self.backends}
        self._builds_cache: dict[str, list[dict]] = {backend: [] for backend in self.backends}
        self._build_in_progress = False

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
                yield Label("Upstream version check", classes="panel-title")
                yield Static("llama.cpp: not checked yet", id="update-llama-cpp")
                yield Static("llama-swap: not checked yet", id="update-llama-swap")
                yield Button("Check for updates", id="check-updates-btn")

            if not self.backends:
                yield Static(
                    "host profile declares no GPU backends (hosts/*.yaml gpus[].backends) — "
                    "nothing to build.",
                    id="no-backends",
                    classes="panel",
                )

            for backend in self.backends:
                with Vertical(id=f"backend-panel-{backend}", classes="panel"):
                    yield Label(f"Backend: {backend}", classes="panel-title")
                    yield Static("", id=f"pinned-{backend}")
                    with Horizontal(classes="button-row"):
                        yield Button(f"Build {backend}", id=f"build-{backend}", variant="primary")
                        yield Button("Roll back to selected", id=f"rollback-{backend}", variant="warning")
                    yield Label("Retained builds (click a row to select it for rollback):")
                    yield DataTable(id=f"builds-table-{backend}")
                    yield Static("selected for rollback: (none)", id=f"selected-{backend}")
                    yield Label("Recent build history:")
                    yield DataTable(id=f"history-table-{backend}")

            yield Static("", id="build-status")
            yield RichLog(id="build-log", highlight=False, markup=False, max_lines=400)

    def on_mount(self) -> None:
        for backend in self.backends:
            builds_table = self.query_one(f"#builds-table-{backend}", DataTable)
            builds_table.cursor_type = "row"
            builds_table.zebra_stripes = True
            builds_table.add_columns("ref", "current", "sane")

            history_table = self.query_one(f"#history-table-{backend}", DataTable)
            history_table.zebra_stripes = True
            history_table.add_columns("timestamp (UTC)", "outcome", "detail")

        self._refresh_builds_and_history()
        # Initial check on mount so the panel isn't blank; the button below is for
        # re-checking on demand afterwards — refresh (the 'r' binding) never re-hits it.
        self._run_update_check()

    def on_refresh_requested(self) -> None:
        """Called by the app's global 'r' binding. Re-reads local build state only — never
        re-triggers the network update check (that stays a manual action, see point 2)."""
        self._refresh_builds_and_history()

    # ------------------------------------------------------------------ rendering

    def _refresh_builds_and_history(self) -> None:
        ref = self.manifest["llama_cpp"]["ref"]
        for backend in self.backends:
            pinned_widget = self.query_one(f"#pinned-{backend}", Static)
            pinned_widget.update(f"pinned llama.cpp ref: {self._short(ref)}")
            pinned_widget.tooltip = ref

            builds = build_step.list_builds(self.host_profile, backend)
            self._builds_cache[backend] = builds
            table = self.query_one(f"#builds-table-{backend}", DataTable)
            table.clear()
            for b in builds:
                current_cell = Text("current", style="bold green") if b["current"] else Text("")
                sane_cell = Text("ok", style="green") if b["sane"] else Text("NOT SANE", style="bold red")
                table.add_row(Text(self._short(b["ref"])), current_cell, sane_cell, key=b["ref"])

            if self._selected_build.get(backend) not in {b["ref"] for b in builds}:
                self._selected_build[backend] = None
            self._update_selected_label(backend)

            history = build_step.read_build_history(self.host_profile, backend, limit=5)
            htable = self.query_one(f"#history-table-{backend}", DataTable)
            htable.clear()
            for h in history:
                detail = h.get("detail") or ""
                first_line = detail.splitlines()[0] if detail else ""
                ts = (h.get("timestamp") or "")[:19].replace("T", " ")
                outcome = h.get("outcome", "")
                style = (
                    "green" if outcome == "smoke_pass"
                    else "bold red" if outcome in ("smoke_failed", "build_failed")
                    else ""
                )
                htable.add_row(Text(ts), Text(outcome, style=style), Text(first_line))

    def _update_selected_label(self, backend: str) -> None:
        label = self.query_one(f"#selected-{backend}", Static)
        ref = self._selected_build.get(backend)
        if ref:
            label.update(f"selected for rollback: {self._short(ref)} (full: {ref})")
        else:
            label.update("selected for rollback: (none — click a row above)")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        table_id = event.data_table.id or ""
        if not table_id.startswith("builds-table-"):
            return
        backend = table_id.removeprefix("builds-table-")
        self._selected_build[backend] = event.row_key.value if event.row_key is not None else None
        self._update_selected_label(backend)

    # ------------------------------------------------------------------ button dispatch

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "check-updates-btn":
            self._run_update_check()
        elif button_id.startswith("build-"):
            await self._handle_build_press(button_id.removeprefix("build-"))
        elif button_id.startswith("rollback-"):
            await self._handle_rollback_press(button_id.removeprefix("rollback-"))

    async def _handle_build_press(self, backend: str) -> None:
        if self._build_in_progress:
            self.notify("a build is already in progress", severity="warning")
            return

        ref = self.manifest["llama_cpp"]["ref"]
        message = f"Build {backend} at pinned ref {ref}? This can take several minutes."
        # build.run() builds+smoke-tests+swaps every backend the host needs in one pass, not
        # just the backend whose button was pressed — say so, rather than letting the
        # operator think this only touches `backend`.
        if len(self.backends) > 1:
            message += f" This builds every backend this host needs: {', '.join(self.backends)}."
        if self.runner.dry_run:
            message += " (dry-run preview only — no real build will run)"

        confirmed = await self.app.push_screen_wait(ConfirmModal(message, confirm_label="Build"))
        if confirmed:
            self._run_build()

    async def _handle_rollback_press(self, backend: str) -> None:
        target_ref = self._selected_build.get(backend)
        if not target_ref:
            self.notify(f"select a build in the {backend} table first", severity="warning")
            return

        matching = next((b for b in self._builds_cache.get(backend, []) if b["ref"] == target_ref), None)
        if matching is None:
            self.notify("selected build is no longer available — refresh and try again", severity="warning")
            return
        if matching["current"]:
            self.notify(f"{backend}: {self._short(target_ref)} is already current", severity="information")
            return

        message = (
            f"Roll back {backend} to {target_ref}? This points 'current' at an "
            f"already-built, retained prefix — no rebuild, no re-smoke-test."
        )
        if self.runner.dry_run:
            message += " (dry-run preview only — no real change will happen)"

        confirmed = await self.app.push_screen_wait(
            ConfirmModal(message, confirm_label="Roll back", danger=True)
        )
        if confirmed:
            self._run_rollback(backend, target_ref)

    # ------------------------------------------------------------------ build (blocking, off main thread)

    @work(thread=True)
    def _run_build(self) -> None:
        self.app.call_from_thread(self._set_building, True)

        handler = _BuildLogHandler(self)
        provision_logger = logging.getLogger("provision")
        prior_level = provision_logger.level
        if provision_logger.level == logging.NOTSET or provision_logger.level > logging.INFO:
            provision_logger.setLevel(logging.INFO)
        provision_logger.addHandler(handler)

        try:
            build_step.run(self.host_profile, self.manifest, self.models, self.runner, self.repo_root)
        except SystemExit as e:
            self.app.call_from_thread(self.app.notify, f"build failed: {e}", severity="error")
        except Exception as e:  # never let a build-time exception crash the whole TUI
            self.app.call_from_thread(self.app.notify, f"build failed: {e}", severity="error")
        else:
            done_msg = "build finished"
            if self.runner.dry_run:
                done_msg += " (dry-run preview only — nothing was actually built)"
            self.app.call_from_thread(self.app.notify, done_msg)
        finally:
            provision_logger.removeHandler(handler)
            provision_logger.setLevel(prior_level)
            self.app.call_from_thread(self._set_building, False)
            self.app.call_from_thread(self._refresh_builds_and_history)

    def _set_building(self, active: bool) -> None:
        self._build_in_progress = active
        self.query_one("#build-status", Static).update("building... (see log below)" if active else "")
        for backend in self.backends:
            self.query_one(f"#build-{backend}", Button).disabled = active
            self.query_one(f"#rollback-{backend}", Button).disabled = active
        if active:
            self.query_one("#build-log", RichLog).clear()

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
        if self.runner.dry_run:
            msg += " (dry-run preview only — no real change was made)"
        self.app.call_from_thread(self.app.notify, msg)
        self.app.call_from_thread(self._refresh_builds_and_history)

    # ------------------------------------------------------------------ upstream version check (network, off main thread)

    @work(thread=True)
    def _run_update_check(self) -> None:
        self.app.call_from_thread(self._set_checking_status, True)
        result_cpp = update_check.check_llama_cpp(self.manifest)
        result_swap = update_check.check_llama_swap(self.manifest)
        self.app.call_from_thread(self._apply_update_check_results, result_cpp, result_swap)
        self.app.call_from_thread(self._set_checking_status, False)

    def _set_checking_status(self, checking: bool) -> None:
        self.query_one("#check-updates-btn", Button).disabled = checking
        if checking:
            self.query_one("#update-llama-cpp", Static).update("llama.cpp: checking...")
            self.query_one("#update-llama-swap", Static).update("llama-swap: checking...")

    def _apply_update_check_results(self, result_cpp: dict, result_swap: dict) -> None:
        self.query_one("#update-llama-cpp", Static).update(self._format_check("llama.cpp", result_cpp))
        self.query_one("#update-llama-swap", Static).update(self._format_check("llama-swap", result_swap))

    def _format_check(self, name: str, result: dict) -> str:
        if not result.get("ok"):
            return f"{name}: couldn't check ({result.get('error')})"

        shorten = name == "llama.cpp"  # llama.cpp pins a full commit SHA; llama-swap pins a release tag
        pinned = self._short(result["pinned"]) if shorten else result["pinned"]
        latest = self._short(result["latest"]) if shorten else result["latest"]
        marker = " — UPDATE AVAILABLE" if result["update_available"] else " — up to date"
        date_suffix = f" (latest {result['latest_date']})" if result.get("latest_date") else ""
        return f"{name}: pinned {pinned} | latest {latest}{date_suffix}{marker}"
