"""Integration: after Phase A deletions, the agent imports and runs
without the removed modules."""


def test_agent_imports_cleanly():
    from bitgn_contest_agent.agent import AgentLoop  # noqa: F401


def test_deleted_modules_are_really_gone():
    import importlib
    for mod in [
        "bitgn_contest_agent.routed_preflight",
        "bitgn_contest_agent.preflight.inbox",
        "bitgn_contest_agent.preflight.finance",
        "bitgn_contest_agent.preflight.entity",
        "bitgn_contest_agent.preflight.project",
        "bitgn_contest_agent.preflight.doc_migration",
        "bitgn_contest_agent.preflight.unknown",
        "bitgn_contest_agent.preflight.canonicalize",
    ]:
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError:
            continue
        raise AssertionError(f"{mod} still importable — Phase A incomplete")
