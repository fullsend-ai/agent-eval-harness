"""Case-mode exit aggregation must fail whenever any case fails (#300).

Runners report launch failures and timeouts as -1. Aggregating per-case exit
codes with max() turned [0, -1] into 0, so a run with a timed-out case
recorded success in run_result.json and exited 0.
"""

import json
import types

import execute as ex
import pytest
from agent_eval.config import EvalConfig


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


def _run_case_mode(tmp_path, monkeypatch, exit_codes, parallelism=None):
    """Drive ``_execute_per_case`` with canned per-case results.

    Returns ``(exit status raised by sys.exit, parsed run_result.json)``.
    """
    workspace = tmp_path / "workspace"
    output_dir = tmp_path / "output"
    workspace.mkdir()
    output_dir.mkdir()
    case_ids = [f"case-{i:03d}" for i in range(len(exit_codes))]
    (workspace / "case_order.yaml").write_text(
        "".join(f"- {cid}\n" for cid in case_ids))
    codes = dict(zip(case_ids, exit_codes))

    def fake_run_single_case(runner, target, case_id, *a, **kw):
        return case_id, {"exit_code": codes[case_id], "duration_s": 1.0,
                         "cost_usd": 0.0, "num_turns": 1,
                         "permission_denials": []}

    monkeypatch.setattr(ex, "_run_single_case", fake_run_single_case)
    runner = types.SimpleNamespace(name="fake", version="0")
    config = EvalConfig()
    config.skill = "test-skill"
    args = types.SimpleNamespace(
        workspace=str(workspace), run_id="r", config=str(tmp_path / "eval.yaml"),
        input_override=None)
    with pytest.raises(SystemExit) as exc:
        ex._execute_per_case(
            args, config, runner, None, output_dir, 5.0, 600, "sonnet", None,
            skill_args_template="", parallelism=parallelism,
            target="test-skill")
    run_result = json.loads((output_dir / "run_result.json").read_text())
    return exc.value.code, run_result


@pytest.mark.parametrize("exit_codes, expected", [
    ([0, 0], 0),
    ([0, -1], -1),
    ([-1, 0], -1),
    ([0, 1], 1),
])
def test_case_mode_run_exit_code(tmp_path, monkeypatch, exit_codes, expected):
    code, run_result = _run_case_mode(tmp_path, monkeypatch, exit_codes)
    assert code == expected
    assert run_result["exit_code"] == expected
    # Per-case codes are read, never rewritten.
    assert [r["exit_code"] for r in run_result["per_case"].values()] == exit_codes
