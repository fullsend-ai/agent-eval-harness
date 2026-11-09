"""Place a rollout's artifacts into the standard harness run-dir layout.

Every optimization rollout, regardless of search engine, lands as an ordinary
run directory — ``run_result.json``, ``summary.yaml``, ``report.html``, a
``condition.json`` identifying it — under a **parallel runs root**::

    $AGENT_EVAL_RUNS_DIR/.optimize/<eval-name>/<run-id>

The scripts that take a ``--run-id`` (score.py, report.py, log_results.py,
attach_feedback.py) resolve it as ``$AGENT_EVAL_RUNS_DIR/<eval-name>/<run-id>``;
each also accepts ``--run-dir <path>`` naming the directory outright, which is
how the rollout runner hands them a rollout and how a person does by hand
(``python3 skills/eval-mlflow/scripts/log_results.py --run-dir
eval/runs/.optimize/<eval>/<run-id> --config eval.yaml``). The environment
variable is deliberately left alone: it is forwarded into the agent under test
and read by the few-shot ``examples:`` harvest, neither of which should see
the rollout root.

The separate root is what keeps a campaign from drowning the ordinary run set:
one campaign is ``|val| x num_trials x (iterations + 1)`` run dirs, each one a
legitimate ``summary.yaml`` that ``/eval-compare`` and ``/eval-anova`` would
otherwise sweep up by ``rglob`` and chart beside the two or three runs a person
meant to compare. :func:`is_rollout_run` is the shared predicate those
discoverers filter on; pointing a tool straight at ``.optimize`` (or anywhere
inside it) still works, because then the segment is above the input root
rather than below it — that is how you browse the rollouts' own reports (the
search curve itself is cap-evolve's: ``--status``, ``history.jsonl``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

#: Name of the parallel runs root, directly under ``$AGENT_EVAL_RUNS_DIR``.
ROLLOUT_SUBDIR = ".optimize"


def rollout_runs_root(manifest: dict) -> Path:
    """``$AGENT_EVAL_RUNS_DIR/.optimize``, resolved against the project."""
    base = Path(os.environ.get("AGENT_EVAL_RUNS_DIR", "eval/runs"))
    if not base.is_absolute():
        base = Path(manifest["project_root"]) / base
    return base.resolve() / ROLLOUT_SUBDIR


def runs_dir_for(manifest: dict) -> Path:
    """Where this campaign's rollouts go. Recorded in the harness manifest at
    scaffold time (``rollouts_dir``) so every later reader — the rollouts
    themselves, ``--status``, the coverage warning — agrees on one place
    regardless of the environment it happens to run in; computed only for a
    manifest from before that key existed."""
    recorded = manifest.get("rollouts_dir")
    if recorded:
        return Path(recorded)
    return rollout_runs_root(manifest) / manifest["eval_name"]


def is_rollout_dir(run_dir: Path | str) -> bool:
    """A rollout's ``condition.json`` records provenance (candidate, seed,
    harness run id), never factor ``levels``. Releases up to 1.62.x wrote
    rollouts flat into ``<runs>/<eval-name>/``, so the path alone cannot
    identify those — the file can."""
    try:
        data = json.loads((Path(run_dir) / "condition.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (isinstance(data, dict) and "levels" not in data
            and "harness_run_id" in data and "candidate_id" in data)


def is_rollout_run(run_dir: Path | str, input_dir: Path | str) -> bool:
    """True when *run_dir* is an optimization rollout nested under *input_dir*.

    False when *input_dir* is itself the ``.optimize`` root or inside it, or is
    the rollout directory itself: asking for the rollouts is how you get the
    search curve, and that stays available. Only the incidental sweep is
    filtered — including legacy flat rollouts, recognised by their
    ``condition.json`` rather than their path.
    """
    run_dir = Path(run_dir)
    root = Path(input_dir).resolve()
    if ROLLOUT_SUBDIR in root.parts:
        return False  # the caller asked for the rollouts
    try:
        relative = run_dir.resolve().relative_to(root)
    except (ValueError, OSError):
        return False
    if not relative.parts:
        return False
    if ROLLOUT_SUBDIR in relative.parts[:-1]:
        return True
    return is_rollout_dir(run_dir)


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
    # judge_usage carries its own token fields (prompt/completion/reasoning) —
    # score.py's aggregate_judge_usage() docstring — separate from run_result's
    # token_usage, so the exported total silently dropped them otherwise.
    tokens += sum(
        int(judge_usage[key]) for key in ("prompt_tokens", "completion_tokens", "reasoning_tokens")
        if _is_number(judge_usage.get(key))
    )
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
