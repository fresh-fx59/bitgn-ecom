"""PowerTools-OS /bin/jq emulation — pinned to the live PROD probe
(2026-05-30, artifacts/prod_explore/jq_probe*.json).

The custom binary is NOT real jq: banner-prefixed stdout, only -r,
path-extraction-only grammar, and some unsupported filters silently
yield null. These tests lock the local mock to that observed behaviour
so prompt guidance can be A/B-validated faithfully.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from bitgn_contest_agent.local.ecom_client import (
    LocalEcomClient,
    _eval_jq_filter,
    _jq_format_value,
    _JQ_BANNER,
)

DOC = {
    "status": "paid",
    "amount": 120,
    "three_ds": {"recovery_allowed": True, "attempts": 2},
    "lines": [
        {"sku": "PWR-1", "qty": 1},
        {"sku": "PWR-2", "qty": 3},
    ],
}


def _req(**kw):
    return SimpleNamespace(**kw)


# ---- pure evaluator ----------------------------------------------------

@pytest.mark.parametrize("filt, expected", [
    (".", [DOC]),
    (".status", ["paid"]),
    (".three_ds.recovery_allowed", [True]),
    (".lines[0].sku", ["PWR-1"]),
    (".lines[1].sku", ["PWR-2"]),
    (".lines[].sku", ["PWR-1", "PWR-2"]),
    (".lines[]", [{"sku": "PWR-1", "qty": 1}, {"sku": "PWR-2", "qty": 3}]),
    (".lines", [DOC["lines"]]),
    (".three_ds", [DOC["three_ds"]]),
    ("keys", [["amount", "lines", "status", "three_ds"]]),
    (".missing", [None]),           # missing key -> null, no error
    (".lines[9]", [None]),          # out of range -> null
])
def test_supported_paths(filt, expected):
    out, err = _eval_jq_filter(DOC, filt)
    assert err is None, f"{filt} unexpectedly errored: {err}"
    assert out == expected


@pytest.mark.parametrize("filt", [
    ".lines | length",      # length unsupported -> null
    ".status | type",
    ".amount | tostring",
    '.missing // "none"',   # // default unsupported -> null
    ".lines[1].qty > 2",    # comparison -> null
])
def test_silent_null_bucket(filt):
    out, err = _eval_jq_filter(DOC, filt)
    assert err is None
    assert out == [None]


@pytest.mark.parametrize("filt", [
    ".lines[] | select(.qty > 2) | .sku",
    "has(\"status\")",
    "[.lines[].qty]",
    ".amount - .fee",
    ".lines[] | .sku",
    '"\\(.status)"',
])
def test_hard_error_bucket(filt):
    out, err = _eval_jq_filter(DOC, filt)
    assert out is None
    assert err is not None and err.startswith("jq:")


def test_index_non_object_errors():
    out, err = _eval_jq_filter(DOC, ".amount.fee")
    assert out is None
    assert "cannot index non-object" in err


# ---- formatting --------------------------------------------------------

def test_format_raw_string_unquoted():
    assert _jq_format_value("paid", raw=True) == "paid"
    assert _jq_format_value("paid", raw=False) == '"paid"'


def test_format_sorts_object_keys():
    assert _jq_format_value({"b": 1, "a": 2}, raw=True) == '{"a":2,"b":1}'


# ---- end-to-end exec (stdin form) -------------------------------------

@pytest.fixture
def client(tmp_path):
    return LocalEcomClient(tmp_path)


def _run(client, args, stdin=""):
    r = client.exec(_req(path="/bin/jq", args=args, stdin=stdin))
    return r.exit_code or 0, r.stdout, r.stderr


def test_exec_banner_and_field(client):
    code, out, err = _run(client, ["-r", ".status"], json.dumps(DOC))
    assert code == 0 and err == ""
    assert out == _JQ_BANNER + "paid\n"


def test_exec_iteration_newline_separated(client):
    code, out, _ = _run(client, ["-r", ".lines[].sku"], json.dumps(DOC))
    assert code == 0
    assert out == _JQ_BANNER + "PWR-1\nPWR-2\n"


def test_exec_nested_bool(client):
    code, out, _ = _run(client, ["-r", ".three_ds.recovery_allowed"], json.dumps(DOC))
    assert out == _JQ_BANNER + "true\n"


def test_exec_no_raw_quotes_strings(client):
    _, out, _ = _run(client, [".status"], json.dumps(DOC))
    assert out == _JQ_BANNER + '"paid"\n'


def test_exec_unsupported_flag(client):
    code, out, err = _run(client, ["-c", ".status"], json.dumps(DOC))
    assert code == 1
    assert out == _JQ_BANNER
    assert "flag provided but not defined: -c" in err


def test_exec_expected_filter(client):
    code, out, err = _run(client, [], json.dumps(DOC))
    assert code == 1
    assert "expected filter" in err


def test_exec_select_errors(client):
    code, out, err = _run(client, ["-r", ".lines[] | select(.qty>2)"], json.dumps(DOC))
    assert code == 1
    assert out == _JQ_BANNER


def test_exec_invalid_json(client):
    code, _, err = _run(client, ["-r", "."], "not json")
    assert code == 1
    assert "not valid JSON" in err


def test_jq_registered_as_bin_stub():
    """/bin/jq is a zero-byte stub on PROD (probe v1: read -> empty,
    sha e3b0c442…); ensure it is registered so a materialised workspace
    reads it as a stub rather than leaking script bytes."""
    from bitgn_contest_agent.local.ecom_client import _BIN_STUB_PATHS
    assert "/bin/jq" in _BIN_STUB_PATHS
