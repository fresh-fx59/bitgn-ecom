"""§5.2 Test 3 — adapter.dispatch maps each Req_* to the right method
and proto request shape."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from bitgn.vm import pcm_pb2

from bitgn_contest_agent.adapter.pcm import PcmAdapter, ToolResult
from bitgn_contest_agent.schemas import (
    ReportTaskCompletion,
    Req_Context,
    Req_Delete,
    Req_Find,
    Req_List,
    Req_MkDir,
    Req_Move,
    Req_Read,
    Req_Search,
    Req_Tree,
    Req_Write,
)


def _mk_adapter(runtime: MagicMock) -> PcmAdapter:
    return PcmAdapter(runtime=runtime, max_tool_result_bytes=16384)


def test_dispatch_read_calls_runtime_read_with_path() -> None:
    runtime = MagicMock()
    # Plan deviation: use a real ReadResponse proto rather than a MagicMock,
    # because _response_to_text uses MessageToJson which yields "{}" for a
    # MagicMock (no descriptor fields) and wraps real protos as
    # {"content": "..."}. Both facts are checked below.
    runtime.read.return_value = pcm_pb2.ReadResponse(content="file contents")

    adapter = _mk_adapter(runtime)
    result = adapter.dispatch(Req_Read(tool="read", path="AGENTS.md"))

    runtime.read.assert_called_once()
    sent = runtime.read.call_args.args[0]
    assert isinstance(sent, pcm_pb2.ReadRequest)
    assert sent.path == "AGENTS.md"
    assert result.ok
    assert "file contents" in result.content
    assert result.refs == ("AGENTS.md",)


def test_dispatch_write_passes_path_and_content() -> None:
    runtime = MagicMock()
    runtime.write.return_value = MagicMock()

    adapter = _mk_adapter(runtime)
    result = adapter.dispatch(Req_Write(tool="write", path="/tmp/a", content="hi"))

    sent = runtime.write.call_args.args[0]
    assert isinstance(sent, pcm_pb2.WriteRequest)
    assert sent.path == "/tmp/a"
    assert sent.content == "hi"
    assert result.ok


def test_dispatch_delete() -> None:
    runtime = MagicMock()
    adapter = _mk_adapter(runtime)
    adapter.dispatch(Req_Delete(tool="delete", path="/tmp/a"))
    sent = runtime.delete.call_args.args[0]
    assert isinstance(sent, pcm_pb2.DeleteRequest)
    assert sent.path == "/tmp/a"


def test_dispatch_mkdir_uses_mk_dir_method_name() -> None:
    runtime = MagicMock()
    adapter = _mk_adapter(runtime)
    adapter.dispatch(Req_MkDir(tool="mkdir", path="/tmp/new"))
    # PcmRuntimeClientSync method is mk_dir (snake_case), not mkdir.
    sent = runtime.mk_dir.call_args.args[0]
    assert isinstance(sent, pcm_pb2.MkDirRequest)
    assert sent.path == "/tmp/new"


def test_dispatch_move_maps_from_name_and_to_name() -> None:
    runtime = MagicMock()
    adapter = _mk_adapter(runtime)
    adapter.dispatch(Req_Move(tool="move", from_name="src", to_name="dst"))
    sent = runtime.move.call_args.args[0]
    assert isinstance(sent, pcm_pb2.MoveRequest)
    assert sent.from_name == "src"
    assert sent.to_name == "dst"


def test_dispatch_list_maps_name_field() -> None:
    runtime = MagicMock()
    adapter = _mk_adapter(runtime)
    adapter.dispatch(Req_List(tool="list", name="/"))
    sent = runtime.list.call_args.args[0]
    assert isinstance(sent, pcm_pb2.ListRequest)
    assert sent.name == "/"


def test_dispatch_tree_maps_root_field() -> None:
    runtime = MagicMock()
    adapter = _mk_adapter(runtime)
    adapter.dispatch(Req_Tree(tool="tree", root="/"))
    sent = runtime.tree.call_args.args[0]
    assert isinstance(sent, pcm_pb2.TreeRequest)
    assert sent.root == "/"


def test_dispatch_find_maps_type_enum() -> None:
    runtime = MagicMock()
    adapter = _mk_adapter(runtime)
    adapter.dispatch(
        Req_Find(tool="find", root="/", name="*.py", type="TYPE_FILES", limit=10)
    )
    sent = runtime.find.call_args.args[0]
    assert isinstance(sent, pcm_pb2.FindRequest)
    assert sent.root == "/"
    assert sent.name == "*.py"
    assert sent.type == pcm_pb2.FindRequest.TYPE_FILES
    assert sent.limit == 10


def test_dispatch_search() -> None:
    runtime = MagicMock()
    adapter = _mk_adapter(runtime)
    adapter.dispatch(Req_Search(tool="search", root="/", pattern="TODO", limit=25))
    sent = runtime.search.call_args.args[0]
    assert isinstance(sent, pcm_pb2.SearchRequest)
    assert sent.root == "/"
    assert sent.pattern == "TODO"
    assert sent.limit == 25


def test_dispatch_search_preamble_carries_total_matches() -> None:
    """t30-class counting tasks fail because the JSON matches[] array is
    truncated at max_tool_result_bytes before the agent can count it. The
    adapter must stamp an explicit `total_matches` field at the TOP of the
    search response so the exact count survives truncation. The count is
    the length of the server-returned matches list — exact when the result
    isn't limit-capped; treated as a lower bound when it is."""
    runtime = MagicMock()
    resp = pcm_pb2.SearchResponse()
    # 5 fake matches, well under any limit.
    for i in range(5):
        m = resp.matches.add()
        m.path = f"docs/file{i}.txt"
        m.line = i + 1
        m.line_text = f"line content {i}"
    runtime.search.return_value = resp

    adapter = _mk_adapter(runtime)
    result = adapter.dispatch(
        Req_Search(tool="search", root="/", pattern="line", limit=1000)
    )
    assert result.ok
    # total_matches must be present in the response body.
    assert '"total_matches": 5' in result.content or '"total_matches":5' in result.content, (
        f"total_matches not in response: {result.content[:200]}"
    )
    # It must appear BEFORE the matches array so truncation can't hide it.
    idx_total = result.content.index("total_matches")
    idx_matches = result.content.index('"matches"')
    assert idx_total < idx_matches, (
        "total_matches must come before matches[] so it survives truncation"
    )


def test_dispatch_search_total_matches_survives_truncation() -> None:
    """Even when the matches array is aggressively truncated, the agent
    must still see the exact total in the response preamble."""
    runtime = MagicMock()
    resp = pcm_pb2.SearchResponse()
    # 400 matches, each with a long enough line_text that the JSON will
    # blow past 2 KB easily — forcing truncation under max_tool_result_bytes=2048.
    big_line = "x" * 100
    for i in range(400):
        m = resp.matches.add()
        m.path = f"d/f{i}.txt"
        m.line = i + 1
        m.line_text = big_line
    runtime.search.return_value = resp

    adapter = PcmAdapter(runtime=runtime, max_tool_result_bytes=2048)
    result = adapter.dispatch(
        Req_Search(tool="search", root="/", pattern="x", limit=10000)
    )
    assert result.ok
    assert result.truncated is True
    assert '"total_matches": 400' in result.content or '"total_matches":400' in result.content, (
        f"truncation hid total_matches, got: {result.content[:300]}"
    )


def test_dispatch_context_sends_empty_request() -> None:
    runtime = MagicMock()
    adapter = _mk_adapter(runtime)
    adapter.dispatch(Req_Context(tool="context"))
    sent = runtime.context.call_args.args[0]
    assert isinstance(sent, pcm_pb2.ContextRequest)


def test_submit_terminal_calls_answer_with_outcome_enum_and_refs() -> None:
    runtime = MagicMock()
    adapter = _mk_adapter(runtime)
    completion = ReportTaskCompletion(
        tool="report_completion",
        message="done",
        grounding_refs=["AGENTS.md", "README.md"],
        rulebook_notes="n",
        outcome_justification="j",
        completed_steps_laconic=["read", "report"],
        outcome="OUTCOME_OK",
    )
    adapter.submit_terminal(completion)
    sent = runtime.answer.call_args.args[0]
    assert isinstance(sent, pcm_pb2.AnswerRequest)
    assert sent.message == "done"
    assert sent.outcome == pcm_pb2.OUTCOME_OK
    assert list(sent.refs) == ["AGENTS.md", "README.md"]


def test_dispatch_truncates_large_responses() -> None:
    runtime = MagicMock()
    big = "x" * 100_000
    # Plan deviation (same reason as test_dispatch_read above): real proto
    # so MessageToJson yields a populated body instead of "{}".
    runtime.read.return_value = pcm_pb2.ReadResponse(content=big)
    adapter = PcmAdapter(runtime=runtime, max_tool_result_bytes=4096)
    result = adapter.dispatch(Req_Read(tool="read", path="big"))
    assert result.truncated is True
    assert result.bytes <= 4096
    # original_bytes is the pre-truncation JSON length, which includes the
    # {"content": "..."} framing — so it's strictly greater than len(big).
    assert result.original_bytes > len(big.encode("utf-8"))


def test_dispatch_rpc_failure_returns_error_result() -> None:
    runtime = MagicMock()
    runtime.read.side_effect = RuntimeError("backend down")
    adapter = _mk_adapter(runtime)
    result = adapter.dispatch(Req_Read(tool="read", path="AGENTS.md"))
    assert result.ok is False
    assert "backend down" in (result.error or "")
    assert result.error_code in {"PCM_ERROR", "UNKNOWN", "RPC_UNAVAILABLE"}
