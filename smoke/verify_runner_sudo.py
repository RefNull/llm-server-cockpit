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
import subprocess
import sys
import tempfile
import time

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import bootstrap  # noqa: E402

bootstrap.add_venv_site_packages(_REPO_ROOT)

import provision.common as common  # noqa: E402
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

        # env and unset_env under sudo=True propagate via env in the elevated argv
        env_res = runner.run(["sh", "-c", 'printf "%s" "$TEST_SUDO_ENV"'], env={"TEST_SUDO_ENV": "active"}, capture=True)
        assert env_res is not None and env_res.stdout == "active", f"env under sudo failed: {env_res}"
        os.environ["TEST_BAD_ENV"] = "bad"
        unset_res = runner.run(["sh", "-c", 'printf "%s" "$TEST_BAD_ENV"'], unset_env=["TEST_BAD_ENV"], capture=True)
        assert unset_res is not None and unset_res.stdout == "", f"unset_env under sudo failed: {unset_res}"
        os.environ.pop("TEST_BAD_ENV", None)

        calls = log.read_text().splitlines()
        assert all(c.startswith("-n ") for c in calls), f"not every call was `sudo -n`: {calls}"
        for expected in ("install -m 0644", "install -m 0755", "mkdir -p -m 0755", "os.replace", "bash -c", "env TEST_SUDO_ENV=active", "env -u TEST_BAD_ENV"):
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

        # env under sudo=False must MERGE onto the inherited environment, not replace it —
        # a direct-exec cmake call (provision/steps/build.py's non-source_script path) passes
        # only the 1-2 keys it cares about (e.g. CUDACXX) and still needs HOME/PATH/etc. to
        # reach the child. Regression coverage for the merge dropped in 220827f.
        os.environ["MARKER_VAR"] = "should-survive"
        try:
            merge_res = plain.run(
                ["sh", "-c", 'printf "%s|%s" "$HOME" "$OVERRIDE_VAR"'],
                env={"OVERRIDE_VAR": "set"},
                capture=True,
            )
            home, override = merge_res.stdout.split("|")
            assert home == os.environ.get("HOME", ""), f"HOME not inherited under sudo=False env=: {merge_res.stdout!r}"
            assert override == "set", f"explicit env override lost: {merge_res.stdout!r}"

            marker_res = plain.run(["sh", "-c", 'printf "%s" "$MARKER_VAR"'], env={"OVERRIDE_VAR": "set"}, capture=True)
            assert marker_res.stdout == "should-survive", f"unrelated inherited var lost: {marker_res.stdout!r}"

            unset_res2 = plain.run(["sh", "-c", 'printf "%s" "$MARKER_VAR"'], unset_env=["MARKER_VAR"], capture=True)
            assert unset_res2.stdout == "", f"unset_env under sudo=False failed: {unset_res2.stdout!r}"
        finally:
            os.environ.pop("MARKER_VAR", None)
        print("sudo=False: env= merges onto inherited environment, unset_env still removes")

        # on_output: streaming, not capture-then-dump. A script that prints, sleeps, prints
        # must deliver each line to the callback as it happens, not all at once at the end.
        script = work / "stream.sh"
        script.write_text("#!/bin/sh\necho one\nsleep 0.3\necho two\nsleep 0.3\necho three\n")
        script.chmod(0o755)
        t0 = time.monotonic()
        arrivals: list[float] = []
        lines: list[str] = []

        def _collector(line: str) -> None:
            arrivals.append(time.monotonic() - t0)
            lines.append(line)

        Runner(on_output=_collector).run(["sh", str(script)])
        assert lines == ["one", "two", "three"], lines
        elapsed = arrivals[-1] - arrivals[0]
        # Two 0.3s sleeps separate the three callbacks; tolerate scheduling noise rather than
        # asserting an exact threshold — the point is "incremental", not "exactly 0.6s".
        assert elapsed >= 0.8 * 0.6, f"lines did not arrive incrementally (elapsed={elapsed:.3f}s): {arrivals}"
        print(f"on_output: {len(lines)} lines delivered incrementally over {elapsed:.2f}s")

        # on_output=None must leave the default (uncaptured) path byte-identical: stdout/stderr
        # still None on the subprocess.run call, not silently redirected.
        captured_kwargs: dict = {}
        real_run = subprocess.run

        def _spy_run(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return real_run(*args, **kwargs)

        common.subprocess.run = _spy_run
        try:
            Runner().run(["true"])
        finally:
            common.subprocess.run = real_run
        assert captured_kwargs.get("stdout") is None, captured_kwargs
        assert captured_kwargs.get("stderr") is None, captured_kwargs
        print("on_output=None: stdout/stderr left as None (default path unchanged)")

        # Runner.shell honours on_output too (the sycl/source_script path is otherwise
        # structurally uncapturable — provision/steps/build.py's source_script branch).
        shell_lines: list[str] = []
        Runner(on_output=shell_lines.append).shell("echo a; echo b")
        assert shell_lines == ["a", "b"], shell_lines
        print("Runner.shell: on_output honoured")

        # A non-zero exit must still raise CalledProcessError under check=True on the streaming
        # path — an error path that silently swallows a failed build is worse than the leak
        # this whole change fixes.
        try:
            Runner(on_output=lambda _line: None).run(["sh", "-c", "echo failing; exit 3"])
        except subprocess.CalledProcessError as e:
            assert e.returncode == 3, e.returncode
        else:
            raise AssertionError("expected CalledProcessError from the streaming path on non-zero exit")
        print("on_output streaming: non-zero exit still raises CalledProcessError under check=True")
    finally:
        shutil.rmtree(bindir, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)

    print("Runner sudo verification PASSED.")


if __name__ == "__main__":
    main()
