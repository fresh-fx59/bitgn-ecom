import json
from pathlib import Path


def test_v1_0_artifact_loads_under_v1_1_parser():
    """The frozen v1.0 artifact must parse through the v1.1 reader and
    preserve every field the ratchet check depends on."""
    src = Path("artifacts/bench/1623b40_20260410T181832Z.json")
    assert src.exists(), f"fixture missing: {src}"
    with src.open() as f:
        raw = json.load(f)

    # v1.0 file carries schema_version 1.0.0; reader must accept it.
    assert raw["schema_version"] == "1.0.0"

    from scripts.bench_summary import load_summary  # added in T1.4
    parsed = load_summary(raw)

    # Fields the ratchet gate depends on must round-trip.
    assert parsed["overall"]["pass_rate"] == raw["overall"]["pass_rate"]
    assert parsed["overall"]["total_passes"] == raw["overall"]["total_passes"]
    assert parsed["overall"]["total_runs"] == raw["overall"]["total_runs"]
    # v1.1 additive fields default sensibly when absent.
    assert parsed["overall"].get("pass_rate_median") == raw["overall"]["pass_rate"]
    assert parsed["overall"].get("pass_rate_min") == raw["overall"]["pass_rate"]
    assert parsed["overall"].get("total_input_tokens", 0) == 0
    assert parsed["overall"].get("divergence_count", 0) == 0
