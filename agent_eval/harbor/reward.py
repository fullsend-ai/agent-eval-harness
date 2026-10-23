#!/usr/bin/env python3
"""Judge -> Harbor ``reward.json`` bridge.

Runs the agent-eval-harness judge engine against a SINGLE case directory and
writes Harbor's reward contract (``reward.json`` / ``reward.txt``) plus a richer
``judges.json`` sidecar (per-judge value + rationale) for the suite layer.

This is what makes judgment-graded tasks work on Harbor: inside the task
container, the verifier (``tests/test.sh``) calls this module, which reuses
``load_judges`` + ``score_cases`` from ``skills/eval-run/scripts/score.py`` —
the same engine the local ``/eval-run`` path uses — so per-case grading is
identical whether run locally or in a Harbor trial.

Reward composition (resolution order):
1. If a ``reward:`` section exists in eval.yaml, use its formula/weights
   to compose the reward from judge results. Supports ``weighted``,
   single judge reference, or Python expression modes.
2. Otherwise: boolean judges gate (any fail -> 0.0), numeric judges
   normalized to [0,1] and averaged.

Either way a numeric judge is normalized over its OWN declared ``score_range``;
``reward.score_range`` is only a fallback for judges that declare none.

Pairwise comparison and regression thresholds are SUITE-level (need >=2 runs /
the full set) and stay above Harbor — they are not computed here.

Usage:
    python3 -m agent_eval.harbor.reward --config eval.yaml [--case-dir .] \\
        [--run-id <id>] [--out-dir /logs/verifier]
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv before 3p imports
import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Optional

import ast

from agent_eval.config import EvalConfig, RewardConfig

_SCORE_MIN_DEFAULT = 1.0
_SCORE_MAX_DEFAULT = 5.0

_SAFE_AST_NODES = (
    ast.Module, ast.Expr, ast.Expression, ast.Assign,
    ast.BinOp, ast.UnaryOp, ast.Compare,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
    ast.USub, ast.UAdd,
    ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.Eq, ast.NotEq,
    ast.Constant, ast.Name, ast.Load, ast.Store,
    ast.Call, ast.List, ast.Tuple,
    ast.IfExp, ast.BoolOp, ast.And, ast.Or,
)
# ast.Pow (**) is intentionally excluded: integer exponentiation is the cheap
# path to a CPU/memory blow-up (e.g. 2 ** 10**9) and reward formulas never need
# it. Reward formulas are bounded numeric arithmetic, so cap overall size and
# literal magnitude as defence-in-depth even though eval.yaml is trusted config.
_MAX_FORMULA_NODES = 200
_MAX_CONSTANT_ABS = 1e6

# Functions callable from within a reward formula expression. The keys double
# as the allow-list of permitted call names during AST validation.
_SAFE_FUNCS = {
    "min": min, "max": max, "abs": abs, "round": round,
    "sum": sum, "len": len,
    "mean": lambda xs: sum(xs) / len(xs) if xs else 0.0,
}


def _validate_ast(node: ast.AST, allowed_calls: set[str]) -> None:
    """Walk AST and reject any node not in the safe allowlist."""
    if not isinstance(node, _SAFE_AST_NODES):
        raise ValueError(
            f"Disallowed expression node: {type(node).__name__}")
    if isinstance(node, ast.Call):
        if not (isinstance(node.func, ast.Name)
                and node.func.id in allowed_calls):
            func_name = getattr(node.func, 'id', '?')
            raise ValueError(
                f"Disallowed function call: {func_name}")
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
        if node.id.startswith('_'):
            raise ValueError(
                f"Disallowed variable name: {node.id}")
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, bool) or v is None:
            pass  # bool/None are fine (e.g. ternary fallbacks)
        elif isinstance(v, (int, float)):
            if abs(v) > _MAX_CONSTANT_ABS:
                raise ValueError(f"Constant magnitude too large: {v}")
        else:
            # Reject str/bytes constants — they have no place in numeric
            # arithmetic and enable repetition blow-ups (e.g. "x" * 10**6).
            raise ValueError(
                f"Disallowed constant type: {type(v).__name__}")
    for child in ast.iter_child_nodes(node):
        _validate_ast(child, allowed_calls)


def _parse_and_validate(formula: str, allowed_calls: set[str]) -> ast.Module:
    """Parse a formula and reject anything outside the safe subset.

    Enforces: parses cleanly, stays under the node-count cap, uses only
    allow-listed AST nodes/calls/constants, and ends in an expression (the
    return value). Returns the parsed module for evaluation.
    """
    try:
        tree = ast.parse(formula.strip(), mode="exec")
    except SyntaxError as exc:
        raise ValueError(f"could not parse formula: {exc}") from exc
    node_count = sum(1 for _ in ast.walk(tree))
    if node_count > _MAX_FORMULA_NODES:
        raise ValueError(
            f"reward formula too complex ({node_count} nodes > "
            f"{_MAX_FORMULA_NODES})")
    _validate_ast(tree, allowed_calls)
    if not tree.body:
        raise ValueError("Empty reward formula")
    if not isinstance(tree.body[-1], ast.Expr):
        raise ValueError(
            "Last line of reward formula must be an expression "
            "(the return value), not an assignment")
    return tree


def formula_judge_names(formula: str) -> set:
    """Judge names an expression formula reads.

    Excludes the safe builtins and any name the formula assigns to itself, so
    ``gate = mean([a, b])\ngate * score`` yields {a, b, score}. Used to scope
    the reward.score_range deprecation warning to judges a formula can
    actually normalize. Returns an empty set for an unparseable formula —
    `validate_formula` is what rejects those.
    """
    try:
        tree = ast.parse(formula.strip())
    except SyntaxError:
        return set()
    assigned = {t.id for node in ast.walk(tree)
                if isinstance(node, ast.Assign)
                for t in node.targets if isinstance(t, ast.Name)}
    read = {node.id for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
    return read - assigned - set(_SAFE_FUNCS)


def _safe_eval_formula(formula: str, variables: dict[str, float],
                       safe_funcs: dict) -> float:
    """Evaluate a reward formula using AST validation (no raw exec/eval).

    The formula is parsed as a Python module. All statements except the
    last must be assignments. The last statement must be an expression
    whose value is returned as the reward.
    """
    tree = _parse_and_validate(formula, set(safe_funcs.keys()))
    last_stmt = tree.body[-1]

    ns = {**variables, **safe_funcs}

    if len(tree.body) > 1:
        setup = ast.Module(body=tree.body[:-1], type_ignores=[])
        ast.fix_missing_locations(setup)
        code = compile(setup, "<reward>", "exec")
        exec(code, {"__builtins__": {}}, ns)  # noqa: S102 — AST-validated

    expr = ast.Expression(body=last_stmt.value)
    ast.fix_missing_locations(expr)
    code = compile(expr, "<reward>", "eval")
    return eval(code, {"__builtins__": {}}, ns)  # noqa: S307 — AST-validated


def validate_formula(formula: str) -> None:
    """Parse and AST-validate a reward formula without evaluating it.

    Raises ``ValueError`` if the formula is syntactically invalid, uses a
    disallowed construct/function, or does not end in an expression. Called at
    config-load time (see ``EvalConfig.from_yaml``) so a malformed or unsafe
    formula fails loudly there instead of silently yielding reward 0.0 on
    every case during a run. Note this validates structure only; runtime
    failures (e.g. an undefined judge name, division by zero) are still
    caught at evaluation time and degrade to reward 0.0 with a warning.
    """
    _parse_and_validate(formula, set(_SAFE_FUNCS))


# Harbor's canonical verifier output directory inside the container.
_DEFAULT_OUT_DIR = "/logs/verifier"


def _load_score_module():
    """Load the judge engine from skills/eval-run/scripts/score.py.

    The engine lives with the eval-run skill (not in the agent_eval package),
    so load it by path — mirrors agent_eval/evalhub/adapter.py._get_score_module.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    score_path = repo_root / "skills" / "eval-run" / "scripts" / "score.py"
    if not score_path.exists():
        raise FileNotFoundError(f"Judge engine not found: {score_path}")
    spec = importlib.util.spec_from_file_location("agent_eval_score", score_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for required in ("load_judges", "score_cases"):
        if not hasattr(mod, required):
            raise AttributeError(f"score.py is missing {required}()")
    return mod


def score_case(config: EvalConfig, case_dir: Path,
               run_id: Optional[str] = None) -> dict:
    """Run all (non-pairwise) judges against one case directory.

    Returns the per-judge result dict: ``{judge_name: {value, rationale,
    judge_type}}`` for the single case.
    """
    return _score_case_counted(config, case_dir, run_id=run_id)[0]


def _score_case_counted(config: EvalConfig, case_dir: Path,
                        run_id: Optional[str] = None) -> tuple:
    """`score_case` plus the spec 015 verdict-transport counters of the
    engine instance that ran it: ``(per_judge, transports, fallbacks)``."""
    score = _load_score_module()
    judges = score.load_judges(config, project_root=Path.cwd())
    # load_case_record only reads stdout/stderr/metrics when a run_id is set; in
    # the container there is no run dir, so default it to the case dir name so
    # stdout-based judges (e.g. pipeline_flow) see case_dir/stdout.log.
    effective_run_id = run_id or case_dir.name
    result = score.score_cases(judges, [case_dir], config, run_id=effective_run_id)
    transports, fallbacks = score._verdict_transport_counts()
    return result.get("per_case", {}).get(case_dir.name, {}), transports, fallbacks


def _extract_metrics(per_judge: dict) -> dict[str, float]:
    """Build flat metric dict from per-judge results."""
    metrics: dict[str, float] = {}
    for name, rec in per_judge.items():
        value = rec.get("value")
        if value is None:
            continue
        if isinstance(value, bool):
            metrics[name] = 1.0 if value else 0.0
        elif isinstance(value, (int, float)):
            metrics[name] = float(value)
    return metrics


def _normalize(value: float, score_range: list[float]) -> float:
    """Normalize a score to [0, 1] given a [min, max] range."""
    lo, hi = score_range
    span = hi - lo
    if span <= 0:
        return 0.0
    return max(0.0, min(1.0, (value - lo) / span))


def _resolve_range(name: str, judge_ranges: Optional[dict],
                   fallback) -> tuple[float, float]:
    """The scale a judge's value is normalized over.

    The judge's own declared `score_range` wins. That range is what the harness
    already puts in the judge's prompt and tool schema, enforces the answer
    against, and bands the report cell with — normalizing it against a
    different one is a unit error. `fallback` (``reward.score_range``, else
    ``[1, 5]``) applies only to judges that declare nothing.
    """
    declared = (judge_ranges or {}).get(name)
    lo, hi = declared if declared else fallback
    return float(lo), float(hi)


def _judge_value(per_judge: dict, name: str,
                 score_range: list[float],
                 raw_judges: list[str] = (),
                 judge_ranges: Optional[dict] = None) -> Optional[float]:
    """Extract a judge's value as a float in [0, 1]."""
    rec = per_judge.get(name, {})
    val = rec.get("value")
    if val is None:
        return None
    if isinstance(val, bool):
        return 1.0 if val else 0.0
    if isinstance(val, (int, float)):
        if name in raw_judges:
            return max(0.0, min(1.0, float(val)))
        return _normalize(float(val), list(_resolve_range(
            name, judge_ranges, score_range)))
    return None


def compute_reward_from_config(per_judge: dict,
                               reward_cfg: RewardConfig,
                               *,
                               judge_ranges: Optional[dict] = None) -> float:
    """Compute reward using the eval.yaml reward: section.

    Resolution within the section:
    - ``judge``: that single judge's value is the reward (clamped to [0, 1],
      or normalized over that judge's own score_range when ``normalize`` is
      set).
    - "weighted": weighted sum of ``weights``.
    - "<expression>": Python expression with judge names as variables.

    ``judge_ranges`` maps judge name -> declared ``(lo, hi)``; a judge that
    declares one is normalized over it, and ``reward_cfg.score_range`` (else
    [1, 5]) covers only the rest. ``raw`` judges and a clamped single judge
    consult no range at all.
    """
    score_range = reward_cfg.effective_score_range
    raw_judges = reward_cfg.raw

    if reward_cfg.gate:
        for name, rec in per_judge.items():
            val = rec.get("value")
            if isinstance(val, bool) and not val:
                return 0.0

    # Single-judge mode: the judge's value IS the reward. Clamp as-is by
    # default; normalize via score_range only when asked. A missing/skipped
    # judge (value None) scores 0.0.
    if reward_cfg.judge is not None:
        clamp_raw = () if reward_cfg.normalize else (reward_cfg.judge,)
        jv = _judge_value(per_judge, reward_cfg.judge, score_range, clamp_raw,
                          judge_ranges=judge_ranges)
        return jv if jv is not None else 0.0

    formula = reward_cfg.formula.strip()

    if formula == "weighted":
        if not reward_cfg.weights:
            return 0.0
        total = 0.0
        weight_sum = 0.0
        for judge_name, weight in reward_cfg.weights.items():
            jv = _judge_value(per_judge, judge_name, score_range, raw_judges,
                              judge_ranges=judge_ranges)
            if jv is not None:
                total += float(weight) * jv
                weight_sum += float(weight)
        return max(0.0, min(1.0, total / weight_sum)) if weight_sum > 0 else 0.0

    judge_vars: dict[str, float] = {}
    for name, rec in per_judge.items():
        jv = _judge_value(per_judge, name, score_range, raw_judges,
                          judge_ranges=judge_ranges)
        if jv is not None:
            judge_vars[name] = jv

    try:
        result = _safe_eval_formula(formula, judge_vars, _SAFE_FUNCS)
        return max(0.0, min(1.0, float(result)))
    except Exception as exc:
        print(f"Warning: reward formula evaluation failed: {exc}",
              file=sys.stderr)
        return 0.0


def compose_reward(per_judge: dict, *,
                   score_min: float = _SCORE_MIN_DEFAULT,
                   score_max: float = _SCORE_MAX_DEFAULT,
                   judge_ranges: Optional[dict] = None,
                   reward_cfg: Optional[RewardConfig] = None) -> tuple[float, dict]:
    """Collapse per-judge results into an overall reward + flat metric dict.

    Resolution order:
    1. If reward_cfg is provided (from eval.yaml reward: section), use it.
    2. Otherwise fall back to: boolean gates + average of normalized numerics.

    On BOTH paths each numeric judge is normalized over its OWN declared
    ``score_range`` when ``judge_ranges`` supplies one, and over the fallback
    otherwise. Without this a 0-2 judge normalized against the 1-5 default maps
    both 0 and 1 to 0.0 and its top score to 0.25, silently collapsing most of
    the scale.
    """
    metrics = _extract_metrics(per_judge)

    if reward_cfg is not None:
        reward = compute_reward_from_config(per_judge, reward_cfg,
                                            judge_ranges=judge_ranges)
        return reward, metrics

    gate_ok = True
    normalized_scores: list[float] = []
    failed = False

    for name, rec in per_judge.items():
        value = rec.get("value")
        if value is None:
            # A judge that errored produced no verdict; one skipped by its
            # `if:` condition was never meant to. Only the former means the
            # trial went unscored.
            failed = failed or bool(rec.get("error"))
            continue
        if isinstance(value, bool) and not value:
            gate_ok = False
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            lo, hi = _resolve_range(name, judge_ranges, (score_min, score_max))
            span = hi - lo
            norm = (float(value) - lo) / span if span else 0.0
            normalized_scores.append(max(0.0, min(1.0, norm)))

    if not gate_ok:
        reward = 0.0
    elif normalized_scores:
        reward = sum(normalized_scores) / len(normalized_scores)
    elif failed:
        # Nothing scored because every scoring judge errored. The 1.0 below is
        # for a gates-only config, where passing every gate IS the top reward;
        # handing it to a trial we could not score inverts the signal — a model
        # that ignores its rubric's scale earns a ScoreRangeError on each judge
        # and outscores one that obeys it (2/2 on [0, 2] composes to 1.0, but
        # 1/1 composes to 0.5). Since a failed gate is already 0.0, an unscored
        # trial is too.
        reward = 0.0
    else:
        reward = 1.0
    return reward, metrics


def judge_ranges(config) -> dict:
    """Map judge name -> declared ``(lo, hi)`` from each judge's score_range.

    Only judges that declare a range appear; the rest fall back to the default
    scale. Feeds ``compose_reward``'s per-judge normalization.
    """
    return {j.name: (j.score_range[0], j.score_range[1])
            for j in (getattr(config, "judges", None) or [])
            if getattr(j, "score_range", None)}


def build_reward(config: EvalConfig, case_dir: Path,
                 run_id: Optional[str] = None) -> dict:
    """Score a case and build the full reward payload.

    Returns a dict with ``reward`` (overall float), per-judge ``metrics``, and
    the full ``per_judge`` detail (value + rationale) for the sidecar.
    """
    per_judge, transports, fallbacks = _score_case_counted(config, case_dir, run_id=run_id)

    reward_cfg = getattr(config, "reward", None)

    reward, metrics = compose_reward(per_judge, reward_cfg=reward_cfg,
                                     judge_ranges=judge_ranges(config))
    payload = {"reward": reward, "metrics": metrics, "per_judge": per_judge}
    # Spec 015 judge-usage counters: the in-container engine's transport tally
    # rides the sidecar so the host can aggregate it across trials.
    if transports:
        payload["verdict_transports"] = transports
    if fallbacks:
        payload["verdict_fallbacks"] = fallbacks
    return payload


def write_reward(payload: dict, out_dir: Path, case_dir: Optional[Path] = None) -> None:
    """Write Harbor's reward files + the judges.json sidecar.

    - ``<out_dir>/reward.json``  flat {reward, <judge>: <num>, ...} (Harbor reads this)
    - ``<out_dir>/reward.txt``   the scalar reward (Harbor's fallback)
    - ``<out_dir>/judges.json``  full per-judge detail (value + rationale)
    - ``<case_dir>/judges.json`` same sidecar, alongside the artifacts, for the
      suite layer / report when results are downloaded back to the host
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    reward = payload["reward"]

    reward_json = {"reward": reward, **payload["metrics"]}
    (out_dir / "reward.json").write_text(json.dumps(reward_json, indent=2))
    (out_dir / "reward.txt").write_text(str(reward))

    sidecar_data = {"reward": reward, "per_judge": payload["per_judge"]}
    for counter in ("verdict_transports", "verdict_fallbacks"):
        if payload.get(counter):
            sidecar_data[counter] = payload[counter]
    sidecar = json.dumps(sidecar_data, indent=2, default=str)
    (out_dir / "judges.json").write_text(sidecar)
    if case_dir is not None:
        (case_dir / "judges.json").write_text(sidecar)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", required=True, help="Path to eval.yaml")
    parser.add_argument("--case-dir", default=".",
                        help="Case workspace dir holding the collected artifacts "
                             "(default: cwd)")
    parser.add_argument("--run-id", default=None,
                        help="Run id for execution-metadata lookup (optional)")
    parser.add_argument("--out-dir", default=_DEFAULT_OUT_DIR,
                        help=f"Where to write reward files (default: {_DEFAULT_OUT_DIR})")
    args = parser.parse_args()

    config = EvalConfig.from_yaml(args.config)
    case_dir = Path(args.case_dir).resolve()

    payload = build_reward(config, case_dir, run_id=args.run_id)
    write_reward(payload, Path(args.out_dir), case_dir=case_dir)

    metric_summary = ", ".join(
        f"{k}={v}" for k, v in payload["metrics"].items()) or "(no judges scored)"
    print(f"reward={payload['reward']:.4f}  [{metric_summary}]")
    print(f"wrote {args.out_dir}/reward.json, reward.txt, judges.json")


if __name__ == "__main__":
    main()
