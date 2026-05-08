"""Unit tests for the body preservation hook in the agent loop.

Tests _extract_body and the hook logic that fires after writes to
previously-read non-outbox files, detecting body text mutations.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from bitgn_contest_agent.agent import _extract_body
from bitgn_contest_agent.backend.base import Message


# --- _extract_body tests ---


def test_extract_body_with_frontmatter():
    content = "---\nsubject: Hello\n---\nBody text here.\n"
    assert _extract_body(content) == "Body text here.\n"


def test_extract_body_no_frontmatter():
    assert _extract_body("Just plain text.") == ""


def test_extract_body_empty():
    assert _extract_body("") == ""


def test_extract_body_only_opening_delimiter():
    assert _extract_body("---\nsubject: Hello\n") == ""


def test_extract_body_multiline_body():
    content = "---\ntitle: Test\n---\nLine 1\nLine 2\nLine 3\n"
    assert _extract_body(content) == "Line 1\nLine 2\nLine 3\n"


def test_extract_body_body_starting_with_heading():
    content = "---\ntitle: Test\n---\n# Heading\nParagraph.\n"
    assert _extract_body(content) == "# Heading\nParagraph.\n"


def test_extract_body_preserves_exact_whitespace():
    """Body whitespace must be preserved byte-for-byte."""
    body = "  indented\n\n  double blank\n"
    content = f"---\nk: v\n---\n{body}"
    assert _extract_body(content) == body


# --- Hook logic tests ---


def _run_body_hook(
    write_path: str,
    write_content: str,
    read_cache: dict,
    messages: list,
) -> None:
    """Replicate the body preservation hook from agent.py."""
    fn = SimpleNamespace(tool="write", path=write_path, content=write_content)
    tool_result_ok = True

    if getattr(fn, "tool", "") == "write" and tool_result_ok:
        wp = getattr(fn, "path", "")
        if (
            wp
            and wp in read_cache
            and "outbox" not in wp.lower()
        ):
            new_content = fn.content
            cached = read_cache[wp]
            old_body = _extract_body(cached)
            if not old_body and not cached.startswith("---"):
                old_body = cached
            new_body = _extract_body(new_content)
            if old_body and new_body and old_body != new_body:
                body_msg = (
                    f"BODY PRESERVATION ERROR in your last write:\n"
                    f"  File: {wp}\n"
                    f"  The original body text was altered during migration.\n"
                    f"  Expected body length: {len(old_body)} chars\n"
                    f"  Actual body length:   {len(new_body)} chars\n"
                    f"\nRe-read the file and rewrite it, preserving the EXACT "
                    f"original body below the closing `---` delimiter. "
                    f"No extra blank lines, no reformatting."
                )
                messages.append(Message(role="user", content=body_msg))


def test_hook_fires_on_body_mismatch():
    original = "---\ntitle: Old\n---\n# Heading\nOriginal body.\n"
    rewritten = "---\ntitle: New\ndate: 2024-01-01\n---\n\n# Heading\nOriginal body.\n"
    messages: list = []
    _run_body_hook("notes/doc.md", rewritten, {"notes/doc.md": original}, messages)
    assert len(messages) == 1
    assert "BODY PRESERVATION ERROR" in messages[0].content


def test_hook_silent_on_matching_body():
    original = "---\ntitle: Old\n---\n# Heading\nBody.\n"
    rewritten = "---\ntitle: New\ndate: 2024-01-01\n---\n# Heading\nBody.\n"
    messages: list = []
    _run_body_hook("notes/doc.md", rewritten, {"notes/doc.md": original}, messages)
    assert len(messages) == 0


def test_hook_skips_outbox_paths():
    original = "---\nsubject: Hi\n---\nBody.\n"
    rewritten = "---\nsubject: Hi\nto: x\n---\nDifferent body.\n"
    messages: list = []
    _run_body_hook("outbox/email.md", rewritten, {"outbox/email.md": original}, messages)
    assert len(messages) == 0


def test_hook_skips_uncached_paths():
    rewritten = "---\ntitle: New\n---\nBody.\n"
    messages: list = []
    _run_body_hook("notes/doc.md", rewritten, {}, messages)
    assert len(messages) == 0


def test_hook_fires_when_original_has_no_frontmatter_and_body_altered():
    """Original file without frontmatter — entire content is the body."""
    original = "Just plain text, no frontmatter."
    rewritten = "---\ntitle: New\n---\nDifferent text.\n"
    messages: list = []
    _run_body_hook("notes/doc.md", rewritten, {"notes/doc.md": original}, messages)
    assert len(messages) == 1
    assert "BODY PRESERVATION ERROR" in messages[0].content


def test_hook_silent_when_original_has_no_frontmatter_and_body_preserved():
    """Original without frontmatter, body preserved verbatim."""
    original = "Just plain text, no frontmatter."
    rewritten = "---\ntitle: New\n---\nJust plain text, no frontmatter."
    messages: list = []
    _run_body_hook("notes/doc.md", rewritten, {"notes/doc.md": original}, messages)
    assert len(messages) == 0


def test_hook_detects_extra_newline():
    """The exact t066 failure: extra blank line between --- and body."""
    body = "# Meeting Notes\nDiscussed budget.\n"
    original = f"---\ntitle: Old\n---\n{body}"
    rewritten = f"---\ntitle: New\ndate: 2024-01-01\n---\n\n{body}"  # extra \n
    messages: list = []
    _run_body_hook("notes/doc.md", rewritten, {"notes/doc.md": original}, messages)
    assert len(messages) == 1
    assert "BODY PRESERVATION ERROR" in messages[0].content
