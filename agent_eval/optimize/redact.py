"""Gold-answer scrubbing and bounded feedback construction.

Keeps any optimizer's proposer from ever seeing expected/gold content: the
workspace a rollout runs in never carries the judges' answer key
(annotations.yaml), and the feedback string carries pass/fail/numeric outcome
labels. Judge rationale is redacted by default (it can quote gold); the one
exception is a judge the eval author marked gold-free
(``feedback_rationale: true``), whose rationale reaches the proposer as a
bounded, whitespace-collapsed excerpt — a mis-marked judge leaks a fragment,
not the whole answer key. answers.yaml is NOT gold: it is
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


def build_redacted_feedback(per_case_judges: dict, reward: float,
                            rationale_judges: "frozenset[str] | set[str] | None" = None) -> str:
    """Outcome labels for every judge; bounded rationale only for opted-in ones.

    ``rationale_judges`` names judges whose rationale the eval author has
    marked gold-free (``feedback_rationale: true`` in eval.yaml). Even then
    the excerpt is whitespace-collapsed and length-capped: an opted-in judge
    that nonetheless quotes the answer key leaks a bounded fragment, not the
    whole thing. Everyone else stays outcome-only — a judge that paraphrases
    a gold answer defeats any content matcher, so the default is redaction
    by construction rather than filtering.
    """
    rationale_judges = frozenset(rationale_judges or ())
    outcomes = []
    for name, result in sorted(per_case_judges.items()):
        value = result.get("value") if isinstance(result, dict) else None
        if isinstance(value, bool):
            outcomes.append(f"{name}={'pass' if value else 'fail'}")
        elif isinstance(value, (int, float)):
            outcomes.append(f"{name}={float(value):g}")
        elif value is None:
            outcomes.append(f"{name}=unscored")
        if name in rationale_judges:
            rationale = (result.get("rationale") or "") if isinstance(result, dict) else ""
            excerpt = " ".join(str(rationale).split())[:400]
            if excerpt:
                outcomes.append(f"{name}_rationale={excerpt}")
    return f"Harness reward: {reward:.4f}. Judge outcomes: " + ", ".join(outcomes)
