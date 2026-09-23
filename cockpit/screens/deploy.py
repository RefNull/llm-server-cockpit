"""Cockpit "Deploy" tab: list/add/edit/delete entries in models.yaml, preview the
llama-swap config.yaml that would be generated from them, and apply it (install +
restart llama-swap) via provision.steps.swap.run — the same function the CLI uses.

Every write to the real models.yaml goes: build candidate in memory -> write to a
temp file -> schema.load_models(temp_path, ...) (the authoritative jsonschema +
cross-file validator) -> only on success, overwrite models.yaml and tell the app to
reload. A failed validation never touches the real file.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

import yaml
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Select, Static, TextArea

from cockpit.screens.downloads import list_model_files
from provision.steps import sysinfo
from cockpit.widgets import (
    CockpitScreenBase,
    ConfirmModal,
    InfoModal,
    SingleClickDataTable,
    TableAction,
    selection_marker,
)
from provision import schema
from provision.common import Runner
from provision.steps import swap

_ENGINE_OPTIONS = [
    ("llama-server (GGUF)", "llama-cpp"),
    ("Python script", "python"),
    ("Custom command", "unmanaged"),
]

# The prefill EditModelModal drops into a NEW python model's (empty) args box on engine
# switch (Decision 2, plans/09 Phase 2 item 3) — not every script takes --port, so this is a
# convenience default, not a hardcoded part of the python cmd. Also the sentinel the
# coordinator's away-from-python clear checks against, so switching python -> llama-cpp -> back
# doesn't strand it as a stale llama-server flag no one typed.
_PYTHON_ARGS_PREFILL = "--port ${PORT}"


def _args_lines_to_tokens(text: str) -> list[str]:
    """Parse an args TextArea's text into a flat token list (Decision 4, plans/09 Phase 2 item
    6): shlex.split per non-blank line, not one line = one token — `--ctx-size 262144` on one
    line must survive as two argv tokens or it reaches llama-server as a single unknown
    argument (plan §0b). Raises ValueError (from shlex.split, an unbalanced quote) with the
    1-based line number prepended so the caller can name the offending line in a form error.
    """
    tokens: list[str] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            tokens.extend(shlex.split(line))
        except ValueError as e:
            raise ValueError(f"line {lineno}: {e}") from e
    return tokens


def _args_tokens_to_lines(tokens: list[str]) -> str:
    """Inverse of _args_lines_to_tokens, for display on load (Decision 4): re-pair `--flag
    value` onto one line via shlex.join for readability. A token following a `-`-prefixed
    token is paired with it only if that following token does NOT itself start with `-` — so
    two flags in a row (e.g. --flash-attn, --jinja) each keep their own line rather than one
    swallowing the other as a fake "value". Round-trip is lossless:
    _args_lines_to_tokens(_args_tokens_to_lines(tokens)) == tokens (smoke/verify_swap_import.py).
    """
    lines: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok.startswith("-") and i + 1 < n and not tokens[i + 1].startswith("-"):
            lines.append(shlex.join([tok, tokens[i + 1]]))
            i += 2
        else:
            lines.append(shlex.join([tok]))
            i += 1
    return "\n".join(lines)


class ExamplesModal(ModalScreen[str | None]):
    """Copy-in snippets for the arguments/env fields (QA item c.4 + d; table form, plans/09
    Phase 3, item c).

    One two-column table (Purpose | Snippet) per DESIGN.md §4.6 — cells are rich.text.Text,
    not str: the JSON snippet below carries braces and quotes the app console's markup=True
    would otherwise try to parse. Row select dismisses with that row's snippet; the caller
    (EditModelModal._insert_example) appends it as a new line rather than overwriting whatever
    is already typed.

    Every snippet below is copied verbatim from the operator's own llama-swap config (its
    genericized shape lives in smoke/fixtures/llama-swap-import.yaml) or from
    models.example.yaml.
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    ExamplesModal {
        align: center middle;
    }
    #examples-dialog {
        width: 80%;
        height: 80%;
        border: thick $background 80%;
        background: $surface;
        padding: $space-normal $space-section;
    }
    #examples-title {
        text-style: bold;
        color: $accent;
        margin-bottom: $space-normal;
    }
    #examples-intro {
        color: $text-muted;
        margin-bottom: $space-normal;
    }
    #examples-scroll {
        height: 1fr;
    }
    """

    # (purpose, snippet) — llama-server CLI flags, from the operator's config (fixture) and
    # models.example.yaml. --port/--model/--mmproj excluded: the form owns those.
    ARG_EXAMPLES: list[tuple[str, str]] = [
        ("Context window", "--ctx-size 262144"),
        ("KV cache K type", "--cache-type-k q4_0"),
        ("KV cache V type", "--cache-type-v q4_0"),
        ("Flash attention", "--flash-attn"),
        ("Jinja chat template", "--jinja"),
        ("Chat template kwargs", '--chat-template-kwargs \'{"reasoning_effort":"medium","preserve_thinking":true}\''),
        ("Reasoning budget", "--reasoning-budget 5000"),
        ("Speculative decoding", "--spec-type draft-mtp"),
        ("Max draft tokens", "--spec-draft-n-max 2"),
        ("Auto-fit off", "--fit off"),
        ("Context checkpoints", "--ctx-checkpoints 0"),
        ("Embedding", "--embedding"),
        ("Pooling (embedding)", "--pooling cls"),
        ("Reranking", "--reranking"),
        ("Pooling (reranker)", "--pooling rank"),
    ]
    ENV_EXAMPLES: list[tuple[str, str]] = [
        ("Intel Vulkan ICD", "VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/intel_icd.x86_64.json"),
        ("Pin CUDA device", "CUDA_VISIBLE_DEVICES=0"),
    ]

    def __init__(self, kind: str) -> None:
        super().__init__()
        self.kind = kind
        self._examples = self.ARG_EXAMPLES if kind == "args" else self.ENV_EXAMPLES

    def compose(self) -> ComposeResult:
        title = "llama-server argument examples" if self.kind == "args" else "env examples"
        with Vertical(id="examples-dialog"):
            yield Static(title, id="examples-title")
            if self.kind == "args":
                yield Static(
                    "llama-server CLI flags (with --). Not llama.cpp preset keys, not a full command.",
                    id="examples-intro",
                )
            with VerticalScroll(id="examples-scroll"):
                table = SingleClickDataTable(id="examples-table", zebra_stripes=True, classes="data-table")
                table.cursor_type = "row"
                yield table
            with Horizontal(classes="action-row-primary"):
                yield Button("Close", id="btn-examples-close", classes="thin-button")

    def on_mount(self) -> None:
        table = self.query_one("#examples-table", SingleClickDataTable)
        table.add_column("Purpose", width=22)
        table.add_column("Snippet", width=79)
        for i, (purpose, snippet) in enumerate(self._examples):
            table.add_row(Text(purpose), Text(snippet), key=str(i))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "examples-table" or event.row_key is None or event.row_key.value is None:
            return
        idx = int(event.row_key.value)
        self.dismiss(self._examples[idx][1])

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-examples-close":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfigPasteModal(ModalScreen[str | None]):
    """Paste-a-llama-swap-config.yaml modal for importing models."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    ConfigPasteModal {
        align: center middle;
    }
    #paste-dialog {
        width: 90%;
        height: 80%;
        border: thick $background 80%;
        background: $surface;
        padding: $space-normal $space-section;
    }
    #paste-title {
        text-style: bold;
        color: $accent;
        margin-bottom: $space-normal;
    }
    #paste-text {
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="paste-dialog"):
            yield Static("Paste a llama-swap config.yaml below", id="paste-title")
            yield TextArea(id="paste-text")
            with Horizontal(classes="action-row-primary"):
                yield Button("Parse", id="btn-parse", variant="primary", classes="thin-button")
                yield Button("Cancel", id="btn-paste-cancel", classes="thin-button")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-parse":
            self.dismiss(self.query_one("#paste-text", TextArea).text)
        elif event.button.id == "btn-paste-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class EditModelModal(ModalScreen[bool]):
    """Modal dialog for adding or editing a model in models.yaml.

    Validates candidate configuration against schema.validate_models_dict before writing.
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    EditModelModal {
        align: center middle;
    }
    #edit-model-columns {
        height: 1fr;
    }
    #edit-model-scroll {
        width: 1fr;
        padding-right: $space-section;
    }
    /* Holds args/cmd (whichever the engine needs) above env, not env alone. Real configs
       invert what this used to assume here: the operator's biggest model carries 19 arg
       tokens and 1 env line, not 15-20 env lines (plans/09 §0f) — args, not env, is the field
       that needs the room, hence #f-args below getting the flexible row instead of #f-env.
       VerticalScroll, not a plain Vertical: stacking a flexible-height args/cmd box above env
       is still more content than a non-scrolling height: 1fr column can hold at the 80x24
       floor — the same clipping mechanism §0f found on the left, reproduced here if this
       stayed a plain Vertical (plans/05-qa-remediation-pass.md Phase 5, coordinator
       correction). */
    #edit-model-right {
        width: 1fr;
        height: 1fr;
    }
    #edit-model-dialog {
        width: 90%;
        height: 90%;
        border: thick $background 80%;
        background: $surface;
        padding: $space-normal $space-section;
    }
    #edit-model-title {
        text-style: bold;
        color: $accent;
        margin-bottom: $space-normal;
    }
    #edit-model-scroll {
        height: 1fr;
    }
    /* Both columns now carry Labels (args/cmd moved into the right column, above env) so this
       is scoped to the shared columns container, not just the left scroll. width: 1fr makes a
       Label wrap inside its column instead of clipping at 80 cols (§0f: the args label's
       ~110-char text used to run off the right edge). */
    #edit-model-columns Label {
        margin-top: $space-normal;
        color: $text-muted;
    }
    #edit-model-right Label {
        width: 1fr;
    }
    /* args is the field real configs actually grow (§0f: 19 tokens on the operator's biggest
       model) — flexible with headroom, not a fixed 5 rows. cmd and env stay fixed: cmd is one
       engine's whole command, env the operator's configs carry 0-1 lines of. */
    #f-args {
        height: 1fr;
        min-height: 12;
    }
    #f-cmd, #f-env {
        height: 5;
    }
    /* Plain Vertical, not VerticalScroll: these hold a handful of Selects/Inputs/a TextArea
       each and must size to their own content inside a VerticalScroll parent, the same c.3 fix
       as #f-llamacpp-fields (§0f) — a plain Vertical's framework default of height: 1fr
       resolves against the viewport, not the content, and clips. */
    #f-llamacpp-fields, #f-python-group, #f-args-group, #f-cmd-group {
        height: auto;
    }
    .form-hint {
        color: $text-muted;
        margin-top: $space-normal;
    }
    #form-error {
        color: $error;
        margin-top: $space-normal;
    }
    """

    def __init__(
        self,
        host_profile: dict,
        manifest: dict,
        models: dict,
        editing_id: str | None,
        repo_root: Path,
        app_ref: Any,
        resident: bool = False,
    ) -> None:
        super().__init__()
        # Seeds the ttl field for a NEW binding: 0 is "never unload" (upstream llama-swap), so
        # "Add Resident Model" prefills 0 and "Add Swappable" leaves it blank, which means
        # llama-swap's own default. Editing never re-seeds — the stored value wins.
        self.resident_seed = resident
        self.host_profile = host_profile
        self.manifest = manifest
        self.models = models
        self.editing_id = editing_id
        self.repo_root = repo_root
        self.app_ref = app_ref
        # #f-id autofill (Decision 5): touched once the operator types in it themselves, so a
        # later file pick no longer overwrites a chosen id. Always touched when editing — the
        # id is disabled there and never autofilled. _autofilling_id guards the reentrant
        # Input.Changed our own programmatic writes fire (on_input_changed must not mistake
        # that for the operator typing).
        self._id_touched = editing_id is not None
        self._autofilling_id = False
        self._model = (
            next((m for m in self.models.get("models", []) if m["id"] == editing_id), None)
            if editing_id
            else None
        )
        # Filenames on disk, relative to models_dir — the same shape models.yaml stores in
        # quant_file, so a selection can be written straight through.
        self._files = [f["name"] for f in list_model_files(Path(host_profile["paths"]["models_dir"]))]

    def _file_options(self) -> list[tuple[str, str]]:
        return [(name, name) for name in self._files]

    def _derived_id(self) -> str:
        """The llama-swap route name, from the chosen filename.

        Derived rather than typed (the id field was removed): the operator picks a file, and
        two bindings of the same file are disambiguated with a numeric suffix the way the HF
        Downloads scan used to do it.
        """
        selection = self.query_one("#f-quant-file", Select).value
        if selection is Select.BLANK:
            return ""
        stem = Path(str(selection)).stem
        clean = re.sub(r"[^a-zA-Z0-9_\.\-]", "-", stem).lower().strip("-") or "model"
        taken = {m["id"] for m in self.models.get("models", []) if m["id"] != self.editing_id}
        candidate, counter = clean, 2
        while candidate in taken:
            candidate = f"{clean}-{counter}"
            counter += 1
        return candidate

    def compose(self) -> ComposeResult:
        title = f"Edit Model: {self.editing_id}" if self.editing_id else "Add Model"
        m = self._model
        engine_val = m.get("engine", "llama-cpp") if m else "llama-cpp"
        # Args box holds llama_server_args for llama-cpp, args for python — same slot, same
        # parser (Decision 4). A brand-new model always starts blank, even though it opens on
        # engine llama-cpp by default: the --port ${PORT} python prefill only fires when the
        # operator actually switches the Select to python (on_select_changed), not here.
        if m:
            stored_args = m.get("llama_server_args") if engine_val == "llama-cpp" else m.get("args", [])
            initial_args_text = _args_tokens_to_lines(stored_args or [])
        else:
            initial_args_text = ""

        with Vertical(id="edit-model-dialog"):
            yield Static(title, id="edit-model-title")
            with Horizontal(id="edit-model-columns", classes="columns-responsive"):
              with VerticalScroll(id="edit-model-scroll"):
                # id: typed for python/unmanaged, autofilled from the chosen file for llama-cpp
                # until the operator types over it (_maybe_autofill_id / Decision 5). Disabled
                # when editing — renames are out of scope, the stored id wins.
                yield Label("id")
                yield Input(
                    id="f-id",
                    placeholder="letters, numbers, '_', '.', '-'",
                    value=self.editing_id or "",
                    disabled=self.editing_id is not None,
                )
                yield Label("engine")
                yield Select(_ENGINE_OPTIONS, id="f-engine", allow_blank=False, value=engine_val)

                with Vertical(id="f-llamacpp-fields"):
                    # Dropdowns over what is actually in models_dir, not free text. repo_id is
                    # gone from the form entirely: the weights are already on disk by the time
                    # a binding is created (HF Downloads tab), so the Hub id is provenance
                    # rather than something to retype. It is preserved when editing and
                    # recorded as "local" for a new binding.
                    # No value= here: Select.BLANK is literally `False` in Textual 8.2.8, and
                    # passing it to the constructor trips _validate_value ("Illegal select
                    # value False"). A blank Select is made by omitting value entirely; an
                    # existing selection is assigned in on_mount, the same way f-gpu already is.
                    yield Label("model file")
                    yield Select(self._file_options(), id="f-quant-file", allow_blank=True)
                    yield Label("mmproj file (optional — vision projector)")
                    yield Select(self._file_options(), id="f-mmproj-file", allow_blank=True)
                    yield Label("bind.gpu")
                    yield Select(self._gpu_options(), id="f-gpu", allow_blank=False)
                    yield Label("bind.backend")
                    yield Select([], id="f-backend", allow_blank=True)

                with Vertical(id="f-python-group"):
                    yield Label("python interpreter path")
                    yield Input(
                        id="f-python-interpreter",
                        placeholder="/opt/services/asr/.venv/bin/python",
                        value=(m.get("python", "") if m else ""),
                    )
                    yield Label("script path")
                    yield Input(
                        id="f-python-script",
                        placeholder="/opt/services/asr/asr_server.py",
                        value=(m.get("script", "") if m else ""),
                    )
                    yield Static(
                        "Runs on demand under llama-swap on ${PORT}. For an always-on systemd "
                        "service use the Scripts tab.",
                        classes="form-hint",
                    )

                yield Label("ttl (seconds; 0 = never unload, blank = llama-swap default)")
                # Blank for a new binding, not "0": 0 means never unload, so defaulting the
                # box to it would make every model created here resident. Only an explicit ttl
                # already in models.yaml is prefilled.
                yield Input(
                    id="f-ttl",
                    value=("0" if m is None and self.resident_seed else "" if m is None or "ttl" not in m else str(m["ttl"])),
                )
                yield Label("group (optional)")
                yield Input(id="f-group", placeholder="", value=m.get("group", "") if m else "")

                yield Label("check endpoint (optional; path only, e.g. /health)")
                yield Input(
                    id="f-check-endpoint",
                    placeholder="/health",
                    value=(m.get("check_endpoint", "") if m else ""),
                )

                yield Static("", id="form-error", classes="error-text")

              with VerticalScroll(id="edit-model-right"):
                  # llama_server_args and cmd are mutually exclusive by engine
                  # (_toggle_engine_fields) and share this slot, above env — both are
                  # multi-line and both deserve the width (QA item c.4).
                  with Vertical(id="f-args-group"):
                      yield Label(
                          "llama-server flags — one flag (and its value) per line",
                          id="f-args-label",
                      )
                      yield Button("Examples", id="btn-args-examples", classes="thin-button")
                      yield TextArea(initial_args_text, id="f-args")
                  with Vertical(id="f-cmd-group"):
                      yield Label(
                          "cmd — the complete command llama-swap runs. ${PORT} and "
                          "${MODEL_ID} are substituted."
                      )
                      yield TextArea(m.get("cmd", "") if m else "", id="f-cmd")

                  yield Label("env — one KEY=VALUE per line. Injected into the command's environment.")
                  yield Button("Examples", id="btn-env-examples", classes="thin-button")
                  yield TextArea("\n".join(m.get("env", [])) if m else "", id="f-env")

            with Horizontal(classes="action-row-primary"):
                yield Button("Save", id="btn-save", variant="primary", classes="thin-button")
                yield Button("Cancel", id="btn-cancel", classes="thin-button")

    def on_mount(self) -> None:
        m = self._model
        engine_val = m.get("engine", "llama-cpp") if m else "llama-cpp"
        self._toggle_engine_fields(engine_val)

        # Preselect the file(s) this model already names, but only when they still exist on
        # disk — a models.yaml entry can outlive its weights, and Select rejects a value that
        # is not among its options.
        for field, key in (("#f-quant-file", "quant_file"), ("#f-mmproj-file", "mmproj_file")):
            current = (m or {}).get(key)
            if current and current in self._files:
                self.query_one(field, Select).value = current

        gpus = self.host_profile.get("gpus", [])
        if gpus:
            selected_gpu = m.get("bind", {}).get("gpu") if m else gpus[0]["id"]
            if any(g["id"] == selected_gpu for g in gpus):
                self.query_one("#f-gpu", Select).value = selected_gpu
            else:
                self.query_one("#f-gpu", Select).value = gpus[0]["id"]
            selected_backend = m.get("bind", {}).get("backend") if m else None
            self._refresh_backend_options(str(self.query_one("#f-gpu", Select).value), selected=selected_backend)

    def _gpu_options(self) -> list[tuple[str, str]]:
        """Label carries the product name, value stays the configured id.

        Only the id is ever written to models.yaml — bind.gpu must match hosts/*.yaml. The name
        is there so "gpu-nvidia" is not the only thing distinguishing two accelerators at the
        moment you choose one. Names come from lspci, which does not wake a device.
        """
        try:
            pci = sysinfo.read_pci_gpus()
        except Exception:
            pci = []
        seen: dict[str, int] = {}
        options: list[tuple[str, str]] = []
        for gpu in self.host_profile.get("gpus", []):
            vendor = gpu.get("vendor", "")
            ordinal = seen.get(vendor, 0)
            seen[vendor] = ordinal + 1
            matches = [n for n in pci if vendor and vendor.lower() in n.lower()]
            name = matches[ordinal] if ordinal < len(matches) else None
            options.append((f"{gpu['id']} — {name}" if name else gpu["id"], gpu["id"]))
        return options

    def _backend_options_for_gpu(self, gpu_id: str) -> list[tuple[str, str]]:
        for g in self.host_profile.get("gpus", []):
            if g["id"] == gpu_id:
                return [(b, b) for b in g.get("backends", [])]
        return []

    def _refresh_backend_options(self, gpu_id: str, selected: str | None = None) -> None:
        backend_select = self.query_one("#f-backend", Select)
        options = self._backend_options_for_gpu(gpu_id)
        backend_select.set_options(options)
        if selected is not None and any(value == selected for _, value in options):
            backend_select.value = selected
        elif options:
            backend_select.value = options[0][1]
        else:
            backend_select.value = Select.BLANK

    def _toggle_engine_fields(self, engine: str) -> None:
        is_llama = engine == "llama-cpp"
        is_python = engine == "python"
        is_unmanaged = engine == "unmanaged"
        self.query_one("#f-llamacpp-fields").display = is_llama
        self.query_one("#f-python-group").display = is_python
        self.query_one("#f-args-group").display = is_llama or is_python
        self.query_one("#f-cmd-group").display = is_unmanaged
        # Examples snippets are llama-server CLI flags — meaningless for a python script's own
        # args, and no python-args variant exists (plans/09 Phase 3 item 4: prefer hiding).
        self.query_one("#btn-args-examples").display = is_llama
        args_label = self.query_one("#f-args-label", Label)
        if is_python:
            args_label.update("script arguments — one per line")
        else:
            args_label.update("llama-server flags — one flag (and its value) per line")

    def _maybe_autofill_id(self) -> None:
        """#f-id from the chosen file, only for a NEW llama-cpp model that hasn't been typed
        into yet (Decision 5) — python/unmanaged have no file select to derive from."""
        if self.editing_id or self._id_touched:
            return
        derived = self._derived_id()
        if derived:
            self._autofilling_id = True
            self.query_one("#f-id", Input).value = derived
            self._autofilling_id = False

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "f-engine":
            engine = str(event.value)
            args = self.query_one("#f-args", TextArea)
            # Switching a NEW model away from python: drop the --port ${PORT} prefill this
            # modal itself inserted, but only if the box still holds exactly that — an operator
            # who typed real args, or edited the prefill, keeps whatever is there (coordinator
            # course-correction, mid-package).
            if engine != "python" and self._model is None and args.text == _PYTHON_ARGS_PREFILL:
                args.text = ""
            self._toggle_engine_fields(engine)
            if engine == "python" and self._model is None and not args.text.strip():
                args.text = _PYTHON_ARGS_PREFILL
        elif event.select.id == "f-gpu":
            self._refresh_backend_options(str(event.value))
        elif event.select.id == "f-quant-file":
            self._maybe_autofill_id()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "f-id" and not self._autofilling_id:
            self._id_touched = True

    def _set_form_error(self, text: str) -> None:
        self.query_one("#form-error", Static).update(text)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def _build_model_from_form(self) -> tuple[dict | None, str | None]:
        engine = self.query_one("#f-engine", Select).value
        if engine is Select.BLANK:
            return None, "engine is required"
        engine = str(engine)

        model_id = self.query_one("#f-id", Input).value.strip()
        if not model_id:
            return None, "id is required"
        if not re.match(r"^[a-zA-Z0-9_.\-]+$", model_id):
            return None, "id may only contain letters, numbers, '_', '.', '-'"

        # Blank means unspecified, NOT 0. Upstream llama-swap: "a ttl of 0 will mean never
        # unload", default "-1 (use global default)" — so coercing an empty box to 0 silently
        # pinned the model in VRAM (fixed in swap.py; this is the same trap on the input side).
        ttl_raw = self.query_one("#f-ttl", Input).value.strip()
        ttl: int | None = None
        if ttl_raw:
            try:
                ttl = int(ttl_raw)
            except ValueError:
                return None, "ttl must be an integer, or blank for llama-swap's default"
            if ttl < 0:
                return None, "ttl must be >= 0, or blank for llama-swap's default"

        group = self.query_one("#f-group", Input).value.strip()
        check_endpoint = self.query_one("#f-check-endpoint", Input).value.strip()
        env_lines = [line.strip() for line in self.query_one("#f-env", TextArea).text.splitlines() if line.strip()]

        model: dict[str, Any] = {"id": model_id, "engine": engine}

        if engine == "llama-cpp":
            quant_sel = self.query_one("#f-quant-file", Select).value
            mmproj_sel = self.query_one("#f-mmproj-file", Select).value
            gpu = self.query_one("#f-gpu", Select).value
            backend = self.query_one("#f-backend", Select).value
            if quant_sel is Select.BLANK:
                return None, "pick a model file — download one on the HF Downloads tab first"
            if gpu is Select.BLANK or backend is Select.BLANK:
                return None, "bind.gpu and bind.backend are required for llama-cpp models"
            quant_file = str(quant_sel)
            # repo_id stays required by the schema but is no longer typed: preserved when
            # editing, "local" for a new binding whose weights are simply already on disk.
            model["repo_id"] = (self._model or {}).get("repo_id") or "local"
            model["quant_file"] = quant_file
            if mmproj_sel is not Select.BLANK and str(mmproj_sel) != quant_file:
                model["mmproj_file"] = str(mmproj_sel)
            model["bind"] = {"gpu": str(gpu), "backend": str(backend)}
            try:
                args_tokens = _args_lines_to_tokens(self.query_one("#f-args", TextArea).text)
            except ValueError as e:
                return None, f"could not parse arguments — {e}"
            if args_tokens:
                model["llama_server_args"] = args_tokens
        elif engine == "python":
            python_path = self.query_one("#f-python-interpreter", Input).value.strip()
            script_path = self.query_one("#f-python-script", Input).value.strip()
            if not python_path or not script_path:
                return None, "python interpreter path and script path are required for python models"
            model["python"] = python_path
            model["script"] = script_path
            try:
                args_tokens = _args_lines_to_tokens(self.query_one("#f-args", TextArea).text)
            except ValueError as e:
                return None, f"could not parse arguments — {e}"
            if args_tokens:
                model["args"] = args_tokens
        else:  # unmanaged
            cmd = self.query_one("#f-cmd", TextArea).text
            if not cmd.strip():
                return None, "cmd is required for unmanaged models"
            model["cmd"] = cmd

        if env_lines:
            model["env"] = env_lines
        if ttl is not None:
            model["ttl"] = ttl
        if group:
            model["group"] = group
        if check_endpoint:
            model["check_endpoint"] = check_endpoint
        return model, None

    async def _insert_example(self, kind: str, target_id: str) -> None:
        chosen = await self.app.push_screen_wait(ExamplesModal(kind))
        if not chosen:
            return
        ta = self.query_one(target_id, TextArea)
        ta.text = f"{ta.text.rstrip()}\n{chosen}" if ta.text.strip() else chosen

    @work
    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(False)
            return
        if event.button.id == "btn-args-examples":
            await self._insert_example("args", "#f-args")
            return
        if event.button.id == "btn-env-examples":
            await self._insert_example("env", "#f-env")
            return
        if event.button.id != "btn-save":
            return

        model, error = self._build_model_from_form()
        if error:
            self._set_form_error(error)
            return

        existing_models = self.models.get("models", [])
        others = [m for m in existing_models if m["id"] != self.editing_id] if self.editing_id else list(existing_models)
        if any(m["id"] == model["id"] for m in others):
            self._set_form_error(f"duplicate model id {model['id']!r}")
            return
        candidate = {"models": others + [model]}

        try:
            validated = schema.validate_models_dict(candidate, self.host_profile, self.manifest, source="models.yaml")
        except schema.ValidationError as e:
            self._set_form_error(str(e))
            return

        verb = "Save changes to" if self.editing_id else "Add"
        confirmed = await self.app.push_screen_wait(
            ConfirmModal(f"{verb} model {model['id']!r} in models.yaml?", confirm_label="Save", danger=True)
        )
        if not confirmed:
            return

        target = self.repo_root / "models.yaml"
        target.write_text(yaml.safe_dump(validated, sort_keys=False), encoding="utf-8")
        self.app_ref.reload_models()
        self.app.notify(f"saved model {model['id']!r}")
        self.dismiss(True)


class ImportModelsModal(ModalScreen[bool]):
    """Modal dialog for importing discovered or pasted models into models.yaml."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    ImportModelsModal {
        align: center middle;
    }
    #import-dialog {
        width: 85;
        height: 85%;
        border: thick $background 80%;
        background: $surface;
        padding: $space-normal $space-section;
    }
    #import-title {
        text-style: bold;
        color: $accent;
        margin-bottom: $space-normal;
    }
    #import-scroll {
        height: 1fr;
    }
    #import-error {
        color: $error;
        margin-top: $space-normal;
    }
    """

    def __init__(
        self,
        host_profile: dict,
        manifest: dict,
        models: dict,
        repo_root: Path,
        app_ref: Any,
    ) -> None:
        super().__init__()
        self.host_profile = host_profile
        self.manifest = manifest
        self.models = models
        self.repo_root = repo_root
        self.app_ref = app_ref
        self._import_candidates: dict[str, dict[str, Any]] = {}
        self._import_selected: set[str] = set()

    def compose(self) -> ComposeResult:
        with Vertical(id="import-dialog"):
            yield Static("Import Models from Llama-Swap", id="import-title")
            with VerticalScroll(id="import-scroll"):
                table = SingleClickDataTable(
                    id="import-table", zebra_stripes=True, classes="data-table"
                )
                table.cursor_type = "row"
                yield table
                yield Static("", id="import-error", classes="error-text")
            with Horizontal(classes="action-row-primary"):
                yield Button("Import selected", id="btn-import-selected", variant="primary", classes="thin-button")
                yield Button("Paste config.yaml", id="btn-import-paste", classes="thin-button")
                yield Button("Cancel", id="btn-import-close", classes="thin-button")

    def on_mount(self) -> None:
        table = self.query_one("#import-table", SingleClickDataTable)
        table.add_column("", width=3)
        table.add_column("ID", width=24)
        table.add_column("Engine", width=12)
        table.add_column("cmd", width=60)

        # Discover from state_dir config.yaml if present
        state_dir = self.host_profile.get("paths", {}).get("state_dir")
        if state_dir:
            config_path = Path(state_dir) / "llama-swap" / "config.yaml"
            if config_path.exists():
                try:
                    content = config_path.read_text(encoding="utf-8")
                    proposed = swap.parse_config_for_import(content, self.host_profile)
                    self._import_candidates = {r["model"]["id"]: r["model"] for r in proposed}
                except Exception:
                    pass
        self._refresh_import_table()

    def _refresh_import_table(self) -> None:
        table = self.query_one("#import-table", SingleClickDataTable)
        table.clear()
        for model_id, model in self._import_candidates.items():
            cmd_preview = model.get("cmd", "")[:80] + ("…" if len(model.get("cmd", "")) > 80 else "")
            # rich.text.Text, not raw str (DESIGN.md §4.6 / Phase 0a.6): the app console has
            # markup=True, so an unmanaged engine's cmd line (very likely to contain brackets)
            # would have that span silently eaten by Rich as a markup tag.
            table.add_row(
                selection_marker(model_id in self._import_selected),
                Text(model_id),
                Text(model.get("engine", "unmanaged")),
                Text(cmd_preview),
                key=model_id,
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "import-table" or event.row_key is None or event.row_key.value is None:
            return
        model_id = str(event.row_key.value)
        if model_id in self._import_selected:
            self._import_selected.discard(model_id)
        else:
            self._import_selected.add(model_id)
        self._refresh_import_table()

    def action_cancel(self) -> None:
        self.dismiss(False)

    @work
    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-import-close":
            self.dismiss(False)
        elif bid == "btn-import-paste":
            await self._paste_config()
        elif bid == "btn-import-selected":
            await self._confirm_and_import()

    async def _paste_config(self) -> None:
        text = await self.app.push_screen_wait(ConfigPasteModal())
        if text is None or not text.strip():
            return
        try:
            proposed = swap.parse_config_for_import(text, self.host_profile)
        except ValueError as e:
            self.query_one("#import-error", Static).update(f"parse failed: {e}")
            return
        if not proposed:
            self.query_one("#import-error", Static).update("no models found in pasted config.yaml")
            return
        self.query_one("#import-error", Static).update("")
        self._import_candidates = {r["model"]["id"]: r["model"] for r in proposed}
        self._import_selected = set()
        self._refresh_import_table()

    async def _confirm_and_import(self) -> None:
        error_widget = self.query_one("#import-error", Static)
        error_widget.update("")
        if not self._import_selected:
            error_widget.update("tick at least one row to import")
            return

        existing_ids = {m["id"] for m in self.models.get("models", [])}
        colliding = sorted(self._import_selected & existing_ids)
        if colliding:
            error_widget.update(f"id(s) already exist in models.yaml: {', '.join(colliding)}")
            return

        to_import = [self._import_candidates[mid] for mid in sorted(self._import_selected)]
        candidate = {"models": list(self.models.get("models", [])) + to_import}
        try:
            validated = schema.validate_models_dict(candidate, self.host_profile, self.manifest, source="models.yaml")
        except schema.ValidationError as e:
            error_widget.update(str(e))
            return

        confirmed = await self.app.push_screen_wait(
            ConfirmModal(
                f"Import {len(to_import)} model(s) into models.yaml?\n{', '.join(m['id'] for m in to_import)}",
                confirm_label="Import",
                danger=True,
            )
        )
        if not confirmed:
            return

        target = self.repo_root / "models.yaml"
        target.write_text(yaml.safe_dump(validated, sort_keys=False), encoding="utf-8")
        self.app_ref.reload_models()
        self.app.notify(f"imported {len(to_import)} model(s)")
        self.dismiss(True)


class DeployScreen(CockpitScreenBase):
    BINDINGS = [("i", "open_swap_repo", "llama-swap install page")]

    """Cockpit "Deploy" tab conforming to Archetype B (Table-Driven Inventory)."""

    DEFAULT_CSS = """
    DeployScreen {
        height: 1fr;
    }
    /* Not a Button — a plain rule character marking off "Apply & Restart" (the consequential
       action) from the rest of the row, without touching any Button's own geometry/margin
       (DESIGN.md §9 forbids per-screen Button CSS overrides). */
    DeployScreen .action-row-divider {
        width: 3;
        color: $text-muted;
        text-align: center;
    }
    """

    def __init__(
        self,
        host_profile: dict,
        manifest: dict,
        models: dict,
        runner: Runner,
        repo_root: Path,
        app_ref: Any,
    ) -> None:
        super().__init__()
        self.host_profile = host_profile
        self.manifest = manifest
        self.models = models
        self.runner = runner
        self.repo_root = repo_root
        self.app_ref = app_ref
        # model id -> llama-swap state, from GET /running. Empty when llama-swap is down, which
        # renders as "—" rather than an error: not-running is a normal state for this table.
        # NOT `_running`: Textual's MessagePump already owns that attribute as a bool, and
        # shadowing it made _populate_table fail with "'bool' object has no attribute 'get'".
        self._running_models: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            # Split by what each row costs at rest, which is the question an operator actually
            # has about a model catalogue: "what is pinned in my VRAM right now?" Both tables
            # carry identical columns and actions — the split is the information.
            # Shown only when llama-swap is absent: every row in both tables is a route it
            # serves, so without it this whole tab describes something that cannot run.
            yield Static("", id="swap-missing", classes="error-text")

            yield Static("Resident (never unloaded from vRAM)", classes="section-title")
            yield SingleClickDataTable(id="models-resident", classes="data-table")
            with Horizontal(classes="action-row-secondary"):
                yield Button("Add Resident Model", id="btn-add-resident", classes="thin-button")

            yield Static("Swappable (evicted when idle)", classes="section-title")
            yield SingleClickDataTable(id="models-swappable", classes="data-table")
            with Horizontal(classes="action-row-secondary"):
                yield Button("Add Swappable Model", id="btn-add-swappable", classes="thin-button")

            with Horizontal(classes="action-row-primary"):
                yield Button("Apply & Restart llama-swap", id="btn-apply", variant="primary", classes="thin-button")
                yield Static("│", classes="action-row-divider")
                yield Button("Import from llama-swap", id="btn-import-toggle", classes="thin-button")
                yield Button("Preview llama-swap YAML", id="btn-preview-yaml", classes="thin-button")
            yield Static("", id="status-message", classes="status-text")

    def on_mount(self) -> None:
        # The two tables carry different columns now. Resident drops TTL — it is 0 or group
        # membership by definition — to pay for the Start/Stop toggle; Swappable keeps TTL
        # because there the number is the whole point. Budgets: 96 + 2*8 = 112 and
        # 97 + 2*8 = 113, both inside 115 (DESIGN.md §4).
        resident = self.query_one("#models-resident", SingleClickDataTable)
        resident.cursor_type = "row"
        resident.add_column("ID", width=22)
        resident.add_column("Engine", width=10)
        resident.add_column("GPU", width=9)
        resident.add_column("Backend", width=9)
        resident.add_column("Group", width=10)
        resident.add_column("Status", width=9)
        resident.add_action_column(
            TableAction(
                "run",
                lambda mid: "Stop" if self._running_models.get(mid) else "Start",
                width=9,
                confirm="{action} {row}?",
            )
        )
        resident.add_action_column(TableAction("edit", "Edit"))
        resident.add_action_column(
            TableAction("delete", "Delete", destructive=True, confirm="Delete model {row} from models.yaml?")
        )

        swappable = self.query_one("#models-swappable", SingleClickDataTable)
        swappable.cursor_type = "row"
        swappable.add_column("ID", width=22)
        swappable.add_column("Engine", width=10)
        swappable.add_column("GPU", width=9)
        swappable.add_column("Backend", width=9)
        swappable.add_column("Group", width=10)
        swappable.add_column("TTL", width=10)
        swappable.add_column("Status", width=9)
        swappable.add_action_column(TableAction("edit", "Edit"))
        swappable.add_action_column(
            TableAction("delete", "Delete", destructive=True, confirm="Delete model {row} from models.yaml?")
        )
        self._populate_table()

    @staticmethod
    def _is_resident(model: dict) -> bool:
        """True when this model, once requested, stays in VRAM.

        Two ways that happens, both from upstream llama-swap's own documentation:
        `ttl: 0` is "a ttl of 0 will mean never unload", and group membership is resident here
        because this repo only ever emits `swap: false` for a group
        (provision/steps/swap.py::_generate_config), which upstream defines as "all members can
        run together, no swapping".

        An omitted ttl is NOT resident: upstream's default is "-1 (use global default)", and
        swap.py now leaves it out rather than writing 0.
        """
        return model.get("ttl") == 0 or bool(model.get("group"))

    def action_open_swap_repo(self) -> None:
        self.app.open_url(self.manifest.get("llama_swap", {}).get("repo", "https://github.com/mostlygeek/llama-swap"))

    def _refresh_swap_notice(self) -> None:
        """Every row here is a llama-swap route, so say plainly when llama-swap is missing.

        `App.open_url` rather than printing a URL to copy — README's own multi-surface rule
        (§7) routes external links through it so this still works under textual-serve.
        """
        notice = self.query_one("#swap-missing", Static)
        if Path("/usr/local/bin/llama-swap").exists():
            notice.update("")
            notice.display = False
            return
        notice.display = True
        notice.update(
            "REQUIRES LLAMA-SWAP — not installed at /usr/local/bin/llama-swap. "
            "Nothing below can be served until it is. Press 'i' to open the install page, "
            "or deploy it from Settings > System Services > Apply Service Settings."
        )

    def _populate_table(self) -> None:
        self._refresh_swap_notice()
        resident = self.query_one("#models-resident", SingleClickDataTable)
        swappable = self.query_one("#models-swappable", SingleClickDataTable)
        resident.clear()
        swappable.clear()
        for m in self.models.get("models", []):
            is_resident = self._is_resident(m)
            table = resident if is_resident else swappable
            if m["engine"] == "llama-cpp":
                gpu = m.get("bind", {}).get("gpu", "")
                backend = m.get("bind", {}).get("backend", "")
            else:
                gpu, backend = "", ""
            state = self._running_models.get(m["id"])
            status_cell = Text(state or "—", style="green" if state else "dim")
            # rich.text.Text, not raw str (DESIGN.md §4.6 / Phase 0a.6): the app console has
            # markup=True, so an operator-chosen model id or repo_id containing brackets would
            # have that span silently eaten by Rich as a markup tag.
            table.add_row(
                Text(m["id"]),
                Text(m["engine"]),
                Text(gpu),
                Text(backend),
                Text(m.get("group", "")),
                *(
                    (status_cell,)
                    if is_resident
                    else (
                        Text("0 (pinned)" if m.get("ttl") == 0 else str(m.get("ttl", "default"))),
                        status_cell,
                    )
                ),
                *table.action_cells(m["id"]),
                key=m["id"],
            )

    def on_first_view(self) -> None:
        self.on_refresh_requested()

    def on_refresh_requested(self) -> None:
        """Called by CockpitApp.action_refresh_all — pick up fresh models.yaml."""
        self.models = self.app_ref.models
        self._populate_table()
        self._refresh_running()

    def _set_status(self, text: str) -> None:
        self.query_one("#status-message", Static).update(text)

    # -- Actions ---------------------------------------------------------------

    @work
    async def _on_add_model(self, resident: bool = False) -> None:
        saved = await self.app.push_screen_wait(
            EditModelModal(
                host_profile=self.host_profile,
                manifest=self.manifest,
                models=self.models,
                editing_id=None,
                repo_root=self.repo_root,
                app_ref=self.app_ref,
                resident=resident,
            )
        )
        if saved:
            self.models = self.app_ref.models
            self._populate_table()
            self._set_status("model added — models.yaml written")

    async def _edit_model(self, model_id: str) -> None:
        saved = await self.app.push_screen_wait(
            EditModelModal(
                host_profile=self.host_profile,
                manifest=self.manifest,
                models=self.models,
                editing_id=model_id,
                repo_root=self.repo_root,
                app_ref=self.app_ref,
            )
        )
        if saved:
            self.models = self.app_ref.models
            self._populate_table()
            self._set_status(f"model {model_id!r} updated — models.yaml written")

    async def _delete_model(self, model_id: str) -> None:
        # Already confirmed: this is a TableAction(destructive=True), so CockpitScreenBase's
        # _on_table_action_invoked has already run the ConfirmModal before calling here.
        new_list = [m for m in self.models.get("models", []) if m["id"] != model_id]
        candidate = {"models": new_list}
        try:
            validated = schema.validate_models_dict(candidate, self.host_profile, self.manifest, source="models.yaml")
        except schema.ValidationError as e:
            self._set_status(f"delete blocked by validation: {e}")
            return

        target = self.repo_root / "models.yaml"
        target.write_text(yaml.safe_dump(validated, sort_keys=False), encoding="utf-8")
        self.app_ref.reload_models()
        self.models = self.app_ref.models
        self._populate_table()
        self._set_status(f"deleted model {model_id!r} — models.yaml written")
        self.app.notify(f"deleted model {model_id!r}")

    @work
    async def _on_import_toggle(self) -> None:
        imported = await self.app.push_screen_wait(
            ImportModelsModal(
                host_profile=self.host_profile,
                manifest=self.manifest,
                models=self.models,
                repo_root=self.repo_root,
                app_ref=self.app_ref,
            )
        )
        if imported:
            self.models = self.app_ref.models
            self._populate_table()
            self._set_status("imported models — models.yaml written")

    def _on_preview_yaml(self) -> None:
        try:
            text = swap._generate_config(self.host_profile, self.models)
        except Exception as e:
            self._set_status(f"failed to generate config preview: {e}")
            return
        self.app.push_screen(InfoModal("Generated config.yaml preview", text))

    @work
    async def _confirm_and_apply(self) -> None:
        # DESIGN.md §5 tier 2: installs units and restarts llama-swap.
        confirmed = await self.confirm(
            "Deploy llama-swap service and apply configuration to systemd?",
            confirm_label="Deploy",
            mutates_system=True,
            requires_root=True,
        )
        if not confirmed:
            return
        self._set_status("deploying configuration...")
        self._apply_in_background()

    @work(thread=True)
    def _apply_in_background(self) -> None:
        # DESIGN.md §6: toasts on both success and failure for background operations.
        try:
            swap.run(self.host_profile, self.manifest, self.models, self.privileged_runner, self.repo_root)
        except SystemExit as e:
            msg = f"deploy failed: {e.code}"
        except Exception as e:
            msg = f"deploy failed: {e}"
        else:
            msg = "deployed configuration and restarted llama-swap"
            self.app.call_from_thread(self._set_status, msg)
            self.app.call_from_thread(self.app.notify, msg)
            return
        self.app.call_from_thread(self._set_status, msg)
        self.app.call_from_thread(self.app.notify, msg, severity="error")

    # -- Per-row table actions --------------------------------------------------

    @work(thread=True)
    def _refresh_running(self) -> None:
        """One GET /running, off the main thread. llama-swap owns the process lifecycle; this
        only reports it."""
        running = swap.running_models(self.host_profile)
        self.app.call_from_thread(self._apply_running, running)

    def _apply_running(self, running: dict[str, str]) -> None:
        if not self.is_mounted:
            return
        self._running_models = running
        self._populate_table()

    @work(thread=True)
    def _run_model_action(self, verb: str, model_id: str) -> None:
        """Start warms via GET /upstream/<id>/ — llama-swap has no load endpoint, and this is
        the same zero-token trick its own startup preload uses. Stop is the real
        POST /api/models/unload/<id>."""
        error = (
            swap.unload_model(self.host_profile, model_id)
            if verb == "stop"
            else swap.load_model(self.host_profile, model_id)
        )
        if error:
            self.app.call_from_thread(self.app.notify, f"{model_id}: {verb} failed — {error}", severity="error")
        else:
            self.app.call_from_thread(self.app.notify, f"{model_id}: {verb} complete")
        self.app.call_from_thread(self._refresh_running)

    async def handle_table_action(self, action_id: str, row_key: str, table) -> None:
        if action_id == "run":
            self._run_model_action("stop" if self._running_models.get(row_key) else "start", row_key)
        elif action_id == "edit":
            await self._edit_model(row_key)
        elif action_id == "delete":
            await self._delete_model(row_key)

    # -- Button Dispatch -------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # No `await` on a @work method: the decorator returns a Worker, which is not
        # awaitable — awaiting one raises TypeError and takes down the app. The worker is
        # already running by the time the call returns; there is nothing to wait for here.
        bid = event.button.id
        if bid == "btn-apply":
            self._confirm_and_apply()
        elif bid in ("btn-add-resident", "btn-add-swappable"):
            # Which button was pressed seeds the new binding's residency, so "Add" under a
            # table puts the model in that table rather than wherever the ttl default lands.
            self._on_add_model(resident=bid == "btn-add-resident")
        elif bid == "btn-import-toggle":
            self._on_import_toggle()
        elif bid == "btn-preview-yaml":
            self._on_preview_yaml()
