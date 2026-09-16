"""Cockpit "Settings" tab.

First-run (host_profile is None): the ONLY tab shown (see app.py, which titles it
"First setup") — presents a multi-step wizard using ContentSwitcher:
  - Step 1: Host identity & directory paths
  - Step 2: Network bind & GPU acceleration (DataTable + inline add GPU form)
  - Step 3: Review host configuration & deploy (YAML preview + dummy/real deploy)
A successful real deploy writes hosts/<hostname>.yaml and exits asking for restart.

Normal (host_profile is a real dict): structural fields (network/paths/retain_builds/hf)
stay editable via "Save host profile" with a restart notice. Configured GPUs are displayed
in a clean DataTable.

Operational sections apply live: WOL status/enable, GPU driver-drift status/check, and
service + update-check settings (which also nudges provision.steps.swap to (re)install
the systemd unit/timers).
"""
from __future__ import annotations

import copy
import os
import socket
import subprocess
from pathlib import Path
from typing import Any

import yaml
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    ContentSwitcher,
    Input,
    Label,
    Select,
    Static,
    Switch,
    TabbedContent,
    TabPane,
    TextArea,
)

from cockpit.widgets import (
    CockpitDataTable,
    CockpitScreenBase,
    ConfirmModal,
    InfoModal,
    run_shell_capture,
)
from provision import schema
from provision.common import Runner
from provision.steps import drivers, swap, tailscale, wol

_RESTART_POLICY_OPTIONS = [("on-failure", "on-failure"), ("always", "always"), ("no", "no")]
_GPU_VENDOR_OPTIONS = [("nvidia", "nvidia"), ("amd", "amd"), ("intel", "intel")]

# hosts/example.yaml's placeholder convention for WOL fields that are no longer part of the
# structural form (moved to their own section, normal mode only).
_WOL_PLACEHOLDER = {"interface": "TODO", "mac": "00:00:00:00:00:00"}

# The exact one-liner requested for the "List PCIe devices" popup.
_LSPCI_CMD = (
    'i=0; lspci | grep -E "VGA compatible controller|3D controller" | '
    "while read -r line; do name=$(echo \"$line\" | cut -d':' -f3- | "
    "sed 's/^ //; s/ (rev .*//'); echo \"gpu$i: $name\"; i=$((i+1)); done"
)


def _root_hint() -> str:
    """Appended to a failed apply. This step installs into /usr/local/bin and writes
    /etc/systemd/system (README "Privileges"), so an unprivileged cockpit fails with a bare
    `install ... returned non-zero exit status 1` that says nothing about why."""
    if os.geteuid() == 0:
        return ""
    return (
        f" — cockpit is running as uid {os.geteuid()}; this step installs into /usr/local/bin "
        "and /etc/systemd/system, which needs root (sudo bin/cockpit)."
    )


def _default_paths() -> dict[str, str]:
    """A single root under the invoking user's home, with subfolders — proposed as real
    prefilled values (not just placeholder ghost text), editable before saving."""
    root = str(Path.home() / "llm-server-cockpit")
    return {"models_dir": f"{root}/models", "state_dir": f"{root}/state", "prefix_root": f"{root}/builds"}


class SettingsScreen(CockpitScreenBase):
    """Mounted inside a TabPane by cockpit/app.py — not a Textual Screen."""

    DEFAULT_CSS = """
    SettingsScreen {
        height: 1fr;
    }
    SettingsScreen #wizard-switcher {
        height: 1fr;
    }
    /* No side padding: the first-run wizard sits at the same side inset as every normal-mode
       screen, which is #main-tabs' $space-edge and nothing else (DESIGN.md §2). */
    SettingsScreen #step-identity, SettingsScreen #step-hardware, SettingsScreen #step-review {
        height: 1fr;
    }
    SettingsScreen #settings-tabs {
        height: 1fr;
    }
    SettingsScreen Label {
        margin-top: $space-normal;
    }
    SettingsScreen .hint {
        color: $text-muted;
        margin-bottom: $space-normal;
    }
    SettingsScreen #vpn-resolve-preview {
        color: $text-muted;
    }
    SettingsScreen .data-table {
        margin-top: $space-normal;
    }
    SettingsScreen .settings-columns {
        height: auto;
    }
    SettingsScreen #gpu-add-form {
        border: solid $accent;
        padding: $space-normal;
        margin-top: $space-normal;
        margin-bottom: $space-normal;
        height: auto;
    }
    SettingsScreen #review-yaml {
        height: 1fr;
        min-height: 14;
        margin-top: $space-normal;
        margin-bottom: $space-normal;
    }
    """

    def __init__(
        self,
        host_profile: dict | None,
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
        # First-run only: in-memory GPU rows being built up before the profile exists —
        # the schema requires gpus: minItems 1, so this always starts with one blank entry
        # matching the previous single-GPU default (id=gpu0/vendor=nvidia/backends=cuda).
        self._pending_gpus: list[dict[str, Any]] = [{"id": "gpu0", "vendor": "nvidia", "backends": ["cuda"]}]
        # NIC name -> live MAC, refreshed by _populate_wol_form. Empty on a host without
        # sysfs (or in the first-run wizard, where the WOL form isn't composed at all).
        self._iface_macs: dict[str, str] = {}
        # Driver Status used to be a standalone text dump below the GPU table; it's now a
        # per-row column (item 8b), so the check result is kept per-gpu-id here and applied by
        # re-rendering the table rather than updating a separate Static.
        self._driver_status_by_gpu: dict[str, str] = {}
        self._driver_status_default = "not checked yet"

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        if self.host_profile is None:
            yield from self._compose_wizard()
        else:
            yield from self._compose_normal()

    def _compose_wizard(self) -> ComposeResult:
        with ContentSwitcher(initial="step-identity", id="wizard-switcher"):
            with VerticalScroll(id="step-identity"):
                yield Static("Step 1 of 3: Host Identity & Directory Paths", classes="panel-title")
                yield Label("Hostname")
                yield Input(id="f-hostname")
                yield Label("Builds to keep")
                yield Input(id="f-retain")
                yield Label("Model files directory")
                yield Input(id="f-models-dir")
                yield Label("App state directory")
                yield Input(id="f-state-dir")
                yield Label("Build output directory")
                yield Input(id="f-prefix-root")
                yield Static("", id="step-identity-error", classes="error-text")
                with Horizontal(classes="action-row-primary"):
                    yield Button("Next: Hardware & Network →", id="btn-next-hardware", variant="primary", classes="thin-button")

            with VerticalScroll(id="step-hardware"):
                yield Static("Step 2 of 3: Network Bind & GPU Acceleration", classes="panel-title")
                yield Static("Network bind", classes="section-title")
                yield Label("VPN interface (not an IP)")
                with Horizontal(classes="inline-row"):
                    yield Input(id="f-vpn-interface", placeholder="e.g. tailscale0, wg0")
                    yield Button("ip a", id="btn-ip-a")  # inline archetype (DESIGN.md §9): bare Button inside .inline-row
                yield Static("", id="vpn-resolve-preview")
                yield Label("Gateway port")
                yield Input(id="f-port")
                yield Label("Hugging Face token env var")
                yield Input(id="f-token-env")

                yield Static("GPUs", classes="section-title")
                yield CockpitDataTable(id="gpu-table", zebra_stripes=True, classes="data-table")
                with Horizontal(classes="action-row-primary"):
                    yield Button("Add GPU", id="btn-add-gpu", variant="primary", classes="thin-button")
                    yield Button("Remove Selected", id="btn-remove-gpu", variant="error", classes="thin-button")
                    yield Button("List PCIe devices", id="btn-lspci", classes="thin-button")

                with Vertical(id="gpu-add-form"):
                    yield Static("Add GPU", classes="panel-title")
                    yield Label("GPU ID")
                    yield Input(id="gpu-add-id", placeholder="e.g. gpu0")
                    yield Label("Vendor")
                    yield Select(_GPU_VENDOR_OPTIONS, id="gpu-add-vendor", allow_blank=False, value="nvidia")
                    yield Label("Backends (comma-separated: cuda, rocm, vulkan, sycl)")
                    yield Input(id="gpu-add-backends", placeholder="cuda")
                    yield Static("", id="gpu-add-error", classes="error-text")
                    with Horizontal(classes="action-row-primary"):
                        yield Button("Save GPU", id="btn-gpu-save", classes="thin-button", variant="primary")
                        yield Button("Cancel", id="btn-gpu-cancel", classes="thin-button")

                yield Static("", id="step-hardware-error", classes="error-text")
                with Horizontal(classes="action-row-primary"):
                    yield Button("← Back: Host Identity", id="btn-back-identity", classes="thin-button")
                    yield Button("Next: Review & Deploy →", id="btn-next-review", variant="primary", classes="thin-button")

            with VerticalScroll(id="step-review"):
                yield Static("Step 3 of 3: Review Host Configuration & Deploy", classes="panel-title")
                yield TextArea(id="review-yaml", language="yaml", read_only=True)
                yield Static("", id="step-review-error", classes="error-text")
                yield Static("", id="step-review-status", classes="status-text")
                with Horizontal(classes="action-row-primary"):
                    yield Button("← Back: Hardware & Network", id="btn-back-hardware", classes="thin-button")
                    yield Button("Preview YAML", id="btn-dummy-deploy", classes="thin-button")
                    yield Button("Deploy Host Profile", id="btn-real-deploy", variant="primary", classes="thin-button")

    def _compose_normal(self) -> ComposeResult:
        with TabbedContent(initial="settings-tab-host", id="settings-tabs"):
            with TabPane("Host Profile", id="settings-tab-host"):
                with VerticalScroll():
                  # Side by side above 121 cells, stacked below it (SHARED_CSS
                  # .columns-responsive, the same hook the Dashboard's two columns use). With
                  # .form-field capped at 40 a form row is 60 cells, so two panels fit inside
                  # the 115-cell usable width — which is the point: these sub-tabs were one
                  # tall column that had to be scrolled to reach the save buttons.
                  with Horizontal(classes="columns-responsive settings-columns"):
                   with Vertical(classes="settings-column"):
                    with Vertical(classes="panel"):
                        yield Static("Host Identity & Network", classes="panel-title")
                        with Horizontal(classes="form-row"):
                            yield Static("Host Name", classes="form-label")
                            yield Input(id="f-hostname", classes="form-field")
                        with Horizontal(classes="form-row"):
                            yield Static("VPN Interface", classes="form-label")
                            with Horizontal(classes="inline-row"):
                                yield Input(id="f-vpn-interface", placeholder="e.g. tailscale0, wg0")
                                yield Button("ip a", id="btn-ip-a")  # inline archetype (DESIGN.md §9): bare Button inside .inline-row
                        with Horizontal(classes="form-row"):
                            yield Static("VPN Resolution", classes="form-label")
                            yield Static("", id="vpn-resolve-preview", classes="form-field")
                        with Horizontal(classes="form-row"):
                            yield Static("Gateway Port", classes="form-label")
                            yield Input(id="f-port", classes="form-field")
                        with Horizontal(classes="form-row"):
                            yield Static("Health Timeout (s)", classes="form-label")
                            yield Input(id="f-gw-timeout", placeholder="optional (seconds)", classes="form-field")
                        with Horizontal(classes="form-row"):
                            yield Static("Builds to Keep", classes="form-label")
                            yield Input(id="f-retain", classes="form-field")

                   with Vertical(classes="settings-column"):
                    with Vertical(classes="panel"):
                        yield Static("Storage Paths", classes="panel-title")
                        with Horizontal(classes="form-row"):
                            yield Static("Models Directory", classes="form-label")
                            yield Input(id="f-models-dir", classes="form-field")
                        with Horizontal(classes="form-row"):
                            yield Static("State Directory", classes="form-label")
                            yield Input(id="f-state-dir", classes="form-field")
                        with Horizontal(classes="form-row"):
                            yield Static("Builds Directory", classes="form-label")
                            yield Input(id="f-prefix-root", classes="form-field")

                  yield Static("", id="profile-error", classes="error-text")
                  with Horizontal(classes="action-row-primary"):
                        yield Button("Save Profile", id="btn-save-profile", variant="primary", classes="thin-button")
                        yield Button("Deploy", id="btn-deploy-profile", classes="thin-button")
                  yield Static("", id="profile-status", classes="status-text")

            with TabPane("GPU Topology", id="settings-tab-gpus"):
                with VerticalScroll():
                    with Vertical(classes="panel"):
                        yield Static("Configured Accelerators", classes="panel-title")
                        # Driver Status used to be a text dump below this table; it's now the
                        # table's own last column (item 8b) — one row already is one GPU, so a
                        # separate text block repeated the same identifiers instead of adding
                        # information.
                        yield CockpitDataTable(id="gpu-table", zebra_stripes=True, classes="data-table")
                    # Reuses the first-run wizard's own add-GPU form (below) rather than a
                    # second implementation — see _save_gpu_form's host_profile-is-None branch.
                    with Vertical(id="gpu-add-form", classes="panel"):
                        yield Static("Add GPU", classes="panel-title")
                        yield Label("GPU ID")
                        yield Input(id="gpu-add-id", placeholder="e.g. gpu2")
                        yield Label("Vendor")
                        yield Select(_GPU_VENDOR_OPTIONS, id="gpu-add-vendor", allow_blank=False, value="nvidia")
                        yield Label("Backends (comma-separated: cuda, rocm, vulkan, sycl)")
                        yield Input(id="gpu-add-backends", placeholder="cuda")
                        yield Static("", id="gpu-add-error", classes="error-text")
                        with Horizontal(classes="action-row-primary"):
                            yield Button("Save GPU", id="btn-gpu-save", classes="thin-button", variant="primary")
                            yield Button("Cancel", id="btn-gpu-cancel", classes="thin-button")
                    with Horizontal(classes="action-row-primary"):
                        yield Button("Add GPU", id="btn-add-gpu", classes="thin-button")
                        yield Button("Check Drivers", id="btn-check-drivers", classes="thin-button")
                        yield Button("List PCIe Devices", id="btn-lspci", classes="thin-button")

            with TabPane("Connectors", id="settings-tab-connectors"):
                with VerticalScroll():
                    with Vertical(classes="panel"):
                        yield Static("Hugging Face", classes="panel-title")
                        with Horizontal(classes="form-row"):
                            yield Static("HF Token Env Var", classes="form-label")
                            yield Input(id="f-token-env", classes="form-field")
                    yield Static("", id="connectors-error", classes="error-text")
                    with Horizontal(classes="action-row-primary"):
                        yield Button("Save Connectors", id="btn-save-connectors", variant="primary", classes="thin-button")
                    yield Static("", id="connectors-status", classes="status-text")

            with TabPane("System Services", id="settings-tab-services"):
                with VerticalScroll():
                  with Horizontal(classes="columns-responsive settings-columns"):
                   with Vertical(classes="settings-column"):
                    with Vertical(classes="panel"):
                        yield Static("Inference & Supervision", classes="panel-title")
                        with Horizontal(classes="form-row"):
                            yield Static("Restart Policy", classes="form-label")
                            yield Select(_RESTART_POLICY_OPTIONS, id="f-restart-policy", allow_blank=False, value="on-failure", classes="form-field")
                        with Horizontal(classes="form-row"):
                            yield Static("Restart Delay (s)", classes="form-label")
                            yield Input(id="f-restart-sec", classes="form-field")
                        with Horizontal(classes="form-row"):
                            yield Static("Scheduled Restart", classes="form-label")
                            yield Switch(id="f-scheduled-restart-enabled")
                        with Horizontal(classes="form-row"):
                            yield Static("Restart Schedule", classes="form-label")
                            yield Input(id="f-scheduled-restart-calendar", placeholder="e.g. daily", classes="form-field")
                        with Horizontal(classes="form-row"):
                            yield Static("Update-Check Timer", classes="form-label")
                            yield Switch(id="f-update-check-enabled")
                        with Horizontal(classes="form-row"):
                            yield Static("Check Schedule", classes="form-label")
                            yield Input(id="f-update-check-calendar", placeholder="e.g. daily", classes="form-field")
                    yield Static("", id="service-error", classes="error-text")
                    with Horizontal(classes="action-row-primary"):
                        yield Button("Apply Service Settings", id="btn-apply-service", variant="primary", classes="thin-button")
                    yield Static("", id="service-status", classes="status-text")

                   # Wake-on-LAN and Tailscale were one "Host & Network Services" panel sharing
                   # one action row with the llama-swap settings above. They have nothing to do
                   # with each other, and worse: saving a WOL interface went through "Apply
                   # Service Settings", which runs swap.run() — a llama-swap reinstall into
                   # /usr/local/bin that needs root and fails for an unprivileged cockpit. Each
                   # concern now owns its own panel and its own buttons.
                   with Vertical(classes="settings-column"):
                    with Vertical(classes="panel"):
                        yield Static("Wake-on-LAN", classes="panel-title")
                        with Horizontal(classes="form-row"):
                            yield Static("Interface", classes="form-label")
                            # Populated from live NICs in _populate_wol_form. The operator had
                            # to know and retype an interface name exactly, with the answer one
                            # tab away behind the Host Profile tab's "ip a" button.
                            yield Select([], id="f-wol-interface", allow_blank=True, classes="form-field")
                        with Horizontal(classes="form-row"):
                            yield Static("MAC Address", classes="form-label")
                            yield Input(id="f-wol-mac", placeholder="e.g. 00:11:22:33:44:55", classes="form-field")
                        with Horizontal(classes="form-row"):
                            yield Static("Status", classes="form-label")
                            yield Static("not checked yet", id="wol-status", classes="form-field status-text")
                        yield Static("", id="wol-error", classes="error-text")
                        with Horizontal(classes="action-row-primary"):
                            # Save writes hosts/<hostname>.yaml and nothing else — declarative,
                            # so it works unprivileged. Applying it to the NIC (ethtool, a
                            # systemd unit, TLP/NetworkManager) is the separate button, and
                            # that one does need root.
                            yield Button("Save WOL Settings", id="btn-save-wol", variant="primary", classes="thin-button")
                            yield Button("Check / Enable WOL", id="btn-check-wol", variant="warning", classes="thin-button")
                        yield Static("", id="wol-save-status", classes="status-text")

                    with Vertical(classes="panel"):
                        yield Static("Tailscale", classes="panel-title")
                        with Horizontal(classes="form-row"):
                            yield Static("Status", classes="form-label")
                            yield Static("not checked yet", id="tailscale-status", classes="form-field status-text")
                        with Horizontal(classes="action-row-primary"):
                            yield Button("Check / Enable Tailscale", id="btn-check-tailscale", variant="warning", classes="thin-button")

    def on_mount(self) -> None:
        # prevent(): _populate_profile_form assigns #f-vpn-interface.value, which fires
        # Input.Changed, which re-runs `ip -4 addr` — a shell-out nobody asked for, at launch,
        # on a tab that isn't even visible.
        with self.prevent(Input.Changed):
            self._populate_profile_form()
        table = self.query_one("#gpu-table", CockpitDataTable)
        table.cursor_type = "row"
        table.add_column("GPU ID", width=16)
        table.add_column("Vendor", width=16)
        table.add_column("Backends", width=30)
        table.add_column("Driver Status", width=40)
        self.query_one("#gpu-add-form").display = False

        if self.host_profile is None:
            self._populate_wizard_gpu_table()
        else:
            # Form population is local and free; the three status reads shell out, so they wait
            # for ensure_first_view() (see on_refresh_requested, which is what it calls).
            self._render_gpu_list()
            with self.prevent(Input.Changed):
                self._populate_service_form()

    def on_refresh_requested(self) -> None:
        """Called by CockpitApp.action_refresh_all, and by ensure_first_view the first time
        this tab is shown. Every read here shells out, which is exactly why none of it happens
        at mount any more."""
        if self.host_profile is None:
            return
        self._update_vpn_preview(self.query_one("#f-vpn-interface", Input).value)
        self._refresh_wol_status()
        self._refresh_drivers_status()
        self._refresh_tailscale_status()

    # -- form population -----------------------------------------------------------

    def _populate_profile_form(self) -> None:
        hp = self.host_profile
        if hp is None:
            default_host = getattr(self.app_ref, "host_name", None) or socket.gethostname()
            self.query_one("#f-hostname", Input).value = default_host
            self.query_one("#f-port", Input).value = "8090"
            self.query_one("#f-retain", Input).value = "3"
            self.query_one("#f-token-env", Input).value = "HF_TOKEN"
            defaults = _default_paths()
            self.query_one("#f-models-dir", Input).value = defaults["models_dir"]
            self.query_one("#f-state-dir", Input).value = defaults["state_dir"]
            self.query_one("#f-prefix-root", Input).value = defaults["prefix_root"]
            return

        self.query_one("#f-hostname", Input).value = hp["hostname"]
        self.query_one("#f-vpn-interface", Input).value = hp["network"]["vpn"]["interface"]
        self.query_one("#f-port", Input).value = str(hp["network"]["gateway"]["port"])
        if self.query("#f-gw-timeout"):
            self.query_one("#f-gw-timeout", Input).value = str(hp["network"]["gateway"].get("health_check_timeout", ""))
        self.query_one("#f-models-dir", Input).value = hp["paths"]["models_dir"]
        self.query_one("#f-state-dir", Input).value = hp["paths"]["state_dir"]
        self.query_one("#f-prefix-root", Input).value = hp["paths"]["prefix_root"]
        self.query_one("#f-retain", Input).value = str(hp["retain_builds"])
        self.query_one("#f-token-env", Input).value = hp["hf"]["token_env"]

    # -- GPUs DataTable helpers -----------------------------------------------------

    def _populate_wizard_gpu_table(self) -> None:
        table = self.query_one("#gpu-table", CockpitDataTable)
        table.clear()
        for gpu in self._pending_gpus:
            table.add_row(
                Text(gpu["id"]), Text(gpu["vendor"]), Text(", ".join(gpu["backends"])), Text("—"), key=gpu["id"]
            )

    def _render_gpu_list(self) -> None:
        table = self.query_one("#gpu-table", CockpitDataTable)
        table.clear()
        if self.host_profile and "gpus" in self.host_profile:
            for gpu in self.host_profile["gpus"]:
                status = self._driver_status_by_gpu.get(gpu["id"], self._driver_status_default)
                table.add_row(
                    Text(gpu["id"]),
                    Text(gpu["vendor"]),
                    Text(", ".join(gpu.get("backends", []))),
                    Text(status),
                    key=gpu["id"],
                )

    def _show_add_gpu_form(self) -> None:
        form = self.query_one("#gpu-add-form")
        form.display = True
        existing_count = (
            len(self._pending_gpus) if self.host_profile is None else len(self.host_profile.get("gpus", []))
        )
        next_id = f"gpu{existing_count}"
        self.query_one("#gpu-add-id", Input).value = next_id
        self.query_one("#gpu-add-vendor", Select).value = "nvidia"
        self.query_one("#gpu-add-backends", Input).value = "cuda"
        self.query_one("#gpu-add-error", Static).update("")
        self.query_one("#gpu-add-id", Input).focus()

    def _hide_add_gpu_form(self) -> None:
        self.query_one("#gpu-add-form").display = False
        self.query_one("#gpu-add-error", Static).update("")

    def _validate_gpu_form(self, existing_ids: set[str]) -> tuple[dict[str, Any] | None, str | None]:
        gpu_id = self.query_one("#gpu-add-id", Input).value.strip()
        vendor = self.query_one("#gpu-add-vendor", Select).value
        backends_raw = self.query_one("#gpu-add-backends", Input).value.strip()

        if not gpu_id:
            return None, "GPU ID is required"
        if gpu_id in existing_ids:
            return None, f"GPU ID '{gpu_id}' already exists"
        if vendor is Select.BLANK or not vendor:
            return None, "Vendor is required"
        backends = [b.strip() for b in backends_raw.split(",") if b.strip()]
        if not backends:
            return None, "At least one backend is required (e.g. cuda)"
        for b in backends:
            if b not in ("cuda", "rocm", "vulkan", "sycl"):
                return None, f"Unknown backend '{b}' (must be cuda, rocm, vulkan, or sycl)"
        return {"id": gpu_id, "vendor": str(vendor), "backends": backends}, None

    def _save_gpu_form(self) -> None:
        # First-run wizard (host_profile is None): held in memory only, folded into the
        # candidate profile at Step 3. Normal mode: a GPU appended to an already-deployed host
        # is a persisted, mutating write (DESIGN.md §5 declarative-write idiom) — routed to
        # _confirm_and_add_gpu, which is the only branch that differs; the form itself (and its
        # validation) is the same one the wizard already had.
        if self.host_profile is None:
            existing_ids = {g["id"] for g in self._pending_gpus}
            new_gpu, err = self._validate_gpu_form(existing_ids)
            if err:
                self.query_one("#gpu-add-error", Static).update(err)
                return
            self._pending_gpus.append(new_gpu)
            self._populate_wizard_gpu_table()
            self._hide_add_gpu_form()
            self.query_one("#step-hardware-error", Static).update("")
        else:
            existing_ids = {g["id"] for g in self.host_profile.get("gpus", [])}
            new_gpu, err = self._validate_gpu_form(existing_ids)
            if err:
                self.query_one("#gpu-add-error", Static).update(err)
                return
            self._confirm_and_add_gpu(new_gpu)

    @work
    async def _confirm_and_add_gpu(self, new_gpu: dict[str, Any]) -> None:
        candidate = copy.deepcopy(self.host_profile)
        candidate["gpus"] = list(candidate.get("gpus", [])) + [new_gpu]
        try:
            schema.validate_host_profile_dict(candidate)
        except schema.ValidationError as e:
            self.query_one("#gpu-add-error", Static).update(f"validation failed: {e}")
            return

        message = f"Add GPU {new_gpu['id']!r} to hosts/{candidate['hostname']}.yaml?"
        confirmed = await self.app.push_screen_wait(ConfirmModal(message, confirm_label="Add", danger=True))
        if not confirmed:
            return

        content = yaml.safe_dump(candidate, sort_keys=False)
        target_path = self._host_profile_path()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding="utf-8")

        self.host_profile = candidate
        self._render_gpu_list()
        self._hide_add_gpu_form()
        self._set_profile_status(f"GPU {new_gpu['id']!r} added — hosts/{candidate['hostname']}.yaml written")
        self.notify(f"GPU {new_gpu['id']!r} added")

    def _remove_selected_gpu(self) -> None:
        if len(self._pending_gpus) <= 1:
            self.query_one("#step-hardware-error", Static).update("At least one GPU is required")
            return
        table = self.query_one("#gpu-table", CockpitDataTable)
        selected_id = None
        if table.row_count > 0 and table.cursor_row is not None and 0 <= table.cursor_row < table.row_count:
            row_data = table.get_row_at(table.cursor_row)
            selected_id = str(row_data[0])
        elif len(self._pending_gpus) > 0:
            selected_id = self._pending_gpus[-1]["id"]

        if selected_id:
            self._pending_gpus = [g for g in self._pending_gpus if g["id"] != selected_id]
            self._populate_wizard_gpu_table()
            self.query_one("#step-hardware-error", Static).update("")
        else:
            self.query_one("#step-hardware-error", Static).update("Select a GPU row to remove")

    # -- wizard step transitions & validation --------------------------------------

    def _validate_and_go_to_hardware(self) -> None:
        hostname = self.query_one("#f-hostname", Input).value.strip()
        if not hostname:
            self.query_one("#step-identity-error", Static).update("Hostname is required")
            return
        retain_raw = self.query_one("#f-retain", Input).value.strip()
        try:
            retain = int(retain_raw) if retain_raw else 3
            if retain < 1:
                raise ValueError
        except ValueError:
            self.query_one("#step-identity-error", Static).update("Builds to keep must be an integer >= 1")
            return
        models_dir = self.query_one("#f-models-dir", Input).value.strip()
        state_dir = self.query_one("#f-state-dir", Input).value.strip()
        prefix_root = self.query_one("#f-prefix-root", Input).value.strip()
        if not all([models_dir, state_dir, prefix_root]):
            self.query_one("#step-identity-error", Static).update("Model files, state, and build directories are all required")
            return

        self.query_one("#step-identity-error", Static).update("")
        self.query_one("#wizard-switcher", ContentSwitcher).current = "step-hardware"

    def _go_to_identity(self) -> None:
        self.query_one("#wizard-switcher", ContentSwitcher).current = "step-identity"

    def _validate_and_go_to_review(self) -> None:
        vpn_interface = self.query_one("#f-vpn-interface", Input).value.strip()
        if not vpn_interface:
            self.query_one("#step-hardware-error", Static).update("VPN interface is required")
            return
        port_raw = self.query_one("#f-port", Input).value.strip()
        try:
            port = int(port_raw)
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            self.query_one("#step-hardware-error", Static).update("Gateway port must be an integer between 1 and 65535")
            return

        if not self._pending_gpus:
            self.query_one("#step-hardware-error", Static).update("At least one GPU is required")
            return

        candidate, err = self._build_wizard_candidate()
        if err:
            self.query_one("#step-hardware-error", Static).update(err)
            return
        try:
            schema.validate_host_profile_dict(candidate)
        except schema.ValidationError as e:
            self.query_one("#step-hardware-error", Static).update(f"Validation error: {e}")
            return

        self.query_one("#step-hardware-error", Static).update("")
        yaml_content = yaml.safe_dump(candidate, sort_keys=False)
        self.query_one("#review-yaml", TextArea).text = yaml_content
        self.query_one("#wizard-switcher", ContentSwitcher).current = "step-review"

    def _go_to_hardware(self) -> None:
        self.query_one("#wizard-switcher", ContentSwitcher).current = "step-hardware"

    def _build_wizard_candidate(self) -> tuple[dict | None, str | None]:
        hostname = self.query_one("#f-hostname", Input).value.strip()
        if not hostname:
            return None, "Hostname is required"

        retain_raw = self.query_one("#f-retain", Input).value.strip()
        try:
            retain_builds = int(retain_raw) if retain_raw else 3
            if retain_builds < 1:
                return None, "Builds to keep must be an integer >= 1"
        except ValueError:
            return None, "Builds to keep must be an integer >= 1"

        models_dir = self.query_one("#f-models-dir", Input).value.strip()
        state_dir = self.query_one("#f-state-dir", Input).value.strip()
        prefix_root = self.query_one("#f-prefix-root", Input).value.strip()
        if not all([models_dir, state_dir, prefix_root]):
            return None, "Model files, state, and build directories are all required"

        vpn_interface = self.query_one("#f-vpn-interface", Input).value.strip()
        port_raw = self.query_one("#f-port", Input).value.strip()
        if not vpn_interface:
            return None, "VPN interface is required"
        if not port_raw:
            return None, "Gateway port is required"
        try:
            port = int(port_raw)
            if not (1 <= port <= 65535):
                return None, "Gateway port must be an integer between 1 and 65535"
        except ValueError:
            return None, "Gateway port must be an integer between 1 and 65535"

        token_env = self.query_one("#f-token-env", Input).value.strip() or "HF_TOKEN"

        if not self._pending_gpus:
            return None, "At least one GPU is required"

        candidate: dict[str, Any] = {
            "hostname": hostname,
            "network": {
                "vpn": {"interface": vpn_interface},
                "wol": dict(_WOL_PLACEHOLDER),
                "gateway": {"port": port},
            },
            "gpus": copy.deepcopy(self._pending_gpus),
            "paths": {
                "models_dir": models_dir,
                "state_dir": state_dir,
                "prefix_root": prefix_root,
            },
            "retain_builds": retain_builds,
            "hf": {"token_env": token_env},
        }
        return candidate, None

    def _dummy_deploy(self) -> None:
        candidate, err = self._build_wizard_candidate()
        if err:
            self.query_one("#step-review-error", Static).update(err)
            return
        try:
            schema.validate_host_profile_dict(candidate)
        except schema.ValidationError as e:
            self.query_one("#step-review-error", Static).update(f"validation failed: {e}")
            return
        self.query_one("#step-review-error", Static).update("")
        yaml_content = yaml.safe_dump(candidate, sort_keys=False)
        self.app.push_screen(InfoModal("Generated host profile preview", yaml_content))

    @work
    async def _real_deploy(self) -> None:
        candidate, err = self._build_wizard_candidate()
        if err:
            self.query_one("#step-review-error", Static).update(err)
            return
        try:
            schema.validate_host_profile_dict(candidate)
        except schema.ValidationError as e:
            self.query_one("#step-review-error", Static).update(f"validation failed: {e}")
            return
        self.query_one("#step-review-error", Static).update("")

        hostname = candidate["hostname"]
        message = f"Deploy host profile at hosts/{hostname}.yaml?"
        confirmed = await self.app.push_screen_wait(ConfirmModal(message, confirm_label="Deploy", danger=True))
        if not confirmed:
            return

        content = yaml.safe_dump(candidate, sort_keys=False)
        target_path = self.repo_root / "hosts" / f"{hostname}.yaml"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding="utf-8")

        self.app.exit(
            message=f"Host profile created at hosts/{hostname}.yaml — "
            "restart the cockpit (bin/cockpit) to continue."
        )

    # -- normal mode form & saving -------------------------------------------------

    def _populate_service_form(self) -> None:
        service = self.host_profile.get("service", {})
        update_check_cfg = self.host_profile.get("update_check", {})
        scheduled = service.get("scheduled_restart", {})
        self.query_one("#f-restart-policy", Select).value = service.get("restart_policy", "on-failure")
        self.query_one("#f-restart-sec", Input).value = str(service.get("restart_sec", 5))
        self.query_one("#f-scheduled-restart-enabled", Switch).value = scheduled.get("enabled", False)
        self.query_one("#f-scheduled-restart-calendar", Input).value = scheduled.get("on_calendar", "daily")
        self.query_one("#f-update-check-enabled", Switch).value = update_check_cfg.get("enabled", False)
        self.query_one("#f-update-check-calendar", Input).value = update_check_cfg.get("on_calendar", "daily")
        self._populate_wol_form()

    def _populate_wol_form(self) -> None:
        """Offer the host's real NICs instead of asking the operator to retype a name exactly.

        The configured interface is added to the options even when it isn't among them — a
        profile carrying hosts/example.yaml's "TODO" placeholder, or a NIC that has since been
        renamed or removed, must still round-trip through this form rather than silently
        resolving to a different interface on save.
        """
        if not self.query("#f-wol-interface"):
            return
        wol_cfg = (self.host_profile or {}).get("network", {}).get("wol", {})
        configured = wol_cfg.get("interface", "")
        self._iface_macs = {i["name"]: i["mac"] for i in wol.list_interfaces()}
        names = list(self._iface_macs)
        if configured and configured not in self._iface_macs:
            names.append(configured)
        select = self.query_one("#f-wol-interface", Select)
        # prevent(): set_options and value both fire Select.Changed, and the handler below
        # overwrites the MAC field from the chosen interface. Without this, populating the
        # form would clobber a deliberately-overridden MAC with the NIC's real one.
        with self.prevent(Select.Changed):
            select.set_options([(n, n) for n in names])
            select.value = configured if configured in names else Select.BLANK
            self.query_one("#f-wol-mac", Input).value = wol_cfg.get("mac", "")

    def on_select_changed(self, event: Select.Changed) -> None:
        """Auto-fill the MAC from the chosen NIC. It stays an editable Input, not a read-only
        label: a profile can legitimately carry a MAC this host doesn't currently report (a
        replaced NIC, a bonded pair), and wol.run() treats the profile as the source of truth."""
        if event.select.id != "f-wol-interface":
            return
        mac = self._iface_macs.get(str(event.value), "")
        if mac:
            self.query_one("#f-wol-mac", Input).value = mac

    def _update_vpn_preview(self, interface: str) -> None:
        interface = interface.strip()
        preview = self.query_one("#vpn-resolve-preview", Static)
        if not interface:
            preview.update("")
            return
        try:
            ip = swap.resolve_vpn_ip(interface)
        except Exception:
            preview.update("→ could not resolve (is `ip` available on this host?)")
            return
        preview.update(f"→ resolves to: {ip}" if ip else "→ not found / no address yet")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "f-vpn-interface":
            self._update_vpn_preview(event.value)

    def _show_pcie_devices(self) -> None:
        self.app.push_screen(InfoModal("PCIe devices (lspci)", run_shell_capture(_LSPCI_CMD)))

    def _show_ip_a(self) -> None:
        self.app.push_screen(InfoModal("Network interfaces (ip a)", run_shell_capture("ip a")))

    def _set_profile_error(self, text: str) -> None:
        # Saving from the Connectors tab (item 7) mutates the same host-profile candidate the
        # Host Profile tab does, via the same _confirm_and_save_profile — so feedback shows on
        # whichever tab's own status line the operator is actually looking at.
        for widget in self.query("#profile-error, #connectors-error"):
            widget.update(text)

    def _set_profile_status(self, text: str) -> None:
        for widget in self.query("#profile-status, #connectors-status"):
            widget.update(text)

    def _set_service_error(self, text: str) -> None:
        self.query_one("#service-error", Static).update(text)

    def _set_service_status(self, text: str) -> None:
        self.query_one("#service-status", Static).update(text)

    def _read_network_fields(self) -> tuple[dict | None, str | None]:
        vpn_interface = self.query_one("#f-vpn-interface", Input).value.strip()
        gw_port_raw = self.query_one("#f-port", Input).value.strip()
        gw_timeout_raw = ""
        if self.query("#f-gw-timeout"):
            gw_timeout_raw = self.query_one("#f-gw-timeout", Input).value.strip()
        if not vpn_interface or not gw_port_raw:
            return None, "network.vpn.interface and gateway.port are required"
        try:
            gw_port = int(gw_port_raw)
            if not (1 <= gw_port <= 65535):
                raise ValueError
        except ValueError:
            return None, "gateway.port must be an integer between 1 and 65535"
        wol = dict(_WOL_PLACEHOLDER) if self.host_profile is None else dict(self.host_profile.get("network", {}).get("wol", _WOL_PLACEHOLDER))
        if self.query("#f-wol-interface"):
            wol_iface = self._selected_wol_interface()
            if wol_iface:
                wol["interface"] = wol_iface
        if self.query("#f-wol-mac"):
            wol_mac = self.query_one("#f-wol-mac", Input).value.strip()
            if wol_mac:
                wol["mac"] = wol_mac
        network: dict[str, Any] = {
            "vpn": {"interface": vpn_interface},
            "gateway": {"port": gw_port},
            "wol": wol,
        }
        if gw_timeout_raw:
            try:
                network["gateway"]["health_check_timeout"] = int(gw_timeout_raw)
            except ValueError:
                return None, "gateway.health_check_timeout must be an integer"
        return network, None

    def _read_paths_fields(self) -> tuple[dict | None, str | None]:
        models_dir = self.query_one("#f-models-dir", Input).value.strip()
        state_dir = self.query_one("#f-state-dir", Input).value.strip()
        prefix_root = self.query_one("#f-prefix-root", Input).value.strip()
        if not all([models_dir, state_dir, prefix_root]):
            return None, "model/state/build paths are all required"
        return {"models_dir": models_dir, "state_dir": state_dir, "prefix_root": prefix_root}, None

    def _build_profile_candidate(self) -> tuple[dict | None, str | None]:
        hostname = self.query_one("#f-hostname", Input).value.strip()
        if not hostname:
            return None, "hostname is required"
        network, err = self._read_network_fields()
        if err:
            return None, err
        paths, err = self._read_paths_fields()
        if err:
            return None, err

        retain_raw = self.query_one("#f-retain", Input).value.strip()
        try:
            retain_builds = int(retain_raw) if retain_raw else 3
        except ValueError:
            return None, "retain_builds must be an integer"
        if retain_builds < 1:
            return None, "retain_builds must be >= 1"

        token_env = self.query_one("#f-token-env", Input).value.strip() or "HF_TOKEN"

        candidate: dict[str, Any] = {
            "hostname": hostname,
            "network": network,
            "paths": paths,
            "retain_builds": retain_builds,
            "hf": {"token_env": token_env},
            "gpus": self.host_profile["gpus"],
        }

        if "service" in self.host_profile:
            candidate["service"] = self.host_profile["service"]
        if "update_check" in self.host_profile:
            candidate["update_check"] = self.host_profile["update_check"]

        return candidate, None

    def _build_service_fields(self) -> tuple[dict | None, dict | None, str | None]:
        restart_policy = self.query_one("#f-restart-policy", Select).value
        restart_sec_raw = self.query_one("#f-restart-sec", Input).value.strip()
        try:
            restart_sec = int(restart_sec_raw) if restart_sec_raw else 5
        except ValueError:
            return None, None, "restart_sec must be an integer"
        if restart_sec < 0:
            return None, None, "restart_sec must be >= 0"

        scheduled_enabled = self.query_one("#f-scheduled-restart-enabled", Switch).value
        scheduled_calendar = self.query_one("#f-scheduled-restart-calendar", Input).value.strip() or "daily"
        update_enabled = self.query_one("#f-update-check-enabled", Switch).value
        update_calendar = self.query_one("#f-update-check-calendar", Input).value.strip() or "daily"

        service = {
            "restart_policy": restart_policy,
            "restart_sec": restart_sec,
            "scheduled_restart": {"enabled": scheduled_enabled, "on_calendar": scheduled_calendar},
        }
        update_check_cfg = {"enabled": update_enabled, "on_calendar": update_calendar}
        return service, update_check_cfg, None

    def _host_profile_path(self) -> Path:
        hostname = self.host_profile["hostname"] if self.host_profile else self.app_ref.host_name
        return self.repo_root / "hosts" / f"{hostname}.yaml"

    @work
    async def _confirm_and_save_profile(self) -> None:
        candidate, err = self._build_profile_candidate()
        if err:
            self._set_profile_error(err)
            return
        try:
            schema.validate_host_profile_dict(candidate)
        except schema.ValidationError as e:
            self._set_profile_error(f"validation failed: {e}")
            return
        self._set_profile_error("")

        hostname = candidate["hostname"]
        message = f"Save host profile at hosts/{hostname}.yaml?"
        confirmed = await self.app.push_screen_wait(ConfirmModal(message, confirm_label="Save", danger=True))
        if not confirmed:
            return

        content = yaml.safe_dump(candidate, sort_keys=False)
        target_path = self._host_profile_path()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding="utf-8")

        self.host_profile = candidate
        self._render_gpu_list()
        self._set_profile_status("host profile saved — restart the cockpit for other tabs to see the change")

    @work
    async def _confirm_and_deploy_profile(self) -> None:
        candidate, err = self._build_profile_candidate()
        if err:
            self._set_profile_error(err)
            return
        try:
            schema.validate_host_profile_dict(candidate)
        except schema.ValidationError as e:
            self._set_profile_error(f"validation failed: {e}")
            return
        self._set_profile_error("")

        hostname = candidate["hostname"]
        message = (
            f"Deploy host profile at hosts/{hostname}.yaml and reconfigure services?\n"
            "(writes host configuration and restarts background services)"
        )
        # Same _apply_service_in_background as "Apply Service Settings" below, so the same
        # root requirement.
        confirmed = await self.confirm(
            message, confirm_label="Deploy", mutates_system=True, requires_root=True
        )
        if not confirmed:
            return

        content = yaml.safe_dump(candidate, sort_keys=False)
        target_path = self._host_profile_path()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding="utf-8")

        self.host_profile = candidate
        self._render_gpu_list()
        self._set_profile_status("deploying host profile and reconfiguring services...")
        self._apply_service_in_background(candidate)

    # -- WOL status + check/enable ---------------------------------------------------

    @work(thread=True)
    def _refresh_wol_status(self) -> None:
        # No toast on completion (DESIGN.md §6): this is a passive status read that renders into
        # the inline #wol-status label and is triggered by mount/refresh, not by an operator
        # action — every failure mode is already reported in that label.
        # wol.status() shells out to ip/ethtool/systemctl (see provision/steps/wol.py) — off
        # the main thread like every other status/apply call in this file, so a slow or hung
        # command can't freeze the whole TUI.
        if self.host_profile is None:
            return
        try:
            status = wol.status(self.host_profile)
            lines = [
                f"MAC matches host profile: {'yes' if status['mac_matches'] else 'no'} "
                f"(actual: {status['actual_mac'] or 'unknown'})",
                # sysfs first: it needs no ethtool, so it still answers on a host where the
                # package is absent — which used to make this read as a failure on a machine
                # with working WOL.
                f"wake armed: {'yes' if status.get('armed') else 'no'}"
                f" (power/wakeup: {status.get('wakeup_sysfs') or 'n/a'},"
                f" ethtool: {status['wake_flags'] or 'n/a'})",
                f"persistence unit: {status.get('unit_name') or 'none found'}",
            ]
            text = "\n".join(lines)
        except Exception as e:
            text = f"not available: {e}"
        self.app.call_from_thread(self._apply_wol_status_text, text)

    def _apply_wol_status_text(self, text: str) -> None:
        if not self.is_mounted:
            return
        self.query_one("#wol-status", Static).update(text)

    def _selected_wol_interface(self) -> str:
        value = self.query_one("#f-wol-interface", Select).value
        return "" if value is Select.BLANK else str(value).strip()

    @work
    async def _confirm_and_save_wol(self) -> None:
        """Write network.wol to hosts/<hostname>.yaml and stop there.

        Declarative only — no ethtool, no systemd, no llama-swap — so it works as an
        unprivileged user. Applying the saved config to the NIC is "Check / Enable WOL",
        which does need root.
        """
        iface = self._selected_wol_interface()
        mac = self.query_one("#f-wol-mac", Input).value.strip()
        if not iface or not mac:
            self.query_one("#wol-error", Static).update("interface and MAC address are both required")
            return

        candidate = copy.deepcopy(self.host_profile)
        candidate.setdefault("network", {})["wol"] = {"interface": iface, "mac": mac}
        try:
            schema.validate_host_profile_dict(candidate)
        except schema.ValidationError as e:
            self.query_one("#wol-error", Static).update(f"validation failed: {e}")
            return
        self.query_one("#wol-error", Static).update("")

        actual = self._iface_macs.get(iface)
        drift = ""
        if actual and actual.lower() != mac.lower():
            # wol.run() exits on this mismatch rather than proceeding, so say it now instead
            # of letting the operator discover it from "Check / Enable WOL" later.
            drift = f"\nNote: {iface} currently reports {actual}, not {mac}."
        confirmed = await self.confirm(
            f"Save Wake-on-LAN settings ({iface} / {mac}) to hosts/{self.host_profile['hostname']}.yaml?"
            f"{drift}\nThis only writes the file — use 'Check / Enable WOL' to apply it to the NIC.",
            confirm_label="Save",
        )
        if not confirmed:
            return

        target_path = self._host_profile_path()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(yaml.safe_dump(candidate, sort_keys=False), encoding="utf-8")
        self.host_profile = candidate
        self.query_one("#wol-save-status", Static).update(
            f"saved — {target_path.name} now records {iface} / {mac}"
        )
        self.notify("Wake-on-LAN settings saved")
        self._refresh_wol_status()

    @work
    async def _confirm_and_check_wol(self) -> None:
        message = (
            "Check / enable Wake-on-LAN? (may change NIC wake flags, "
            "TLP/NetworkManager config, and enable a systemd persistence unit)"
        )
        # DESIGN.md §5 tier 1+2: NIC wake flags, TLP/NetworkManager config, a systemd unit.
        confirmed = await self.confirm(
            message, confirm_label="Check / Enable", mutates_system=True, requires_root=True
        )
        if not confirmed:
            return
        self.query_one("#wol-status", Static).update("checking...")
        self._run_wol()

    @work(thread=True)
    def _run_wol(self) -> None:
        try:
            wol.run(self.host_profile, self.manifest, self.models, self.privileged_runner, self.repo_root)
        except SystemExit as e:
            self.app.call_from_thread(self.app.notify, f"WOL check failed: {e.code}", severity="error")
        except Exception as e:
            self.app.call_from_thread(self.app.notify, f"WOL check failed: {e}", severity="error")
        else:
            self.app.call_from_thread(self.app.notify, "WOL enabled/verified")
        finally:
            self.app.call_from_thread(self._refresh_wol_status)

    # -- Tailscale status + check/enable ---------------------------------------------

    @work(thread=True)
    def _refresh_tailscale_status(self) -> None:
        # Passive status read, no toast — see _refresh_wol_status (DESIGN.md §6).
        if self.host_profile is None:
            return
        try:
            status = tailscale.status(self.host_profile)
            if not status["installed"]:
                text = "not installed"
            elif status["logged_in"]:
                text = f"logged in — {status['ip'] or 'no IP yet'}"
            else:
                text = f"not logged in (state: {status['backend_state'] or status.get('error') or 'unknown'})"
        except Exception as e:
            text = f"not available: {e}"
        self.app.call_from_thread(self._apply_tailscale_status_text, text)

    def _apply_tailscale_status_text(self, text: str) -> None:
        if not self.is_mounted:
            return
        self.query_one("#tailscale-status", Static).update(text)

    @work
    async def _confirm_and_check_tailscale(self) -> None:
        message = (
            "Check / enable Tailscale? Installs the client if missing. If not already logged "
            "in, this suspends the TUI and hands you an interactive `tailscale up` login "
            "prompt in the real terminal — complete it there, then you'll return here."
        )
        # DESIGN.md §5 tier 1: installs a system package if missing and joins the host to a
        # tailnet — host-level state, not a declarative preview file.
        confirmed = await self.confirm(
            message, confirm_label="Check / Enable", mutates_system=True, requires_root=True
        )
        if not confirmed:
            return
        self.query_one("#tailscale-status", Static).update("checking...")
        # Deliberately NOT @work(thread=True): `tailscale up`'s interactive login can only be
        # mediated by actually suspending the TUI and handing over the real terminal (see
        # containers.py's Exec shell for the same pattern) — that has to happen on the main
        # thread, so this runs synchronously rather than threading the whole flow only to hop
        # back for the suspend step.
        self._run_tailscale_check()

    def _run_tailscale_check(self) -> None:
        try:
            tailscale.ensure_installed(self.privileged_runner)
        except Exception as e:
            self.notify(f"tailscale install failed: {e}", severity="error")
            self._refresh_tailscale_status()
            return
        status = tailscale.status(self.host_profile)
        if status["logged_in"]:
            self.notify(f"tailscale already logged in — {status['ip'] or 'no IP yet'}")
        else:
            with self.app.suspend():
                subprocess.run(["tailscale", "up"])
        self._refresh_tailscale_status()

    # -- driver-drift status + check --------------------------------------------------

    def _refresh_drivers_status(self) -> None:
        """Was a standalone text dump below the GPU table; now feeds the table's own Driver
        Status column (item 8b) — self._driver_status_by_gpu keyed by gpu_id, applied by
        re-rendering the table rather than updating a separate widget."""
        if self.host_profile is None:
            return
        try:
            data = drivers.read_lockfile_status(self.host_profile, self.repo_root)
        except Exception as e:
            self._driver_status_by_gpu = {}
            self._driver_status_default = f"failed to read lockfile: {e}"
            self._render_gpu_list()
            return
        if data is None:
            self._driver_status_by_gpu = {}
            self._driver_status_default = "not checked yet"
            self._render_gpu_list()
            return
        self._driver_status_default = f"not in lockfile (generated {data.get('generated_at', 'unknown')})"
        by_gpu: dict[str, str] = {}
        for gpu_id, facts in (data.get("gpus") or {}).items():
            facts = dict(facts)
            facts.pop("vendor", "")
            by_gpu[gpu_id] = ", ".join(f"{k}={v}" for k, v in facts.items()) or "ok"
        self._driver_status_by_gpu = by_gpu
        self._render_gpu_list()

    @work
    async def _confirm_and_check_drivers(self) -> None:
        message = (
            "Check GPU drivers for drift? (captures driver/runtime versions; "
            "exits loudly on detected drift, never auto-corrects)"
        )
        confirmed = await self.app.push_screen_wait(ConfirmModal(message, confirm_label="Check"))
        if not confirmed:
            return
        self._driver_status_default = "checking..."
        self._render_gpu_list()
        self._run_drivers_check()

    @work(thread=True)
    def _run_drivers_check(self) -> None:
        try:
            drivers.run(self.host_profile, self.manifest, self.models, self.runner, self.repo_root)
        except SystemExit as e:
            self.app.call_from_thread(self.app.notify, str(e), severity="error")
        except Exception as e:
            self.app.call_from_thread(self.app.notify, f"driver check failed: {e}", severity="error")
        else:
            self.app.call_from_thread(self.app.notify, "driver check: no drift detected")
        finally:
            self.app.call_from_thread(self._refresh_drivers_status)

    # -- service + update-check settings ------------------------------------------------

    @work
    async def _confirm_and_apply_service(self) -> None:
        service, update_check_cfg, err = self._build_service_fields()
        if err:
            self._set_service_error(err)
            return

        candidate = copy.deepcopy(self.host_profile)
        candidate["service"] = service
        candidate["update_check"] = update_check_cfg
        # WOL is deliberately NOT read here any more. It used to ride along on this button,
        # which means changing an interface name ran swap.run() — a llama-swap reinstall into
        # /usr/local/bin — and failed outright for an unprivileged cockpit. WOL has its own
        # Save button, which only writes the profile.
        try:
            schema.validate_host_profile_dict(candidate)
        except schema.ValidationError as e:
            self._set_service_error(f"validation failed: {e}")
            return
        self._set_service_error("")

        message = (
            "Apply service/update-check settings? (writes hosts/<hostname>.yaml and may "
            "restart llama-swap and (re)install/remove systemd timers)"
        )
        # DESIGN.md §5 tier 2: restarts llama-swap and (re)installs/removes systemd timers.
        confirmed = await self.confirm(
            message, confirm_label="Apply", mutates_system=True, requires_root=True
        )
        if not confirmed:
            return

        content = yaml.safe_dump(candidate, sort_keys=False)
        target_path = self._host_profile_path()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding="utf-8")
        self.host_profile = candidate
        self._set_service_status("applying...")
        self._apply_service_in_background(candidate)

    @work(thread=True)
    def _apply_service_in_background(self, profile: dict) -> None:
        # DESIGN.md §6: #service-status is on a Settings sub-tab the operator may well have
        # navigated away from while systemd is restarting, so toast every outcome too.
        try:
            runner = self.privileged_runner
            swap.run(profile, self.manifest, self.models, runner, self.repo_root)
            swap.sync_scheduled_restart(profile, self.repo_root, runner)
            swap.sync_update_check_timer(profile, self.repo_root, runner)
        except SystemExit as e:
            msg = f"apply failed: {e.code}{_root_hint()}"
        except Exception as e:
            msg = f"apply failed: {e}{_root_hint()}"
        else:
            msg = "service settings applied"
            self.app.call_from_thread(self._set_service_status, msg)
            self.app.call_from_thread(self._set_profile_status, "host profile deployed")
            self.app.call_from_thread(self.app.notify, msg)
            return
        self.app.call_from_thread(self._set_service_status, msg)
        self.app.call_from_thread(self._set_profile_error, msg)
        self.app.call_from_thread(self.app.notify, msg, severity="error")

    # -- button dispatch ------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        # Wizard buttons
        if bid == "btn-next-hardware":
            self._validate_and_go_to_hardware()
        elif bid == "btn-back-identity":
            self._go_to_identity()
        elif bid == "btn-next-review":
            self._validate_and_go_to_review()
        elif bid == "btn-back-hardware":
            self._go_to_hardware()
        elif bid == "btn-dummy-deploy":
            self._dummy_deploy()
        elif bid == "btn-real-deploy":
            self._real_deploy()
        elif bid == "btn-add-gpu":
            self._show_add_gpu_form()
        elif bid == "btn-remove-gpu":
            self._remove_selected_gpu()
        elif bid == "btn-gpu-save":
            self._save_gpu_form()
        elif bid == "btn-gpu-cancel":
            self._hide_add_gpu_form()
        # Shared & normal buttons
        elif bid == "btn-lspci":
            self._show_pcie_devices()
        elif bid == "btn-ip-a":
            self._show_ip_a()
        elif bid in ("btn-save-profile", "btn-save-connectors"):
            self._confirm_and_save_profile()
        elif bid == "btn-deploy-profile":
            self._confirm_and_deploy_profile()
        elif bid == "btn-save-wol":
            self._confirm_and_save_wol()
        elif bid in ("btn-check-wol", "btn-wol-check"):
            self._confirm_and_check_wol()
        elif bid in ("btn-check-tailscale", "btn-tailscale-check"):
            self._confirm_and_check_tailscale()
        elif bid in ("btn-check-drivers", "btn-drivers-check"):
            self._confirm_and_check_drivers()
        elif bid == "btn-apply-service":
            self._confirm_and_apply_service()
