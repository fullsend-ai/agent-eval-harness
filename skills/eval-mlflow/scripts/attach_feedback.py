#!/usr/bin/env python3
"""Attach judge and human feedback to MLflow traces, or pull annotations.

Push mode: reads summary.yaml (judge results) and/or review.yaml (human
feedback), finds matching traces, and logs feedback entries.

Pull mode: searches traces for annotations added via MLflow UI and
writes them back to review.yaml for eval-optimize to consume.

Usage:
    # Push judge feedback
    python3 ${CLAUDE_SKILL_DIR}/scripts/attach_feedback.py \\
        --run-id <id> --config eval.yaml --source judge

    # Push human review feedback
    python3 ${CLAUDE_SKILL_DIR}/scripts/attach_feedback.py \\
        --run-id <id> --config eval.yaml --source human

    # Push both
    python3 ${CLAUDE_SKILL_DIR}/scripts/attach_feedback.py \\
        --run-id <id> --config eval.yaml --source all

    # Pull MLflow annotations back to review.yaml
    python3 ${CLAUDE_SKILL_DIR}/scripts/attach_feedback.py \\
        --run-id <id> --config eval.yaml --action pull
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import argparse
import os
import sys
from pathlib import Path

import yaml

try:
    import mlflow
except ImportError:
    print("MLflow not installed. Install with: pip install 'mlflow[genai]'",
          file=sys.stderr)
    sys.exit(0)

from agent_eval.config import EvalConfig, _validate_path_segment
from agent_eval.mlflow.experiment import log_feedback, resolve_tracking_uri
from agent_eval.mlflow.traces import find_run_traces


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--run-id", help="Run under $AGENT_EVAL_RUNS_DIR/<eval-name>/")
    where.add_argument("--run-dir", help="The run directory itself, wherever it is — e.g. an "
                                          "optimization rollout under <runs>/.optimize/<eval-name>/ "
                                          "(its basename is the run id)")
    parser.add_argument("--config", required=True)
    parser.add_argument("--action", choices=["push", "pull"], default="push")
    parser.add_argument("--source", choices=["judge", "human", "all"], default="judge",
                        help="What feedback to push (push mode only)")
    parser.add_argument("--trace-id", default=None,
                        help="Direct trace ID (skip search)")
    args = parser.parse_args()

    config = EvalConfig.from_yaml(args.config)
    mlflow.set_tracking_uri(resolve_tracking_uri(config))
    if args.run_dir:
        run_dir = Path(args.run_dir).resolve()
        args.run_id = run_dir.name
    else:
        # Validate run_id to prevent path traversal (CWE-22)
        args.run_id = _validate_path_segment(args.run_id, "--run-id")
        runs_base = Path(os.environ.get("AGENT_EVAL_RUNS_DIR", "eval/runs"))
        run_dir = runs_base / config.eval_name() / args.run_id

    experiment_name = config.mlflow.experiment or config.name

    if args.action == "pull":
        _pull_feedback(run_dir, experiment_name, args)
    else:
        _push_feedback(run_dir, experiment_name, config, args)


def _push_feedback(run_dir, experiment_name, config, args):
    """Push judge and/or human feedback to MLflow traces."""
    # Find traces
    if args.trace_id:
        trace_ids = [args.trace_id]
    else:
        traces = find_run_traces(experiment_name, args.run_id)
        trace_ids = [t["trace_id"] for t in traces if t.get("trace_id")]

    if not trace_ids:
        print("TRACES: 0 found (tracing may not be enabled)")
        print("FEEDBACK: 0 entries")
        return

    feedback_count = 0

    # Push judge feedback
    if args.source in ("judge", "all"):
        summary_path = run_dir / "summary.yaml"
        if summary_path.exists():
            with open(summary_path) as f:
                summary = yaml.safe_load(f) or {}

            per_case = summary.get("per_case", {})
            for case_id, case_results in per_case.items():
                if not isinstance(case_results, dict):
                    continue
                for judge_name, result in case_results.items():
                    if not isinstance(result, dict):
                        continue
                    value = result.get("value")
                    rationale = str(result.get("rationale", ""))
                    for trace_id in trace_ids:
                        log_feedback(
                            trace_id=trace_id,
                            name=f"{case_id}/{judge_name}",
                            value=value,
                            source_type="CODE",
                            source_id="agent-eval",
                            rationale=rationale,
                        )
                        feedback_count += 1

    # Push human review feedback
    if args.source in ("human", "all"):
        review_path = run_dir / "review.yaml"
        if review_path.exists():
            with open(review_path) as f:
                review = yaml.safe_load(f) or {}

            feedback = review.get("feedback", {})
            for case_id, comment in feedback.items():
                if not comment:
                    continue
                for trace_id in trace_ids:
                    log_feedback(
                        trace_id=trace_id,
                        name=f"{case_id}/human_review",
                        value=comment,
                        source_type="HUMAN",
                        source_id="eval-review",
                        rationale="",
                    )
                    feedback_count += 1

    print(f"TRACES: {len(trace_ids)} found")
    print(f"FEEDBACK: {feedback_count} entries attached")


def _pull_feedback(run_dir, experiment_name, args):
    """Pull feedback annotations from MLflow traces into review.yaml."""
    traces = find_run_traces(experiment_name, args.run_id)
    if not traces:
        print("TRACES: 0 found")
        print("PULLED: 0 annotations")
        return

    # Load existing review
    review_path = run_dir / "review.yaml"
    review = {}
    if review_path.exists():
        with open(review_path) as f:
            review = yaml.safe_load(f) or {}

    mlflow_feedback = review.get("mlflow_feedback", {})
    pulled = 0

    for trace_info in traces:
        trace_id = trace_info.get("trace_id")
        if not trace_id:
            continue

        try:
            trace = mlflow.get_trace(trace_id)
        except Exception:
            continue

        # Read assessments/feedback from the trace
        if not hasattr(trace, "info"):
            continue

        assessments = getattr(trace.info, "assessments", [])
        if not assessments:
            continue

        for assessment in assessments:
            name = getattr(assessment, "name", "")
            value = getattr(assessment, "value", None)
            source = getattr(assessment, "source", None)
            rationale = getattr(assessment, "rationale", "")

            # Skip feedback we pushed ourselves
            source_id = getattr(source, "source_id", "") if source else ""
            if source_id in ("agent-eval", "eval-review"):
                continue

            # Store under case_id if namespaced, otherwise under trace_id
            key = name if "/" in name else f"{trace_id}/{name}"
            mlflow_feedback[key] = {
                "value": value,
                "rationale": rationale,
                "source": str(source_id),
            }
            pulled += 1

    if pulled:
        review["mlflow_feedback"] = mlflow_feedback
        run_dir.mkdir(parents=True, exist_ok=True)
        with open(review_path, "w") as f:
            yaml.dump(review, f, default_flow_style=False, allow_unicode=True)

    print(f"TRACES: {len(traces)} found")
    print(f"PULLED: {pulled} annotations")
    if pulled:
        print(f"SAVED: {review_path}")


if __name__ == "__main__":
    main()
