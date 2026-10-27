"""Tests for log_results.py path-safety helpers (harbor job-dir traversal)."""

from pathlib import Path

import pytest

try:
    from log_results import _is_within, _safe_trajectory_path
    _has_mlflow = True
except ModuleNotFoundError as exc:
    # Skip only when mlflow itself is missing; re-raise unrelated import errors
    # so path-safety regressions are never silently skipped.
    if exc.name != "mlflow":
        raise
    _has_mlflow = False
except SystemExit as exc:
    # log_results.py calls sys.exit(0) when mlflow is missing at import time.
    if exc.code not in (0, None):
        raise
    _has_mlflow = False


@pytest.mark.skipif(not _has_mlflow, reason="mlflow not installed")
class TestSafeTrajectoryPath:
    def test_accepts_real_file_under_job_root(self, tmp_path):
        """A regular trajectory.json alongside the transcript is accepted."""
        job_root = tmp_path / "job"
        step_dir = job_root / "case__abc" / "agent"
        step_dir.mkdir(parents=True)
        transcript = step_dir / "claude-code.txt"
        transcript.write_text("{}\n")
        traj = step_dir / "trajectory.json"
        traj.write_text("{}")

        result = _safe_trajectory_path(transcript, job_root)
        assert result == traj

    def test_missing_trajectory_returns_none(self, tmp_path):
        """No trajectory.json present -> None, no error."""
        job_root = tmp_path / "job"
        step_dir = job_root / "case__abc" / "agent"
        step_dir.mkdir(parents=True)
        transcript = step_dir / "claude-code.txt"
        transcript.write_text("{}\n")

        assert _safe_trajectory_path(transcript, job_root) is None

    def test_rejects_symlinked_trajectory(self, tmp_path):
        """A trajectory.json symlink pointing outside job_root is rejected."""
        job_root = tmp_path / "job"
        step_dir = job_root / "case__abc" / "agent"
        step_dir.mkdir(parents=True)
        transcript = step_dir / "claude-code.txt"
        transcript.write_text("{}\n")

        secret = tmp_path / "outside" / "secret.json"
        secret.parent.mkdir(parents=True)
        secret.write_text('{"leaked": true}')
        traj = step_dir / "trajectory.json"
        traj.symlink_to(secret)

        assert _safe_trajectory_path(transcript, job_root) is None

    def test_rejects_symlink_even_when_target_is_inside_root(self, tmp_path):
        """Symlinks are rejected outright, regardless of where they point."""
        job_root = tmp_path / "job"
        step_dir = job_root / "case__abc" / "agent"
        step_dir.mkdir(parents=True)
        transcript = step_dir / "claude-code.txt"
        transcript.write_text("{}\n")

        real = job_root / "real_trajectory.json"
        real.write_text("{}")
        traj = step_dir / "trajectory.json"
        traj.symlink_to(real)

        assert _safe_trajectory_path(transcript, job_root) is None


@pytest.mark.skipif(not _has_mlflow, reason="mlflow not installed")
class TestIsWithin:
    def test_descendant_path_is_within(self, tmp_path):
        root = tmp_path / "root"
        child = root / "a" / "b.txt"
        child.parent.mkdir(parents=True)
        child.write_text("x")
        assert _is_within(child, root) is True

    def test_escaping_path_is_not_within(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("x")
        assert _is_within(outside, root) is False

    def test_nonexistent_path_is_not_within(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        assert _is_within(root / "missing.txt", root) is False


@pytest.mark.skipif(not _has_mlflow, reason="mlflow not installed")
class TestCostMetricsFromSummary:
    """Judge-side spend reaches MLflow as its own metrics (spec 014 Decision
    15): never folded into `cost_usd`, never re-derived from an estimate."""

    def test_logs_judge_and_total_cost_when_numeric(self):
        from log_results import _cost_metrics_from_summary

        summary = {"judge_usage": {"judge_cost_usd": 0.25, "requests": 8,
                                   "requests_missing_cost": 0,
                                   "tool_choice_fallbacks": 1},
                   "total_cost_usd": 1.75, "total_cost_source": "complete"}
        metrics, tags = _cost_metrics_from_summary(summary)
        assert metrics == {"judge_cost_usd": 0.25, "judge/requests": 8,
                           "judge/requests_missing_cost": 0,
                           "judge/tool_choice_fallbacks": 1,
                           "total_cost_usd": 1.75}
        assert tags == {"total_cost_source": "complete"}

    def test_null_costs_are_not_logged_but_the_source_is_tagged(self):
        from log_results import _cost_metrics_from_summary

        summary = {"judge_usage": {"judge_cost_usd": None, "requests": 4,
                                   "requests_missing_cost": 4},
                   "total_cost_usd": None, "total_cost_source": "agent-only"}
        metrics, tags = _cost_metrics_from_summary(summary)
        assert "judge_cost_usd" not in metrics and "total_cost_usd" not in metrics
        assert metrics["judge/requests_missing_cost"] == 4
        assert tags == {"total_cost_source": "agent-only"}

    def test_summary_without_judge_usage_logs_nothing(self):
        from log_results import _cost_metrics_from_summary

        assert _cost_metrics_from_summary({"judges": {}}) == ({}, {})
        assert _cost_metrics_from_summary({}) == ({}, {})


@pytest.mark.skipif(not _has_mlflow, reason="mlflow not installed")
class TestProvenanceTagsAndMetrics:
    def test_tags_and_metrics_from_a_reconciled_run(self):
        from log_results import _provenance_metrics, _provenance_tags

        rr = {"cost_source": "openrouter:generation", "cost_confidence": "high",
              "routing": {"enforcement": "audit", "sha": "9f3a", "violations": [], "audit_complete": True},
              "provider": {"key_scope": "operator"}, "budget": {"enforcement": "cli-estimate"},
              "hook_cost_usd": 0.004,
              "providers": {"novita": {"requests": 3, "cost_usd": 0.03},
                            "unknown": {"requests": 1, "cost_usd": None}}}
        assert _provenance_tags(rr) == {
            "cost_source": "openrouter:generation", "cost_confidence": "high",
            "routing_enforcement": "audit", "routing_sha": "9f3a", "audit_violations": "0",
            "audit_clean": "yes", "key_scope": "operator", "budget_enforcement": "cli-estimate"}
        assert _provenance_metrics(rr) == {"hook_cost_usd": 0.004, "provider/novita/cost_usd": 0.03}

    def test_legacy_run_yields_nothing(self):
        from log_results import _provenance_metrics, _provenance_tags

        assert _provenance_tags({"cost_usd": 1.0}) == {}
        assert _provenance_metrics({"cost_usd": 1.0}) == {}
        dirty = _provenance_tags({"routing": {"violations": [{"gen_id": "x"}], "audit_complete": False}})
        assert dirty["audit_clean"] == "no" and dirty["audit_violations"] == "1"


@pytest.mark.skipif(not _has_mlflow, reason="mlflow not installed")
class TestRunnerProvenanceParams:
    def test_logs_non_null_fields_the_runner_declares(self):
        from log_results import _runner_provenance_params

        from agent_eval.agent import RUNNERS

        keys = RUNNERS["fullsend"].PROVENANCE_KEYS
        eval_params = {key: f"v-{key}" for key in keys}
        eval_params["sandbox_arch"] = None
        eval_params["max_budget_usd"] = 5  # not provenance
        got = _runner_provenance_params("fullsend", {"eval_params": eval_params})
        assert got == {k: f"v-{k}" for k in keys if k != "sandbox_arch"}

    def test_other_runners_and_missing_params_log_nothing(self):
        from log_results import _runner_provenance_params

        rr = {"eval_params": {"fullsend_version": "1", "sandbox_image": "x"}}
        assert _runner_provenance_params("claude-code", rr) == {}
        assert _runner_provenance_params("no-such-runner", rr) == {}
        assert _runner_provenance_params("fullsend", {}) == {}
        assert _runner_provenance_params("fullsend", {"eval_params": None}) == {}

    def test_recorded_runner_wins_over_the_configured_one(self):
        from log_results import _runner_provenance_params

        from agent_eval.agent import RUNNERS

        keys = RUNNERS["fullsend"].PROVENANCE_KEYS
        eval_params = {key: f"v-{key}" for key in keys}
        # --agent fullsend over a claude-code config
        rr = {"agent": "fullsend", "eval_params": eval_params}
        assert _runner_provenance_params("claude-code", rr) == eval_params
        # --agent claude-code over a fullsend config: nothing to log
        rr = {"agent": "claude-code", "eval_params": eval_params}
        assert _runner_provenance_params("fullsend", rr) == {}
        # runs that never recorded the agent fall back to the config
        rr = {"eval_params": eval_params}
        assert _runner_provenance_params("fullsend", rr) == eval_params

    def test_log_results_does_not_import_the_concrete_runner_module(self):
        import log_results

        assert not hasattr(log_results, "FULLSEND_PROVENANCE_KEYS")
        assert "agent_eval.agent.fullsend" not in Path(log_results.__file__).read_text()
