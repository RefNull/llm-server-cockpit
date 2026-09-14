#!/usr/bin/env python3
"""Phase 4 verification script: mounts CockpitApp under Textual's Pilot test harness,
validates layout reflow at 80x24 (-narrow) and 120x30 (-wide), navigates through all 7
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
from textual.widgets import TabbedContent  # noqa: E402


async def verify_geometry_and_export_screenshots() -> None:
    out_dir = _REPO_ROOT / "screenshots" / "verification"
    out_dir.mkdir(parents=True, exist_ok=True)

    resolutions = [
        ((80, 24), "-narrow", "vertical"),
        ((120, 30), "-wide", "horizontal"),
    ]

    screens = [
        ("dashboard", "dashboard", None, None),
        ("builds", "llm", "backends", None),
        ("containers", "containers", None, None),
        ("scripts", "scripts", None, None),
        ("deploy", "llm", "models", None),
        ("downloads", "llm", "hf-downloads", None),
        ("settings", "settings", None, "settings-tab-host"),
        ("settings_gpus", "settings", None, "settings-tab-gpus"),
        ("settings_services", "settings", None, "settings-tab-services"),
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
                assert banner_art.styles.display == "block", "Expected banner-art to be visible at 120x30"
                assert banner_compact.styles.display == "none", "Expected banner-compact to be hidden at 120x30"

            dashboard_cols = app.query_one("#dashboard-columns")
            actual_layout = str(dashboard_cols.styles.layout).strip("<>")
            print(f"#dashboard-columns layout: {actual_layout}")
            assert actual_layout == expected_layout, f"Expected layout {expected_layout}, got {actual_layout}"

            root_tc = app.query(TabbedContent).first()
            llm_tc = app.query(TabbedContent)[1]

            for name, root_pane, sub_pane, settings_pane in screens:
                print(f"Navigating to {name} (root: {root_pane}, sub: {sub_pane}, settings: {settings_pane})...")
                root_tc.active = root_pane
                if sub_pane:
                    llm_tc.active = sub_pane
                if settings_pane:
                    settings_tc = app.query_one("#settings-tabs", TabbedContent)
                    settings_tc.active = settings_pane
                await pilot.pause(0.2)

                svg = app.export_screenshot()
                svg_path = out_dir / f"{name}_{w}x{h}.svg"
                svg_path.write_text(svg, encoding="utf-8")
                print(f"Saved {svg_path.relative_to(_REPO_ROOT)} ({len(svg)} bytes)")

    print("All geometry and screenshot verification checks PASSED.")


if __name__ == "__main__":
    asyncio.run(verify_geometry_and_export_screenshots())
