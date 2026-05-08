"""Integration tests for the format validation hook in the agent loop.

Tests the hook logic that fires after write tool calls and injects
FORMAT VALIDATION ERROR messages when YAML frontmatter is malformed.
"""
from __future__ import annotations

from types import SimpleNamespace

from bitgn_contest_agent.backend.base import Message
from bitgn_contest_agent.format_validator import validate_yaml_frontmatter


def _run_hook(fn, tool_result_ok: bool, messages: list) -> None:
    """Replicate the agent loop's format validation hook logic.

    This mirrors the code in agent.py lines 384-410 so we can test
    the hook's behavior without standing up a full agent loop.
    """
    if getattr(fn, "tool", "") == "write" and tool_result_ok:
        write_content = ""
        if hasattr(fn, "content"):
            write_content = fn.content
        elif hasattr(fn, "model_dump"):
            write_content = fn.model_dump().get("content", "")
        if write_content:
            val_result = validate_yaml_frontmatter(write_content)
            if not val_result.ok:
                write_path = getattr(fn, "path", "<unknown>")
                error_msg = (
                    f"FORMAT VALIDATION ERROR in your last write:\n"
                    f"  File: {write_path}\n"
                    f"  Error: {val_result.error}\n"
                )
                if val_result.line is not None:
                    error_msg += f"  Line: {val_result.line}\n"
                error_msg += "\nFix the error and rewrite the file."
                messages.append(
                    Message(role="user", content=error_msg)
                )


def test_hook_injects_error_on_bad_yaml_write() -> None:
    """Write with unquoted colon triggers FORMAT VALIDATION ERROR injection."""
    fn = SimpleNamespace(
        tool="write",
        path="/sandbox/60_outbox/outbox/eml_2026-03-30.md",
        content=(
            "---\n"
            "record_type: outbound_email\n"
            "subject: Re: Invoice request\n"
            "---\n"
            "Body.\n"
        ),
    )
    messages: list[Message] = []
    _run_hook(fn, tool_result_ok=True, messages=messages)

    assert len(messages) == 1
    msg = messages[0]
    assert msg.role == "user"
    assert "FORMAT VALIDATION ERROR" in msg.content
    assert "/sandbox/60_outbox/outbox/eml_2026-03-30.md" in msg.content
    assert "Fix the error and rewrite the file" in msg.content


def test_hook_no_injection_on_valid_yaml_write() -> None:
    """Write with valid YAML should not inject any message."""
    fn = SimpleNamespace(
        tool="write",
        path="/sandbox/60_outbox/outbox/eml.md",
        content=(
            "---\n"
            'subject: "Re: Invoice request"\n'
            "---\n"
            "Body.\n"
        ),
    )
    messages: list[Message] = []
    _run_hook(fn, tool_result_ok=True, messages=messages)

    assert len(messages) == 0


def test_hook_skips_non_write_tools() -> None:
    """Hook only fires on tool=='write', not read or other tools."""
    fn = SimpleNamespace(
        tool="read",
        path="/sandbox/some/file.md",
        content="---\nbad: yaml: here\n---\n",
    )
    messages: list[Message] = []
    _run_hook(fn, tool_result_ok=True, messages=messages)

    assert len(messages) == 0


def test_hook_skips_failed_tool_result() -> None:
    """Hook should not fire when tool_result.ok is False."""
    fn = SimpleNamespace(
        tool="write",
        path="/sandbox/file.md",
        content="---\nbad: yaml: here\n---\n",
    )
    messages: list[Message] = []
    _run_hook(fn, tool_result_ok=False, messages=messages)

    assert len(messages) == 0


def test_hook_skips_plain_text_write() -> None:
    """Write with no frontmatter should not inject any message."""
    fn = SimpleNamespace(
        tool="write",
        path="/sandbox/notes.txt",
        content="Just plain text, no frontmatter.",
    )
    messages: list[Message] = []
    _run_hook(fn, tool_result_ok=True, messages=messages)

    assert len(messages) == 0


def test_hook_includes_line_number_in_error() -> None:
    """Error message should include the line number from the validator."""
    fn = SimpleNamespace(
        tool="write",
        path="/sandbox/file.md",
        content=(
            "---\n"
            "key1: value1\n"
            "subject: Re: Something bad\n"
            "---\n"
        ),
    )
    messages: list[Message] = []
    _run_hook(fn, tool_result_ok=True, messages=messages)

    assert len(messages) == 1
    assert "Line:" in messages[0].content
