"""Self-learning integration stub (spec §13).

The signature is the contract. All future persistent-memory writes
from the bitgn agent — proposed new matchers, skill body updates,
self-corrections — must go through this one function. The M0 body is
a no-op; when the next project lands, the intent-vs-request gate
described in §13 is attached here.

Signature drift fails CI via tests/test_learning_stub.py so the
integration point stays stable.
"""
from __future__ import annotations

from typing import Any, Mapping


def persist_learning(kind: str, payload: Mapping[str, Any]) -> None:
    """Record a proposed learning artifact.

    Args:
        kind: a short identifier for the learning shape, e.g.
            "new_matcher", "skill_body_patch", "router_miscategorization".
        payload: structured data describing the artifact.

    Returns:
        None. M0 is a no-op; later milestones may persist to disk or
        a memory store subject to the intent-vs-request gate.
    """
    # Intentionally a no-op. See module docstring.
    return None
