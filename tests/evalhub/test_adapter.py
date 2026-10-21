"""Tests for EvalHub FrameworkAdapter."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from agent_eval.agent.base import RunResult
from agent_eval.config import EvalConfig
from agent_eval.evalhub.s3_dataset import DatasetInfo


def _make_eval_yaml(
    tmpdir: Path,
    skill: str = "test-skill",
    arguments: str = "--input {prompt}",
    runner_type: str = "claude-code",
    runner_extra: dict = None,
) -> Path:
    config = {
        "name": "test-eval",
        "execution": {
            "skill": skill,
            "mode": "case",
            "arguments": arguments,
            "timeout": 300,
            "max_budget_usd": 2.0,
        },
        "runner": {"type": runner_type, **(runner_extra or {})},
        "permissions": {"allow": ["Bash", "Read"]},
        "dataset": {"path": "cases"},
    }
    path = tmpdir / "eval.yaml"
    path.write_text(yaml.dump(config))
    return path


def _make_case_dir(cases_dir: Path, case_id: str, input_data: dict) -> Path:
    case_dir = cases_dir / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "input.yaml").write_text(yaml.dump(input_data))
    return case_dir


def _make_run_result(exit_code: int = 0, duration: float = 10.0) -> RunResult:
    return RunResult(
        exit_code=exit_code,
        stdout="output text",
        stderr="",
        duration_s=duration,
        cost_usd=0.05,
        num_turns=5,
        resolved_model="claude-sonnet-4-20250514",
    )


class _StubModelConfig:
    def __init__(self, name="claude-sonnet-4", url="https://api.anthropic.com"):
        self.name = name
        self.url = url
        self.auth = None


class _StubJobSpec:
    def __init__(self, **kwargs):
        self.id = kwargs.get("id", "job-123")
        self.provider_id = kwargs.get("provider_id", "agent-eval")
        self.benchmark_id = kwargs.get("benchmark_id", "skill-eval-v1")
        self.benchmark_index = kwargs.get("benchmark_index", 0)
        self.model = kwargs.get("model", _StubModelConfig())
        self.parameters = kwargs.get("parameters", {})
        self.callback_url = kwargs.get("callback_url", "")
        self.num_examples = kwargs.get("num_examples", None)
        self.experiment_name = kwargs.get("experiment_name", "")
        self.tags = kwargs.get("tags", {})
        self.exports = kwargs.get("exports", {})


class _StubJobCallbacks:
    def __init__(self):
        self.statuses = []
        self.results = []

    def report_status(self, update):
        self.statuses.append(update)

    def report_results(self, results):
        self.results.append(results)

    def create_oci_artifact(self, spec):
        return None


def _run_adapter(tmpdir, mock_runner, eval_yaml, config, callbacks):
    """Helper: run the adapter with a mocked runner via the RUNNERS registry."""
    cases_dir = tmpdir / "cases"
    dataset_info = DatasetInfo(
        num_cases=len(list(cases_dir.iterdir())),
        case_ids=sorted(d.name for d in cases_dir.iterdir() if d.is_dir()),
        dest=cases_dir,
    )
    mock_runner_cls = MagicMock()
    mock_runner_cls.from_config.return_value = mock_runner

    with (
        patch("agent_eval.evalhub.adapter.download_dataset", return_value=dataset_info),
        patch("agent_eval.evalhub.adapter.boto3"),
        patch("agent_eval.evalhub.adapter.RUNNERS", {"claude-code": mock_runner_cls}),
        patch("agent_eval.evalhub.adapter._framework_adapter_init"),
    ):
        from agent_eval.evalhub.adapter import AgentEvalAdapter
        adapter = AgentEvalAdapter(eval_config_path=str(eval_yaml))
        return adapter.run_benchmark_job(config, callbacks)


def test_adapter_runs_skill_and_scores():
    """Adapter runs skill per case and returns JobResults."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        eval_yaml = _make_eval_yaml(tmpdir)
        _make_case_dir(tmpdir / "cases", "case-001", {"prompt": "hello world"})
        _make_case_dir(tmpdir / "cases", "case-002", {"prompt": "goodbye world"})

        mock_runner = MagicMock()
        mock_runner.execute.return_value = _make_run_result(exit_code=0)

        config = _StubJobSpec(
            id="job-abc", benchmark_id="bench-1", benchmark_index=0,
            model=_StubModelConfig(name="claude-sonnet-4"),
            parameters={"s3_bucket": "test-bucket", "s3_prefix": "dataset/v1"},
        )
        callbacks = _StubJobCallbacks()
        result = _run_adapter(tmpdir, mock_runner, eval_yaml, config, callbacks)

        assert mock_runner.execute.call_count == 2
        calls = mock_runner.execute.call_args_list
        for call in calls:
            args_val = call.kwargs.get("args", "")
            assert args_val in ("--input hello world", "--input goodbye world")
        assert len(callbacks.statuses) >= 4
        assert result.id == "job-abc"
        assert result.model_name == "claude-sonnet-4"
        assert result.num_examples_evaluated == 2
        assert result.benchmark_id == "bench-1"


def test_adapter_handles_runner_failure():
    """Adapter returns results even when runner exits with error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        eval_yaml = _make_eval_yaml(tmpdir)
        _make_case_dir(tmpdir / "cases", "case-001", {"prompt": "fail case"})

        mock_runner = MagicMock()
        mock_runner.execute.return_value = _make_run_result(exit_code=1, duration=5.0)

        config = _StubJobSpec(
            id="job-fail", benchmark_id="bench-1",
            model=_StubModelConfig(name="claude-sonnet-4"),
            parameters={"s3_bucket": "test-bucket", "s3_prefix": "dataset/v1"},
        )
        callbacks = _StubJobCallbacks()
        result = _run_adapter(tmpdir, mock_runner, eval_yaml, config, callbacks)

        assert result is not None
        assert result.id == "job-fail"
        assert result.num_examples_evaluated == 1
        exit_metric = next(
            (r for r in result.results if r.metric_name == "exit_code"), None)
        assert exit_metric is not None
        assert exit_metric.metric_value == 1


def test_adapter_rejects_fullsend_runner():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        eval_yaml = _make_eval_yaml(
            tmpdir, runner_type="fullsend", runner_extra={"agent": "a"})
        config = _StubJobSpec()
        callbacks = _StubJobCallbacks()

        with patch("agent_eval.evalhub.adapter._framework_adapter_init"):
            from agent_eval.evalhub.adapter import AgentEvalAdapter
            adapter = AgentEvalAdapter(eval_config_path=str(eval_yaml))
            with pytest.raises(ValueError, match=r"does not support runner\.type: fullsend"):
                adapter.run_benchmark_job(config, callbacks)


def test_adapter_rejects_cursor_runner():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        eval_yaml = _make_eval_yaml(tmpdir, runner_type="cursor")
        config = _StubJobSpec()
        callbacks = _StubJobCallbacks()

        with patch("agent_eval.evalhub.adapter._framework_adapter_init"):
            from agent_eval.evalhub.adapter import AgentEvalAdapter
            adapter = AgentEvalAdapter(eval_config_path=str(eval_yaml))
            with pytest.raises(ValueError, match=r"does not support runner\.type: cursor"):
                adapter.run_benchmark_job(config, callbacks)
