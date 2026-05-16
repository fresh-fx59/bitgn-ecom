#!/usr/bin/env python3
"""Reconstruct local workspaces from BITGN_TRACE_RAW_RESPONSES dumps.

The raw dump (one ``ecom_responses.<pid>.jsonl`` per worker process)
records every protobuf request/response pair the agent saw on the
wire. For ``read`` responses the ``content`` field carries the live
file body — exactly what we need to rebuild a workspace snapshot for
local replay.

This script:

  1. Reads the per-task JSONL trace under ``logs/<bench-dir>/<ts>/
     <task_id>__run<N>.jsonl`` to get the task's instruction +
     ``ecom_op`` timestamp range.
  2. Walks every dump file under ``--dump-dir``, keeping only records
     whose ``ts`` falls inside the task's range.
  3. For each ``read`` response, writes ``response.content`` to the
     reconstructed workspace at the response's ``path``.
  4. For each ``list`` / ``tree`` response, materialises empty
     directories so the agent's later ``list`` calls see them.
  5. Generates ``metadata.json`` from the trace's task text + the
     trial outcome / grader score_detail.

Usage:

    scripts/rebuild_ws_from_raw.py \\
        --bench-log logs/ecom_rawcap_20260515T171653Z/20260515_171653 \\
        --dump-dir  artifacts/raw_dumps/full_20260515T171653Z \\
        --bench-summary artifacts/bench/<sha>_ecom_rawcap_*.json \\
        --task t23 --task t24 --task t29 --task t30 \\
        --output-root artifacts/ws_snapshots

The output directory layout matches scripts/local_bench.py's expected
``<output-root>/<task_id>_<descr>/run_0/{workspace,metadata.json}``.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


# Forbidden / required ref hints derived from the bench grader's
# score_detail string. Pattern:
#   "answer contains invalid reference '<path>'"
#   "answer missing required reference '<path>'"
#   "expected outcome OUTCOME_X, got OUTCOME_Y"
INVALID_REF_RE = re.compile(r"invalid reference ['\"]([^'\"]+)['\"]")
MISSING_REF_RE = re.compile(r"missing required reference ['\"]([^'\"]+)['\"]")
EXPECTED_OUTCOME_RE = re.compile(r"expected outcome (OUTCOME_\w+)")


@dataclass
class TaskTrace:
    """Per-task signature of every ecom_op the agent issued.

    Used to pull matching responses from the cross-task dump file
    (parallel benches interleave dump records across workers; the
    per-task JSONL trace is the only authoritative partition).
    """
    task_id: str
    instruction: str
    ts_first: str | None
    ts_last: str | None
    actor_id: str | None
    context_date: str | None
    # Multiset of (op, path, wall_ms) signatures. wall_ms is high-
    # resolution so collisions across concurrent tasks are rare; we
    # also key on op + path to narrow further. Counts let us consume
    # repeated identical ops in trace order.
    op_signatures: dict[tuple[str, str, int], int]


def _parse_iso(ts: str) -> datetime:
    # accept "...+00:00" and "...Z"
    s = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
    return datetime.fromisoformat(s)


def load_task_trace(jsonl_path: Path) -> TaskTrace:
    """Pull instruction + every ecom_op (op, path, wall_ms) signature
    from a per-task JSONL trace.

    Notes on the trace shape:
      - ecom_op records do NOT carry a `ts` field, only `wall_ms`
        (call duration). The signature multiset is therefore the
        only authoritative way to attribute dump records back to
        this task when --max-parallel > 1 interleaves them.
      - meta.started_at is recorded once at trial start and gives
        the lower bound of a permissive TS window we use to narrow
        the candidate pool before signature matching kicks in.
    """
    instruction = ""
    actor: str | None = None
    started_at: str | None = None
    sigs: dict[tuple[str, str, int], int] = defaultdict(int)
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            kind = rec.get("kind")
            if kind == "task":
                instruction = rec.get("task_text") or rec.get("instruction") or ""
            elif kind == "ecom_op":
                op = rec.get("op") or ""
                path = rec.get("path") or ""
                wall = int(rec.get("wall_ms") or 0)
                sigs[(op, path, wall)] += 1
            elif kind == "meta":
                started_at = rec.get("started_at") or started_at
    return TaskTrace(
        task_id=jsonl_path.stem.split("__", 1)[0],
        instruction=instruction,
        ts_first=started_at,
        ts_last=None,
        actor_id=actor,
        context_date=started_at,
        op_signatures=dict(sigs),
    )


def iter_dump_records(dump_dir: Path) -> Iterable[tuple[Path, dict]]:
    for f in sorted(dump_dir.glob("ecom_responses.*.jsonl")):
        with f.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    yield f, json.loads(line)
                except ValueError:
                    continue


def in_range(ts: str, lo: str | None, hi: str | None) -> bool:
    if lo is None or hi is None:
        return False
    try:
        t = _parse_iso(ts)
        return _parse_iso(lo) <= t <= _parse_iso(hi)
    except Exception:
        return False


def safe_path(root: Path, p: str) -> Path:
    """Resolve `p` (a ECOM-absolute path like /docs/security.md)
    under `root`. Reject anything that escapes the root."""
    rel = p.lstrip("/")
    resolved = (root / rel).resolve()
    root_resolved = root.resolve()
    if not str(resolved).startswith(str(root_resolved)):
        raise ValueError(f"path escapes workspace: {p}")
    return resolved


def write_response_file(workspace: Path, path: str, content: str) -> None:
    full = safe_path(workspace, path)
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")


def write_response_dir(workspace: Path, path: str) -> None:
    full = safe_path(workspace, path)
    full.mkdir(parents=True, exist_ok=True)


def materialise_workspace(
    workspace: Path,
    dump_records: list[dict],
) -> tuple[int, int]:
    """Apply every read/list/tree dump record to the workspace.

    Returns (files_written, dirs_materialised)."""
    files = 0
    dirs = 0
    for rec in dump_records:
        op = rec.get("op")
        ok = rec.get("ok")
        resp = rec.get("response") or {}
        if not ok:
            continue
        if op == "read":
            p = resp.get("path")
            content = resp.get("content", "")
            if p:
                try:
                    write_response_file(workspace, p, content)
                    files += 1
                except (ValueError, OSError):
                    continue
        elif op == "list":
            p = resp.get("path")
            entries = resp.get("entries") or []
            if p:
                try:
                    write_response_dir(workspace, p)
                except (ValueError, OSError):
                    continue
            for e in entries:
                ep = e.get("path") or ((p.rstrip("/") + "/" + e["name"]) if p else None)
                if not ep:
                    continue
                kind = e.get("kind", "")
                try:
                    if kind == "NODE_KIND_DIR":
                        write_response_dir(workspace, ep)
                        dirs += 1
                    else:
                        # Touch the file path as empty if no read covered it.
                        full = safe_path(workspace, ep)
                        if not full.exists():
                            full.parent.mkdir(parents=True, exist_ok=True)
                            full.write_text("", encoding="utf-8")
                except (ValueError, OSError):
                    continue
        elif op == "tree":
            # tree.root is a single Entry with nested children — flatten it
            root_entry = resp.get("root")
            if root_entry:
                _flatten_tree(workspace, "/", root_entry, files_dirs=[0, 0])
                # we don't separately count tree-materialised entries; the
                # subsequent list/read passes will refine them.
    return files, dirs


def _flatten_tree(
    workspace: Path,
    parent_path: str,
    entry: dict,
    files_dirs: list[int],
) -> None:
    """Recursively materialise an empty directory tree from a tree
    response. Children that already exist as files (from earlier read
    passes) are left alone."""
    name = entry.get("name", "")
    kind = entry.get("kind", "")
    children = entry.get("children") or []
    if name == "/" or name == "":
        cur = parent_path or "/"
    else:
        cur = (parent_path.rstrip("/") + "/" + name) if parent_path != "/" else "/" + name
    try:
        if kind == "NODE_KIND_DIR":
            write_response_dir(workspace, cur)
            files_dirs[1] += 1
            for ch in children:
                _flatten_tree(workspace, cur, ch, files_dirs)
        else:
            full = safe_path(workspace, cur)
            if not full.exists():
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_text("", encoding="utf-8")
                files_dirs[0] += 1
    except (ValueError, OSError):
        return


def load_bench_outcomes(bench_path: Path | None) -> dict[str, dict]:
    """Return {task_id → {outcome, score, score_detail, instruction}}
    from a bench summary JSON, or {} if not supplied."""
    if not bench_path or not bench_path.exists():
        return {}
    d = json.loads(bench_path.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for t in d.get("tasks", []):
        tid = t.get("task_id")
        if tid:
            out[tid] = {
                "outcome": t.get("outcome") or "",
                "score": t.get("score"),
                "score_detail": list(t.get("score_detail") or []),
                "instruction": t.get("instruction") or "",
            }
    return out


def load_agent_outcome(jsonl_path: Path) -> str | None:
    """Pull the outcome the agent reported (kind=outcome.reported) from
    a per-task JSONL trace. Used as a fallback when the bench's
    grader didn't surface an explicit expected_outcome string."""
    try:
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") == "outcome":
                return rec.get("reported")
    except OSError:
        pass
    return None


def extract_actor_from_dump(
    matched: list[dict],
) -> tuple[str | None, str | None, str | None]:
    """Pull the PROD trial's `/bin/id` stdout from the matched dump
    records and parse out actor + roles + the `/bin/date` stamp.

    Returns (actor_id, roles, context_date). The local mock defaults
    to anonymous/GUEST; without this, a rebuilt snapshot of a trial
    that ran as an employee (e.g. emp_036, discount_manager) would
    behave incorrectly locally because the agent's prepass would see
    GUEST and refuse role-gated actions.
    """
    user_re = re.compile(r"^user:\s*(.+)$", re.MULTILINE)
    roles_re = re.compile(r"^roles:\s*(.+)$", re.MULTILINE)
    actor: str | None = None
    roles: str | None = None
    ctx_date: str | None = None
    for rec in matched:
        if rec.get("op") != "exec":
            continue
        req = rec.get("request") or {}
        resp = rec.get("response") or {}
        path = req.get("path") or ""
        stdout = (resp.get("stdout") or "")
        if path == "/bin/id" and stdout:
            m_user = user_re.search(stdout)
            m_roles = roles_re.search(stdout)
            if m_user and actor is None:
                actor = m_user.group(1).strip()
            if m_roles and roles is None:
                roles = m_roles.group(1).strip()
        elif path == "/bin/date" and stdout and ctx_date is None:
            s = stdout.strip()
            if s:
                ctx_date = s
    return actor, roles, ctx_date


def derive_grader_hints(score_detail: list[str]) -> dict[str, Any]:
    """Map the grader's failure strings to metadata fields.

    Returns {expected_outcome?, required_refs[], forbidden_refs[]}."""
    out: dict[str, Any] = {"required_refs": [], "forbidden_refs": []}
    for sd in score_detail:
        m = EXPECTED_OUTCOME_RE.search(sd)
        if m:
            out["expected_outcome"] = m.group(1)
        for p in INVALID_REF_RE.findall(sd):
            out["forbidden_refs"].append(p)
        for p in MISSING_REF_RE.findall(sd):
            out["required_refs"].append(p)
    return out


def rebuild_task(
    *,
    task_id: str,
    bench_log_dir: Path,
    dump_dir: Path,
    bench_outcomes: dict[str, dict],
    output_root: Path,
    descriptor: str | None,
) -> Path:
    """Rebuild one task's snapshot. Returns the snapshot root."""
    # Per-task JSONL trace path: e.g. logs/.../t23__run0.jsonl
    trace_path = next(bench_log_dir.glob(f"{task_id}__run*.jsonl"), None)
    if trace_path is None:
        raise FileNotFoundError(
            f"no trace under {bench_log_dir} matching {task_id}__run*.jsonl"
        )
    tt = load_task_trace(trace_path)
    if not tt.op_signatures:
        raise ValueError(f"{task_id}: trace has no ecom_op records — cannot bound dump")

    # Match dump records to this task by (op, path, wall_ms) signature.
    # Each signature in the task's trace is consumed exactly once from
    # the dump pool. Since ecom_op records carry no `ts`, a permissive
    # lower-bound TS window (started_at onward) is the only narrowing
    # filter — collisions on (op, path, wall_ms) across concurrent
    # tasks are rare in practice because wall_ms is high-resolution.
    remaining_sigs: dict[tuple[str, str, int], int] = dict(tt.op_signatures)
    matched: list[dict] = []
    for _f, rec in iter_dump_records(dump_dir):
        ts = rec.get("ts")
        if tt.ts_first and ts:
            try:
                if _parse_iso(ts) < _parse_iso(tt.ts_first):
                    continue
            except Exception:
                pass
        op = rec.get("op") or ""
        req = rec.get("request") or {}
        # Dump records key path on the request side. For most ops the
        # trace's ecom_op.path mirrors the request's `path` or `root`.
        path = req.get("path") or req.get("root") or ""
        wall = int(rec.get("wall_ms") or 0)
        key = (op, path, wall)
        if remaining_sigs.get(key, 0) > 0:
            matched.append(rec)
            remaining_sigs[key] -= 1
    unmatched_sigs = {k: v for k, v in remaining_sigs.items() if v > 0}
    if unmatched_sigs:
        print(
            f"  ! {task_id}: {sum(unmatched_sigs.values())} trace ops had no "
            f"dump match (signature collisions or capture gaps): "
            f"{list(unmatched_sigs.keys())[:3]}{'…' if len(unmatched_sigs) > 3 else ''}",
            file=sys.stderr,
        )

    snap_name = f"{task_id}_{descriptor}" if descriptor else task_id
    snap_root = output_root / snap_name / "run_0"
    workspace = snap_root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    files, dirs = materialise_workspace(workspace, matched)

    # Build metadata.json
    bench_meta = bench_outcomes.get(task_id, {})
    instruction = tt.instruction or bench_meta.get("instruction", "")
    grader_hints = derive_grader_hints(bench_meta.get("score_detail", []))

    # expected_outcome resolution priority:
    #   1. explicit "expected outcome X, got Y" in grader's score_detail
    #   2. the outcome the agent reported on this trial (when the only
    #      failure was a forbidden/missing ref — the grader accepted
    #      the outcome itself).
    expected_outcome = grader_hints.get("expected_outcome")
    if expected_outcome is None:
        expected_outcome = load_agent_outcome(trace_path)

    # Pull the trial's actual identity + clock from the dump's
    # /bin/id and /bin/date probes. Without this the snapshot would
    # default to anonymous/GUEST locally even when the PROD trial
    # ran as an employee or customer — local A/B would mis-classify
    # role-gated tasks.
    actor_id, roles, ctx_date = extract_actor_from_dump(matched)
    if ctx_date is None:
        ctx_date = tt.context_date

    metadata = {
        "instruction": instruction,
        "context_date": ctx_date,
        "actor_id": actor_id or "anonymous",
        "roles": roles or "GUEST",
        "source": f"rebuilt from {dump_dir.name} on {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "notes": (
            f"Auto-rebuilt from BITGN_TRACE_RAW_RESPONSES dump. "
            f"Materialised {files} files / {dirs} dirs from {len(matched)} dump records. "
            f"Window: started_at={tt.ts_first}. "
            f"Trial outcome was {bench_meta.get('outcome') or 'unknown'} "
            f"with score {bench_meta.get('score')}; "
            f"score_detail={bench_meta.get('score_detail') or []}. "
            f"Identity: actor={actor_id!r} roles={roles!r}."
        ),
        "expected_outcome": expected_outcome,
        "required_refs": grader_hints["required_refs"],
        "forbidden_refs": grader_hints["forbidden_refs"],
        "expected_answer": None,
    }
    (snap_root / "metadata.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8",
    )
    print(
        f"  ✓ {task_id} → {snap_root}  "
        f"({files} files, {dirs} dirs, {len(matched)} dump records)"
    )
    return snap_root


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="rebuild_ws_from_raw", description=__doc__.split("\n\n", 1)[0])
    p.add_argument("--bench-log", required=True, type=Path,
                   help="Per-trial JSONL dir, e.g. logs/<bench>/<ts>/")
    p.add_argument("--dump-dir", required=True, type=Path,
                   help="BITGN_TRACE_RAW_DIR root containing ecom_responses.*.jsonl")
    p.add_argument("--bench-summary", type=Path, default=None,
                   help="Bench summary JSON (for outcome + score_detail hints)")
    p.add_argument("--task", action="append", required=True,
                   help="Task id to rebuild (repeatable). Use the form t23.")
    p.add_argument("--descriptor", default=None,
                   help="Suffix appended to snapshot dir name (e.g. inj_override).")
    p.add_argument("--output-root", type=Path,
                   default=Path("artifacts/ws_snapshots"))
    args = p.parse_args(argv)

    if not args.bench_log.exists():
        raise SystemExit(f"bench-log dir not found: {args.bench_log}")
    if not args.dump_dir.exists():
        raise SystemExit(f"dump-dir not found: {args.dump_dir}")

    outcomes = load_bench_outcomes(args.bench_summary)
    args.output_root.mkdir(parents=True, exist_ok=True)
    print(f"# rebuild_ws_from_raw: {len(args.task)} task(s)")
    for tid in args.task:
        try:
            rebuild_task(
                task_id=tid,
                bench_log_dir=args.bench_log,
                dump_dir=args.dump_dir,
                bench_outcomes=outcomes,
                output_root=args.output_root,
                descriptor=args.descriptor,
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"  ✗ {tid}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
