"""Signature tests for the persist_learning() stub.

Signature drift fails CI so the self-learning integration point stays
wired up across milestones.
"""
from __future__ import annotations

import inspect

from bitgn_contest_agent import learning


def test_persist_learning_exists() -> None:
    assert callable(learning.persist_learning)


def test_persist_learning_signature() -> None:
    sig = inspect.signature(learning.persist_learning)
    params = list(sig.parameters)
    assert params == ["kind", "payload"], params


def test_persist_learning_is_a_noop_in_m0() -> None:
    result = learning.persist_learning(kind="test", payload={"x": 1})
    assert result is None
