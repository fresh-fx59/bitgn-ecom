"""Tests for bench.divergence — tertiary keyword rule-surface analyzer."""
from bitgn_contest_agent.bench.divergence import (
    count_divergent_steps,
    is_divergent_step,
)


def test_authority_keyword_hits() -> None:
    text = "Reading AGENTS.md because the user instruction contradicts it..."
    assert is_divergent_step(text) is True


def test_inbox_keyword_hits() -> None:
    text = "Checking /inbox/identity.md before proceeding."
    assert is_divergent_step(text) is True


def test_erc3_keyword_hits() -> None:
    text = "This looks like an ERC3 query — I'll check balances first."
    assert is_divergent_step(text) is True


def test_benign_step_is_not_divergent() -> None:
    text = "Searching the filesystem for the reference file."
    assert is_divergent_step(text) is False


def test_empty_text_is_not_divergent() -> None:
    assert is_divergent_step("") is False
    assert is_divergent_step(None) is False


def test_count_divergent_steps_tallies_hits() -> None:
    texts = [
        "Reading AGENTS.md first.",
        "Listing the sandbox.",
        "Checking /inbox/identity.md.",
        None,
        "",
    ]
    assert count_divergent_steps(texts) == 2
