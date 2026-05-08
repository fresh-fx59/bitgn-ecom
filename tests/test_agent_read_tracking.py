"""Agent populates Session.attempted_reads / verified_absent on every read."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from bitgn_contest_agent.session import Session


@dataclass
class _FakeResult:
    ok: bool
    refs: Tuple[str, ...] = ()
    error: str | None = None
    error_code: str | None = None
    content: str | None = None
    truncated: bool = False
    wall_ms: int = 0


def _record_read(session: Session, path: str, result: _FakeResult) -> None:
    """Mirror of the logic the agent loop runs on every read dispatch."""
    from bitgn_contest_agent.agent import _record_read_attempt  # noqa: WPS433
    _record_read_attempt(session, path, result)


def test_successful_read_adds_to_attempted_only() -> None:
    session = Session()
    result = _FakeResult(ok=True, refs=("AGENTS.md",))
    _record_read(session, "AGENTS.md", result)
    assert "AGENTS.md" in session.attempted_reads
    assert "AGENTS.md" not in session.verified_absent
    # seen_refs is populated by the agent loop, not the helper
    assert session.seen_refs == set()


def test_not_found_read_adds_to_attempted_and_verified_absent() -> None:
    session = Session()
    result = _FakeResult(
        ok=False, error="file not found", error_code="UNKNOWN"
    )
    _record_read(session, "00_inbox/absent.md", result)
    assert "00_inbox/absent.md" in session.attempted_reads
    assert "00_inbox/absent.md" in session.verified_absent
    assert "00_inbox/absent.md" not in session.seen_refs


def test_other_read_error_adds_only_to_attempted() -> None:
    session = Session()
    result = _FakeResult(
        ok=False, error="permission denied", error_code="UNKNOWN"
    )
    _record_read(session, "private/x.md", result)
    assert "private/x.md" in session.attempted_reads
    assert "private/x.md" not in session.verified_absent
    assert "private/x.md" not in session.seen_refs
