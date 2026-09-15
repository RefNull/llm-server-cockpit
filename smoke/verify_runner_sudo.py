#!/usr/bin/env python3
"""Verifies Runner's `sudo=True` branches against a stub `sudo` on PATH.

Every mutating action goes through Runner, so elevation has to be threaded through *all* of
it — a Runner that only sudo-wraps `run()` half-succeeds: subprocesses go as root while
Python's own file writes still fail with EACCES, which is worse than failing outright.

Run: .venv/bin/python smoke/verify_runner_sudo.py
"""
from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import bootstrap  # noqa: E402

bootstrap.add_venv_site_packages(_REPO_ROOT)

from provision.common import Runner  # noqa: E402

# Logs each invocation on one line (newlines inside an argument become "~") and then execs the
# real command, so the assertions below check both what was elevated and that it worked.
_STUB_SUDO = """#!/bin/sh
{{ printf "%s " "$@" | tr "\\n" "~"; printf "\\n"; }} >> {log}
[ "$1" = "-n" ] && shift
exec "$@"
"""


def main() -> None:
    bindir = pathlib.Path(tempfile.mkdtemp())
    work = pathlib.Path(tempfile.mkdtemp())
    log = bindir / "calls.log"
    try:
        (bindir / "sudo").write_text(_STUB_SUDO.format(log=log))
        (bindir / "sudo").chmod(0o755)
        os.environ["PATH"] = f"{bindir}{os.pathsep}{os.environ['PATH']}"

        runner = Runner(sudo=True)

        # write_file: creates missing parents, honours the mode, content survives intact.
        unit = work / "deep" / "nested" / "unit.service"
        runner.write_file(unit, "[Unit]\nDescription=x\n")
        assert unit.read_text() == "[Unit]\nDescription=x\n", unit.read_text()
        assert oct(unit.stat().st_mode)[-4:] == "0644", oct(unit.stat().st_mode)
        runner.write_file(work / "exec.sh", "#!/bin/sh\n", mode=0o755)
        assert oct((work / "exec.sh").stat().st_mode)[-4:] == "0755"

        runner.mkdir(work / "a" / "b")
        assert (work / "a" / "b").is_dir()

        # atomic_symlink: repoints an existing link and leaves no temp behind.
        (work / "v1").mkdir()
        (work / "v2").mkdir()
        runner.atomic_symlink(work / "current", work / "v1")
        assert (work / "current").resolve() == (work / "v1").resolve()
        runner.atomic_symlink(work / "current", work / "v2")
        assert (work / "current").resolve() == (work / "v2").resolve()
        assert not list(work.glob(".current.tmp-*")), "temp symlink left behind"

        runner.run(["touch", str(work / "ran")])
        assert (work / "ran").exists()
        runner.shell(f"echo hi > {work / 'shelled'}")
        assert (work / "shelled").read_text().strip() == "hi"

        calls = log.read_text().splitlines()
        assert all(c.startswith("-n ") for c in calls), f"not every call was `sudo -n`: {calls}"
        for expected in ("install -m 0644", "install -m 0755", "mkdir -p -m 0755", "os.replace", "bash -c"):
            assert any(expected in c for c in calls), f"no elevated call matched {expected!r}: {calls}"
        print(f"sudo=True: {len(calls)} elevated commands, all via `sudo -n`")

        # The default path must be untouched — sudo is never invoked without the flag.
        before = len(log.read_text().splitlines())
        plain = Runner()
        plain.write_file(work / "plain.txt", "x")
        plain.mkdir(work / "plaindir")
        plain.run(["true"])
        plain.atomic_symlink(work / "plainlink", work / "v1")
        assert len(log.read_text().splitlines()) == before, "sudo was invoked with sudo=False"
        print("sudo=False: sudo invoked zero times")
    finally:
        shutil.rmtree(bindir, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)

    print("Runner sudo verification PASSED.")


if __name__ == "__main__":
    main()
