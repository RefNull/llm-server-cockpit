#!/usr/bin/env python3
"""Phase 4 verification script: mounts CockpitApp under Textual's Pilot test harness,
validates layout reflow at 80x24 (-narrow) and 121x30 (-wide), navigates through all 7
screens, and exports SVG screenshots.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import os
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
import sys

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import bootstrap  # noqa: E402
bootstrap.add_venv_site_packages(_REPO_ROOT)

from cockpit.app import CockpitApp  # noqa: E402
from cockpit.screens.scripts import ScriptsScreen  # noqa: E402
from cockpit.screens.settings import SettingsScreen  # noqa: E402
from provision.steps import wol  # noqa: E402
from cockpit.widgets import CockpitDataTable  # noqa: E402
from textual.widgets import Button, TabbedContent  # noqa: E402

USABLE_WIDTH = 115
"""DESIGN.md §4.2: 121 (the -wide breakpoint) minus $space-edge on both sides. Every
non-modal table's render width must fit this, because no table pins columns any more."""


def _assert_table_widths_in_budget(app: CockpitApp, context: str) -> None:
    """DESIGN.md §4.2/§4.4. `fixed_columns` is gone, so a table that overflows has no pinned
    identifier to fall back on and its right-hand action columns are simply unreachable —
    which is the defect that budget replaced pinning to prevent."""
    for table in app.query(CockpitDataTable):
        if not table.is_on_screen:
            continue
        assert table.fixed_columns == 0, (
            f"[{context}] {table.id}: fixed_columns={table.fixed_columns}; DESIGN.md §4.2 "
            "forbids pinned columns (datatable--fixed replaces the row style and bands the "
            "zebra stripes)"
        )
        columns = list(table.ordered_columns)
        render_width = sum(c.width for c in columns) + 2 * len(columns)
        assert render_width <= USABLE_WIDTH, (
            f"[{context}] {table.id}: render width {render_width} > {USABLE_WIDTH} usable "
            f"cells — its rightmost column is off-screen at 121x30"
        )


def _assert_script_toggle_labels(app: CockpitApp, context: str) -> None:
    """The two toggle columns on scripts-table resolve their label per row (DESIGN.md §9). A
    static label would silently offer "Start" on an already-running unit."""
    screen = app.query_one(ScriptsScreen)
    screen._apply_rows(
        [
            ({"id": "running", "path": "/opt/a.py"}, {"unit_active": True, "unit_enabled": True}, "active, enabled"),
            ({"id": "stopped", "path": "/opt/b.py"}, {"unit_active": False, "unit_enabled": False}, "stopped, disabled"),
        ]
    )
    table = app.query_one("#scripts-table", CockpitDataTable)
    expected = {
        ("run", "running"): "Stop",
        ("run", "stopped"): "Start",
        ("boot", "running"): "Disable",
        ("boot", "stopped"): "Enable",
    }
    for (action_id, row_key), want in expected.items():
        got = table._actions[action_id].resolve_label(row_key)
        assert got == want, f"[{context}] {action_id} on {row_key}: expected {want!r}, got {got!r}"
    # The prompt must name the verb the operator actually clicked, not a fixed one.
    assert table._actions["run"].confirm_message("running") == "Stop script running?"


def _assert_no_awaited_workers() -> None:
    """`@work` returns a `Worker`, which is not awaitable — `await self._some_worker()` raises
    `TypeError: 'Worker' object can't be awaited` and takes the whole app down the moment the
    button is pressed. It type-checks as a coroutine call at a glance and nothing catches it
    until a user clicks, so this is a static sweep rather than a per-button click test: driving
    every button under the pilot would fire real host mutations (container restarts, deploys).

    Seven call sites shipped with this bug across deploy.py, downloads.py and scripts.py.
    """
    offenders: list[str] = []
    for path in sorted(Path(_REPO_ROOT / "cockpit").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        workers = {
            fn.name
            for fn in ast.walk(tree)
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
            for d in fn.decorator_list
            if (d.func.id if isinstance(d, ast.Call) and isinstance(d.func, ast.Name) else
                d.id if isinstance(d, ast.Name) else None) == "work"
        }
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Await) and isinstance(node.value, ast.Call)):
                continue
            func = node.value.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "self"
                and func.attr in workers
            ):
                rel = path.relative_to(_REPO_ROOT)
                offenders.append(f"{rel}:{node.lineno}: await self.{func.attr}() — {func.attr} is @work")
    assert not offenders, "awaited @work method(s):\n  " + "\n  ".join(offenders)


async def _assert_launch_defers_hidden_tabs() -> None:
    """A tab the operator cannot see must do no I/O until it is opened.

    Textual mounts every TabPane's content up front, so each screen's initial load used to fire
    at launch — seven screens' worth of subprocesses and four GitHub calls to render the one
    tab on screen, with the same facts fetched two and three times over. Data reads therefore
    live in on_first_view (CockpitScreenBase.ensure_first_view), never on_mount.

    Probed with the upstream version check: the Dashboard's own upstream line legitimately
    costs 2 GitHub calls at launch, and the Backends tab's check must add its 2 only when that
    tab is actually opened.
    """
    calls: list[str] = []
    real_urlopen = urllib.request.urlopen
    real_popen = subprocess.Popen

    class _Popen(real_popen):  # type: ignore[misc]
        def __init__(self, cmd, *a, **k):
            calls.append(cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd[:3]))
            super().__init__(cmd, *a, **k)

    def _urlopen(req, *a, **k):
        calls.append("NET " + (req if isinstance(req, str) else req.full_url).split("?")[0])
        return real_urlopen(req, *a, **k)

    # run() funnels through Popen, so wrapping only Popen counts each spawn exactly once.
    subprocess.Popen = _Popen
    urllib.request.urlopen = _urlopen
    try:
        app = CockpitApp(host="example")
        async with app.run_test(size=(121, 30)) as pilot:
            await pilot.pause(2.5)
            launch = list(calls)
            duplicated = {c: launch.count(c) for c in set(launch) if launch.count(c) > 1}
            assert not duplicated, f"launch repeats the same work: {duplicated}"
            github = [c for c in launch if "api.github.com" in c]
            assert len(github) <= 2, f"launch made {len(github)} GitHub calls, expected the Dashboard's 2: {github}"

            # Opening Backends must now do the work that used to happen at launch.
            app.query_one("#main-tabs", TabbedContent).active = "llm"
            await pilot.pause(0.2)
            app.query_one("#llm-tabs", TabbedContent).active = "backends"
            await pilot.pause(2.0)
            opened = [c for c in calls if "api.github.com" in c]
            assert len(opened) > len(github), (
                "opening Backends ran no upstream check — its first-view load never fired, so "
                "this check cannot tell deferral from deletion"
            )
        print(f"launch: {len(launch)} operations, none duplicated, {len(github)} GitHub calls; "
              f"Backends adds {len(opened) - len(github)} more only when opened")
    finally:
        subprocess.Popen = real_popen
        urllib.request.urlopen = real_urlopen


async def _assert_markup_escaping_survives_textual() -> None:
    """Operator/vendor data with a `[` must reach the screen intact.

    Textual 8.2.8 parses its own content markup and accepts tags Rich does not: Rich's tag
    regex requires `[a-z#/@]` after the bracket, Textual's does not. So `Intel Corporation DG2
    [Arc A770]` passes through BOTH `rich.markup.escape` and `textual.markup.escape` untouched
    — each built around the Rich-era rule — and is then eaten by Textual's parser, truncating
    the value at the bracket with no error and no exception. `cockpit.widgets.escape_markup`
    exists for exactly this and is the only thing that works.

    Measured before the fix: a GPU name rendered as `gpu-intel · Intel Corporation DG2`.
    """
    from textual.app import App
    from textual.geometry import Region
    from textual.widgets import Static

    from cockpit.widgets import escape_markup, service_row

    raw = "Intel Corporation DG2 [Arc A770]"

    class _Probe(App):
        def compose(self):
            yield Static(f"[$accent]{escape_markup(raw)}[/]", id="probe-escaped")
            yield Static(service_row("[prod] web", True, raw), id="probe-row")

    app = _Probe()
    async with app.run_test(size=(80, 6)) as pilot:
        await pilot.pause(0.3)
        for widget_id, must_contain in (("probe-escaped", raw), ("probe-row", "[prod] web")):
            widget = app.query_one(f"#{widget_id}", Static)
            strip = widget.render_lines(Region(0, 0, widget.size.width, 1))[0]
            rendered = "".join(seg.text for seg in strip._segments)
            assert must_contain in rendered, (
                f"{widget_id}: Textual's markup parser ate the bracket span — "
                f"expected {must_contain!r} in {rendered.strip()!r}"
            )
    print("bracketed vendor/operator data survives Textual's markup parser")


def _assert_apply_service_ignores_wol() -> None:
    """Saving a WOL interface must not go through "Apply Service Settings".

    That button runs swap.run() — a llama-swap reinstall into /usr/local/bin plus systemd
    units — so changing an interface name failed outright for an unprivileged cockpit with
    `install ... returned non-zero exit status 1`. WOL is declarative config and has its own
    Save button; if these fields ever get read here again the same crash comes back.
    """
    source = inspect.getsource(SettingsScreen._confirm_and_apply_service)
    assert "f-wol" not in source, (
        "SettingsScreen._confirm_and_apply_service reads the WOL fields again — that routes a "
        "declarative WOL save through swap.run(), which needs root"
    )


async def _assert_wol_form_wiring(app: CockpitApp, pilot, context: str) -> None:
    """The Interface Select offers real NICs and auto-fills the MAC from the chosen one.

    Driven against a fixture sysfs tree so it runs anywhere, including the macOS dev box that
    has no /sys/class/net at all.
    """
    from textual.widgets import Input, Select

    with tempfile.TemporaryDirectory() as td:
        net = Path(td) / "net"
        for name, mac in (("enp6s0", "d8:bb:c1:00:11:22"), ("eno1", "00:1a:2b:33:44:55")):
            iface = net / name
            iface.mkdir(parents=True)
            (iface / "type").write_text("1\n")
            (iface / "address").write_text(mac + "\n")
            (iface / "device").mkdir()
        real_sysfs, wol._SYSFS_NET = wol._SYSFS_NET, net
        try:
            screen = app.query_one(SettingsScreen)
            screen._populate_wol_form()
            await pilot.pause(0.1)
            select = app.query_one("#f-wol-interface", Select)
            mac_field = app.query_one("#f-wol-mac", Input)

            options = [value for _, value in select._options if value is not Select.BLANK]
            for name in ("enp6s0", "eno1"):
                assert name in options, f"[{context}] {name} missing from the interface Select: {options}"

            select.value = "enp6s0"
            await pilot.pause(0.2)
            assert mac_field.value == "d8:bb:c1:00:11:22", (
                f"[{context}] MAC did not auto-fill from the chosen NIC: {mac_field.value!r}"
            )

            # An operator override must survive a repopulate — hence prevent(Select.Changed)
            # in _populate_wol_form.
            mac_field.value = "de:ad:be:ef:00:01"
            screen.host_profile["network"]["wol"] = {"interface": "enp6s0", "mac": "de:ad:be:ef:00:01"}
            screen._populate_wol_form()
            await pilot.pause(0.2)
            assert mac_field.value == "de:ad:be:ef:00:01", (
                f"[{context}] repopulating clobbered the operator's MAC override: {mac_field.value!r}"
            )
        finally:
            wol._SYSFS_NET = real_sysfs


async def _assert_root_gate(app: CockpitApp, pilot, context: str) -> None:
    """confirm(requires_root=True) warns about root, elevates a SEPARATE Runner, and aborts
    cleanly when it cannot authenticate.

    The separate Runner is the load-bearing part: `self.runner` is shared app-wide, so
    elevating it in place would silently put every later action on the same screen — an HF
    download, say — through sudo and leave root-owned files in the operator's models_dir.
    """
    from textual.widgets import Static

    screen = app.query_one(SettingsScreen)
    bindir = Path(tempfile.mkdtemp())
    real_path, real_notify = os.environ["PATH"], app.notify
    try:
        # A stub `sudo` that reports a live credential cache, so no terminal prompt is needed.
        (bindir / "sudo").write_text("#!/bin/sh\nexit 0\n")
        (bindir / "sudo").chmod(0o755)
        os.environ["PATH"] = f"{bindir}{os.pathsep}{real_path}"

        screen._privileged_runner = None
        worker = screen.run_worker(screen.confirm("Do the thing?", requires_root=True))
        await pilot.pause(0.3)
        prompt = str(app.screen.query_one("#confirm-message", Static).render())
        assert "needs root" in prompt and "sudo password" in prompt, (
            f"[{context}] root prompt does not mention sudo: {prompt!r}"
        )
        app.screen.dismiss(True)
        assert await worker.wait() is True, f"[{context}] confirm(requires_root=True) refused a cached sudo"
        assert screen.privileged_runner.sudo, f"[{context}] privileged_runner was not elevated"
        assert not screen.runner.sudo, (
            f"[{context}] elevation leaked onto the shared Runner — every later action on this "
            "screen would now run as root"
        )

        # No sudo available at all: refuse, and say what to do. Never a traceback from a worker.
        (bindir / "sudo").write_text("#!/bin/sh\nexit 1\n")
        (bindir / "sudo").chmod(0o755)
        screen._privileged_runner = None
        notifications: list[str] = []
        app.notify = lambda message, **kwargs: notifications.append(str(message))
        worker = screen.run_worker(screen.confirm("Do the thing?", requires_root=True))
        await pilot.pause(0.3)
        app.screen.dismiss(True)
        assert await worker.wait() is False, f"[{context}] confirm proceeded without sudo"
        assert notifications, f"[{context}] refused silently — the operator was told nothing"

        # An action that does not require root must not be elevated or warned about.
        app.notify = real_notify
        screen._privileged_runner = None
        worker = screen.run_worker(screen.confirm("Harmless?", mutates_system=True))
        await pilot.pause(0.3)
        prompt = str(app.screen.query_one("#confirm-message", Static).render())
        assert "needs root" not in prompt, f"[{context}] non-root action asked for sudo: {prompt!r}"
        app.screen.dismiss(True)
        await worker.wait()
        assert screen.privileged_runner is screen.runner, (
            f"[{context}] a non-root action got an elevated Runner"
        )
    finally:
        os.environ["PATH"] = real_path
        app.notify = real_notify
        screen._privileged_runner = None
        shutil.rmtree(bindir, ignore_errors=True)


def _assert_buttons_in_bounds(app: CockpitApp, context: str) -> None:
    """Defect 3 (plans/03-ui-qa-pass.md remediation pass): a mounted, enabled Button must
    never extend past the right edge of the screen viewport. This script used to only assert
    against a phantom vertical scrollbar, which passed while action-row buttons ("Preview
    YAML", "Remove selected", "Check / Enable Tailscale") sat off-screen at 80x24 with no
    horizontal-scroll affordance to reach them. `is_on_screen` excludes buttons in an inactive
    TabPane, so this only checks buttons the operator can actually see right now."""
    right_edge = app.screen.region.right
    for button in app.query(Button):
        if button.disabled or not button.is_on_screen:
            continue
        region = button.region
        assert region.right <= right_edge, (
            f"[{context}] Button {button.label!r} (id={button.id!r}) extends past the "
            f"screen's right edge: button.region.right={region.right} > "
            f"screen.region.right={right_edge}"
        )


async def verify_geometry_and_export_screenshots() -> None:
    _assert_no_awaited_workers()
    print("No awaited @work methods.")
    _assert_apply_service_ignores_wol()
    print("Apply Service Settings does not read the WOL fields.")
    await _assert_launch_defers_hidden_tabs()
    await _assert_markup_escaping_survives_textual()

    out_dir = _REPO_ROOT / "screenshots" / "verification"
    out_dir.mkdir(parents=True, exist_ok=True)

    resolutions = [
        ((80, 24), "-narrow", "vertical"),
        ((121, 30), "-wide", "horizontal"),
    ]

    # (name, root TabPane id, nested TabbedContent id or None, nested TabPane id or None)
    screens = [
        ("dashboard", "dashboard", None, None),
        ("builds", "llm", "#llm-tabs", "backends"),
        ("deploy", "llm", "#llm-tabs", "models"),
        ("downloads", "llm", "#llm-tabs", "hf-downloads"),
        ("containers", "system", "#system-tabs", "containers"),
        ("scripts", "system", "#system-tabs", "scripts"),
        ("systemd", "system", "#system-tabs", "systemd"),
        ("settings", "settings", "#settings-tabs", "settings-tab-host"),
        ("settings_gpus", "settings", "#settings-tabs", "settings-tab-gpus"),
        ("settings_connectors", "settings", "#settings-tabs", "settings-tab-connectors"),
        ("settings_services", "settings", "#settings-tabs", "settings-tab-services"),
    ]

    for size, expected_class, expected_layout in resolutions:
        w, h = size
        print(f"=== Testing resolution {w}x{h} ===")
        app = CockpitApp(host="example")
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.2)
            classes = set(app.screen.classes)
            print(f"Screen classes: {classes}")
            assert expected_class in classes, f"Expected {expected_class} in {classes}"
            assert app.screen.show_vertical_scrollbar is False, f"Phantom scrollbar detected at {w}x{h}"

            banner_art = app.query_one("#banner-art")
            banner_compact = app.query_one("#banner-compact")
            if expected_class == "-narrow":
                assert banner_art.styles.display == "none", "Expected banner-art to be hidden at 80x24"
                assert banner_compact.styles.display == "block", "Expected banner-compact to be visible at 80x24"
            else:
                assert banner_art.styles.display == "block", "Expected banner-art to be visible at 121x30"
                assert banner_compact.styles.display == "none", "Expected banner-compact to be hidden at 121x30"

            dashboard_cols = app.query_one("#dashboard-columns")
            actual_layout = str(dashboard_cols.styles.layout).strip("<>")
            print(f"#dashboard-columns layout: {actual_layout}")
            assert actual_layout == expected_layout, f"Expected layout {expected_layout}, got {actual_layout}"

            # By id, not by position: the nested TabbedContents used to be addressed as
            # query(TabbedContent)[1], which silently pointed at a different group the moment
            # another one was added (Deployments).
            root_tc = app.query_one("#main-tabs", TabbedContent)

            for name, root_pane, sub_tc_id, sub_pane in screens:
                print(f"Navigating to {name} (root: {root_pane}, sub: {sub_tc_id}/{sub_pane})...")
                root_tc.active = root_pane
                if sub_tc_id:
                    await pilot.pause(0.1)
                    app.query_one(sub_tc_id, TabbedContent).active = sub_pane
                await pilot.pause(0.2)
                _assert_buttons_in_bounds(app, f"{name} @ {w}x{h}")
                _assert_table_widths_in_budget(app, f"{name} @ {w}x{h}")
                if name == "scripts":
                    _assert_script_toggle_labels(app, f"{name} @ {w}x{h}")
                    await pilot.pause(0.1)
                if name == "settings_services":
                    await _assert_wol_form_wiring(app, pilot, f"{name} @ {w}x{h}")
                    await _assert_root_gate(app, pilot, f"{name} @ {w}x{h}")

                svg = app.export_screenshot()
                svg_path = out_dir / f"{name}_{w}x{h}.svg"
                svg_path.write_text(svg, encoding="utf-8")
                print(f"Saved {svg_path.relative_to(_REPO_ROOT)} ({len(svg)} bytes)")

    print("All geometry and screenshot verification checks PASSED.")


if __name__ == "__main__":
    asyncio.run(verify_geometry_and_export_screenshots())
