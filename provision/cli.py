"""provision <step> [--host NAME] [--dry-run] — entry point, see bin/provision."""
from __future__ import annotations

import argparse
import logging
import socket
import sys
from pathlib import Path

from provision import schema
from provision.common import Runner, require_root
from provision.schema import ValidationError
from provision.steps import build, drivers, hf, swap, wol

REPO_ROOT = Path(__file__).resolve().parent.parent

# Dependency order per the project scope: wol -> drivers -> hf -> build -> swap.
STEPS = {
    "wol": wol.run,
    "drivers": drivers.run,
    "hf": hf.run,
    "build": build.run,
    "swap": swap.run,
}


def _load(host: str):
    # Manifest first: load_host_profile validates gpus[].backends against manifest["backends"]
    # (schema.py), so the manifest has to already be in hand for that check to run at all.
    manifest = schema.load_manifest(REPO_ROOT / "manifest.yaml")
    host_profile = schema.load_host_profile(REPO_ROOT / "hosts" / f"{host}.yaml", manifest)
    models = schema.load_models(REPO_ROOT / "models.yaml", host_profile, manifest)
    return host_profile, manifest, models


def main() -> None:
    parser = argparse.ArgumentParser(prog="provision")
    parser.add_argument("--host", default=socket.gethostname(), help="host profile to use (hosts/<host>.yaml)")
    parser.add_argument("--dry-run", action="store_true", help="print every action, take none of them")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="step", required=True)
    for name in (*STEPS, "all"):
        sub.add_parser(name)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.dry_run:
        require_root()  # every step writes under /opt, /etc, /var/lib or installs apt packages

    try:
        host_profile, manifest, models = _load(args.host)
    except ValidationError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    runner = Runner(dry_run=args.dry_run)

    order = list(STEPS) if args.step == "all" else [args.step]
    for step in order:
        logging.info("=== %s ===", step)
        STEPS[step](host_profile, manifest, models, runner, REPO_ROOT)


if __name__ == "__main__":
    main()
