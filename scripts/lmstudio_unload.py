"""Operator CLI — force-unload a model on an LM Studio instance.

Same primitive the watchdog invokes on a timeout breach. Use when a slot
looks wedged and you want to free it without waiting for the next
watchdog fire.

Run:   .venv/bin/python scripts/lmstudio_unload.py [HOST] MODEL
       .venv/bin/python scripts/lmstudio_unload.py localhost:1236 qwen3.5-35b-a3b
       .venv/bin/python scripts/lmstudio_unload.py qwen3.5-35b-a3b   # defaults HOST=localhost:1236
"""
from __future__ import annotations

import argparse
import sys

from bitgn_contest_agent.backend.lmstudio_watchdog import force_unload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "args", nargs="+",
        help="Either MODEL (defaults HOST=localhost:1236) or HOST MODEL.",
    )
    ns = parser.parse_args(argv)
    if len(ns.args) == 1:
        host, model = "localhost:1236", ns.args[0]
    elif len(ns.args) == 2:
        host, model = ns.args
    else:
        parser.error("expected 1 or 2 positional args")

    print(f"[unload] host={host} model={model}")
    force_unload(host, model)
    print("[unload] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
