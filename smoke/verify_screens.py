#!/usr/bin/env python3
"""Phase 4 verification script: mounts CockpitApp under Textual's Pilot test harness,
validates layout reflow at 80x24 (-narrow) and 121x30 (-wide), navigates through all 7
screens, and exports SVG screenshots.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import bootstrap  # noqa: E402
bootstrap.add_venv_site_packages(_REPO_ROOT)

from cockpit.app import CockpitApp  # noqa: E402
from cockpit.screens.scripts import ScriptsScreen  # noqa: E402
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
        ("containers", "deployments", "#deployment-tabs", "containers"),
        ("scripts", "deployments", "#deployment-tabs", "scripts"),
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

                svg = app.export_screenshot()
                svg_path = out_dir / f"{name}_{w}x{h}.svg"
                svg_path.write_text(svg, encoding="utf-8")
                print(f"Saved {svg_path.relative_to(_REPO_ROOT)} ({len(svg)} bytes)")

    print("All geometry and screenshot verification checks PASSED.")


if __name__ == "__main__":
    asyncio.run(verify_geometry_and_export_screenshots())
