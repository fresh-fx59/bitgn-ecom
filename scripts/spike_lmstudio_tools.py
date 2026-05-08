"""Verification spike — does lmstudio-python SDK surface tool_calls for a
one-shot (non-act) request when tools are passed via ``config.rawTools``?

This is the blocker for T2 (transport swap): if rawTools does NOT surface
tool_calls on respond()/respond_stream(), T2 is not viable without giving
up our agent.py orchestration (act() runs an internal agent loop we can't
plug our validator/orchestrator into).

Acceptance: the PredictionResult (or stream fragments) must expose a
tool_call with a known function name and parseable JSON arguments.

Run:   .venv/bin/python scripts/spike_lmstudio_tools.py
"""
from __future__ import annotations

import json as _json
import sys
import time

import lmstudio as lms


MODEL = "qwen3.5-35b-a3b"
HOST = "localhost:1236"

TOOL_SCHEMA = {
    "type": "toolArray",
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": "Read a file and return its contents.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Absolute path of the file to read.",
                        },
                    },
                    "required": ["path"],
                },
            },
        },
    ],
    "force": True,
}

PROMPT = (
    "You have one tool: read(path: str). The user asked: "
    "'Read /tmp/foo.txt and summarize it.' "
    "Emit exactly one tool_call to the read tool for that path. "
    "Do not reply with prose."
)


def dump(label: str, obj: object) -> None:
    print(f"--- {label} ---")
    try:
        print(_json.dumps(obj, indent=2, default=str))
    except Exception:
        print(repr(obj))


def main() -> int:
    client = lms.Client(HOST)
    model = client.llm.model(MODEL)
    print(f"[spike] connected to {HOST}, model={MODEL}")

    config = {
        "rawTools": TOOL_SCHEMA,
        "maxTokens": 512,
        "temperature": 0.0,
    }

    print("[spike] phase 1 — respond() with rawTools + force=True")
    t0 = time.monotonic()
    result = model.respond(PROMPT, config=config)
    elapsed = time.monotonic() - t0
    print(f"[spike] respond() returned in {elapsed:.2f}s")
    print()

    # PredictionResult shape
    print(f"type(result) = {type(result).__name__}")
    print(f"dir(result)  = {[a for a in dir(result) if not a.startswith('_')]}")
    print()

    # Inspect key attrs
    for attr in ("content", "parsed", "structured", "reasoning",
                 "tool_calls", "stats", "model_info"):
        if hasattr(result, attr):
            val = getattr(result, attr)
            preview = str(val)
            if len(preview) > 400:
                preview = preview[:400] + "…"
            print(f"result.{attr}: {preview}")
    print()

    # Try to access raw protocol data
    for attr in ("as_dict", "to_dict", "model_dump"):
        if hasattr(result, attr):
            fn = getattr(result, attr)
            if callable(fn):
                try:
                    dump(f"result.{attr}()", fn())
                except Exception as exc:
                    print(f"result.{attr}() raised: {exc!r}")

    # ---------------- Phase 2: stream + fragment inspection ----------------
    print()
    print("[spike] phase 2 — respond_stream() with fragment callback")
    fragments: list = []

    def on_frag(frag):
        fragments.append(frag)

    stream = model.respond_stream(
        PROMPT,
        config=config,
        on_prediction_fragment=on_frag,
    )
    for _ in stream:
        pass
    final = stream.result()
    print(f"[spike] stream produced {len(fragments)} fragments")

    # Peek at the shape of one fragment
    if fragments:
        f0 = fragments[0]
        print(f"fragment[0] type = {type(f0).__name__}")
        print(f"fragment[0] dir  = {[a for a in dir(f0) if not a.startswith('_')]}")
        for attr in ("content", "reasoning", "tool_call", "tool_calls",
                     "role", "text"):
            if hasattr(f0, attr):
                print(f"fragment[0].{attr} = {getattr(f0, attr)!r}")

    # Look for tool-call fragments specifically
    tc_like = [f for f in fragments
               if any("tool" in a.lower() for a in dir(f) if not a.startswith('_'))]
    print(f"[spike] fragments with any 'tool*' attr: {len(tc_like)}")

    # Final stream result shape
    print()
    print(f"type(final) = {type(final).__name__}")
    for attr in ("content", "tool_calls", "reasoning"):
        if hasattr(final, attr):
            val = getattr(final, attr)
            preview = str(val)
            if len(preview) > 400:
                preview = preview[:400] + "…"
            print(f"final.{attr}: {preview}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
