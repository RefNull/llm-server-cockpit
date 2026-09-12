"""Shared execution primitives: dry-run gating, idempotency helpers, atomic swap.

Every mutating action goes through Runner so --dry-run is threaded by
construction, not by remembering to check a flag at each call site.
"""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Sequence

log = logging.getLogger("provision")


class Runner:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

    def _announce(self, action: str) -> None:
        log.info("%s%s", "[dry-run] " if self.dry_run else "", action)

    def run(
        self,
        cmd: Sequence[str],
        *,
        check: bool = True,
        env: dict | None = None,
        cwd: str | Path | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess | None:
        self._announce(f"run: {' '.join(str(c) for c in cmd)}" + (f"  (cwd={cwd})" if cwd else ""))
        if self.dry_run:
            return None
        return subprocess.run(
            cmd,
            check=check,
            env=env,
            cwd=cwd,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
            text=True,
        )

    def shell(self, script: str, *, check: bool = True, cwd: str | Path | None = None) -> subprocess.CompletedProcess | None:
        """For steps that must `source` a vendor env script (e.g. oneAPI setvars.sh) before a build."""
        self._announce(f"shell: {script}" + (f"  (cwd={cwd})" if cwd else ""))
        if self.dry_run:
            return None
        return subprocess.run(["bash", "-c", script], check=check, cwd=cwd)

    def write_file(self, path: str | Path, content: str, *, mode: int | None = None) -> None:
        path = Path(path)
        self._announce(f"write file: {path} ({len(content)} bytes)")
        if self.dry_run:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if mode is not None:
            path.chmod(mode)

    def mkdir(self, path: str | Path, *, mode: int = 0o755) -> None:
        path = Path(path)
        if path.exists():
            return
        self._announce(f"mkdir -p: {path}")
        if self.dry_run:
            return
        path.mkdir(parents=True, mode=mode, exist_ok=True)

    def atomic_symlink(self, link_path: str | Path, target_path: str | Path) -> None:
        """Point link_path at target_path with no window where it points nowhere or at a partial build."""
        link_path = Path(link_path)
        target_path = Path(target_path)
        self._announce(f"atomically point {link_path} -> {target_path}")
        if self.dry_run:
            return
        tmp = link_path.with_name(f".{link_path.name}.tmp-{os.getpid()}")
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        tmp.symlink_to(target_path)
        os.replace(tmp, link_path)

    def apt_install(self, packages: Sequence[str]) -> None:
        if not packages:
            return
        missing = [p for p in packages if not _apt_package_installed(p)]
        if not missing:
            log.info("apt packages already present: %s", ", ".join(packages))
            return
        self._announce(f"apt-get install -y {' '.join(missing)}")
        if self.dry_run:
            return
        subprocess.run(["apt-get", "install", "-y", *missing], check=True)


def _apt_package_installed(name: str) -> bool:
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${Status}", name],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return result.returncode == 0 and "install ok installed" in result.stdout


def resolve_env_value(value: str) -> str:
    """manifest.yaml build_env values may be shell command substitutions (e.g. "$(hipconfig -l)/clang")."""
    if "$(" not in value:
        return value
    result = subprocess.run(["sh", "-c", f"echo -n {shlex.quote(value)}"], stdout=subprocess.PIPE, check=True, text=True)
    return result.stdout


def require_root() -> None:
    if os.geteuid() != 0:
        sys.exit("this step must be run as root (sudo) — it installs apt packages and writes under /opt, /etc, /var/lib")
