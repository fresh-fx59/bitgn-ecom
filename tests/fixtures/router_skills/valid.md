---
name: test-valid
description: Use when the task contains the magic string 'TEST-ROUTE'.
type: rigid
category: TEST_CATEGORY
matcher_patterns:
  - 'TEST-ROUTE'
  - 'test (\w+) route'
variables:
  - target_name
---

# Test Valid Skill

## Rule

When the task matches TEST-ROUTE, emit OUTCOME_OK immediately.

## Process

1. Read AGENTS.md.
2. Emit OUTCOME_OK.
