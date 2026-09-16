"""Wake-on-LAN: verify the configured MAC, enable magic-packet wake, persist across reboot."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from provision.common import Runner

log = logging.getLogger("provision")

# Local, fast commands (ip/ethtool/systemctl/nmcli) — a timeout here is only a backstop against
# one hanging and freezing the cockpit's status-check worker, not a real operational limit.
_TIMEOUT_S = 10

# Module-level so a test can point list_interfaces() at a fixture tree; this host's real
# sysfs is the only value it ever takes in production.
_SYSFS_NET = Path("/sys/class/net")


def _read_iface_mac(iface: str) -> str | None:
    if shutil.which("ip") is None:  # iproute2 isn't guaranteed present on a minimal image
        return None
    result = subprocess.run(
        ["ip", "link", "show", iface],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=_TIMEOUT_S,
    )
    if result.returncode != 0:
        return None
    m = re.search(r"link/ether ([0-9a-fA-F:]{17})", result.stdout)
    return m.group(1) if m else None


def list_interfaces() -> list[dict[str, str]]:
    """Wake-capable NICs on this host as [{"name", "mac"}], sorted by name.

    sysfs rather than `ip link`: no iproute2 dependency (it isn't guaranteed present on a
    minimal image — see _read_iface_mac's guard), and the filters below are the ones that
    matter here. `device` exists only for an interface backed by real hardware, which excludes
    lo, docker0, veth*, tailscale0 and friends — none of which can be woken. `type` 1 is
    ARPHRD_ETHER. `wireless`/`phy80211` marks a wifi NIC, where magic-packet wake is a
    different (and usually absent) mechanism, so it is not offered.

    Returns [] rather than raising on a host without sysfs, so callers can fall back to a
    free-typed interface name.
    """
    root = _SYSFS_NET
    if not root.is_dir():
        return []
    interfaces: list[dict[str, str]] = []
    for path in sorted(root.iterdir()):
        if not (path / "device").exists():
            continue
        if (path / "wireless").is_dir() or (path / "phy80211").exists():
            continue
        try:
            if (path / "type").read_text().strip() != "1":
                continue
            mac = (path / "address").read_text().strip()
        except OSError:
            continue
        if not mac or mac == "00:00:00:00:00:00":
            continue
        interfaces.append({"name": path.name, "mac": mac})
    return interfaces


def _read_wake_flags(iface: str) -> str | None:
    if shutil.which("ethtool") is None:  # often not preinstalled on minimal Debian images
        return None
    result = subprocess.run(
        ["ethtool", iface],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=_TIMEOUT_S,
    )
    if result.returncode != 0:
        return None
    m = re.search(r"Wake-on:\s*(\S+)", result.stdout)
    return m.group(1) if m else None


def _is_enabled(unit: str) -> bool:
    result = subprocess.run(
        ["systemctl", "is-enabled", unit],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=_TIMEOUT_S,
    )
    return result.returncode == 0 and result.stdout.strip() == "enabled"


def _unit_content(iface: str) -> str:
    # RemainAfterExit + ExecStop + Before/Conflicts=shutdown.target: some r8xxx-family NICs
    # (this host's RTL8126A included) drop WOL arming on a full poweroff, not just reboot —
    # a plain boot-time oneshot isn't enough, it has to re-arm again on the way down too.
    return (
        "[Unit]\n"
        f"Description=Enable Wake-on-LAN magic packet for {iface}\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "DefaultDependencies=no\n"
        "Before=shutdown.target\n"
        "Conflicts=shutdown.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "RemainAfterExit=yes\n"
        f"ExecStart=/usr/sbin/ethtool -s {iface} wol g\n"
        f"ExecStop=/usr/sbin/ethtool -s {iface} wol g\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def _fix_tlp(iface: str, runner: Runner) -> None:
    """TLP ships with WOL_DISABLE=Y by default and clobbers WOL on every boot/resume/NM event."""
    if shutil.which("tlp") is None:
        return
    conf_path = Path("/etc/tlp.d/01-wol.conf")
    desired = "WOL_DISABLE=N\n"
    current = conf_path.read_text() if conf_path.exists() else None
    if current == desired:
        log.info("wol: TLP already configured not to disable WOL")
        return
    log.info("wol: TLP detected and WOL_DISABLE not overridden — writing %s", conf_path)
    runner.write_file(conf_path, desired)
    runner.run(["tlp", "start"])


def _active_nm_connection(iface: str) -> str | None:
    result = subprocess.run(
        ["nmcli", "-t", "-f", "DEVICE,NAME", "con", "show", "--active"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=_TIMEOUT_S,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        device, _, name = line.partition(":")
        if device == iface:
            return name
    return None


def _arm_networkmanager(iface: str, runner: Runner) -> None:
    """Without this, NM reapplies its own (default) wake-on-lan setting on every link-up/resume,
    independently of whatever ethtool -s was last told — same class of problem as TLP."""
    if shutil.which("nmcli") is None:
        return
    active = subprocess.run(
        ["systemctl", "is-active", "NetworkManager"], stdout=subprocess.PIPE, text=True, timeout=_TIMEOUT_S,
    )
    if active.returncode != 0:
        return
    conn = _active_nm_connection(iface)
    if conn is None:
        log.warning("wol: NetworkManager is active but no connection is bound to %r — skipping nmcli WOL arming", iface)
        return
    current = subprocess.run(
        ["nmcli", "-t", "-f", "802-3-ethernet.wake-on-lan", "con", "show", conn],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=_TIMEOUT_S,
    )
    if "magic" in current.stdout:
        log.info("wol: NetworkManager connection %r already arms wake-on-lan=magic", conn)
        return
    log.info("wol: arming wake-on-lan=magic on NetworkManager connection %r", conn)
    runner.run(["nmcli", "con", "mod", conn, "802-3-ethernet.wake-on-lan", "magic"])
    runner.run(["nmcli", "con", "up", conn])


def read_wakeup_flag(iface: str) -> str | None:
    """`/sys/class/net/<iface>/device/power/wakeup` — "enabled" or "disabled".

    The authoritative "is wake armed right now" answer, and the only one that needs neither
    ethtool nor root nor any particular unit name. It is what an operator checks by hand
    (`cat /sys/class/net/enp6s0/device/power/wakeup`), and it stays correct on a host where
    WOL was set up by something other than this toolkit.
    """
    try:
        return (_SYSFS_NET / iface / "device" / "power" / "wakeup").read_text().strip() or None
    except OSError:
        return None


def find_persistence_unit(iface: str) -> str | None:
    """An enabled unit that re-arms WOL for `iface`, whatever it is called.

    This used to check only `wol-<iface>.service`, the name run() installs — so a host where
    the operator had already set WOL up under a different name (`wol.service` is the obvious
    one) was reported as "persistence unit enabled: no" while working perfectly. Checks the
    canonical name, then the common bare name, then any unit file in /etc/systemd/system whose
    ExecStart actually arms this interface. The last step is a plain file read: no systemctl,
    no root.
    """
    for candidate in (f"wol-{iface}.service", "wol.service"):
        if _is_enabled(candidate):
            return candidate
    unit_dir = Path("/etc/systemd/system")
    needle = re.compile(rf"ethtool\s+-s\s+{re.escape(iface)}\s+wol")
    try:
        candidates = sorted(unit_dir.glob("*.service"))
    except OSError:
        return None
    for path in candidates:
        try:
            if needle.search(path.read_text()) and _is_enabled(path.name):
                return path.name
        except OSError:
            continue
    return None


def status(host_profile: dict[str, Any]) -> dict[str, Any]:
    """Read-only WOL facts for display (e.g. the cockpit's Settings tab) — reuses run()'s own
    verification helpers but never mutates anything (no ethtool -s, no systemctl enable)."""
    wol_cfg = host_profile["network"]["wol"]
    iface = wol_cfg["interface"]
    expected_mac = wol_cfg["mac"]
    actual_mac = _read_iface_mac(iface)
    mac_matches = actual_mac is not None and actual_mac.lower() == expected_mac.lower()
    wake_flags = _read_wake_flags(iface)
    wakeup = read_wakeup_flag(iface)
    unit = find_persistence_unit(iface)
    # armed: sysfs is preferred because it needs no ethtool; ethtool's "Wake-on: g" is the
    # fallback for a NIC whose driver exposes no power/wakeup attribute.
    armed = (wakeup == "enabled") if wakeup is not None else (bool(wake_flags) and "g" in wake_flags)
    return {
        "mac_matches": mac_matches,
        "actual_mac": actual_mac,
        "wake_flags": wake_flags,
        "wakeup_sysfs": wakeup,
        "armed": armed,
        "unit_name": unit,
        "unit_enabled": unit is not None,
    }


def run(host_profile: dict[str, Any], manifest: dict[str, Any], models: dict[str, Any], runner: Runner, repo_root: Path) -> None:
    wol_cfg = host_profile["network"]["wol"]
    iface = wol_cfg["interface"]
    expected_mac = wol_cfg["mac"]

    actual_mac = _read_iface_mac(iface)
    if actual_mac is None:
        sys.exit(f"wol: interface {iface!r} does not exist on this host — host profile has drifted, fix hosts/{host_profile['hostname']}.yaml")
    if actual_mac.lower() != expected_mac.lower():
        sys.exit(
            f"wol: MAC mismatch on {iface!r} — host profile says {expected_mac}, machine reports {actual_mac}. "
            f"Host profile is a source of truth; fix hosts/{host_profile['hostname']}.yaml, don't proceed on a mismatch."
        )
    log.info("wol: %s MAC matches host profile (%s)", iface, actual_mac)

    _fix_tlp(iface, runner)
    _arm_networkmanager(iface, runner)

    wake_flags = _read_wake_flags(iface)
    if wake_flags is None:
        sys.exit(f"wol: could not read Wake-on-LAN state for {iface!r} via ethtool — is ethtool installed?")
    if "g" not in wake_flags:
        log.info("wol: %s currently Wake-on:%s, enabling magic-packet wake now", iface, wake_flags)
        runner.run(["ethtool", "-s", iface, "wol", "g"])
    else:
        log.info("wol: %s already has magic-packet wake enabled (Wake-on:%s)", iface, wake_flags)

    unit_name = f"wol-{iface}.service"
    unit_path = Path("/etc/systemd/system") / unit_name
    desired_content = _unit_content(iface)
    current_content = unit_path.read_text() if unit_path.exists() else None
    if current_content != desired_content:
        log.info("wol: (re)installing persistence unit %s", unit_name)
        runner.write_file(unit_path, desired_content)
        runner.run(["systemctl", "daemon-reload"])
    else:
        log.info("wol: persistence unit %s already up to date", unit_name)

    if not _is_enabled(unit_name):
        log.info("wol: enabling %s", unit_name)
        runner.run(["systemctl", "enable", unit_name])
    else:
        log.info("wol: %s already enabled", unit_name)

    # Evidence-of-absence verification: re-check reality every run, never assume prior steps worked.
    # Under --dry-run no mutation actually happened, so a pre-existing gap can't have been closed;
    # in that case downgrade to a warning instead of sys.exit so dry-run stays side-effect-free.
    verify_flags = _read_wake_flags(iface)
    verify_enabled = _is_enabled(unit_name)
    problems = []
    if verify_flags is None or "g" not in verify_flags:
        problems.append(f"{iface!r} does not report Wake-on:g (got {verify_flags!r})")
    if not verify_enabled:
        problems.append(f"{unit_name} is not enabled")

    if problems:
        message = "wol: verification FAILED — " + "; ".join(problems)
        if runner.dry_run:
            log.warning("%s (expected under --dry-run: no mutation actually ran)", message)
        else:
            sys.exit(message)
    else:
        log.info("wol: verified — %s has magic-packet wake enabled and %s is enabled for persistence", iface, unit_name)
