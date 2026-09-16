"""Cockpit "Dashboard" tab: a static inventory, not a monitor.

Left column answers "what machine is this" — board, CPU, memory, OS, kernel, and the
configured accelerators with whatever the driver lockfile recorded. Right column answers
"what is deployed and is it on" — one standardised row per managed service (see
cockpit.widgets.service_row: name, then glyph, then detail).

**There is no live measurement here, by decision (plans/04-dashboard-inventory.md).** This
toolkit deploys LLM services; monitoring the host belongs to a monitoring stack. The previous
CPU/RAM/GPU gauges cost two fan-ramp incidents — 5c986a4 removed a 1Hz sampler, b799d83 removed
the mount-time GPU read — because reading GPU telemetry wakes the device and this is the
landing tab. Removing the capability removes the thing that can regress.

Every reader below is a file read, a systemd query, or `lspci`. Nothing here spawns a GPU
vendor tool; `smoke/verify_no_gpu_wake.py` holds that line at the subprocess boundary.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static

from cockpit import __version__
from cockpit.widgets import CockpitScreenBase, escape_markup, service_row
from provision.common import Runner
from provision.steps import build as build_step
from provision.steps import docker, drivers, scripts as scripts_step
from provision.steps import swap, sysinfo, tailscale, wol


def _fmt_gb(num_bytes: float) -> str:
    return f"{num_bytes / (1024 ** 3):.1f} GB"


class DashboardScreen(CockpitScreenBase):
    """Mounted as the app's default tab by cockpit/app.py — not a Textual Screen."""

    DEFAULT_CSS = """
    DashboardScreen {
        height: 1fr;
    }
    DashboardScreen #dashboard-columns {
        height: auto;
    }
    DashboardScreen #dashboard-left, DashboardScreen #dashboard-right {
        width: 1fr;
        height: auto;
    }
    /* DESIGN.md §2/§3.5: side-by-side only above the 121-cell breakpoint. The layout switch
       itself is SHARED_CSS's .columns-responsive rule; the #dashboard-left/#dashboard-right
       gutter rule lives there too, not here — a widget's DEFAULT_CSS is SCOPED_CSS=True by
       default, which silently breaks a selector whose first token is an ancestor ("Screen...")
       rather than the widget itself (see SHARED_CSS's comment on this exact rule). */
    DashboardScreen .section-title {
        margin-top: $space-normal;
        margin-bottom: 0;
    }
    DashboardScreen .service-line {
        margin-top: 0;
    }
    DashboardScreen .inventory-row {
        height: auto;
        margin-bottom: 0;
    }
    DashboardScreen .inventory-label {
        width: 14;
        color: $text-muted;
    }
    DashboardScreen .inventory-value {
        width: 1fr;
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
        self.scripts = getattr(app_ref, "scripts", {"scripts": []})
        self.backends = self._compute_backends()

    def _compute_backends(self) -> list[str]:
        seen: list[str] = []
        for gpu in self.host_profile.get("gpus", []):
            for backend in gpu.get("backends", []):
                if backend not in seen:
                    seen.append(backend)
        return seen

    # ------------------------------------------------------------------ compose

    def _inventory_row(self, label: str, widget_id: str) -> ComposeResult:
        with Horizontal(classes="inventory-row"):
            yield Static(label, classes="inventory-label")
            yield Static("", id=widget_id, classes="inventory-value")

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            with Horizontal(id="dashboard-columns", classes="columns-responsive"):
                with Vertical(id="dashboard-left", classes="panel"):
                    yield Static("System", classes="panel-title")
                    for label, widget_id in (
                        ("Model", "inv-model"),
                        ("CPU", "inv-cpu"),
                        ("Memory", "inv-memory"),
                        ("OS", "inv-os"),
                        ("Kernel", "inv-kernel"),
                        ("Cockpit", "inv-cockpit"),
                    ):
                        yield from self._inventory_row(label, widget_id)

                    yield Static("Accelerators", classes="section-title")
                    yield Static("", id="inv-accelerators", classes="service-line")

                with Vertical(id="dashboard-right", classes="panel"):
                    yield Static("Services", classes="panel-title")
                    for title, widget_id in (
                        ("LLM", "svc-llm"),
                        ("Scripts", "svc-scripts"),
                        ("Containers", "svc-containers"),
                        ("Host", "svc-host"),
                    ):
                        yield Static(title, classes="section-title")
                        yield Static("", id=widget_id, classes="service-line")

    def on_mount(self) -> None:
        super().on_mount()
        # No data read here — ensure_first_view() does it when this tab is first shown.

    def on_refresh_requested(self) -> None:
        self.models = getattr(self.cockpit_app, "models", self.models)
        self.scripts = getattr(self.cockpit_app, "scripts", self.scripts)
        self._refresh_all()

    @work(thread=True)
    def _refresh_all(self) -> None:
        """One worker computes every panel off the main thread. Passive background read:
        toasts on error only, per DESIGN.md §6."""
        try:
            texts = {
                **self._compute_system_inventory(),
                "inv-accelerators": self._compute_accelerators_text(),
                "svc-llm": self._compute_llm_text(),
                "svc-scripts": self._compute_scripts_text(),
                "svc-containers": self._compute_containers_text(),
                "svc-host": self._compute_host_text(),
            }
            self.app.call_from_thread(self._apply_texts, texts)
        except Exception as e:
            self.app.call_from_thread(
                self.app.notify, f"dashboard refresh failed: {e}", severity="error"
            )

    def _apply_texts(self, texts: dict[str, str]) -> None:
        if not self.is_mounted:
            return
        for widget_id, text in texts.items():
            self.query_one(f"#{widget_id}", Static).update(text)

    # ------------------------------------------------------------------ left column: System

    def _compute_system_inventory(self) -> dict[str, str]:
        """Static host identity. Every value degrades to "unknown" rather than raising —
        sysinfo's readers return None on a host that does not expose the file."""
        facts = sysinfo.read_all()
        os_facts, cpu_facts, board = facts["os"], facts["cpu"], facts["board"]

        model = board["model"] or "unknown"
        if board["vendor"] and board["model"]:
            model = f"{board['vendor']} {board['model']}"
        cores = cpu_facts["cores"]
        cpu = cpu_facts["model"] or "unknown"
        if cores and cpu_facts["model"]:
            cpu = f"{cores} × {cpu_facts['model']}"
        total = facts["memory_total_bytes"]
        kernel = os_facts["kernel"] or "unknown"
        if os_facts["arch"]:
            kernel = f"{kernel} ({os_facts['arch']})"
        return {
            "inv-model": model,
            "inv-cpu": cpu,
            "inv-memory": _fmt_gb(total) if total else "unknown",
            "inv-os": os_facts["pretty_name"] or "unknown",
            "inv-kernel": kernel,
            "inv-cockpit": f"v{__version__}",
        }

    def _compute_accelerators_text(self) -> str:
        """One line per GPU the host profile declares, joined to whatever the driver lockfile
        recorded. Declared, not probed: the profile is the source of truth and probing means
        waking the device. `lspci` supplies product names — it is not a GPU vendor tool and does
        not wake anything."""
        try:
            lock = drivers.read_lockfile_status(self.host_profile, self.repo_root)
        except Exception as e:
            lock = None
            lock_error = str(e)
        else:
            lock_error = ""
        locked_gpus = (lock or {}).get("gpus", {})

        try:
            pci_names = sysinfo.read_pci_gpus()
        except Exception:
            pci_names = []

        lines: list[str] = []
        seen_per_vendor: dict[str, int] = {}
        for i, gpu in enumerate(self.host_profile.get("gpus", [])):
            gpu_id = gpu.get("id", f"gpu{i}")
            vendor = gpu.get("vendor", "")
            backends = ", ".join(gpu.get("backends", []))
            # Match by (vendor, ordinal within that vendor), NOT by position in the list.
            # lspci enumerates in PCI bus order and host_profile["gpus"] is in config order;
            # a plain positional match would silently label the Intel slot with the NVIDIA
            # card's name on any host where those orders differ. Same pairing metrics.py used,
            # for the same reason: a hosts/*.yaml entry shares no identifier with a PCI device.
            ordinal = seen_per_vendor.get(vendor, 0)
            seen_per_vendor[vendor] = ordinal + 1
            matches = [n for n in pci_names if vendor and vendor.lower() in n.lower()]
            name = matches[ordinal] if ordinal < len(matches) else None

            facts = dict(locked_gpus.get(gpu_id) or {})
            facts.pop("vendor", None)
            if facts:
                driver = ", ".join(f"{k.replace('_version', '')} {v}" for k, v in facts.items())
            elif lock_error:
                driver = f"lockfile unreadable ({lock_error})"
            else:
                driver = "driver not checked"

            # escape_markup(): a PCI product name routinely contains brackets — "Intel
            # Corporation DG2 [Arc A770]" — and Textual's markup parser eats the span,
            # truncating the name silently. rich.markup.escape does NOT protect against this;
            # see escape_markup's docstring.
            head = f"{gpu_id} · {name or vendor}"
            lines.append(f"[$accent]{escape_markup(head)}[/]")
            lines.append(f"  {escape_markup(f'{backends or chr(45)} · {driver}')}")
        return "\n".join(lines) if lines else "none configured"

    # ------------------------------------------------------------------ right column: Services

    def _compute_llm_text(self) -> str:
        lines: list[str] = []
        iface = self.host_profile.get("network", {}).get("vpn", {}).get("interface", "unknown")
        port = self.host_profile.get("network", {}).get("gateway", {}).get("port", 8090)
        try:
            ip = swap.resolve_vpn_ip(iface)
        except Exception:
            ip = None
        endpoint = f"{ip or iface}:{port}"

        try:
            st = swap.status(self.host_profile)
            version = st.get("installed_version") or "not installed"
            detail = f"{'running' if st['unit_active'] else 'stopped'} · {version} · {endpoint}"
            lines.append(service_row("llama-swap", bool(st["unit_active"]), detail))
        except FileNotFoundError:
            lines.append(service_row("llama-swap", None, f"systemd not available · {endpoint}"))
        except Exception as e:
            lines.append(service_row("llama-swap", None, f"{e} · {endpoint}"))

        for backend in self.backends:
            try:
                builds = build_step.list_builds(self.host_profile, backend)
            except Exception as e:
                lines.append(service_row(f"llama.cpp ({backend})", None, str(e)))
                continue
            current = next((b for b in builds if b.get("current")), None)
            if current:
                ref = (current.get("ref") or "")[:10]
                lines.append(service_row(f"llama.cpp ({backend})", True, f"built · {ref}"))
            else:
                lines.append(service_row(f"llama.cpp ({backend})", False, "not built"))
        return "\n".join(lines)

    def _compute_scripts_text(self) -> str:
        registered = self.scripts.get("scripts", [])
        if not registered:
            return "[$text-muted]none registered[/]"
        lines: list[str] = []
        for script in registered:
            script_id = script["id"]
            try:
                st = scripts_step.status(script_id)
            except Exception as e:
                lines.append(service_row(script_id, None, str(e)))
                continue
            detail = "enabled at boot" if st["unit_enabled"] else "not enabled at boot"
            lines.append(service_row(script_id, bool(st["unit_active"]), detail))
        return "\n".join(lines)

    def _compute_containers_text(self) -> str:
        """One row per container, not a `4/4 healthy` roll-up (plans/04, settled question 1):
        the roll-up hides which one is down, which is the only thing the row is for."""
        try:
            containers = docker.list_containers()
        except RuntimeError as e:
            return service_row("docker", None, str(e))
        except Exception as e:
            return service_row("docker", None, f"error ({e})")
        if not containers:
            return "[$text-muted]none found[/]"
        lines: list[str] = []
        for container in containers:
            status = (container.get("status") or "").strip()
            running = container.get("state") == "running"
            if "unhealthy" in status.lower():
                lines.append(service_row(container["name"], False, status))
            else:
                lines.append(service_row(container["name"], running, status))
        return "\n".join(lines)

    def _compute_host_text(self) -> str:
        lines: list[str] = []
        try:
            ts = tailscale.status(self.host_profile)
            if not ts.get("installed"):
                lines.append(service_row("tailscaled", None, "not installed"))
            elif ts.get("logged_in"):
                lines.append(service_row("tailscaled", True, ts.get("ip") or "no IP yet"))
            else:
                lines.append(service_row("tailscaled", False, ts.get("backend_state") or "stopped"))
        except Exception as e:
            lines.append(service_row("tailscaled", None, f"error ({e})"))

        iface = self.host_profile.get("network", {}).get("wol", {}).get("interface", "unknown")
        try:
            wol_st = wol.status(self.host_profile)
            # The glyph reports whether wake is ARMED, not whether our own unit exists: a host
            # where WOL was set up by hand under another unit name is working, and reporting it
            # as off was the bug. The unit, whatever it is called, is detail.
            source = wol_st.get("unit_name") or "no persistence unit"
            detail = f"{wol_st.get('wakeup_sysfs') or wol_st.get('wake_flags') or 'state unknown'} · {source}"
            if not wol_st.get("mac_matches"):
                detail += f" · MAC drift (actual {wol_st.get('actual_mac') or 'unknown'})"
            lines.append(service_row(f"wake-on-lan ({iface})", bool(wol_st.get("armed")), detail))
        except Exception as e:
            lines.append(service_row(f"wake-on-lan ({iface})", None, f"error ({e})"))

        lines.append(self._power_row())
        return "\n".join(lines)

    def _power_row(self) -> str:
        """TLP and the ACPI platform profile — the one pair where "not managed" is a normal,
        correct answer rather than a fault, so it renders None rather than ✖."""
        tlp_active = False
        if shutil.which("tlp") is not None:
            try:
                res = subprocess.run(["systemctl", "is-active", "tlp"], capture_output=True, text=True, timeout=2)
                tlp_active = res.returncode == 0 and res.stdout.strip() == "active"
            except Exception:
                pass

        profile = None
        if shutil.which("powerprofilesctl") is not None:
            try:
                res = subprocess.run(["powerprofilesctl", "get"], capture_output=True, text=True, timeout=2)
                if res.returncode == 0 and res.stdout.strip():
                    profile = res.stdout.strip()
            except Exception:
                pass
        if not profile:
            acpi = Path("/sys/firmware/acpi/platform_profile")
            if acpi.exists():
                try:
                    profile = acpi.read_text().strip()
                except Exception:
                    pass

        if tlp_active:
            return service_row("tlp", True, f"power profile {profile}" if profile else "active")
        if shutil.which("tlp") is not None:
            return service_row("tlp", False, f"power profile {profile}" if profile else "inactive")
        if profile:
            return service_row("power profile", None, profile)
        return service_row("tlp / power", None, "not managed")
