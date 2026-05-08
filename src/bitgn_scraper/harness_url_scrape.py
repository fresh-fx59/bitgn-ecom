# src/bitgn_scraper/harness_url_scrape.py
"""One-shot scraper for the BitGN per-trial harness URL.

A live trial publishes a public log viewer at
``https://<trial_id>.eu.bitgn.com/`` with a JSON sibling endpoint
``/?format=json`` that returns the full ordered transcript:

    { "trial_id", "offset", "next_offset", "closed_ms",
      "logs": [ { "logged_ms", "rendered" }, ... ] }

Each ``rendered`` entry is HTML-ish text with ANSI escape codes. The
PCM tool calls appear as ``\u276f <command>`` lines (logged_ms > 0)
followed by output entries (logged_ms == 0). The transcript ends with
the agent's ``answer`` command, the ``[  OK  ] AnswerProvided`` marker,
and the grader's ``[ ERR ] AI agent score X.XX\n        <reason>``.

This module turns one URL into a structured ``TrialDump``: trial id,
ordered command list with timestamps, the submitted answer, and the
grader's score + expected value. Use ``fetch_trial_data(url)`` for the
default urllib transport, or pass ``http_get=`` for tests.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_COMMAND_RE = re.compile(r"^\[(?P<ts>[^\]]+)\]\s+\u276f\s+(?P<cmd>.*?)\n?$", re.DOTALL)
_GRADER_SCORE_RE = re.compile(
    r"AI agent score\s+(?P<score>\d+\.\d+)(?:\s*\n\s*(?P<msg>.+?))?\s*$",
    re.DOTALL,
)
_EXPECTED_RE = re.compile(r"Expected:\s+'(?P<expected>[^']+)'")
_ANSWER_RE = re.compile(
    r"^(?:\[[^\]]+\]\s+)?\u276f\s+answer\s+--outcome\s+(?P<outcome>\S+)\s+"
    r"<<'EOF'\s*\n(?P<body>.*?)\s*EOF\s*$",
    re.DOTALL,
)


@dataclass(frozen=True)
class CommandEntry:
    iso_ts: str
    logged_ms: int
    command: str
    output: str

    def to_dict(self) -> dict:
        return {
            "iso_ts": self.iso_ts,
            "logged_ms": self.logged_ms,
            "command": self.command,
            "output": self.output,
        }


@dataclass(frozen=True)
class ParsedCommand:
    iso_ts: str
    command: str


@dataclass(frozen=True)
class SubmittedAnswer:
    outcome: str
    text: str

    def to_dict(self) -> dict:
        return {"outcome": self.outcome, "text": self.text}


@dataclass(frozen=True)
class GraderFeedback:
    score: float
    message: str
    expected: str | None

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "message": self.message,
            "expected": self.expected,
        }


@dataclass
class TrialDump:
    trial_id: str
    source_url: str
    closed_ms: int
    log_count: int
    commands: list[CommandEntry] = field(default_factory=list)
    submitted_answer: SubmittedAnswer | None = None
    grader: GraderFeedback | None = None
    raw_logs: list[dict] = field(default_factory=list)

    @property
    def is_closed(self) -> bool:
        return self.closed_ms > 0

    def to_dict(self) -> dict:
        return {
            "trial_id": self.trial_id,
            "source_url": self.source_url,
            "closed_ms": self.closed_ms,
            "is_closed": self.is_closed,
            "log_count": self.log_count,
            "commands": [c.to_dict() for c in self.commands],
            "submitted_answer": (
                self.submitted_answer.to_dict() if self.submitted_answer else None
            ),
            "grader": self.grader.to_dict() if self.grader else None,
        }


HttpGet = Callable[[str], bytes]


def strip_ansi(text: str) -> str:
    """Remove ANSI CSI escape sequences from a rendered log entry."""
    return _ANSI_RE.sub("", text)


def extract_trial_id_from_url(url: str) -> str:
    """Extract the trial id from a harness URL.

    Accepts ``https://<trial_id>.eu.bitgn.com/`` or any subdomain
    prefix on ``bitgn.com``.
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc or parsed.path
    return host.split(".", 1)[0]


def parse_command(rendered: str) -> ParsedCommand | None:
    """Detect a `❯ <cmd>` log entry; return ParsedCommand or None."""
    plain = strip_ansi(rendered)
    m = _COMMAND_RE.match(plain)
    if not m:
        return None
    return ParsedCommand(iso_ts=m.group("ts").strip(), command=m.group("cmd").strip())


def parse_grader(rendered: str) -> GraderFeedback | None:
    """Detect the grader's `AI agent score X.XX` entry."""
    plain = strip_ansi(rendered).strip()
    m = _GRADER_SCORE_RE.search(plain)
    if not m:
        return None
    score = float(m.group("score"))
    message = (m.group("msg") or "").strip()
    expected_match = _EXPECTED_RE.search(message)
    expected = expected_match.group("expected") if expected_match else None
    return GraderFeedback(score=score, message=message, expected=expected)


def parse_answer(rendered: str) -> SubmittedAnswer | None:
    """Detect the agent's `answer --outcome ok <<'EOF' ... EOF` command."""
    plain = strip_ansi(rendered).strip()
    m = _ANSWER_RE.match(plain)
    if not m:
        return None
    return SubmittedAnswer(outcome=m.group("outcome"), text=m.group("body").strip())


def _default_http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def _build_json_url(harness_url: str) -> str:
    parsed = urllib.parse.urlparse(harness_url)
    query = dict(urllib.parse.parse_qsl(parsed.query))
    query["format"] = "json"
    new_query = urllib.parse.urlencode(query)
    path = parsed.path or "/"
    return urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, path, parsed.params, new_query, "")
    )


def fetch_trial_data(
    harness_url: str,
    *,
    http_get: HttpGet | None = None,
    include_raw_logs: bool = False,
) -> TrialDump:
    """Fetch and parse one trial transcript.

    Args:
        harness_url: trial URL, e.g. ``https://vm-xyz.eu.bitgn.com/``.
        http_get: override transport (returns response body bytes).
        include_raw_logs: keep raw ``logs`` entries on the dump for
            downstream inspection (off by default — they're large).
    """
    fetcher = http_get or _default_http_get
    json_url = _build_json_url(harness_url)
    payload = json.loads(fetcher(json_url).decode("utf-8"))

    trial_id = payload.get("trial_id") or extract_trial_id_from_url(harness_url)
    closed_ms = int(payload.get("closed_ms") or 0)
    logs = payload.get("logs") or []

    commands: list[CommandEntry] = []
    submitted_answer: SubmittedAnswer | None = None
    grader: GraderFeedback | None = None

    pending: ParsedCommand | None = None
    pending_logged_ms: int = 0
    pending_outputs: list[str] = []

    def _flush() -> None:
        nonlocal pending, pending_logged_ms, pending_outputs
        if pending is None:
            return
        commands.append(CommandEntry(
            iso_ts=pending.iso_ts,
            logged_ms=pending_logged_ms,
            command=pending.command,
            output="".join(pending_outputs),
        ))
        pending = None
        pending_logged_ms = 0
        pending_outputs = []

    for entry in logs:
        rendered = entry.get("rendered", "")
        logged_ms = int(entry.get("logged_ms") or 0)

        # Try answer-command first since it also matches `❯ <cmd>` shape.
        ans = parse_answer(rendered)
        if ans is not None:
            _flush()
            submitted_answer = ans
            commands.append(CommandEntry(
                iso_ts=_extract_ts_only(rendered),
                logged_ms=logged_ms,
                command=f"answer --outcome {ans.outcome} <<EOF…EOF",
                output="",
            ))
            continue

        cmd = parse_command(rendered)
        if cmd is not None:
            _flush()
            pending = cmd
            pending_logged_ms = logged_ms
            continue

        g = parse_grader(rendered)
        if g is not None:
            _flush()
            grader = g
            continue

        # Output line — accumulate against the last command if any.
        if pending is not None:
            pending_outputs.append(strip_ansi(rendered))

    _flush()

    dump = TrialDump(
        trial_id=trial_id,
        source_url=harness_url,
        closed_ms=closed_ms,
        log_count=len(logs),
        commands=commands,
        submitted_answer=submitted_answer,
        grader=grader,
    )
    if include_raw_logs:
        dump.raw_logs = list(logs)
    return dump


def _extract_ts_only(rendered: str) -> str:
    plain = strip_ansi(rendered)
    m = re.match(r"^\[([^\]]+)\]", plain)
    return m.group(1).strip() if m else ""
