"""Place a rollout's artifacts into the standard harness run-dir layout.

Every optimization rollout, regardless of search engine, lands as an ordinary
``$AGENT_EVAL_RUNS_DIR/<eval-name>/<run-id>`` run directory with a
``condition.json`` identifying it — so ``/eval-compare``, ``/eval-mlflow`` and
``/eval-review`` work on the result unchanged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def runs_dir_for(manifest: dict) -> Path:
    base = Path(os.environ.get("AGENT_EVAL_RUNS_DIR", "eval/runs"))
    if not base.is_absolute():
        base = Path(manifest["project_root"]) / base
    return base.resolve() / manifest["eval_name"]


def write_condition(run_dir: Path, condition: dict) -> None:
    (Path(run_dir) / "condition.json").write_text(json.dumps(condition, indent=2) + "\n", encoding="utf-8")


def _is_number(value) -> bool:
    # bool is an int subclass: a stray True must never read as $1.00.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def read_usage(run_dir: Path, summary: dict) -> tuple[float, int, str]:
    run_result = {}
    try:
        run_result = json.loads((Path(run_dir) / "run_result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    cost = run_result.get("cost_usd")
    usage = run_result.get("token_usage") or {}
    tokens = sum(int(value) for value in usage.values() if _is_number(value))
    judge_usage = summary.get("judge_usage") or {}
    judge_cost = judge_usage.get("judge_cost_usd")
    has_runner_cost = _is_number(cost)
    has_judge_cost = _is_number(judge_cost)
    total_cost = (float(cost) if has_runner_cost else 0.0) + (
        float(judge_cost) if has_judge_cost else 0.0
    )
    if has_runner_cost and has_judge_cost:
        return total_cost, tokens, "agent-eval-harness"
    if has_runner_cost:
        return total_cost, tokens, "partial:runner-only"
    if has_judge_cost:
        return total_cost, tokens, "partial:judge-only"
    return 0.0, tokens, "unavailable"
