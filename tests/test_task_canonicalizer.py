"""Unit tests for task_canonicalizer — language detection heuristic
and preservation guards. No live LLM calls (classifier is mocked)."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from bitgn_contest_agent.task_canonicalizer import (
    CanonicalizationResult,
    _detect_preserve_tokens,
    _looks_english,
    canonicalize,
)


# ── EN heuristic ──────────────────────────────────────────────────────


class TestLooksEnglish:
    def test_short_english_sentence_passes(self):
        assert _looks_english("Submit checkout for my basket.")

    def test_real_contest_en_instructions_all_pass(self):
        # Representative samples from 44-task contest at 2026-05-23
        samples = [
            "Do you have the Wood and Drywall Screw from Heco in the Heco Zinc Plated TopFix GTU-YPJ Wood and Drywall Screw line that has fastener type bolt and diameter 10 mm?",
            "How many catalogue products are Cleaning Liquid? Answer in exactly format \"<COUNT:%d>\" (no quotes).",
            "Please apply a 10% service_recovery discount to my basket basket_081.",
            "Go ahead and complete checkout for my basket basket_139.",
            "We have a confirmed fraud incident in archived payment history.",
            "please refund my purchase for EUR 59,00",
            "I'm helping Nils Kramer (nils.kramer+cust553@outlook.com) finish their order.",
        ]
        for s in samples:
            assert _looks_english(s), f"falsely flagged non-EN: {s!r}"

    def test_german_with_umlauts_rejected(self):
        assert not _looks_english(
            "Bitte erstatten Sie meinen Kauf für EUR 59,00"
        )

    def test_czech_with_diacritics_rejected(self):
        assert not _looks_english(
            "Prosím vraťte mi peníze za nákup ve výši EUR 59,00."
        )

    def test_hungarian_rejected(self):
        assert not _looks_english(
            "Kérem, térítse vissza a EUR 59,00 értékű vásárlásomat."
        )

    def test_japanese_rejected(self):
        assert not _looks_english("EUR 59,00 の購入分を返金してください。")

    def test_english_with_one_umlaut_name_still_en(self):
        # The Möller case — name contains umlaut, sentence is English
        assert _looks_english(
            "Please check if Kai Möller really manages PowerTool Linz Hauptplatz."
        )


# ── preserve-token detection ──────────────────────────────────────────


class TestPreserveTokens:
    def test_detects_basket_id(self):
        req, hint = _detect_preserve_tokens(
            "Refund basket basket_139 for cust_002."
        )
        assert "basket_139" in req
        assert "cust_002" in req

    def test_detects_eur_amount(self):
        req, _ = _detect_preserve_tokens("refund EUR 59,00 please")
        assert any("EUR" in t for t in req)

    def test_detects_percent(self):
        req, _ = _detect_preserve_tokens("apply 10% service_recovery")
        assert "10%" in req
        assert "service_recovery" in req

    def test_detects_format_token_as_hint_not_required(self):
        req, hint = _detect_preserve_tokens(
            'answer in format "<COUNT:%d>" please'
        )
        # Format tokens are HINT only (don't fail preservation)
        assert '"<COUNT:%d>"' in hint
        assert '"<COUNT:%d>"' not in req

    def test_detects_brand_name(self):
        req, _ = _detect_preserve_tokens(
            "Do you have the Wood Screw from Heco?"
        )
        assert "Heco" in req

    def test_empty_text_yields_no_tokens(self):
        req, hint = _detect_preserve_tokens("")
        assert req == [] and hint == []


# ── canonicalize() with mocked classifier ─────────────────────────────


class _FakeClassifier:
    def __init__(self, response: str):
        self.response = response
        self.calls = 0

    def raw_completion(self, *, prompt: str, **_):
        self.calls += 1
        return self.response


def _fake_classifier_module(response: str):
    """Patch bitgn_contest_agent.classifier.raw_completion to return a fixed body."""
    from bitgn_contest_agent import classifier as _cm
    return patch.object(_cm, "raw_completion", side_effect=lambda **k: response)


class TestCanonicalize:
    def test_empty_input_returns_empty_fallback(self):
        r = canonicalize(task_text="")
        assert r.instruction_language == "en"
        assert not r.canonicalized

    def test_english_input_skips_llm_call(self):
        # _looks_english fires, no LLM call needed
        text = "How many catalogue products are Extension Cable?"
        with _fake_classifier_module("THIS SHOULD NOT BE CALLED"):
            r = canonicalize(task_text=text)
        assert r.instruction_language == "en"
        assert r.task_text_en == text
        assert r.canonicalized

    def test_german_with_good_response_canonicalizes(self):
        resp = json.dumps({
            "instruction_language": "de",
            "task_text_en": "please refund my purchase for EUR 59,00",
        })
        with _fake_classifier_module(resp):
            r = canonicalize(task_text="bitte erstatten Sie meinen Kauf für EUR 59,00")
        assert r.canonicalized
        assert r.instruction_language == "de"
        assert "EUR 59,00" in r.task_text_en
        assert r.task_text_en.startswith("please refund")

    def test_german_preservation_failure_falls_back(self):
        # Response missing the EUR amount → fail preservation
        resp = json.dumps({
            "instruction_language": "de",
            "task_text_en": "please refund my purchase",
        })
        with _fake_classifier_module(resp):
            r = canonicalize(task_text="bitte erstatten Sie meinen Kauf für EUR 59,00")
        assert not r.canonicalized
        assert r.instruction_language == "de"  # detected lang preserved
        assert r.task_text_en == "bitte erstatten Sie meinen Kauf für EUR 59,00"
        assert "preservation_failed" in (r.failure_reason or "")

    def test_german_format_token_missing_does_not_fail(self):
        # The "<COUNT:%d>" is HINT only — paraphrase missing it should succeed
        text = (
            'Wie viele Extension Cable Produkte soll ich heute melden? '
            'Antworten Sie genau im Format "<COUNT:%d>" (ohne Anführungszeichen).'
        )
        resp = json.dumps({
            "instruction_language": "de",
            "task_text_en": "How many Extension Cable products should I report today?",
        })
        with _fake_classifier_module(resp):
            r = canonicalize(task_text=text)
        assert r.canonicalized
        assert r.task_text_en.startswith("How many")

    def test_llm_failure_falls_back_to_raw_text(self):
        from bitgn_contest_agent import classifier as _cm
        with patch.object(_cm, "raw_completion", side_effect=RuntimeError("boom")):
            r = canonicalize(task_text="bitte erstatten Sie meinen Kauf für EUR 59,00")
        assert not r.canonicalized
        # Language guessed as "en" only because we never got a response
        assert r.failure_reason.startswith("call_failed")

    def test_bad_json_response_falls_back(self):
        with _fake_classifier_module("not json at all"):
            r = canonicalize(task_text="bitte erstatten Sie meinen Kauf für EUR 59,00")
        assert not r.canonicalized
        assert "parse_failed" in (r.failure_reason or "")

    def test_fenced_json_response_parses(self):
        resp = (
            "```json\n"
            + json.dumps({
                "instruction_language": "ja",
                "task_text_en": "Please refund my purchase of EUR 59,00",
            })
            + "\n```"
        )
        with _fake_classifier_module(resp):
            r = canonicalize(task_text="EUR 59,00 の購入分を返金してください。")
        assert r.canonicalized
        assert r.instruction_language == "ja"
