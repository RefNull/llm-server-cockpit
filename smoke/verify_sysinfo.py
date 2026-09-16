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


def main() -> None:
    _check_board()
    _check_cpu()
    check_cpu_amd_core_suffix()
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
