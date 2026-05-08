"""Pre-write YAML frontmatter validation — rejects bad writes before
dispatching to PCM, preventing duplicate-write grader violations."""
from __future__ import annotations

from unittest.mock import MagicMock

from bitgn_contest_agent.adapter.pcm import PcmAdapter
from bitgn_contest_agent.schemas import Req_Write


def _mk_adapter():
    runtime = MagicMock()
    runtime.write.return_value = MagicMock()
    return runtime, PcmAdapter(runtime=runtime, max_tool_result_bytes=65536)


def test_valid_yaml_frontmatter_dispatches_to_pcm():
    runtime, adapter = _mk_adapter()
    content = (
        "---\n"
        "record_type: outbound_email\n"
        "subject: Hello world\n"
        "---\n"
        "Body.\n"
    )
    req = Req_Write(tool="write", path="60_outbox/test.md", content=content)
    result = adapter.dispatch(req)
    assert result.ok is True
    runtime.write.assert_called_once()


def test_invalid_yaml_frontmatter_rejected_before_dispatch():
    runtime, adapter = _mk_adapter()
    # Unquoted colon in `subject:` value — the t071 regression.
    content = (
        "---\n"
        "record_type: outbound_email\n"
        "subject: Re: Invoice request\n"
        "---\n"
        "Body.\n"
    )
    req = Req_Write(tool="write", path="60_outbox/test.md", content=content)
    result = adapter.dispatch(req)
    assert result.ok is False
    assert result.error_code == "FORMAT_INVALID"
    assert "YAML" in (result.error or "")
    runtime.write.assert_not_called()


def test_content_without_frontmatter_dispatches_without_validation():
    runtime, adapter = _mk_adapter()
    req = Req_Write(tool="write", path="note.md", content="Just prose, no YAML.\n")
    result = adapter.dispatch(req)
    assert result.ok is True
    runtime.write.assert_called_once()


def test_format_pre_write_reject_arch_emitted(monkeypatch):
    """Verify a FORMAT_PRE_WRITE_REJECT arch event is emitted on reject."""
    import bitgn_contest_agent.adapter.pcm as pcm_mod
    captured = []

    def _capture(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(pcm_mod, "emit_arch", _capture)

    runtime, adapter = _mk_adapter()
    content = (
        "---\n"
        "subject: Re: broken\n"
        "---\n"
    )
    req = Req_Write(tool="write", path="60_outbox/bad.md", content=content)
    result = adapter.dispatch(req)
    assert result.ok is False
    assert any(
        str(k.get("category")) == "FORMAT_PRE_WRITE_REJECT"
        for k in captured
    ), f"no FORMAT_PRE_WRITE_REJECT emitted, got: {captured}"
