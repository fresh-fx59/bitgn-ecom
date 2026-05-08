"""Mine BitGN server-side .log files for failed-task score lines.

Server logs (vm-*.eu.bitgn.com.txt and t*-*.log files at repo root)
end each trial with one or more lines like:

  2026-04-13T10:35:05Z [ ERR ] AI agent score 0.00 / <score_detail>

We extract just the (score, score_detail) tail. The task_id is unknown
from the log alone — caller decides how to attribute findings (typically
by VM hostname → task mapping or filename convention).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_SCORE_LINE = re.compile(
    r"\[\s*ERR\s*\]\s*AI agent score\s+([0-9.]+)\s*/\s*(.+?)\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ServerLogFinding:
    score: float
    score_detail: str
    source_path: str


def mine_server_log(path: Path) -> list[ServerLogFinding]:
    """Return failed-task score lines from a server log."""
    text = path.read_text(encoding="utf-8", errors="replace")
    out: list[ServerLogFinding] = []
    for m in _SCORE_LINE.finditer(text):
        score = float(m.group(1))
        if score >= 1.0:
            continue
        out.append(ServerLogFinding(
            score=score,
            score_detail=m.group(2),
            source_path=str(path),
        ))
    return out
