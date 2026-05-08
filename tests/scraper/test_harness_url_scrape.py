"""Tests for harness_url_scrape — fetch + parse trial transcript JSON."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bitgn_scraper import harness_url_scrape as hus

FIXTURE = Path(__file__).parent / "fixtures" / "t001_harness_url.json"


def _load_fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def _stub_fetch(payload: dict):
    def _fn(url: str) -> bytes:
        return json.dumps(payload).encode("utf-8")
    return _fn


def test_strip_ansi_removes_escape_sequences():
    s = "\x1b[90m[ts] \x1b[0m\x1b[36m\u276f\x1b[0m tree\n"
    assert hus.strip_ansi(s) == "[ts] \u276f tree\n"


def test_extract_trial_id_from_url():
    assert hus.extract_trial_id_from_url(
        "https://vm-03pb97nxparkzg3ofp.eu.bitgn.com/"
    ) == "vm-03pb97nxparkzg3ofp"
    assert hus.extract_trial_id_from_url(
        "https://abc123.eu.bitgn.com"
    ) == "abc123"


def test_parse_command_extracts_text_and_iso_timestamp():
    rendered = "\x1b[90m[2026-04-26T20:50:11.94Z] \x1b[0m\x1b[36m\u276f\x1b[0m tree\n"
    parsed = hus.parse_command(rendered)
    assert parsed is not None
    assert parsed.iso_ts == "2026-04-26T20:50:11.94Z"
    assert parsed.command == "tree"


def test_parse_command_returns_none_when_not_a_command():
    assert hus.parse_command("[  OK  ] AnswerProvided\n") is None
    assert hus.parse_command("plain output line\n") is None


def test_parse_grader_extracts_score_and_expected():
    rendered = (
        "\x1b[31m[ ERR  ]\x1b[0m AI agent score 0.00\n"
        "\x1b[90m        \x1b[0m answer is incorrect. Expected: '18-04-2026'\n"
    )
    g = hus.parse_grader(rendered)
    assert g is not None
    assert g.score == 0.0
    assert g.message == "answer is incorrect. Expected: '18-04-2026'"
    assert g.expected == "18-04-2026"


def test_parse_grader_handles_correct_score_without_expected():
    rendered = "\x1b[32m[  OK  ]\x1b[0m AI agent score 1.00\n"
    g = hus.parse_grader(rendered)
    assert g is not None
    assert g.score == 1.0
    assert g.expected is None


def test_parse_answer_extracts_heredoc_body():
    rendered = (
        "\x1b[90m[2026-04-26T20:50:25.49Z] \x1b[0m\x1b[36m\u276f\x1b[0m "
        "answer --outcome ok <<'EOF'\n\x1b[33malpha\n\x1b[0mEOF\n"
    )
    a = hus.parse_answer(rendered)
    assert a is not None
    assert a.outcome == "ok"
    assert a.text == "alpha"


def test_parse_answer_returns_none_for_non_answer():
    assert hus.parse_answer("\u276f tree\n") is None


def test_fetch_trial_data_against_fixture():
    payload = _load_fixture()
    dump = hus.fetch_trial_data(
        "https://vm-03pb97nxparkzg3ofp.eu.bitgn.com/",
        http_get=_stub_fetch(payload),
    )
    assert dump.trial_id == "vm-03pb97nxparkzg3ofp"
    assert dump.source_url == "https://vm-03pb97nxparkzg3ofp.eu.bitgn.com/"
    assert dump.closed_ms == payload["closed_ms"]
    assert dump.is_closed is True
    assert dump.log_count == len(payload["logs"])

    # Commands extracted: tree, ~120 cats, answer
    assert len(dump.commands) >= 100
    assert dump.commands[0].command == "tree"

    # Submitted answer captured
    assert dump.submitted_answer is not None
    assert dump.submitted_answer.text == "alpha"
    assert dump.submitted_answer.outcome == "ok"

    # Grader feedback captured
    assert dump.grader is not None
    assert dump.grader.score == 0.0
    assert dump.grader.expected == "18-04-2026"


def test_fetch_trial_data_uses_format_json_query():
    captured = {}
    payload = _load_fixture()

    def _capture(url):
        captured["url"] = url
        return json.dumps(payload).encode("utf-8")

    hus.fetch_trial_data(
        "https://vm-03pb97nxparkzg3ofp.eu.bitgn.com/",
        http_get=_capture,
    )
    assert "format=json" in captured["url"]


def test_fetch_trial_data_open_trial_has_is_closed_false():
    payload = dict(_load_fixture())
    payload["closed_ms"] = 0
    dump = hus.fetch_trial_data(
        "https://vm-test.eu.bitgn.com/",
        http_get=_stub_fetch(payload),
    )
    assert dump.is_closed is False


def test_to_dict_round_trips_json():
    payload = _load_fixture()
    dump = hus.fetch_trial_data(
        "https://vm-03pb97nxparkzg3ofp.eu.bitgn.com/",
        http_get=_stub_fetch(payload),
    )
    serialized = dump.to_dict()
    # Must be JSON-encodable
    re_parsed = json.loads(json.dumps(serialized))
    assert re_parsed["trial_id"] == "vm-03pb97nxparkzg3ofp"
    assert re_parsed["grader"]["score"] == 0.0
    assert re_parsed["submitted_answer"]["text"] == "alpha"
    assert isinstance(re_parsed["commands"], list)
