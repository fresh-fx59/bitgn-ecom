#!/usr/bin/env -S .venv/bin/python3 -u
"""Reconstruct a local workspace from a PCM transcript.

The pcm_transcript.txt files in artifacts/ws_snapshots/*/run_0/ are
recordings of the BitGN playground harness emitting `[ts] ❯ cmd` +
command output. This parses `cat PATH` blocks into a local workspace
tree so `local_bench.py --workspace <dir>` can replay the task.

Usage:
    python scripts/rebuild_ws_from_transcript.py \
        --transcript artifacts/ws_snapshots/t091_ocr_badger/run_0/pcm_transcript.txt \
        --output     artifacts/ws_snapshots/t091_ocr_badger/run_0/workspace
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

# A command line: `[2026-04-21T15:59:55.569Z] ❯ cat AGENTS.md`
CMD_RE = re.compile(r"^\[[^\]]+\]\s+❯\s+(.*)$")


def parse(transcript_path: Path, out_root: Path) -> tuple[int, int]:
    """Walk the transcript, extract every `cat <PATH>` block, and write
    PATH under out_root. Returns (files_written, blocks_skipped).
    """
    out_root.mkdir(parents=True, exist_ok=True)
    lines = transcript_path.read_text(encoding="utf-8").splitlines()
    i = 0
    n = len(lines)
    written = 0
    skipped = 0
    while i < n:
        m = CMD_RE.match(lines[i])
        if not m:
            i += 1
            continue
        cmd = m.group(1).strip()
        i += 1
        # Collect lines until next command OR end of file
        body: list[str] = []
        while i < n and not CMD_RE.match(lines[i]):
            body.append(lines[i])
            i += 1
        # Trim trailing blank line produced by the transcript format
        while body and body[-1] == "":
            body.pop()

        if cmd.startswith("cat "):
            path = cmd[4:].strip()
            if not path or path.startswith("-"):
                skipped += 1
                continue
            full = out_root / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text("\n".join(body) + ("\n" if body else ""), encoding="utf-8")
            written += 1
        else:
            skipped += 1

    return written, skipped


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--transcript", required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args()
    transcript = Path(args.transcript).resolve()
    out = Path(args.output).resolve()
    written, skipped = parse(transcript, out)
    print(f"wrote {written} files; skipped {skipped} non-cat commands")
    print(f"output: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
