"""Post-experiment analysis — ANOVA + Pareto + archival.

Two entry points:

- ``analyze_experiment(run_results, factors)`` — analyse in-memory RunResult
  objects (used by the harbor benchmark driver and tests).
- ``analyze_runs(runs_dir, eval_config)`` — analyse a directory of *standard*
  eval-run runs (each a dir with ``summary.yaml`` [+ ``run_result.json`` /
  ``condition.json``]) and write an ``anova.json`` stats artifact next to them.
  This is what ``/eval-anova`` (and ``--analyze-only``) use, and what lets any
  set of runs — including ones produced by an external CI fan-out — be analysed
  without eval-anova having executed them.
"""

from __future__ import annotations

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import datetime
import hashlib
import json
import logging
import math
import sys
from pathlib import Path
from typing import Any

from agent_eval.anova.stats import ANOVA_AVAILABLE, missing_deps_message
from agent_eval.optimize.export import is_rollout_run

# pandas is module-level (DataFrames are this module's currency), so a missing
# anova extra fails here — long before the ANOVA_AVAILABLE check below could
# explain it. Raise the actionable message from the point that actually breaks.
try:
    import pandas as pd
except ImportError as exc:
    raise ImportError(missing_deps_message(exc)) from exc

import yaml

from agent_eval.anova.archive import ResultsArchiver
from agent_eval.anova.composite import aggregate_replications
from agent_eval.anova.corrections import DEFAULT_CORRECTION, normalize_correction
from agent_eval.harbor.reward import compose_reward, judge_ranges

logger = logging.getLogger(__name__)

# Per-judge observations travel in the same rows as the composite, under
# prefixed column names. The prefix keeps a judge called e.g. "model" from
# colliding with a factor or fixed column of the same name.
_JUDGE_PREFIX = "judge:"


def _judge_columns(per_judge: dict[str, Any]) -> dict[str, float]:
    """Per-judge observation columns for one case, prefixed ``judge:``.

    Accepts both judge-result shapes: the summary.yaml record
    (``{name: {"value": ...}}``) and the in-memory ``RunResult.judge_results``
    scalar form (``{name: True}`` / ``{name: 0.9}``) that analyze_experiment
    receives. Numeric judges pass through as-is and booleans coerce to 0/1 (a
    pass/fail judge is analysable as a rate). Pairwise verdicts, error/None
    samples, and non-finite values (``nan``/``inf`` — not a measurement, and
    ``inf`` would reach the fit as variance) yield no observation — the column
    still exists but holds NaN for that row, rather than an invented 0 that
    would drag the judge's mean. Registering the column even when nothing is
    observed is what lets a judge that never scores show up in ``excluded``
    with a reason instead of vanishing from the artifact.
    """
    cols: dict[str, float] = {}
    for name, rec in per_judge.items():
        if name == "pairwise":
            continue
        if isinstance(rec, dict):
            if rec.get("judge_type") == "pairwise":
                continue
            value = rec.get("value")
        else:
            value = rec
        key = f"{_JUDGE_PREFIX}{name}"
        if isinstance(value, bool):
            cols[key] = 1.0 if value else 0.0
        elif isinstance(value, (int, float)):
            try:
                as_float = float(value)
            except OverflowError:  # int beyond float range: not a measurement
                as_float = float("nan")
            cols[key] = as_float if math.isfinite(as_float) else float("nan")
        else:
            cols[key] = float("nan")
    return cols


def build_results_dataframe(
    run_results: list[Any],
) -> pd.DataFrame:
    """Convert RunResult list to a DataFrame for statistical analysis."""
    rows = []
    for r in run_results:
        row = {
            "case_id": r.case_id,
            "replication": r.replication,
            "composite": r.composite,
            "condition_id": r.condition.condition_id,
        }
        row.update(r.condition.levels)
        judge_results = getattr(r, "judge_results", None)
        if isinstance(judge_results, dict):
            row.update(_judge_columns(judge_results))
        rows.append(row)
    return pd.DataFrame(rows)


def analyze_experiment(
    run_results: list[Any],
    factors: list[str],
    *,
    alpha: float = 0.05,
    correction: str = DEFAULT_CORRECTION,
    per_judge: bool = False,
) -> dict[str, Any]:
    """Run statistical analysis on in-memory RunResult objects.

    Uses repeated-measures ANOVA for single-factor designs,
    mixed-effects model for multi-factor designs.
    """
    df = build_results_dataframe(run_results)
    return _analyze_df(df, factors, alpha=alpha, correction=correction,
                       n_runs=len(run_results), per_judge=per_judge)


def analyze_runs(
    runs_dir: Path | str,
    eval_config: Any,
    *,
    alpha: float = 0.05,
    correction: str = DEFAULT_CORRECTION,
    per_judge: bool = False,
    write_to: Path | str | None = None,
    allow_unaudited: bool = False,
    allow_mixed_enforcement: bool = False,
) -> tuple[dict[str, Any], Path]:
    """Analyse a directory of standard eval-run runs and write ``anova.json``.

    Discovers runs by ``summary.yaml``, computes each case's composite via the
    canonical harness reward composition (``compose_reward`` — honours the
    eval.yaml ``reward:`` section, else boolean-gates + normalised-numeric
    average), groups by condition (from ``condition.json`` levels, falling back
    to the model in ``run_result.json``), runs the ANOVA + Pareto, and writes
    the stats artifact. Returns ``(analysis, artifact_path)``.
    """
    runs_dir = Path(runs_dir)
    rows, factors, cost_by_condition = load_conditions_from_runs(
        runs_dir, eval_config, allow_unaudited=allow_unaudited,
        allow_mixed_enforcement=allow_mixed_enforcement)
    if not rows:
        raise ValueError(
            f"No scored runs found under {runs_dir} "
            "(expected run directories with a summary.yaml containing per_case)."
        )
    df = pd.DataFrame(rows)
    n_runs = int(df[["condition_id", "replication"]].drop_duplicates().shape[0])
    analysis = _analyze_df(
        df, factors, alpha=alpha, correction=correction, n_runs=n_runs,
        cost_by_condition=cost_by_condition, per_judge=per_judge,
    )
    analysis["generated_at"] = datetime.datetime.now(
        datetime.timezone.utc
    ).isoformat(timespec="seconds")

    out = Path(write_to) if write_to else runs_dir / "anova.json"
    out.write_text(json.dumps(_make_serializable(analysis), indent=2, default=str))
    logger.info("Wrote stats artifact %s", out)
    return analysis, out


def _analyze_df(
    df: pd.DataFrame,
    factors: list[str],
    *,
    alpha: float = 0.05,
    correction: str = DEFAULT_CORRECTION,
    n_runs: int | None = None,
    cost_by_condition: dict[str, float] | None = None,
    per_judge: bool = False,
) -> dict[str, Any]:
    """Core statistical analysis over a results DataFrame.

    Columns required: ``case_id``, ``composite``, ``condition_id`` and one
    column per factor. ``replication`` is optional. ``correction`` is the
    multiple-comparison correction applied across the ANOVA's term family
    (holm | bh | none). ``per_judge`` opts into the per-judge fan-out over
    any ``judge:``-prefixed observation columns (see ``_per_judge_analysis``).
    """
    if not ANOVA_AVAILABLE:
        raise ImportError(missing_deps_message())
    correction = normalize_correction(correction)

    from agent_eval.anova.stats.anova import mixed_effects_anova, repeated_measures_anova
    from agent_eval.anova.stats.pareto import pareto_frontier

    # Repeated-measures / mixed-effects ANOVA assume a fully-crossed design;
    # pingouin/statsmodels silently drop (listwise) any case missing from a
    # condition, which would leave the reported case count overstating what was
    # actually analysed. Restrict to cases present under every condition and
    # record the rest explicitly so the design/report stay honest.
    df, common_cases, excluded_cases = _restrict_to_common_cases(df)
    if excluded_cases:
        logger.warning(
            "Excluding %d case(s) not present under every condition: %s",
            len(excluded_cases), ", ".join(excluded_cases),
        )

    # rm_anova / mixedlm expect one observation per subject×cell, so average
    # replications per (condition, case) for the ANOVA input. The full df is
    # kept for the spread stats below.
    anova_df = df
    if {"condition_id", "case_id"}.issubset(df.columns):
        agg_spec: dict[str, Any] = {"composite": "mean"}
        for f in factors:
            if f in df.columns:
                agg_spec[f] = "first"
        anova_df = df.groupby(
            ["condition_id", "case_id"], as_index=False, dropna=False
        ).agg(agg_spec)

    n_conditions = int(df["condition_id"].nunique()) if "condition_id" in df.columns else 0
    # A factor with only one observed level can't contribute to the ANOVA (its
    # dummy coding has no contrasts) — drop it so, e.g., a single-model × context
    # matrix analyses cleanly as a one-way context comparison.
    effective = [f for f in factors if f in anova_df.columns and anova_df[f].nunique() >= 2]
    if not effective or n_conditions < 2:
        anova_result = {
            "method": "ANOVA (skipped)",
            "factor": effective[0] if effective else (factors[0] if factors else None),
            "factors": effective,
            "f_statistic": None,
            "p_value": None,
            "p_adjusted": None,
            "significant": False,
            "correction": correction,
            "family_size": 0,
            "alpha": alpha,
            "note": (f"No factor has >=2 levels to compare (conditions={n_conditions})."
                     if factors else "No factors to analyse."),
        }
    elif len(effective) == 1:
        anova_result = repeated_measures_anova(anova_df, factor=effective[0],
                                               alpha=alpha, correction=correction)
    else:
        anova_result = mixed_effects_anova(anova_df, factors=effective,
                                           alpha=alpha, correction=correction)

    cost_by_condition = cost_by_condition or {}
    condition_summaries = []
    for cid, group in df.groupby("condition_id"):
        scores = group["composite"].tolist()
        agg = aggregate_replications(scores)
        levels = {f: group[f].iloc[0] for f in factors if f in group.columns}
        summary = {
            "condition_id": cid,
            "levels": levels,
            # Factor levels are also flattened to top level (e.g. "model") so
            # the report renderer can read them directly without unpacking
            # "levels". Keep "levels" too for programmatic consumers.
            **levels,
            **agg,
        }
        if cid in cost_by_condition:
            summary["cost"] = cost_by_condition[cid]
        condition_summaries.append(summary)

    # Pareto frontier needs a real per-condition cost. Only compute it when
    # every condition has one; otherwise leave the frontier as all conditions
    # (no domination possible without a cost axis).
    if condition_summaries and all("cost" in c for c in condition_summaries):
        frontier = pareto_frontier(
            condition_summaries, cost_key="cost", quality_key="mean"
        )
    else:
        frontier = condition_summaries

    design = _build_design(df, factors)
    if excluded_cases:
        design["excluded_cases"] = excluded_cases
    per_case = _build_per_case(df, factors)

    # Post-hoc pairwise level contrasts are produced by the ANOVA functions
    # (from the same fit — no refitting); lift them to a top-level artifact
    # key so renderers don't dig inside the omnibus result.
    contrasts = anova_result.pop("contrasts", {})

    analysis = {
        "anova": anova_result,
        "contrasts": contrasts,
        "condition_summaries": condition_summaries,
        "pareto_frontier": frontier,
        "design": design,
        "per_case": per_case,
        "excluded_cases": excluded_cases,
        "n_runs": n_runs if n_runs is not None else int(len(df)),
        "n_conditions": len(condition_summaries),
    }
    # Opt-in fan-out over the same (common-case restricted) frame. Off by
    # default: it multiplies model fits and report rows, and the composite
    # above stays the headline either way.
    if per_judge:
        analysis["per_judge"] = _per_judge_analysis(df, factors, alpha=alpha)
    return analysis


def _per_judge_analysis(
    df: pd.DataFrame,
    factors: list[str],
    *,
    alpha: float = 0.05,
) -> dict[str, Any]:
    """Per-judge ANOVA fan-out — a screening companion to the composite.

    Runs the same single/multi-factor analysis the composite gets, once per
    judge over that judge's own per-case values, then applies one
    Benjamini-Hochberg correction across the whole judges×terms family of raw
    p-values. FDR is the right control here: the fan-out multiplies tests and
    per-judge effects are a screening question ("which judge moves?"), while
    the composite ANOVA keeps its own separate (Holm by default) family.

    A judge with a degenerate design — values under fewer than 2 conditions,
    fewer than 2 cases scored under every condition, a constant response, or a
    failed model fit — is excluded with an explicit reason and contributes no
    fabricated p to the family, so ``family_size`` counts only real tests.
    """
    from agent_eval.anova.stats.anova import (
        adjust_term_p_values, mixed_effects_anova, repeated_measures_anova)

    judges: dict[str, dict[str, Any]] = {}
    excluded: list[dict[str, str]] = []
    family: dict[tuple[str, str], float | None] = {}

    judge_cols = sorted(c for c in df.columns
                        if isinstance(c, str) and c.startswith(_JUDGE_PREFIX))
    has_keys = {"condition_id", "case_id"}.issubset(df.columns)
    for col in judge_cols if has_keys else []:
        name = col[len(_JUDGE_PREFIX):]
        sub = df[df[col].notna()]
        if sub.empty:
            # Two causes land here: every sample errored/was non-numeric, OR
            # the judge's scored rows were all dropped by the composite's
            # common-case restriction — don't misattribute one as the other.
            excluded.append({"judge": name,
                             "reason": "no scored samples among the analysed "
                                       "(common) cases — values were error/"
                                       "None, non-numeric, non-finite, or "
                                       "only on cases excluded by the "
                                       "common-case restriction"})
            continue
        n_conditions = int(sub["condition_id"].nunique())
        if n_conditions < 2:
            excluded.append({"judge": name,
                             "reason": f"values under only {n_conditions} "
                                       "condition(s) — nothing to compare"})
            continue
        # The same crossed-design restriction the composite gets, but on this
        # judge's own coverage: only cases it scored under every condition.
        case_sets = [set(g["case_id"]) for _, g in sub.groupby("condition_id")]
        common = set(case_sets[0]).intersection(*case_sets[1:])
        if len(common) < 2:
            excluded.append({"judge": name,
                             "reason": f"only {len(common)} case(s) scored "
                                       "under every condition (need >= 2)"})
            continue
        sub = sub[sub["case_id"].isin(common)]
        agg_spec: dict[str, Any] = {col: "mean"}
        for f in factors:
            if f in sub.columns:
                agg_spec[f] = "first"
        jdf = sub.groupby(["condition_id", "case_id"],
                          as_index=False, dropna=False).agg(agg_spec)
        if jdf[col].nunique() <= 1:
            excluded.append({"judge": name,
                             "reason": "constant value — no variance to analyse"})
            continue
        effective = [f for f in factors
                     if f in jdf.columns and jdf[f].nunique() >= 2]
        if not effective:
            excluded.append({"judge": name,
                             "reason": "no factor with >= 2 levels among its "
                                       "scored rows"})
            continue
        # The prefixed column name would read as an interaction in a patsy
        # formula ("judge:x" -> judge × x), so fit under a plain alias.
        jdf = jdf.rename(columns={col: "judge_value"})
        # correction="none" here means RAW per-term p-values: the multiplicity
        # correction happens once, below, across the whole judges×terms family.
        try:
            if len(effective) == 1:
                result = repeated_measures_anova(
                    jdf, factor=effective[0], response="judge_value",
                    alpha=alpha, correction="none")
            else:
                result = mixed_effects_anova(
                    jdf, factors=effective, response="judge_value",
                    alpha=alpha, correction="none")
        except Exception as exc:  # noqa: BLE001 — one judge must not sink the fan-out
            excluded.append({"judge": name,
                             "reason": f"model fit failed: {exc}"})
            continue
        raw = (result["p_values"] if isinstance(result.get("p_values"), dict)
               else {str(result.get("factor") or "effect"): result.get("p_value")})
        if all(p is None for p in raw.values()):
            excluded.append({"judge": name,
                             "reason": str(result.get("note")
                                           or "degenerate design — no finite "
                                              "p-value produced")})
            continue
        entry: dict[str, Any] = {
            "method": result.get("method"),
            "terms": {term: {"p_raw": p} for term, p in raw.items()},
            "n_cases": int(jdf["case_id"].nunique()),
            "n_conditions": n_conditions,
        }
        if result.get("note"):
            entry["note"] = str(result["note"])
        judges[name] = entry
        for term, p in raw.items():
            family[(name, term)] = p

    # One BH family across every real (judge, term) test. A term whose p is
    # None stays out of the family (p_adjusted None, not significant).
    p_adjusted, significant, family_size = adjust_term_p_values(
        family, correction="bh", alpha=alpha)
    for key in family:
        name, term = key
        cell = judges[name]["terms"][term]
        cell["p_adjusted"] = p_adjusted[key]
        cell["significant"] = significant[key]

    return {
        "correction": "bh",
        "family_size": family_size,
        "alpha": alpha,
        "judges": judges,
        "excluded": excluded,
        "note": ("Benjamini-Hochberg (FDR) across the one family of "
                 f"{family_size} (judge, term) test(s); screening only — the "
                 "composite ANOVA keeps its own separate correction family."),
    }


# --------------------------------------------------------------------------
# Loading standard eval-run runs into analysis rows
# --------------------------------------------------------------------------

def load_conditions_from_runs(
    runs_dir: Path | str,
    eval_config: Any,
    *,
    allow_unaudited: bool = False,
    allow_mixed_enforcement: bool = False,
) -> tuple[list[dict[str, Any]], list[str], dict[str, float]]:
    """Build analysis rows from a directory of standard eval-run runs.

    Returns ``(rows, factors, cost_by_condition)`` where each row is
    ``{case_id, replication, composite, condition_id, **levels}`` plus one
    ``judge:<name>`` column per non-pairwise judge that produced a value
    (bools as 0/1 — see ``_judge_columns``), feeding the opt-in per-judge
    fan-out. Runs sharing the same factor levels are treated as replications
    of one condition.
    """
    runs_dir = Path(runs_dir)
    reward_cfg = getattr(eval_config, "reward", None)
    ranges = judge_ranges(eval_config)

    rows: list[dict[str, Any]] = []
    factor_keys: set[str] = set()
    rep_counter: dict[str, int] = {}
    cost_sum: dict[str, float] = {}
    cost_n: dict[str, int] = {}
    cost_sources: dict[str, set] = {}
    enforcement_by_condition: dict[str, str] = {}

    for run_dir in _discover_run_dirs(runs_dir):
        try:
            summary = yaml.safe_load((run_dir / "summary.yaml").read_text()) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("Skipping %s: unreadable summary.yaml (%s)", run_dir, exc)
            continue
        per_case = summary.get("per_case") or {}
        if not per_case:
            continue
        levels = _condition_levels_for_run(run_dir)
        if not levels:
            logger.warning(
                "Skipping %s: no condition.json and no model in run_result.json "
                "— cannot assign it to a condition.", run_dir,
            )
            continue
        factor_keys.update(levels.keys())
        condition_id = _levels_id(levels)
        rep = rep_counter.get(condition_id, 0)
        rep_counter[condition_id] = rep + 1

        cost_info = _run_cost_info(run_dir)
        if cost_info["degraded"] and not allow_unaudited:
            # A run whose served providers escaped the declared pins is a
            # different condition, not a replication (spec 014 pooling rule).
            logger.warning("Skipping %s: routing audit degraded (violations or unattributed "
                           "generations under policy: strict); pass allow_unaudited to pool it.",
                           run_dir)
            rep_counter[condition_id] = rep
            continue
        first_level = enforcement_by_condition.setdefault(condition_id, cost_info["enforcement"])
        if cost_info["enforcement"] != first_level and not allow_mixed_enforcement:
            # audit vs key-guardrail vs none are different factor levels, not
            # replications of one condition (spec 014 pooling rule).
            logger.warning("Skipping %s: routing enforcement %r differs from the condition's %r; "
                           "pass allow_mixed_enforcement to pool it.",
                           run_dir, cost_info["enforcement"], first_level)
            rep_counter[condition_id] = rep
            continue
        cost = cost_info["cost"]
        if cost is not None:
            cost_sum[condition_id] = cost_sum.get(condition_id, 0.0) + cost
            cost_n[condition_id] = cost_n.get(condition_id, 0) + 1
            cost_sources.setdefault(condition_id, set()).add(cost_info["source"])

        for case_id, judges in per_case.items():
            if not isinstance(judges, dict):
                continue
            composite, _ = compose_reward(judges, reward_cfg=reward_cfg,
                                          judge_ranges=ranges)
            row = {
                "case_id": str(case_id),
                "replication": rep,
                "composite": float(composite),
                "condition_id": condition_id,
            }
            row.update(levels)
            # After the levels: the "judge:" prefix guarantees these never
            # shadow a factor column.
            row.update(_judge_columns(judges))
            rows.append(row)

    factors = sorted(factor_keys)
    cost_by_condition = {
        cid: cost_sum[cid] / cost_n[cid] for cid in cost_sum if cost_n.get(cid)
    }
    for cid, sources in cost_sources.items():
        classes = {"real" if src.startswith("openrouter:") else src for src in sources}
        if len(classes) > 1:
            logger.warning("Condition %s pools runs with mixed cost sources %s — provider-priced "
                           "spend and runner estimates are not comparable.", cid, sorted(sources))
    return rows, factors, cost_by_condition


def _discover_run_dirs(runs_dir: Path) -> list[Path]:
    """Run directories under ``runs_dir`` (each containing a ``summary.yaml``).

    Optimization rollouts are excluded: a campaign writes hundreds of run dirs
    whose ``condition.json`` describes a candidate and a seed, not a factor
    level, and pooling them into a design matrix produces one "condition" per
    rollout. Their provenance is not a factorial design either, so there is
    no ``--runs-dir`` that turns a campaign into a matrix here — use
    ``/eval-compare`` on the ``.optimize`` root to look at the search curve.
    """
    if not runs_dir.is_dir():
        return []
    found = sorted({p.parent for p in runs_dir.rglob("summary.yaml")})
    kept = [d for d in found if not is_rollout_run(d, runs_dir)]
    if len(kept) < len(found):
        # stderr, not logger.info: nothing configures logging here, so an info
        # record would be dropped and the operator told nothing.
        print(f"note: skipped {len(found) - len(kept)} optimization rollout dir(s) under {runs_dir} "
              "(not factorial cells)", file=sys.stderr)
    return kept


def _condition_levels_for_run(run_dir: Path) -> dict[str, Any]:
    """Factor levels for a run: ``condition.json`` if present, else the model
    from ``run_result.json`` (covers single-factor / externally-produced runs)."""
    cond_path = run_dir / "condition.json"
    if cond_path.is_file():
        try:
            data = json.loads(cond_path.read_text())
            levels = data.get("levels", data) if isinstance(data, dict) else None
            if isinstance(levels, dict) and "harness_run_id" in levels and "levels" not in data:
                # An optimization rollout's condition.json: candidate, seed,
                # substrate — provenance, not a factorial cell. Falling back
                # to the model keeps it from becoming its own condition if one
                # ever reaches here outside the .optimize tree.
                levels = None
            if isinstance(levels, dict) and levels:
                return {str(k): v for k, v in levels.items()}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Ignoring unreadable %s: %s", cond_path, exc)

    rr_path = run_dir / "run_result.json"
    if rr_path.is_file():
        try:
            model = json.loads(rr_path.read_text()).get("model")
            if model:
                return {"model": str(model)}
        except (json.JSONDecodeError, OSError):
            pass
    return {}


_LEGACY_COST_SOURCES = {"openrouter-reconciled": "openrouter:generation",
                        "runner-reported": "runner:reported", "harness-estimate": "harness:estimate"}


def _run_cost_info(run_dir: Path) -> dict:
    """Cost provenance of a run from ``run_result.json`` (spec 014): ``cost``
    (USD or None), ``source`` (canonical; legacy literals normalised; missing =
    runner:reported), ``degraded`` (routing audit failed under policy strict)
    and ``audit_clean``."""
    rr_path = run_dir / "run_result.json"
    info = {"cost": None, "source": "runner:reported", "degraded": False, "audit_clean": True,
            "enforcement": "none"}
    if not rr_path.is_file():
        return info
    try:
        rr = json.loads(rr_path.read_text())
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return info
    if not isinstance(rr, dict):
        return info
    cost = rr.get("cost_usd")
    info["cost"] = (float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool)
                    else None)
    source = rr.get("cost_source") or "runner:reported"
    info["source"] = _LEGACY_COST_SOURCES.get(source, source)
    routing = rr.get("routing") or {}
    info["degraded"] = bool(routing.get("degraded"))
    info["audit_clean"] = (not routing.get("violations")
                           and routing.get("audit_complete") is not False)
    info["enforcement"] = routing.get("enforcement") or "none"
    return info


def _run_cost(run_dir: Path) -> float | None:
    """Total USD cost for a run from ``run_result.json`` (``cost_usd``)."""
    return _run_cost_info(run_dir)["cost"]


def _levels_id(levels: dict[str, Any]) -> str:
    """Stable condition id from factor levels (matches matrix._condition_id)."""
    canonical = json.dumps(levels, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------
# Design / per-case helpers (shared)
# --------------------------------------------------------------------------

def _restrict_to_common_cases(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, list[str], list[str]]:
    """Keep only cases present under *every* condition (a balanced design).

    Returns ``(filtered_df, common_cases, excluded_cases)``. If the frame lacks
    the needed columns, is empty, or has a single condition, nothing is
    excluded. If no case is shared across all conditions the frame is returned
    unchanged (the ANOVA guards then flag the degenerate design).
    """
    if not {"condition_id", "case_id"}.issubset(df.columns) or df.empty:
        return df, [], []
    case_sets = [set(g["case_id"]) for _, g in df.groupby("condition_id")]
    all_cases = set().union(*case_sets)
    common = set(all_cases)
    for s in case_sets:
        common &= s
    excluded = sorted(str(c) for c in (all_cases - common))
    if not excluded:
        return df, sorted(str(c) for c in all_cases), []
    if not common:
        return df, [], excluded
    filtered = df[df["case_id"].isin(common)].copy()
    return filtered, sorted(str(c) for c in common), excluded


def _build_design(df: pd.DataFrame, factors: list[str]) -> dict[str, Any]:
    """Derive the experiment design (factors/levels, case count, reps)."""
    factor_levels = {
        f: sorted(df[f].dropna().unique().tolist())
        for f in factors
        if f in df.columns
    }
    n_cases = int(df["case_id"].nunique()) if "case_id" in df.columns else 0
    # Replications = the largest number of rows for any condition×case pair.
    if {"condition_id", "case_id"}.issubset(df.columns):
        replications = int(df.groupby(["condition_id", "case_id"]).size().max())
    else:
        replications = 1
    return {
        "factors": factor_levels,
        "n_cases": n_cases,
        "replications": replications,
    }


def _build_per_case(df: pd.DataFrame, factors: list[str]) -> dict[str, Any]:
    key_cols = [factor for factor in factors if factor in df.columns]
    if not key_cols and "condition_id" in df.columns:
        key_cols = ["condition_id"]
    if not key_cols or "case_id" not in df.columns:
        return {}

    per_case: dict[str, dict[str, float]] = {}
    for keys, group in df.groupby([*key_cols, "case_id"], dropna=False):
        values = keys if isinstance(keys, tuple) else (keys,)
        factor_values = values[:-1]
        case_id = values[-1]
        if len(key_cols) == 1:
            condition_key = str(factor_values[0])
        else:
            condition_key = _condition_key(key_cols, factor_values)
        per_case.setdefault(condition_key, {})[str(case_id)] = float(
            group["composite"].mean()
        )
    return per_case


def _condition_key(factors: list[str], values: tuple[Any, ...]) -> str:
    return ", ".join(f"{factor}={value}" for factor, value in zip(factors, values))


def archive_results(
    experiment_id: str,
    analysis: dict[str, Any],
    run_results: list[Any],
    repo_path: Path,
) -> Path:
    """Archive experiment results to the results repo."""
    archiver = ResultsArchiver(repo_path=repo_path)

    data = {
        "experiment_id": experiment_id,
        "analysis": _make_serializable(analysis),
        "n_runs": len(run_results),
    }

    return archiver.archive_experiment(experiment_id, data, fallback=True)


def _make_serializable(obj: Any) -> Any:
    """Convert non-serializable types for JSON output."""
    if isinstance(obj, dict):
        return {k: _make_serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_make_serializable(i) for i in obj]
    if isinstance(obj, float) and (obj != obj):  # NaN check
        return None
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return obj
