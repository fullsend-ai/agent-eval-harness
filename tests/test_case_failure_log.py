"""Per-case judge failures in the scoring log (issue #317).

An aggregate threshold can pass while one case fails a judge; that case used
to be visible only in the HTML report. `score.py judges` now lists it, using
the report's own pass/fail/skip criteria, without changing the exit verdict.
"""

import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import score as sc  # noqa: E402
from agent_eval.judge_status import (ERROR, FAIL, PASS, SKIP,  # noqa: E402
                                     classify_judge_result)

_EVAL_YAML = """\
name: t
execution: {mode: case}
judges:
  - name: protocol
    check: "return 'case-a' not in outputs['case_dir']"
  - name: quality
    score_range: [1, 5]
    if: "'case-c' not in outputs['case_dir']"
    check: "return (1, 'low') if 'case-a' in outputs['case_dir'] else (5, 'ok')"
  - name: broken
    check: |
      if 'case-b' in outputs['case_dir']:
          raise RuntimeError('boom\\n::error::forged')
      return True
thresholds:
  protocol: {min_pass_rate: 0.5}
  quality: {min_mean: 2.0}
"""


def _run_judges(tmp_path, case_ids):
    config = tmp_path / "eval.yaml"
    config.write_text(_EVAL_YAML)
    run_dir = tmp_path / "runs" / "r1"
    for cid in case_ids:
        (run_dir / "cases" / cid).mkdir(parents=True)
    args = argparse.Namespace(config=str(config), run_dir=str(run_dir),
                              run_id=None, samples=None, no_llm_judges=False,
                              workspace=None, model=None)
    sc.cmd_judges(args)


def test_failing_case_is_logged_beside_passing_and_skipped(tmp_path, capsys):
    # Thresholds pass (protocol 2/3, quality mean 3.0), so cmd_judges returns
    # normally — the logged per-case failures do not change the verdict.
    _run_judges(tmp_path, ["case-a", "case-b", "case-c"])
    out = capsys.readouterr().out

    assert "CASE FAILURES: 2 failed, 1 errored, 1 skipped" in out
    assert "FAIL  case-a / protocol = False" in out
    assert "FAIL  case-a / quality = 1 (scale 1-5)" in out
    assert "ERROR case-b / broken\n" in out
    assert "boom" not in out
    assert "See report.html or summary.yaml" in out
    # Passing case-b results and skipped case-c are not listed as failures.
    listed = [line for line in out.splitlines()
              if line.lstrip().startswith(("FAIL", "ERROR"))]
    assert len(listed) == 3
    assert not any("case-c" in line for line in listed)
    assert "REGRESSIONS: 0" in out


def test_threshold_failure_still_exits_nonzero(tmp_path, capsys):
    # Only case-a: protocol pass_rate 0 < 0.5 — same exit as before.
    with pytest.raises(SystemExit) as exc:
        _run_judges(tmp_path, ["case-a"])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "FAIL  case-a / protocol = False" in out
    assert "REGRESSIONS: 2 detected" in out  # protocol and quality


def test_untrusted_names_cannot_start_a_workflow_command(tmp_path, capsys):
    _run_judges(tmp_path, ["::error::case-a", "case-b"])
    out = capsys.readouterr().out

    assert "\\:\\:error::case-a / protocol = False" in out
    # The judge's exception message is never logged.
    assert "boom" not in out
    assert "forged" not in out
    assert not any(line.lstrip().startswith("::") for line in out.splitlines())


@pytest.mark.parametrize("result, rng, expected", [
    ({"value": True}, None, PASS),
    ({"value": False}, None, FAIL),
    ({"value": 1}, (1.0, 5.0), FAIL),
    ({"value": 3}, (1.0, 5.0), PASS),   # mid band is amber, not a failure
    ({"value": 1}, None, PASS),         # unknown scale is neutral
    ({"value": None, "rationale": "Skipped: condition"}, None, SKIP),
    ({"value": None, "error": "boom"}, None, ERROR),
    ({"value": None, "error": ""}, None, ERROR),  # message-less exception
    ("not-a-dict", None, None),
])
def test_classification_matches_report_criteria(result, rng, expected):
    assert classify_judge_result(result, rng) == expected


def test_messageless_judge_exception_is_logged_as_error(tmp_path, capsys):
    # `raise RuntimeError()` stores error == "": it must be an ERROR naming the
    # case and judge, not silently counted as a skip.
    config = tmp_path / "eval.yaml"
    config.write_text(
        "name: t\n"
        "execution: {mode: case}\n"
        "judges:\n"
        "  - name: silent\n"
        "    check: |\n"
        "      raise RuntimeError()\n"
    )
    run_dir = tmp_path / "runs" / "r1"
    (run_dir / "cases" / "case-a").mkdir(parents=True)
    args = argparse.Namespace(config=str(config), run_dir=str(run_dir),
                              run_id=None, samples=None, no_llm_judges=False,
                              workspace=None, model=None)
    sc.cmd_judges(args)
    out = capsys.readouterr().out

    assert "CASE FAILURES: 0 failed, 1 errored, 0 skipped" in out
    assert "ERROR case-a / silent\n" in out
