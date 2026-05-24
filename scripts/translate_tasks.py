#!/usr/bin/env python3
"""Translate selected task instructions into target languages, preserving
entities/IDs/amounts/format-tokens verbatim. Outputs a JSON table.

Usage:
    scripts/translate_tasks.py
        --tasks t11,t13,t21,t28,t40,t43 \\
        --langs de,cs,hu,ja \\
        --out artifacts/i18n/translations.json
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
from pathlib import Path

from openai import OpenAI


_LANG_NAMES = {
    "de": "German",
    "cs": "Czech",
    "hu": "Hungarian",
    "ja": "Japanese",
    "fr": "French",
    "sl": "Slovenian",
    "es": "Spanish",
    "it": "Italian",
}


_TRANSLATE_PROMPT = """\
Translate the following English customer/employee instruction into {lang_name}.

CRITICAL preservation rules — these tokens MUST appear in the translation BYTE-IDENTICAL:
{preserved}

DO NOT translate:
- IDs like `basket_NNN`, `pay_NNN`, `cust_NNN`, `emp_NNN`, `ret_NNN`, `store_*`
- Amounts (e.g., `EUR 59,00`, `EUR 135.00`, `5%`, `4 items`)
- Format tokens (e.g., `"<COUNT:%d>"`, `"%d"`)
- Product family names that act as identifiers (`Wood and Drywall Screw`, `Heco`, `Extension Cable`, `Festool`, `TopFix GTU-YPJ`)
- Store names (`PowerTool Vienna Meidling`, `PowerTool Salzburg station`)
- Person names (`Luisa Scholz`, `Kai Möller`)
- Policy keywords used as enum-like tokens (`service_recovery`)
- Technical jargon that has no good translation (`checkout`, `basket` — keep when ambiguous; otherwise translate naturally)

DO translate:
- Verbs, helper words, syntax (e.g., "please refund my purchase" → "bitte erstatten Sie meinen Kauf")
- Politeness and meaning markers
- Quantifier phrases (e.g., "How many" → "Wie viele")

Be natural — sound like a real native-{lang_name} customer/employee. Output ONLY the translation, no preface, no quotes.

INSTRUCTION:
{text}
"""


def _preserved_tokens(text: str) -> list[str]:
    """Detect tokens that must appear verbatim in the translation."""
    tokens = set()
    for m in re.finditer(r'(?:basket|pay|cust|emp|ret|store)_[A-Za-z0-9_]+', text):
        tokens.add(m.group(0))
    for m in re.finditer(r'EUR\s+[\d.,]+', text):
        tokens.add(m.group(0))
    for m in re.finditer(r'[\d.,]+\s*%', text):
        tokens.add(m.group(0))
    for m in re.finditer(r'"<COUNT:%d>"|"%d"', text):
        tokens.add(m.group(0))
    for m in re.finditer(r'service_recovery|3DS|3-D Secure', text):
        tokens.add(m.group(0))
    # Product/brand multi-words (heuristic: capitalised noun phrases >= 2 words)
    for m in re.finditer(
        r'\b(?:Heco|Festool|TopFix|HECO|Wood and Drywall Screw|Nut Bolt and Washer|Tool Box and Bag|Extension Cable|GTU-YPJ|3JJ-9LM|3DW-64B)\b',
        text,
    ):
        tokens.add(m.group(0))
    # Store + city
    for m in re.finditer(
        r'PowerTool [A-Z][\w\s]+?(?=\s+(?:today|near|station|shop|store|branch|\.|,|;|:|$))',
        text,
    ):
        tokens.add(m.group(0).strip())
    for m in re.finditer(r'\b(?:Vienna|Graz|Linz|Salzburg|Brno|Ljubljana|Meidling|Praterstern)\b', text):
        tokens.add(m.group(0))
    # Person names like "Luisa Scholz" / "Kai Möller"
    for m in re.finditer(r'\b[A-ZÄÖÜ][\wÄÖÜäöüß]+\s+[A-ZÄÖÜ][\wÄÖÜäöüß]+\b', text):
        # Filter out title-cased non-name phrases
        if m.group(0) not in tokens and len(m.group(0).split()) == 2:
            tokens.add(m.group(0))
    return sorted(tokens)


def translate(client, model, text, lang, lang_name) -> str:
    import time
    preserved = _preserved_tokens(text)
    body = _TRANSLATE_PROMPT.format(
        lang_name=lang_name,
        preserved="\n".join(f"- {t}" for t in preserved) or "  (none)",
        text=text,
    )
    last_exc = None
    for attempt in range(8):
        try:
            stream = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": body}],
                stream=True,
            )
            parts = []
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    parts.append(delta.content)
            out = "".join(parts).strip()
            if out:
                break
        except Exception as exc:
            last_exc = exc
            wait = min(60, 5 * (2 ** attempt))
            print(f"     retry {attempt+1}/8 after {wait}s ({type(exc).__name__})", flush=True)
            time.sleep(wait)
    else:
        raise last_exc or RuntimeError("translation failed after retries")
    # Strip wrapping quotes if model added them
    if out.startswith('"') and out.endswith('"') and out.count('"') == 2:
        out = out[1:-1]
    return out, preserved


def verify_preservation(translation: str, preserved: list[str]) -> list[str]:
    missing = []
    for t in preserved:
        if t not in translation:
            missing.append(t)
    return missing


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--tasks", required=True)
    p.add_argument("--langs", required=True)
    p.add_argument("--instructions-source",
                   default="artifacts/enum/enum_20260522T231133Z/trials")
    p.add_argument("--out", required=True)
    args = p.parse_args()

    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    langs = [l.strip() for l in args.langs.split(",") if l.strip()]

    client = OpenAI(
        base_url=os.environ["CLIPROXY_BASE_URL"],
        api_key=os.environ["CLIPROXY_API_KEY"],
    )
    model = os.environ.get("AGENT_MODEL", "gpt-5.3-codex")

    src = Path(args.instructions_source)
    results = {}
    for tid in tasks:
        meta = json.load(open(src / f"{tid}.json"))
        en = meta["instruction"]
        results[tid] = {"en": en, "translations": {}}
        for lang in langs:
            lang_name = _LANG_NAMES.get(lang, lang)
            print(f"  [{tid}/{lang}] translating…", flush=True)
            trans, preserved = translate(client, model, en, lang, lang_name)
            missing = verify_preservation(trans, preserved)
            results[tid]["translations"][lang] = {
                "text": trans,
                "preserved": preserved,
                "missing": missing,
            }
            status = "ok" if not missing else f"MISSING:{missing}"
            print(f"     {status}  → {trans[:120]!r}", flush=True)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[done] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
