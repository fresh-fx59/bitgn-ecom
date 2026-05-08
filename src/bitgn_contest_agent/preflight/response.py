"""Uniform {summary, data} JSON response shape for all preflight tools."""
from __future__ import annotations

import json
from typing import Any


def build_response(summary: str, data: dict[str, Any]) -> str:
    """Serialize a preflight response as compact JSON with unicode preserved."""
    return json.dumps(
        {"summary": summary, "data": data},
        ensure_ascii=False,
        separators=(", ", ": "),
    )
