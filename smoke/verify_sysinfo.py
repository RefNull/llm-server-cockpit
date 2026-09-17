#!/usr/bin/env python3
"""Verifies provision/steps/sysinfo.py's readers against fixture /proc and /sys trees, and a
stubbed lspci on PATH — the dev box is macOS, so none of the real Linux paths exist here.

Run: .venv/bin/python smoke/verify_sysinfo.py
"""
from __future__ import annotations

import os
import pathlib
import shutil
import stat
import sys
import tempfile

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import bootstrap  # noqa: E402

bootstrap.add_venv_site_packages(_REPO_ROOT)

from provision.steps import sysinfo  # noqa: E402

_LSPCI_STUB_OK = """#!/bin/sh
cat <<'EOF'
00:02.0 VGA compatible controller: NVIDIA Corporation GA102 [GeForce RTX 3090] (rev a1)
00:1f.3 Audio device: Intel Corporation Device abcd
02:00.0 3D controller: NVIDIA Corporation GA104M [GeForce RTX 3070 Mobile]
EOF
"""

_LSPCI_STUB_FAIL = """#!/bin/sh
exit 1
"""


def _write_stub(bindir: pathlib.Path, content: str) -> None:
    path = bindir / "lspci"
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _check_board() -> None:
    fixture = pathlib.Path(tempfile.mkdtemp())
    real_sys = sysinfo._SYS
    try:
        dmi = fixture / "class" / "dmi" / "id"
        dmi.mkdir(parents=True)
        (dmi / "product_name").write_text("ASUS System Product Name (PRIME Z790-P)\n")
        (dmi / "sys_vendor").write_text("ASUSTeK COMPUTER INC.\n")
        sysinfo._SYS = fixture
        board = sysinfo.read_board()
        assert board == {
            "model": "ASUS System Product Name (PRIME Z790-P)",
            "vendor": "ASUSTeK COMPUTER INC.",
        }, board
        print("read_board(): DMI present — PASSED")

        empty = pathlib.Path(tempfile.mkdtemp())
        sysinfo._SYS = empty
        board = sysinfo.read_board()
        assert board == {"model": None, "vendor": None}, board
        print("read_board(): DMI absent — PASSED")
        shutil.rmtree(empty, ignore_errors=True)
    finally:
        sysinfo._SYS = real_sys
        shutil.rmtree(fixture, ignore_errors=True)


def _check_cpu() -> None:
    fixture = pathlib.Path(tempfile.mkdtemp())
    real_proc = sysinfo._PROC
    try:
        (fixture / "cpuinfo").write_text(
            "processor\t: 0\n"
            "vendor_id\t: GenuineIntel\n"
            "model name\t: 13th Gen Intel(R) Core(TM) i9-13900K\n"
            "cpu MHz\t\t: 3000.000\n"
        )
        sysinfo._PROC = fixture
        cpu = sysinfo.read_cpu()
        assert cpu["model"] == "13th Gen Intel(R) Core(TM) i9-13900K", cpu
        assert cpu["cores"] == os.cpu_count(), cpu
        print("read_cpu(): model name present — PASSED")

        (fixture / "cpuinfo").write_text("processor\t: 0\nvendor_id\t: GenuineIntel\n")
        cpu = sysinfo.read_cpu()
        assert cpu["model"] is None, cpu
        print("read_cpu(): model name absent — PASSED")
    finally:
        sysinfo._PROC = real_proc
        shutil.rmtree(fixture, ignore_errors=True)


def _check_memory() -> None:
    fixture = pathlib.Path(tempfile.mkdtemp())
    real_proc = sysinfo._PROC
    try:
        (fixture / "meminfo").write_text("MemFree:         100000 kB\nBuffers:           2048 kB\n")
        sysinfo._PROC = fixture
        assert sysinfo.read_memory_total() is None, sysinfo.read_memory_total()
        print("read_memory_total(): no MemTotal — PASSED")

        (fixture / "meminfo").write_text("MemTotal:       65850000 kB\nMemFree:         100000 kB\n")
        assert sysinfo.read_memory_total() == 65850000 * 1024
        print("read_memory_total(): MemTotal present — PASSED")
    finally:
        sysinfo._PROC = real_proc
        shutil.rmtree(fixture, ignore_errors=True)


def _check_pci_gpus() -> None:
    bindir = pathlib.Path(tempfile.mkdtemp())
    real_path = os.environ.get("PATH", "")
    try:
        _write_stub(bindir, _LSPCI_STUB_OK)
        os.environ["PATH"] = f"{bindir}{os.pathsep}{real_path}"
        gpus = sysinfo.read_pci_gpus()
        assert gpus == [
            "NVIDIA Corporation GA102 [GeForce RTX 3090]",
            "NVIDIA Corporation GA104M [GeForce RTX 3070 Mobile]",
        ], gpus
        print("read_pci_gpus(): VGA + 3D controller, rev suffix stripped, audio skipped — PASSED")

        _write_stub(bindir, _LSPCI_STUB_FAIL)
        assert sysinfo.read_pci_gpus() == [], sysinfo.read_pci_gpus()
        print("read_pci_gpus(): non-zero exit -> [] — PASSED")
    finally:
        os.environ["PATH"] = real_path
        shutil.rmtree(bindir, ignore_errors=True)


def check_pci_gpus_domain_qualified() -> None:
    """lspci prints a domain prefix ("0000:00:02.0") on a host with more than one PCI domain.

    The first implementation split on the Nth colon, so a domain prefix shifted the count and
    returned "00.0 VGA compatible controller: NVIDIA ..." as the product name. Flagged by the
    implementer as an unverified risk; confirmed real, then fixed by anchoring on the class
    name. This is the case that would have shipped broken.
    """
    stub = pathlib.Path(tempfile.mkdtemp())
    try:
        (stub / "lspci").write_text(
            "#!/bin/sh\n"
            "echo '0000:01:00.0 VGA compatible controller: NVIDIA Corporation GA102 [GeForce RTX 3090] (rev a1)'\n"
            "echo '0000:02:00.0 3D controller: Intel Corporation DG2 [Arc A770]'\n"
            "echo '0000:01:00.1 Audio device: NVIDIA Corporation GA102 High Definition Audio'\n"
        )
        (stub / "lspci").chmod(0o755)
        original = os.environ["PATH"]
        os.environ["PATH"] = f"{stub}{os.pathsep}{original}"
        try:
            got = sysinfo.read_pci_gpus()
        finally:
            os.environ["PATH"] = original
        assert got == [
            "NVIDIA Corporation GA102 [GeForce RTX 3090]",
            "Intel Corporation DG2 [Arc A770]",
        ], f"domain-qualified lspci mis-parsed: {got}"
        print("read_pci_gpus(): domain-qualified slots parsed, audio skipped — PASSED")
    finally:
        shutil.rmtree(stub, ignore_errors=True)


def check_cpu_amd_core_suffix() -> None:
    """AMD's model string carries the physical core count; the Dashboard already prints the
    thread count beside it. Real string from llm-host, 2026-09-16."""
    root = pathlib.Path(tempfile.mkdtemp())
    try:
        (root / "cpuinfo").write_text(
            "processor\t: 0\nvendor_id\t: AuthenticAMD\n"
            "model name\t: AMD Ryzen 7 7800X3D 8-Core Processor\n"
        )
        original, sysinfo._PROC = sysinfo._PROC, root
        try:
            got = sysinfo.read_cpu()["model"]
        finally:
            sysinfo._PROC = original
        assert got == "AMD Ryzen 7 7800X3D 8-Core Processor".replace(" 8-Core Processor", ""), got
        print(f"read_cpu(): AMD core-count suffix stripped -> {got!r} — PASSED")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_wol_detects_foreign_unit() -> None:
    """WOL armed by a unit this toolkit did not install must still read as armed.

    The operator's host arms enp6s0 from a hand-written `wol.service`; status() only ever
    checked `wol-<iface>.service`, so a working setup reported "persistence unit enabled: no".
    The sysfs wake flag needs neither ethtool nor root nor a particular unit name, which is why
    it is now the primary signal.
    """
    from provision.steps import wol

    root = pathlib.Path(tempfile.mkdtemp())
    try:
        power = root / "enp6s0" / "device" / "power"
        power.mkdir(parents=True)
        (power / "wakeup").write_text("enabled\n")
        original, wol._SYSFS_NET = wol._SYSFS_NET, root
        try:
            assert wol.read_wakeup_flag("enp6s0") == "enabled", wol.read_wakeup_flag("enp6s0")
            assert wol.read_wakeup_flag("missing0") is None
            (power / "wakeup").write_text("disabled\n")
            assert wol.read_wakeup_flag("enp6s0") == "disabled"
        finally:
            wol._SYSFS_NET = original
        print("read_wakeup_flag(): armed/disabled/absent — PASSED")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_wol_tlp_precedence() -> None:
    """`_tlp_effective_wol_disable()` must resolve the winning file, not just 01-wol.conf.

    This toolkit's own drop-in (`/etc/tlp.d/01-wol.conf`) sorts early by design (it matches the
    operator's own hand-written repair — plans/05-qa-remediation-pass.md §0e/Phase 4). A
    later-sorting drop-in setting the same key silently wins over it; the detection signal must
    say so by naming the winning file, not just report a boolean.

    NOTE: TLP's load order (main conf, then /etc/tlp.d/*.conf lexically, later wins) is an
    assumption documented in wol.py — `tlp` is not installed on this dev box, so it could not
    be verified against TLP's own source here (see the ASSUMPTION comment on
    `_tlp_effective_wol_disable`). This test locks in that assumed behaviour, not TLP's actual
    behaviour on a real host.
    """
    from provision.steps import wol

    root = pathlib.Path(tempfile.mkdtemp())
    try:
        main_conf = root / "tlp.conf"
        dropin_dir = root / "tlp.d"
        dropin_dir.mkdir()
        original_main, original_dir = wol._TLP_MAIN_CONF, wol._TLP_DROPIN_DIR
        wol._TLP_MAIN_CONF, wol._TLP_DROPIN_DIR = main_conf, dropin_dir
        try:
            # Nothing set anywhere: TLP's own undocumented-here default (Y) applies.
            value, source = wol._tlp_effective_wol_disable()
            assert (value, source) == (None, None), f"expected no file to win, got {(value, source)}"

            # This toolkit's own fix: 01-wol.conf sets N, nothing else present.
            (dropin_dir / "01-wol.conf").write_text("WOL_DISABLE=N\n")
            value, source = wol._tlp_effective_wol_disable()
            assert value == "N" and source == dropin_dir / "01-wol.conf", (
                f"01-wol.conf alone should win: got {(value, source)}"
            )

            # A later-sorting drop-in re-disables it — the exact silent-failure this signal
            # exists to catch: writing 01-wol.conf did not actually fix anything.
            (dropin_dir / "50-power.conf").write_text("WOL_DISABLE=Y\n")
            value, source = wol._tlp_effective_wol_disable()
            assert value == "Y" and source == dropin_dir / "50-power.conf", (
                f"50-power.conf sorts after 01-wol.conf and must win: got {(value, source)}"
            )

            # `/etc/tlp.conf` is read LAST by TLP (https://linrunner.de/tlp/settings/
            # introduction.html: "parameters in /etc/tlp.conf will override anything else
            # because it is read last") — it beats every drop-in regardless of lexical name,
            # including this toolkit's own 01-wol.conf. This is the regression case: an
            # earlier version of this helper read tlp.conf FIRST, so on a host with
            # WOL_DISABLE=Y in tlp.conf it reported the drop-in's "N" as effective while TLP
            # actually applied "Y" — the exact defect this fix exists to close, reconstructed
            # inside the fix.
            main_conf.write_text("WOL_DISABLE=Y\n")
            value, source = wol._tlp_effective_wol_disable()
            assert value == "Y" and source == main_conf, (
                f"/etc/tlp.conf must win over every drop-in (TLP reads it last): got {(value, source)}"
            )
        finally:
            wol._TLP_MAIN_CONF, wol._TLP_DROPIN_DIR = original_main, original_dir
        print("_tlp_effective_wol_disable(): later-sorting drop-in wins, tlp.conf wins over all drop-ins — PASSED")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_build_detects_foreign_build() -> None:
    """A hand-built llama.cpp outside `paths.prefix_root` must be reported, read-only; a
    managed build under `prefix_root` — including the `current` symlink resolved through —
    must not be.

    Precedent: check_wol_detects_foreign_unit() above (monkeypatch the module's fixed lookup
    location, same as build_step._FOREIGN_BUILD_ROOTS here, since a hermetic test cannot
    create files under the real /opt on the machine running it — see the docstring on
    _FOREIGN_BUILD_ROOTS in provision/steps/build.py).
    """
    from provision.steps import build as build_step

    root = pathlib.Path(tempfile.mkdtemp())
    try:
        # Foreign: upstream's own in-tree cmake layout, unmanaged by this toolkit.
        foreign_bin = root / "opt-llama-cpp" / "build-vulkan" / "bin"
        foreign_bin.mkdir(parents=True)
        foreign_server = foreign_bin / "llama-server"
        foreign_server.write_text("#!/bin/sh\n")
        foreign_server.chmod(foreign_server.stat().st_mode | stat.S_IEXEC)

        # Managed: prefix_root/<backend>/<ref>, activated via the `current` symlink — the
        # shape a provisioned host actually has, and the shape shutil.which("llama-server")
        # legitimately resolves to on one.
        prefix_root = root / "prefix"
        managed = prefix_root / "vulkan" / "abc123"
        managed_bin = managed / "bin"
        managed_bin.mkdir(parents=True)
        managed_server = managed_bin / "llama-server"
        managed_server.write_text("#!/bin/sh\n")
        managed_server.chmod(managed_server.stat().st_mode | stat.S_IEXEC)
        current_link = prefix_root / "vulkan" / "current"
        current_link.symlink_to(managed, target_is_directory=True)

        host_profile = {"paths": {"prefix_root": str(prefix_root)}}

        original_roots = build_step._FOREIGN_BUILD_ROOTS
        original_which = build_step.shutil.which
        # The `current` symlink is exactly what a real shutil.which("llama-server") resolves
        # to on a provisioned host (build.py:296) — route it through the same exclusion path
        # a bare os.environ["PATH"] lookup would.
        build_step.shutil.which = lambda name: str(current_link / "bin" / "llama-server")
        try:
            build_step._FOREIGN_BUILD_ROOTS = (root / "opt-llama-cpp", current_link)

            found = build_step.find_foreign_builds(host_profile)
            paths_found = {f["path"] for f in found}
            assert str(foreign_bin.parent.resolve()) in paths_found, found
            match = next(f for f in found if f["path"] == str(foreign_bin.parent.resolve()))
            assert match["sane"] is True, match
            assert str(managed.resolve()) not in paths_found, (
                f"a managed build under prefix_root must never be reported as foreign: {found}"
            )
            assert str(current_link.resolve()) not in paths_found, (
                f"the current symlink (and shutil.which resolving to it) must be excluded: {found}"
            )
            assert len(found) == 1, f"expected exactly the one foreign build, got {found}"
            print("find_foreign_builds(): foreign build reported, managed/current excluded — PASSED")
        finally:
            build_step._FOREIGN_BUILD_ROOTS = original_roots
            build_step.shutil.which = original_which
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> None:
    _check_board()
    _check_cpu()
    check_cpu_amd_core_suffix()
    check_wol_detects_foreign_unit()
    check_wol_tlp_precedence()
    check_build_detects_foreign_build()
    _check_memory()
    _check_pci_gpus()
    check_pci_gpus_domain_qualified()

    # read_all() must never raise, on any host — including this one.
    result = sysinfo.read_all()
    assert set(result) == {"os", "cpu", "memory_total_bytes", "board", "pci_gpus"}, result
    print("read_all(): shape intact — PASSED")

    print("sysinfo verification PASSED.")


if __name__ == "__main__":
    main()
