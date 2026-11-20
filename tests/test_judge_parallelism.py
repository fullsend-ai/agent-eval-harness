"""Case judging honours the run's case parallelism limit (issue #323).

`score_cases` judges cases concurrently. Its worker count comes from
`score.py judges --parallelism`, then `execution.judge_parallelism` (issue
#329), then `execution.parallelism`, then the CPU count, and is always capped
at the number of cases.
"""

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "skills" / "eval-run" / "scripts"))

import score as sc  # noqa: E402
from agent_eval.config import EvalConfig, JudgeConfig  # noqa: E402


def _config(parallelism=None, judge_parallelism=None):
    config = EvalConfig(name="t", skill="t")
    config.execution.parallelism = parallelism
    config.execution.judge_parallelism = judge_parallelism
    config.judges = [JudgeConfig(name="ok", check="return (True, 'ok')")]
    return config


def _cases(root, n):
    dirs = []
    for i in range(n):
        d = root / f"case-{i:03d}"
        d.mkdir(parents=True)
        dirs.append(d)
    return dirs


class _Spy:
    """Records the `max_workers` of every pool the scorer creates."""

    def __init__(self):
        self.max_workers = []

    def __call__(self, *args, max_workers=None, **kwargs):
        self.max_workers.append(max_workers)
        return ThreadPoolExecutor(*args, max_workers=max_workers, **kwargs)


def _workers(config, case_dirs, **kwargs):
    spy = _Spy()
    with patch("score.ThreadPoolExecutor", spy):
        result = sc.score_cases(sc.load_judges(config), case_dirs, config, **kwargs)
    assert spy.max_workers, "score_cases did not create a pool"
    return spy.max_workers[0], result


def test_configured_parallelism_reaches_case_judging(tmp_path):
    workers, _ = _workers(_config(15), _cases(tmp_path, 20))
    assert workers == 15


def test_unset_parallelism_keeps_cpu_default(tmp_path):
    with patch("score.os.cpu_count", return_value=3):
        workers, _ = _workers(_config(), _cases(tmp_path, 20))
    assert workers == 3


def test_unknown_cpu_count_falls_back_to_four(tmp_path):
    with patch("score.os.cpu_count", return_value=None):
        workers, _ = _workers(_config(), _cases(tmp_path, 20))
    assert workers == 4


def test_parallelism_is_capped_at_case_count(tmp_path):
    workers, _ = _workers(_config(15), _cases(tmp_path, 4))
    assert workers == 4


def test_explicit_parallelism_overrides_config(tmp_path):
    workers, _ = _workers(_config(15), _cases(tmp_path, 20), parallelism=6)
    assert workers == 6


def test_judge_parallelism_overrides_execution_parallelism(tmp_path):
    workers, _ = _workers(_config(2, judge_parallelism=12), _cases(tmp_path, 20))
    assert workers == 12


def test_judge_parallelism_overrides_cpu_default(tmp_path):
    with patch("score.os.cpu_count", return_value=3):
        workers, _ = _workers(_config(judge_parallelism=9), _cases(tmp_path, 20))
    assert workers == 9


def test_judge_parallelism_is_capped_at_case_count(tmp_path):
    workers, _ = _workers(_config(2, judge_parallelism=12), _cases(tmp_path, 4))
    assert workers == 4


def test_explicit_parallelism_overrides_judge_parallelism(tmp_path):
    workers, _ = _workers(_config(2, judge_parallelism=12), _cases(tmp_path, 20),
                          parallelism=6)
    assert workers == 6


def test_config_only_caller_picks_up_judge_parallelism(tmp_path):
    """A caller passing only the loaded config (EvalHub, Harbor, the Python
    API) gets `execution.judge_parallelism` from eval.yaml."""
    config_path = tmp_path / "eval.yaml"
    config_path.write_text(
        "name: t\nexecution: {mode: case, parallelism: 2, judge_parallelism: 11}\n"
        "judges:\n  - name: ok\n    check: \"return (True, 'ok')\"\n")
    config = EvalConfig.from_yaml(config_path)
    assert config.execution.judge_parallelism == 11
    assert config.execution.parallelism == 2
    workers, result = _workers(config, _cases(tmp_path / "cases", 20))
    assert workers == 11
    assert result["aggregated"]["ok"]["pass_rate"] == 1.0


def test_judge_parallelism_defaults_to_none(tmp_path):
    config_path = tmp_path / "eval.yaml"
    config_path.write_text("name: t\nexecution: {mode: case, parallelism: 5}\n")
    config = EvalConfig.from_yaml(config_path)
    assert config.execution.judge_parallelism is None
    workers, _ = _workers(config, _cases(tmp_path / "cases", 20))
    assert workers == 5


def test_judge_parallelism_reaches_pairwise(tmp_path):
    assert _pairwise_workers(_config(2, judge_parallelism=12), tmp_path, 20) == 12


@pytest.mark.parametrize("value", [0, -2])
def test_non_positive_parallelism_still_judges(tmp_path, value):
    workers, result = _workers(_config(value), _cases(tmp_path, 3))
    assert workers == 1
    assert result["aggregated"]["ok"]["pass_rate"] == 1.0


def test_results_do_not_depend_on_worker_count(tmp_path):
    config = _config()
    config.judges = [JudgeConfig(
        name="even",
        check=("from pathlib import Path\n"
               "n = int(Path(outputs['case_dir']).name[-1])\n"
               "return (n % 2 == 0, 'parity')"))]
    case_dirs = _cases(tmp_path, 6)
    _, serial = _workers(config, case_dirs, parallelism=1)
    _, parallel = _workers(config, case_dirs, parallelism=6)
    # Guard against comparing identical error results: every case must judge
    # successfully, with three true and three false verdicts.
    for result in (serial, parallel):
        assert sorted(result["aggregated"]["even"]["values"]) == [False] * 3 + [True] * 3
        for case in result["per_case"].values():
            assert not case["even"].get("error"), case
    assert serial["per_case"] == parallel["per_case"]
    # `values` are appended in completion order, so compare them as a multiset.
    for name, agg in serial["aggregated"].items():
        other = parallel["aggregated"][name]
        assert sorted(agg["values"]) == sorted(other["values"])
        assert ({k: v for k, v in agg.items() if k != "values"}
                == {k: v for k, v in other.items() if k != "values"})


def test_cli_flag_reaches_case_judging(tmp_path, monkeypatch):
    config_path = tmp_path / "eval.yaml"
    config_path.write_text(
        "name: t\nexecution: {mode: case, parallelism: 15}\n"
        "judges:\n  - name: ok\n    check: \"return (True, 'ok')\"\n")
    run_dir = tmp_path / "runs" / "t" / "run-a"
    _cases(run_dir / "cases", 20)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(sys, "argv", [
        "score.py", "judges", "--run-dir", str(run_dir),
        "--config", str(config_path), "--parallelism", "7"])
    spy = _Spy()
    with patch("score.ThreadPoolExecutor", spy):
        try:
            sc.main()
        except SystemExit as e:
            assert not e.code
    assert spy.max_workers[0] == 7


def _pairwise_workers(config, tmp_path, n_cases, **kwargs):
    case_ids = [f"case-{i:03d}" for i in range(n_cases)]
    for side in ("a", "b"):
        for cid in case_ids:
            (tmp_path / side / "cases" / cid).mkdir(parents=True)
    spy = _Spy()
    with patch("score.ThreadPoolExecutor", spy), \
            patch("score._get_anthropic_client", return_value=object()):
        result = sc.compare_runs(tmp_path / "a", tmp_path / "b", config,
                                 case_ids, model="anthropic:/judge", **kwargs)
    assert "error" not in result, result
    assert spy.max_workers, "compare_runs did not create a pool"
    return spy.max_workers[0]


def test_configured_parallelism_reaches_pairwise(tmp_path):
    assert _pairwise_workers(_config(15), tmp_path, 20) == 15


def test_explicit_parallelism_overrides_config_in_pairwise(tmp_path):
    assert _pairwise_workers(_config(15), tmp_path, 20, parallelism=6) == 6


def test_pairwise_parallelism_is_capped_at_case_count(tmp_path):
    assert _pairwise_workers(_config(15), tmp_path, 4) == 4


def test_pairwise_cli_flag_reaches_compare_runs(tmp_path, monkeypatch):
    config_path = tmp_path / "eval.yaml"
    config_path.write_text(
        "name: t\nexecution: {mode: case, parallelism: 15}\n"
        "models: {judge: 'anthropic:/judge'}\n"
        "judges:\n  - name: ok\n    check: \"return (True, 'ok')\"\n")
    runs = tmp_path / "runs"
    _cases(runs / "t" / "run-a" / "cases", 20)
    (runs / "t" / "run-b").mkdir(parents=True)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(runs))
    monkeypatch.setattr(sys, "argv", [
        "score.py", "pairwise", "--run-id", "run-a", "--baseline", "run-b",
        "--config", str(config_path), "--parallelism", "7"])
    spy = _Spy()
    with patch("score.ThreadPoolExecutor", spy), \
            patch("score._get_anthropic_client", return_value=object()):
        try:
            sc.main()
        except SystemExit as e:
            assert not e.code
    assert spy.max_workers[0] == 7


# Issue #341: both keys are validated once, at load, so a typo fails before
# any case runs and execution and judging see the same integer.

_KEYS = ("parallelism", "judge_parallelism")


def _load(tmp_path, key, value):
    config_path = tmp_path / "eval.yaml"
    config_path.write_text(
        f"name: t\nexecution:\n  mode: case\n  {key}: {value}\n"
        "judges:\n  - name: ok\n    check: \"return (True, 'ok')\"\n")
    return EvalConfig.from_yaml(config_path)


@pytest.mark.parametrize("key", _KEYS)
@pytest.mark.parametrize("value", ['"four"', '"4"', "2.7", "true", ".inf", ".nan", "[2]"])
def test_unusable_value_is_rejected_at_load(tmp_path, key, value):
    with pytest.raises(ValueError, match=rf"execution\.{key} must be an integer"):
        _load(tmp_path, key, value)


@pytest.mark.parametrize("key", _KEYS)
@pytest.mark.parametrize("value, stored, workers", [
    ("null", None, 3),   # CPU default (patched to 3)
    ("4", 4, 4),
    ("2.0", 2, 2),
    ("0", 0, 1),
    ("-3", -3, 1),
])
def test_accepted_values_load_and_resolve_unchanged(tmp_path, key, value, stored, workers):
    config = _load(tmp_path, key, value)
    loaded = getattr(config.execution, key)
    assert loaded == stored
    assert loaded is None or type(loaded) is int
    with patch("score.os.cpu_count", return_value=3):
        got, _ = _workers(config, _cases(tmp_path / "cases", 20))
    assert got == workers
