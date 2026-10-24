"""Gold-answer scrubbing and outcome-only feedback construction.

Keeps any optimizer's proposer from ever seeing expected/gold content: the
workspace a rollout runs in never carries the judges' answer key
(annotations.yaml), and only pass/fail/numeric outcome labels — never judge
rationale — reach the returned feedback string. answers.yaml is NOT gold: it is
the guidance the AskUserQuestion interceptor reads from the workspace, so it
stays, exactly as in a plain /eval-run.
"""

from __future__ import annotations

from pathlib import Path


def remove_gold(case_workspace: Path) -> None:
    # The workspace builders never copy it; this is a guard, not a filter.
    gold = Path(case_workspace) / "annotations.yaml"
    if gold.is_symlink() or gold.is_file():
        gold.unlink()


def build_redacted_feedback(per_case_judges: dict, reward: float) -> str:
    outcomes = []
    for name, result in sorted(per_case_judges.items()):
        value = result.get("value") if isinstance(result, dict) else None
        if isinstance(value, bool):
            outcomes.append(f"{name}={'pass' if value else 'fail'}")
        elif isinstance(value, (int, float)):
            outcomes.append(f"{name}={float(value):g}")
        elif value is None:
            outcomes.append(f"{name}=unscored")
    return f"Harness reward: {reward:.4f}. Judge outcomes: " + ", ".join(outcomes)
