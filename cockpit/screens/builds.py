"""Installs tab: per-backend llama.cpp build state, a per-backend Builds list (activate/config/
log/remove a specific build), and a manual upstream-version check. A thin view + confirm layer
over provision.steps.build and cockpit.update_check — no build/rollback/version-check logic
lives here, only rendering and the ConfirmModal gate in front of every mutating call.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
from pathlib import Path

import yaml
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, RichLog, Static, TextArea

from cockpit import update_check
from cockpit.widgets import (
    CockpitScreenBase,
    ConfirmModal,
    InfoModal,
    SingleClickDataTable,
    TableAction,
    TableActionInvoked,
    acquire_sudo,
    escape_markup,
    root_note,
)
from provision.common import Runner
from provision.steps import build as build_step
from provision.steps import swap as swap_step

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

# Same bound _MANIFEST_REF_RE already puts on the *existing* line, and the same convention
# ChangeVersionModal._select_manual uses for a pasted ref — one definition of "looks like a
# commit SHA", not a second one invented here.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _write_manifest_ref(path: Path, new_ref: str, new_comment: str | None = None) -> None:
    """Rewrite manifest.yaml's `llama_cpp.ref` line in place, byte-identical otherwise.

    Validates `new_ref` BEFORE touching the file (plans/06 §0b, a live bug): `_MANIFEST_REF_RE`
    only ever constrained the line being *replaced*, never the incoming value, so a caller that
    handed this a build directory id (e.g. "build3" — meaningful since Phase 1, when a build's
    directory name stopped being its version) would substitute cleanly and report success while
    writing a non-SHA into manifest.yaml's `ref:` line. Same validate-before-write discipline
    `_write_manifest_llama_swap_version` already uses for its own pin.
    """
    if not _SHA_RE.fullmatch(new_ref):
        raise ValueError(
            f"{new_ref!r} does not look like a commit SHA (expected 7-40 hex chars) — "
            "refusing to write manifest.yaml"
        )
    text = path.read_text()

    def _sub(m: re.Match[str]) -> str:
        comment = f"  # {new_comment}" if new_comment else (m.group(3) or "")
        return f"{m.group(1)}{new_ref}{comment}"

    new_text, count = _MANIFEST_REF_RE.subn(_sub, text, count=1)
    if count != 1:
        raise RuntimeError(f"manifest.yaml: could not find a llama_cpp.ref line to rewrite in {path}")
    path.write_text(new_text)


def _yaml_quote(value: str) -> str:
    """Double-quote a scalar for a manifest.yaml list item, matching the file's existing
    cmake_flags style (`- "-DGGML_CUDA=ON"`) rather than leaving it bare — a bare `-D...`
    value is valid YAML today but not worth relying on for anything an operator typed."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_manifest_cmake_flags(
    path: Path, backend: str, new_flags: list[str], *, clear_example: bool = False
) -> None:
    """Rewrite manifest.yaml's `backends.<backend>.cmake_flags` list in place — the list-block
    equivalent of `_write_manifest_ref`'s scalar-line rewrite, for the same reason: never
    `yaml.safe_dump`, which would destroy the header comment block and every inline comment
    (e.g. `apt_packages: []  # CUDA toolkit presence is checked...`) on every backend, not
    only the one being edited.

    Order-independent by construction, not by assumption: the backend's own block is found by
    its key line and bounded by the next sibling key at the same indent (never by assuming
    cmake_flags is that block's first child key), and cmake_flags is then located *inside*
    that bounded slice — so a backend that lists apt_packages before cmake_flags, or sits
    between two other backends, is handled the same way as the first-key case.

    manifest.yaml uses a 2-space indent per nesting level throughout (`backends:` at column 0,
    `<backend>:` at column 2, its keys at column 4, list items at column 6) — that file-wide
    style, not a per-call guess, is what fixes the indent widths below.

    `clear_example=True` additionally strips that backend's own `example: true` sibling key
    (if present) from the same bounded block — the mark means "still stock", and an accepted
    cmake_flags edit through this function is exactly the thing that makes that false. A
    recipe with no `example` key is untouched either way; `clear_example=False` (the default)
    never looks for the key at all, so a caller rewriting flags for a reason unrelated to the
    example mark (there is none today, but the parameter exists so that stays true) leaves it
    alone by construction, not by omission.
    """
    text = path.read_text()

    backend_key_re = re.compile(rf"^( {{2}}){re.escape(backend)}:[ \t]*\n", re.MULTILINE)
    key_match = backend_key_re.search(text)
    if key_match is None:
        raise RuntimeError(f"manifest.yaml: no backends.{backend} block found in {path}")
    indent = key_match.group(1)  # "  " — this backend's own key indent
    block_start = key_match.end()

    # Bounded by the next line at the *same* indent (the next backend key, or a dedent back to
    # "backends:"/EOF) — never by counting a fixed number of lines, so sibling key order and
    # count never matter.
    next_sibling_re = re.compile(rf"^{indent}\S", re.MULTILINE)
    next_match = next_sibling_re.search(text, block_start)
    block_end = next_match.start() if next_match else len(text)
    block = text[block_start:block_end]

    flags_re = re.compile(rf"^{indent}  cmake_flags:[ \t]*\n((?:{indent}    - .*\n)+)", re.MULTILINE)
    flags_match = flags_re.search(block)
    if flags_match is None:
        raise RuntimeError(f"manifest.yaml: backends.{backend} has no cmake_flags list to rewrite in {path}")

    new_items = "".join(f"{indent}    - {_yaml_quote(f)}\n" for f in new_flags)
    new_block = block[: flags_match.start(1)] + new_items + block[flags_match.end(1):]

    if clear_example:
        # Same indent convention as cmake_flags above: the backend's own key sits at `indent`,
        # its child keys (cmake_flags, apt_packages, example, ...) at `indent + "  "`. Bounded
        # to this backend's own block (already sliced above), so a sibling backend's
        # `example: true` can never match here.
        example_re = re.compile(rf"^{indent}  example:[ \t]*true[ \t]*\n", re.MULTILINE)
        new_block = example_re.sub("", new_block, count=1)

    new_text = text[:block_start] + new_block + text[block_end:]

    # Never trust the regex alone: parse the result back (yaml.safe_load — read-only, not the
    # forbidden safe_dump) and confirm it says what was meant before committing anything to
    # disk. Abort without writing on any mismatch.
    parsed = yaml.safe_load(new_text)
    written_recipe = ((parsed or {}).get("backends", {}) or {}).get(backend, {}) or {}
    actual = written_recipe.get("cmake_flags")
    if actual != new_flags:
        raise RuntimeError(
            f"manifest.yaml: cmake_flags rewrite for {backend!r} would produce {actual!r}, "
            f"expected {new_flags!r} — aborting without writing"
        )
    if clear_example and written_recipe.get("example"):
        raise RuntimeError(
            f"manifest.yaml: example: true for {backend!r} was not cleared as requested — "
            "aborting without writing"
        )
    path.write_text(new_text)


_SWAP_VERSION_RE = re.compile(r"^v\d+(?:\.\d+){0,3}$")

# `llama_swap:` is the block's own key line at column 0; its children (`version:`, `repo:`)
# sit at column 2 — same convention `_write_manifest_cmake_flags` documents for `backends:`.
_MANIFEST_SWAP_KEY_RE = re.compile(r"^llama_swap:[ \t]*\n", re.MULTILINE)
_MANIFEST_SWAP_VERSION_LINE_RE = re.compile(r"^(  version:\s*)(\S+)(\s*#.*)?$", re.MULTILINE)


def _write_manifest_llama_swap_version(path: Path, new_version: str, new_comment: str | None = None) -> None:
    """Rewrite manifest.yaml's `llama_swap.version` line in place, byte-identical otherwise —
    the same targeted-line-rewrite idiom as `_write_manifest_ref` (never `yaml.safe_dump`,
    which would destroy the header comment block and every inline comment), but scoped to the
    `llama_swap:` block first: `version:` is not a unique key in this file the way `ref:` is —
    `huggingface_hub.version` and `textual.version` both use the same bare key name — so an
    unscoped line match could rewrite the wrong one.

    Unlike `_write_manifest_ref`, which accepts any string today (plans/06 §0b records that as
    a live bug on the llama_cpp side), this validates the value BEFORE touching the file: a
    llama-swap version is a release tag like `v255`, not an arbitrary string, and a bad value
    reaching the pin is exactly what proceeding as-typed on the other pin already gets wrong.
    """
    if not _SWAP_VERSION_RE.fullmatch(new_version):
        raise ValueError(
            f"{new_version!r} does not look like a llama-swap release tag (expected e.g. 'v255') "
            "— refusing to write manifest.yaml"
        )

    text = path.read_text()
    key_match = _MANIFEST_SWAP_KEY_RE.search(text)
    if key_match is None:
        raise RuntimeError(f"manifest.yaml: no llama_swap: block found in {path}")
    block_start = key_match.end()

    # Bounded the same way _write_manifest_cmake_flags bounds a backend's block: the next line
    # back at column 0 (the next top-level key, or EOF), never a fixed line count.
    next_top_level_re = re.compile(r"^\S", re.MULTILINE)
    next_match = next_top_level_re.search(text, block_start)
    block_end = next_match.start() if next_match else len(text)
    block = text[block_start:block_end]

    version_match = _MANIFEST_SWAP_VERSION_LINE_RE.search(block)
    if version_match is None:
        raise RuntimeError(f"manifest.yaml: llama_swap has no version: line to rewrite in {path}")

    comment = f"  # {new_comment}" if new_comment else (version_match.group(3) or "")
    new_line = f"{version_match.group(1)}{new_version}{comment}"
    new_block = block[: version_match.start()] + new_line + block[version_match.end():]
    new_text = text[:block_start] + new_block + text[block_end:]

    # Same never-trust-the-regex-alone guard as _write_manifest_ref/_write_manifest_cmake_flags:
    # parse the result back (read-only yaml.safe_load) and confirm it says what was meant before
    # writing anything to disk.
    parsed = yaml.safe_load(new_text)
    actual = ((parsed or {}).get("llama_swap") or {}).get("version")
    if actual != new_version:
        raise RuntimeError(
            f"manifest.yaml: llama_swap.version rewrite would produce {actual!r}, expected "
            f"{new_version!r} — aborting without writing"
        )
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


class BuildsListModal(ModalScreen[None]):
    """One backend's build inventory (plans/06 Phase 3, A7) — replaces both the old
    RetainedBuildsModal (rollback/remove) and BuildHistoryModal (a separate, JSONL-backed view
    of the same builds under a different name). Scoped to one backend, opened from that row's
    `[ Builds ]` action, which is what removes the need for a Backend column here and is why
    this fits inside a modal dialog where the always-visible two-backend combined table used to
    need 115 cells: one row per build_step.list_builds() entry, not one per (backend, build).

    Per row: the build's directory id, the version it was built at, whether it is active and
    sane (one Status cell — "active"/"built"/"NOT SANE", never "retained": operator decision
    plans/06 §0h-1, pruning is automatic and not a thing the operator manages), its outcome and
    timestamp, and four actions:
      - Activate: build_step.rollback() by id — never by version (plans/06 §0a: a build's
        directory id is what identifies it now, its version is a separate, non-unique field).
      - Config: the build's own build-info.json (ref, cmake_flags, argv, outcome, timestamp) —
        the operator's "link to respective build's config as it was used" — via InfoModal.
      - Log: the persisted compile log (build_step.build_log_path) — the operator's "and build
        log" — via InfoModal. Distinct from BuildLogModal, which is the *live* transcript of a
        build in progress; this reads whatever Phase 1 already wrote to disk for a past build.
      - Remove: manual "this build is known-bad, delete it now" — distinct from
        _prune_old_builds' automatic disk-space budget, which stays silent and untouched by this
        modal. Guarded against removing whatever `current` points at (build_step.remove_build
        re-checks this itself; _can_remove is the UI-level mirror of the same guard, not a
        substitute for it).

    Not a CockpitScreenBase, same reason RetainedBuildsModal/ChangeVersionModal weren't: a
    ModalScreen is a different widget-tree root, so this implements its own confirm-then-dispatch
    for Activate/Remove rather than inheriting one; Config/Log fire without a confirm, being
    read-only.
    """

    BINDINGS = [("escape", "dismiss_modal", "Close")]

    DEFAULT_CSS = """
    BuildsListModal {
        align: center middle;
    }
    #builds-list-dialog {
        width: 95%;
        height: 80%;
        border: thick $background 80%;
        background: $surface;
        padding: $space-normal $space-section;
    }
    #builds-list-header {
        height: auto;
        margin-bottom: $space-normal;
    }
    #builds-list-title {
        width: 1fr;
        text-style: bold;
    }
    #builds-list-table {
        height: 1fr;
        margin-bottom: $space-normal;
    }
    """

    def __init__(self, host_profile: dict, backend: str, runner: Runner) -> None:
        super().__init__()
        self.host_profile = host_profile
        self.backend = backend
        self.runner = runner
        # build id -> the list_builds() dict for it, refreshed on every _refresh() call.
        self._builds: dict[str, dict] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="builds-list-dialog"):
            with Horizontal(id="builds-list-header"):
                yield Static(f"Builds — {self.backend}", id="builds-list-title")
                yield Button("×", id="builds-list-close", classes="close-button", variant="error")
            table = SingleClickDataTable(id="builds-list-table", zebra_stripes=True)
            table.cursor_type = "row"
            yield table
            with Horizontal(classes="action-row-secondary"):
                yield Button("Close", id="btn-builds-list-close-bottom", classes="thin-button")

    def on_mount(self) -> None:
        table = self.query_one("#builds-list-table", SingleClickDataTable)
        # Measured, not projected (DESIGN.md §4.4: a modal table is budgeted against its own
        # dialog, not the 115-cell screen). At 121x30 this dialog is 114 cells wide, and
        # 10+13+10+16 + four action columns (12+10+7+10) = 88 content, render 88 + 2*8 = 104.
        # A first pass carried a separate "Outcome" column and rendered 116 — two cells wider
        # than the dialog it lives in, which would have put [ Remove ] out of reach at the one
        # breakpoint §4.2 requires to fit. Status and Outcome described overlapping facts
        # ("NOT SANE" and "build_failed" never disagree), so they are one column.
        table.add_column("Build", width=10)
        table.add_column("Status", width=13)
        table.add_column("Version", width=10)
        table.add_column("When", width=16)
        table.add_action_column(
            TableAction(
                "activate",
                "Activate",
                confirm="Activate {row}? Points 'current' at this already-built prefix — "
                "no rebuild, no re-smoke-test.",
                available=self._can_activate,
            )
        )
        table.add_action_column(TableAction("config", "Config", available=self._has_build))
        table.add_action_column(TableAction("log", "Log", available=self._has_build))
        table.add_action_column(
            TableAction(
                "remove",
                "Remove",
                destructive=True,
                confirm="Remove build {row}? Frees disk space; cannot be undone.",
                available=self._can_remove,
            )
        )
        self._refresh()

    def _has_build(self, row_key: str) -> bool:
        return row_key in self._builds

    def _can_activate(self, row_key: str) -> bool:
        build = self._builds.get(row_key)
        return bool(build) and not build["current"] and build["sane"]

    def _can_remove(self, row_key: str) -> bool:
        # Mirrors build_step.remove_build's own guard (the one that actually matters — this is
        # only the UI-level reflection of it, so a stale row can never fire an action the
        # function itself would refuse).
        build = self._builds.get(row_key)
        return bool(build) and not build["current"]

    def _refresh(self) -> None:
        if not self.is_mounted:
            return
        table = self.query_one("#builds-list-table", SingleClickDataTable)
        table.clear()
        self._builds.clear()
        for b in build_step.list_builds(self.host_profile, self.backend):
            key = b["id"]
            self._builds[key] = b
            # One column, because "not sane" and a failed outcome are the same event seen
            # from two sides. Outcome wins when it says the build failed — "build failed" tells
            # the operator what to open the log for; "NOT SANE" only says something is missing.
            outcome = b.get("outcome") or ""
            if b["current"]:
                status_cell = Text("active", style="bold green")
            elif outcome == "build_failed":
                status_cell = Text("build failed", style="bold red")
            elif outcome == "smoke_failed":
                status_cell = Text("smoke failed", style="bold red")
            elif not b["sane"]:
                status_cell = Text("NOT SANE", style="bold red")
            else:
                status_cell = Text("built", style="dim")
            when = (b.get("timestamp") or "")[:16].replace("T", " ") or "-"
            table.add_row(
                Text(_short(key)),
                status_cell,
                Text(_short(b.get("version", ""))),
                Text(when),
                *table.action_cells(key),
                key=key,
            )

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id in ("builds-list-close", "btn-builds-list-close-bottom"):
            self.dismiss(None)

    def _show_config(self, build_id: str) -> None:
        meta = build_step.read_build_metadata(self.host_profile, self.backend, build_id)
        if meta is None:
            content = "no config recorded — this build predates per-build metadata (build-info.json)."
        else:
            lines = [
                f"ref: {meta.get('ref', '')}",
                f"outcome: {meta.get('outcome', '')}",
                f"timestamp: {meta.get('timestamp', '')}",
                "cmake_flags:",
                *(f"  {f}" for f in meta.get("cmake_flags", [])),
                "argv:",
                "  " + " ".join(shlex.quote(a) for a in meta.get("argv", [])),
            ]
            content = "\n".join(lines)
        self.app.push_screen(InfoModal(f"Config — {self.backend} {build_id}", escape_markup(content)))

    def _show_log(self, build_id: str) -> None:
        log_path = build_step.build_log_path(self.host_profile, self.backend, build_id)
        content = log_path.read_text() if log_path.is_file() else "no log persisted for this build."
        self.app.push_screen(InfoModal(f"Build log — {self.backend} {build_id}", escape_markup(content)))

    @work
    async def _on_table_action_invoked(self, event: TableActionInvoked) -> None:
        event.stop()
        build = self._builds.get(event.row_key)
        if build is None:
            return
        if event.action.id == "config":
            self._show_config(build["id"])
            return
        if event.action.id == "log":
            self._show_log(build["id"])
            return
        message = event.action.confirm_message(event.row_key)
        if message is not None:
            # Both remaining actions (activate, remove) write under prefix_root (/opt/...), so
            # this modal repeats CockpitScreenBase.confirm's root gate — it can't inherit it,
            # being a ModalScreen.
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
        if event.action.id == "activate":
            self._run_activate(build["id"])
        elif event.action.id == "remove":
            self._run_remove(build["id"])

    @work(thread=True)
    def _run_activate(self, build_id: str) -> None:
        try:
            build_step.rollback(self.host_profile, self.backend, build_id, self.runner)
        except SystemExit as e:
            self.app.call_from_thread(self.app.notify, f"activate failed: {e}", severity="error")
            return
        except Exception as e:
            self.app.call_from_thread(self.app.notify, f"activate failed: {e}", severity="error")
            return
        self.app.call_from_thread(self.app.notify, f"{self.backend}: current now points at {build_id}")
        self.app.call_from_thread(self._refresh)

    @work(thread=True)
    def _run_remove(self, build_id: str) -> None:
        try:
            build_step.remove_build(self.host_profile, self.backend, build_id, self.runner)
        except SystemExit as e:
            self.app.call_from_thread(self.app.notify, f"remove failed: {e}", severity="error")
            return
        except Exception as e:
            self.app.call_from_thread(self.app.notify, f"remove failed: {e}", severity="error")
            return
        self.app.call_from_thread(self.app.notify, f"{self.backend}: removed build {build_id}")
        self.app.call_from_thread(self._refresh)


class ChangeVersionModal(ModalScreen[str | None]):
    """Version picker for BuildsScreen's "Change version…" action (plans/05 Phase 2 item 4).

    Always dismisses with a resolved commit SHA (or None on cancel) — never a bare tag — so the
    caller can write it straight into manifest.yaml's `ref:` line. Sources, in the order the
    operator asked for ("go back" first): on-disk installed builds (list_builds(), which already
    carries a real SHA per build's own build-info.json since Phase 1), then upstream releases
    (network, backgrounded so opening the modal never blocks on it — a release's tag is resolved
    to a SHA lazily, only if it's the one picked), plus a manual SHA entry for anything not
    already known locally.

    build-history.jsonl (build_step.read_build_history) used to be a third local source here,
    for a ref that was built and later pruned. Dropped in plans/06 Phase 3: that file has had no
    writer since Phase 1 folded per-run history into each build's own build-info.json, so it is
    frozen legacy data — carrying a reader for a file nothing writes, to populate rows nothing
    new will ever add to, is the "Cost of existing" the kit's contract calls out. A ref built
    before the migration and since pruned is still reachable via manual SHA entry below.

    Not a CockpitScreenBase — a ModalScreen is a different widget-tree root, same reason
    BuildsListModal implements its own confirm-free select-and-dismiss dispatch here. The
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
        # build-history.jsonl is no longer read here (plans/06 Phase 3 — see class docstring):
        # every installed build already carries a real SHA via list_builds(), and that file has
        # had no writer since Phase 1.
        for backend in self.backends:
            for b in build_step.list_builds(self.host_profile, backend):
                ref = b.get("version") or ""
                if not ref:
                    continue
                state = "current" if b.get("current") else "built"
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


class BackendDetailModal(ModalScreen[None]):
    """Edit view for one backend row on the Installs table (plans/06 Phase 3, A6) — a plain
    field:value form, not the earlier What/Where/How narrative the operator asked to have
    removed ("simply instead list fields and their respective value"): Backend, GPU, Build
    status, Build location, then — bottom-weighted, since this is the one thing an operator
    actually edits here — the editable cmake_flags TextArea followed by the read-only Build
    command it produces. Retained-build detail (which build is current, what each one's config
    and log were) moved out of this modal entirely; it lives in BuildsListModal now, reachable
    from the table's own `[ Builds ]` action, not nested inside Edit.

    The Build command line is recomputed on every `TextArea.Changed` through
    `build_step.resolve_cmake_argv` — the same function `_build_backend` calls to configure the
    real build — against the *unsaved* text in the box, so it can never drift from what saving
    (and then building) would actually run, and it reflects an edit before the operator commits
    to it.

    cmake_flags is the one editable field: one flag per line, confirmed with
    `ConfirmModal(danger=True)` old -> new (DESIGN.md §5's declarative-write idiom), written
    by `_write_manifest_cmake_flags` — a targeted block rewrite, never `yaml.safe_dump`.

    Not a CockpitScreenBase, same reason BuildsListModal/ChangeVersionModal aren't: a
    ModalScreen is a different widget-tree root, so this implements its own confirm-then-write
    dispatch rather than inheriting one.
    """

    BINDINGS = [("escape", "dismiss_modal", "Close")]

    DEFAULT_CSS = """
    BackendDetailModal {
        align: center middle;
    }
    #detail-dialog {
        width: 90%;
        height: 80%;
        border: thick $background 80%;
        background: $surface;
        padding: $space-normal $space-section;
    }
    #detail-header {
        height: auto;
        margin-bottom: $space-normal;
    }
    #detail-title {
        width: 1fr;
        text-style: bold;
    }
    #detail-body {
        height: 1fr;
    }
    #detail-flags-group {
        height: auto;
        margin-bottom: $space-normal;
    }
    #f-detail-cmake-flags {
        height: 5;
    }
    #detail-command-group {
        height: auto;
        margin-bottom: $space-normal;
    }
    #detail-build-command {
        color: $text-muted;
    }
    #detail-error {
        color: $error;
        height: auto;
        margin-bottom: $space-normal;
    }
    """

    def __init__(self, host_profile: dict, manifest: dict, backend: str, repo_root: Path, app_ref) -> None:
        super().__init__()
        self.host_profile = host_profile
        self.manifest = manifest
        self.backend = backend
        self.repo_root = repo_root
        self.app_ref = app_ref

    def _title(self) -> str:
        prefix = "EX. " if self._recipe().get("example") else ""
        return f"{prefix}llama.cpp ({self.backend})"

    def compose(self) -> ComposeResult:
        with Vertical(id="detail-dialog"):
            with Horizontal(id="detail-header"):
                yield Static(self._title(), id="detail-title")
                yield Button("×", id="detail-close", classes="close-button", variant="error")
            with VerticalScroll(id="detail-body"):
                with Horizontal(classes="form-row"):
                    yield Static("Backend", classes="form-label")
                    yield Static(id="detail-backend", classes="form-field")
                with Horizontal(classes="form-row"):
                    yield Static("GPU", classes="form-label")
                    yield Static(id="detail-gpu", classes="form-field")
                with Horizontal(classes="form-row"):
                    yield Static("Build status", classes="form-label")
                    yield Static(id="detail-build-status", classes="form-field")
                with Horizontal(classes="form-row"):
                    yield Static("Build location", classes="form-label")
                    yield Static(id="detail-build-location", classes="form-field")
                with Vertical(id="detail-flags-group"):
                    yield Static("cmake_flags", classes="form-label")
                    yield TextArea(id="f-detail-cmake-flags")
                with Vertical(id="detail-command-group"):
                    yield Static("Build command", classes="form-label")
                    yield Static(id="detail-build-command")
                yield Static("", id="detail-error")
            with Horizontal(classes="action-row-primary"):
                yield Button("Save flags", id="btn-detail-save", variant="primary", classes="thin-button")
                yield Button("Close", id="btn-detail-close-bottom", classes="thin-button")

    def on_mount(self) -> None:
        self._populate_detail()

    def _recipe(self) -> dict:
        return self.manifest.get("backends", {}).get(self.backend, {}) or {}

    def _populate_detail(self) -> None:
        recipe = self._recipe()
        self.query_one("#detail-title", Static).update(self._title())

        gpus = [
            f"{g.get('id', '?')} ({g.get('vendor', '?')})"
            for g in self.host_profile.get("gpus", [])
            if self.backend in g.get("backends", [])
        ]
        self.query_one("#detail-backend", Static).update(escape_markup(self.backend))
        self.query_one("#detail-gpu", Static).update(escape_markup(", ".join(gpus)) or "(none)")

        prefix_root = Path(self.host_profile["paths"]["prefix_root"])
        backend_dir = prefix_root / self.backend
        current = next(
            (b for b in build_step.list_builds(self.host_profile, self.backend) if b.get("current")),
            None,
        )
        status = f"{current['id']} active" if current else "not built"
        self.query_one("#detail-build-status", Static).update(escape_markup(status))
        self.query_one("#detail-build-location", Static).update(escape_markup(f"{backend_dir}/"))

        self.query_one("#f-detail-cmake-flags", TextArea).text = "\n".join(recipe.get("cmake_flags", []))
        self._update_build_command()

    def _current_flags(self) -> list[str]:
        return [
            line.strip()
            for line in self.query_one("#f-detail-cmake-flags", TextArea).text.splitlines()
            if line.strip()
        ]

    def _update_build_command(self) -> None:
        """Recomputes the displayed Build command from whatever is in the cmake_flags TextArea
        right now — including an unsaved edit — through the same `resolve_cmake_argv` the real
        build calls, so the display can never drift from what saving-and-building would run.
        Named to avoid `_render`, which collides with `Widget._render()` (DESIGN.md §9 note)."""
        recipe = {**self._recipe(), "cmake_flags": self._current_flags()}
        prefix = Path(self.host_profile["paths"]["prefix_root"]) / self.backend
        checkout_dir = build_step.checkout_dir_for(self.host_profile)
        argv = build_step.resolve_cmake_argv(self.backend, recipe, checkout_dir, prefix)
        cmd = " ".join(shlex.quote(a) for a in argv)
        self.query_one("#detail-build-command", Static).update(escape_markup(cmd))

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "f-detail-cmake-flags":
            self._update_build_command()

    def _set_error(self, message: str) -> None:
        self.query_one("#detail-error", Static).update(escape_markup(message))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id in ("detail-close", "btn-detail-close-bottom"):
            self.dismiss(None)
        elif event.button.id == "btn-detail-save":
            self._confirm_and_save_flags()

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    @work
    async def _confirm_and_save_flags(self) -> None:
        new_flags = self._current_flags()
        if not new_flags:
            self._set_error("cmake_flags cannot be emptied — at least one flag is required")
            return
        recipe = self._recipe()
        old_flags = recipe.get("cmake_flags", [])
        if new_flags == old_flags:
            self._set_error("no change")
            return
        was_example = bool(recipe.get("example"))
        message = (
            f"Update backends.{self.backend}.cmake_flags in manifest.yaml?\n"
            f"- old: {' '.join(old_flags)}\n"
            f"+ new: {' '.join(new_flags)}"
            + ("\nThis clears the EX. (stock example) mark on this recipe." if was_example else "")
        )
        confirmed = await self.app.push_screen_wait(
            ConfirmModal(message, confirm_label="Save flags", danger=True)
        )
        if not confirmed:
            return
        try:
            _write_manifest_cmake_flags(
                self.repo_root / "manifest.yaml", self.backend, new_flags, clear_example=was_example
            )
        except Exception as e:
            self.app.notify(f"could not write manifest.yaml: {e}", severity="error")
            return
        self.app_ref.reload_manifest()
        self.manifest = getattr(self.app_ref, "manifest", self.manifest)
        self._set_error("")
        self._populate_detail()
        self.app.notify(f"{self.backend}: cmake_flags updated")


class BuildsScreen(CockpitScreenBase):
    """The 'Installs' tab. One panel per backend the host's GPUs actually use (cuda, vulkan,
    etc. per hosts/<hostname>.yaml), plus one upstream-version-check panel.
    """

    DEFAULT_CSS = """
    BuildsScreen {
        height: 1fr;
    }
    BuildsScreen #btn-foreign-builds {
        display: none;
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
        # Cache of the last filesystem scan (populated in the refresh path, DESIGN.md §3.0) —
        # the "Foreign builds" button reads this on click rather than rescanning, and it also
        # drives whether the button is shown at all.
        self._foreign_builds: list[dict] = []

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
            # Section one — Llama-swap (operator decision §0h-3: symmetric with section two,
            # same shape — version, status, an update action — but never a table: this is one
            # global service, not a per-backend collection, and its Build/Info cells on the old
            # combined table were always blank because "llama-swap" was never in self.backends).
            yield Static("Llama-swap", classes="section-title")
            with Vertical(id="swap-panel", classes="panel"):
                with Horizontal(classes="form-row"):
                    yield Static("Installed version", classes="form-label")
                    yield Static(id="swap-installed-version", classes="form-field")
                with Horizontal(classes="form-row"):
                    yield Static("Update status", classes="form-label")
                    yield Static(id="swap-update-status", classes="form-field")
                with Horizontal(classes="form-row"):
                    yield Static("Binary path", classes="form-label")
                    yield Static(id="swap-binary-path", classes="form-field")
                with Horizontal(classes="form-row"):
                    yield Static("Systemd unit", classes="form-label")
                    yield Static(id="swap-unit-status", classes="form-field")
            with Horizontal(classes="action-row-secondary"):
                yield Button("Update to latest", id="btn-swap-update-to-latest", classes="thin-button", disabled=True)

            # Section two — Llama.cpp, mirroring section one's shape.
            yield Static("Llama.cpp", classes="section-title")
            with Vertical(id="cpp-panel", classes="panel"):
                with Horizontal(classes="form-row"):
                    yield Static("Version", classes="form-label")
                    yield Static(id="cpp-version", classes="form-field")
                with Horizontal(classes="form-row"):
                    yield Static("Update status", classes="form-label")
                    yield Static(id="cpp-update-status", classes="form-field")
            with Horizontal(classes="action-row-secondary"):
                yield Button("Update to latest", id="btn-update-to-latest", classes="thin-button", disabled=True)
                yield Button("Change version…", id="btn-change-version", classes="thin-button")
                yield Button("Check for Updates", id="btn-check-updates", classes="thin-button")

            if not self.backends:
                yield Static(
                    "host profile declares no GPU backends (hosts/*.yaml gpus[].backends) — "
                    "nothing to build.",
                    id="no-backends",
                    classes="panel",
                )

            # No fixed_columns (DESIGN.md §4.2): datatable--fixed REPLACES the row style rather
            # than compositing with it, so a pinned column cut a flat band down column 0 through
            # the zebra stripes — the "first column is always highlighted" defect. Every column
            # has an explicit width that fits, so nothing scrolls horizontally for it to pin.
            backends_table = SingleClickDataTable(
                id="backends-table", zebra_stripes=True, classes="data-table"
            )
            backends_table.cursor_type = "row"
            yield backends_table
            # Answers "where does a row come from, and how do I add one" — the operator's
            # 2026-09-18 QA report: the bind lives in Settings -> GPU Topology's "Add GPU" form
            # (gpus[].backends). Not a second GPU-editing form — that stays the only one.
            with Horizontal(classes="action-row-secondary"):
                yield Button("Build log", id="btn-build-log", classes="thin-button")
                # Hidden by default (DEFAULT_CSS) until _refresh_backends_table finds something —
                # a scan result behind a button (A2), not an always-visible block, and never
                # rescanned on click (DESIGN.md §3.0: the scan itself stays in the refresh path).
                yield Button("Foreign builds", id="btn-foreign-builds", classes="thin-button")

    def on_mount(self) -> None:
        backends_table = self.query_one("#backends-table", SingleClickDataTable)
        backends_table.cursor_type = "row"
        backends_table.zebra_stripes = True
        # The section title already says "llama.cpp" — this column is just the backend name,
        # with an "EX. " prefix on a still-stock recipe (manifest.yaml backends.<name>.example).
        # "EX. vulkan" is the longest at 10 chars; width 14 leaves headroom.
        backends_table.add_column("Backend", width=14)
        # "build3 · 481c65f09" is 19 chars (identity and version, now separable — Phase 1);
        # width 20 fits it and "not built".
        backends_table.add_column("Active build", width=20)
        # 14 + 20 + 32 + 9 + 8 + 10 = 93 content, render 93 + 2*6 = 105 (DESIGN.md §4.4,
        # updated in Phase 3 to add [ Builds ]). "update available (d1d3c33ab1)" is 29 chars
        # and fits; the failure case ("couldn't check (HTTP Error 403: rate limit exceeded)")
        # is unchanged text and can now clip earlier than at the old width=35 — an accepted
        # trade for the column budget this phase asks for, not something silently widened back.
        backends_table.add_column("Status", width=32)
        backends_table.add_action_column(
            TableAction(
                "build",
                "Build",  # always "Build" (A8) — the confirm dialog says whether it's a rebuild
                width=9,  # len("Build") + 4
                confirm=self._build_confirm_message,
                requires_root=True,
                available=lambda row_key: row_key in self.backends,
            )
        )
        backends_table.add_action_column(
            TableAction(
                "info",
                "Edit",  # relabelled from "Info" this phase; id/handler/modal are Phase 3's
                width=8,  # len("Edit") + 4
                available=lambda row_key: row_key in self.backends,
            )
        )
        backends_table.add_action_column(
            TableAction(
                "builds",
                "Builds",  # opens BuildsListModal, scoped to this row's backend (Phase 3, A7)
                width=10,  # len("Builds") + 4
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

    def _current_build(self, backend: str) -> dict | None:
        for b in build_step.list_builds(self.host_profile, backend):
            if b.get("current"):
                return b
        return None

    def _is_rebuild(self, backend: str, selected_ref: str) -> bool:
        """The single definition of "already built", shared by the confirm dialog's wording and
        the `Active build` cell's colour: is the *current* build already at this ref? (Not "is
        any build at this ref sane" — that was the old per-row build-label callable's
        definition, and it disagreeing with the old installed-cell's "is one current" is
        exactly the inconsistency the operator reported: `Not built` beside `Rebuild`. One
        definition, used by both.)"""
        build = self._current_build(backend)
        return bool(build) and build.get("version") == selected_ref

    def _build_confirm_message(self, row_key: str) -> str:
        """Composed per row (TableAction.confirm as a callable — cockpit/widgets.py, same
        treatment `label` already gets): the operator asked to see the ref, the backend, the
        resolved cmake flags and the target prefix before committing ten minutes, not just a
        generic "Build llama.cpp (cuda)?" (operator QA 2026-09-18). `resolve_cmake_argv` is
        the same function `_build_backend` uses to configure the real build, so this is
        provably what will run, not a description of it. The `[ Build ]` column is always
        "Build" (A8); this confirm is the one place that still says "Rebuild" when it is one."""
        ref = self.manifest.get("llama_cpp", {}).get("ref", "")
        label = "Rebuild" if self._is_rebuild(row_key, ref) else "Build"
        recipe = self.manifest.get("backends", {}).get(row_key, {})
        prefix = Path(self.host_profile["paths"]["prefix_root"]) / row_key
        checkout_dir = build_step.checkout_dir_for(self.host_profile)
        argv = build_step.resolve_cmake_argv(row_key, recipe, checkout_dir, prefix)
        cmd = " ".join(shlex.quote(a) for a in argv)
        return (
            f"{label} llama.cpp ({row_key}) at version {_short(ref)}?\n{cmd}\n"
            f"Installs to {prefix}. This can take several minutes."
        )

    def _active_build_cell(self, backend: str, selected_ref: str) -> Text:
        """§0b: `current` is a per-backend symlink target, independent of the (global) pin.
        Shows identity *and* version together (Phase 1 made them separable) — `build3 ·
        481c65f09`, not just one or the other."""
        build = self._current_build(backend)
        if build is None:
            return Text("not built", style="dim")
        label = f"{build['id']} · {_short(build.get('version', ''))}"
        style = "green" if build.get("version") == selected_ref else "bold yellow"
        return Text(label, style=style)

    def _refresh_backends_table(self) -> None:
        cpp_ref = self.manifest.get("llama_cpp", {}).get("ref", "")
        self.query_one("#cpp-version", Static).update(escape_markup(_short(cpp_ref)))
        self.query_one("#cpp-update-status", Static).update(self._format_update_cell(self._llama_cpp_check))

        self.query_one("#swap-update-status", Static).update(self._format_update_cell(self._llama_swap_check))
        # dashboard.py's _compute_llm_text guards this same call the same way: systemd isn't
        # available on every host this cockpit runs on (e.g. a dev machine), and swap.status()
        # shells out to `systemctl` with no guard of its own.
        try:
            swap_facts = swap_step.status(self.host_profile)
        except FileNotFoundError:
            swap_facts = None
        except Exception as e:
            log.warning("swap.status() failed: %s", e)
            swap_facts = None
        if swap_facts is None:
            self.query_one("#swap-installed-version", Static).update("unknown (systemd not available)")
            self.query_one("#swap-binary-path", Static).update("unknown")
            self.query_one("#swap-unit-status", Static).update("unknown")
        else:
            self.query_one("#swap-installed-version", Static).update(
                escape_markup(swap_facts.get("installed_version") or "not installed")
            )
            self.query_one("#swap-binary-path", Static).update(escape_markup(swap_facts["binary_path"]))
            unit_state = "active" if swap_facts["unit_active"] else "inactive"
            unit_state += ", enabled" if swap_facts["unit_enabled"] else ", disabled"
            self.query_one("#swap-unit-status", Static).update(
                escape_markup(f"{swap_facts['unit_name']} — {unit_state}")
            )

        table = self.query_one("#backends-table", SingleClickDataTable)
        table.clear()
        for backend in self.backends:
            status_cell = (
                Text("building…", style="dim")
                if backend in self._building_backends
                else self._format_update_cell(self._llama_cpp_check)
            )
            # "EX. " marks a still-stock recipe (manifest.yaml backends.<name>.example) — see
            # BackendDetailModal's Edit view for what it means and how it clears.
            is_example = bool(self.manifest.get("backends", {}).get(backend, {}).get("example"))
            backend_label = f"{'EX. ' if is_example else ''}{backend}"
            table.add_row(
                Text(backend_label),
                self._active_build_cell(backend, cpp_ref),
                status_cell,
                *table.action_cells(backend),
                key=backend,
            )

        self._refresh_foreign_builds_button()

    def _refresh_foreign_builds_button(self) -> None:
        """Read-only filesystem scan (build_step.find_foreign_builds) — belongs here, not in
        __init__/compose/on_mount, per DESIGN.md §3.0: a hidden tab does no I/O, and this runs
        only from on_first_view/on_refresh_requested via _refresh_backends_table. The result is
        cached (self._foreign_builds) so the button's own press handler never rescans; toggling
        `display`/label each call is what keeps the button honest if a foreign tree appears or
        disappears while the tab stays open, and hides it entirely (A2) when there is nothing
        to report."""
        self._foreign_builds = build_step.find_foreign_builds(self.host_profile)
        btn = self.query_one("#btn-foreign-builds", Button)
        if not self._foreign_builds:
            btn.display = False
            return
        btn.label = f"Foreign builds ({len(self._foreign_builds)})"
        btn.display = True

    def _foreign_builds_text(self) -> str:
        lines = ["Detected outside paths.prefix_root — not managed by this toolkit:"]
        for f in self._foreign_builds:
            suffix = "" if f["sane"] else " (binary missing or not executable)"
            lines.append(f"  {escape_markup(f['path'])}{suffix}")
        return "\n".join(lines)

    def _format_update_cell(self, result: dict | None) -> Text:
        if result is None:
            return Text("not checked yet", style="dim")
        if not result.get("ok"):
            return Text(f"couldn't check ({result.get('error')})", style="dim")
        if not result["update_available"]:
            return Text("up to date", style="green")
        return Text(f"update available ({_short(result['latest'])})", style="bold yellow")

    def _update_action_buttons_state(self) -> None:
        if not self.is_mounted:
            return
        can_update_cpp = bool(
            self._llama_cpp_check
            and self._llama_cpp_check.get("ok")
            and self._llama_cpp_check.get("update_available")
        )
        btn = self.query("#btn-update-to-latest")
        if btn:
            btn.first(Button).disabled = not can_update_cpp
        can_update_swap = bool(
            self._llama_swap_check
            and self._llama_swap_check.get("ok")
            and self._llama_swap_check.get("update_available")
        )
        swap_btn = self.query("#btn-swap-update-to-latest")
        if swap_btn:
            swap_btn.first(Button).disabled = not can_update_swap

    # ------------------------------------------------------------------ per-row action dispatch

    async def handle_table_action(self, action_id: str, row_key: str, table: DataTable) -> None:
        if action_id == "info":
            self.app.push_screen(
                BackendDetailModal(self.host_profile, self.manifest, row_key, self.repo_root, self.app_ref)
            )
            return
        if action_id == "builds":
            # Scoped to this row's backend — that scoping is what keeps the modal's table
            # inside its dialog budget (DESIGN.md §4.4) without a Backend column. The dismiss
            # callback refreshes because Activate repoints `current`, which `Active build`
            # reports; without it the table would still show the previous build.
            self.app.push_screen(
                BuildsListModal(self.host_profile, row_key, self.privileged_runner),
                callback=lambda _: self._refresh_backends_table(),
            )
            return
        if action_id != "build":
            return
        if self._building_backends:
            self.notify("a build is already in progress", severity="warning")
            return
        ref = self.manifest.get("llama_cpp", {}).get("ref", "")
        force = self._is_rebuild(row_key, ref)
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
        elif button_id == "btn-build-log":
            self._open_log_modal("Build log")
        elif button_id == "btn-foreign-builds":
            self.app.push_screen(InfoModal("Foreign builds", self._foreign_builds_text()))
        elif button_id == "btn-swap-update-to-latest":
            self._confirm_and_update_swap()

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
    async def _confirm_and_update_swap(self) -> None:
        """Bumps manifest.yaml's llama_swap.version pin, installs the binary at that pin, and
        restarts the unit — three effects, none of them optional: installing the binary without
        restarting would leave `_current_installed_version` reporting the new version while the
        running service still executes the old inode, and this deliberately stops short of
        run() (no config.yaml regeneration, no unit reinstall) — "update llama-swap" is not
        licence to redeploy the whole gateway. Unlike the declarative-only llama_cpp pin write,
        this actually restarts a running service, so it goes through self.confirm(...) with
        both mutates_system and requires_root, not a bare ConfirmModal."""
        check = self._llama_swap_check
        if not check or not check.get("ok") or not check.get("update_available"):
            self.notify("no update to apply — run 'Check for Updates' first", severity="warning")
            return
        old_version = self.manifest.get("llama_swap", {}).get("version", "")
        new_version = check["latest"]
        try:
            unit_name = swap_step.status(self.host_profile)["unit_name"]
        except Exception:
            unit_name = "the llama-swap systemd unit"
        message = (
            f"Update llama-swap {old_version} → {new_version}?\n"
            "This writes manifest.yaml, installs the new binary, and restarts "
            f"{unit_name} — dropping any models currently loaded in VRAM."
        )
        if not await self.confirm(
            message, confirm_label="Update llama-swap", mutates_system=True, requires_root=True
        ):
            return
        self._run_swap_update(new_version)

    @work(thread=True)
    def _run_swap_update(self, new_version: str) -> None:
        try:
            _write_manifest_llama_swap_version(self.repo_root / "manifest.yaml", new_version)
        except Exception as e:
            self.app.call_from_thread(self.app.notify, f"could not write manifest.yaml: {e}", severity="error")
            return
        self.app.call_from_thread(self.app_ref.reload_manifest)
        self.manifest = getattr(self.app_ref, "manifest", self.manifest)
        try:
            swap_step.install_pinned_binary(self.host_profile, self.manifest, self.privileged_runner)
            swap_step.restart_or_start(self.privileged_runner)
        except SystemExit as e:
            self.app.call_from_thread(self.app.notify, f"llama-swap update failed: {e}", severity="error")
            return
        except Exception as e:
            self.app.call_from_thread(self.app.notify, f"llama-swap update failed: {e}", severity="error")
            return
        self.app.call_from_thread(self.app.notify, f"llama-swap updated to {new_version}")
        self.app.call_from_thread(self._refresh_backends_table)
        self.app.call_from_thread(self._update_action_buttons_state)

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
            if b.get("version") == new_ref and b.get("sane")
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
