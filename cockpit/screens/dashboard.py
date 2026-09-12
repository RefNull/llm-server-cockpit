"""Cockpit "Dashboard" tab: read-only status board over the rest of the app — no new
provisioning logic, only provision.steps.* read paths already used by the other screens
(list_builds, model_status, swap.status, wol.status, drivers.read_lockfile_status). Landing tab;
every mutating/checking action stays on its own tab, this only answers "what's the state of my
stack right now".
"""
from __future__ import annotations

from collections import deque
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import ProgressBar, Sparkline, Static

from provision.common import Runner
from provision.steps import build as build_step
from provision.steps import docker, drivers, hf, metrics, swap, wol
from provision.steps import scripts as scripts_step

# ~2 minutes of history at the 1Hz sampling rate below.
_HISTORY_LEN = 120


def _fmt_gb(num_bytes: float) -> str:
    return f"{num_bytes / (1024 ** 3):.1f} GB"


def _fmt_mb(num_mb: float) -> str:
    return f"{num_mb / 1024:.1f} GB" if num_mb >= 1024 else f"{num_mb:.0f} MB"


class DashboardScreen(Widget):
    """Mounted as the app's default tab by cockpit/app.py — not a Textual Screen."""

    # .panel / .panel-title / .subtitle come from cockpit/widgets.py's SHARED_CSS.
    DEFAULT_CSS = """
    DashboardScreen {
        height: 1fr;
    }
    DashboardScreen #dashboard-columns {
        height: auto;
    }
    DashboardScreen #dashboard-left, DashboardScreen #dashboard-right {
        width: 1fr;
    }
    DashboardScreen #dashboard-left {
        margin-right: 1;
    }
    DashboardScreen .panel Static {
        margin-top: 1;
    }
    DashboardScreen .resource-row {
        height: auto;
        align-vertical: middle;
        margin-top: 1;
    }
    DashboardScreen .resource-label {
        width: 8;
    }
    DashboardScreen .resource-row ProgressBar {
        width: 26;
        margin-right: 1;
    }
    DashboardScreen .resource-row Sparkline {
        width: 1fr;
        height: 1;
        margin-right: 1;
    }
    DashboardScreen .resource-text {
        width: auto;
    }
    DashboardScreen .resource-gpu-block {
        height: auto;
        margin-top: 1;
    }
    DashboardScreen .resource-subtitle {
        text-style: bold;
        color: $accent;
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
        # nvidia/intel only — amd has no metrics.py query implemented yet (out of scope).
        self._gpu_slots = [
            g for g in self.host_profile.get("gpus", []) if g.get("vendor") in ("nvidia", "intel")
        ]
        self._cpu_history: deque[float] = deque(maxlen=_HISTORY_LEN)
        self._mem_history: deque[float] = deque(maxlen=_HISTORY_LEN)
        self._gpu_util_history: dict[int, deque[float]] = {}
        self._cpu_prev_times: tuple[int, int] | None = None

    def _compute_backends(self) -> list[str]:
        seen: list[str] = []
        for gpu in self.host_profile.get("gpus", []):
            for backend in gpu.get("backends", []):
                if backend not in seen:
                    seen.append(backend)
        return seen

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("At-a-glance status across the LLM stack.", classes="subtitle")
            with Vertical(classes="panel", id="dashboard-resources"):
                yield Static("Resources", classes="panel-title")
                with Horizontal(classes="resource-row"):
                    yield Static("CPU", classes="resource-label")
                    yield ProgressBar(total=100, show_eta=False, id="res-cpu-bar")
                    yield Sparkline([], id="res-cpu-spark")
                    yield Static("", id="res-cpu-text", classes="resource-text")
                with Horizontal(classes="resource-row"):
                    yield Static("Memory", classes="resource-label")
                    yield ProgressBar(total=100, show_eta=False, id="res-mem-bar")
                    yield Sparkline([], id="res-mem-spark")
                    yield Static("", id="res-mem-text", classes="resource-text")
                with Horizontal(classes="resource-row"):
                    yield Static("Disk", classes="resource-label")
                    yield ProgressBar(total=100, show_eta=False, id="res-disk-bar")
                    yield Static("", id="res-disk-text", classes="resource-text")
                for slot_idx, gpu in enumerate(self._gpu_slots):
                    with Vertical(classes="resource-gpu-block"):
                        yield Static(
                            f"GPU: {gpu.get('id', slot_idx)} ({gpu['vendor']})",
                            classes="resource-subtitle",
                        )
                        if gpu["vendor"] == "nvidia":
                            with Horizontal(classes="resource-row"):
                                yield Static("Util", classes="resource-label")
                                yield ProgressBar(total=100, show_eta=False, id=f"res-gpu-{slot_idx}-util-bar")
                                yield Sparkline([], id=f"res-gpu-{slot_idx}-util-spark")
                        with Horizontal(classes="resource-row"):
                            yield Static("Mem", classes="resource-label")
                            yield ProgressBar(total=100, show_eta=False, id=f"res-gpu-{slot_idx}-mem-bar")
                            yield Static("", id=f"res-gpu-{slot_idx}-mem-text", classes="resource-text")
                        yield Static("", id=f"res-gpu-{slot_idx}-info-text", classes="status-text")
            with Horizontal(id="dashboard-columns"):
                with Vertical(id="dashboard-left"):
                    with Vertical(classes="panel"):
                        yield Static("LLM Backends", classes="panel-title")
                        yield Static("", id="db-backends")
                    with Vertical(classes="panel"):
                        yield Static("Models & llama-swap", classes="panel-title")
                        yield Static("", id="db-models")
                    with Vertical(classes="panel"):
                        yield Static("Containers", classes="panel-title")
                        yield Static("", id="db-containers")
                with Vertical(id="dashboard-right"):
                    with Vertical(classes="panel"):
                        yield Static("HF Downloads", classes="panel-title")
                        yield Static("", id="db-downloads")
                    with Vertical(classes="panel"):
                        yield Static("Hardware & Networking", classes="panel-title")
                        yield Static("", id="db-hardware")
                    with Vertical(classes="panel"):
                        yield Static("Scripts", classes="panel-title")
                        yield Static("", id="db-scripts")

    def on_mount(self) -> None:
        self._refresh_all()
        # First reading has nothing to delta against — read_cpu_times() here (a plain /proc
        # read, no subprocess) so the very first tick already has a `prev` to compare to,
        # rather than skipping a tick or showing a bogus 0%/100% on startup.
        self._cpu_prev_times = metrics.read_cpu_times()
        self.set_interval(1.0, self._sample_resources)

    def on_refresh_requested(self) -> None:
        """Called by CockpitApp.action_refresh_all — same pattern every other screen follows."""
        self.models = getattr(self.cockpit_app, "models", self.models)
        self.scripts = getattr(self.cockpit_app, "scripts", self.scripts)
        self._refresh_all()

    @work(thread=True)
    def _refresh_all(self) -> None:
        """One worker computes every panel's text (each of these shells out — build_step is a
        fast fs read, but swap/wol/docker/scripts status all run subprocesses with real
        timeouts) — off the main thread like every other status check in this app, then one
        call_from_thread applies them all so the panels update together. Disk usage rides this
        same normal-cadence refresh rather than the 1Hz sampler below — a `models_dir` size
        doesn't change second to second, and shutil.disk_usage() has no reason to run that
        often."""
        texts = {
            "db-backends": self._compute_backends_text(),
            "db-models": self._compute_models_text(),
            "db-containers": self._compute_containers_text(),
            "db-downloads": self._compute_downloads_text(),
            "db-hardware": self._compute_hardware_text(),
            "db-scripts": self._compute_scripts_text(),
        }
        self.app.call_from_thread(self._apply_texts, texts)
        disk_path = self.host_profile.get("paths", {}).get("models_dir")
        if disk_path:
            self.app.call_from_thread(self._apply_disk, metrics.read_disk(disk_path))

    def _apply_texts(self, texts: dict[str, str]) -> None:
        if not self.is_mounted:
            return
        for widget_id, text in texts.items():
            self.query_one(f"#{widget_id}", Static).update(text)

    def _apply_disk(self, disk: dict) -> None:
        if not self.is_mounted:
            return
        bar = self.query_one("#res-disk-bar", ProgressBar)
        text = self.query_one("#res-disk-text", Static)
        if not disk.get("exists"):
            bar.update(progress=0)
            text.update("models_dir not found")
            return
        bar.update(progress=disk["percent"])
        text.update(f"{disk['percent']:.0f}% ({_fmt_gb(disk['used_bytes'])} / {_fmt_gb(disk['total_bytes'])})")

    # ------------------------------------------------------------------ 1Hz resource sampler
    # Runs only while the app is open — an in-memory deque per metric, nothing persisted or
    # sampled in the background, per the explicit "does not run in the background" requirement.

    @work(thread=True, exclusive=True, group="resource-sampler")
    def _sample_resources(self) -> None:
        curr_cpu_times = metrics.read_cpu_times()
        cpu_pct = metrics.cpu_percent_from_delta(self._cpu_prev_times, curr_cpu_times)
        self._cpu_prev_times = curr_cpu_times
        mem = metrics.read_mem()
        gpus = metrics.read_gpus()
        self.app.call_from_thread(self._apply_resource_sample, cpu_pct, mem, gpus)

    def _apply_resource_sample(self, cpu_pct: float, mem: dict, gpus: list[dict]) -> None:
        if not self.is_mounted:
            return
        self._cpu_history.append(cpu_pct)
        self.query_one("#res-cpu-bar", ProgressBar).update(progress=cpu_pct)
        self.query_one("#res-cpu-spark", Sparkline).data = list(self._cpu_history)
        self.query_one("#res-cpu-text", Static).update(f"{cpu_pct:.0f}%")

        self._mem_history.append(mem["percent"])
        self.query_one("#res-mem-bar", ProgressBar).update(progress=mem["percent"])
        self.query_one("#res-mem-spark", Sparkline).data = list(self._mem_history)
        self.query_one("#res-mem-text", Static).update(
            f"{mem['percent']:.0f}% ({_fmt_gb(mem['used_bytes'])} / {_fmt_gb(mem['total_bytes'])})"
        )

        self._apply_gpu_samples(gpus)

    def _apply_gpu_samples(self, live_gpus: list[dict]) -> None:
        """Declared host_profile GPU slots are fixed at compose time (Textual widgets can't be
        created on the fly for hardware only discovered at runtime); live GPUs are matched back
        to slots by vendor + ordinal position — the closest thing to an identifier a hosts/*.yaml
        gpu entry and an nvidia-smi/xpu-smi device index share."""
        by_vendor: dict[str, list[dict]] = {}
        for gpu in live_gpus:
            by_vendor.setdefault(gpu["vendor"], []).append(gpu)
        vendor_seen: dict[str, int] = {}
        for slot_idx, slot in enumerate(self._gpu_slots):
            vendor = slot["vendor"]
            ordinal = vendor_seen.get(vendor, 0)
            vendor_seen[vendor] = ordinal + 1
            live = by_vendor.get(vendor, [])
            gpu = live[ordinal] if ordinal < len(live) else None
            if vendor == "nvidia":
                self._apply_nvidia_gpu_sample(slot_idx, gpu)
            else:
                self._apply_intel_gpu_sample(slot_idx, gpu)

    def _apply_nvidia_gpu_sample(self, slot_idx: int, gpu: dict | None) -> None:
        info_text = self.query_one(f"#res-gpu-{slot_idx}-info-text", Static)
        if gpu is None:
            info_text.update("not detected (nvidia-smi unavailable or no matching device)")
            return
        history = self._gpu_util_history.setdefault(slot_idx, deque(maxlen=_HISTORY_LEN))
        util = gpu["utilization_pct"] or 0.0
        history.append(util)
        self.query_one(f"#res-gpu-{slot_idx}-util-bar", ProgressBar).update(progress=util)
        self.query_one(f"#res-gpu-{slot_idx}-util-spark", Sparkline).data = list(history)

        mem_total = gpu["memory_total_mb"] or 0.0
        mem_pct = (100.0 * gpu["memory_used_mb"] / mem_total) if mem_total else 0.0
        self.query_one(f"#res-gpu-{slot_idx}-mem-bar", ProgressBar).update(progress=mem_pct)
        self.query_one(f"#res-gpu-{slot_idx}-mem-text", Static).update(
            f"{_fmt_mb(gpu['memory_used_mb'])} / {_fmt_mb(gpu['memory_total_mb'])}"
        )
        power = f"{gpu['power_w']:.0f} W" if gpu["power_w"] is not None else "power: n/a"
        info_text.update(f"{util:.0f}% util — {gpu['name']} — {power}")

    def _apply_intel_gpu_sample(self, slot_idx: int, gpu: dict | None) -> None:
        info_text = self.query_one(f"#res-gpu-{slot_idx}-info-text", Static)
        if gpu is None:
            info_text.update("not detected (xpu-smi unavailable or no matching device)")
            return
        mem_bar = self.query_one(f"#res-gpu-{slot_idx}-mem-bar", ProgressBar)
        mem_text = self.query_one(f"#res-gpu-{slot_idx}-mem-text", Static)
        if gpu["memory_total_mb"]:
            mem_bar.update(progress=100.0 * gpu["memory_used_mb"] / gpu["memory_total_mb"])
            mem_text.update(f"{_fmt_mb(gpu['memory_used_mb'])} / {_fmt_mb(gpu['memory_total_mb'])}")
        else:
            mem_bar.update(progress=0)
            mem_text.update("memory: n/a")
        # utilization_pct is always None here — xpu-smi 2.1.0's utilization telemetry doesn't
        # report real numbers yet (see provision/steps/metrics.py's module docstring).
        power = f"{gpu['power_w']:.0f} W" if gpu["power_w"] is not None else "power: n/a"
        freq = f"{gpu['frequency_mhz']:.0f} MHz" if gpu["frequency_mhz"] is not None else "freq: n/a"
        info_text.update(f"utilization: n/a (xpu-smi) — {power}, {freq}")

    # ------------------------------------------------------------------ panels (pure computation, off main thread)

    def _compute_backends_text(self) -> str:
        """Current installed state per backend — deliberately not an upstream-version check
        (that stays a manual action on the Backends tab); this answers "what's running now"."""
        if not self.backends:
            return "no GPU backends declared in host profile"
        lines: list[str] = []
        for backend in self.backends:
            builds = build_step.list_builds(self.host_profile, backend)
            current = next((b for b in builds if b.get("current")), None)
            if current is None:
                lines.append(f"{backend}: not built yet")
            else:
                ref = (current.get("ref") or "")[:10]
                sane = "ok" if current.get("sane") else "NOT SANE"
                lines.append(f"{backend}: {ref} ({sane})")
        return "\n".join(lines)

    def _compute_models_text(self) -> str:
        counts: dict[str, int] = {}
        for model in self.models.get("models", []):
            engine = model.get("engine", "unknown")
            counts[engine] = counts.get(engine, 0) + 1
        count_line = ", ".join(f"{n} {engine}" for engine, n in sorted(counts.items())) or "no models configured"
        try:
            swap_status = swap.status(self.host_profile)
        except Exception as e:
            return f"{count_line}\nllama-swap: error checking status ({e})"
        swap_line = (
            f"llama-swap: {'running' if swap_status['unit_active'] else 'stopped'} "
            f"({'enabled' if swap_status['unit_enabled'] else 'disabled'} on boot)"
        )
        if swap_status["installed_version"]:
            swap_line += f" — {swap_status['installed_version']}"
        return f"{count_line}\n{swap_line}"

    def _compute_containers_text(self) -> str:
        try:
            containers = docker.list_containers()
        except RuntimeError as e:
            return f"not available: {e}"
        if not containers:
            return "no containers found"
        running = sum(1 for c in containers if c.get("state") == "running")
        unhealthy = [c["name"] for c in containers if "unhealthy" in (c.get("status") or "").lower()]
        text = f"{running}/{len(containers)} running"
        if unhealthy:
            text += f"\nunhealthy: {', '.join(unhealthy)}"
        return text

    def _compute_downloads_text(self) -> str:
        downloaded = missing = placeholder = 0
        for model in self.models.get("models", []):
            if model.get("engine") != "llama-cpp":
                continue
            status = hf.model_status(model, self.host_profile)
            if status == "downloaded":
                downloaded += 1
            elif status == "missing":
                missing += 1
            elif status == "placeholder":
                placeholder += 1
        total = downloaded + missing + placeholder
        if total == 0:
            return "no llama-cpp models configured"
        return f"{downloaded}/{total} downloaded, {missing} missing, {placeholder} placeholder"

    def _compute_hardware_text(self) -> str:
        gpu_count = len(self.host_profile.get("gpus", []))
        lines = [f"{gpu_count} GPU(s) configured"]

        try:
            lock_data = drivers.read_lockfile_status(self.host_profile, self.repo_root)
        except Exception as e:
            lines.append(f"driver lockfile: error reading ({e})")
        else:
            lines.append(
                "driver lockfile: not checked yet" if lock_data is None
                else f"driver lockfile: last checked {lock_data.get('generated_at', 'unknown')}"
            )

        try:
            wol_status = wol.status(self.host_profile)
        except Exception as e:
            lines.append(f"Wake-on-LAN: error checking ({e})")
        else:
            lines.append(
                f"Wake-on-LAN: {wol_status['wake_flags'] or 'unknown'} flags, "
                f"persistence {'enabled' if wol_status['unit_enabled'] else 'disabled'}"
            )
        return "\n".join(lines)

    def _compute_scripts_text(self) -> str:
        registered = self.scripts.get("scripts", [])
        if not registered:
            return "no scripts registered"
        running = 0
        for script in registered:
            try:
                st = scripts_step.status(script["id"])
                if st["unit_active"]:
                    running += 1
            except Exception:
                continue
        return f"{running}/{len(registered)} running"
