#!/usr/bin/env -S .venv/bin/python3 -u
"""Run agent tasks against local workspace snapshots using the mock PCM.

Tests preflight + routing logic WITHOUT calling PROD or consuming LLM
tokens for the main agent loop. Validates that:
  1. Schema discovery finds all roots correctly
  2. Router classifies the task correctly
  3. Routed preflight returns useful data
  4. Preflight match points to the right files

Usage:
    # Test a specific task from the catalogue
    python scripts/local_test_runner.py \
        --catalogue artifacts/test_cases/eac8b36_full.json \
        --task-id t001

    # Test all tasks that have workspace snapshots
    python scripts/local_test_runner.py \
        --catalogue artifacts/test_cases/eac8b36_full.json \
        --all-with-snapshots

    # Test preflight only (no LLM agent loop)
    python scripts/local_test_runner.py \
        --catalogue artifacts/test_cases/eac8b36_full.json \
        --task-id t001 \
        --preflight-only

    # Test with custom workspace + instruction
    python scripts/local_test_runner.py \
        --workspace artifacts/ws_snapshots/t001/run_0/workspace \
        --instruction "What is the start date of the house AI thing? YYYY-MM-DD"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_pcm import LocalPcmClient


def run_preflight_pipeline(
    workspace_path: str | Path,
    instruction: str,
    context_date: str | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    """Run the full preflight pipeline (schema + router + routed_preflight)
    against a local workspace snapshot.

    Returns a dict with all pipeline outputs for verification.
    """
    from bitgn_contest_agent.preflight.schema import discover_schema_from_fs
    from bitgn_contest_agent.router import _get_default_router
    from bitgn_contest_agent.routed_preflight import dispatch_routed_preflight

    client = LocalPcmClient(workspace_path, context_date=context_date)
    router = _get_default_router()

    result: dict[str, Any] = {
        "instruction": instruction,
        "workspace": str(workspace_path),
    }

    # Step 1: Schema discovery
    if verbose:
        print(f"  [1] Schema discovery...")
    schema = discover_schema_from_fs(Path(workspace_path))
    result["schema"] = {
        "entities_root": schema.entities_root,
        "projects_root": schema.projects_root,
        "inbox_root": schema.inbox_root,
        "finance_roots": list(schema.finance_roots) if schema.finance_roots else [],
        "outbox_root": schema.outbox_root,
    }
    if verbose:
        for k, v in result["schema"].items():
            if v:
                print(f"       {k}: {v}")

    # Step 2: Router classification
    if verbose:
        print(f"  [2] Router classification...")
    skills_by_name = router.skills_by_name()
    decision = router.route(instruction)
    result["routing"] = {
        "skill_name": decision.skill_name,
        "source": decision.source,
        "extracted": decision.extracted,
        "task_text": decision.task_text[:100] if decision.task_text else None,
    }
    if verbose:
        print(f"       skill={decision.skill_name} source={decision.source}")

    # Step 3: Routed preflight
    if verbose:
        print(f"  [3] Routed preflight...")

    # Build a minimal adapter that routes to local PCM
    adapter = _LocalPreflightAdapter(client)

    # For preflight_unknown we need a backend — skip it in local mode
    try:
        outcome = dispatch_routed_preflight(
            decision=decision,
            schema=schema,
            adapter=adapter,
            skills_by_name=skills_by_name,
            backend=None,  # No LLM backend for local testing
        )
        result["preflight"] = {
            "tool": outcome.tool,
            "skipped_reason": outcome.skipped_reason,
            "error": outcome.error,
            "has_result": outcome.result is not None,
        }
        if outcome.result:
            result["preflight"]["ok"] = outcome.result.ok
            result["preflight"]["refs"] = list(outcome.result.refs) if outcome.result.refs else []
            # Parse the content JSON
            try:
                content = json.loads(outcome.result.content) if outcome.result.content else {}
                result["preflight"]["summary"] = content.get("summary", "")
                result["preflight"]["data"] = content.get("data", {})
            except (json.JSONDecodeError, TypeError):
                result["preflight"]["content_raw"] = (
                    outcome.result.content[:500] if outcome.result.content else ""
                )
        if verbose:
            print(f"       tool={outcome.tool} skipped={outcome.skipped_reason}")
            if outcome.result and outcome.result.ok:
                try:
                    c = json.loads(outcome.result.content)
                    print(f"       summary: {c.get('summary', '')[:120]}")
                except Exception:
                    pass
    except Exception as e:
        result["preflight"] = {"error": str(e)}
        if verbose:
            print(f"       ERROR: {e}")

    # Step 4: PCM ops log
    result["pcm_ops"] = len(client.ops_log)
    result["reads"] = sorted(client.reads)

    return result


class _LocalPreflightAdapter:
    """Minimal adapter that dispatches preflight requests to the local PCM."""

    def __init__(self, client: LocalPcmClient):
        self._client = client

    def dispatch(self, req: Any) -> Any:
        """Routed preflight dispatch — removed 2026-04-21 (match_found=True was 0/104 on PROD)."""
        raise NotImplementedError(
            "Routed preflight matcher modules were deleted on 2026-04-21. "
            "dispatch() is no longer supported. Use preflight_schema directly."
        )


def _verify_against_expected(
    result: dict[str, Any],
    expected: dict[str, Any],
    test_case: dict[str, Any],
) -> list[str]:
    """Check pipeline result against expected test case outcomes."""
    issues: list[str] = []

    # Check schema discovery
    schema = result.get("schema", {})
    if not schema.get("entities_root"):
        issues.append("schema: entities_root not discovered")
    if not schema.get("projects_root"):
        issues.append("schema: projects_root not discovered")
    if not schema.get("finance_roots"):
        issues.append("schema: finance_roots not discovered")

    # Check routing matches expected skill
    expected_skill = test_case.get("skill")
    actual_skill = result.get("routing", {}).get("skill_name")
    if expected_skill and actual_skill != expected_skill:
        issues.append(f"routing: expected skill={expected_skill}, got {actual_skill}")

    # Check preflight found expected files
    preflight = result.get("preflight", {})
    exp = test_case.get("expected", {})
    if exp.get("missing_refs"):
        refs = preflight.get("refs", [])
        for missing in exp["missing_refs"]:
            if not any(missing in r for r in refs):
                issues.append(f"preflight: expected ref to {missing}, not in refs")

    return issues


def _run_variant_catalogue(
    catalogue_path: str,
    workspace_path: str,
    task_id: str | None = None,
    template: str | None = None,
    difficulty: str | None = None,
    quiet: bool = False,
) -> None:
    """Run generated variant catalogue against a workspace snapshot.

    Validates routing accuracy and preflight data quality for each variant.
    """
    catalogue = json.load(open(catalogue_path))
    cases = catalogue["test_cases"]

    # Filters
    if task_id:
        cases = [c for c in cases if c["task_id"] == task_id]
    if template:
        cases = [c for c in cases if c.get("template") == template]
    if difficulty:
        cases = [c for c in cases if c.get("difficulty") == difficulty]

    print(f"Testing {len(cases)} variants from {Path(catalogue_path).name}")
    if template:
        print(f"  template filter: {template}")
    if difficulty:
        print(f"  difficulty filter: {difficulty}")
    print()

    # Expected routing map: template → expected skill
    TEMPLATE_TO_SKILL = {
        "which_projects": "project-involvement",
        "active_project_count": "project-involvement",
        "birthday": "entity-date",
        "important_date": "entity-date",
        "next_birthday": "entity-date",
        "project_start_date": "project-date",
        "finance_line_item": "finance-query",
        "finance_counterparty": "finance-query",
        "finance_line_counterparty": "finance-query",
    }

    stats = {
        "total": 0,
        "routing_correct": 0,
        "routing_wrong": 0,
        "routing_errors": {},  # skill_expected → {skill_got → count}
        "by_template": {},
        "by_difficulty": {},
    }

    for tc in cases:
        tid = tc["task_id"]
        intent = tc["intent"]
        tmpl = tc.get("template", "unknown")
        diff = tc.get("difficulty", "unknown")
        expected_skill = TEMPLATE_TO_SKILL.get(tmpl)

        stats["total"] += 1
        stats["by_template"].setdefault(tmpl, {"total": 0, "correct": 0})
        stats["by_template"][tmpl]["total"] += 1
        stats["by_difficulty"].setdefault(diff, {"total": 0, "correct": 0})
        stats["by_difficulty"][diff]["total"] += 1

        try:
            result = run_preflight_pipeline(
                workspace_path,
                intent,
                verbose=False,
            )
            actual_skill = result.get("routing", {}).get("skill_name")

            if expected_skill and actual_skill == expected_skill:
                stats["routing_correct"] += 1
                stats["by_template"][tmpl]["correct"] += 1
                stats["by_difficulty"][diff]["correct"] += 1
                if not quiet:
                    print(f"  {tid} [{diff}] OK  skill={actual_skill}")
            elif expected_skill:
                stats["routing_wrong"] += 1
                stats["routing_errors"].setdefault(expected_skill, {})
                stats["routing_errors"][expected_skill].setdefault(actual_skill, 0)
                stats["routing_errors"][expected_skill][actual_skill] += 1
                print(f"  {tid} [{diff}] MISROUTE  expected={expected_skill} got={actual_skill}")
                print(f"    intent: {intent[:100]}")
            else:
                stats["routing_correct"] += 1
                stats["by_template"][tmpl]["correct"] += 1
                stats["by_difficulty"][diff]["correct"] += 1
        except Exception as e:
            print(f"  {tid} [{diff}] ERROR: {e}")

    # Summary
    total = stats["total"]
    correct = stats["routing_correct"]
    wrong = stats["routing_wrong"]
    print(f"\n{'='*60}")
    print(f"Routing accuracy: {correct}/{total} ({100*correct/total:.1f}%)")
    print(f"  correct: {correct}, misrouted: {wrong}")

    print(f"\nBy template:")
    for tmpl, s in sorted(stats["by_template"].items()):
        pct = 100 * s["correct"] / s["total"] if s["total"] else 0
        print(f"  {tmpl}: {s['correct']}/{s['total']} ({pct:.0f}%)")

    print(f"\nBy difficulty:")
    for diff, s in sorted(stats["by_difficulty"].items()):
        pct = 100 * s["correct"] / s["total"] if s["total"] else 0
        print(f"  {diff}: {s['correct']}/{s['total']} ({pct:.0f}%)")

    if stats["routing_errors"]:
        print(f"\nMisroute details:")
        for expected, misroutes in sorted(stats["routing_errors"].items()):
            for got, count in sorted(misroutes.items(), key=lambda x: -x[1]):
                print(f"  expected {expected} → got {got}: {count} cases")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local preflight test runner")
    parser.add_argument("--catalogue", help="Test case catalogue JSON (harvested or generated)")
    parser.add_argument("--task-id", help="Specific task ID to test")
    parser.add_argument("--all-with-snapshots", action="store_true",
                        help="Test all tasks with workspace snapshots")
    parser.add_argument("--workspace", help="Direct workspace path")
    parser.add_argument("--instruction", help="Task instruction text")
    parser.add_argument("--context-date", help="Override context date (ISO format)")
    parser.add_argument("--preflight-only", action="store_true",
                        help="Only test preflight pipeline, no agent loop")
    parser.add_argument("--template", help="Filter variants by template name")
    parser.add_argument("--difficulty", help="Filter variants by difficulty (easy/medium/hard)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if args.workspace and args.instruction:
        # Direct mode
        result = run_preflight_pipeline(
            args.workspace,
            args.instruction,
            context_date=args.context_date,
            verbose=not args.quiet,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    if not args.catalogue:
        parser.error("--catalogue is required unless using --workspace + --instruction")

    catalogue = json.load(open(args.catalogue))

    # Detect catalogue type: generated variants vs harvested traces
    is_generated = catalogue.get("source") == "generated_variants"

    if is_generated:
        # Generated variant catalogue — requires workspace path
        ws = args.workspace or catalogue.get("workspace_path")
        if not ws:
            parser.error("Generated variant catalogue requires --workspace")
        _run_variant_catalogue(
            args.catalogue,
            ws,
            task_id=args.task_id,
            template=args.template,
            difficulty=args.difficulty,
            quiet=args.quiet,
        )
        return

    # Harvested trace catalogue — original flow
    cases = catalogue["test_cases"]

    if args.task_id:
        cases = [c for c in cases if c["task_id"] == args.task_id]
        if not cases:
            print(f"ERROR: task {args.task_id} not found in catalogue", file=sys.stderr)
            sys.exit(1)
    elif args.all_with_snapshots:
        cases = [c for c in cases if c["has_snapshot"]]
    else:
        parser.error("Specify --task-id, --all-with-snapshots, or --workspace + --instruction")

    print(f"Testing {len(cases)} tasks\n")

    passed = 0
    failed = 0
    errors = 0

    for tc in cases:
        tid = tc["task_id"]
        intent = tc["intent"]
        snap = tc.get("snapshot_path")

        if not snap or not Path(snap).exists():
            print(f"  {tid}: SKIP (no snapshot)")
            continue

        print(f"  {tid}: {intent[:70]}...")
        try:
            result = run_preflight_pipeline(
                snap,
                intent,
                verbose=not args.quiet,
            )
            issues = _verify_against_expected(result, tc.get("expected", {}), tc)
            if issues:
                failed += 1
                for issue in issues:
                    print(f"    ISSUE: {issue}")
            else:
                passed += 1
                if not args.quiet:
                    print(f"    OK")
        except Exception as e:
            errors += 1
            print(f"    ERROR: {e}")
        print()

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed, {errors} errors")


if __name__ == "__main__":
    main()
