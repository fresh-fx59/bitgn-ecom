"""Lock the lazy-imported SDK symbols phase0 spikes will execute at PROD time.

Catches SDK renames at unit-test time instead of in the middle of Task 10's
~2-hour PROD run.
"""


def test_phase0_sdk_imports_resolve() -> None:
    from bitgn.harness_pb2 import (  # noqa: F401
        EndTrialRequest,
        StartPlaygroundRequest,
    )
    from bitgn.vm.pcm_pb2 import (  # noqa: F401
        AnswerRequest,
        ContextRequest,
        Outcome,
        ReadRequest,
        TreeRequest,
        WriteRequest,
    )
    # Outcome.OUTCOME_OK is referenced in _spike_answer_replay
    assert hasattr(Outcome, "OUTCOME_OK"), "Outcome.OUTCOME_OK missing — phase0._spike_answer_replay will crash"
