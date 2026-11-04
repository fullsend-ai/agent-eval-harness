"""The EvalHub adapter under an OpenRouter plan (spec 014, PR-7): the plan is
built in the pod from the JobSpec model, the real claude-code runner (a fake
``claude`` binary) is bound to the session, the run is reconciled in the pod
and the provenance travels back in ``evaluation_metadata``; the client writes
it into ``run_result.json`` through the reconciling writer."""

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_eval.agent.claude_code import ClaudeCodeRunner  # noqa: E402
from agent_eval.evalhub import adapter as adapter_mod  # noqa: E402
from agent_eval.evalhub import runner as client  # noqa: E402
from agent_eval.evalhub.s3_dataset import DatasetInfo  # noqa: E402
from agent_eval.providers.openrouter.session import ProviderSession  # noqa: E402
from openrouter_fakes import FAKE_KEY, GEN_COST, FakeOpenRouter, fake_claude_records, install_fake_claude  # noqa: E402


class _StubModelConfig:
    def __init__(self, name="claude-sonnet-4", url="https://api.anthropic.com"):
        self.name, self.url, self.auth = name, url, None


class _StubJobSpec:
    def __init__(self, **kwargs):
        self.id = kwargs.get("id", "job-123")
        self.provider_id = kwargs.get("provider_id", "agent-eval")
        self.benchmark_id = kwargs.get("benchmark_id", "skill-eval-v1")
        self.benchmark_index = kwargs.get("benchmark_index", 0)
        self.model = kwargs.get("model", _StubModelConfig())
        self.parameters = kwargs.get("parameters", {})
        self.callback_url, self.num_examples, self.experiment_name = "", None, ""
        self.tags, self.exports = {}, {}


class _StubJobCallbacks:
    def __init__(self):
        self.statuses, self.results = [], []

    def report_status(self, update):
        self.statuses.append(update)

    def report_results(self, results):
        self.results.append(results)

    def create_oci_artifact(self, spec):
        return None


class FastSession(ProviderSession):
    def __init__(self, *a, **kw):
        kw.update(first_poll_s=0.0, poll_s=0.01, give_up_s=0.3, settle_s=0.0, key_poll_s=0.0, key_max_s=2.0)
        super().__init__(*a, **kw)


def _setup(tmp_path, base_url):
    cases = tmp_path / "cases"
    for cid in ("case-1", "case-2"):
        (cases / cid).mkdir(parents=True)
        (cases / cid / "input.yaml").write_text(f"id: {cid}\n")
    eval_yaml = tmp_path / "eval.yaml"
    eval_yaml.write_text(yaml.safe_dump({
        "name": "t", "runner": {"type": "claude-code"},
        "execution": {"prompt": "say hi", "max_budget_usd": 2},
        "dataset": {"path": str(cases)}, "outputs": [{"path": "output"}],
        "models": {"skill": "openrouter:/z-ai/glm-5.2:exacto", "providers": {"openrouter": {
            "base_url": base_url,
            "routing": {"models": {"z-ai/glm-5.2": {"order": ["novita"], "allow_fallbacks": False}}}}}},
        "judges": [{"name": "ok", "check": "return (True, 'ok')\n"}],
    }, sort_keys=False))
    return cases, eval_yaml


def test_adapter_runs_the_plan_in_the_pod(tmp_path, monkeypatch):
    install_fake_claude(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.setattr(adapter_mod, "SESSION_FACTORY", FastSession)
    fake = FakeOpenRouter()
    base = fake.start()
    try:
        cases, eval_yaml = _setup(tmp_path, base)
        info = DatasetInfo(num_cases=2, case_ids=["case-1", "case-2"], dest=cases)
        spec = _StubJobSpec(id="job-7", model=_StubModelConfig(name="openrouter:/z-ai/glm-5.2:exacto"))
        callbacks = _StubJobCallbacks()
        with (patch("agent_eval.evalhub.adapter.download_dataset", return_value=info),
              patch("agent_eval.evalhub.adapter.boto3"),
              patch("agent_eval.evalhub.adapter.RUNNERS", {"claude-code": ClaudeCodeRunner}),
              patch("agent_eval.evalhub.adapter._framework_adapter_init")):
            adapter = adapter_mod.AgentEvalAdapter(eval_config_path=str(eval_yaml))
            results = adapter.run_benchmark_job(spec, callbacks)
    finally:
        fake.stop()
    pod_cases = Path(adapter._tmp_dir.name) / "cases"        # the adapter works on its copy of the dataset
    prov = results.evaluation_metadata["provenance"]
    # the SDK forwards evaluation_metadata["artifacts"] only: the same dict travels there
    assert results.evaluation_metadata["artifacts"]["agent-eval.provenance"] is prov
    assert prov["cost_source"] == "openrouter:generation"
    assert prov["cost_usd"] == pytest.approx(4 * GEN_COST, abs=1e-6)     # 2 cases × 2 generations, priced
    assert prov["routing"]["violations"] == [] and prov["routing"]["audit_complete"] is True
    assert prov["provider"]["runner"] == "evalhub" and prov["provider"]["key_scope"] == "operator"
    assert (prov["cases_priced"] == 2 and prov["message_ids_count"] == 4
            and prov["generation_ids_count"] == 4
            and "message_ids" not in prov and "generation_ids" not in prov)
    metrics = {r.metric_name: r.metric_value for r in results.results}
    assert metrics["cost_usd"] == pytest.approx(4 * GEN_COST, abs=1e-6)   # the reconciled value, not 2 × $0.5
    assert metrics["exit_code"] == 0 and results.model_name == "openrouter:/z-ai/glm-5.2:exacto"
    # the pod's agent saw the operator key only as the overlay token, none of the host Vertex env
    for cid in ("case-1", "case-2"):
        rec = fake_claude_records(pod_cases / cid)[-1]
        assert json.loads((Path(adapter._tmp_dir.name) / "run" / "cases" / cid / "run_result.json").read_text())[
            "cost_source"] == "openrouter:generation"
        assert rec["token_sha"] == hashlib.sha256(FAKE_KEY.encode()).hexdigest()[:8]
        assert "CLAUDE_CODE_USE_VERTEX" not in rec["env_keys"] and "OPENROUTER_API_KEY" not in rec["env_keys"]
        assert rec["argv"][rec["argv"].index("--model") + 1] == "z-ai/glm-5.2:exacto"


def test_adapter_without_a_plan_is_unchanged(tmp_path, monkeypatch):
    """A bare model keeps the pre-plan path: no session, the runners' own cost summed."""
    from unittest.mock import MagicMock

    from agent_eval.agent.base import RunResult

    cases, eval_yaml = _setup(tmp_path, "https://openrouter.ai/api")
    raw = yaml.safe_load(eval_yaml.read_text())
    raw["models"] = {"skill": "claude-sonnet-4-5"}
    eval_yaml.write_text(yaml.safe_dump(raw))
    runner = MagicMock()
    runner.execute.return_value = RunResult(exit_code=0, stdout="x", stderr="", duration_s=1.0, cost_usd=0.05,
                                            num_turns=5, resolved_model="claude-sonnet-4-20250514")
    runner_cls = MagicMock()
    runner_cls.from_config.return_value = runner
    info = DatasetInfo(num_cases=2, case_ids=["case-1", "case-2"], dest=cases)
    with (patch("agent_eval.evalhub.adapter.download_dataset", return_value=info),
          patch("agent_eval.evalhub.adapter.boto3"),
          patch("agent_eval.evalhub.adapter.RUNNERS", {"claude-code": runner_cls}),
          patch("agent_eval.evalhub.adapter._framework_adapter_init")):
        results = adapter_mod.AgentEvalAdapter(eval_config_path=str(eval_yaml)).run_benchmark_job(
            _StubJobSpec(model=_StubModelConfig(name="claude-sonnet-4-5")), _StubJobCallbacks())
    assert "provenance" not in results.evaluation_metadata
    assert {r.metric_name: r.metric_value for r in results.results}["cost_usd"] == pytest.approx(0.10)
    assert runner_cls.from_config.call_args.kwargs == {"log_prefix": "evalhub"}
    assert not runner.bind_provider.called


def test_client_writes_the_pod_provenance_through_the_writer(tmp_path):
    provenance = {"cost_usd": 0.004, "cost_source": "openrouter:generation", "cost_confidence": "high",
                  "routing": {"enforcement": "audit", "violations": [], "audit_complete": True},
                  "provider": {"runner": "evalhub", "key_scope": "operator"}}
    # the real SDK shape: BenchmarkResult.artifacts carries what the callbacks forwarded
    bench = SimpleNamespace(metrics={"exit_code": 0, "cost_usd": 0.004, "num_examples_evaluated": 2},
                            mlflow_run_id="r1", artifacts={"agent-eval.provenance": provenance,
                                                           "evalhub.env_card": {"python": "3.12"}})
    job = SimpleNamespace(results=SimpleNamespace(mlflow_experiment_url="http://mlflow/1"))
    meta = client._run_meta(job, bench, bench.metrics, model="openrouter:/z-ai/glm-5.2", provider_id="agent-eval",
                            job_id="j1")
    assert meta["cost_source"] == "openrouter:generation" and meta["routing"]["violations"] == []
    assert meta["provider"]["runner"] == "evalhub" and meta["execution_mode"] == "evalhub"
    # in-process/stub objects may still carry evaluation_metadata on the job results
    bench2 = SimpleNamespace(metrics=bench.metrics, mlflow_run_id="r1")
    job2 = SimpleNamespace(results=SimpleNamespace(mlflow_experiment_url=None,
                                                   evaluation_metadata={"provenance": provenance}))
    assert client._run_meta(job2, bench2, bench.metrics, model="m", provider_id="p", job_id="j")["cost_source"] \
        == "openrouter:generation"
    # and without provenance the legacy shape is untouched
    job3 = SimpleNamespace(results=SimpleNamespace(mlflow_experiment_url=None))
    plain = client._run_meta(job3, SimpleNamespace(metrics=bench.metrics, mlflow_run_id=None), bench.metrics,
                             model="m", provider_id="p", job_id="j")
    assert "cost_source" not in plain and plain["cost_usd"] == 0.004


def test_a_runner_construction_failure_still_closes_the_session(tmp_path, monkeypatch):
    """from_config runs under the session's guard: the backfill worker stops
    and (at key-guardrail) the key is revoked even when the runner cannot be built."""
    from unittest.mock import MagicMock

    from openrouter_fakes import FAKE_MGMT_KEY, quiet_atexit

    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", FAKE_MGMT_KEY)
    monkeypatch.setattr(adapter_mod, "SESSION_FACTORY", FastSession)
    quiet_atexit(monkeypatch)
    fake = FakeOpenRouter()
    base = fake.start()
    try:
        cases, eval_yaml = _setup(tmp_path, base)
        raw = yaml.safe_load(eval_yaml.read_text())
        raw["models"]["providers"]["openrouter"]["routing"]["enforcement"] = "key-guardrail"
        raw["models"]["providers"]["openrouter"]["budget"] = {"run_usd": 0.5}
        eval_yaml.write_text(yaml.safe_dump(raw))
        runner_cls = MagicMock()
        runner_cls.from_config.side_effect = FileNotFoundError("plugin dir missing in the pod")
        info = DatasetInfo(num_cases=2, case_ids=["case-1", "case-2"], dest=cases)
        with (patch("agent_eval.evalhub.adapter.download_dataset", return_value=info),
              patch("agent_eval.evalhub.adapter.boto3"),
              patch("agent_eval.evalhub.adapter.RUNNERS", {"claude-code": runner_cls}),
              patch("agent_eval.evalhub.adapter._framework_adapter_init")):
            with pytest.raises(FileNotFoundError):
                adapter_mod.AgentEvalAdapter(eval_config_path=str(eval_yaml)).run_benchmark_job(
                    _StubJobSpec(model=_StubModelConfig(name="openrouter:/z-ai/glm-5.2:exacto")), _StubJobCallbacks())
    finally:
        fake.stop()
    assert len(fake.issued) == 1 and fake.deletes == list(fake.issued)

