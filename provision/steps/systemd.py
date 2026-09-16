"""Host-wide systemd unit inspection for the cockpit's Deployments > Systemd tab.

Read paths only — listing, reading a unit's source, and the journal live here; starting and
stopping go through `Runner` at the call site like every other mutation. Unlike
`provision/steps/scripts.py`, which knows only about the units this toolkit generates, this
module makes no assumption about who installed a unit: it reports what the host actually has
loaded, which is the point of the tab.

There is no `run()`. Nothing here provisions anything.
"""
from __future__ import annotations

import subprocess
from typing import Any

from provision.common import Runner

_TIMEOUT_S = 15

# Everything that runs or can trigger something running. Deliberately excludes `target`, `slice`
# and `device`, which are grouping/topology units with nothing to start, stop or read a journal
# from — listing them would pad the table without adding an actionable row. The hide list is
# what handles the volume this still produces on a general-purpose host.
_DEFAULT_KINDS = ("service", "timer", "socket", "path", "mount")


def _systemctl(args: list[str], runner: Runner | None = None) -> tuple[int, str]:
    """Returns (returncode, combined output). Never raises — a host without systemd, or a unit
    that does not exist, is a normal state for this tab rather than an error to propagate."""
    cmd = ["systemctl", *args]
    try:
        if runner is not None:
            result = runner.run(cmd, check=False, capture=True)
            return (0, "") if result is None else (result.returncode, result.stdout or "")
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=_TIMEOUT_S
        )
        return result.returncode, result.stdout or ""
    except Exception as e:  # FileNotFoundError on a host with no systemd, timeouts, OSError
        return 1, f"{type(e).__name__}: {e}"


def list_units(kinds: tuple[str, ...] = _DEFAULT_KINDS) -> list[dict[str, Any]]:
    """Every loaded unit of the given kinds, as [{"unit","load","active","sub","description"}].

    `--plain --no-legend` strips the bullet column and the trailing summary so every line is a
    record. Loaded units only — deliberately not `--all`, which also lists units that are
    merely *referenced* (state `not-found`) and would bury the real ones. Sorted by unit name
    so the table order is stable between refreshes rather than following systemd's own.
    """
    code, out = _systemctl(
        ["list-units", f"--type={','.join(kinds)}", "--no-pager", "--plain", "--no-legend"]
    )
    if code != 0:
        return []
    units: list[dict[str, Any]] = []
    for line in out.splitlines():
        # UNIT LOAD ACTIVE SUB DESCRIPTION — description is the only field with spaces, so a
        # 5-way split from the left keeps it intact.
        parts = line.split(None, 4)
        if len(parts) < 4 or not parts[0].endswith(tuple(f".{k}" for k in kinds)):
            continue
        units.append(
            {
                "unit": parts[0],
                "load": parts[1],
                "active": parts[2],
                "sub": parts[3],
                "description": parts[4] if len(parts) > 4 else "",
            }
        )
    return sorted(units, key=lambda u: u["unit"])


def unit_source(unit: str, runner: Runner | None = None) -> str:
    """The unit's own file plus any drop-ins, via `systemctl cat`.

    `systemctl cat` rather than reading /etc/systemd/system/<unit> directly: a unit can live in
    /lib, /usr/lib or /run, and drop-in fragments under <unit>.d/ change its behaviour without
    appearing in the main file at all. Reading one path would show a file that is real but not
    the whole truth.
    """
    code, out = _systemctl(["cat", unit], runner=runner)
    if code != 0:
        return out or f"could not read the unit source for {unit}"
    return out or "(empty)"
