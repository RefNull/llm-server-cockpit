"""Cockpit "Dashboard" tab: read-only status board over the rest of the app — no new
provisioning logic, only provision.steps.* read paths already used by the other screens
(list_builds, model_status, swap.status, wol.status, drivers.read_lockfile_status). Landing tab;
every mutating/checking action stays on its own tab, this only answers "what's the state of my
stack right now".
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import ProgressBar, Static

from cockpit import update_check
from cockpit.widgets import CockpitScreenBase
from provision.common import Runner
from provision.steps import build as build_step
from provision.steps import docker, drivers, metrics, swap, tailscale, wol


def _fmt_gb(num_bytes: float) -> str:
    return f"{num_bytes / (1024 ** 3):.1f} GB"


def _fmt_mb(num_mb: float) -> str:
    return f"{num_mb / 1024:.1f} GB" if num_mb >= 1024 else f"{num_mb:.0f} MB"


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
    /* DESIGN.md §2/§3.5: side-by-side only above the 120-cell breakpoint. The layout switch
       itself is SHARED_CSS's .columns-responsive rule; these two only fix up the gutter, which
       is a right margin between columns when they sit beside each other and a bottom margin
       between stacked panels when they don't. */
    Screen.-wide DashboardScreen #dashboard-left {
        margin-right: 1;
    }
    Screen.-narrow DashboardScreen #dashboard-left {
        margin-right: 0;
        margin-bottom: 1;
    }
    DashboardScreen .res-row {
        margin-bottom: 0;
    }
    DashboardScreen .res-row Static {
        margin: 0;
    }
    DashboardScreen .hardware-meta {
        margin-top: 1;
        color: $text-muted;
    }
    DashboardScreen .section-title {
        margin-top: 1;
        margin-bottom: 0;
    }
    DashboardScreen .service-line {
        margin-top: 0;
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

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            with Horizontal(id="dashboard-columns", classes="columns-responsive"):
                with Vertical(id="dashboard-left", classes="panel"):
                    yield Static("Hardware & Resources", classes="panel-title")
                    with Horizontal(classes="res-row"):
                        yield Static("CPU", classes="res-label")
                        yield ProgressBar(total=100, show_bar=True, show_percentage=False, show_eta=False, id="res-cpu-bar")
                        yield Static("", id="res-cpu-text", classes="res-val")
                    with Horizontal(classes="res-row"):
                        yield Static("RAM", classes="res-label")
                        yield ProgressBar(total=100, show_bar=True, show_percentage=False, show_eta=False, id="res-mem-bar")
                        yield Static("", id="res-mem-text", classes="res-val")
                    with Horizontal(classes="res-row"):
                        yield Static("DISK", classes="res-label")
                        yield ProgressBar(total=100, show_bar=True, show_percentage=False, show_eta=False, id="res-disk-bar")
                        yield Static("", id="res-disk-text", classes="res-val")
                    with Horizontal(classes="res-row"):
                        yield Static("GPU", classes="res-label")
                        yield ProgressBar(total=100, show_bar=True, show_percentage=False, show_eta=False, id="res-gpu-bar")
                        yield Static("", id="res-gpu-text", classes="res-val")
                    with Horizontal(classes="res-row"):
                        yield Static("VRAM", classes="res-label")
                        yield ProgressBar(total=100, show_bar=True, show_percentage=False, show_eta=False, id="res-vram-bar")
                        yield Static("", id="res-vram-text", classes="res-val")
                    yield Static("", id="db-hardware", classes="hardware-meta")

                with Vertical(id="dashboard-right", classes="panel"):
                    yield Static("Stack & Services", classes="panel-title")

                    yield Static("LLM Services", classes="section-title")
                    yield Static("", id="db-llm-services", classes="service-line")

                    yield Static("System Services", classes="section-title")
                    yield Static("", id="db-system-services", classes="service-line")

                    yield Static("Docker Containers", classes="section-title")
                    yield Static("", id="db-docker-containers", classes="service-line")

                    yield Static("Upstream Updates", classes="section-title")
                    yield Static("", id="db-upstream-updates", classes="service-line")

    def on_mount(self) -> None:
        super().on_mount()
        self._refresh_all()

    def on_refresh_requested(self) -> None:
        """Called by CockpitApp.action_refresh_all — same pattern every other screen follows."""
        self.models = getattr(self.cockpit_app, "models", self.models)
        self.scripts = getattr(self.cockpit_app, "scripts", self.scripts)
        self._refresh_all()

    @work(thread=True)
    def _refresh_all(self) -> None:
        """One worker computes every panel's text and the telemetry metrics off the main thread.
        Passive background check: toasts on error only per DESIGN.md §6.
        """
        try:
            metrics_data = self._compute_metrics()
            texts = {
                "db-hardware": self._compute_hardware_text(metrics_data.get("gpus", [])),
                "db-llm-services": self._compute_llm_services_text(),
                "db-system-services": self._compute_system_services_text(),
                "db-docker-containers": self._compute_containers_text(),
                "db-upstream-updates": self._compute_upstream_updates_text(),
            }
            self.app.call_from_thread(self._apply_metrics, metrics_data)
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

    # ------------------------------------------------------------------ Telemetry metrics

    def _compute_metrics(self) -> dict:
        try:
            t0 = metrics.read_cpu_times()
            time.sleep(0.1)
            cpu_pct = metrics.cpu_percent_from_delta(t0, metrics.read_cpu_times())
        except Exception:
            cpu_pct = 0.0

        try:
            mem = metrics.read_mem()
        except Exception:
            mem = {"used_bytes": 0, "total_bytes": 0, "percent": 0.0}

        try:
            disk_path = self.host_profile.get("paths", {}).get("models_dir")
            disk = metrics.read_disk(disk_path) if disk_path else None
        except Exception:
            disk = None

        try:
            gpus = metrics.read_gpus()
        except Exception:
            gpus = []

        return {"cpu_pct": cpu_pct, "mem": mem, "disk": disk, "gpus": gpus}

    def _apply_metrics(self, data: dict) -> None:
        if not self.is_mounted:
            return

        # CPU
        cpu_pct = data.get("cpu_pct", 0.0)
        cpu_bar = self.query_one("#res-cpu-bar", ProgressBar)
        cpu_bar.progress = cpu_pct
        self.query_one("#res-cpu-text", Static).update(f"{cpu_pct:.1f}%")

        # RAM
        mem = data.get("mem", {"percent": 0.0, "used_bytes": 0, "total_bytes": 0})
        mem_pct = mem.get("percent", 0.0)
        mem_bar = self.query_one("#res-mem-bar", ProgressBar)
        mem_bar.progress = mem_pct
        if mem.get("total_bytes"):
            self.query_one("#res-mem-text", Static).update(
                f"{_fmt_gb(mem['used_bytes'])} / {_fmt_gb(mem['total_bytes'])}"
            )
        else:
            self.query_one("#res-mem-text", Static).update("n/a")

        # DISK
        disk = data.get("disk")
        disk_bar = self.query_one("#res-disk-bar", ProgressBar)
        disk_text = self.query_one("#res-disk-text", Static)
        if disk is None:
            disk_bar.progress = 0
            disk_text.update("models_dir not configured")
        elif not disk.get("exists"):
            disk_bar.progress = 0
            disk_text.update("models_dir not found")
        else:
            disk_bar.progress = disk.get("percent", 0.0)
            disk_text.update(
                f"{_fmt_gb(disk['used_bytes'])} / {_fmt_gb(disk['total_bytes'])}"
            )

        # GPU Util & VRAM
        gpus = data.get("gpus", [])
        gpu_bar = self.query_one("#res-gpu-bar", ProgressBar)
        gpu_text = self.query_one("#res-gpu-text", Static)
        vram_bar = self.query_one("#res-vram-bar", ProgressBar)
        vram_text = self.query_one("#res-vram-text", Static)

        if not gpus:
            gpu_bar.progress = 0
            gpu_text.update("not detected")
            vram_bar.progress = 0
            vram_text.update("not detected")
        else:
            primary = gpus[0]
            util = primary.get("utilization_pct")
            if util is not None:
                gpu_bar.progress = util
                power = f" ({primary['power_w']:.0f}W)" if primary.get("power_w") is not None else ""
                gpu_text.update(f"{util:.0f}%{power}")
            else:
                gpu_bar.progress = 0
                gpu_text.update("n/a (xpu-smi)" if primary.get("vendor") == "intel" else "n/a")

            mem_used = sum(g.get("memory_used_mb") or 0.0 for g in gpus)
            mem_total = sum(g.get("memory_total_mb") or 0.0 for g in gpus)
            if mem_total > 0:
                vram_pct = 100.0 * mem_used / mem_total
                vram_bar.progress = vram_pct
                vram_text.update(f"{_fmt_mb(mem_used)} / {_fmt_mb(mem_total)}")
            else:
                vram_bar.progress = 0
                vram_text.update("n/a")

    # ------------------------------------------------------------------ Panel text computation

    def _compute_hardware_text(self, live_gpus: list[dict]) -> str:
        lines: list[str] = []
        if live_gpus:
            names = [
                g.get("name") or f"{g.get('vendor', 'GPU').upper()} {g.get('index', i)}"
                for i, g in enumerate(live_gpus)
            ]
            lines.append(f"GPU: {', '.join(names)}")
        else:
            cfg_count = len(self.host_profile.get("gpus", []))
            lines.append(f"GPU: {cfg_count} configured in profile" if cfg_count else "GPU: none configured")

        try:
            lock_data = drivers.read_lockfile_status(self.host_profile, self.repo_root)
            lines.append(
                "Drivers: not checked"
                if lock_data is None
                else f"Drivers: locked ({lock_data.get('generated_at', 'ok')})"
            )
        except Exception:
            lines.append("Drivers: status unavailable")

        try:
            wol_st = wol.status(self.host_profile)
            persist = "on" if wol_st.get("unit_enabled") else "off"
            flags = wol_st.get("wake_flags") or "none"
            lines.append(f"Wake-on-LAN: {flags} flags · persist {persist}")
        except Exception:
            lines.append("Wake-on-LAN: status unavailable")

        return "\n".join(lines)

    def _compute_llm_services_text(self) -> str:
        lines: list[str] = []
        try:
            swap_status = swap.status(self.host_profile)
            if swap_status.get("unit_active"):
                ver = f" ({swap_status['installed_version']})" if swap_status.get("installed_version") else ""
                lines.append(f"[$success]✔[/] llama-swap: running{ver}")
            else:
                lines.append("[$error]✖[/] llama-swap: stopped")
        except FileNotFoundError:
            lines.append("[$text-muted]●[/] llama-swap: systemd not available")
        except Exception as e:
            lines.append(f"[$warning]●[/] llama-swap: error ({e})")

        iface = self.host_profile.get("network", {}).get("vpn", {}).get("interface", "unknown")
        port = self.host_profile.get("network", {}).get("gateway", {}).get("port", 8090)
        try:
            ip = swap.resolve_vpn_ip(iface)
            lines.append(f"Endpoint: {ip}:{port}" if ip else f"Endpoint: {iface}:{port}")
        except Exception:
            lines.append(f"Endpoint: {iface}:{port}")

        active_backends: list[str] = []
        for backend in self.backends:
            try:
                builds = build_step.list_builds(self.host_profile, backend)
                current = next((b for b in builds if b.get("current")), None)
                if current:
                    ref = (current.get("ref") or "")[:10]
                    active_backends.append(f"{backend} ({ref})")
            except Exception:
                pass
        if active_backends:
            lines.append(f"Active backend: {', '.join(active_backends)}")
        else:
            lines.append("Active backend: none built")

        return "\n".join(lines)

    def _compute_system_services_text(self) -> str:
        lines: list[str] = []
        try:
            ts_status = tailscale.status(self.host_profile)
            if not ts_status.get("installed"):
                lines.append("[$warning]●[/] tailscaled: not installed")
            elif ts_status.get("logged_in"):
                ip_str = f" ({ts_status['ip']})" if ts_status.get("ip") else ""
                lines.append(f"[$success]✔[/] tailscaled: connected{ip_str}")
            else:
                state = ts_status.get("backend_state") or "stopped"
                lines.append(f"[$warning]●[/] tailscaled: {state}")
        except Exception as e:
            lines.append(f"[$warning]●[/] tailscaled: error ({e})")

        # TLP / Power profile
        tlp_active = False
        if shutil.which("tlp") is not None:
            try:
                res = subprocess.run(
                    ["systemctl", "is-active", "tlp"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                tlp_active = res.returncode == 0 and res.stdout.strip() == "active"
            except Exception:
                pass

        profile_str = None
        if shutil.which("powerprofilesctl") is not None:
            try:
                res = subprocess.run(
                    ["powerprofilesctl", "get"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if res.returncode == 0 and res.stdout.strip():
                    profile_str = res.stdout.strip()
            except Exception:
                pass
        if not profile_str:
            p = Path("/sys/firmware/acpi/platform_profile")
            if p.exists():
                try:
                    profile_str = p.read_text().strip()
                except Exception:
                    pass

        if tlp_active and profile_str:
            lines.append(f"[$success]✔[/] tlp: active · power: {profile_str}")
        elif tlp_active:
            lines.append("[$success]✔[/] tlp: active")
        elif profile_str:
            lines.append(f"[$success]✔[/] power profile: {profile_str}")
        elif shutil.which("tlp") is not None:
            lines.append("[$warning]●[/] tlp: inactive")
        else:
            lines.append("[$text-muted]●[/] tlp/power: not managed")

        return "\n".join(lines)

    def _compute_containers_text(self) -> str:
        try:
            containers = docker.list_containers()
        except RuntimeError as e:
            return f"[$warning]●[/] Docker: {e}"
        except Exception as e:
            return f"[$warning]●[/] Docker: error ({e})"

        if not containers:
            return "[$text-muted]●[/] Docker: no containers found"

        total = len(containers)
        running = sum(1 for c in containers if c.get("state") == "running")
        unhealthy = [c["name"] for c in containers if "unhealthy" in (c.get("status") or "").lower()]

        if unhealthy:
            return f"[$error]✖[/] {running}/{total} containers running ({len(unhealthy)} unhealthy: {', '.join(unhealthy)})"
        elif running == total:
            return f"[$success]✔[/] {total}/{total} containers healthy"
        else:
            stopped = total - running
            return f"[$warning]●[/] {running}/{total} containers running ({stopped} stopped)"

    def _compute_upstream_updates_text(self) -> str:
        try:
            cpp_res = update_check.check_llama_cpp(self.manifest)
            swap_res = update_check.check_llama_swap(self.manifest)

            updates: list[str] = []
            if cpp_res.get("ok") and cpp_res.get("update_available"):
                latest_sha = (cpp_res.get("latest") or "")[:7]
                updates.append(f"llama.cpp {latest_sha}")
            if swap_res.get("ok") and swap_res.get("update_available"):
                latest_ver = swap_res.get("latest") or ""
                updates.append(f"llama-swap {latest_ver}")

            if updates:
                return f"[$warning]★[/] Update available: {', '.join(updates)}"
            elif cpp_res.get("ok") and swap_res.get("ok"):
                return "[$success]✔[/] All components up to date"
            else:
                return "[$text-muted]●[/] Upstream check offline"
        except Exception:
            return "[$text-muted]●[/] Upstream check offline"
