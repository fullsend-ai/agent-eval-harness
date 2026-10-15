"""Gold-answer scrubbing and outcome-only feedback construction.

Keeps any optimizer's proposer from ever seeing expected/gold content: the
workspace a rollout runs in never carries answers.yaml, and only pass/fail/
numeric outcome labels — never judge rationale — reach the returned feedback
string.
"""

from __future__ import annotations

from pathlib import Path


def remove_gold(case_workspace: Path) -> None:
    answers = Path(case_workspace) / "answers.yaml"
    if answers.is_symlink() or answers.is_file():
        answers.unlink()


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
