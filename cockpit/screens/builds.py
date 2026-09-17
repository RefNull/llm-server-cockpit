"""Installs tab: per-backend llama.cpp build state, retained-build/rollback controls, and a
manual upstream-version check. A thin view + confirm layer over provision.steps.build and
cockpit.update_check — no build/rollback/version-check logic lives here, only rendering and
the ConfirmModal gate in front of every mutating call.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, RichLog, Static

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


# Matches only the `ref:` line under `llama_cpp:` — manifest.yaml has exactly one `ref:` key
# today, so anchoring on the key name (rather than tracking YAML section nesting) is sufficient
# and keeps this a plain line rewrite, not a parser. Preserves indentation and any trailing
# `# bNNNNN` comment unless a new one is supplied — never `yaml.safe_dump`, which would destroy
# both the header comment block above this line and this line's own trailing comment (plans/05
# Phase 2 item 8).
_MANIFEST_REF_RE = re.compile(r"^(\s*ref:\s*)([0-9a-fA-F]{7,40})(\s*#.*)?$", re.MULTILINE)


def _write_manifest_ref(path: Path, new_ref: str, new_comment: str | None = None) -> None:
    """Rewrite manifest.yaml's `llama_cpp.ref` line in place, byte-identical otherwise."""
    text = path.read_text()

    def _sub(m: re.Match[str]) -> str:
        comment = f"  # {new_comment}" if new_comment else (m.group(3) or "")
        return f"{m.group(1)}{new_ref}{comment}"

    new_text, count = _MANIFEST_REF_RE.subn(_sub, text, count=1)
    if count != 1:
        raise RuntimeError(f"manifest.yaml: could not find a llama_cpp.ref line to rewrite in {path}")
    path.write_text(new_text)


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


class BuildLogModal(ModalScreen[None]):
    """A view over BuildsScreen's build-log buffer, not the buffer itself.

    The buffer (`BuildsScreen._log_buffer`) survives independently of whether this modal is
    open — closing it (escape or ×, both live at all times, even mid-build: DESIGN.md §6.1
    explicitly anticipates the operator switching away from a long-running build, and a
    completion toast is how they learn it finished without this view open) loses nothing.
    Reopening via the 'Build log' button replays the buffer, then keeps receiving live appends.
    """

    BINDINGS = [("escape", "dismiss_modal", "Close")]

    DEFAULT_CSS = """
    BuildLogModal {
        align: center middle;
    }
    #build-log-dialog {
        width: 90%;
        height: 80%;
        border: thick $background 80%;
        background: $surface;
        padding: $space-normal $space-section;
    }
    #build-log-header {
        height: auto;
        margin-bottom: $space-normal;
    }
    #build-log-title {
        width: 1fr;
        text-style: bold;
    }
    #build-log-body {
        height: 1fr;
    }
    """

    def __init__(self, title: str, buffer: list[str]) -> None:
        super().__init__()
        self.log_title = title
        self._buffer = buffer

    def compose(self) -> ComposeResult:
        with Vertical(id="build-log-dialog"):
            with Horizontal(id="build-log-header"):
                yield Static(self.log_title, id="build-log-title")
                yield Button("×", id="build-log-close", classes="close-button", variant="error")
            yield RichLog(id="build-log-body", highlight=False, markup=False, max_lines=400)
            with Horizontal(classes="action-row-secondary"):
                yield Button("Close", id="btn-build-log-close-bottom", classes="thin-button")

    def on_mount(self) -> None:
        body = self.query_one("#build-log-body", RichLog)
        for line in self._buffer:
            body.write(line)

    def append(self, msg: str) -> None:
        # A line can arrive between push_screen and on_mount — measured: a build whose first
        # log line lands in that window raised NoMatches straight into the build worker, where
        # `except Exception` would have reported a perfectly good build as failed. Dropping it
        # here costs nothing: BuildsScreen._log_buffer is the source of truth and is appended
        # to before this call, so on_mount replays the line in full.
        try:
            self.query_one("#build-log-body", RichLog).write(msg)
        except NoMatches:
            pass

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id in ("build-log-close", "btn-build-log-close-bottom"):
            self.dismiss(None)


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


class ChangeVersionModal(ModalScreen[str | None]):
    """Version picker for BuildsScreen's "Change version…" action (plans/05 Phase 2 item 4).

    Always dismisses with a resolved commit SHA (or None on cancel) — never a bare tag — so the
    caller can write it straight into manifest.yaml's `ref:` line. Sources, in the order the
    operator asked for ("go back" first): build-history outcomes and on-disk retained builds
    (both already carry real SHAs), then upstream releases (network, backgrounded so opening the
    modal never blocks on it — a release's tag is resolved to a SHA lazily, only if it's the one
    picked), plus a manual SHA entry for anything not already known locally.

    Not a CockpitScreenBase — a ModalScreen is a different widget-tree root, same reason
    RetainedBuildsModal implements its own confirm-free select-and-dismiss dispatch here. The
    confirm step belongs to the caller (BuildsScreen._confirm_and_change_version): it is the
    same confirm §3's "Update to latest" already uses, and this modal's only job is to return a
    ref.
    """

    BINDINGS = [("escape", "dismiss_modal", "Close")]

    DEFAULT_CSS = """
    ChangeVersionModal {
        align: center middle;
    }
    #version-dialog {
        width: 90%;
        height: 80%;
        border: thick $background 80%;
        background: $surface;
        padding: $space-normal $space-section;
    }
    #version-header {
        height: auto;
        margin-bottom: $space-normal;
    }
    #version-title {
        width: 1fr;
        text-style: bold;
    }
    #version-table {
        height: 1fr;
        margin-bottom: $space-normal;
    }
    #version-manual-row {
        margin-bottom: $space-normal;
    }
    #version-error {
        color: $error;
        height: auto;
        margin-bottom: $space-normal;
    }
    """

    def __init__(self, host_profile: dict, manifest: dict, backends: list[str]) -> None:
        super().__init__()
        self.host_profile = host_profile
        self.manifest = manifest
        self.backends = backends
        # row key (a real SHA, or an unresolved release tag) -> {"kind", "sources", "details"}
        self._rows: dict[str, dict] = {}
        # resolved SHA -> the release tag it came from — read by the caller after dismiss so
        # manifest.yaml's trailing comment can record it (item 8's "when known").
        self.resolved_tags: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="version-dialog"):
            with Horizontal(id="version-header"):
                yield Static("Change Version", id="version-title")
                yield Button("×", id="version-close", classes="close-button", variant="error")
            table = SingleClickDataTable(id="version-table", zebra_stripes=True)
            table.cursor_type = "row"
            yield table
            yield Static("", id="version-error")
            with Horizontal(id="version-manual-row", classes="inline-row"):
                yield Input(placeholder="or paste a full SHA", id="f-manual-ref")
                yield Button("Use this SHA", id="btn-manual-select")  # inline archetype (DESIGN.md §9): bare Button inside .inline-row
            with Horizontal(classes="action-row-secondary"):
                yield Button("Cancel", id="btn-version-cancel", classes="thin-button")

    def on_mount(self) -> None:
        table = self.query_one("#version-table", SingleClickDataTable)
        table.add_column("Source", width=12)
        table.add_column("Ref", width=14)
        table.add_column("Detail", width=44)
        table.add_action_column(TableAction("select", "Select"))
        self._populate_local(table)
        self._fetch_releases(table)

    def _add_row(self, key: str, source: str, detail: str, *, kind: str) -> None:
        row = self._rows.setdefault(key, {"kind": kind, "sources": set(), "details": []})
        row["sources"].add(source)
        row["details"].append(detail)
        if kind == "sha":  # a real SHA always wins over a same-string tag coincidence
            row["kind"] = "sha"

    def _refresh_table(self, table: SingleClickDataTable) -> None:
        if not self.is_mounted:
            return
        table.clear()
        for key, row in self._rows.items():
            display_ref = _short(key) if row["kind"] == "sha" else key
            table.add_row(
                Text("/".join(sorted(row["sources"]))),
                Text(display_ref),
                Text("; ".join(row["details"])[:200]),
                *table.action_cells(key),
                key=key,
            )

    def _populate_local(self, table: SingleClickDataTable) -> None:
        for backend in self.backends:
            for h in build_step.read_build_history(self.host_profile, backend, limit=10):
                ref = h.get("ref") or ""
                if ref:
                    self._add_row(ref, "history", f"{backend}: {h.get('outcome', '')}", kind="sha")
            for b in build_step.list_builds(self.host_profile, backend):
                ref = b.get("ref") or ""
                if not ref:
                    continue
                state = "current" if b.get("current") else "retained"
                if not b.get("sane"):
                    state += " NOT SANE"
                self._add_row(ref, "installed", f"{backend}: {state}", kind="sha")
        self._refresh_table(table)

    @work(thread=True)
    def _fetch_releases(self, table: SingleClickDataTable) -> None:
        repo = self.manifest.get("llama_cpp", {}).get("repo", "")
        if not repo:
            return
        result = update_check.check_releases(repo)
        if not result.get("ok"):
            return
        for r in result["releases"]:
            tag = r.get("tag_name") or ""
            if tag:
                self._add_row(tag, "upstream", f"released {(r.get('published_at') or '')[:10]}", kind="tag")
        self.app.call_from_thread(self._refresh_table, table)

    def _set_error(self, message: str) -> None:
        if self.is_mounted:
            self.query_one("#version-error", Static).update(message)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id in ("version-close", "btn-version-cancel"):
            self.dismiss(None)
        elif event.button.id == "btn-manual-select":
            self._select_manual()

    def _select_manual(self) -> None:
        value = self.query_one("#f-manual-ref", Input).value.strip()
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", value):
            self._set_error("not a SHA — paste the full (or a >=7-char short) commit hash")
            return
        self.dismiss(value)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    @work
    async def _on_table_action_invoked(self, event: TableActionInvoked) -> None:
        event.stop()
        row = self._rows.get(event.row_key)
        if row is None:
            return
        if row["kind"] == "sha":
            self.dismiss(event.row_key)
            return
        self._resolve_tag_and_dismiss(event.row_key)

    @work(thread=True)
    def _resolve_tag_and_dismiss(self, tag: str) -> None:
        try:
            sha = update_check.resolve_ref_sha(self.manifest["llama_cpp"]["repo"], tag)
        except Exception as e:
            self.app.call_from_thread(self.app.notify, f"could not resolve {tag}: {e}", severity="error")
            return
        self.resolved_tags[sha] = tag
        self.app.call_from_thread(self.dismiss, sha)


class BuildsScreen(CockpitScreenBase):
    """The 'Installs' tab. One panel per backend the host's GPUs actually use (cuda, vulkan,
    etc. per hosts/<hostname>.yaml), plus one upstream-version-check panel.
    """

    DEFAULT_CSS = """
    BuildsScreen {
        height: 1fr;
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

        # Which backends currently have a build worker running — a set, not a bool: the guard
        # in handle_table_action derives "a build is already in progress" from non-emptiness,
        # and it stays a hard guard (never widened to allow concurrent builds) because every
        # backend's build shares one source checkout (state_dir/src/llama.cpp, build.py:235)
        # that a second, concurrent build would race on.
        self._building_backends: set[str] = set()
        self._llama_cpp_check: dict | None = None
        self._llama_swap_check: dict | None = None
        # Owned by the screen, not the modal: the log survives a closed/reopened BuildLogModal
        # and a build the operator has switched tabs away from (DESIGN.md §6.1).
        self._log_buffer: list[str] = []
        self._log_modal: BuildLogModal | None = None

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

            # Two rows, split by what the operator is deciding, not by width — both rows fit
            # one line at normal widths and both stack under Screen.-narrow (SHARED_CSS,
            # DESIGN.md §9 "Action-row overflow at narrow widths"). Row 1 is the decision
            # (which version); row 2 is the record (what has actually been built). Build itself
            # is deliberately not here — it's per-backend, so it lives in the table.
            with Horizontal(classes="action-row-secondary"):
                yield Button("Update to latest", id="btn-update-to-latest", classes="thin-button", disabled=True)
                yield Button("Change version…", id="btn-change-version", classes="thin-button")
                yield Button("Check for Updates", id="btn-check-updates", classes="thin-button")
            with Horizontal(classes="action-row-secondary"):
                yield Button("Retained Builds", id="btn-retained-builds", classes="thin-button")
                yield Button("Build History", id="btn-build-history", classes="thin-button")
                yield Button("Build log", id="btn-build-log", classes="thin-button")

    def on_mount(self) -> None:
        backends_table = self.query_one("#backends-table", SingleClickDataTable)
        backends_table.cursor_type = "row"
        backends_table.zebra_stripes = True
        backends_table.add_column("Component", width=22)
        backends_table.add_column("Version", width=14)
        backends_table.add_column("Installed", width=14)
        # 22 + 14 + 14 + 35 + 11 = 96 content, render 96 + 2*5 = 106 (DESIGN.md §4, updated for
        # the Phase 2 5-column shape — Version/Installed replace the old single manifest-pin
        # column). Status keeps enough room for both the useful case ("update available (latest
        # d1d3c33)") and the failure case ("couldn't check (HTTP Error 403: rate limit
        # exceeded)").
        backends_table.add_column("Status", width=35)
        backends_table.add_action_column(
            TableAction(
                "build",
                self._build_label,
                width=11,  # fits "[ Rebuild ]" (7+4); "[ Build ]" (9) fits the same column
                confirm="{action} llama.cpp ({row})? This can take several minutes.",
                requires_root=True,
                available=lambda row_key: row_key in self.backends,
            )
        )

        # No data read here — ensure_first_view() does it when this tab is first shown.

    def on_refresh_requested(self) -> None:
        """Called by the app's global 'r' binding. Re-reads local (manifest/check) state —
        never re-triggers the network update check."""
        # The one place BuildsScreen picks up a manifest.yaml pin change made from any screen —
        # dashboard.py:147 is the precedent for this getattr shape.
        self.manifest = getattr(self.app_ref, "manifest", self.manifest)
        self._refresh_backends_table()
        self._update_action_buttons_state()

    def on_first_view(self) -> None:
        """The one screen whose first view does more than a refresh: the upstream version
        check is a network call, so it runs once when the tab is first opened and then only on
        the explicit button — never on 'r'."""
        self._refresh_backends_table()
        self._run_update_check(notify_result=False, force=False)

    # ------------------------------------------------------------------ rendering

    def _build_label(self, row_key: str) -> str:
        """Rebuild when the selected version is already built and sane for this backend —
        pressing Build on the current version then literally is a rebuild (plans/05 Phase 2
        item 5), rather than the old silent no-op _prefix_ready used to produce."""
        ref = self.manifest.get("llama_cpp", {}).get("ref", "")
        for b in build_step.list_builds(self.host_profile, row_key):
            if b.get("ref") == ref and b.get("sane"):
                return "Rebuild"
        return "Build"

    def _installed_ref(self, backend: str) -> str | None:
        for b in build_step.list_builds(self.host_profile, backend):
            if b.get("current"):
                return b.get("ref")
        return None

    def _installed_cell(self, backend: str, selected_ref: str) -> Text:
        """§0b: `current` is a per-backend symlink target, independent of the (global) pin —
        this is the cell that would have told QA the truth immediately."""
        current_ref = self._installed_ref(backend)
        if current_ref is None:
            return Text("not built", style="dim")
        if current_ref == selected_ref:
            return Text(_short(current_ref), style="green")
        return Text(_short(current_ref), style="bold yellow")

    def _refresh_backends_table(self) -> None:
        table = self.query_one("#backends-table", SingleClickDataTable)
        table.clear()
        cpp_ref = self.manifest.get("llama_cpp", {}).get("ref", "")
        for backend in self.backends:
            status_cell = (
                Text("building…", style="dim")
                if backend in self._building_backends
                else self._format_update_cell(self._llama_cpp_check, shorten=True)
            )
            table.add_row(
                Text(f"llama.cpp ({backend})"),
                Text(_short(cpp_ref)),
                self._installed_cell(backend, cpp_ref),
                status_cell,
                *table.action_cells(backend),
                key=backend,
            )
        swap_version = self.manifest.get("llama_swap", {}).get("version", "")
        table.add_row(
            Text("llama-swap"),
            Text(swap_version),
            Text("—", style="dim"),
            self._format_update_cell(self._llama_swap_check, shorten=False),
            # The llama-swap row is informational only — updated from the Deploy tab's deploy
            # action, not from here — so its Build cell is always blank ("llama-swap" is never
            # in self.backends).
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

    def _update_action_buttons_state(self) -> None:
        if not self.is_mounted:
            return
        can_update = bool(
            self._llama_cpp_check
            and self._llama_cpp_check.get("ok")
            and self._llama_cpp_check.get("update_available")
        )
        btn = self.query("#btn-update-to-latest")
        if btn:
            btn.first(Button).disabled = not can_update

    # ------------------------------------------------------------------ per-row action dispatch

    async def handle_table_action(self, action_id: str, row_key: str, table: DataTable) -> None:
        if action_id != "build":
            return
        if self._building_backends:
            self.notify("a build is already in progress", severity="warning")
            return
        force = self._build_label(row_key) == "Rebuild"
        self._log_buffer = []
        self._open_log_modal(f"Build log — {row_key}")
        self._run_build([row_key], force=force)

    # ------------------------------------------------------------------ button dispatch

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "btn-check-updates":
            self._run_update_check()
        elif button_id == "btn-update-to-latest":
            self._confirm_and_update_to_latest()
        elif button_id == "btn-change-version":
            self._confirm_and_change_version()
        elif button_id == "btn-build-history":
            self.app.push_screen(BuildHistoryModal(self.host_profile, self.backends))
        elif button_id == "btn-retained-builds":
            self.app.push_screen(RetainedBuildsModal(self.host_profile, self.backends, self.runner))
        elif button_id == "btn-build-log":
            self._open_log_modal("Build log")

    def _open_log_modal(self, title: str) -> None:
        modal = BuildLogModal(title, self._log_buffer)
        self._log_modal = modal
        self.app.push_screen(modal, callback=lambda _: self._clear_log_modal(modal))

    def _clear_log_modal(self, modal: "BuildLogModal") -> None:
        if self._log_modal is modal:
            self._log_modal = None

    # ------------------------------------------------------------------ version selection (writes manifest.yaml, never builds)

    @work
    async def _confirm_and_update_to_latest(self) -> None:
        """Writes llama_cpp.ref to the SHA the last check found upstream. Does not build —
        that is a separate act the operator takes per backend from the table (plans/05 Phase 2
        item 3)."""
        check = self._llama_cpp_check
        if not check or not check.get("ok") or not check.get("update_available"):
            self.notify("no update to apply — run 'Check for Updates' first", severity="warning")
            return
        old_ref = self.manifest.get("llama_cpp", {}).get("ref", "")
        new_ref = check["latest"]
        message = (
            f"Update llama.cpp version {_short(old_ref)} → {_short(new_ref)} "
            f"(latest as of {check.get('latest_date', 'unknown date')})? "
            "Writes manifest.yaml; does not build."
        )
        confirmed = await self.app.push_screen_wait(ConfirmModal(message, confirm_label="Update Version", danger=True))
        if not confirmed:
            return
        self._write_pin(new_ref)
        # We know the relationship without a network call: the pin now equals what the last
        # check called "latest", so update_available flips locally — on_refresh_requested's
        # no-network contract stays intact, this is just reflecting what we already learned.
        self._llama_cpp_check = {**check, "pinned": new_ref, "update_available": False}
        self._refresh_backends_table()
        self._update_action_buttons_state()
        self.notify(f"llama.cpp pin updated to {_short(new_ref)}")

    @work
    async def _confirm_and_change_version(self) -> None:
        """Opens the version picker and writes whatever it returns. Does not build."""
        modal = ChangeVersionModal(self.host_profile, self.manifest, self.backends)
        new_ref = await self.app.push_screen_wait(modal)
        if new_ref is None:
            return
        old_ref = self.manifest.get("llama_cpp", {}).get("ref", "")
        already_built = sorted(
            backend for backend in self.backends
            for b in build_step.list_builds(self.host_profile, backend)
            if b.get("ref") == new_ref and b.get("sane")
        )
        message = f"Update llama.cpp version {_short(old_ref)} → {_short(new_ref)}? Writes manifest.yaml; does not build."
        if already_built:
            message += (
                f" Already built and sane for {', '.join(already_built)} — use Retained Builds "
                "to activate it there instead of building again."
            )
        confirmed = await self.app.push_screen_wait(ConfirmModal(message, confirm_label="Update Version", danger=True))
        if not confirmed:
            return
        self._write_pin(new_ref, new_comment=modal.resolved_tags.get(new_ref))
        self._refresh_backends_table()

    def _write_pin(self, new_ref: str, *, new_comment: str | None = None) -> None:
        _write_manifest_ref(self.repo_root / "manifest.yaml", new_ref, new_comment)
        # Reload through the app, not a private load here: cockpit/app.py hands the same
        # manifest dict to every screen's constructor, so rebinding only self.manifest would
        # leave every other tab holding the pre-write dict until the app restarts. reload_manifest
        # is the third sibling to reload_models()/reload_scripts() (cockpit/app.py).
        self.app_ref.reload_manifest()
        self.manifest = getattr(self.app_ref, "manifest", self.manifest)

    # ------------------------------------------------------------------ build (blocking, off main thread)

    @work(thread=True)
    def _run_build(self, backends: list[str], *, force: bool = False) -> None:
        self.app.call_from_thread(self._set_building, backends, True)
        self.app.call_from_thread(self._refresh_backends_table)

        handler = _BuildLogHandler(self)
        provision_logger = logging.getLogger("provision")
        prior_level = provision_logger.level
        if provision_logger.level == logging.NOTSET or provision_logger.level > logging.INFO:
            provision_logger.setLevel(logging.INFO)
        provision_logger.addHandler(handler)

        # A fresh Runner, not a mutation of self.privileged_runner: that property can fall back
        # to self.runner, which is shared app-wide (widgets.py:862-870) — setting on_output on
        # it would leak this build's subprocess lines into every other screen's use of the
        # shared Runner. sudo is only a bool; the credential lives in the OS sudo timestamp
        # cache, not on the Runner object, so a new instance carrying the same flags is
        # equivalent and cannot leak.
        pr = self.privileged_runner
        runner = Runner(dry_run=pr.dry_run, sudo=pr.sudo, on_output=self._on_build_output)

        try:
            build_step.run(
                self.host_profile, self.manifest, self.models, runner, self.repo_root,
                backends=backends, force=force,
            )
        except SystemExit as e:
            self.app.call_from_thread(self.app.notify, f"build failed: {e}", severity="error")
        except Exception as e:  # never let a build-time exception crash the whole TUI
            self.app.call_from_thread(self.app.notify, f"build failed: {e}", severity="error")
        else:
            self.app.call_from_thread(self.app.notify, "build finished")
        finally:
            provision_logger.removeHandler(handler)
            provision_logger.setLevel(prior_level)
            self.app.call_from_thread(self._set_building, backends, False)
            # Re-reads list_builds() for these backends (via _refresh_backends_table's Installed
            # column), never the network check — on_refresh_requested's no-network contract
            # stands (plans/05 Phase 2 item 6).
            self.app.call_from_thread(self._refresh_backends_table)

    def _on_build_output(self, line: str) -> None:
        """Runs on the build's worker thread (called from Runner._run_streaming) — bridge to
        the main thread exactly as _BuildLogHandler.emit does."""
        self.app.call_from_thread(self._append_build_log, f"$ {line}")

    def _set_building(self, backends: list[str], active: bool) -> None:
        if active:
            self._building_backends.update(backends)
        else:
            self._building_backends.difference_update(backends)

    def _append_build_log(self, msg: str) -> None:
        self._log_buffer.append(msg)
        if self._log_modal is not None:
            self._log_modal.append(msg)

    # ------------------------------------------------------------------ upstream version check (network, off main thread)

    @work(thread=True)
    def _run_update_check(self, *, notify_result: bool = True, force: bool = True) -> None:
        """`force=False` is served from update_check's 24h cache. Opening this tab used to fire
        two GitHub calls every time, against a 60/hour unauthenticated budget — enough to start
        returning 403s that rendered as "couldn't check" and read like a network fault. The
        button below always forces, because that is what pressing it means."""
        self.app.call_from_thread(self._set_checking_status, True)
        try:
            results = update_check.check_all(self.manifest, force=force)
            result_cpp, result_swap = results["llama_cpp"], results["llama_swap"]
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
            self._update_action_buttons_state()

    def _apply_update_check_results(self, result_cpp: dict, result_swap: dict) -> None:
        if not self.is_mounted:
            return
        self._llama_cpp_check = result_cpp
        self._llama_swap_check = result_swap
        self._refresh_backends_table()
        self._update_action_buttons_state()
