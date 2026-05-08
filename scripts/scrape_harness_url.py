"""One-shot CLI for scraping a BitGN per-trial harness URL.

Usage:
    scripts/scrape_harness_url.py <url> [--out path] [--include-raw-logs]

Hits ``<url>?format=json`` once, parses the transcript, and prints a
JSON dump (or writes it to ``--out``). The dump captures: trial id,
closed_ms, every PCM command with its timestamp + captured output, the
agent's submitted answer, and the grader's score + expected value.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_SRC = _SCRIPTS.parent / "src"
sys.path = [p for p in sys.path if Path(p).resolve() != _SCRIPTS]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scrape_harness_url")
    parser.add_argument("url", help="trial URL, e.g. https://vm-xyz.eu.bitgn.com/")
    parser.add_argument("--out", type=Path, default=None,
                        help="write JSON dump to this path (default: stdout)")
    parser.add_argument("--include-raw-logs", action="store_true",
                        help="keep raw log entries in the dump (large)")
    parser.add_argument("--summary", action="store_true",
                        help="also print a one-screen summary to stderr")
    args = parser.parse_args(argv)

    from bitgn_scraper.harness_url_scrape import fetch_trial_data

    dump = fetch_trial_data(args.url, include_raw_logs=args.include_raw_logs)
    payload = dump.to_dict()
    if args.include_raw_logs:
        payload["raw_logs"] = dump.raw_logs

    text = json.dumps(payload, indent=2, sort_keys=True)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
        print(f"[scrape] wrote {args.out} ({len(text)} bytes)", file=sys.stderr)
    else:
        print(text)

    if args.summary:
        ans = dump.submitted_answer.text if dump.submitted_answer else "<none>"
        score = dump.grader.score if dump.grader else None
        expected = dump.grader.expected if dump.grader else None
        msg = (
            f"[summary] trial_id={dump.trial_id} closed={dump.is_closed} "
            f"log_entries={dump.log_count} commands={len(dump.commands)} "
            f"answer={ans!r} score={score} expected={expected!r}"
        )
        print(msg, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
