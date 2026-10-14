"""EvalHub FrameworkAdapter for agent-eval-harness.

Orchestrates the full evaluation loop inside the EvalHub Job pod:
download dataset → run agent per case → score with judges → map to JobResults.

The adapter runs IN-PROCESS (matching EvalHub's architecture where adapter pods
are execution-only). It uses the runner registry (ClaudeCodeRunner, CodexRunner,
CliRunner, ResponsesAPIRunner) based on ``runner.type`` in eval.yaml — no Harbor,
no sub-pods. Cursor is local-only and is rejected explicitly because the base
EvalHub image does not ship its CLI.

Resources (eval.yaml, dataset, project) can come from:
- The container filesystem (baked into the image or mounted)
- S3 (EvalHub's standard dataset delivery)
- Kubernetes ConfigMaps (passed as job parameters — no image rebuild needed;
  created programmatically via ``agent_eval.harbor.k8s_resources``)

Uses conditional imports so the module works without eval-hub-sdk
installed (stubs are provided for testing/CI).
"""

import json
import logging
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

try:
    import boto3
except ImportError:
    boto3 = None  # type: ignore[assignment]

from agent_eval.agent import RUNNERS
from agent_eval.agent.base import RunResult
from agent_eval.config import EvalConfig, resolve_arguments
from agent_eval.evalhub.results_mapper import map_to_job_results
from agent_eval.evalhub.s3_dataset import DatasetInfo, download_dataset

SESSION_FACTORY = None       # tests inject a ProviderSession subclass with fake timing

# Provenance fields of the reconciled run-level run_result.json that travel
# back to the client (results_mapper.PROVENANCE_ARTIFACT). The id list itself
# stays in the pod; its size travels as message_ids_count.
PROVENANCE_FIELDS = ("cost_usd", "cost_source", "cost_confidence", "cost_usd_estimate",
                     "cost_coverage", "cost_warnings", "cases_priced", "hook_cost_usd",
                     "providers", "routing", "provider", "budget")

try:
    from evalhub.adapter import (
        EvaluationResult,
        FrameworkAdapter,
        JobCallbacks,
        JobResults,
        JobSpec,
        JobStatus,
        JobPhase,
        JobStatusUpdate,
    )
    from evalhub.adapter.models.job import MessageInfo, ModelConfig

    EVALHUB_AVAILABLE = True
except ImportError:
    from agent_eval.evalhub.stubs import (  # type: ignore[assignment]
        EvaluationResult,
        FrameworkAdapter,
        JobCallbacks,
        JobResults,
        JobSpec,
        JobStatus,
        JobPhase,
        JobStatusUpdate,
        MessageInfo,
        ModelConfig,
    )

    EVALHUB_AVAILABLE = False


_score_module = None
_score_module_loaded = False


def _get_score_module():
    """Load scoring module from eval-run scripts once, cache the result."""
    global _score_module, _score_module_loaded
    if _score_module_loaded:
        return _score_module
    _score_module_loaded = True
    try:
        import importlib.util
        score_path = Path(__file__).parent.parent.parent / "skills" / "eval-run" / "scripts" / "score.py"
        spec = importlib.util.spec_from_file_location("score", score_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if hasattr(mod, "load_judges") and hasattr(mod, "score_cases"):
            _score_module = mod
    except Exception as exc:
        log.warning("Judge scoring unavailable: %s", exc)
    return _score_module


def _load_judges_and_score(eval_config, case_dirs):
    """Try to score using eval-run scripts. Returns aggregated dict on success."""
    mod = _get_score_module()
    if mod is None:
        return {}
    try:
        judges = mod.load_judges(eval_config)
        result = mod.score_cases(judges, case_dirs, eval_config)
        return result.get("aggregated", {})
    except Exception as exc:
        log.warning("Judge scoring failed: %s", exc)
        return {}


def _framework_adapter_init(adapter_instance):
    """Call FrameworkAdapter.__init__. Extracted for testability."""
    FrameworkAdapter.__init__(adapter_instance)


def _read_configmap(name: str, namespace: str) -> dict[str, str]:
    """Read a ConfigMap's data via the Kubernetes API.

    Works in-cluster (ServiceAccount token) and locally (kubeconfig).
    Returns the ConfigMap's ``data`` dict, or raises on error.
    """
    from kubernetes import client as k8s_client, config as k8s_config
    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()
    core = k8s_client.CoreV1Api()
    cm = core.read_namespaced_config_map(name, namespace)
    return cm.data or {}


def _configmap_to_dir(cm_data: dict[str, str], dest: Path) -> None:
    """Write ConfigMap data to a directory, restoring ``--`` path separators.

    ConfigMap keys use ``--`` instead of ``/`` (created by
    ``k8s_resources._collect_files``). This reverses that encoding so the
    directory structure matches the original project layout.
    """
    dest_resolved = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    for key, content in cm_data.items():
        rel_path = key.replace("--", "/")
        file_path = (dest / rel_path).resolve()
        if not file_path.is_relative_to(dest_resolved):
            raise ValueError(f"Path traversal detected in ConfigMap key: {key}")
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)


def _start_provider_plan(eval_config, model_name, runner_type, *, run_id, run_dir):
    """The pod-side half of spec 014's startup order: plan (iff the effective
    skill model is ``openrouter:/``), preflight, session. Returns
    ``(plan, session, run_dir)`` — ``(None, None, None)`` without a plan."""
    from agent_eval.providers.openrouter.plan import build_plan, effective_roles, plan_is_active

    roles = effective_roles(eval_config, {"skill": model_name})
    if not plan_is_active(roles):
        return None, None, None
    if runner_type != "claude-code":
        raise ValueError(f"the direct OpenRouter transport is implemented for the claude-code "
                         f"runner; runner.type {runner_type!r} cannot run {roles['skill']!r}")
    from agent_eval.config import OpenRouterConfig
    from agent_eval.providers.base import ConfigError
    from agent_eval.providers.openrouter.plan import judge_routing_keys, judge_verdict_policies
    from agent_eval.providers.openrouter.preflight import run_preflight
    from agent_eval.providers.openrouter.session import ProviderSession

    try:
        plan = build_plan(eval_config, roles, runner="evalhub", run_id=run_id)
    except ConfigError as exc:
        raise ValueError(f"OpenRouter plan: {exc}") from None
    orc = getattr(getattr(eval_config.models, "providers", None), "openrouter", None) or OpenRouterConfig()
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        pre = run_preflight(plan, level=orc.preflight, run_dir=run_dir,
                            judges=judge_routing_keys(eval_config),
                            verdict=getattr(eval_config.models, "judge_verdict", "auto"),
                            judge_verdicts=judge_verdict_policies(eval_config))
        for warning in pre.warnings:
            log.warning("preflight: %s", warning)
        factory = SESSION_FACTORY or ProviderSession
        session = factory(plan, run_dir, parallelism=1, snapshot=pre.snapshot).start()
    except BaseException:
        plan.close()                     # a per-run key must not outlive a failed startup
        raise
    plan.attach(session)
    scope = "per-run" if plan.key_scope == "per-run" else "operator"
    log.info("Provider: openrouter direct | model: %s | enforcement: %s | preflight: %s%s | "
             "key exposed to agent: %s key sha256:%s", plan.skill.id, plan.enforcement, orc.preflight,
             f" (degraded: {pre.degraded_reason})" if pre.degraded_reason else "", scope, plan.key_hash)
    return plan, session, run_dir


def _case_payload(result: RunResult) -> dict:
    return {
        "exit_code": result.exit_code,
        "duration_s": round(result.duration_s, 1),
        "token_usage": result.token_usage,
        "cost_usd": result.cost_usd,
        "num_turns": result.num_turns,
        "per_model_usage": result.per_model_usage,
        "per_model_turns": result.per_model_turns,
        "permission_denials": result.permission_denials or [],
        "message_ids": result.message_ids or [],
        "cost_source": result.cost_source,
        "error_class": result.error_class,
        "budget": result.budget,
    }


def _write_case_result(run_dir: Path, case_id: str, result: RunResult, session) -> dict:
    """Per-case ``run_result.json`` in the pod's run dir (the reconciling
    writer; the run-end pass converges it once the backfill lands)."""
    from agent_eval.providers.reconcile import write_run_result

    return write_run_result(Path(run_dir) / "cases" / case_id / "run_result.json",
                            _case_payload(result), plan=session.plan, ledger=session.ledger,
                            catalog=session.catalog)


def _finish_provider_plan(session, run_dir: Path, case_results: list) -> dict:
    """Run end in the pod: write the run-level file, then close the session —
    drain the backfill, read the key usage, run the final reconcile pass and,
    at ``key-guardrail``, revoke the per-run key — **before** the provenance
    leaves the pod, so a revoke failure (``cost_warnings`` naming the key
    hash) travels with the results; the pod's run dir is a temp dir nobody
    can retry against. Returns the provenance fields for the client."""
    from agent_eval.providers.reconcile import write_run_result

    per_case = {cr["case_id"]: _case_payload(cr["run_result"]) for cr in case_results}
    payload = {
        "exit_code": max((cr["run_result"].exit_code for cr in case_results), default=-1, key=abs),
        "execution_mode": "evalhub", "cost_usd": None, "per_case": per_case,
        "message_ids": sorted({i for cr in case_results for i in (cr["run_result"].message_ids or [])}),
    }
    write_run_result(Path(run_dir) / "run_result.json", payload, plan=session.plan, ledger=session.ledger,
                     catalog=session.catalog)
    session.close()
    try:
        final = json.loads((Path(run_dir) / "run_result.json").read_text())
    except (OSError, ValueError):
        final = payload
    provenance = {k: final.get(k) for k in PROVENANCE_FIELDS if k in final}
    provenance["message_ids_count"] = len(final.get("message_ids") or [])
    return provenance


def _get_namespace() -> str:
    """Get the current K8s namespace (in-cluster or from kubeconfig)."""
    ns_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
    if ns_path.is_file():
        return ns_path.read_text().strip() or "default"
    ns = os.environ.get("AGENT_EVAL_K8S_NAMESPACE")
    if ns:
        return ns
    return "default"


class AgentEvalAdapter(FrameworkAdapter):
    """EvalHub adapter that runs agent evaluations in-process.

    Dispatches to the runner specified by ``runner.type`` in eval.yaml
    (claude-code, cli, responses-api). Runs all cases in the Job pod.

    Supports three resource delivery modes (checked in order):
    1. **ConfigMap** — job parameters ``eval_configmap``, ``dataset_configmap``,
       ``project_configmap`` name ConfigMaps to read via the K8s API. No image
       rebuild needed; created programmatically via ``k8s_resources``.
    2. **Filesystem** — eval.yaml + cases baked into the image or volume-mounted.
    3. **S3** — ``s3_bucket`` + ``s3_prefix`` parameters (EvalHub's standard).
    """

    def __init__(self, eval_config_path: str = "eval.yaml"):
        _framework_adapter_init(self)
        self._eval_config_path = eval_config_path

    def run_benchmark_job(self, config: JobSpec, callbacks: JobCallbacks) -> JobResults:
        start_time = time.monotonic()
        params = config.parameters or {}
        log.info("run_benchmark_job starting: params=%s", list(params.keys()))

        # Temp dir for materializing ConfigMap content
        self._tmp_dir = tempfile.TemporaryDirectory()
        tmp_root = Path(self._tmp_dir.name)
        namespace = params.get("namespace") or _get_namespace()

        # 1. Load eval.yaml (ConfigMap > filesystem)
        self._report_status(callbacks, JobStatus.RUNNING, JobPhase.INITIALIZING,
                            "Loading evaluation configuration")
        eval_config_path = self._resolve_eval_config(params, tmp_root, namespace)
        eval_config = EvalConfig.from_yaml(eval_config_path)
        log.info("Eval config loaded: skill=%s, dataset=%s, %d judges",
                 eval_config.resolve_skill() or "(prompt mode)", eval_config.dataset.path,
                 len(eval_config.judges))

        if eval_config.runner.type == "cursor":
            raise ValueError(
                "EvalHub does not support runner.type: cursor; use "
                "claude-code or codex")

        # Mount project resources from ConfigMap if specified
        if params.get("project_configmap"):
            self._materialize_project(params["project_configmap"], namespace, tmp_root)

        # 2. Load dataset (ConfigMap > filesystem > S3)
        dataset_info = self._load_dataset(config, callbacks, eval_config,
                                          tmp_root, namespace,
                                          eval_config_path=eval_config_path)
        model_name = config.model.name

        # 3. Build runner from eval.yaml runner.type
        runner_type = eval_config.runner.type
        if runner_type not in RUNNERS:
            raise ValueError(
                f"Unknown runner type '{runner_type}' in eval.yaml. "
                f"Available: {list(RUNNERS.keys())}")
        runner_cls = RUNNERS[runner_type]
        # OpenRouter plan (spec 014): built in the pod iff the effective skill
        # model is openrouter:/ (the JobSpec model overrides eval.yaml, like
        # --model locally). The pod's own env supplies OPENROUTER_API_KEY (and
        # the management key at key-guardrail) — the EvalHub server owns the
        # pod spec, so credentials reach it out of band, as today.
        plan, session, run_dir = _start_provider_plan(eval_config, model_name, runner_type,
                                                      run_id=config.id, run_dir=tmp_root / "run")
        if plan is not None:
            model_name = plan.skill.uri
        overrides = {"provider_plan": plan, "run_id": config.id} if plan is not None else {}
        try:
            runner = runner_cls.from_config(eval_config, log_prefix="evalhub", **overrides)
            log.info("Runner: %s (%s)", runner.name, runner_type)
            return self._run_cases(config, callbacks, eval_config, dataset_info, runner,
                                   model_name, start_time, session, run_dir)
        finally:
            if session is not None:
                session.close()          # idempotent: the normal path closed it before mapping results

    def _run_cases(self, config, callbacks, eval_config, dataset_info, runner, model_name,
                   start_time, session, run_dir):

        # 4. Execute per case
        self._report_status(callbacks, JobStatus.RUNNING, JobPhase.RUNNING_EVALUATION,
                            f"Running {dataset_info.num_cases} test cases",
                            total_steps=dataset_info.num_cases, completed_steps=0)

        case_results = []
        for i, case_id in enumerate(dataset_info.case_ids):
            case_dir = dataset_info.dest / case_id
            input_path = case_dir / "input.yaml"
            input_data = {}
            if input_path.exists():
                with open(input_path, encoding="utf-8") as f:
                    input_data = yaml.safe_load(f) or {}

            # Resolve the invocation: prompt mode sends the rendered prompt
            # with no skill wrapper; skill mode resolves execution.skill (or
            # the deprecated top-level skill) and renders its arguments.
            if eval_config.is_prompt_mode():
                target = None
                args = resolve_arguments(eval_config.execution.prompt, input_data)
            else:
                target = eval_config.resolve_skill()
                args = resolve_arguments(eval_config.execution.arguments, input_data) \
                    if eval_config.execution.arguments else ""
            timeout = eval_config.execution.timeout or 600
            budget = eval_config.execution.max_budget_usd or 5.0

            bind = getattr(runner, "bind_provider", None)
            if bind is not None and session is not None:
                bind(session.bind(case_id))
            result = runner.execute(
                target=target,
                args=args,
                workspace=case_dir,
                model=model_name,
                max_budget_usd=budget,
                timeout_s=timeout,
            )

            cost_str = f"{result.cost_usd:.4f}" if result.cost_usd is not None else "n/a"
            log.info("Case %s: exit=%s cost=%s %.1fs",
                     case_id, result.exit_code, cost_str, result.duration_s)
            case_results.append({"case_id": case_id, "run_result": result})
            if session is not None:
                _write_case_result(run_dir, case_id, result, session)

            self._report_status(callbacks, JobStatus.RUNNING, JobPhase.RUNNING_EVALUATION,
                                f"Completed case {case_id}",
                                total_steps=dataset_info.num_cases,
                                completed_steps=i + 1,
                                progress=(i + 1) / dataset_info.num_cases)

        # 5. Score with judges
        self._report_status(callbacks, JobStatus.RUNNING, JobPhase.POST_PROCESSING,
                            "Scoring results with judges")
        case_dirs = [dataset_info.dest / cr["case_id"] for cr in case_results]
        judge_scores = _load_judges_and_score(eval_config, case_dirs)

        # 6. Aggregate + map to JobResults. Under a plan the run's cost is the
        # reconciled truth (null-cost arithmetic), never the runner estimate,
        # and the provenance travels back in evaluation_metadata.
        provenance = _finish_provider_plan(session, run_dir, case_results) if session is not None else None
        aggregate = self._aggregate(case_results, start_time,
                                    cost_usd=provenance.get("cost_usd") if provenance else ...)
        log.info("Mapping results: %d cases, exit_code=%d",
                 len(case_results), aggregate.exit_code)
        job_results = map_to_job_results(
            job_id=config.id,
            benchmark_id=config.benchmark_id,
            model_name=model_name,
            run_result=aggregate,
            judge_scores=judge_scores,
            num_cases=dataset_info.num_cases,
            benchmark_index=config.benchmark_index,
            provenance=provenance,
        )

        self._report_status(callbacks, JobStatus.COMPLETED, JobPhase.COMPLETED,
                            "Evaluation complete", progress=1.0)
        return job_results

    # --- resource resolution -------------------------------------------------

    def _resolve_eval_config(self, params: dict, tmp_root: Path,
                             namespace: str) -> Path:
        """Resolve eval.yaml: ConfigMap parameter > filesystem path."""
        cm_name = params.get("eval_configmap")
        if cm_name:
            log.info("Reading eval config from ConfigMap %s/%s", namespace, cm_name)
            cm_data = _read_configmap(cm_name, namespace)
            config_dir = tmp_root / "eval-config"
            _configmap_to_dir(cm_data, config_dir)
            return config_dir / "eval.yaml"
        return Path(self._eval_config_path)

    def _materialize_project(self, cm_name: str, namespace: str,
                             tmp_root: Path) -> None:
        """Read project resources from a ConfigMap into a temp directory."""
        log.info("Reading project from ConfigMap %s/%s", namespace, cm_name)
        cm_data = _read_configmap(cm_name, namespace)
        project_dir = tmp_root / "project"
        _configmap_to_dir(cm_data, project_dir)
        os.environ["AGENT_EVAL_PROJECT_DIR"] = str(project_dir)

    def _load_dataset(self, config: JobSpec, callbacks: JobCallbacks,
                      eval_config: EvalConfig, tmp_root: Path,
                      namespace: str, eval_config_path: Path | None = None,
                      ) -> DatasetInfo:
        """Load dataset: ConfigMap parameter > local path > S3."""
        params = config.parameters or {}

        # ConfigMap dataset
        cm_name = params.get("dataset_configmap")
        if cm_name:
            self._report_status(callbacks, JobStatus.RUNNING, JobPhase.LOADING_DATA,
                                f"Reading dataset from ConfigMap {cm_name}")
            cm_data = _read_configmap(cm_name, namespace)
            dest = tmp_root / "cases"
            _configmap_to_dir(cm_data, dest)
            case_ids = sorted(d.name for d in dest.iterdir() if d.is_dir())
            log.info("Dataset from ConfigMap: %d cases", len(case_ids))
            return DatasetInfo(num_cases=len(case_ids), case_ids=case_ids, dest=dest)

        # Local filesystem — resolve relative to the actual config location
        config_base = eval_config_path or Path(self._eval_config_path)
        eval_config_dir = Path(config_base).parent
        local_dataset = eval_config_dir / eval_config.dataset.path
        if local_dataset.is_dir() and any(local_dataset.iterdir()):
            self._report_status(callbacks, JobStatus.RUNNING, JobPhase.LOADING_DATA,
                                f"Using local dataset at {local_dataset}")
            dest = tmp_root / "cases"
            shutil.copytree(local_dataset, dest)
            case_ids = sorted(d.name for d in dest.iterdir() if d.is_dir())
            log.info("Copied local dataset → %s (%d cases)", dest, len(case_ids))
            return DatasetInfo(num_cases=len(case_ids), case_ids=case_ids, dest=dest)

        # S3
        self._report_status(callbacks, JobStatus.RUNNING, JobPhase.LOADING_DATA,
                            "Downloading test cases from S3")
        if not boto3:
            raise RuntimeError(
                "boto3 is required for S3 dataset download. "
                "Install with: pip install agent-eval-harness[evalhub]")
        dest = tmp_root / "cases"
        dest.mkdir(parents=True, exist_ok=True)
        return download_dataset(
            boto3.client("s3"), params.get("s3_bucket", ""),
            params.get("s3_prefix", ""), dest)

    # --- helpers -------------------------------------------------------------

    @staticmethod
    def _aggregate(case_results: list, start_time: float, cost_usd=...) -> RunResult:
        """One RunResult for the job. ``cost_usd`` (when given, ``None``
        included) replaces the sum of the runners' own numbers: under an
        OpenRouter plan that sum is an estimate and the reconciled value —
        or ``null`` when a case is unpriced — is the only honest total."""
        if not case_results:
            return RunResult(exit_code=-1, stdout="", stderr="No cases executed",
                             duration_s=time.monotonic() - start_time)
        runs = [cr["run_result"] for cr in case_results]
        failed = sum(1 for r in runs if r.exit_code != 0)
        return RunResult(
            exit_code=max((r.exit_code for r in runs), key=abs),
            stdout="",
            stderr=f"{failed}/{len(runs)} cases failed" if failed else "",
            duration_s=time.monotonic() - start_time,
            cost_usd=(sum(r.cost_usd or 0 for r in runs) or None) if cost_usd is ... else cost_usd,
            num_turns=sum(r.num_turns or 0 for r in runs) or None,
            resolved_model=runs[0].resolved_model,
        )

    @staticmethod
    def _report_status(
        callbacks: JobCallbacks, status: str, phase: str, message: str,
        progress: float | None = None, total_steps: int | None = None,
        completed_steps: int | None = None,
    ) -> None:
        try:
            callbacks.report_status(JobStatusUpdate(
                status=status, phase=phase, progress=progress,
                message=MessageInfo(message=message, message_code="info"),
                total_steps=total_steps, completed_steps=completed_steps,
                timestamp=datetime.now(timezone.utc),
            ))
        except Exception as exc:
            log.warning("Failed to report status: %s", exc)
