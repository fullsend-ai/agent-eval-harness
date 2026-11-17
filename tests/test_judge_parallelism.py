"""Case judging honours the run's case parallelism limit (issue #323).

`score_cases` judges cases concurrently. Its worker count comes from
`score.py judges --parallelism`, then `execution.parallelism`, then the CPU
count, and is always capped at the number of cases.
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


def _config(parallelism=None):
    config = EvalConfig(name="t", skill="t")
    config.execution.parallelism = parallelism
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
