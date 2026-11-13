"""A judge whose `if:` is false for every selected case is not applicable.

Its `min_pass_rate`/`min_mean` thresholds are neither a pass nor a regression
(issue #311). Only an explicit skip counts: errors, condition errors,
incomplete evidence, results without a value and wrong-type thresholds still
fail, and a summary written before `skipped_cases` existed keeps the old
behaviour. The scoring command, the standalone regression gate and the report
have to agree, so most cases here run the real chain: `score.py judges` writes
summary.yaml, `score.py regression` reads it back, and the report renders it.
"""

import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "skills" / "eval-run" / "scripts"))

import report  # noqa: E402
import score as sc  # noqa: E402

GATES = {"min_pass_rate": 0.9, "min_mean": 0.9}

BOOL_JUDGE = ("  - name: triage\n"
              "    if: \"annotations.get('triage')\"\n"
              "    check: \"return (True, 'ok')\"\n")


def _judge(check, if_=True, extra=""):
    cond = "    if: \"annotations.get('triage')\"\n" if if_ else ""
    return (f"  - name: triage\n{extra}{cond}"
            f"    check: \"{check}\"\n")


class Chain:
    """One run through judges -> summary.yaml -> regression -> report."""


def _run_chain(tmp_path, monkeypatch, capsys, judges_yaml, annotations,
               thresholds=None):
    """`annotations` holds one raw annotations.yaml text per case. Returns the
    exit codes and output of both gates, the persisted aggregate, the report's
    status labels and whether it rendered a Regressions table."""
    thresholds = {"triage": GATES} if thresholds is None else thresholds
    tmp_path.mkdir(parents=True, exist_ok=True)
    runs_root = tmp_path / "runs"
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(runs_root))
    config_path = tmp_path / "eval.yaml"
    config_path.write_text(
        f"name: t\nexecution: {{mode: case}}\n"
        f"dataset: {{path: {tmp_path}/cases}}\njudges:\n{judges_yaml}"
        + yaml.safe_dump({"thresholds": thresholds}))
    for i, text in enumerate(annotations):
        case = f"case-{i}"
        (tmp_path / "cases" / case).mkdir(parents=True)
        (tmp_path / "cases" / case / "annotations.yaml").write_bytes(
            text if isinstance(text, bytes) else text.encode())
        (runs_root / "t" / "run-a" / "cases" / case).mkdir(parents=True)

    def invoke(fn, **kw):
        try:
            fn(SimpleNamespace(config=str(config_path), run_id="run-a",
                               baseline=None, run_dir=None, workspace="",
                               model=None, samples=None, no_llm_judges=False,
                               **kw))
            code = 0
        except SystemExit as e:
            code = e.code or 0
        return code, capsys.readouterr().out

    chain = Chain()
    chain.judges_code, chain.judges_out = invoke(sc.cmd_judges)
    chain.regression_code, chain.regression_out = invoke(sc.cmd_regression)
    summary = yaml.safe_load((runs_root / "t" / "run-a" / "summary.yaml")
                             .read_text())
    chain.agg = summary["judges"]["triage"]
    # The CLI prints the metric; the cause is in the regression detail.
    chain.details = " | ".join(r.detail for r in sc.detect_regressions(
        summary["judges"], thresholds))
    chain.html_statuses, chain.html_regressions = _render(summary, thresholds)
    return chain


def _render(summary, thresholds):
    cfg = {"thresholds": thresholds, "judges": []}
    html = report._render_scoring_summary(summary, cfg)
    statuses = re.findall(r">(PASS|FAIL|SKIP|ERROR|N/A)<", html)
    return statuses, report._render_regressions(summary, cfg) != ""


def _flags(*values):
    return [f"triage: {str(v).lower()}\n" for v in values]


def test_all_skipped_judge_is_not_applicable_in_every_consumer(
        tmp_path, monkeypatch, capsys):
    c = _run_chain(tmp_path, monkeypatch, capsys, BOOL_JUDGE,
                   _flags(False, False, False))
    assert c.agg["skipped_cases"] == 3 and c.agg["selected_cases"] == 3
    assert c.agg["scored_cases"] == 0 and c.agg["errored_cases"] == 0
    assert (c.judges_code, c.regression_code) == (0, 0)
    for out in (c.judges_out, c.regression_out):
        assert "NOT APPLICABLE: [triage]" in out and "all 3 cases" in out
    assert c.html_statuses == ["N/A"] and not c.html_regressions


def test_mixed_skipped_and_scored_cases_still_enforce_thresholds(
        tmp_path, monkeypatch, capsys):
    c = _run_chain(tmp_path, monkeypatch, capsys,
                   _judge("return (False, 'bad')"), _flags(True, False))
    assert c.agg["skipped_cases"] == 1 and c.agg["scored_cases"] == 1
    assert (c.judges_code, c.regression_code) == (1, 1)
    assert "NOT APPLICABLE" not in c.judges_out + c.regression_out
    assert c.html_statuses == ["FAIL"] and c.html_regressions


@pytest.mark.parametrize("label,judge,flags,needle", [
    ("a condition error", _judge("return (True, 'ok')").replace(
        "annotations.get('triage')", "annotations['missing']"),
     _flags(False, False), "errored on 2 cases"),
    ("a result without a value", _judge("return None", if_=False),
     _flags(True), "no case produced a value"),
    ("a skip beside a valueless case", _judge("return None"),
     _flags(True, False), "no case produced a value"),
])
def test_only_an_explicit_skip_is_not_applicable(
        tmp_path, monkeypatch, capsys, label, judge, flags, needle):
    c = _run_chain(tmp_path, monkeypatch, capsys, judge, flags)
    assert (c.judges_code, c.regression_code) == (1, 1), label
    assert needle in c.details, label
    assert "NOT APPLICABLE" not in c.regression_out, label
    assert "N/A" not in c.html_statuses and c.html_regressions, label


@pytest.mark.parametrize("content", [
    "triage: [unclosed\n",          # YAML parse error
    "- just\n- a list\n",            # not a mapping
    "[]\n",                          # falsy non-mapping documents
    "false\n",
    "0\n",
    '""\n',
    b"\xff\xfe\x00 bad",            # undecodable
])
def test_unreadable_annotations_are_condition_errors_not_skips(
        tmp_path, monkeypatch, capsys, content):
    c = _run_chain(tmp_path, monkeypatch, capsys, BOOL_JUDGE, [content])
    assert c.agg["skipped_cases"] == 0 and c.agg["errored_cases"] == 1
    assert (c.judges_code, c.regression_code) == (1, 1)
    assert "N/A" not in c.html_statuses


def test_a_wrong_type_threshold_still_fails(tmp_path, monkeypatch, capsys):
    c = _run_chain(tmp_path, monkeypatch, capsys,
                   _judge("return (3, 'numeric')"), _flags(True, False),
                   {"triage": {"min_pass_rate": 0.5}})
    assert c.regression_code == 1
    assert "pass_rate: >= 0.5 -> n/a" in c.regression_out
    # min_win_rate on a non-pairwise judge is wrong-type even when skipped.
    c = _run_chain(tmp_path / "w", monkeypatch, capsys, BOOL_JUDGE,
                   _flags(False, False), {"triage": {"min_win_rate": 0.5}})
    assert "win_rate" in c.regression_out and c.regression_code == 1


NUMERIC_CHECK = ("    feedback_type: float\n    score_range: [0, 1]\n")
UNDECLARED_LLM = ("  - name: triage\n"
                  "    prompt: \"grade {{ conversation }}\"\n"
                  "    model: claude-opus-5-5\n"
                  "    if: \"annotations.get('triage')\"\n")


@pytest.mark.parametrize("label,judge", [
    ("a declared numeric check", _judge("return (0.5, 'n')",
                                        extra=NUMERIC_CHECK)),
    ("an LLM judge declaring neither field", UNDECLARED_LLM),
])
def test_min_pass_rate_on_an_all_skipped_numeric_judge_fails(
        tmp_path, monkeypatch, capsys, label, judge):
    c = _run_chain(tmp_path, monkeypatch, capsys, judge, _flags(False, False),
                   {"triage": {"min_pass_rate": 0.5}})
    assert c.agg["skipped_cases"] == 2 and c.agg["value_kind"] == "numeric"
    assert c.regression_code == 1, label
    assert "not a boolean judge" in c.details, label
    assert "NOT APPLICABLE" not in c.regression_out
    assert c.html_statuses == ["FAIL"], label
    # `min_mean` fits a numeric judge, so it is still waived.
    c = _run_chain(tmp_path / "m", monkeypatch, capsys, judge,
                   _flags(False, False), {"triage": {"min_mean": 0.5}})
    assert (c.regression_code, c.html_statuses) == (0, ["N/A"]), label


def test_a_check_judge_declaring_no_type_keeps_the_exemption(
        tmp_path, monkeypatch, capsys):
    c = _run_chain(tmp_path, monkeypatch, capsys, BOOL_JUDGE,
                   _flags(False, False), {"triage": {"min_pass_rate": 0.5}})
    assert "value_kind" not in c.agg
    assert c.regression_code == 0 and c.html_statuses == ["N/A"]


@pytest.mark.parametrize("thresholds,status", [
    # Nothing was waived: `max_error_rate` is evaluated (0 of 2 errored), so
    # the row is a PASS, like any other satisfied threshold.
    ({"max_error_rate": 0.5}, "PASS"),
    ({"min_mean": 0.9, "max_error_rate": 0.5}, "N/A"),
])
def test_the_report_marks_only_waived_thresholds_not_applicable(
        tmp_path, monkeypatch, capsys, thresholds, status):
    c = _run_chain(tmp_path, monkeypatch, capsys, BOOL_JUDGE,
                   _flags(False, False), {"triage": thresholds})
    assert c.regression_code == 0
    assert c.html_statuses == [status]
    assert ("NOT APPLICABLE" in c.regression_out) is (status == "N/A")


def test_a_legacy_summary_keeps_failing(tmp_path, monkeypatch, capsys):
    """A summary written before `skipped_cases` existed cannot tell an
    all-skipped judge from a wrong-type threshold."""
    runs = tmp_path / "runs"
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(runs))
    (runs / "t" / "run-a").mkdir(parents=True)
    legacy = {"mean": None, "pass_rate": None, "scored_cases": 0,
              "errored_cases": 0}
    summary = {"run_id": "run-a", "judges": {"triage": legacy}}
    (runs / "t" / "run-a" / "summary.yaml").write_text(yaml.safe_dump(summary))
    config = tmp_path / "eval.yaml"
    config.write_text("name: t\nexecution: {mode: case}\njudges:\n"
                      + BOOL_JUDGE + yaml.safe_dump({"thresholds":
                                                     {"triage": GATES}}))
    with pytest.raises(SystemExit) as e:
        sc.cmd_regression(SimpleNamespace(config=str(config), run_id="run-a",
                                          baseline=None))
    assert e.value.code == 1
    assert "NOT APPLICABLE" not in capsys.readouterr().out
    regs = sc.detect_regressions({"triage": legacy}, {"triage": GATES})
    assert len(regs) == 2
    assert all("skipped for all cases" in r.detail for r in regs)
    assert "N/A" not in _render(summary, {"triage": GATES})[0]


@pytest.mark.parametrize("label,agg", [
    ("all errored", {"mean": None, "pass_rate": None, "scored_cases": 0,
                     "errored_cases": 2, "skipped_cases": 1}),
    ("incomplete evidence", {"mean": None, "pass_rate": None,
                             "scored_cases": 0, "errored_cases": 0,
                             "incomplete_cases": 1, "skipped_cases": 1}),
    ("no selected-case total", {"mean": None, "pass_rate": None,
                                "scored_cases": 0, "errored_cases": 0,
                                "skipped_cases": 3}),
    ("no selected case", {"mean": None, "pass_rate": None, "scored_cases": 0,
                          "errored_cases": 0, "skipped_cases": 0}),
])
def test_failures_alongside_skips_still_regress(label, agg):
    assert not sc.judge_not_applicable(agg), label
    assert sc.detect_regressions({"triage": agg}, {"triage": GATES}), label


def test_the_report_escapes_a_hand_edited_skip_count():
    evil = "<script>alert(1)</script>"
    agg = {"mean": None, "pass_rate": None, "scored_cases": 0,
           "errored_cases": 0, "incomplete_cases": 0, "skipped_cases": evil,
           "selected_cases": evil}
    html = report._render_scoring_summary(
        {"judges": {"triage": agg}, "per_case": {}},
        {"thresholds": {"triage": GATES}, "judges": []})
    assert evil not in html
