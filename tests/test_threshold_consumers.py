"""The report and the MLflow tag must agree with the CLI's exit code.

`score.py::detect_regressions` is the gate CI exits on. `report.py` and
`log_results.py` each reimplemented a subset of its rules and had drifted to
two of the four threshold keys, so a run could exit 1 on `min_win_rate` or
`max_error_rate` while report.html showed no Regressions table and MLflow
tagged `regressions_detected=no`. Both now call the detector.
"""

import contextlib
import importlib.util
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "skills" / "eval-run" / "scripts"))

import report  # noqa: E402
import score  # noqa: E402
from agent_eval.config import EvalConfig  # noqa: E402
from score import detect_regressions  # noqa: E402


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- pairwise sections come from the real producer ---------------------------
#
# A `win_rate` literal in the judges aggregate is a shape nothing writes, which
# is how `min_win_rate` shipped unsatisfiable. These helpers drive the real
# `compare_runs` (and `cmd_pairwise`) with a scripted judge instead.
#
# One character per case: "A" the run under test wins, "B" the baseline wins,
# "T" tie, "E" the comparison errored, "L" a side's case record could not be
# read (an error that is also counted in `incomplete_cases`).

def _scripted_judge(verdicts, candidate="run-a"):
    case_ids = [f"case-{i:03d}" for i in range(len(verdicts))]
    by_case = dict(zip(case_ids, verdicts))

    def load_case_record(case_dir, config):
        if by_case[case_dir.name] == "L":
            raise OSError("unreadable output")
        return {"run": case_dir.parent.parent.name, "case": case_dir.name}

    def fmt(record):
        return f"<<{record['run']}|{record['case']}>>"

    def call_judge(backend, client, system_prompt, user_message, model, **kw):
        # Position-swapped: find which run sits in the "Output A" slot.
        sides = re.findall(r"<<([^|]+)\|(case-\d+)>>", user_message)
        first_run, case_id = sides[0]
        v = by_case[case_id]
        if v == "E":
            return None, "judge unavailable"
        if v == "T":
            return {"reasoning": "r", "preferred": "tie"}, None
        winner_run = candidate if v == "A" else "baseline"
        winner_first = (first_run == candidate) == (winner_run == candidate)
        return {"reasoning": "r", "preferred": "A" if winner_first else "B"}, None

    stack = contextlib.ExitStack()
    stack.enter_context(patch("score.load_case_record", side_effect=load_case_record))
    stack.enter_context(patch("score._format_outputs_for_pairwise", side_effect=fmt))
    stack.enter_context(patch("score._call_judge", side_effect=call_judge))
    stack.enter_context(patch("score._get_anthropic_client", return_value=object()))
    return stack, case_ids


def _pairwise_section(verdicts):
    """`summary["pairwise"]` exactly as compare_runs builds it."""
    if verdicts is None:
        return None
    stack, case_ids = _scripted_judge(verdicts)
    with stack:
        return score.compare_runs(Path("run-a"), Path("baseline"),
                                  EvalConfig(name="t", skill="s"), case_ids,
                                  model="claude-opus-5-5")


def test_the_producer_emits_the_incomplete_count_the_gate_reads():
    pw = _pairwise_section("AAAAAAAAAL")
    assert (pw["wins_a"], pw["errors"], pw["incomplete_cases"]) == (9, 1, 1)
    assert _pairwise_section("AAAAAAAAAE")["incomplete_cases"] == 0


def test_the_scripted_judge_produces_the_counts_it_is_told():
    pw = _pairwise_section("AAABTE")
    assert (pw["wins_a"], pw["wins_b"], pw["ties"], pw["errors"]) == (3, 1, 1, 1)


# judges aggregate, pairwise verdicts, thresholds, and whether the CLI regresses
CASES = [
    ("min_mean", {"q": {"mean": 0.5, "scored_cases": 10}}, None,
     {"q": {"min_mean": 1.0}}, True),
    ("min_pass_rate", {"q": {"pass_rate": 0.5}}, None,
     {"q": {"min_pass_rate": 0.9}}, True),
    ("min_win_rate breach", {}, "AAABBBBBTT",
     {"pairwise": {"min_win_rate": 0.6}}, True),
    ("min_win_rate met", {}, "AAAAAAABBT",
     {"pairwise": {"min_win_rate": 0.6}}, False),
    ("min_win_rate, no comparison recorded", {}, None,
     {"pairwise": {"min_win_rate": 0.6}}, True),
    ("max_error_rate", {"q": {"scored_cases": 1, "errored_cases": 9}}, None,
     {"q": {"max_error_rate": 0.2}}, True),
    ("clean", {"q": {"mean": 4.0, "scored_cases": 10, "errored_cases": 0}}, None,
     {"q": {"min_mean": 1.0}}, False),
]


def _summary(judges, pairwise):
    summary = {"judges": judges, "per_case": {}}
    if pairwise is not None:
        summary["pairwise"] = pairwise
    return summary


@pytest.mark.parametrize("key,judges,verdicts,thresholds,regresses", CASES)
def test_the_cli_gate_behaves_as_the_table_says(key, judges, verdicts,
                                                thresholds, regresses):
    regs = detect_regressions(judges, thresholds,
                              pairwise=_pairwise_section(verdicts))
    assert bool(regs) is regresses


@pytest.mark.parametrize("key,judges,verdicts,thresholds,regresses", CASES)
def test_the_report_shows_every_breach_the_cli_exits_on(key, judges, verdicts,
                                                        thresholds, regresses):
    html = report._render_regressions(
        _summary(judges, _pairwise_section(verdicts)), {"thresholds": thresholds})
    assert bool(html) is regresses, f"{key}: report and CLI disagree"
    if regresses:
        assert "Regressions" in html


@pytest.mark.parametrize("key,judges,verdicts,thresholds,regresses", CASES)
def test_the_mlflow_tag_matches_the_cli(key, judges, verdicts, thresholds,
                                        regresses):
    log_results = _load(
        REPO_ROOT / "skills" / "eval-mlflow" / "scripts" / "log_results.py",
        "_log_results_under_test")
    assert bool(log_results._detect_regressions(
        judges, thresholds, _pairwise_section(verdicts))) is regresses


# --- min_win_rate end to end: score.py pairwise -> summary.yaml -> regression -

def _pairwise_then_regression(tmp_path, monkeypatch, capsys, verdicts,
                              min_win_rate=0.6):
    """Run `score.py pairwise` (unless `verdicts` is None) and then the
    standalone `score.py regression` gate; return (exit code, gate output)."""
    config_path = tmp_path / "eval.yaml"
    config_path.write_text(yaml.safe_dump({
        "name": "t", "execution": {"skill": "s"},
        "models": {"judge": "claude-opus-5-5"},
        "judges": [{"name": "pairwise", "prompt": "compare"}],
        "thresholds": {"pairwise": {"min_win_rate": min_win_rate}}}))
    runs_dir = tmp_path / "runs"
    n = len(verdicts) if verdicts else 1
    for run in ("run-a", "baseline"):
        for i in range(n):
            (runs_dir / run / "cases" / f"case-{i:03d}").mkdir(parents=True)
    (runs_dir / "run-a" / "summary.yaml").write_text(
        yaml.safe_dump({"run_id": "run-a", "judges": {}}))
    monkeypatch.setattr(score, "_get_runs_dir", lambda name="": runs_dir)

    if verdicts is not None:
        stack, _ = _scripted_judge(verdicts)
        with stack:
            score.cmd_pairwise(SimpleNamespace(
                config=str(config_path), run_id="run-a", baseline="baseline",
                judge="pairwise", model=None, prompt_file=None, samples=None))
    capsys.readouterr()

    code = 0
    try:
        score.cmd_regression(SimpleNamespace(config=str(config_path),
                                             run_id="run-a", baseline=None))
    except SystemExit as e:
        code = e.code
    return code, capsys.readouterr().out


@pytest.mark.parametrize("label,verdicts,code,needle", [
    # wins_a / (wins_a + wins_b + ties): the run under test is A.
    ("7 of 10 won", "AAAAAAABBT", 0, "REGRESSIONS: 0"),
    ("3 of 10 won", "AAABBBBBTT", 1, "win_rate: >= 0.6 -> 0.300"),
    ("the baseline won 7 — not a pass", "BBBBBBBAAT", 1, "-> 0.200"),
    ("ties count against the run", "TTTT", 1, "-> 0.000"),
    ("errored comparisons leave the denominator", "AAAEE", 0, "REGRESSIONS: 0"),
    ("every comparison errored", "EEE", 1, "win_rate: >= 0.6 -> n/a"),
    ("no pairwise comparison ran", None, 1, "win_rate: >= 0.6 -> n/a"),
])
def test_min_win_rate_gates_the_recorded_comparison(tmp_path, monkeypatch, capsys,
                                                    label, verdicts, code, needle):
    got_code, out = _pairwise_then_regression(tmp_path, monkeypatch, capsys,
                                              verdicts)
    assert got_code == code, f"{label}: {out}"
    assert needle in out, label


def test_min_win_rate_is_not_satisfied_while_evidence_is_incomplete():
    """Nine wins and one case whose evidence could not be read: the win rate
    over the survivors is 1.0, which must not pass a 0.9 gate."""
    pw = _pairwise_section("AAAAAAAAAL")
    assert score.pairwise_win_rate(pw)[0] == 1.0
    regs = detect_regressions({}, {"pairwise": {"min_win_rate": 0.9}}, pairwise=pw)
    assert [(r.metric, r.current_value) for r in regs] == [("incomplete_staging", "1")]


def test_the_incomplete_count_is_not_folded_into_the_win_rate():
    """A real breach is still reported, alongside the incomplete gate."""
    pw = _pairwise_section("AABBBBBBBL")
    regs = detect_regressions({}, {"pairwise": {"min_win_rate": 0.9}}, pairwise=pw)
    assert [r.metric for r in regs] == ["incomplete_staging", "win_rate"]
    assert regs[1].current_value == f"{2 / 9:.3f}"


def test_min_win_rate_is_gated_normally_without_incomplete_evidence():
    thresholds = {"pairwise": {"min_win_rate": 0.9}}
    # An ordinary error leaves the denominator without tripping the gate.
    pw = _pairwise_section("AAAAAAAAAE")
    assert pw["incomplete_cases"] == 0
    assert detect_regressions({}, thresholds, pairwise=pw) == []
    pw = _pairwise_section("AAAAAAAAAA")
    assert detect_regressions({}, thresholds, pairwise=pw) == []


def test_incomplete_pairwise_evidence_is_not_reported_twice():
    """A `pairwise` judge that also has a judges aggregate with its own
    incomplete count gets one `incomplete_staging`, not two."""
    pw = _pairwise_section("AAAAAAAAAL")
    regs = detect_regressions(
        {"pairwise": {"incomplete_cases": 2, "scored_cases": 8}},
        {"pairwise": {"min_win_rate": 0.9}}, pairwise=pw)
    assert [r.metric for r in regs].count("incomplete_staging") == 1


def test_incomplete_pairwise_evidence_ignores_a_coverage_only_threshold():
    """Without `min_win_rate` the pairwise count gates nothing."""
    pw = _pairwise_section("AAAAAAAAAL")
    assert detect_regressions({}, {"pairwise": {"max_error_rate": 0.5}},
                              pairwise=pw) == []


def test_pairwise_max_error_rate_is_breached_alongside_min_win_rate():
    """The pairwise judge has no `judges` aggregate, so `max_error_rate` has to
    be read from the section's own `errors` / `cases_compared`: nine wins and
    five ordinary errors passes `min_win_rate` over the survivors but not the
    coverage gate."""
    pw = _pairwise_section("AAAAAAAAAEEEEE")
    assert (pw["errors"], pw["cases_compared"], pw["incomplete_cases"]) == (5, 14, 0)
    regs = detect_regressions(
        {}, {"pairwise": {"min_win_rate": 0.5, "max_error_rate": 0.0}},
        pairwise=pw)
    assert [(r.metric, r.baseline_value, r.current_value) for r in regs] == [
        ("error_rate", "<= 0.0", f"{5 / 14:.3f}")]
    assert regs[0].detail == "5 of 14 cases errored"


def test_pairwise_max_error_rate_is_satisfied_within_budget():
    pw = _pairwise_section("AAAAAAAAAE")
    thresholds = {"pairwise": {"min_win_rate": 0.5, "max_error_rate": 0.1}}
    assert detect_regressions({}, thresholds, pairwise=pw) == []
    # The rate is `>`-compared, so a stricter budget trips on the same section.
    assert [r.metric for r in detect_regressions(
        {}, {"pairwise": {"max_error_rate": 0.05}}, pairwise=pw)] == ["error_rate"]


def test_pairwise_max_error_rate_without_min_win_rate_and_with_a_failing_win_rate():
    """Each key reports on its own; neither short-circuits the other."""
    pw = _pairwise_section("ABBBBBBBEE")
    regs = detect_regressions(
        {}, {"pairwise": {"min_win_rate": 0.9, "max_error_rate": 0.1}}, pairwise=pw)
    assert [r.metric for r in regs] == ["error_rate", "win_rate"]
    assert [r.metric for r in detect_regressions(
        {}, {"pairwise": {"max_error_rate": 0.1}}, pairwise=pw)] == ["error_rate"]


def test_pairwise_max_error_rate_is_left_alone_for_a_caller_without_a_comparison():
    assert detect_regressions(
        {}, {"pairwise": {"max_error_rate": 0.0}}) == []


@pytest.mark.parametrize("section", [
    None, {}, "not-a-dict", {"errors": 0, "cases_compared": 0},
], ids=["none", "empty", "non-dict", "zero-compared"])
def test_pairwise_max_error_rate_is_unavailable_without_a_comparison(section):
    """0 of 0 is an unmeasured error rate, not a perfect one — `min_win_rate`
    already reports the same input as unavailable."""
    regs = detect_regressions(
        {}, {"pairwise": {"max_error_rate": 0.0}}, pairwise=section)
    assert [(r.metric, r.current_value) for r in regs] == [("error_rate", "n/a")]
    assert "unavailable" in regs[0].detail


def test_pairwise_error_rate_is_unavailable_alongside_min_win_rate():
    regs = detect_regressions(
        {}, {"pairwise": {"min_win_rate": 0.5, "max_error_rate": 0.0}},
        pairwise=None)
    assert [r.metric for r in regs] == ["error_rate", "win_rate"]


def test_pairwise_error_rate_is_not_reported_twice_given_an_aggregate():
    pw = _pairwise_section("AAAAAAAAEE")
    regs = detect_regressions(
        {"pairwise": {"errored_cases": 2, "scored_cases": 8}},
        {"pairwise": {"max_error_rate": 0.0}}, pairwise=pw)
    assert [r.metric for r in regs] == ["error_rate"]


def test_unavailable_win_rate_says_why():
    assert "every pairwise comparison errored (3 cases)" in detect_regressions(
        {}, {"pairwise": {"min_win_rate": 0.6}},
        pairwise=_pairwise_section("EEE"))[0].detail
    assert "no pairwise comparison recorded" in detect_regressions(
        {}, {"pairwise": {"min_win_rate": 0.6}}, pairwise=None)[0].detail


def test_min_win_rate_on_a_non_pairwise_judge_fails_loudly():
    """A winning pairwise section must not satisfy a wrong-type threshold."""
    regs = detect_regressions(
        {"quality": {"mean": 4.0, "scored_cases": 10}},
        {"quality": {"min_win_rate": 0.8}},
        pairwise=_pairwise_section("AAAAAAAAAA"))
    assert [(r.judge_name, r.metric, r.current_value) for r in regs] == [
        ("quality", "win_rate", "n/a")]
    assert "not a pairwise judge" in regs[0].detail


def test_a_caller_without_a_comparison_leaves_min_win_rate_alone():
    """`score.py judges` runs before `score.py pairwise` (and Harbor/EvalHub
    never run one), so the default must not fail the gate on a comparison that
    has not happened yet."""
    assert detect_regressions({}, {"pairwise": {"min_win_rate": 0.6}}) == []


def test_a_judge_gated_only_on_coverage_is_not_reported_as_passing():
    """The summary table picks one bound to display; the PASS/FAIL verdict has
    to come from the detector, or a judge whose only gate is `max_error_rate`
    renders green while the run exits 1."""
    summary = {"judges": {"q": {"mean": 4.0, "scored_cases": 1,
                                "errored_cases": 9}}, "per_case": {}}
    html = report._render_scoring_summary(
        summary, {"thresholds": {"q": {"max_error_rate": 0.2}}, "judges": []})
    assert "FAIL" in html and ">PASS<" not in html


# --- a metric the table cannot display is still an authoritative breach ------

def _statuses(judges, thresholds, pairwise=None):
    html = report._render_scoring_summary(_summary(judges, pairwise),
                                          {"thresholds": thresholds, "judges": []})
    return re.findall(r">(PASS|FAIL|SKIP|ERROR)<", html)


@pytest.mark.parametrize("label,judges,verdicts,thresholds,expected", [
    # win_rate and error rate are not columns in this table, so the old
    # "no mean and no pass_rate" branch swallowed them into SKIP. The pairwise
    # comparison has its own row, which takes the gate's verdict.
    ("win_rate breach", {}, "AAABBBBBTT",
     {"pairwise": {"min_win_rate": 0.6}}, "FAIL"),
    ("win_rate satisfied", {}, "AAAAAAAABT",
     {"pairwise": {"min_win_rate": 0.6}}, "PASS"),
    ("coverage breach, every case errored",
     {"q": {"mean": None, "pass_rate": None, "scored_cases": 0,
            "errored_cases": 10}}, None, {"q": {"max_error_rate": 0.2}}, "FAIL"),
    ("min_mean with no surviving case",
     {"q": {"mean": None, "pass_rate": None, "scored_cases": 0,
            "errored_cases": 10}}, None, {"q": {"min_mean": 1.0}}, "FAIL"),
    ("clean numeric", {"q": {"mean": 4.0, "scored_cases": 10}}, None,
     {"q": {"min_mean": 1.0}}, "PASS"),
])
def test_the_summary_status_matches_the_detector(label, judges, verdicts,
                                                 thresholds, expected):
    assert _statuses(judges, thresholds,
                     _pairwise_section(verdicts)) == [expected], label


def test_a_standalone_pairwise_coverage_breach_is_not_rendered_as_a_win():
    """With only `max_error_rate` set the row used to keep its head-to-head
    `WIN` while `score.py regression` exited 1."""
    thresholds = {"pairwise": {"max_error_rate": 0.0}}
    assert _statuses({}, thresholds, _pairwise_section("AAAAAAAAAEEEEE")) == ["FAIL"]
    html = report._render_scoring_summary(
        _summary({}, _pairwise_section("AAAAAAAAAEEEEE")),
        {"thresholds": thresholds, "judges": []})
    assert "&le; 0% errored" in html and ">WIN<" not in html
    # Within budget it is the detector's PASS, with the bound shown.
    assert _statuses({}, {"pairwise": {"max_error_rate": 0.5}},
                     _pairwise_section("AAAAAAAAAE")) == ["PASS"]


def test_an_ungated_pairwise_row_keeps_its_head_to_head_verdict():
    html = report._render_scoring_summary(
        _summary({}, _pairwise_section("AAB")), {"thresholds": {}, "judges": []})
    assert ">WIN<" in html


# --- a detector that cannot run is not a clean run ---------------------------

def _break_detector(monkeypatch, module):
    def boom(*_a, **_k):
        raise RuntimeError("engine unavailable")
    monkeypatch.setattr(module, "_detect_regressions", boom)


def test_the_regressions_section_says_so_when_it_cannot_evaluate(monkeypatch):
    _break_detector(monkeypatch, report)
    html = report._render_regressions({"judges": {"q": {"mean": 0.5}}},
                                      {"thresholds": {"q": {"min_mean": 1.0}}})
    assert "Could not evaluate thresholds" in html
    assert "engine unavailable" in html


def test_the_summary_does_not_claim_pass_when_it_cannot_evaluate(monkeypatch):
    """An empty breach set has to mean "checked, none" — not "never checked"."""
    _break_detector(monkeypatch, report)
    assert _statuses({"q": {"mean": 4.0, "scored_cases": 10}},
                     {"q": {"min_mean": 1.0}}) != ["PASS"]


def test_the_mlflow_helper_raises_rather_than_reporting_clean(monkeypatch):
    """It used to swallow every failure and return [], which the caller then
    tagged `regressions_detected=no` — indistinguishable from a clean run."""
    log_results = _load(
        REPO_ROOT / "skills" / "eval-mlflow" / "scripts" / "log_results.py",
        "_log_results_tag_test")

    # A working call still returns a list.
    assert log_results._detect_regressions(
        {"q": {"mean": 0.5, "scored_cases": 10}}, {"q": {"min_mean": 1.0}})

    # An unreachable engine raises instead of looking clean.
    monkeypatch.setattr(log_results, "Path", _MissingEnginePath)
    with pytest.raises(FileNotFoundError):
        log_results._detect_regressions({"q": {"mean": 1.0}},
                                        {"q": {"min_mean": 1.0}})


class _MissingEnginePath(type(Path())):
    """A Path whose resolved root has no score.py."""

    def resolve(self):
        return Path("/nonexistent-harness-root/skills/x/scripts/log_results.py")
