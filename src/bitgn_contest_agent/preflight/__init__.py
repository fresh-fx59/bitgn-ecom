"""Preflight package — workspace schema discovery + uniform response helper.

The per-skill matcher modules (inbox, finance, entity, project,
doc_migration, unknown, canonicalize) were removed on 2026-04-21 after
log evidence showed match_found=True fires 0/104 times on PROD. See
docs/superpowers/specs/2026-04-21-preflight-trim-verify-design.md.
"""
