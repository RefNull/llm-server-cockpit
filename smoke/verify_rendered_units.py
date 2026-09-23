#!/usr/bin/env python3
"""Render every generated artifact and assert on the OUTPUT, not on the code that wrote it.

This repo generates six systemd units from `systemd/*.tmpl` plus llama-swap's `config.yaml`,
and until this script existed nothing checked any of them. That is a real gap, not a
theoretical one: a malformed unit passes `py_compile`, passes pyflakes, passes every screen
check, and fails at `systemctl daemon-reload` on the server — or worse, loads fine and does
something subtly different from what the operator configured.

The approach is borrowed from a sibling project (`PiDeployment/tests/render-check.sh`), which
exists because a stray `\\$(` in a rendered WireGuard `PostUp` passed `bash -n` and then took a
whole stack down at runtime. Its lesson generalises: **render the artifact, then check the
artifact the way its consumer will read it.**

What that catches here, demonstrated by the escaping case below: systemd expands `%` specifiers
in every unit setting and `$VAR` in command settings, so a path or argument containing either
character silently rendered a unit that did something else — `WorkingDirectory=/opt/%HOSTDIR`
became `/opt/<hostname>OSTDIR`, and an argument of `$HOME/data` reached the process as `/data`.

Run: .venv/bin/python smoke/verify_rendered_units.py
"""
from __future__ import annotations

import configparser
import pathlib
import shlex
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import bootstrap  # noqa: E402

bootstrap.add_venv_site_packages(_REPO_ROOT)

import yaml  # noqa: E402

from provision import schema  # noqa: E402
from provision.common import Runner  # noqa: E402
from provision.steps import scripts as scripts_step  # noqa: E402
from provision.steps import swap  # noqa: E402
from provision.steps import wol  # noqa: E402


class CapturingRunner(Runner):
    """Records what would have been written instead of touching the filesystem.

    Not `dry_run=True`: that short-circuits write_file before the content exists, and the
    content is the whole point. This keeps the real install/sync code paths running.
    """

    def __init__(self) -> None:
        super().__init__()
        self.written: dict[str, str] = {}
        # Not just "nothing to do here" any more: a regression test needs to observe that a
        # guard genuinely prevented a mutation, not merely that this stub swallowed one.
        self.run_calls: list[list[str]] = []

    def write_file(self, path, content: str, *, mode: int | None = None) -> None:
        self.written[pathlib.Path(path).name] = content

    def run(self, cmd, **kwargs):  # systemctl daemon-reload / enable — nothing to do here
        self.run_calls.append(list(cmd))
        return None


def parse_unit(name: str, text: str) -> configparser.ConfigParser:
    """Parse as systemd would: INI-ish, `#` comments, `=` only, duplicate keys allowed.

    `strict=False` because a unit legitimately repeats keys (two ExecStartPre= lines); this is
    a structural check that the file is loadable at all, not a semantic one.
    """
    parser = configparser.ConfigParser(strict=False, allow_no_value=True, delimiters=("=",), interpolation=None)
    parser.optionxform = str
    try:
        parser.read_string(text)
    except configparser.Error as e:
        raise AssertionError(f"{name}: does not parse as a unit file — {e}\n---\n{text}") from None
    return parser


def assert_no_unexpanded_placeholder(name: str, text: str) -> None:
    """`string.Template.substitute` raises on a missing key, so a bare `$name` surviving into
    the output means a template placeholder was renamed without its call site — or that a value
    carried a `$` that should have been escaped."""
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # `$$` is the correct escape for a literal; anything else is an unexpanded placeholder.
        for token in line.replace("$$", "").split("$")[1:]:
            raise AssertionError(
                f"{name}:{lineno}: unexpanded placeholder `${token.split()[0] if token.split() else ''}` "
                f"in rendered output:\n  {line}"
            )


def check_systemd_units() -> None:
    host_profile = schema.load_host_profile(_REPO_ROOT / "hosts" / "example.yaml")
    host_profile.setdefault("service", {})["scheduled_restart"] = {"enabled": True, "on_calendar": "daily"}
    host_profile["update_check"] = {"enabled": True, "on_calendar": "weekly"}

    runner = CapturingRunner()
    swap._install_unit(
        _REPO_ROOT, pathlib.Path("/usr/local/bin/llama-swap"),
        pathlib.Path("/var/lib/llm-server-cockpit/llama-swap/config.yaml"),
        "100.64.0.1:8090", host_profile, runner,
    )
    # _sync_timer_pair consults `systemctl is-enabled`; there is no systemd on the dev box and
    # the answer does not change what gets rendered.
    swap._is_enabled = lambda unit: True
    swap.sync_scheduled_restart(host_profile, _REPO_ROOT, runner)
    swap.sync_update_check_timer(host_profile, _REPO_ROOT, runner)
    scripts_step.install_unit(
        {"id": "demo", "path": "/opt/demo/serve.py", "args": ["--port", "9000"], "restart_policy": "always"},
        host_profile, _REPO_ROOT, runner,
    )
    scripts_step.install_unit(
        {"id": "demo-bash", "type": "bash", "path": "/opt/demo/run.sh", "args": ["--clean"], "restart_policy": "on-failure"},
        host_profile, _REPO_ROOT, runner,
    )
    wol_iface = host_profile["network"]["wol"]["interface"]
    wol_unit_name = f"wol-{wol_iface}.service"
    runner.write_file(pathlib.Path("/etc/systemd/system") / wol_unit_name, wol._unit_content(wol_iface, _REPO_ROOT))

    expected = {
        "llama-swap.service": ("Unit", "Service", "Install"),
        "llama-swap-restart.service": ("Unit", "Service"),
        "llama-swap-restart.timer": ("Unit", "Timer", "Install"),
        "llm-server-cockpit-update-check.service": ("Unit", "Service"),
        "llm-server-cockpit-update-check.timer": ("Unit", "Timer", "Install"),
        "cockpit-script-demo.service": ("Unit", "Service", "Install"),
        "cockpit-script-demo-bash.service": ("Unit", "Service", "Install"),
        wol_unit_name: ("Unit", "Service", "Install"),
    }
    missing = set(expected) - set(runner.written)
    assert not missing, f"these units were never rendered, so nothing below checked them: {sorted(missing)}"

    for name, sections in expected.items():
        text = runner.written[name]
        assert text.strip(), f"{name}: rendered empty"
        parser = parse_unit(name, text)
        for section in sections:
            assert parser.has_section(section), f"{name}: missing [{section}] — has {parser.sections()}"
        assert_no_unexpanded_placeholder(name, text)
        if parser.has_section("Service"):
            assert parser.has_option("Service", "ExecStart"), f"{name}: [Service] with no ExecStart="
        if parser.has_section("Timer"):
            assert parser.get("Timer", "OnCalendar").strip(), f"{name}: [Timer] with empty OnCalendar="
    wol_text = runner.written[wol_unit_name]
    wol_parser = parse_unit(wol_unit_name, wol_text)
    assert wol_parser.has_option("Service", "ExecStop"), f"{wol_unit_name}: [Service] with no ExecStop= (some NICs drop WOL arming on poweroff, not just reboot)"
    print(f"  {len(expected)} systemd units render, parse, and carry their required sections")


def check_wol_unit_byte_identical_to_prior_output() -> None:
    """`wol.service.tmpl` + Template.substitute() must render exactly what the inline
    `_unit_content()` produced before this relocation — captured from the pre-relocation
    code with iface="enp6s0" (a realistic name, not the render-shape test's placeholder
    "TODO", so this actually exercises unit_value/unit_command's escaping and isn't vacuously
    true for a value with no `%` or `$` in it either way).
    """
    # Captured verbatim via `python3 -c "from provision.steps.wol import _unit_content;
    # print(repr(_unit_content('enp6s0')))"` against the pre-relocation code, before
    # `_unit_content` was changed to take `repo_root` and render from the .tmpl file.
    prior_output = (
        "[Unit]\n"
        "Description=Enable Wake-on-LAN magic packet for enp6s0\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "DefaultDependencies=no\n"
        "Before=shutdown.target\n"
        "Conflicts=shutdown.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "RemainAfterExit=yes\n"
        "ExecStart=/usr/sbin/ethtool -s enp6s0 wol g\n"
        "ExecStop=/usr/sbin/ethtool -s enp6s0 wol g\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    rendered = wol._unit_content("enp6s0", _REPO_ROOT)
    assert rendered == prior_output, (
        "wol.service.tmpl relocation changed the rendered unit's bytes — it must not (this is a "
        "relocation for testability, not a redesign; plans/05-qa-remediation-pass.md §0e/Phase 4):\n"
        f"--- prior ---\n{prior_output!r}\n--- rendered ---\n{rendered!r}"
    )
    print("  wol-<iface>.service renders byte-identical to the pre-relocation _unit_content() output")


def check_wol_ethtool_missing_mutates_nothing() -> None:
    """Regression test for §0e defect (ii): the run must fail before touching anything.

    Before the fix, the ethtool availability check ran AFTER `_fix_tlp` and
    `_arm_networkmanager` — both of which mutate (write a TLP drop-in, restart tlp, and/or run
    `nmcli con up`, a link-up event). A host with no ethtool then aborted having already
    disturbed the NIC, with no re-arm. `tlp` is made resolvable here (a fake path — never
    executed, since CapturingRunner intercepts every `runner.run`/`write_file` call) so that a
    reintroduced ordering bug would actually attempt a mutation and this test would catch it;
    `nmcli` is left unresolvable so `_arm_networkmanager` short-circuits without touching
    `subprocess.run` for a binary (`systemctl`) this dev box may not even have.
    """
    host_profile = schema.load_host_profile(_REPO_ROOT / "hosts" / "example.yaml")
    host_profile["network"]["wol"] = {"interface": "enp6s0", "mac": "d8:bb:c1:00:11:22"}

    runner = CapturingRunner()
    original_resolve_tool = wol._resolve_tool
    original_read_iface_mac = wol._read_iface_mac
    # Bypass the `ip link show` MAC check (no `ip` on this dev box, and it isn't what this test
    # is about) so the run reaches the ethtool-availability guard being exercised.
    wol._read_iface_mac = lambda iface: "d8:bb:c1:00:11:22"
    wol._resolve_tool = lambda name: (
        None if name in ("ethtool", "nmcli") else "/usr/sbin/tlp" if name == "tlp" else original_resolve_tool(name)
    )
    try:
        try:
            wol.run(host_profile, {}, {}, runner, _REPO_ROOT)
        except SystemExit as e:
            assert "ethtool" in str(e.code), f"wrong reason for exit: {e.code!r}"
        else:
            raise AssertionError("wol.run() did not exit despite ethtool being unresolvable")
        assert runner.run_calls == [], (
            f"wol.run() ran a command despite ethtool being unresolvable — mutated before "
            f"failing: {runner.run_calls}"
        )
        assert runner.written == {}, (
            f"wol.run() wrote a file despite ethtool being unresolvable — mutated before "
            f"failing: {sorted(runner.written)}"
        )
    finally:
        wol._resolve_tool = original_resolve_tool
        wol._read_iface_mac = original_read_iface_mac
    print("  wol.run() with ethtool unresolvable performs zero mutations (regression for §0e defect ii)")


def check_systemd_metacharacter_escaping() -> None:
    """A path or argument containing `%` or `$` must not change what the unit does.

    `%H` is the hostname specifier and `$HOME` is an environment expansion; both are consumed
    by systemd unless doubled. This renders a script whose path and args carry both, then
    un-escapes exactly as systemd would and asserts the original argv comes back.
    """
    host_profile = schema.load_host_profile(_REPO_ROOT / "hosts" / "example.yaml")
    argv_args = ["--out", "$HOME/data", "--tag", "100%"]
    script = {
        "id": "pct%svc",
        "path": "/opt/%HOSTDIR/run.py",
        "working_dir": "/opt/%HOSTDIR",
        "args": argv_args,
        "restart_policy": "always",
    }
    runner = CapturingRunner()
    scripts_step.install_unit(script, host_profile, _REPO_ROOT, runner)
    text = runner.written["cockpit-script-pct%svc.service"]
    parser = parse_unit("cockpit-script-pct%svc.service", text)

    for setting, value in (("WorkingDirectory", parser.get("Service", "WorkingDirectory")),
                           ("ExecStart", parser.get("Service", "ExecStart"))):
        bare = value.replace("%%", "")
        assert "%" not in bare, (
            f"{setting}= carries an unescaped `%`, which systemd reads as a specifier: {value!r}"
        )
    exec_start = parser.get("Service", "ExecStart")
    bare_dollar = exec_start.replace("$$", "")
    assert "$" not in bare_dollar, (
        f"ExecStart= carries an unescaped `$`, which systemd expands from the environment: {exec_start!r}"
    )

    # Now read it back the way the consumer does: systemd un-doubles, then /bin/sh parses.
    unescaped = exec_start.replace("$$", "$").replace("%%", "%")
    sh_argv = shlex.split(unescaped)
    assert sh_argv[:2] == ["/bin/sh", "-c"] and len(sh_argv) == 3, (
        f"ExecStart is no longer `/bin/sh -c <single quoted payload>`: {sh_argv!r}"
    )
    recovered = shlex.split(sh_argv[2])
    assert recovered == ["python3", "/opt/%HOSTDIR/run.py", *argv_args], (
        f"argv did not survive the round trip through systemd + sh:\n"
        f"  wanted: {['python3', '/opt/%HOSTDIR/run.py', *argv_args]}\n"
        f"  got   : {recovered}"
    )
    print("  `%` and `$` in a script path/args survive systemd and /bin/sh unchanged")


def check_llama_swap_config() -> None:
    """config.yaml is llama-swap's, not systemd's — `${PORT}` there is a llama-swap placeholder
    that must survive verbatim. The systemd escaping above must never reach this file."""
    manifest = schema.load_manifest(_REPO_ROOT / "manifest.example.yaml")
    host_profile = schema.load_host_profile(_REPO_ROOT / "hosts" / "example.yaml", manifest)
    models = schema.load_models(_REPO_ROOT / "models.example.yaml", host_profile, manifest)

    text = swap._generate_config(host_profile, models)
    parsed = yaml.safe_load(text)
    assert isinstance(parsed, dict), f"config.yaml did not parse as a mapping: {type(parsed)}"
    assert parsed.get("models"), "config.yaml has no models — nothing below checked anything"

    declared = {m["id"] for m in models["models"]}
    assert set(parsed["models"]) == declared, (
        f"config.yaml models differ from models.yaml: "
        f"missing={sorted(declared - set(parsed['models']))} extra={sorted(set(parsed['models']) - declared)}"
    )
    for model_id, entry in parsed["models"].items():
        assert entry.get("cmd"), f"model {model_id!r} rendered with no cmd"
    llama_cpp = [m["id"] for m in models["models"] if m["engine"] == "llama-cpp"]
    assert llama_cpp, "fixture has no llama-cpp models — the ${PORT} assertion below is vacuous"
    for model_id in llama_cpp:
        cmd = parsed["models"][model_id]["cmd"]
        assert "${PORT}" in cmd, (
            f"model {model_id!r} lost llama-swap's ${{PORT}} placeholder — systemd escaping has "
            f"leaked into config.yaml: {cmd!r}"
        )
        assert "$$" not in cmd, f"model {model_id!r} cmd was systemd-escaped: {cmd!r}"
    # A group is how the cockpit says "pinned". llama-swap v255 defaults a group to
    # exclusive: true, persistent: false (internal/config/config.go:93-100), which lets any
    # ungrouped model evict the pool — so every flag must be explicit.
    assert parsed.get("groups"), "fixture has no grouped models — the pinned-group assertion below is vacuous"
    for name, group in parsed["groups"].items():
        flags = {k: group.get(k) for k in ("swap", "exclusive", "persistent")}
        assert flags == {"swap": False, "exclusive": False, "persistent": True}, (
            f"group {name!r} is not a pinned group: {flags} — ungrouped loads would evict it"
        )
    print(f"  config.yaml parses, carries all {len(parsed['models'])} models, keeps ${{PORT}} intact, "
          f"and emits every group as persistent/non-exclusive")


def main() -> None:
    check_systemd_units()
    check_systemd_metacharacter_escaping()
    check_llama_swap_config()
    check_wol_unit_byte_identical_to_prior_output()
    check_wol_ethtool_missing_mutates_nothing()
    print("Rendered-artifact verification PASSED.")


if __name__ == "__main__":
    main()
