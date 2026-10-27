"""Orchestrator for /eval-anova — fan out a matrix over eval-run + analyse.

The matrix orchestrator reads the ``matrix:`` block from an eval.yaml, and for
each condition (× replication) drives the *existing* eval-run pipeline
(workspace → execute → collect → score) once, producing a standard run with its
own ``summary.yaml``. It then computes the ANOVA/Pareto stats over those runs
(``analyze.analyze_runs`` → ``anova.json``) and renders the comparison report
via eval-compare. eval-run stays the single-condition primitive; eval-anova
only loops it — it does not re-implement execution.

Usage:
    python3 ${CLAUDE_SKILL_DIR}/scripts/orchestrate.py --config eval.yaml
    python3 ${CLAUDE_SKILL_DIR}/scripts/orchestrate.py --config eval.yaml --dry-run
    python3 ${CLAUDE_SKILL_DIR}/scripts/orchestrate.py --config eval.yaml --analyze-only

``RunResult`` is the per-cell result dataclass consumed by
``analyze.analyze_experiment`` (in-memory analysis) and tests.
"""

from __future__ import annotations

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import argparse
import datetime
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent_eval.config import EvalConfig
from agent_eval.anova.corrections import DEFAULT_CORRECTION
from agent_eval.anova.matrix import Condition, MatrixBuilder, _safe_id_segment

logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    """Result of a single cell execution."""

    condition: Condition
    case_id: str
    replication: int
    judge_results: dict[str, Any]
    composite: float
    metadata: dict[str, Any]


# ==========================================================================
# Matrix fan-out orchestrator (the runnable /eval-anova entrypoint)
# ==========================================================================

# Factors with real runner semantics; everything else is recorded as a
# condition dimension but cannot be applied through eval-run's execute.py.
_RUNNER_FACTORS = ("model", "effort", "subagent", "subagent_model")


def _child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a spawned python child.

    Each child is a fresh ``python3 script.py`` entry that has to activate
    ``.eval-venv`` itself. The bootstrap sentinel is designed to survive
    ``os.execv`` *within* one process; letting it cross into a child would make
    the child short-circuit activation and run without the venv's site-packages.
    """
    env = dict(os.environ)
    if extra:
        env.update(extra)
    # After the overrides, so `extra` cannot put the sentinel back.
    env.pop(agent_eval._bootstrap._SENTINEL, None)
    return env


def _repo_root() -> Path:
    # scripts/ -> eval-anova/ -> skills/ -> repo root. resolve() follows the
    # agent_eval symlink some test/plugin layouts use.
    return Path(__file__).resolve().parents[3]


def _eval_run_scripts() -> Path:
    return _repo_root() / "skills" / "eval-run" / "scripts"


def _enumerate_cases(config: Any) -> list[str]:
    """Case ids = dataset subdirectories that contain an input.yaml."""
    root = Path(config.resolve_path(config.dataset.path))
    if not root.is_dir():
        raise SystemExit(f"Dataset directory not found: {root}")
    return sorted(
        p.name for p in root.iterdir()
        if p.is_dir() and (p / "input.yaml").exists()
    )


def _map_factors(levels: dict[str, Any], config: Any) -> tuple[str, str | None, str | None, list[str]]:
    """Split a condition's factor levels into runner params + unmapped extras."""
    model = levels.get("model")
    if model is None:
        model = getattr(getattr(config, "models", None), "skill", None)
    effort = levels.get("effort")
    subagent = levels.get("subagent") or levels.get("subagent_model")
    unmapped = [k for k in levels if k not in _RUNNER_FACTORS]
    return (str(model) if model is not None else None, effort, subagent, unmapped)


def _cell_run_id(date: str, levels: dict[str, Any], rep: int, replications: int) -> str:
    parts = []
    if "model" in levels:
        parts.append(_safe_id_segment(str(levels["model"])))
    for k in sorted(levels):
        if k == "model":
            continue
        parts.append(f"{_safe_id_segment(k)}-{_safe_id_segment(str(levels[k]))}")
    slug = "-".join(p for p in parts if p) or "cell"
    run_id = f"{date}-{slug}"
    if replications > 1:
        run_id += f"-r{rep + 1}"
    return run_id


def _parse_workspace(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("WORKSPACE:"):
            return line.split("WORKSPACE:", 1)[1].strip()
    raise RuntimeError("workspace.py did not print a WORKSPACE: path")


def _run_eval_for_condition(
    *,
    config_path: str,
    run_id: str,
    output_dir: Path,
    model: str,
    effort: str | None = None,
    subagent_model: str | None = None,
    cases: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
    input_overrides: dict[str, Any] | None = None,
) -> Path:
    """Drive the eval-run pipeline once for a single condition (one run)."""
    scripts = _eval_run_scripts()
    py = sys.executable
    env = _child_env(extra_env)

    def _run(step: str, argv: list[str], capture: bool = False) -> str:
        res = subprocess.run(
            [py, str(scripts / step), *argv],
            capture_output=True, text=True, env=env,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"{step} failed for {run_id} (exit {res.returncode}):\n"
                f"{res.stderr[-2000:]}"
            )
        return res.stdout

    ws_argv = ["--config", config_path, "--run-id", run_id]
    if cases:
        ws_argv += ["--cases", *cases]
    workspace = _parse_workspace(_run("workspace.py", ws_argv, capture=True))

    ex_argv = ["--config", config_path, "--workspace", workspace,
               "--model", model, "--output", str(output_dir), "--run-id", run_id]
    if effort:
        ex_argv += ["--effort", effort]
    if subagent_model:
        ex_argv += ["--subagent-model", subagent_model]
    for key, value in (input_overrides or {}).items():
        ex_argv += ["--input-override", f"{key}={value}"]
    _run("execute.py", ex_argv)

    _run("collect.py", ["--config", config_path,
                        "--workspace", workspace, "--output", str(output_dir)])
    _run("score.py", ["judges", "--run-id", run_id, "--config", config_path,
                      "--workspace", workspace, "--model", model])
    return output_dir


def _stamp_condition(output_dir: Path, levels: dict[str, Any], condition_id: str) -> None:
    """Record a run's factor levels so analyse/compare can group and label it."""
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "condition.json").write_text(
        json.dumps({"condition_id": condition_id, "levels": levels}, indent=2, default=str)
    )


def fan_out(
    config: Any,
    config_path: str,
    conditions: list[Condition],
    cases: list[str],
    *,
    replications: int,
    runs_dir: Path,
    run_cell_fn: Callable[..., Any] | None = None,
) -> list[Path]:
    """Run every condition × replication through eval-run, stamping each run.

    ``run_cell_fn`` defaults to the real eval-run driver; tests inject a stub.
    A failure in one cell is logged and skipped so a partial matrix still
    yields an analysis over the cells that succeeded. Each run dir is stamped
    with its ``condition.json`` before the cell runs, so a cell that fails
    after writing a scored ``summary.yaml`` is still grouped under its real
    condition when the runs directory is analysed.
    """
    run_cell_fn = run_cell_fn or _run_eval_for_condition
    runs_base = str(runs_dir.parent)
    date = datetime.date.today().isoformat()
    produced: list[Path] = []

    for cond in conditions:
        model, effort, subagent, unmapped = _map_factors(cond.levels, config)
        if model is None:
            raise SystemExit(
                "No model for a condition: add a 'model' factor to matrix.factors "
                "or set models.skill in eval.yaml."
            )
        input_overrides = {k: cond.levels[k] for k in unmapped}
        if input_overrides:
            logger.info(
                "Passing non-model factor(s) %s to the runner as input overrides "
                "(usable as {name} in a cli command or {{ input.name }} in arguments).",
                ", ".join(sorted(input_overrides)),
            )
        for rep in range(replications):
            run_id = _cell_run_id(date, cond.levels, rep, replications)
            output_dir = runs_dir / run_id
            # Stamp before running: the levels are the design, known up front.
            # A cell that fails late (score.py exits 1 on any threshold breach)
            # still leaves a run dir that analyze_runs rediscovers — without the
            # stamp it would be re-grouped by model alone.
            _stamp_condition(output_dir, cond.levels, cond.condition_id)
            try:
                run_cell_fn(
                    config_path=config_path, run_id=run_id, output_dir=output_dir,
                    model=model, effort=effort, subagent_model=subagent,
                    cases=cases, extra_env={"AGENT_EVAL_RUNS_DIR": runs_base},
                    input_overrides=input_overrides,
                )
            except Exception as exc:  # noqa: BLE001 — keep the matrix going
                logger.error("Cell %s failed, skipping: %s", run_id, exc)
                continue
            produced.append(output_dir)
            print(f"  ✓ {run_id}", flush=True)

    return produced


def _resolve_correction(cli_value: str | None, matrix: Any) -> str:
    """CLI flag > matrix.analysis.correction > default (holm)."""
    if cli_value:
        return cli_value
    if matrix is not None and getattr(matrix, "correction", None):
        return matrix.correction
    return DEFAULT_CORRECTION


def _resolve_per_judge(cli_value: bool, matrix: Any) -> bool:
    """--per-judge > matrix.analysis.per_judge > off (the flag only enables)."""
    return bool(cli_value) or bool(
        matrix is not None and getattr(matrix, "per_judge", False))


def _analyze_and_report(config: Any, runs_dir: Path, *, report_output: str | None,
                        skip_report: bool, correction: str = DEFAULT_CORRECTION,
                        per_judge: bool = False) -> int:
    from analyze import analyze_runs  # lazy: pulls pandas/scipy only when needed

    analysis, artifact = analyze_runs(runs_dir, config, correction=correction,
                                      per_judge=per_judge)
    print(f"Wrote stats artifact: {artifact}")
    an = analysis.get("anova", {})
    print(f"ANOVA: {an.get('method', '?')} — "
          f"{'SIGNIFICANT' if an.get('significant') else 'not significant'}")
    pj = analysis.get("per_judge")
    if pj:
        print(f"Per-judge ANOVA: {len(pj.get('judges', {}))} judge(s) analysed "
              f"(BH family of {pj.get('family_size', 0)} test(s), "
              f"{len(pj.get('excluded', []))} excluded)")
    if not skip_report:
        _invoke_compare(runs_dir, report_output)
    return 0


def _invoke_compare(runs_dir: Path, output: str | None) -> None:
    compare = _repo_root() / "skills" / "eval-compare" / "scripts" / "compare.py"
    if not compare.exists():
        logger.info("eval-compare not found; skipping report render.")
        return
    argv = [sys.executable, str(compare), "generate", str(runs_dir)]
    if output:
        argv += ["--output", output]
    res = subprocess.run(argv, check=False, env=_child_env())
    if res.returncode != 0:
        logger.warning("eval-compare exited %s", res.returncode)


def _print_design(config: Any, matrix: Any, conditions: list[Condition],
                  cases: list[str], avg_cost: float | None) -> None:
    total = len(conditions) * len(cases) * matrix.replications
    print(f"Experiment: {MatrixBuilder.generate_experiment_id(matrix.factors)}")
    print(f"Factors: {', '.join(matrix.factors)}")
    print(f"Conditions: {len(conditions)} · Cases: {len(cases)} · "
          f"Replications: {matrix.replications}")
    print(f"Total runs: {total}")
    max_budget = getattr(config.execution, "max_budget_usd", None)
    if avg_cost is not None:
        print(f"Estimated cost: ${total * avg_cost:.2f} (@ ${avg_cost:.2f}/run)")
    elif max_budget:
        print(f"Estimated cost: ≤ ${total * float(max_budget):.2f} "
              f"(execution.max_budget_usd ${float(max_budget):.2f}/run upper bound)")
    else:
        print("Estimated cost: unknown — pass --avg-cost-per-run or set "
              "execution.max_budget_usd for an estimate.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, help="Path to eval.yaml")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate the matrix + estimate cost; run nothing")
    parser.add_argument("--analyze-only", action="store_true",
                        help="Re-analyse existing runs + re-render (no execution)")
    parser.add_argument("--cases", nargs="*", default=None,
                        help="Restrict to these case ids (default: all)")
    parser.add_argument("--avg-cost-per-run", type=float, default=None,
                        help="Per-run USD cost for the dry-run estimate")
    parser.add_argument("--output", default=None,
                        help="eval-compare report output dir")
    parser.add_argument("--no-report", action="store_true",
                        help="Skip the eval-compare report render")
    parser.add_argument("--correction", default=None,
                        choices=("holm", "bh", "fdr_bh", "none"),
                        help="Multiple-comparison correction across the ANOVA "
                             "term family (overrides matrix.analysis.correction; "
                             f"default: {DEFAULT_CORRECTION})")
    parser.add_argument("--per-judge", action="store_true",
                        help="Also run the ANOVA once per judge (screening), "
                             "Benjamini-Hochberg-corrected across the whole "
                             "judges×terms family (enables "
                             "matrix.analysis.per_judge)")
    args = parser.parse_args(argv)

    config = EvalConfig.from_yaml(args.config)
    runs_base = Path(os.environ.get("AGENT_EVAL_RUNS_DIR", "eval/runs")).resolve()
    runs_dir = runs_base / config.eval_name()

    if args.analyze_only:
        if not runs_dir.is_dir():
            raise SystemExit(f"No runs to analyse under {runs_dir}")
        # Non-strict: analyze-only also serves run dirs produced without a
        # matrix (CI fan-outs), where there is no block to read a config from.
        matrix = MatrixBuilder.from_yaml(Path(args.config))
        correction = _resolve_correction(args.correction, matrix)
        return _analyze_and_report(config, runs_dir,
                                   report_output=args.output, skip_report=args.no_report,
                                   correction=correction,
                                   per_judge=_resolve_per_judge(args.per_judge, matrix))

    matrix = MatrixBuilder.from_yaml(Path(args.config), strict=True)
    if matrix is None:
        raise SystemExit(f"No 'matrix:' section found in {args.config}")
    correction = _resolve_correction(args.correction, matrix)
    per_judge = _resolve_per_judge(args.per_judge, matrix)
    conditions = MatrixBuilder.expand_full_factorial(matrix.factors)
    cases = args.cases or _enumerate_cases(config)
    if not cases:
        raise SystemExit("No cases found in the dataset.")

    _print_design(config, matrix, conditions, cases, args.avg_cost_per_run)
    if args.dry_run:
        return 0

    runs_dir.mkdir(parents=True, exist_ok=True)
    produced = fan_out(config, args.config, conditions, cases,
                       replications=matrix.replications, runs_dir=runs_dir)
    if not produced:
        raise SystemExit("No cells completed successfully; nothing to analyse.")
    print(f"Completed {len(produced)} run(s).")
    return _analyze_and_report(config, runs_dir,
                               report_output=args.output, skip_report=args.no_report,
                               correction=correction, per_judge=per_judge)


if __name__ == "__main__":
    sys.exit(main())
