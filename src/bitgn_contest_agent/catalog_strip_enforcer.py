"""Catalog-strip enforcer for catalogue_count tasks.

PROD bench v0.1.117-pre+ L_full (run-22RiTyqM..., 2026-05-29):
  t11 instruction: "How many catalogue products are Tool Box and
    Bag? Answer in exactly format <COUNT:%d> (no quotes)."
  Agent emitted <COUNT:10> with grounding_refs = 1 addenda doc +
    10 /proc/catalog/*.json refs.
  Grader: "answer contains too many invalid references".

Prompt rule B already says aggregate counts answered from an
addendum should cite the addendum, NOT N product files. The
agent disobeyed under precision pressure.

This enforcer enforces the rule deterministically:
  When task_spec.kind == "catalogue_count" AND grounding_refs
  carries at least one /docs/<dir>/<date>-<topic>.md addenda
  doc, the catalog/*.json refs are over-cite — strip them.

Safety guards:
  - Only fires when kind == "catalogue_count" (exact match).
  - Only fires when ≥1 addenda doc is present (otherwise we'd
    be stripping the agent's only evidence).
  - NEVER strips refs outside /proc/catalog/.
  - NEVER strips the addenda doc(s) themselves.
  - NEVER strips /AGENTS.MD or other policy/identity refs.

Env gating:
  BITGN_USE_CATALOG_STRIP_ENFORCER=1 → fire
  (default unset)                     → skip
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


# Addenda docs live under /docs/<dir>/<YYYY-MM-DD>-<topic>.md or
# /docs/policy-updates/<YYYY-MM-DD>-<topic>.md etc. Conservative
# pattern: any .md under /docs/ with a date prefix in the filename.
_ADDENDA_RE = re.compile(
    r"^/docs/[^/]+/\d{4}-\d{2}-\d{2}-[^/]+\.md$",
    re.IGNORECASE,
)
# Also accept catalogue-count addenda whose filenames lack the
# date prefix but share the canonical token bag — these are
# accepted by addenda_completer too.
_ADDENDA_LOOSE_RE = re.compile(
    r"^/docs/[^/]+/.*\b(catalogue|count|reporting|addenda|policy-update)s?\b.*\.md$",
    re.IGNORECASE,
)
_CATALOG_REF_RE = re.compile(r"^/proc/catalog/.+\.json$", re.IGNORECASE)


@dataclass(frozen=True)
class CatalogStripResult:
    refs: list[str]
    dropped: list[str] = field(default_factory=list)
    aborted: bool = False
    abort_reason: str = ""


def enabled() -> bool:
    return os.environ.get(
        "BITGN_USE_CATALOG_STRIP_ENFORCER", ""
    ).strip().lower() in ("1", "true", "yes")


def _is_addenda(path: str) -> bool:
    return bool(
        _ADDENDA_RE.match(path) or _ADDENDA_LOOSE_RE.match(path)
    )


def strip_catalog_refs(
    *,
    kind: str,
    refs: list[str],
) -> CatalogStripResult:
    """Drop /proc/catalog/*.json refs from grounding_refs when this
    is a catalogue_count task with an addenda doc already cited."""
    if kind != "catalogue_count":
        return CatalogStripResult(
            refs=list(refs), aborted=True, abort_reason="kind_mismatch",
        )
    has_addenda = any(_is_addenda(r) for r in refs)
    if not has_addenda:
        return CatalogStripResult(
            refs=list(refs), aborted=True, abort_reason="no_addenda_doc",
        )
    kept: list[str] = []
    dropped: list[str] = []
    for r in refs:
        if _CATALOG_REF_RE.match(r):
            dropped.append(r)
        else:
            kept.append(r)
    return CatalogStripResult(
        refs=kept,
        dropped=dropped,
        aborted=False,
    )
