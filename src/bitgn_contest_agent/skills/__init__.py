"""Bitgn skill library — markdown files with YAML-style frontmatter.

At router load time each *.md in this directory is parsed by
skill_loader.load_skill() and its matcher_patterns are compiled into
the tier-1 regex list.

Consumers: src/bitgn_contest_agent/router.py
"""
