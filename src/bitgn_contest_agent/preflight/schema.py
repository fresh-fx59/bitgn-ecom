"""Workspace role discovery — identifies which directories hold inbox,
entities, finance, projects, outbox, rulebook, workflows, schemas by
inspecting frontmatter signatures of the files inside.

Path-agnostic: no directory name is hardcoded. Discovery is by content
signature only.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

from bitgn_contest_agent.adapter.pcm import ToolResult
from bitgn_contest_agent.preflight.response import build_response


_LOG = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Minimum fraction of files in a directory that must match the signature
# for the directory to be tagged with that role.
_MATCH_THRESHOLD = 0.3


@dataclass
class WorkspaceSchema:
    inbox_root: Optional[str] = None
    entities_root: Optional[str] = None
    finance_roots: List[str] = field(default_factory=list)
    projects_root: Optional[str] = None
    outbox_root: Optional[str] = None
    rulebook_root: Optional[str] = None
    workflows_root: Optional[str] = None
    schemas_root: Optional[str] = None
    errors: List[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.inbox_root:
            parts.append(f"inbox at {self.inbox_root}")
        if self.entities_root:
            parts.append(f"entities at {self.entities_root}")
        if self.finance_roots:
            parts.append(f"{len(self.finance_roots)} finance root(s)")
        if self.projects_root:
            parts.append(f"projects at {self.projects_root}")
        if self.outbox_root:
            parts.append(f"outbox at {self.outbox_root}")
        extras = [r for r in (self.rulebook_root, self.workflows_root, self.schemas_root) if r]
        if extras:
            parts.append(f"{len(extras)} doc root(s)")
        return "Workspace schema: " + ", ".join(parts) + "."

    def as_data(self) -> dict[str, Any]:
        return {
            "inbox_root": self.inbox_root,
            "entities_root": self.entities_root,
            "finance_roots": self.finance_roots,
            "projects_root": self.projects_root,
            "outbox_root": self.outbox_root,
            "rulebook_root": self.rulebook_root,
            "workflows_root": self.workflows_root,
            "schemas_root": self.schemas_root,
            "errors": self.errors,
        }


def parse_record_metadata(text: str) -> dict[str, str]:
    """Unified metadata reader for YAML frontmatter, markdown bullet
    lists, and ASCII pipe tables. Returns lowercased-key dict. Returns
    {} on unknown shapes — callers treat empty as "no classifiable
    metadata" (fail-safe).

    Scan order: YAML → bullet list → ASCII table → heading heuristic.
    First non-empty wins.
    """
    yaml_md = _parse_frontmatter_yaml(text)
    if yaml_md:
        return yaml_md
    bullet_md = _parse_bullet_list(text)
    if bullet_md:
        return bullet_md
    table_md = _parse_ascii_table(text)
    if table_md:
        return table_md
    return _infer_from_heading(text)


_HEADING_RE = re.compile(r"^#\s+(.+)", re.MULTILINE)


def _infer_from_heading(text: str) -> dict[str, str]:
    """Last-resort heuristic: extract the H1 heading and infer a
    record_type from keywords. Covers PROD inbox items that are
    plain markdown with no structured metadata (e.g. "# Next inbox
    item\\n\\nPlease handle this request: ...").
    """
    m = _HEADING_RE.search(text[:500])
    if not m:
        return {}
    heading = m.group(1).strip().lower()
    if "inbox" in heading:
        return {"_heading": m.group(1).strip(), "record_type": "inbox"}
    return {}


def _parse_frontmatter_yaml(text: str) -> dict[str, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    body = m.group(1)
    out: dict[str, str] = {}
    for line in body.splitlines():
        if ":" in line and not line.startswith(" "):
            k, _, v = line.partition(":")
            out[k.strip().lower()] = v.strip()
    return out


_BULLET_RE = re.compile(r"^-\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")


def _strip_value(v: str) -> str:
    """Strip leading/trailing whitespace and surrounding backticks from a
    parsed metadata value. PROD bullet-list records wrap scalars in
    backticks (`` `active` ``, `` `entity.miles` ``); we normalize here
    so downstream `_RECORD_TYPE_TO_ROLE` lookups and string matches work.
    """
    v = v.strip()
    if len(v) >= 2 and v.startswith("`") and v.endswith("`"):
        v = v[1:-1].strip()
    return v


def _parse_bullet_list(text: str) -> dict[str, str]:
    """Scan the top of the file for contiguous `- key: value` lines.

    Skips leading blank lines, markdown headings (`# ...`), and any
    unclosed YAML-frontmatter preamble (lines before bullets when the
    file starts with `---` but has no closing `---`).
    Stops at the first line that doesn't match the bullet pattern.
    """
    out: dict[str, str] = {}
    started = False
    in_frontmatter_preamble = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if not started:
            stripped = line.strip()
            # `---` starts or ends a YAML preamble zone.
            if stripped == "---":
                # Second `---` closes the preamble; first opens it.
                in_frontmatter_preamble = not in_frontmatter_preamble
                continue
            # A blank line after entering preamble mode means the closing
            # `---` is absent (malformed frontmatter). Exit preamble mode
            # so we can pick up bullet lines that follow.
            if not stripped and in_frontmatter_preamble:
                in_frontmatter_preamble = False
                continue
            # Skip blank lines, headings, and lines inside a preamble block.
            if not stripped or stripped.startswith("#") or in_frontmatter_preamble:
                continue
            m = _BULLET_RE.match(line)
            if not m:
                # Not a bullet list file.
                return {}
            started = True
            out[m.group(1).lower()] = _strip_value(m.group(2))
            continue
        m = _BULLET_RE.match(line)
        if m:
            out[m.group(1).lower()] = _strip_value(m.group(2))
            continue
        if line.strip() == "":
            # Blank line ends the bullet block.
            break
        # Non-bullet, non-blank line → stop scanning.
        break
    return out


def _is_table_separator(stripped: str) -> bool:
    """True for markdown `|---|---|` or ASCII `+----+----+` separator rows."""
    if len(stripped) < 2:
        return False
    if stripped[0] not in "|+" or stripped[-1] not in "|+":
        return False
    return all(c in "|+-:= \t" for c in stripped)


def _parse_ascii_table(text: str) -> dict[str, str]:
    """Parse a two-column markdown pipe table or ASCII-art pipe table.

    Accepts both separator styles:
        | --- | --- |          (markdown)
        +-----+-----+          (ASCII art, as used by PROD invoices)

    Also tolerates a surrounding ```text ... ``` code fence.

    The first `| key | value |` data row with `key="field"` and
    `value="value"` is treated as a header and skipped.

    Returns {} if no such table exists in the first 60 lines of the
    file. Stops at the first non-table, non-separator, non-blank line
    (or at the closing code fence).
    """
    out: dict[str, str] = {}
    lines = text.splitlines()

    # Locate the first table-ish line (| row or +---+---+ separator),
    # skipping heading, blank, and code-fence preamble.
    i = 0
    scanned = 0
    while i < len(lines) and scanned < 60:
        stripped = lines[i].strip()
        scanned += 1
        if not stripped or stripped.startswith("#") or stripped.startswith("```"):
            i += 1
            continue
        if (stripped.startswith("|") and stripped.endswith("|")) or _is_table_separator(stripped):
            break
        # Any other prose before the table → no table here.
        return {}
    else:
        return {}

    if i >= len(lines):
        return {}

    header_seen = False
    for raw in lines[i:]:
        stripped = raw.strip()
        if not stripped:
            if out:
                break
            continue
        if stripped.startswith("```"):
            # Closing code fence ends the table.
            break
        if _is_table_separator(stripped):
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if len(cells) < 2:
                continue
            key = cells[0].lower()
            if not key:
                continue
            value = _strip_value(cells[1])
            # Skip literal header row `| field | value |`.
            if not header_seen and key == "field" and value.lower() == "value":
                header_seen = True
                continue
            out[key] = value
            continue
        # Any other non-table line ends the table.
        break
    return out


# Back-compat shim — inbox.py, entity.py, doc_migration.py, finance.py,
# and project.py still import this name. Safe to delete after Task 5
# rewires all callers to use parse_record_metadata directly.
_parse_frontmatter = parse_record_metadata


_RECORD_TYPE_TO_ROLE = {
    "project": "projects",
    "invoice": "finance",
    "bill": "finance",
    "receipt": "finance",
    "purchase": "finance",
    "inbound_email": "inbox",
    "inbox": "inbox",
    "outbound_email": "outbox",
    "outbox": "outbox",
    "person": "entities",
    "entity": "entities",
    "cast": "entities",
}


def _infer_record_type(md: dict[str, str]) -> str:
    """Return a record_type for a metadata dict.

    Priority:
    1. Explicit `record_type:` field (DEV frontmatter + PROD ASCII
       tables inside invoices).
    2. Secondary-key inference for PROD bullet-list records that omit
       record_type:
       - `owner_id` → project (project records have owner_id, entity
         records don't)
       - `relationship` or `important_dates` → entity (cast/person
         records in 10_entities/cast)
       - `address` + `kind` where kind is a channel shape → outbox
         (channel definitions in 60_outbox/channels)
    3. Empty string → unclassified (contributes no vote to classifier).
    """
    rt = (md.get("record_type") or "").strip().lower()
    if rt:
        return rt
    keys = md.keys()
    if "owner_id" in keys:
        return "project"
    if "relationship" in keys or "important_dates" in keys:
        return "entity"
    if "address" in keys and "kind" in keys:
        kind = (md.get("kind") or "").strip().lower()
        if kind in {"slack", "email", "discord", "sms", "calendar"}:
            return "outbox"
    return ""


def _classify_dir(frontmatters: list[dict[str, str]]) -> list[str]:
    """Return role labels the directory's records match.

    Threshold: ≥30% of records share a role. Records without a
    recognized (explicit or inferred) record_type contribute no vote.
    """
    if not frontmatters:
        return []
    n = len(frontmatters)
    counts: dict[str, int] = {}
    for fm in frontmatters:
        rt = _infer_record_type(fm)
        role = _RECORD_TYPE_TO_ROLE.get(rt)
        if role:
            counts[role] = counts.get(role, 0) + 1
    return [role for role, c in counts.items() if c / n >= _MATCH_THRESHOLD]


def _common_parent(paths: list[str]) -> str:
    """Return the longest common directory prefix of `paths`, or the
    single path if there is only one. Used to roll up per-subdir
    classifications to their parent directory.

    Example: ["40_projects/a", "40_projects/b"] → "40_projects".
    Single input is returned unchanged.
    """
    if not paths:
        return ""
    if len(paths) == 1:
        return paths[0]
    parts = [p.split("/") for p in paths]
    common: list[str] = []
    for segs in zip(*parts):
        if len(set(segs)) == 1:
            common.append(segs[0])
        else:
            break
    return "/".join(common) if common else paths[0]


def _is_md_name(name: str) -> bool:
    """Case-insensitive match for Markdown records.

    PROD workspaces mix `.md` (entities, invoices) with `.MD` (project
    READMEs, nested AGENTS.MD). Classification must consider both.
    """
    lower = name.lower()
    return lower.endswith(".md")


def _assign_roles(schema: WorkspaceSchema, per_role: dict[str, list[str]]) -> None:
    """Populate single-root fields by rolling up per-dir classifications
    to their common parent; keep `finance_roots` multi-valued."""
    if per_role.get("inbox"):
        schema.inbox_root = _common_parent(per_role["inbox"])
    if per_role.get("entities"):
        schema.entities_root = _common_parent(per_role["entities"])
    if per_role.get("projects"):
        schema.projects_root = _common_parent(per_role["projects"])
    if per_role.get("outbox"):
        schema.outbox_root = _common_parent(per_role["outbox"])
    for d in per_role.get("finance", []):
        if d not in schema.finance_roots:
            schema.finance_roots.append(d)


def discover_schema_from_fs(root: Path) -> WorkspaceSchema:
    """Filesystem-based discovery — used for local tests and as the
    core implementation that the PCM-backed version wraps.
    """
    schema = WorkspaceSchema()
    root = Path(root)
    if not root.exists():
        schema.errors.append(f"root does not exist: {root}")
        return schema

    per_role: dict[str, list[str]] = {}
    for dirpath in sorted(p for p in root.rglob("*") if p.is_dir()):
        md_files = [f for f in dirpath.iterdir() if f.is_file() and _is_md_name(f.name)]
        if not md_files:
            continue
        frontmatters = []
        for f in md_files[:50]:  # cap per dir for speed
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
                frontmatters.append(parse_record_metadata(text))
            except OSError as exc:
                schema.errors.append(f"read failed {f}: {exc}")

        roles = _classify_dir(frontmatters)
        rel = str(dirpath.relative_to(root))
        for role in roles:
            per_role.setdefault(role, []).append(rel)

    _assign_roles(schema, per_role)
    return schema


def run_preflight_schema(client: Any, workspace_ctx: Any) -> ToolResult:
    """PCM-backed entry point. Walks the workspace via the PCM list/tree
    RPC, parses frontmatters via read RPC, returns a ToolResult.

    `workspace_ctx` carries the root path or handle the adapter uses to
    talk to PCM. For the PCM client the adapter will pass `client`'s own
    workspace root.
    """
    from bitgn.vm import pcm_pb2  # local import to keep schema module light

    schema = WorkspaceSchema()
    try:
        # Tree walk from root. TreeResponse.root is a recursive TreeEntry
        # with (name, is_dir, children). Walk it to collect directories
        # that contain .md files, then list+read each to classify.
        tree_resp = client.tree(pcm_pb2.TreeRequest(root="/"))
        dirs: list[str] = []

        def _walk(entry, prefix: str) -> None:
            path = (
                f"{prefix}/{entry.name}".lstrip("/")
                if entry.name
                else prefix.lstrip("/")
            )
            if entry.is_dir:
                has_md = any(
                    _is_md_name(c.name) and not c.is_dir
                    for c in entry.children
                )
                if has_md and path:
                    dirs.append(path)
                for c in entry.children:
                    _walk(c, path)

        _walk(tree_resp.root, "")
        dirs.sort()
        per_role: dict[str, list[str]] = {}
        for d in dirs:
            list_resp = client.list(pcm_pb2.ListRequest(name=d))
            md_names = [
                e.name for e in list_resp.entries
                if _is_md_name(e.name) and not e.is_dir
            ][:50]
            frontmatters = []
            for name in md_names:
                read_resp = client.read(
                    pcm_pb2.ReadRequest(path=f"{d}/{name}")
                )
                frontmatters.append(parse_record_metadata(read_resp.content))
            roles = _classify_dir(frontmatters)
            for role in roles:
                per_role.setdefault(role, []).append(d)
        _assign_roles(schema, per_role)
    except Exception as exc:
        schema.errors.append(f"pcm walk failed: {exc}")

    content = build_response(summary=schema.summary(), data=schema.as_data())
    return ToolResult(
        ok=True,
        content=content,
        refs=tuple(),
        error=None,
        error_code=None,
        wall_ms=0,
    )


def parse_schema_content(content: Optional[str]) -> WorkspaceSchema:
    """Reverse of build_response — parse a preflight_schema content
    string back into a typed WorkspaceSchema. Returns an empty
    WorkspaceSchema on any parse failure (treat as 'no roots discovered').
    """
    if not content:
        return WorkspaceSchema()
    try:
        envelope = json.loads(content)
    except (ValueError, TypeError):
        return WorkspaceSchema()
    if not isinstance(envelope, dict):
        return WorkspaceSchema()
    data = envelope.get("data")
    if not isinstance(data, dict):
        return WorkspaceSchema()

    def _s(v):
        return v if isinstance(v, str) and v else None

    finance_raw = data.get("finance_roots") or []
    if isinstance(finance_raw, str):
        finance_roots = [finance_raw]
    elif isinstance(finance_raw, list):
        finance_roots = [str(x) for x in finance_raw if isinstance(x, str) and x]
    else:
        finance_roots = []

    errors_raw = data.get("errors") or []
    errors = [str(e) for e in errors_raw if isinstance(e, str) and e] if isinstance(errors_raw, list) else []

    return WorkspaceSchema(
        inbox_root=_s(data.get("inbox_root")),
        entities_root=_s(data.get("entities_root")),
        finance_roots=finance_roots,
        projects_root=_s(data.get("projects_root")),
        outbox_root=_s(data.get("outbox_root")),
        rulebook_root=_s(data.get("rulebook_root")),
        workflows_root=_s(data.get("workflows_root")),
        schemas_root=_s(data.get("schemas_root")),
        errors=errors,
    )
