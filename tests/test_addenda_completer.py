"""Tests for addenda_completer (catalogue-count addendum sweep)."""
from __future__ import annotations

import pytest

from bitgn_contest_agent.addenda_completer import (
    complete_addenda_refs,
    _is_catalogue_count_task,
)


def test_is_catalogue_count_task_match():
    s = (
        "How many catalogue products are Pliers and Wrenches? "
        "Answer in exactly format \"<COUNT:%d>\" (no quotes)."
    )
    tok = _is_catalogue_count_task(s)
    assert tok is not None
    full, no_and = tok.split("|")
    assert "pliers-and-wrenches" == full
    assert "pliers-wrenches" == no_and


def test_is_catalogue_count_task_no_and():
    s = "How many catalogue products are Hammers?"
    tok = _is_catalogue_count_task(s)
    assert tok is not None
    full, no_and = tok.split("|")
    assert full == "hammers"
    assert no_and == "hammers"


def test_is_not_count_task():
    assert _is_catalogue_count_task(
        "How many of these products have at least 1 items available?"
    ) is None
    assert _is_catalogue_count_task(
        "Apply a discount to basket_001."
    ) is None


def _fake_tree(file_map: dict[str, list[str]]):
    """file_map: {dir_root: [paths under it]} → emulate tree text."""
    def run_tree(root: str, level: int) -> str | None:
        paths = file_map.get(root)
        if paths is None:
            return None
        return "\n".join(paths)
    return run_tree


def test_completer_adds_matching_addenda():
    files = {
        "/docs/ops-policy-notes": [
            "/docs/ops-policy-notes/catalogue-count-pliers-wrenches-fam-hand-tools-pliers-wrenches-0011-aaaaaaaa-2021-08-09.md",
            "/docs/ops-policy-notes/catalogue-count-pliers-wrenches-fam-hand-tools-pliers-wrenches-0022-2oxrzl9r-2021-08-09.md",
            "/docs/ops-policy-notes/catalogue-count-hammers-fam-hand-tools-hammers-0003-xxxxx.md",
        ],
        "/docs/current-updates": [],
        "/docs/policy-updates": [],
        "/docs/catalogue-addenda": [],
        "/docs/clarifications": [],
    }
    res = complete_addenda_refs(
        task_text=(
            "How many catalogue products are Pliers and Wrenches?"
        ),
        refs=[
            "/AGENTS.MD",
            "/docs/ops-policy-notes/catalogue-count-pliers-wrenches-fam-hand-tools-pliers-wrenches-0011-aaaaaaaa-2021-08-09.md",
        ],
        run_tree=_fake_tree(files),
    )
    # Pliers-wrenches 0011 was already cited
    # Pliers-wrenches 0022 should be added
    # Hammers should NOT be added (different category)
    added = res.added
    assert (
        "/docs/ops-policy-notes/catalogue-count-pliers-wrenches-fam-hand-tools-pliers-wrenches-0022-2oxrzl9r-2021-08-09.md"
        in added
    )
    assert not any("hammers" in a for a in added)
    assert res.aborted is False


def test_completer_no_op_when_all_cited():
    files = {
        "/docs/ops-policy-notes": [
            "/docs/ops-policy-notes/catalogue-count-hammers-fam-hand-tools-hammers-0001.md",
        ],
        "/docs/current-updates": [],
        "/docs/policy-updates": [],
        "/docs/catalogue-addenda": [],
        "/docs/clarifications": [],
    }
    res = complete_addenda_refs(
        task_text="How many catalogue products are Hammers?",
        refs=[
            "/docs/ops-policy-notes/catalogue-count-hammers-fam-hand-tools-hammers-0001.md",
        ],
        run_tree=_fake_tree(files),
    )
    assert res.added == []


def test_completer_abstains_on_non_count_task():
    res = complete_addenda_refs(
        task_text="Apply a 10% discount to basket_001.",
        refs=[],
        run_tree=lambda root, level: "",
    )
    assert res.aborted is True


def test_completer_handles_compound_category_no_and():
    """The grader sometimes drops 'and' from the kebab form
    ('Cordless Saw and Sander' → 'cordless-saw-sander')."""
    files = {
        "/docs/ops-policy-notes": [
            "/docs/ops-policy-notes/catalogue-count-cordless-saw-sander-fam-power-tools-cordless-saw-sander-0011-aaa.md",
            "/docs/ops-policy-notes/catalogue-count-cordless-saw-sander-fam-power-tools-cordless-saw-sander-0014-bbb.md",
        ],
        "/docs/current-updates": [],
        "/docs/policy-updates": [],
        "/docs/catalogue-addenda": [],
        "/docs/clarifications": [],
    }
    res = complete_addenda_refs(
        task_text=(
            "How many catalogue products are Cordless Saw and Sander? "
            "Answer in exactly format \"<COUNT:%d>\" (no quotes)."
        ),
        refs=[],
        run_tree=_fake_tree(files),
    )
    assert len(res.added) == 2


def test_completer_matches_catalogue_counting_variant():
    """The contest also uses 'catalogue-counting-...' filename
    prefix (no 'and')."""
    files = {
        "/docs/ops-policy-notes": [],
        "/docs/current-updates": [
            "/docs/current-updates/catalogue-counting-2021-08-09-manual-garden-tools-fam-garden-tools-manual-garden-tools-0007-x.md",
        ],
        "/docs/policy-updates": [],
        "/docs/catalogue-addenda": [],
        "/docs/clarifications": [],
    }
    res = complete_addenda_refs(
        task_text="How many catalogue products are Manual Garden Tools?",
        refs=[],
        run_tree=_fake_tree(files),
    )
    assert len(res.added) == 1


def test_completer_abstains_when_no_dirs_exist():
    """When tree returns None for every candidate dir, abstain."""
    res = complete_addenda_refs(
        task_text="How many catalogue products are Hammers?",
        refs=[],
        run_tree=lambda root, level: None,
    )
    assert res.aborted is True
