#!/usr/bin/env python3
"""Regression guard: launching the cockpit must not touch GPU telemetry.

Reading GPU telemetry wakes the device. `nvidia-smi`/`xpu-smi` on an Intel Arc keep its sysman
telemetry active, and the fans ramp — hard, within moments of opening the app. This has now
been fixed twice: commit 5c986a4 removed a 1Hz sampler, and the Dashboard's remaining
mount-time read was removed after QA saw the ramp again on plain launch. The Dashboard is the
landing tab, so *any* automatic read means every launch pokes every GPU.

The assertion is at the subprocess boundary rather than on metrics.read_gpus, so it also
catches a new caller reaching a vendor tool some other way (a driver query, a shell one-liner).

Run: .venv/bin/python smoke/verify_no_gpu_wake.py
"""
from __future__ import annotations

import asyncio
import pathlib
import subprocess
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import bootstrap  # noqa: E402

bootstrap.add_venv_site_packages(_REPO_ROOT)

# Anything that talks to a GPU and can bring it out of a low-power state.
GPU_TOOLS = ("nvidia-smi", "xpu-smi", "rocm-smi", "amd-smi", "vulkaninfo", "clinfo", "nvtop", "intel_gpu_top")

_spawned: list[str] = []
_real_run, _real_popen = subprocess.run, subprocess.Popen


def _record(cmd) -> None:
    parts = cmd.split() if isinstance(cmd, str) else [str(c) for c in cmd]
    for part in parts:
        leaf = pathlib.PurePath(part).name
        if leaf in GPU_TOOLS:
            _spawned.append(" ".join(parts[:4]))


def _run(cmd, *args, **kwargs):
    _record(cmd)
    return _real_run(cmd, *args, **kwargs)


class _Popen(_real_popen):  # type: ignore[misc]
    def __init__(self, cmd, *args, **kwargs):
        _record(cmd)
        super().__init__(cmd, *args, **kwargs)


subprocess.run = _run
subprocess.Popen = _Popen

from cockpit.app import CockpitApp  # noqa: E402
from provision.steps import metrics  # noqa: E402
from textual.widgets import Button, TabbedContent  # noqa: E402

_reads = 0
_real_read_gpus = metrics.read_gpus


def _counted_read_gpus():
    global _reads
    _reads += 1
    return _real_read_gpus()


metrics.read_gpus = _counted_read_gpus

# Every sub-view, so a new tab that samples on mount is caught too.
_TABS = [
    ("dashboard", None, None),
    ("llm", "#llm-tabs", "backends"),
    ("llm", "#llm-tabs", "models"),
    ("llm", "#llm-tabs", "hf-downloads"),
    ("deployments", "#deployment-tabs", "containers"),
    ("deployments", "#deployment-tabs", "scripts"),
    ("settings", "#settings-tabs", "settings-tab-host"),
    ("settings", "#settings-tabs", "settings-tab-gpus"),
    ("settings", "#settings-tabs", "settings-tab-services"),
]


async def main() -> None:
    app = CockpitApp(host="example")
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause(0.8)
        assert app.query(".res-label"), "dashboard gauges did not mount — this check proves nothing"

        for root, sub_tc, sub in _TABS:
            app.query_one("#main-tabs", TabbedContent).active = root
            if sub_tc:
                await pilot.pause(0.1)
                app.query_one(sub_tc, TabbedContent).active = sub
            await pilot.pause(0.3)

        app.action_refresh_all()  # the global 'r' binding must not sample either
        await pilot.pause(1.5)

        # Idle: catches a timer-based sampler, which is what 5c986a4 removed the first time.
        for _ in range(8):
            await pilot.pause(0.5)

        assert _reads == 0, f"metrics.read_gpus() ran {_reads}x without the operator asking"
        assert not _spawned, f"a GPU tool was spawned automatically: {_spawned}"
        print(f"launch + all {len(_TABS)} sub-views + global refresh + 4s idle: 0 GPU reads, 0 GPU tool spawns")

        # ...and the feature still works when the operator does ask.
        app.query_one("#main-tabs", TabbedContent).active = "dashboard"
        await pilot.pause(0.4)
        app.query_one("#btn-sample-gpus", Button).press()
        await pilot.pause(1.5)
        assert _reads == 1, f"Sample GPUs took {_reads} readings, expected exactly 1"
        print("Sample GPUs: exactly 1 reading, only when pressed")

    print("No-GPU-wake verification PASSED.")


if __name__ == "__main__":
    asyncio.run(main())
