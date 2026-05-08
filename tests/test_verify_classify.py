from bitgn_contest_agent.verify import AnswerShape, classify_answer_shape
from bitgn_contest_agent.schemas import NextStep, ReportTaskCompletion


def _ns(answer: str, outcome: str = "OUTCOME_OK") -> NextStep:
    return NextStep(
        current_state="done",
        plan_remaining_steps_brief=["submit"],
        identity_verified=True,
        observation="ready",
        outcome_leaning=outcome,
        function=ReportTaskCompletion(
            tool="report_completion",
            message=answer,
            grounding_refs=[],
            rulebook_notes="n/a",
            outcome_justification="n/a",
            completed_steps_laconic=["done"],
            outcome=outcome,
        ),
    )


def test_classify_numeric_from_answer():
    shape = classify_answer_shape(_ns("6"), task_text="anything")
    assert shape is AnswerShape.NUMERIC


def test_classify_numeric_negative_and_decimal():
    assert classify_answer_shape(_ns("-12"), "x") is AnswerShape.NUMERIC
    assert classify_answer_shape(_ns("3.14"), "x") is AnswerShape.NUMERIC


def test_classify_numeric_from_task_text_when_answer_is_prose():
    shape = classify_answer_shape(
        _ns("six euros total"),
        task_text="how much did vendor charge. Number only.",
    )
    assert shape is AnswerShape.NUMERIC


def test_classify_date_iso():
    assert classify_answer_shape(_ns("2026-04-21"), "x") is AnswerShape.DATE


def test_classify_date_from_task_hint():
    shape = classify_answer_shape(
        _ns("april 21st"),
        task_text="what was the start date? Date only, YYYY-MM-DD.",
    )
    assert shape is AnswerShape.DATE


def test_classify_none_clarification_shape():
    ns = _ns("need more info", outcome="OUTCOME_NONE_CLARIFICATION")
    assert classify_answer_shape(ns, "x") is AnswerShape.NONE_CLARIFICATION


def test_classify_freeform_default():
    shape = classify_answer_shape(
        _ns("here is a long explanation of the bill context"),
        task_text="describe the vendor relationship",
    )
    assert shape is AnswerShape.FREEFORM
