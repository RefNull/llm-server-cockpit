"""Wake-on-LAN: verify the configured MAC, enable magic-packet wake, persist across reboot."""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from string import Template
from typing import Any

from provision.common import Runner, unit_command, unit_value

log = logging.getLogger("provision")

# Local, fast commands (ip/ethtool/systemctl/nmcli) — a timeout here is only a backstop against
# one hanging and freezing the cockpit's status-check worker, not a real operational limit.
_TIMEOUT_S = 10

# Module-level so a test can point list_interfaces() at a fixture tree; this host's real
# sysfs is the only value it ever takes in production.
_SYSFS_NET = Path("/sys/class/net")

# Debian keeps these off a non-root user's PATH; `shutil.which` run from the cockpit's
# unprivileged, in-process caller (settings.py:1119) therefore misses tools that plainly exist
# on the host and are hardcoded elsewhere in this file (e.g. /usr/sbin/ethtool in the unit
# template) — see plans/05-qa-remediation-pass.md §0e, defect (i). Checked before falling back
# to shutil.which so a non-standard install still resolves.
_SBIN_DIRS = ("/usr/sbin", "/sbin", "/usr/local/sbin")


def _resolve_tool(name: str) -> str | None:
    """Absolute path to `name`, checked in the standard sbin locations before PATH.

    Returns None if it cannot be found anywhere — the caller decides what that means (skip a
    non-essential step, or abort before mutating anything).
    """
    for directory in _SBIN_DIRS:
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(name)


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


def _read_wake_flags(iface: str, ethtool_path: str | None = None) -> str | None:
    """Read ethtool's Wake-on flags for `iface`.

    `ethtool_path` lets a caller that already resolved the binary (run(), to guarantee the
    same absolute path used for a subsequent mutation) reuse that resolution; status() has no
    mutation to keep consistent with, so it resolves fresh each call.
    """
    if ethtool_path is None:
        ethtool_path = _resolve_tool("ethtool")  # often not preinstalled on minimal Debian images
    if ethtool_path is None:
        return None
    result = subprocess.run(
        [ethtool_path, iface],
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


def _unit_content(iface: str, repo_root: Path) -> str:
    # RemainAfterExit + ExecStop + Before/Conflicts=shutdown.target: some r8xxx-family NICs
    # (this host's RTL8126A included) drop WOL arming on a full poweroff, not just reboot —
    # a plain boot-time oneshot isn't enough, it has to re-arm again on the way down too.
    #
    # Rendered from systemd/wol.service.tmpl (string.Template, matching swap.py:273-288)
    # rather than inlined here, so smoke/verify_rendered_units.py can parse the actual output
    # instead of trusting the code that built it. `iface` appears in a plain field
    # (Description=) and in command lines (ExecStart=/ExecStop=), which the systemd escaping
    # contract (provision/common.py:152-173) treats differently — `$` is only special in a
    # command setting — hence two substitution keys for the same value.
    tmpl_path = repo_root / "systemd" / "wol.service.tmpl"
    template = Template(tmpl_path.read_text())
    return template.substitute(iface=unit_value(iface), iface_cmd=unit_command(iface))


_TLP_MAIN_CONF = Path("/etc/tlp.conf")
_TLP_DROPIN_DIR = Path("/etc/tlp.d")
_TLP_WOL_CONF = _TLP_DROPIN_DIR / "01-wol.conf"
_TLP_WOL_DISABLE_RE = re.compile(r"^\s*WOL_DISABLE\s*=\s*(\S+)")


def _tlp_effective_wol_disable() -> tuple[str | None, Path | None]:
    """The WOL_DISABLE value TLP will actually apply, and which file sets it.

    Load order per TLP's own documentation (https://linrunner.de/tlp/settings/introduction.html):
    "Load order is: 1. intrinsic defaults, 2. /etc/tlp.d/*.conf, 3. /etc/tlp.conf" — drop-ins in
    `/etc/tlp.d/` are "read in lexical (alphabetical) order", and "in case of identical
    parameters in several but also within the same file, the last occurence has precedence."
    The page states this plainly: "parameters in /etc/tlp.conf will override anything else
    because it is read last." So `/etc/tlp.conf` beats every drop-in outright, regardless of
    the drop-in's name — this toolkit's own `01-wol.conf` included. An earlier version of this
    helper read `/etc/tlp.conf` first, which reconstructed the defect this whole fix exists to
    close on a host where `/etc/tlp.conf` itself sets `WOL_DISABLE=Y`: it reported the drop-in's
    "N" as effective while TLP actually applied "Y". See `smoke/verify_sysinfo.py`'s
    `check_wol_tlp_precedence` for the regression test.

    Returns (None, None) if no file sets the key anywhere, matching TLP's documented default of
    WOL_DISABLE=Y (i.e. "disabled" unless something opts back in).
    """
    value: str | None = None
    source: Path | None = None
    candidates: list[Path] = []
    if _TLP_DROPIN_DIR.is_dir():
        candidates.extend(sorted(_TLP_DROPIN_DIR.glob("*.conf")))
    candidates.append(_TLP_MAIN_CONF)  # read LAST — it overrides every drop-in unconditionally
    for path in candidates:
        try:
            text = path.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            m = _TLP_WOL_DISABLE_RE.match(line)
            if m:
                value = m.group(1).strip().strip("\"'")
                source = path
    return value, source


def _fix_tlp(iface: str, runner: Runner) -> bool:
    """TLP ships with WOL_DISABLE=Y by default and clobbers WOL on every boot/resume/NM event.

    Returns whether this call wrote the drop-in and (re)started tlp — the caller reports this
    in its end-state summary rather than presenting a hidden mutation as a no-op.
    """
    tlp_path = _resolve_tool("tlp")
    if tlp_path is None:
        return False
    value, source = _tlp_effective_wol_disable()
    if value == "N":
        log.info("wol: TLP already configured not to disable WOL (%s)", source)
        return False
    desired = "WOL_DISABLE=N\n"
    log.info(
        "wol: TLP's effective WOL_DISABLE is %r (from %s) — writing %s",
        value, source or "TLP's own default (no file sets it)", _TLP_WOL_CONF,
    )
    runner.write_file(_TLP_WOL_CONF, desired)
    runner.run([tlp_path, "start"])
    # A later-sorting drop-in, or /etc/tlp.conf (which TLP reads last and which therefore beats
    # every drop-in unconditionally — see _tlp_effective_wol_disable's docstring), can still
    # override what was just written, and the write accomplished nothing. Re-check and say so
    # loudly rather than reporting success from the mere fact that a file was written.
    if not runner.dry_run:
        after_value, after_source = _tlp_effective_wol_disable()
        if after_value != "N":
            if after_source == _TLP_MAIN_CONF:
                log.warning(
                    "wol: wrote %s but TLP's effective WOL_DISABLE is still %r — /etc/tlp.conf "
                    "sets it and TLP reads that file LAST, so no drop-in in /etc/tlp.d/ (this "
                    "toolkit's own included) can ever override it. This toolkit will not edit "
                    "/etc/tlp.conf — that is the operator's file. Edit WOL_DISABLE in "
                    "/etc/tlp.conf directly to fix this.",
                    _TLP_WOL_CONF, after_value,
                )
            else:
                log.warning(
                    "wol: wrote %s but TLP's effective WOL_DISABLE is still %r — %s sorts after "
                    "01-wol.conf and overrides it. Fix or remove that file; this toolkit will not "
                    "rename its own drop-in to win a sort order it does not control.",
                    _TLP_WOL_CONF, after_value, after_source,
                )
    return True


def _active_nm_connection(iface: str, nmcli_path: str) -> str | None:
    result = subprocess.run(
        [nmcli_path, "-t", "-f", "DEVICE,NAME", "con", "show", "--active"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=_TIMEOUT_S,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        device, _, name = line.partition(":")
        if device == iface:
            return name
    return None


def _arm_networkmanager(iface: str, runner: Runner) -> bool:
    """Without this, NM reapplies its own (default) wake-on-lan setting on every link-up/resume,
    independently of whatever ethtool -s was last told — same class of problem as TLP.

    Returns whether this call disturbed anything (modified the connection's wake-on-lan setting
    and/or cycled the link with `nmcli con up`) — the caller reports this in its end-state
    summary rather than presenting a hidden mutation as a no-op.
    """
    nmcli_path = _resolve_tool("nmcli")
    if nmcli_path is None:
        return False
    active = subprocess.run(
        ["systemctl", "is-active", "NetworkManager"], stdout=subprocess.PIPE, text=True, timeout=_TIMEOUT_S,
    )
    if active.returncode != 0:
        return False
    conn = _active_nm_connection(iface, nmcli_path)
    if conn is None:
        log.warning("wol: NetworkManager is active but no connection is bound to %r — skipping nmcli WOL arming", iface)
        return False
    current = subprocess.run(
        [nmcli_path, "-t", "-f", "802-3-ethernet.wake-on-lan", "con", "show", conn],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=_TIMEOUT_S,
    )
    if "magic" in current.stdout:
        log.info("wol: NetworkManager connection %r already arms wake-on-lan=magic", conn)
        return False
    log.info("wol: arming wake-on-lan=magic on NetworkManager connection %r", conn)
    runner.run([nmcli_path, "con", "mod", conn, "802-3-ethernet.wake-on-lan", "magic"])
    runner.run([nmcli_path, "con", "up", conn])
    return True


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
    tlp_present = _resolve_tool("tlp") is not None
    tlp_wol_disable_value, tlp_wol_disable_source = (
        _tlp_effective_wol_disable() if tlp_present else (None, None)
    )
    # Present even when TLP will not disable WOL: a host that "looks armed" right now can stop
    # being armed after the next suspend/resume/boot if TLP's effective value isn't "N" — see
    # plans/05-qa-remediation-pass.md §0e, defect (i).
    tlp_disables_wol = tlp_present and tlp_wol_disable_value != "N"
    return {
        "mac_matches": mac_matches,
        "actual_mac": actual_mac,
        "wake_flags": wake_flags,
        "wakeup_sysfs": wakeup,
        "armed": armed,
        "unit_name": unit,
        "unit_enabled": unit is not None,
        "tlp_present": tlp_present,
        "tlp_disables_wol": tlp_disables_wol,
        "tlp_wol_disable_value": tlp_wol_disable_value,
        "tlp_wol_disable_source": str(tlp_wol_disable_source) if tlp_wol_disable_source else None,
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

    # Fail before touching anything: resolve and probe ethtool BEFORE the link-disturbing
    # steps below (_fix_tlp restarts tlp; _arm_networkmanager's `nmcli con up` is a link-up
    # event). Previously this check ran after those two, so a missing ethtool aborted the run
    # having already cycled the NIC and re-applied TLP policy, with no re-arm — §0e, defect (ii).
    ethtool_path = _resolve_tool("ethtool")
    if ethtool_path is None:
        sys.exit(f"wol: ethtool not found in {_SBIN_DIRS} or PATH — cannot verify or arm {iface!r}. Nothing has been changed.")
    wake_flags_before = _read_wake_flags(iface, ethtool_path)
    if wake_flags_before is None:
        sys.exit(f"wol: ethtool found at {ethtool_path} but reading Wake-on-LAN state for {iface!r} failed. Nothing has been changed.")

    tlp_mutated = _fix_tlp(iface, runner)
    nm_mutated = _arm_networkmanager(iface, runner)

    # Unconditional re-arm: TLP's/NM's re-application of policy above is not synchronous with
    # `tlp start`/`nmcli con up` returning, so a single sample taken before them (or a
    # check-then-act on it) can suppress the only re-arm in the run — §0e, defect (iii). The
    # command is idempotent, so issuing it regardless costs nothing; the log line below keeps
    # the useful half of the old guard (was it already armed before this run).
    if "g" in wake_flags_before:
        log.info(
            "wol: %s already had magic-packet wake enabled before this run (Wake-on:%s) — "
            "re-arming anyway since TLP/NetworkManager may have just reset it",
            iface, wake_flags_before,
        )
    else:
        log.info("wol: %s was Wake-on:%s before this run, enabling magic-packet wake now", iface, wake_flags_before)
    runner.run([ethtool_path, "-s", iface, "wol", "g"])

    unit_name = f"wol-{iface}.service"
    unit_path = Path("/etc/systemd/system") / unit_name
    desired_content = _unit_content(iface, repo_root)
    current_content = unit_path.read_text() if unit_path.exists() else None
    if current_content != desired_content:
        log.info("wol: (re)installing persistence unit %s", unit_name)
        runner.write_file(unit_path, desired_content)
        runner.run(["systemctl", "daemon-reload"])
    else:
        log.info("wol: persistence unit %s already up to date", unit_name)

    if not _is_enabled(unit_name):
        log.info("wol: enabling and starting %s", unit_name)
        # --now: Type=oneshot + RemainAfterExit=yes means starting it runs ExecStart
        # immediately — the in-band re-arm this run otherwise lacks entirely, since a unit
        # that is merely enabled does not run until the next boot. Matches swap.py:349,420.
        runner.run(["systemctl", "enable", "--now", unit_name])
    else:
        log.info("wol: %s already enabled", unit_name)

    # Evidence-of-absence verification: re-check reality every run, never assume prior steps
    # worked. Both signals: sysfs needs neither ethtool nor root and is what an operator checks
    # by hand; ethtool is the fallback for a driver with no power/wakeup attribute. Under
    # --dry-run no mutation actually happened, so a pre-existing gap can't have been closed; in
    # that case downgrade to a warning instead of sys.exit so dry-run stays side-effect-free.
    verify_flags = _read_wake_flags(iface, ethtool_path)
    verify_wakeup = read_wakeup_flag(iface)
    verify_enabled = _is_enabled(unit_name)
    armed_now = (verify_wakeup == "enabled") if verify_wakeup is not None else (bool(verify_flags) and "g" in verify_flags)
    problems = []
    if not armed_now:
        problems.append(f"{iface!r} does not report armed (power/wakeup: {verify_wakeup!r}, ethtool: {verify_flags!r})")
    if not verify_enabled:
        problems.append(f"{unit_name} is not enabled")

    if problems:
        # Report what THIS RUN changed, not "is ethtool installed?" — the operator who knows
        # the link was just cycled and TLP restarted can act; one told to check a package
        # cannot. See §0e, defect (ii): blaming a missing package after already disturbing the
        # link was the original failure mode.
        changes = []
        if tlp_mutated:
            changes.append(f"wrote {_TLP_WOL_CONF} and ran `tlp start`")
        if nm_mutated:
            changes.append("modified the NetworkManager connection's wake-on-lan setting and ran `nmcli con up` (cycled the link)")
        changes.append(f"ran `ethtool -s {iface} wol g`")
        changes_text = "; ".join(changes)
        message = (
            "wol: verification FAILED — " + "; ".join(problems) +
            f". This run changed: {changes_text}."
        )
        if runner.dry_run:
            log.warning("%s (expected under --dry-run: no mutation actually ran)", message)
        else:
            sys.exit(message)
    else:
        log.info("wol: verified — %s has magic-packet wake enabled and %s is enabled for persistence", iface, unit_name)
