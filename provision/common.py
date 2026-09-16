"""Shared execution primitives: dry-run gating, privilege escalation, idempotency helpers,
atomic swap.

Every mutating action goes through Runner so --dry-run is threaded by
construction, not by remembering to check a flag at each call site. `sudo` is
threaded the same way and for the same reason: the CLI requires root up front
(cli.py's require_root), but the cockpit is normally launched unprivileged and
elevates per action, so *every* mutation has to honour the flag or a step
half-succeeds — subprocesses run as root while Python's own file writes still
fail with EACCES.
"""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

log = logging.getLogger("provision")


_ATOMIC_SYMLINK = (
    "import os, sys\n"
    "target, tmp, link = sys.argv[1:4]\n"
    "if os.path.islink(tmp) or os.path.exists(tmp):\n"
    "    os.unlink(tmp)\n"
    "os.symlink(target, tmp)\n"
    "os.replace(tmp, link)\n"
)
"""Runner.atomic_symlink's sudo branch, as a script rather than a shell one-liner."""


class Runner:
    def __init__(self, dry_run: bool = False, sudo: bool = False):
        self.dry_run = dry_run
        # Set by the cockpit once the operator has authenticated (see
        # cockpit.widgets.ensure_root). `-n` throughout: this never prompts for a password
        # from inside a running TUI — the credential is cached before the flag is set, and an
        # expired cache fails loudly rather than blocking on a hidden prompt.
        self.sudo = sudo

    def _announce(self, action: str) -> None:
        log.info("%s%s%s", "[dry-run] " if self.dry_run else "", "[sudo] " if self.sudo else "", action)

    def _elevate(self, cmd: Sequence[str]) -> list[str]:
        return ["sudo", "-n", *(str(c) for c in cmd)] if self.sudo else [str(c) for c in cmd]

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
            self._elevate(cmd),
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
        return subprocess.run(self._elevate(["bash", "-c", script]), check=check, cwd=cwd)

    def write_file(self, path: str | Path, content: str, *, mode: int | None = None) -> None:
        path = Path(path)
        self._announce(f"write file: {path} ({len(content)} bytes)")
        if self.dry_run:
            return
        if self.sudo:
            # Staged through a temp file and installed, rather than `sudo tee`: the content
            # never has to survive a shell. `mkdir -p` + `install -m` rather than GNU's
            # `install -D`, which BSD install spells differently — the deployment target is
            # Debian, but a portable pair is one that can actually be tested off it.
            # 0644 is what write_text() below produces under a normal umask, so an unspecified
            # mode means the same thing on both paths.
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
                tmp.write(content)
                staged = tmp.name
            try:
                self.run(["mkdir", "-p", str(path.parent)])
                self.run(["install", "-m", format(mode if mode is not None else 0o644, "04o"), staged, str(path)])
            finally:
                os.unlink(staged)
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
        if self.sudo:
            self.run(["mkdir", "-p", "-m", format(mode, "04o"), str(path)])
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
        if self.sudo:
            # The identical create-then-rename, run as root. Not `ln -sfn`: `-f` unlinks
            # before it symlinks, reopening exactly the window this method exists to close.
            # Not `mv -T` either — that spelling is GNU-only. python3 is by definition present
            # (it is running this), and os.replace is the same rename(2) the branch below uses.
            self.run(["python3", "-c", _ATOMIC_SYMLINK, str(target_path), str(tmp), str(link_path)])
            return
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
        subprocess.run(self._elevate(["apt-get", "install", "-y", *missing]), check=True)


def unit_value(value: str) -> str:
    """Escape a value interpolated into ANY systemd unit setting.

    systemd expands `%` specifiers everywhere in a unit file, so a literal `%` must be doubled.
    Unescaped, `WorkingDirectory=/opt/%HOSTDIR` renders as `/opt/<hostname>OSTDIR` — `%H` is the
    hostname specifier. A value containing no `%` is returned unchanged.
    """
    return value.replace("%", "%%")


def unit_command(value: str) -> str:
    """Escape a value interpolated into a systemd COMMAND setting (ExecStart=, ExecStartPre=).

    Command lines additionally expand `$VAR` / `${VAR}` from the service environment, so a
    literal `$` must be doubled too. Unescaped, an argument of `$HOME/data` reached the process
    as `/data`. Only for command settings: `$` is literal in `Description=` and
    `WorkingDirectory=`, where doubling it would render a stray second `$`.

    Never apply either helper to llama-swap's config.yaml — `${PORT}` there is a llama-swap
    placeholder that must survive verbatim (provision/steps/swap.py::_build_model_entry).
    """
    return unit_value(value).replace("$", "$$")


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
