#!/usr/bin/env python3
"""Log eval run results to MLflow.

Reads summary.yaml and run_result.json, logs params, metrics,
artifacts, per-case results table, and creates the main orchestrator
trace from stdout.log.  Also links all experiment traces to the run.

Usage:
    python3 ${CLAUDE_SKILL_DIR}/scripts/log_results.py \\
        --run-id <id> \\
        --config eval.yaml
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

try:
    import mlflow
    from mlflow import MlflowClient
    from mlflow.entities.assessment_source import AssessmentSource, AssessmentSourceType
except ImportError:
    print("MLflow not installed. Install with: pip install 'mlflow[genai]'",
          file=sys.stderr)
    sys.exit(0)

from agent_eval.ci_context import (
    collect_ci_context,
    find_harness_snapshot,
    merge_mlflow_tags,
)
from agent_eval.config import EvalConfig, _validate_path_segment
from agent_eval.mlflow.experiment import resolve_tracking_uri


# ── Trace builder (extracted to agent_eval/mlflow/trace_builder.py) ──
from agent_eval.mlflow.trace_builder import build_trace, log_trace
# Same transcript metric extractor the run-level aggregation uses, so per-step
# trace cost/tokens match the run `cost_usd` metric and the HTML report.
from agent_eval.harbor.results import _extract_transcript_metrics


def _detect_regressions(judges, thresholds):
    """score.py's regression detector, loaded by path.

    The judge engine lives with the eval-run skill rather than in the
    agent_eval package — same approach as agent_eval/harbor/reward.py. Raises
    if it cannot be loaded; the caller tags the run "unknown" rather than
    claiming a clean one.
    """
    import importlib.util
    root = Path(__file__).resolve().parent.parent.parent.parent
    score_path = root / "skills" / "eval-run" / "scripts" / "score.py"
    if not score_path.exists():
        raise FileNotFoundError(f"Judge engine not found: {score_path}")
    spec = importlib.util.spec_from_file_location("_score_for_thresholds",
                                                  score_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.detect_regressions(judges, thresholds)


def _resolve_skill(config: EvalConfig) -> str | None:
    """Skill name compatible with AEH v1.0.3 and newer EvalConfig APIs."""
    if hasattr(config, "resolve_skill"):
        return config.resolve_skill()
    return getattr(config, "skill", None) or None


def _eval_name(config: EvalConfig) -> str:
    """Eval subdirectory name compatible with AEH v1.0.3 and newer APIs."""
    if hasattr(config, "eval_name"):
        return config.eval_name()
    return (
        getattr(config, "skill", None)
        or getattr(config, "name", None)
        or "eval"
    )


def _is_within(path, root):
    """True if ``path`` resolves to ``root`` or a descendant (symlinks resolved)."""
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def _resolve_harbor_job_dir(raw, run_dir):
    """Resolve harbor_job_dir (stored repo-root-relative) to an existing dir.

    The job dir is local harness output, but we still reject absolute paths,
    ``..`` traversal, and symlinks, and require the resolved directory to live
    under one of run_dir's ancestors — so a tampered run_result.json can't point
    trace collection at files outside the expected tree (CWE-22/CWE-59).
    """
    if not raw:
        return None
    p = Path(raw)
    if p.is_absolute() or ".." in p.parts:
        return None
    for root in run_dir.resolve().parents:
        candidate = root / p
        if (candidate.is_dir() and not candidate.is_symlink()
                and _is_within(candidate, root)):
            return candidate
    return None


def _safe_trajectory_path(transcript, job_root):
    """Resolve <transcript-dir>/trajectory.json, rejecting symlinks/escapes.

    trajectory.json lives alongside the Claude transcript in Harbor's job
    output; guard it the same way as the other harbor-job paths (CWE-22/CWE-59)
    so a tampered or symlinked file can't smuggle unrelated data into traces.
    """
    traj = transcript.parent / "trajectory.json"
    if traj.is_file() and not traj.is_symlink() and _is_within(traj, job_root):
        return traj
    return None


def _harbor_steps(job_dir):
    """Yield (case_id, case_dir, step_name, transcript_path, subagent_dir) per step.

    Harbor writes each step's Claude stream-json to
    ``<case>__<hash>/steps/<step>/agent/claude-code.txt`` (multi-step) or
    ``<case>__<hash>/agent/claude-code.txt`` (single-step). Background subagent
    transcripts live under ``.../agent/sessions/projects/*/*/subagents/``.
    The case_id (name before ``__``) matches the summary.yaml per_case keys.
    """
    job_root = job_dir.resolve(strict=True)
    for case_dir in sorted(d for d in job_dir.iterdir()
                           if d.is_dir() and not d.is_symlink()
                           and "__" in d.name and _is_within(d, job_root)):
        case_id = case_dir.name.split("__")[0]
        steps_dir = case_dir / "steps"
        if steps_dir.is_dir():
            step_dirs = sorted(steps_dir.iterdir(), key=lambda p: p.stat().st_mtime)
        elif (case_dir / "agent" / "claude-code.txt").exists():
            step_dirs = [case_dir]  # single-step layout
        else:
            step_dirs = []
        for sd in step_dirs:
            agent_dir = sd / "agent"
            transcript = agent_dir / "claude-code.txt"
            if not (transcript.is_file() and not transcript.is_symlink()
                    and _is_within(transcript, job_root)):
                continue
            step_name = "" if sd is case_dir else sd.name
            subs = [p for p in agent_dir.glob("sessions/projects/*/*/subagents")
                    if p.is_dir() and not p.is_symlink()
                    and _is_within(p, job_root)]
            yield (case_id, case_dir, step_name, transcript,
                   subs[0] if len(subs) == 1 else None)


def _harbor_step_run_result(case_dir, step_name, base, transcript):
    """Per-step run_result for trace annotation: model, exit code, cost, tokens.

    Cost and tokens are read from the step transcript with the SAME extractor
    the run-level aggregation uses (_extract_transcript_metrics -> the result
    event's total_cost_usd / usage), so the trace cost matches the run
    `cost_usd` metric and the HTML report. Crucially total_cost_usd includes
    background subagent cost (e.g. the auto-fix step's subagents), which
    result.json's agent_result.cost_usd omits — so this is the accurate total.
    result.json is still consulted for the step's exit code. Token usage already
    arrives in the {input, output, cache_read, cache_create} schema build_trace
    expects, so no remapping is needed.
    """
    rr = {"model": base.get("model", ""), "exit_code": 0}

    metrics = _extract_transcript_metrics(transcript)
    cost = metrics.get("cost_usd")
    if cost:
        rr["cost_usd"] = cost
    tu = metrics.get("token_usage") or {}
    token_usage = {k: (tu.get(k) or 0)
                   for k in ("input", "output", "cache_read", "cache_create")}
    if any(token_usage.values()):
        rr["token_usage"] = token_usage
        # per_model_usage drives the per-span mlflow.llm.cost distribution that
        # the Cost Breakdown / Cost Over Time charts aggregate.
        if cost and rr["model"]:
            rr["per_model_usage"] = {
                rr["model"]: {**token_usage, "cost_usd": cost},
            }

    # exit_code comes from the step's exception_info in result.json (best-effort).
    result_path = case_dir / "result.json"
    if result_path.is_file():
        try:
            data = json.loads(result_path.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            print(f"WARNING: failed to parse Harbor result {result_path}: {e}",
                  file=sys.stderr)
            data = None
        if data:
            steps = data.get("step_results") or []
            match = next((s for s in steps
                          if s.get("step_name") == step_name), None)
            if match is None and not step_name:
                match = data  # single-step trial
            if match and match.get("exception_info"):
                rr["exit_code"] = 1
    return rr


# ── Main ─────────────────────────────────────────────────────────────

def _provenance_tags(run_result):
    """Cost-provenance and routing tags from run_result.json (spec 014); only
    what the run recorded — a legacy run tags nothing."""
    rr = run_result or {}
    tags = {}
    if rr.get("cost_source"):
        tags["cost_source"] = str(rr["cost_source"])
    if rr.get("cost_confidence"):
        tags["cost_confidence"] = str(rr["cost_confidence"])
    routing = rr.get("routing") or {}
    if routing:
        tags["routing_enforcement"] = str(routing.get("enforcement") or "none")
        if routing.get("sha"):
            tags["routing_sha"] = str(routing["sha"])
        tags["audit_violations"] = str(len(routing.get("violations") or []))
        tags["audit_clean"] = "yes" if (not routing.get("violations")
                                        and routing.get("audit_complete") is not False) else "no"
    provider = rr.get("provider") or {}
    if provider.get("key_scope"):
        tags["key_scope"] = str(provider["key_scope"])
    budget = rr.get("budget") or {}
    if budget.get("enforcement"):
        tags["budget_enforcement"] = str(budget["enforcement"])
    return tags


def _provenance_metrics(run_result):
    """Hook cost and per-provider spend as metrics (numeric values only)."""
    import re as _re

    rr = run_result or {}
    metrics = {}
    hook = rr.get("hook_cost_usd")
    if isinstance(hook, (int, float)) and not isinstance(hook, bool):
        metrics["hook_cost_usd"] = float(hook)
    for slug, stats in (rr.get("providers") or {}).items():
        cost = (stats or {}).get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            safe = _re.sub(r"[^A-Za-z0-9_\-\. :/]", "-", str(slug))
            metrics[f"provider/{safe}/cost_usd"] = float(cost)
    return metrics


def _cost_metrics_from_summary(summary):
    """MLflow metrics/tags for judge-side spend from summary.yaml (spec 014).

    `judge_cost_usd`, `judge/requests`, `judge/requests_missing_cost`,
    `judge/tool_choice_fallbacks` and `total_cost_usd` are logged only when
    numeric — a null total is never re-derived from an estimate — and
    `total_cost_source` is tagged so a `judge-only`/`agent-only` run is not
    read as a complete cost. Judge spend stays out of `cost_usd`.
    """
    metrics, tags = {}, {}
    usage = (summary or {}).get("judge_usage") or {}
    judge_cost = usage.get("judge_cost_usd")
    if isinstance(judge_cost, (int, float)) and not isinstance(judge_cost, bool):
        metrics["judge_cost_usd"] = float(judge_cost)
    for key in ("requests", "requests_missing_cost", "tool_choice_fallbacks"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            metrics[f"judge/{key}"] = value
    total = (summary or {}).get("total_cost_usd")
    if isinstance(total, (int, float)) and not isinstance(total, bool):
        metrics["total_cost_usd"] = float(total)
    source = (summary or {}).get("total_cost_source")
    if source:
        tags["total_cost_source"] = str(source)
    return metrics, tags


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    # Validate run_id to prevent path traversal (CWE-22)
    args.run_id = _validate_path_segment(args.run_id, "--run-id")

    config = EvalConfig.from_yaml(args.config)
    mlflow.set_tracking_uri(resolve_tracking_uri(config))
    runs_base = Path(os.environ.get("AGENT_EVAL_RUNS_DIR", "eval/runs"))
    runs_dir = runs_base / _eval_name(config)
    run_dir = runs_dir / args.run_id

    # Load summary
    summary_path = run_dir / "summary.yaml"
    if not summary_path.exists():
        print(f"ERROR: no summary found at {summary_path}", file=sys.stderr)
        sys.exit(1)

    with open(summary_path) as f:
        summary = yaml.safe_load(f) or {}

    # Load execution metadata
    run_result = {}
    run_result_path = run_dir / "run_result.json"
    if run_result_path.exists():
        with open(run_result_path) as f:
            run_result = json.load(f)

    # Set experiment
    experiment_name = config.mlflow.experiment or config.name
    mlflow.set_experiment(experiment_name)
    client = MlflowClient()

    # Resolve experiment ID
    exp = mlflow.get_experiment_by_name(experiment_name)
    experiment_id = exp.experiment_id if exp else "0"

    with mlflow.start_run(run_name=args.run_id) as run:
        mlflow_run_id = run.info.run_id

        # ── Params ───────────────────────────────────────────────
        params = {
            "skill": _resolve_skill(config),
            "eval_name": _eval_name(config),
            "runner": config.runner.type,
            "run_id": args.run_id,
            "model": run_result.get("model", ""),
        }
        if run_result.get("subagent_model"):
            params["subagent_model"] = run_result["subagent_model"]
        if run_result.get("agent"):
            params["agent"] = run_result["agent"]
        for key, value in params.items():
            if value:
                mlflow.log_param(key, value)

        # ── Execution metrics ────────────────────────────────────
        if run_result.get("duration_s"):
            mlflow.log_metric("duration_s", run_result["duration_s"])
        if run_result.get("cost_usd"):
            mlflow.log_metric("cost_usd", run_result["cost_usd"])
        if run_result.get("num_turns"):
            mlflow.log_metric("num_turns", run_result["num_turns"])
        token_usage = run_result.get("token_usage", {})
        if token_usage:
            for key in ("input", "output", "cache_read", "cache_create"):
                val = token_usage.get(key)
                if val is not None:
                    mlflow.log_metric(f"tokens/{key}", val)

        # ── Per-model cost and token breakdown ───────────────────
        per_model = run_result.get("per_model_usage", {})
        if per_model:
            import re
            for model_name, stats in per_model.items():
                # Sanitize model name for MLflow metric keys:
                # only alphanumerics, underscores, dashes, periods, spaces,
                # colons, and slashes are allowed.
                safe_name = re.sub(r"[^A-Za-z0-9_\-\. :/]", "-", model_name)
                prefix = f"model/{safe_name}"
                if stats.get("cost_usd") is not None:
                    mlflow.log_metric(f"{prefix}/cost_usd", stats["cost_usd"])
                for key in ("input", "output", "cache_read", "cache_create"):
                    val = stats.get(key)
                    if val is not None:
                        mlflow.log_metric(f"{prefix}/tokens/{key}", val)

        # ── Cost provenance (spec 014) ────────────────────────────
        for key, value in _provenance_tags(run_result).items():
            mlflow.set_tag(key, value)
        for key, value in _provenance_metrics(run_result).items():
            mlflow.log_metric(key, value)

        # ── Judge-side cost (spec 014 Decision 15) ───────────────
        cost_metrics, cost_tags = _cost_metrics_from_summary(summary)
        for key, value in cost_metrics.items():
            mlflow.log_metric(key, value)
        for key, value in cost_tags.items():
            mlflow.set_tag(key, value)

        # ── Judge metrics ────────────────────────────────────────
        judges = summary.get("judges", {})
        metric_count = 0
        for judge_name, agg in judges.items():
            if isinstance(agg, dict):
                if agg.get("pass_rate") is not None:
                    mlflow.log_metric(f"{judge_name}/pass_rate", agg["pass_rate"])
                    metric_count += 1
                if agg.get("mean") is not None:
                    mlflow.log_metric(f"{judge_name}/mean", agg["mean"])
                    metric_count += 1

        # ── Tags ─────────────────────────────────────────────────
        # Ask score.py rather than restating its rules: this had drifted to two
        # of the four threshold keys, so a run that exited 1 on `min_win_rate`
        # or `max_error_rate` was still tagged regressions_detected=no.
        # "unknown" is a third state on purpose — tagging a run clean because
        # the detector could not run is the failure this whole change is about.
        if not config.thresholds:
            regressions_tag = "no"
        else:
            try:
                regressions_tag = (
                    "yes" if _detect_regressions(judges, config.thresholds)
                    else "no")
            except Exception as exc:
                print(f"Warning: could not evaluate thresholds: {exc}",
                      file=sys.stderr)
                regressions_tag = "unknown"
        mlflow.set_tag("regressions_detected", regressions_tag)
        mlflow.set_tag("num_judges", str(len(judges)))
        # Disk handoff: harness-snapshot.json under run_dir → tags + artifact.
        # Later readers should fetch the MLflow artifact (prefer_mlflow).
        try:
            # merge_mlflow_tags drops mlflow.runName from config so start_run's
            # run name (eval run id) stays the join key for artifact fetch.
            for tag_key, tag_value in merge_mlflow_tags(
                collect_ci_context(
                    eval_run_id=args.run_id,
                    run_dir=run_dir,
                    experiment_id=experiment_id,
                ),
                config.mlflow.tags,
            ).items():
                if tag_value and tag_key != "mlflow.runName":
                    mlflow.set_tag(tag_key, str(tag_value))
        except Exception as e:
            print(f"WARNING: failed to set CI/harness tags: {e}", file=sys.stderr)

        # ── Artifacts ────────────────────────────────────────────
        if summary_path.exists():
            mlflow.log_artifact(str(summary_path))
        try:
            snap_path = find_harness_snapshot(run_dir)
            if snap_path is not None:
                mlflow.log_artifact(str(snap_path))
        except Exception as e:
            print(f"WARNING: failed to log harness snapshot artifact: {e}",
                  file=sys.stderr)

        # Log input files for from-traces extraction.
        for name in ("batch.yaml", "case_order.yaml"):
            p = run_dir / name
            if p.exists():
                mlflow.log_artifact(str(p), "inputs")
        cases_dir = run_dir / "cases"
        if cases_dir.is_dir():
            for case_dir in sorted(cases_dir.iterdir()):
                if not case_dir.is_dir():
                    continue
                inp = case_dir / "input.yaml"
                if inp.exists():
                    mlflow.log_artifact(str(inp), f"inputs/{case_dir.name}")

        # ── Per-case results table ───────────────────────────────
        per_case = summary.get("per_case", {})
        if per_case:
            table_rows = []
            for case_id, case_results in per_case.items():
                if not isinstance(case_results, dict):
                    continue
                for judge_name, result in case_results.items():
                    if not isinstance(result, dict):
                        continue
                    table_rows.append({
                        "case_id": case_id,
                        "judge": judge_name,
                        "value": result.get("value"),
                        "rationale": str(result.get("rationale", ""))[:500],
                    })
            if table_rows:
                columns = {}
                for key in table_rows[0]:
                    columns[key] = [row[key] for row in table_rows]
                mlflow.log_table(columns, artifact_file="per_case_results.json")

    # ── Find existing execution traces ────────────────────────────
    # Execution traces are created during skill execution by the trace
    # interceptor. They have eval_run_id tags matching the case IDs.
    # We prefer these over synthetic traces built from stdout.log
    # because they already exist in the DB (no async queue issues).
    main_trace_id = None
    case_trace_map = {}  # case_id -> trace_id
    harbor_step_traces = {}  # case_id -> {step_name: trace_id} (harbor per-step)
    trace_ids = []

    # TODO: paginate via page_token for experiments with >500 unlinked traces
    try:
        all_traces = client.search_traces(experiment_ids=[experiment_id],
                                          max_results=500)
        for t in all_traces:
            tags = t.info.tags or {}
            eval_id = tags.get("eval_run_id", "")
            existing_run = tags.get("mlflow.runId")
            # Match unlinked traces whose eval_run_id matches a case ID
            if eval_id and not existing_run:
                if eval_id not in case_trace_map:
                    case_trace_map[eval_id] = t.info.trace_id
                trace_ids.append(t.info.trace_id)
    except Exception as e:
        print(f"WARNING: failed to search traces: {e}", file=sys.stderr)

    # Build synthetic traces from stdout.log for cases not already covered
    # by existing execution traces.
    exec_mode = run_result.get("execution_mode", "batch")
    if exec_mode in ("case", "prompt"):
        cases_dir = run_dir / "cases"
        if cases_dir.exists():
            for case_dir in sorted(d for d in cases_dir.iterdir() if d.is_dir()):
                case_stdout = case_dir / "stdout.log"
                if not case_stdout.exists():
                    continue
                case_id = case_dir.name
                if case_id in case_trace_map:
                    continue
                case_result = run_result.get("per_case", {}).get(case_id, run_result)
                _skill = _resolve_skill(config)
                trace_name = f"{_skill} ({case_id})" if _skill else case_id
                trace_dict = build_trace(case_stdout, case_result, case_id,
                                         experiment_id, trace_name=trace_name)
                if trace_dict:
                    tid = log_trace(trace_dict)
                    if tid:
                        case_trace_map[case_id] = tid
                        trace_ids.append(tid)
                        num_spans = len(trace_dict["data"]["spans"])
                        print(f"TRACE: {tid} ({num_spans} spans) — {case_id}")
    elif exec_mode == "harbor":
        # Harbor runs in-pod and writes no cases/<id>/stdout.log; the Claude
        # stream-json lives per step in the harbor job dir. Build one trace per
        # step (same build_trace path as local runs), nesting background
        # subagents from the step session's subagents/ dir.
        job_dir = _resolve_harbor_job_dir(run_result.get("harbor_job_dir"), run_dir)
        if job_dir is None:
            print("WARNING: harbor_job_dir not found; skipping trace build",
                  file=sys.stderr)
        else:
            for case_id, case_dir, step_name, transcript, sub_dir in \
                    _harbor_steps(job_dir):
                step_key = f"{case_id}/{step_name}" if step_name else case_id
                if step_key in case_trace_map:
                    continue
                step_rr = _harbor_step_run_result(case_dir, step_name,
                                                  run_result, transcript)
                _skill = _resolve_skill(config)
                trace_name = (f"{_skill} ({step_key})"
                              if _skill else step_key)
                trace_dict = build_trace(
                    transcript, step_rr, step_key,
                    experiment_id, trace_name=trace_name,
                    subagent_dir=sub_dir,
                    trajectory_path=_safe_trajectory_path(transcript, job_dir),
                )
                if trace_dict:
                    tid = log_trace(trace_dict)
                    if tid:
                        case_trace_map[step_key] = tid
                        harbor_step_traces.setdefault(case_id, {})[step_name] = tid
                        trace_ids.append(tid)
                        num_spans = len(trace_dict["data"]["spans"])
                        print(f"TRACE: {tid} ({num_spans} spans) — {step_key}"
                              + (" +subagents" if sub_dir else ""))
    else:
        stdout_path = run_dir / "stdout.log"
        if stdout_path.exists() and run_result:
            _skill = _resolve_skill(config)
            trace_name = f"{_skill} ({args.run_id})" if _skill else _eval_name(config)
            trace_dict = build_trace(stdout_path, run_result, args.run_id,
                                     experiment_id, trace_name=trace_name)
            if trace_dict:
                main_trace_id = log_trace(trace_dict)
                if main_trace_id:
                    trace_ids.append(main_trace_id)
                    num_spans = len(trace_dict["data"]["spans"])
                    duration_s = run_result.get("duration_s", 0)
                    print(f"TRACE: {main_trace_id} ({num_spans} spans, {duration_s:.0f}s)")

    # Flush async queue so traces are committed before linking/feedback.
    # The class was renamed between MLflow versions:
    #   <=3.12: AsyncExportQueue   >=3.13: AsyncTraceExportQueue
    if trace_ids:
        flushed = False
        for _cls_name in ("AsyncTraceExportQueue", "AsyncExportQueue"):
            try:
                import importlib
                _mod = importlib.import_module(
                    "mlflow.tracing.export.async_export_queue"
                )
                _cls = getattr(_mod, _cls_name, None)
                if _cls and hasattr(_cls, "get_instance"):
                    _cls.get_instance().flush(timeout_sec=30)
                    flushed = True
                    break
            except Exception:
                pass
        if not flushed:
            import time
            time.sleep(3)  # last-resort: give the async export time to finish

    if not main_trace_id and case_trace_map:
        main_trace_id = next(iter(case_trace_map.values()))

    # ── Link traces to run (must happen before feedback) ─────────
    try:
        if trace_ids:
            client.link_traces_to_run(run_id=mlflow_run_id, trace_ids=trace_ids)
            for tid in trace_ids:
                client.set_trace_tag(tid, "mlflow.runId", mlflow_run_id)
            print(f"LINKED: {len(trace_ids)} traces to run {mlflow_run_id}")
    except Exception as e:
        print(f"WARNING: failed to link traces: {e}", file=sys.stderr)

    # ── Attach judge feedback to traces (populates Quality tab) ──
    feedback_count = 0

    _TRUE_STRS = {"pass", "true", "yes", "y", "1", "ok", "success"}
    _FALSE_STRS = {"fail", "false", "no", "n", "0", "error", "failure"}

    def _to_feedback_value(v):
        if isinstance(v, bool):
            return 1.0 if v else 0.0
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            s = v.strip().lower()
            if s in _TRUE_STRS:
                return 1.0
            if s in _FALSE_STRS:
                return 0.0
            try:
                return float(s)
            except ValueError:
                return None
        return None

    for case_id, case_results in per_case.items():
        if not isinstance(case_results, dict):
            continue
        steps_for_case = harbor_step_traces.get(case_id)
        for judge_name, result in case_results.items():
            if not isinstance(result, dict):
                continue
            value = result.get("value")
            if value is None:
                continue
            fb_value = _to_feedback_value(value)
            if fb_value is None:
                continue
            # Route feedback: for harbor per-step traces, a step judge
            # (create/auto-fix/submit) attaches to its own step trace and any
            # overall judge to the final step; non-harbor runs use the
            # per-case trace.
            if steps_for_case:
                step_trace_ids = list(steps_for_case.values())
                trace_id = (steps_for_case.get(judge_name)
                            or steps_for_case.get("submit")
                            or (step_trace_ids[-1] if step_trace_ids else None))
            else:
                trace_id = case_trace_map.get(case_id, main_trace_id)
            if not trace_id:
                continue
            try:
                mlflow.log_feedback(
                    trace_id=trace_id,
                    name=judge_name,
                    value=fb_value,
                    rationale=str(result.get("rationale", ""))[:500],
                    source=AssessmentSource(
                        source_type=AssessmentSourceType.CODE,
                        source_id=f"eval/{judge_name}",
                    ),
                )
                feedback_count += 1
            except Exception as e:
                print(f"WARNING: failed to log feedback for {case_id}/{judge_name}: {e}",
                      file=sys.stderr)

    print(f"EXPERIMENT: {experiment_name}")
    print(f"RUN: {mlflow_run_id}")
    print(f"PARAMS: {len(params)}")
    print(f"METRICS: {metric_count}")
    print(f"TABLE: per_case_results ({len(per_case)} cases)")
    if feedback_count:
        print(f"FEEDBACK: {feedback_count} assessments attached to traces")


if __name__ == "__main__":
    main()
