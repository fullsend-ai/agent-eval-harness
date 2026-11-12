"""A judge whose `if:` is false for every selected case is not applicable.

Its `min_pass_rate`/`min_mean` thresholds are neither a pass nor a regression
(issue #311). Only an explicit skip counts: errors, condition errors,
incomplete evidence, results without a value and wrong-type thresholds still
fail, and a summary written before `skipped_cases` existed keeps the old
behaviour. The scoring command and the report have to agree.
"""

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "skills" / "eval-run" / "scripts"))

import report  # noqa: E402
import score as sc  # noqa: E402
from agent_eval.config import EvalConfig  # noqa: E402

GATES = {"min_pass_rate": 0.9, "min_mean": 0.9}


def _config(tmp_path, judges_yaml):
    p = tmp_path / "eval.yaml"
    p.write_text(
        f"name: t\nexecution: {{mode: case}}\n"
        f"dataset: {{path: {tmp_path}/cases}}\njudges:\n{judges_yaml}")
    return EvalConfig.from_yaml(p)


def _cases(tmp_path, triage_flags):
    dirs = []
    for i, flag in enumerate(triage_flags):
        cd = tmp_path / "cases" / f"case-{i}"
        cd.mkdir(parents=True)
        (cd / "annotations.yaml").write_text(f"triage: {str(flag).lower()}\n")
        dirs.append(cd)
    return dirs


def _score(tmp_path, judge_yaml, triage_flags):
    config = _config(tmp_path, judge_yaml)
    result = sc.score_cases(sc.load_judges(config), _cases(tmp_path, triage_flags),
                            config)
    return result["aggregated"]["triage"]


def _persisted(agg):
    """What `cmd_judges` writes to summary.yaml."""
    return {k: v for k, v in agg.items() if k != "values"}


BOOL_JUDGE = ("  - name: triage\n"
              "    if: \"annotations.get('triage')\"\n"
              "    check: \"return (True, 'ok')\"\n")


def test_all_skipped_judge_records_its_skips_and_passes_no_gate(tmp_path):
    agg = _score(tmp_path, BOOL_JUDGE, [False, False, False])
    assert agg["skipped_cases"] == 3
    assert agg["scored_cases"] == 0 and agg["errored_cases"] == 0
    assert sc.judge_not_applicable(agg)
    # Live aggregate (score.py judges) and persisted summary (score.py
    # regression) give the same answer.
    assert sc.detect_regressions({"triage": agg}, {"triage": GATES}) == []
    assert sc.detect_regressions({"triage": _persisted(agg)},
                                 {"triage": GATES}) == []


def test_mixed_skipped_and_scored_cases_still_enforce_thresholds(tmp_path):
    judge = ("  - name: triage\n"
             "    if: \"annotations.get('triage')\"\n"
             "    check: \"return (False, 'bad')\"\n")
    agg = _score(tmp_path, judge, [True, False])
    assert agg["skipped_cases"] == 1 and agg["scored_cases"] == 1
    assert not sc.judge_not_applicable(agg)
    metrics = sorted(r.metric for r in sc.detect_regressions(
        {"triage": agg}, {"triage": GATES}))
    assert metrics == ["mean", "pass_rate"]


def test_a_condition_error_is_not_a_skip(tmp_path):
    judge = ("  - name: triage\n"
             "    if: \"annotations['missing']\"\n"
             "    check: \"return (True, 'ok')\"\n")
    agg = _score(tmp_path, judge, [False, False])
    assert agg["skipped_cases"] == 0 and agg["errored_cases"] == 2
    regs = sc.detect_regressions({"triage": agg}, {"triage": GATES})
    assert len(regs) == 2
    assert all("errored on 2 cases" in r.detail for r in regs)


def test_a_result_without_a_value_is_not_a_skip(tmp_path):
    judge = "  - name: triage\n    check: \"return None\"\n"
    agg = _score(tmp_path, judge, [True])
    assert agg["skipped_cases"] == 0
    regs = sc.detect_regressions({"triage": agg}, {"triage": {"min_mean": 1.0}})
    assert regs[0].current_value == "n/a"
    assert "no case produced a value" in regs[0].detail


def test_a_skip_beside_a_valueless_case_is_not_not_applicable(tmp_path):
    """A condition-true case whose judge returns no value and no error moves
    none of the scored/errored/incomplete counters, so only the selected-case
    total tells it from a skip."""
    judge = ("  - name: triage\n"
             "    if: \"annotations.get('triage')\"\n"
             "    check: \"return None\"\n")
    agg = _score(tmp_path, judge, [True, False])
    assert agg["skipped_cases"] == 1 and agg["selected_cases"] == 2
    assert not sc.judge_not_applicable(agg)
    for current in (agg, _persisted(agg)):
        metrics = sorted(r.metric for r in sc.detect_regressions(
            {"triage": current}, {"triage": GATES}))
        assert metrics == ["mean", "pass_rate"]


def test_a_summary_without_a_selected_total_is_not_not_applicable():
    agg = {"mean": None, "pass_rate": None, "scored_cases": 0,
           "errored_cases": 0, "skipped_cases": 3}
    assert not sc.judge_not_applicable(agg)


@pytest.mark.parametrize("content", [
    "triage: [unclosed\n",          # YAML parse error
    "- just\n- a list\n",            # not a mapping
    "\xff\xfe invalid utf-8",        # undecodable (written as bytes below)
])
def test_unreadable_annotations_are_condition_errors_not_skips(tmp_path, content):
    config = _config(tmp_path, BOOL_JUDGE)
    cd = tmp_path / "cases" / "case-0"
    cd.mkdir(parents=True)
    (cd / "annotations.yaml").write_bytes(
        b"\xff\xfe\x00 bad" if content.startswith("\xff") else content.encode())
    agg = sc.score_cases(sc.load_judges(config), [cd], config)[
        "aggregated"]["triage"]
    assert agg["skipped_cases"] == 0 and agg["errored_cases"] == 1
    assert not sc.judge_not_applicable(agg)
    assert sc.detect_regressions({"triage": agg}, {"triage": GATES})


def test_a_wrong_type_threshold_still_fails(tmp_path):
    judge = ("  - name: triage\n"
             "    if: \"annotations.get('triage')\"\n"
             "    check: \"return (3, 'numeric')\"\n")
    agg = _score(tmp_path, judge, [True, False])
    regs = sc.detect_regressions({"triage": agg},
                                 {"triage": {"min_pass_rate": 0.5}})
    assert regs[0].detail == "pass_rate unavailable — not a boolean judge"
    # min_win_rate on a non-pairwise judge is wrong-type even when skipped.
    na = {"skipped_cases": 2, "selected_cases": 2, "scored_cases": 0,
          "errored_cases": 0}
    assert [r.metric for r in sc.detect_regressions(
        {"triage": na}, {"triage": {"min_win_rate": 0.5}})] == ["win_rate"]


@pytest.mark.parametrize("label,agg", [
    ("all errored", {"mean": None, "pass_rate": None, "scored_cases": 0,
                     "errored_cases": 2, "skipped_cases": 1}),
    ("incomplete evidence", {"mean": None, "pass_rate": None,
                             "scored_cases": 0, "errored_cases": 0,
                             "incomplete_cases": 1, "skipped_cases": 1}),
])
def test_failures_alongside_skips_still_regress(label, agg):
    assert not sc.judge_not_applicable(agg), label
    assert sc.detect_regressions({"triage": agg}, {"triage": GATES}), label


def test_a_legacy_summary_keeps_failing():
    legacy = {"mean": None, "pass_rate": None, "scored_cases": 0,
              "errored_cases": 0}
    assert not sc.judge_not_applicable(legacy)
    regs = sc.detect_regressions({"triage": legacy}, {"triage": GATES})
    assert len(regs) == 2
    assert all("skipped for all cases" in r.detail for r in regs)


def test_no_selected_case_is_not_not_applicable():
    """Zero cases is not "every case skipped": nothing was evaluated at all."""
    empty = {"mean": None, "pass_rate": None, "scored_cases": 0,
             "errored_cases": 0, "skipped_cases": 0}
    assert sc.detect_regressions({"triage": empty}, {"triage": GATES})


def test_the_report_shows_not_applicable_rather_than_pass_or_fail():
    agg = {"mean": None, "pass_rate": None, "scored_cases": 0,
           "errored_cases": 0, "incomplete_cases": 0, "skipped_cases": 3,
           "selected_cases": 3}
    html = report._render_scoring_summary(
        {"judges": {"triage": agg}, "per_case": {}},
        {"thresholds": {"triage": GATES}, "judges": []})
    assert re.findall(r">(PASS|FAIL|SKIP|ERROR|N/A)<", html) == ["N/A"]
    assert "skipped (3 cases)" in html
    assert report._render_regressions(
        {"judges": {"triage": agg}},
        {"thresholds": {"triage": GATES}}) == ""


def test_the_report_escapes_a_hand_edited_skip_count():
    evil = "<script>alert(1)</script>"
    agg = {"mean": None, "pass_rate": None, "scored_cases": 0,
           "errored_cases": 0, "incomplete_cases": 0, "skipped_cases": evil,
           "selected_cases": evil}
    html = report._render_scoring_summary(
        {"judges": {"triage": agg}, "per_case": {}},
        {"thresholds": {"triage": GATES}, "judges": []})
    assert evil not in html


def test_the_cli_names_the_waived_gate(capsys):
    agg = {"skipped_cases": 3, "selected_cases": 3, "scored_cases": 0,
           "errored_cases": 0}
    sc._print_not_applicable({"triage": agg, "other": {"mean": 4.0}},
                             {"triage": GATES, "other": {"min_mean": 1.0}})
    out = capsys.readouterr().out
    assert "NOT APPLICABLE: [triage]" in out and "all 3 cases" in out
    assert "other" not in out
