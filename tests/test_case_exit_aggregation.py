"""Case-mode exit aggregation must fail whenever any case fails (#300).

Runners report launch failures and timeouts as -1. Aggregating per-case exit
codes with max() turned [0, -1] into 0, so a run with a timed-out case
recorded success in run_result.json and exited 0.
"""

import inspect

import execute as ex
import pytest


@pytest.mark.parametrize("codes, expected", [
    ([], 0),
    ([0, 0], 0),
    ([0, -1], -1),
    ([-1, 0], -1),
    ([-1, -1], -1),
    ([0, 1], 1),
    ([0, 2, -1], 2),
    ([None, 0], 0),
    ([None, -1], -1),
])
def test_aggregate_exit_codes(codes, expected):
    assert ex._aggregate_exit_codes(codes) == expected


def test_aggregate_exit_codes_accepts_generator():
    per_case = {"passed": {"exit_code": 0}, "timed_out": {"exit_code": -1}}
    agg = ex._aggregate_exit_codes(r["exit_code"] for r in per_case.values())
    assert agg != 0
    # Per-case codes are read, never rewritten.
    assert per_case["timed_out"]["exit_code"] == -1


def test_case_mode_uses_failure_status_aggregation():
    src = inspect.getsource(ex._execute_per_case)
    assert "_aggregate_exit_codes(" in src
    assert 'max((r["exit_code"]' not in src
