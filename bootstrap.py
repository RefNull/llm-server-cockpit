"""Shared by bin/provision, bin/cockpit, bin/check-updates, and cockpit/__init__.py.

Adds .venv's site-packages to sys.path directly rather than re-exec'ing into
.venv/bin/python3 — that file can be a plain symlink to the system python3 (confirmed on a
real deployment target), in which case re-exec launches the exact same interpreter binary and
Python's own venv auto-detection doesn't reliably kick in (Debian's Python packaging patches
this machinery). Adding the path directly needs none of that: it's the one thing a venv
actually provides, regardless of whether this interpreter was invoked through
.venv/bin/python3 or system python3.
"""
from __future__ import annotations

import sys
from pathlib import Path


def add_venv_site_packages(repo_root: Path) -> None:
    lib_dir = repo_root / ".venv" / "lib"
    if not lib_dir.is_dir():
        return
    for py_dir in sorted(lib_dir.glob("python3.*"), reverse=True):
        site_packages = py_dir / "site-packages"
        if site_packages.is_dir() and str(site_packages) not in sys.path:
            sys.path.insert(0, str(site_packages))
            return
