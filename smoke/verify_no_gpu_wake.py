#!/usr/bin/env python3
"""Regression guard: the cockpit must never spawn a GPU vendor tool.

Reading GPU telemetry wakes the device. `nvidia-smi`/`xpu-smi` on an Intel Arc keep its sysman
telemetry active, and the fans ramp — hard, within moments of opening the app. This was fixed
three times: 5c986a4 removed a 1Hz sampler, b799d83 removed the Dashboard's mount-time read and
put the remaining one behind a button, and plans/04 deleted live measurement from the cockpit
outright — because a button still leaves a telemetry path in a deployment tool.

So the assertion is now **never**, not "not at launch": no GPU vendor tool may be spawned by
launching, by visiting any tab, by the global refresh, or by sitting idle. The only sanctioned
caller left in the repo is `drivers.run()` behind Settings → Check Drivers, which this script
does not exercise.

`lspci` is deliberately NOT on the forbidden list. It reads the PCI ID database to name a
device; it does not open the DRM/NVML interfaces that bring a GPU out of a low-power state.
The Dashboard uses it for accelerator product names (plans/04 §0c-bis).

The assertion is at the subprocess boundary rather than on a named function, so it also catches
a new caller reaching a vendor tool some other way (a driver query, a shell one-liner).

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
from textual.widgets import TabbedContent  # noqa: E402

# Every sub-view, so a new tab that samples on mount is caught too.
_TABS = [
    ("dashboard", None, None),
    ("llm", "#llm-tabs", "backends"),
    ("llm", "#llm-tabs", "models"),
    ("llm", "#llm-tabs", "hf-downloads"),
    ("deployments", "#deployment-tabs", "containers"),
    ("deployments", "#deployment-tabs", "scripts"),
    ("deployments", "#deployment-tabs", "systemd"),
    ("settings", "#settings-tabs", "settings-tab-host"),
    ("settings", "#settings-tabs", "settings-tab-gpus"),
    ("settings", "#settings-tabs", "settings-tab-services"),
]


async def main() -> None:
    app = CockpitApp(host="example")
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause(0.8)
        # Sentinel: if the Dashboard never mounted, everything below passes vacuously.
        assert app.query("#inv-model"), "dashboard inventory did not mount — this check proves nothing"

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

        assert not _spawned, f"a GPU vendor tool was spawned: {_spawned}"
        print(f"launch + all {len(_TABS)} sub-views + global refresh + 4s idle: 0 GPU tool spawns")

    print("No-GPU-wake verification PASSED.")


if __name__ == "__main__":
    asyncio.run(main())
