"""Cockpit "Dashboard" tab: read-only status board over the rest of the app — no new
provisioning logic, only provision.steps.* read paths already used by the other screens
(list_builds, model_status, swap.status, wol.status, drivers.read_lockfile_status). Landing tab;
every mutating/checking action stays on its own tab, this only answers "what's the state of my
stack right now".

GPU telemetry is the one reading this tab does NOT take on its own — see _sample_gpus. Reading
it wakes the device, and this is the landing tab, so doing it automatically means every launch
of the cockpit pokes every GPU. That is the fan-ramp defect twice over (commit 5c986a4 removed
a 1Hz sampler for the same reason; this removes the remaining automatic read). Everything else
here is /proc, systemd and file reads, which cost nothing and stay automatic.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, ProgressBar, Static

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

    BINDINGS = [("g", "sample_gpus", "Sample GPUs")]

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
       gutter rule lives there too now, not here — a widget's DEFAULT_CSS is SCOPED_CSS=True by
       default, which silently breaks a selector whose first token is an ancestor ("Screen...")
       rather than the widget itself (see SHARED_CSS's comment on this exact rule). */
    DashboardScreen .res-row {
        margin-bottom: 0;
    }
    DashboardScreen .res-row Static {
        margin: 0;
    }
    DashboardScreen .hardware-meta {
        margin-top: $space-normal;
        color: $text-muted;
    }
    DashboardScreen .section-title {
        margin-top: $space-normal;
        margin-bottom: 0;
    }
    DashboardScreen .service-line {
        margin-top: 0;
    }
    /* Bar defaults to width: 32 (textual/widgets/_progress_bar.py) — 1fr makes it fill the
       row so PercentageStatus (width 5, right-aligned) sits immediately against its right
       edge instead of floating in the middle of the row (DESIGN.md — Phase 6's "values feel
       disconnected from the bars" fix; composition per _progress_bar.py:293-302). Every row
       now also carries a .res-val column, so "fills the row" means the same width on every
       row and the percentages line up. */
    DashboardScreen .res-row Bar {
        width: 1fr;
    }
    DashboardScreen .gpu-unavailable {
        color: $text-muted;
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
        self.cockpit_app = app_ref
        self.scripts = getattr(app_ref, "scripts", {"scripts": []})
        self.backends = self._compute_backends()
        # Last operator-requested GPU reading, or [] when none has been taken this session.
        # Never populated automatically — see _sample_gpus.
        self._last_gpu_sample: list[dict] = []
        # Static at construction time (host_profile is declarative, no hardware probe): one
        # slot per configured GPU, ordinal position within its own vendor — metrics.read_gpus()
        # has no shared identifier with a hosts/*.yaml gpu entry (metrics.py module docstring),
        # so a live reading is matched back to a slot by (vendor, ordinal) at apply time.
        self._gpu_slots: list[dict] = []
        seen_idx: dict[str, int] = {}
        for gpu in self.host_profile.get("gpus", []):
            vendor = gpu.get("vendor", "")
            idx = seen_idx.get(vendor, 0)
            seen_idx[vendor] = idx + 1
            self._gpu_slots.append({"id": gpu.get("id", vendor), "vendor": vendor, "vendor_idx": idx})

    def _compute_backends(self) -> list[str]:
        seen: list[str] = []
        for gpu in self.host_profile.get("gpus", []):
            for backend in gpu.get("backends", []):
                if backend not in seen:
                    seen.append(backend)
        return seen

    def _compose_gpu_slot(self, i: int, slot: dict) -> ComposeResult:
        """One dashboard section per configured GPU (decision 0d.4): never a 0% bar for a stat
        that isn't real. Intel's utilization_pct is always None (xpu-smi 2.1.0 doesn't report
        it — metrics.py module docstring) and AMD has zero telemetry at all (metrics.read_gpus()
        is nvidia+intel only) — both known statically, so those gaps are composed as plain text
        up front rather than decided later from a live reading. A configured nvidia/intel GPU
        that the live tool doesn't currently see is the one gap that can't be known until the
        one-shot sample comes back — res-gpu-{i}-*-unavail rows exist for that and start hidden.
        """
        vendor = slot["vendor"]
        # Title starts as the config id (the only thing known at compose time, before any
        # reading exists) and is rewritten to the live device name once one is matched — see
        # _apply_metrics. Needs an id here so that update can find it.
        yield Static(f"GPU {i}: {slot['id']} ({vendor})", id=f"res-gpu-{i}-title", classes="section-title")
        if vendor == "amd":
            yield Static("no AMD telemetry", id=f"res-gpu-{i}-unavail", classes="gpu-unavailable")
            return
        if vendor == "nvidia":
            # Bars start hidden, not at 0%: nothing has been read yet, and a 0% bar would be a
            # made-up reading (decision 0d.4). The -unavail line carries "not sampled" until
            # the operator asks for a reading.
            with Horizontal(classes="res-row gpu-bar-row", id=f"res-gpu-{i}-util-row"):
                yield Static("Util", classes="res-label")
                yield ProgressBar(total=100, show_eta=False, id=f"res-gpu-{i}-util-bar")
                yield Static("", id=f"res-gpu-{i}-util-extra", classes="res-val")
            yield Static("Util: not sampled", id=f"res-gpu-{i}-util-unavail", classes="gpu-unavailable")
        else:  # intel — utilization_pct is always None, never composed as a bar
            yield Static("Util: not reported by xpu-smi", classes="gpu-unavailable")
        with Horizontal(classes="res-row gpu-bar-row", id=f"res-gpu-{i}-mem-row"):
            yield Static("Mem", classes="res-label")
            yield ProgressBar(total=100, show_eta=False, id=f"res-gpu-{i}-mem-bar")
            yield Static("", id=f"res-gpu-{i}-mem-text", classes="res-val")
        yield Static("Mem: not sampled", id=f"res-gpu-{i}-mem-unavail", classes="gpu-unavailable")

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            with Horizontal(id="dashboard-columns", classes="columns-responsive"):
                with Vertical(id="dashboard-left", classes="panel"):
                    yield Static("Hardware & Resources", classes="panel-title")
                    with Horizontal(classes="res-row"):
                        yield Static("CPU", classes="res-label")
                        yield ProgressBar(total=100, show_eta=False, id="res-cpu-bar")
                        # No absolute reading exists for CPU, but the column does: without it
                        # this bar ran 18 cells longer than RAM's and put its "%" somewhere
                        # else entirely. See SHARED_CSS's .res-val.
                        yield Static("", classes="res-val")
                    with Horizontal(classes="res-row"):
                        yield Static("RAM", classes="res-label")
                        yield ProgressBar(total=100, show_eta=False, id="res-mem-bar")
                        yield Static("", id="res-mem-text", classes="res-val")
                    yield Static("", id="db-hardware", classes="hardware-meta")

                    for i, slot in enumerate(self._gpu_slots):
                        yield from self._compose_gpu_slot(i, slot)

                    if self._gpu_slots:
                        # The only control on this tab. Reading GPU telemetry wakes the device
                        # (nvidia-smi/xpu-smi), so it is an explicit act, not something the
                        # landing tab does to every GPU on every launch.
                        with Horizontal(classes="action-row-secondary"):
                            yield Button("Sample GPUs", id="btn-sample-gpus", classes="thin-button")
                        yield Static(
                            "GPU readings are on demand — sampling wakes the device.",
                            id="gpu-sample-status",
                            classes="status-text",
                        )

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
        # Hidden until a reading exists. Composed-but-hidden rather than composed-on-demand so
        # _apply_gpu_sample can keep toggling .display per row exactly as it already does; a
        # visible 0% bar next to "not sampled" would be a made-up reading (decision 0d.4).
        for row in self.query(".gpu-bar-row"):
            row.display = False
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
                "db-hardware": self._compute_hardware_text(self._last_gpu_sample),
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

        # Deliberately no metrics.read_gpus() here. This runs on mount and on every global
        # refresh; nvidia-smi/xpu-smi wake the device, which is the fan ramp. GPU readings come
        # from _sample_gpus only, when the operator asks.
        return {"cpu_pct": cpu_pct, "mem": mem}

    def _match_live_gpu(self, slot: dict, live_gpus: list[dict]) -> dict | None:
        """Pair a configured GPU slot with its live reading by (vendor, ordinal position) —
        metrics.py's module docstring: there's no shared identifier between a hosts/*.yaml gpu
        entry and an nvidia-smi/xpu-smi device index."""
        matches = [g for g in live_gpus if g.get("vendor") == slot["vendor"]]
        idx = slot["vendor_idx"]
        return matches[idx] if idx < len(matches) else None

    def _apply_metrics(self, data: dict) -> None:
        if not self.is_mounted:
            return

        # CPU
        cpu_pct = data.get("cpu_pct", 0.0)
        self.query_one("#res-cpu-bar", ProgressBar).update(progress=cpu_pct)

        # RAM
        mem = data.get("mem", {"percent": 0.0, "used_bytes": 0, "total_bytes": 0})
        self.query_one("#res-mem-bar", ProgressBar).update(progress=mem.get("percent", 0.0))
        if mem.get("total_bytes"):
            self.query_one("#res-mem-text", Static).update(
                f"{_fmt_gb(mem['used_bytes'])} / {_fmt_gb(mem['total_bytes'])}"
            )
        else:
            self.query_one("#res-mem-text", Static).update("n/a")

    def _apply_gpu_sample(self, live_gpus: list[dict]) -> None:
        """Render one operator-requested GPU reading. Split out of _apply_metrics because the
        two now run on completely different triggers: CPU/RAM on mount and refresh, GPU only
        on demand (decision 0d.4 still holds — mark gaps explicitly, never a 0% bar for a stat
        that isn't real)."""
        if not self.is_mounted:
            return
        self._last_gpu_sample = live_gpus
        for i, slot in enumerate(self._gpu_slots):
            live = self._match_live_gpu(slot, live_gpus)
            # Header shows the live device name when a reading was matched (Defect 4:
            # previously always the opaque host-profile config id, e.g. "gpu0"), falling back
            # to the config id when the GPU isn't detected. Intel has no real product name
            # (metrics.py hardcodes f"Intel GPU {device_id}") — that hardcoded string is still
            # a live reading, so it's used as-is; only an undetected GPU falls back to the id.
            device_label = (live.get("name") if live else None) or slot["id"]
            self.query_one(f"#res-gpu-{i}-title", Static).update(f"GPU {i}: {device_label} ({slot['vendor']})")

            if slot["vendor"] == "amd":
                continue  # static text only, composed once — nothing else to update

            if slot["vendor"] == "nvidia":
                util_row = self.query_one(f"#res-gpu-{i}-util-row", Horizontal)
                util_unavail = self.query_one(f"#res-gpu-{i}-util-unavail", Static)
                util = live.get("utilization_pct") if live else None
                if live is None:
                    util_row.display = False
                    util_unavail.update("Util: not detected")
                    util_unavail.display = True
                elif util is None:
                    util_row.display = False
                    util_unavail.update("Util: n/a")
                    util_unavail.display = True
                else:
                    util_row.display = True
                    util_unavail.display = False
                    self.query_one(f"#res-gpu-{i}-util-bar", ProgressBar).update(progress=util)
                    power = live.get("power_w")
                    self.query_one(f"#res-gpu-{i}-util-extra", Static).update(
                        f"({power:.0f}W)" if power is not None else ""
                    )

            mem_row = self.query_one(f"#res-gpu-{i}-mem-row", Horizontal)
            mem_unavail = self.query_one(f"#res-gpu-{i}-mem-unavail", Static)
            mem_total = live.get("memory_total_mb") if live else None
            if live is None:
                mem_row.display = False
                mem_unavail.update("Mem: not detected")
                mem_unavail.display = True
            elif not mem_total:
                mem_row.display = False
                mem_unavail.update("Mem: n/a")
                mem_unavail.display = True
            else:
                mem_row.display = True
                mem_unavail.display = False
                mem_used = live.get("memory_used_mb") or 0.0
                self.query_one(f"#res-gpu-{i}-mem-bar", ProgressBar).update(
                    progress=100.0 * mem_used / mem_total
                )
                self.query_one(f"#res-gpu-{i}-mem-text", Static).update(
                    f"{_fmt_mb(mem_used)} / {_fmt_mb(mem_total)}"
                )

    # ------------------------------------------------------------------ GPU sampling (on demand)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if (event.button.id or "") == "btn-sample-gpus":
            self.action_sample_gpus()

    def action_sample_gpus(self) -> None:
        self.query_one("#gpu-sample-status", Static).update("sampling...")
        self._sample_gpus()

    @work(thread=True)
    def _sample_gpus(self) -> None:
        """The ONLY path that runs nvidia-smi/xpu-smi. Operator-triggered, one shot, never on
        mount, never on the global 'r' refresh, never on a timer."""
        try:
            gpus = metrics.read_gpus()
            error = None
        except Exception as e:
            gpus, error = [], str(e)
        self.app.call_from_thread(self._apply_gpu_sample, gpus)
        stamp = time.strftime("%H:%M:%S")
        self.app.call_from_thread(
            self.query_one("#gpu-sample-status", Static).update,
            f"sample failed: {error}" if error else f"sampled at {stamp} — readings are a snapshot, not live.",
        )
        # The hardware line names GPUs from the live reading when there is one.
        self.app.call_from_thread(
            self.query_one("#db-hardware", Static).update, self._compute_hardware_text(gpus)
        )

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
            # Not an error: no GPU has been sampled yet, which is the default state.
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
