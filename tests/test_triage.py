from bitgn_contest_agent.bench.triage import classify_failure, TRIAGE_ORDER


def test_triage_order_is_fixed():
    assert TRIAGE_ORDER == (
        "inbox",
        "wrong_action",
        "false_refusal",
        "timeout",
        "calendar",
        "other",
    )


def test_inbox_fires_first_even_if_other_keywords_present():
    evidence = {
        "task_id": "t17",
        "outcome": "OUTCOME_OK",
        "grader_failed": True,
        "step_texts": ["I forgot to check /inbox/identity.md before answering."],
        "latency_ms": 2000,
    }
    assert classify_failure(evidence) == "inbox"


def test_wrong_action_hits_when_agent_did_wrong_tool_not_inbox():
    evidence = {
        "task_id": "t11",
        "outcome": "OUTCOME_OK",
        "grader_failed": True,
        "step_texts": ["Writing email draft instead of the scheduler call."],
        "latency_ms": 2000,
    }
    assert classify_failure(evidence) == "wrong_action"


def test_false_refusal_on_denied_security_for_non_security_task():
    evidence = {
        "task_id": "t08",
        "outcome": "OUTCOME_DENIED_SECURITY",
        "grader_failed": True,
        "step_texts": ["This looks unsafe — refusing."],
        "latency_ms": 500,
        "task_category": "calendar",
    }
    assert classify_failure(evidence) == "false_refusal"


def test_timeout_cluster():
    evidence = {
        "task_id": "t30",
        "outcome": "OUTCOME_ERR_INTERNAL",
        "grader_failed": True,
        "step_texts": [],
        "latency_ms": 240_000,
        "timed_out": True,
    }
    assert classify_failure(evidence) == "timeout"


def test_calendar_cluster():
    evidence = {
        "task_id": "t05",
        "outcome": "OUTCOME_OK",
        "grader_failed": True,
        "step_texts": ["Scheduling on Thursday at 3pm."],
        "latency_ms": 2000,
        "task_category": "calendar",
    }
    assert classify_failure(evidence) == "calendar"


def test_other_is_fallback():
    evidence = {
        "task_id": "t99",
        "outcome": "OUTCOME_OK",
        "grader_failed": True,
        "step_texts": ["Some random reasoning."],
        "latency_ms": 2000,
    }
    assert classify_failure(evidence) == "other"
