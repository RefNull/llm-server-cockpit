"""Cockpit "Settings" tab.

First-run (host_profile is None): the ONLY tab shown (see app.py, which titles it
"First setup") — offers a form to create hosts/<hostname>.yaml from scratch. A successful
real write exits the app and asks for a restart (bin/cockpit) rather than trying to
dynamically grow the other three tabs into a running TabbedContent — simpler and more
robust for a one-time step. WOL is not part of this form at all — first-run writes fixed
placeholder values for network.wol (matching hosts/example.yaml's own convention) and WOL
gets its own section, editable only in normal mode, after the host actually exists.

Normal (host_profile is a real dict): the same structural fields (network/paths/
retain_builds/hf) stay editable via "Save host profile", again with a restart notice —
the other tab screens hold their own host_profile dict captured at construction time and
won't see an in-place edit. GPUs are read-only display only here; topology changes stay a
direct hosts/<hostname>.yaml edit (they cross-validate against models.yaml bindings the
way deploy.py already handles — out of scope for this tab).

Three operational sections apply live, no restart needed: WOL status/enable, GPU
driver-drift status/check, and service + update-check settings (which also nudges
provision.steps.swap to (re)install the systemd unit/timers).

Layout: two independently-scrolling columns, each ONE bordered panel (left = general
settings, right = GPUs) rather than one box per field group — nested boxes don't scroll
their own content, so many small boxes just wasted space without helping the cut-off/
no-scroll problem. Sections within a column are separated by a plain heading, no border.
"""
from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

import yaml
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Button, Input, Label, Select, Static, Switch

from cockpit.widgets import ConfirmModal, InfoModal, run_shell_capture
from provision import schema
from provision.common import Runner
from provision.steps import drivers, swap, wol

log = logging.getLogger("provision")

_RESTART_POLICY_OPTIONS = [("on-failure", "on-failure"), ("always", "always"), ("no", "no")]
_GPU_VENDOR_OPTIONS = [("nvidia", "nvidia"), ("amd", "amd"), ("intel", "intel")]

# hosts/example.yaml's placeholder convention for WOL fields that are no longer part of the
# structural form (moved to their own section, normal mode only) — a string field gets
# "TODO", the regex-constrained MAC field gets a valid-but-obviously-fake value since "TODO"
# would fail its pattern.
_WOL_PLACEHOLDER = {"interface": "TODO", "mac": "00:00:00:00:00:00"}

# The exact one-liner requested for the "List PCIe devices" popup.
_LSPCI_CMD = (
    'i=0; lspci | grep -E "VGA compatible controller|3D controller" | '
    "while read -r line; do name=$(echo \"$line\" | cut -d':' -f3- | "
    "sed 's/^ //; s/ (rev .*//'); echo \"gpu$i: $name\"; i=$((i+1)); done"
)


def _default_paths() -> dict[str, str]:
    """A single root under the invoking user's home, with subfolders — proposed as real
    prefilled values (not just placeholder ghost text), editable before saving."""
    root = str(Path.home() / "llm-server-cockpit")
    return {"models_dir": f"{root}/models", "state_dir": f"{root}/state", "prefix_root": f"{root}/builds"}


def _ancestor(widget: Widget, cls: type) -> Widget | None:
    node: Widget | None = widget
    while node is not None and not isinstance(node, cls):
        node = node.parent  # type: ignore[assignment]
    return node


class _GpuRow(Vertical):
    """Header row (toggle button + optional Remove, same line) plus a detail section shown
    or hidden by the toggle — a hand-built replacement for Collapsible so Remove can share
    the header's row (Collapsible's own title is a plain string, it can't host a second
    widget next to it)."""

    def __init__(self, gpu_id: str, detail: Widget, *, collapsed: bool, show_remove: bool) -> None:
        self.gpu_id = gpu_id
        header_children: list[Widget] = [Button(self._label(collapsed), classes="gpu-row-toggle")]
        if show_remove:
            header_children.append(Button("Remove", classes="gpu-row-remove", variant="error"))
        detail.display = not collapsed
        super().__init__(Horizontal(*header_children, classes="gpu-row-header"), detail, classes="gpu-row")

    def _label(self, collapsed: bool) -> str:
        return f"{'▶' if collapsed else '▼'} {self.gpu_id}"

    def toggle(self) -> None:
        detail = self.children[1]
        detail.display = not detail.display
        self.query_one(".gpu-row-toggle", Button).label = self._label(collapsed=not detail.display)


class SettingsScreen(Widget):
    """Mounted inside a TabPane by cockpit/app.py — not a Textual Screen."""

    # .panel / .panel-title / .button-row / .status-text / .error-text / .inline-row come
    # from cockpit/widgets.py's SHARED_CSS (CockpitApp.CSS) — only this screen's own rules
    # live here. Two independently-scrolling columns (#settings-left/#settings-right), each
    # ONE panel, are the structural fix for the old cut-off/no-scroll layout.
    DEFAULT_CSS = """
    SettingsScreen {
        height: 1fr;
    }
    SettingsScreen #settings-header {
        height: auto;
        margin: 1 0;
    }
    SettingsScreen #settings-header Static {
        width: 1fr;
    }
    SettingsScreen #settings-columns {
        height: 1fr;
    }
    SettingsScreen #settings-left, SettingsScreen #settings-right {
        height: 1fr;
        width: 1fr;
        min-width: 45;
    }
    SettingsScreen #settings-left {
        margin-right: 1;
    }
    SettingsScreen .field-group {
        margin-bottom: 1;
    }
    SettingsScreen Label {
        margin-top: 1;
    }
    SettingsScreen .hint {
        color: $text-muted;
        margin-bottom: 1;
    }
    SettingsScreen #vpn-resolve-preview {
        color: $text-muted;
    }
    SettingsScreen .switch-row {
        height: auto;
        margin-top: 1;
    }
    SettingsScreen .switch-row Label {
        margin-top: 1;
        margin-left: 1;
    }
    SettingsScreen .gpu-row {
        margin-bottom: 1;
    }
    SettingsScreen .gpu-row-header {
        height: auto;
    }
    SettingsScreen .gpu-row-toggle {
        width: 1fr;
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
        self._pending_gpus: list[dict] = [{"id": "gpu0", "vendor": "nvidia", "backends": ["cuda"]}]

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        first_run = self.host_profile is None

        with Horizontal(id="settings-header"):
            yield Static("First setup" if first_run else "Settings", classes="panel-title")
            if first_run:
                yield Button("Create host profile", id="btn-save-profile", variant="primary")

        with Horizontal(id="settings-columns"):
            with VerticalScroll(id="settings-left", classes="panel"):
                with Vertical(classes="field-group"):
                    yield Static("Host identity", classes="panel-title")
                    yield Label("Hostname")
                    yield Input(id="f-hostname")
                    yield Label("Builds to keep")
                    yield Input(id="f-retain-builds")
                    yield Label("HF token env var")
                    yield Input(id="f-token-env")

                with Vertical(classes="field-group"):
                    yield Static("Network bind", classes="panel-title")
                    yield Label("VPN interface (not an IP)")
                    with Horizontal(classes="inline-row"):
                        yield Input(id="f-vpn-interface", placeholder="e.g. tailscale0, wg0")
                        yield Button("ip a", id="btn-show-ip-a")
                    yield Static("", id="vpn-resolve-preview")
                    yield Label("Gateway port")
                    yield Input(id="f-gw-port")
                    yield Label("Health check timeout (opt.)")
                    yield Input(id="f-gw-timeout")

                with Vertical(classes="field-group"):
                    yield Static("Paths", classes="panel-title")
                    yield Label("Model files folder")
                    yield Input(id="f-models-dir")
                    yield Label("App state folder")
                    yield Input(id="f-state-dir")
                    yield Label("Build output folder")
                    yield Input(id="f-prefix-root")

                if first_run:
                    yield Static(
                        "Wake-on-LAN, GPU driver checks, and service/update-check scheduling "
                        "can be configured after initial setup.",
                        classes="hint",
                    )
                    yield Static("", id="profile-error", classes="error-text")
                    yield Static("", id="profile-status", classes="status-text")
                else:
                    yield Static("", id="profile-error", classes="error-text")
                    with Horizontal(classes="button-row"):
                        yield Button("Save host profile", id="btn-save-profile", variant="primary")
                    yield Static("", id="profile-status", classes="status-text")

                    with Vertical(classes="field-group"):
                        yield Static("Wake-on-LAN", classes="panel-title")
                        yield Static("not checked yet", id="wol-status", classes="status-text")
                        with Horizontal(classes="button-row"):
                            yield Button("Check / Enable WOL", id="btn-wol-check")

                    with Vertical(classes="field-group"):
                        yield Static("GPU driver lockfile", classes="panel-title")
                        yield Static("not checked yet", id="drivers-status", classes="status-text")
                        with Horizontal(classes="button-row"):
                            yield Button("Check drivers", id="btn-drivers-check")

                    with Vertical(classes="field-group"):
                        yield Static("Service & update-check settings", classes="panel-title")
                        yield Label("Restart policy")
                        yield Select(_RESTART_POLICY_OPTIONS, id="f-restart-policy", allow_blank=False, value="on-failure")
                        yield Label("Restart delay (sec)")
                        yield Input(id="f-restart-sec")
                        with Horizontal(classes="switch-row"):
                            yield Switch(id="f-scheduled-restart-enabled")
                            yield Label("Scheduled restart")
                        yield Label("Restart schedule")
                        yield Input(id="f-scheduled-restart-calendar")
                        with Horizontal(classes="switch-row"):
                            yield Switch(id="f-update-check-enabled")
                            yield Label("Scheduled update check")
                        yield Label("Check schedule")
                        yield Input(id="f-update-check-calendar")
                        yield Static("", id="service-error", classes="error-text")
                        with Horizontal(classes="button-row"):
                            yield Button("Apply service settings", id="btn-apply-service", variant="primary")
                        yield Static("", id="service-status", classes="status-text")

            with VerticalScroll(id="settings-right", classes="panel"):
                yield Static("GPUs", classes="panel-title")
                if first_run:
                    yield Vertical(id="gpu-editor-list")
                    with Horizontal(classes="button-row"):
                        yield Button("Add GPU", id="btn-add-gpu", variant="primary")
                        yield Button("List PCIe devices", id="btn-show-pcie")
                else:
                    yield Vertical(id="gpu-display-list")

    def on_mount(self) -> None:
        self._populate_profile_form()
        if self.host_profile is None:
            self._render_gpu_editor()
        else:
            self._render_gpu_list()
            self._populate_service_form()
            self._refresh_wol_status()
            self._refresh_drivers_status()
        self._update_vpn_preview(self.query_one("#f-vpn-interface", Input).value)

    def on_refresh_requested(self) -> None:
        """Called by CockpitApp.action_refresh_all — re-reads WOL/driver status only (both are
        read-only queries, safe to run automatically); never re-triggers a mutating action
        like wol.run/drivers.run, which stay explicit button presses behind a ConfirmModal."""
        if self.host_profile is None:
            return
        self._refresh_wol_status()
        self._refresh_drivers_status()

    # -- form population -----------------------------------------------------------

    def _populate_profile_form(self) -> None:
        hp = self.host_profile
        if hp is None:
            self.query_one("#f-hostname", Input).value = self.app_ref.host_name
            self.query_one("#f-gw-port", Input).value = "8090"
            self.query_one("#f-gw-timeout", Input).value = "120"
            self.query_one("#f-retain-builds", Input).value = "3"
            self.query_one("#f-token-env", Input).value = "HF_TOKEN"
            defaults = _default_paths()
            self.query_one("#f-models-dir", Input).value = defaults["models_dir"]
            self.query_one("#f-state-dir", Input).value = defaults["state_dir"]
            self.query_one("#f-prefix-root", Input).value = defaults["prefix_root"]
            return

        self.query_one("#f-hostname", Input).value = hp["hostname"]
        self.query_one("#f-vpn-interface", Input).value = hp["network"]["vpn"]["interface"]
        self.query_one("#f-gw-port", Input).value = str(hp["network"]["gateway"]["port"])
        self.query_one("#f-gw-timeout", Input).value = str(hp["network"]["gateway"].get("health_check_timeout", ""))
        self.query_one("#f-models-dir", Input).value = hp["paths"]["models_dir"]
        self.query_one("#f-state-dir", Input).value = hp["paths"]["state_dir"]
        self.query_one("#f-prefix-root", Input).value = hp["paths"]["prefix_root"]
        self.query_one("#f-retain-builds", Input).value = str(hp["retain_builds"])
        self.query_one("#f-token-env", Input).value = hp["hf"]["token_env"]

    # -- GPUs panel (right column) ---------------------------------------------------

    def _lock_gpu_facts(self) -> dict[str, dict]:
        """Best-effort per-GPU facts from hosts/<hostname>.lock.yaml, for the GPUs panel's
        expanded detail (normal mode only). Returns {} if there's no lockfile yet or it
        fails to parse — this is a display nicety, never a hard requirement. Reads the same
        file _refresh_drivers_status does."""
        if self.host_profile is None:
            return {}
        lock_path = self.repo_root / "hosts" / f"{self.host_profile['hostname']}.lock.yaml"
        if not lock_path.exists():
            return {}
        try:
            data = yaml.safe_load(lock_path.read_text()) or {}
        except Exception:
            return {}
        return data.get("gpus", {}) or {}

    def _render_gpu_list(self) -> None:
        """Normal mode: read-only, one row per gpus[] entry, merged with lockfile facts when
        available. Topology changes stay a direct hosts/<hostname>.yaml edit."""
        container = self.query_one("#gpu-display-list", Vertical)
        container.remove_children()
        lock_facts = self._lock_gpu_facts()
        for gpu in self.host_profile["gpus"]:
            facts = dict(lock_facts.get(gpu["id"], {}))
            facts.pop("vendor", None)
            lines = [f"vendor: {gpu['vendor']}", f"backends: {', '.join(gpu['backends'])}"]
            if facts:
                lines.append("driver/runtime facts (from lockfile):")
                lines.extend(f"  {k}: {v}" for k, v in facts.items())
            detail = Vertical(Static("\n".join(lines)))
            container.mount(_GpuRow(gpu["id"], detail, collapsed=True, show_remove=False))

    def _build_gpu_editor_row(self, index: int, gpu: dict) -> _GpuRow:
        can_remove = len(self._pending_gpus) > 1
        detail = Vertical(
            Label("id"),
            Input(value=gpu["id"], classes="gpu-row-id"),
            Label("vendor"),
            Select(_GPU_VENDOR_OPTIONS, classes="gpu-row-vendor", allow_blank=False, value=gpu["vendor"]),
            Label("backends (comma-separated: cuda, rocm, vulkan, sycl)"),
            Input(value=", ".join(gpu["backends"]), classes="gpu-row-backends"),
        )
        return _GpuRow(gpu["id"] or f"gpu{index}", detail, collapsed=False, show_remove=can_remove)

    def _render_gpu_editor(self) -> None:
        """First-run mode: editable row per pending GPU, plus "Add GPU" below."""
        container = self.query_one("#gpu-editor-list", Vertical)
        container.remove_children()
        for i, gpu in enumerate(self._pending_gpus):
            container.mount(self._build_gpu_editor_row(i, gpu))

    def _read_gpu_row(self, row: _GpuRow) -> dict:
        gpu_id = row.query_one(".gpu-row-id", Input).value.strip()
        vendor = row.query_one(".gpu-row-vendor", Select).value
        backends_raw = row.query_one(".gpu-row-backends", Input).value.strip()
        backends = [b.strip() for b in backends_raw.split(",") if b.strip()]
        return {"id": gpu_id, "vendor": vendor, "backends": backends}

    def _sync_pending_gpus_from_widgets(self) -> None:
        container = self.query_one("#gpu-editor-list", Vertical)
        self._pending_gpus = [self._read_gpu_row(row) for row in container.query(_GpuRow)]

    def _on_add_gpu_row(self) -> None:
        self._sync_pending_gpus_from_widgets()
        next_index = len(self._pending_gpus)
        self._pending_gpus.append({"id": f"gpu{next_index}", "vendor": "nvidia", "backends": ["cuda"]})
        self._render_gpu_editor()

    def _on_remove_gpu_row(self, button: Button) -> None:
        if len(self._pending_gpus) <= 1:
            return
        self._sync_pending_gpus_from_widgets()
        row = _ancestor(button, _GpuRow)
        container = self.query_one("#gpu-editor-list", Vertical)
        rows = list(container.query(_GpuRow))
        if row in rows:
            del self._pending_gpus[rows.index(row)]
        self._render_gpu_editor()

    def _read_pending_gpus(self) -> tuple[list[dict] | None, str | None]:
        self._sync_pending_gpus_from_widgets()
        if not self._pending_gpus:
            return None, "at least one gpu is required"
        gpus = []
        for i, gpu in enumerate(self._pending_gpus):
            if not gpu["id"]:
                return None, f"gpus[{i}].id is required"
            if gpu["vendor"] is Select.BLANK:
                return None, f"gpus[{i}].vendor is required"
            if not gpu["backends"]:
                return None, f"gpus[{i}].backends is required (comma-separated, e.g. cuda)"
            gpus.append(gpu)
        return gpus, None

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

    # -- VPN interface -> IP live preview ---------------------------------------------

    def _update_vpn_preview(self, interface: str) -> None:
        interface = interface.strip()
        preview = self.query_one("#vpn-resolve-preview", Static)
        if not interface:
            preview.update("")
            return
        try:
            ip = swap.resolve_vpn_ip(interface)
        except Exception:
            # Keystroke-driven, outside any @work/try-except-guarded action — never let a
            # missing `ip` binary or similar crash the whole TUI over a live preview.
            preview.update("→ could not resolve (is `ip` available on this host?)")
            return
        preview.update(f"→ resolves to: {ip}" if ip else "→ not found / no address yet")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "f-vpn-interface":
            self._update_vpn_preview(event.value)

    # -- info popups (lspci, ip a) ----------------------------------------------------

    def _show_pcie_devices(self) -> None:
        self.app.push_screen(InfoModal("PCIe devices (lspci)", run_shell_capture(_LSPCI_CMD)))

    def _show_ip_a(self) -> None:
        self.app.push_screen(InfoModal("Network interfaces (ip a)", run_shell_capture("ip a")))

    # -- status helpers -----------------------------------------------------------

    def _set_profile_error(self, text: str) -> None:
        self.query_one("#profile-error", Static).update(text)

    def _set_profile_status(self, text: str) -> None:
        self.query_one("#profile-status", Static).update(text)

    def _set_service_error(self, text: str) -> None:
        self.query_one("#service-error", Static).update(text)

    def _set_service_status(self, text: str) -> None:
        self.query_one("#service-status", Static).update(text)

    # -- read form fields into a candidate host-profile dict ------------------------

    def _read_network_fields(self) -> tuple[dict | None, str | None]:
        vpn_interface = self.query_one("#f-vpn-interface", Input).value.strip()
        gw_port_raw = self.query_one("#f-gw-port", Input).value.strip()
        gw_timeout_raw = self.query_one("#f-gw-timeout", Input).value.strip()
        if not vpn_interface or not gw_port_raw:
            return None, "network.vpn.interface and gateway.port are required"
        try:
            gw_port = int(gw_port_raw)
        except ValueError:
            return None, "gateway.port must be an integer"
        network: dict[str, Any] = {
            "vpn": {"interface": vpn_interface},
            "gateway": {"port": gw_port},
            # WOL is edited elsewhere (or not yet, on first-run) — never part of this form.
            "wol": dict(_WOL_PLACEHOLDER) if self.host_profile is None else self.host_profile["network"]["wol"],
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

        retain_raw = self.query_one("#f-retain-builds", Input).value.strip()
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
        }

        if self.host_profile is None:
            gpus, err = self._read_pending_gpus()
            if err:
                return None, err
            candidate["gpus"] = gpus
        else:
            # GPU topology, service, and update_check are edited elsewhere (GPUs are
            # read-only here by design; service/update_check via "Apply service settings")
            # — carry them forward unchanged rather than dropping them on a structural save.
            candidate["gpus"] = self.host_profile["gpus"]
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

    # -- save host profile (structural fields; first-run creates, normal edits) -----

    def _host_profile_path(self) -> Path:
        return self.repo_root / "hosts" / f"{self.app_ref.host_name}.yaml"

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

        first_run = self.host_profile is None
        verb = "Create" if first_run else "Save"
        message = f"{verb} host profile at hosts/{self.app_ref.host_name}.yaml?"
        if self.runner.dry_run:
            message += " [DRY RUN — preview only, nothing will be written]"
        confirmed = await self.app.push_screen_wait(ConfirmModal(message, confirm_label=verb))
        if not confirmed:
            return

        content = yaml.safe_dump(candidate, sort_keys=False)
        self.runner.write_file(self._host_profile_path(), content)
        wrote = not self.runner.dry_run

        if first_run:
            if wrote:
                self.app.exit(
                    message=f"Host profile created at hosts/{self.app_ref.host_name}.yaml — "
                    "restart the cockpit (bin/cockpit) to continue."
                )
            else:
                self._set_profile_status(
                    "[DRY RUN] would have created the host profile — nothing written. "
                    "Toggle dry-run off (d) and save again to actually create it."
                )
            return

        self.host_profile = candidate
        self._render_gpu_list()
        if wrote:
            self._set_profile_status("host profile saved — restart the cockpit for other tabs to see the change")
        else:
            self._set_profile_status("[DRY RUN] would have saved host profile — nothing written")

    # -- WOL status + check/enable ---------------------------------------------------

    def _refresh_wol_status(self) -> None:
        if self.host_profile is None:
            return
        status = wol.status(self.host_profile)
        lines = [
            f"MAC matches host profile: {'yes' if status['mac_matches'] else 'no'} "
            f"(actual: {status['actual_mac'] or 'unknown'})",
            f"Wake-on-LAN flags: {status['wake_flags'] or 'unknown'}",
            f"persistence unit enabled: {'yes' if status['unit_enabled'] else 'no'}",
        ]
        self.query_one("#wol-status", Static).update("\n".join(lines))

    @work
    async def _confirm_and_check_wol(self) -> None:
        if self.runner.dry_run:
            message = (
                "Check / enable Wake-on-LAN? [DRY RUN — verification reads are real, "
                "but no mutation (ethtool/systemctl/nmcli/TLP config) will actually happen]"
            )
        else:
            message = (
                "Check / enable Wake-on-LAN? [LIVE — may change NIC wake flags, "
                "TLP/NetworkManager config, and enable a systemd persistence unit]"
            )
        confirmed = await self.app.push_screen_wait(ConfirmModal(message, confirm_label="Check / Enable"))
        if not confirmed:
            return
        self.query_one("#wol-status", Static).update("checking...")
        self._run_wol()

    @work(thread=True)
    def _run_wol(self) -> None:
        try:
            wol.run(self.host_profile, self.manifest, self.models, self.runner, self.repo_root)
        except SystemExit as e:
            self.app.call_from_thread(self.app.notify, f"WOL check failed: {e.code}", severity="error")
        except Exception as e:  # never let this crash the whole TUI
            self.app.call_from_thread(self.app.notify, f"WOL check failed: {e}", severity="error")
        else:
            msg = "WOL check complete (dry-run preview only)" if self.runner.dry_run else "WOL enabled/verified"
            self.app.call_from_thread(self.app.notify, msg)
        finally:
            self.app.call_from_thread(self._refresh_wol_status)

    # -- driver-drift status + check --------------------------------------------------

    def _refresh_drivers_status(self) -> None:
        if self.host_profile is None:
            return
        lock_path = self.repo_root / "hosts" / f"{self.host_profile['hostname']}.lock.yaml"
        widget = self.query_one("#drivers-status", Static)
        if not lock_path.exists():
            widget.update("not checked yet (no lockfile at hosts/<hostname>.lock.yaml)")
            return
        try:
            data = yaml.safe_load(lock_path.read_text()) or {}
        except Exception as e:
            widget.update(f"failed to read lockfile: {e}")
            return
        lines = [f"generated_at: {data.get('generated_at', 'unknown')}"]
        for gpu_id, facts in (data.get("gpus") or {}).items():
            facts = dict(facts)
            vendor = facts.pop("vendor", "")
            fact_str = ", ".join(f"{k}={v}" for k, v in facts.items())
            lines.append(f"  {gpu_id} ({vendor}): {fact_str}")
        widget.update("\n".join(lines))

    @work
    async def _confirm_and_check_drivers(self) -> None:
        if self.runner.dry_run:
            message = "Check GPU drivers for drift? [DRY RUN — reads driver/runtime versions only]"
        else:
            message = (
                "Check GPU drivers for drift? [LIVE — captures driver/runtime versions; "
                "exits loudly on detected drift, never auto-corrects]"
            )
        confirmed = await self.app.push_screen_wait(ConfirmModal(message, confirm_label="Check"))
        if not confirmed:
            return
        self.query_one("#drivers-status", Static).update("checking...")
        self._run_drivers_check()

    @work(thread=True)
    def _run_drivers_check(self) -> None:
        try:
            drivers.run(self.host_profile, self.manifest, self.models, self.runner, self.repo_root)
        except SystemExit as e:
            # drivers.run sys.exits loudly by design on detected drift — surface it as an
            # error notification instead of letting it vanish into a dead worker thread.
            self.app.call_from_thread(self.app.notify, str(e), severity="error")
        except Exception as e:
            self.app.call_from_thread(self.app.notify, f"driver check failed: {e}", severity="error")
        else:
            self.app.call_from_thread(self.app.notify, "driver check: no drift detected")
        finally:
            # Show what's actually on disk either way — drift or not.
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
        try:
            schema.validate_host_profile_dict(candidate)
        except schema.ValidationError as e:
            self._set_service_error(f"validation failed: {e}")
            return
        self._set_service_error("")

        if self.runner.dry_run:
            message = (
                "Apply service/update-check settings? [DRY RUN — writes hosts/<hostname>.yaml as a "
                "preview only; no real install/restart/timer change will happen]"
            )
        else:
            message = (
                "Apply service/update-check settings? [LIVE — writes hosts/<hostname>.yaml and may "
                "restart llama-swap and (re)install/remove systemd timers]"
            )
        confirmed = await self.app.push_screen_wait(ConfirmModal(message, confirm_label="Apply"))
        if not confirmed:
            return

        content = yaml.safe_dump(candidate, sort_keys=False)
        self.runner.write_file(self._host_profile_path(), content)
        self.host_profile = candidate
        self._set_service_status("applying...")
        self._apply_service_in_background(candidate)

    @work(thread=True)
    def _apply_service_in_background(self, profile: dict) -> None:
        try:
            swap.run(profile, self.manifest, self.models, self.runner, self.repo_root)
            swap.sync_scheduled_restart(profile, self.repo_root, self.runner)
            swap.sync_update_check_timer(profile, self.repo_root, self.runner)
        except SystemExit as e:
            self.app.call_from_thread(self._set_service_status, f"apply failed: {e.code}")
            return
        except Exception as e:
            self.app.call_from_thread(self._set_service_status, f"apply failed: {e}")
            return
        msg = "dry-run apply complete (nothing actually installed/restarted)" if self.runner.dry_run else "service settings applied"
        self.app.call_from_thread(self._set_service_status, msg)

    # -- button dispatch ------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-save-profile":
            self._confirm_and_save_profile()
        elif bid == "btn-wol-check":
            self._confirm_and_check_wol()
        elif bid == "btn-drivers-check":
            self._confirm_and_check_drivers()
        elif bid == "btn-apply-service":
            self._confirm_and_apply_service()
        elif bid == "btn-add-gpu":
            self._on_add_gpu_row()
        elif bid == "btn-show-pcie":
            self._show_pcie_devices()
        elif bid == "btn-show-ip-a":
            self._show_ip_a()
        elif event.button.has_class("gpu-row-remove"):
            self._on_remove_gpu_row(event.button)
        elif event.button.has_class("gpu-row-toggle"):
            _ancestor(event.button, _GpuRow).toggle()
