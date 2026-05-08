"""BitGN scraper CLI.

Subcommands:
  phase0    — run the lifecycle spike against PROD; write
              artifacts/harness_db/scrape_runs/<ts>/lifecycle_spike.json
  seed      — mine existing JSONL traces + server logs for free
              grader-rule seeds; populate scoring_rules in the SQLite DB
  scrape    — walk PROD workspaces into the local DB (Phase 1)
  probe     — iterate task_instantiations and probe each against the live grader (Phase 2)
  validate  — run integrity + coverage + (optional) determinism checks (Phase 3)

All subcommands are thin shims over functions in src/bitgn_scraper/.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# When invoked as `python scripts/bitgn_scraper.py`, Python inserts
# `scripts/` at sys.path[0], which shadows the `bitgn_scraper` *package*
# (in src/) with this script file itself. Fix: remove scripts/ entries and
# ensure src/ appears before any shadowing path.
_SCRIPTS = Path(__file__).resolve().parent
_SRC = _SCRIPTS.parent / "src"
sys.path = [p for p in sys.path if Path(p).resolve() != _SCRIPTS]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bitgn_scraper",
        description="BitGN PROD harness scraper.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("phase0", help="run the lifecycle spike (Phase 0)")
    sub.add_parser("seed", help="mine existing logs for free grader rules (Phase 1.5)")
    sub.add_parser("scrape", help="Phase 1: walk PROD workspaces into the local DB")
    sub.add_parser("probe", help="Phase 2: probe each instantiation against the live grader")
    sub.add_parser("validate", help="Phase 3: integrity + coverage + determinism checks")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    # Each subcommand re-parses sys.argv[2:] in its own CLI shim, so let
    # subcommand-specific flags pass through here without erroring.
    args, _rest = parser.parse_known_args(argv)
    if args.cmd == "phase0":
        from bitgn_scraper.phase0 import run_phase0_cli
        return run_phase0_cli()
    if args.cmd == "seed":
        from bitgn_scraper.seed_rules import run_seed_cli
        return run_seed_cli()
    if args.cmd == "scrape":
        from bitgn_scraper.scrape_cli import run_scrape_cli
        return run_scrape_cli()
    if args.cmd == "probe":
        from bitgn_scraper.probe_cli import run_probe_cli
        return run_probe_cli()
    if args.cmd == "validate":
        from bitgn_scraper.validate_cli import run_validate_cli
        return run_validate_cli()
    parser.error(f"unknown command: {args.cmd!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
