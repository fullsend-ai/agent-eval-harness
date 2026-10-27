"""Tests for the eval-anova matrix orchestrator (fan-out over eval-run).

The per-cell execution is stubbed, so these validate the orchestration logic —
the grid loop, factor→param mapping, condition.json stamping, dry-run cost,
--analyze-only, and per-cell failure tolerance — without any real agent runs.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import yaml

_scripts_dir = str(Path(__file__).parent.parent / "skills" / "eval-anova" / "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

import orchestrate as O  # noqa: E402
from agent_eval.anova.matrix import MatrixBuilder  # noqa: E402


def _stub(record):
    """A run_cell_fn that records its call and writes a standard summary.yaml."""
    def run_cell_fn(*, config_path, run_id, output_dir, model, effort,
                    subagent_model, cases, extra_env, input_overrides=None):
        record.append({"run_id": run_id, "model": model, "effort": effort,
                       "subagent": subagent_model, "cases": list(cases),
                       "env": extra_env, "overrides": input_overrides})
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        per_case = {c: {"quality": {"value": 4, "judge_type": "numeric"}} for c in cases}
        (out / "summary.yaml").write_text(yaml.dump({"run_id": run_id, "per_case": per_case}))
        (out / "run_result.json").write_text(json.dumps({"model": model, "cost_usd": 0.1}))
    return run_cell_fn


def test_fan_out_one_run_per_cell_maps_factors_and_stamps(tmp_path):
    conds = MatrixBuilder.expand_full_factorial({"model": ["a", "b"], "effort": ["low", "high"]})
    rec = []
    runs_dir = tmp_path / "runs" / "myeval"
    produced = O.fan_out(NS(models=NS(skill=None)), "eval.yaml", conds, ["c1"],
                         replications=1, runs_dir=runs_dir, run_cell_fn=_stub(rec))

    assert len(produced) == 4 == len(rec)
    assert {(r["model"], r["effort"]) for r in rec} == {
        ("a", "low"), ("a", "high"), ("b", "low"), ("b", "high")}
    # each run carries a condition.json with its levels
    cj = json.loads((produced[0] / "condition.json").read_text())
    assert set(cj["levels"]) == {"model", "effort"}
    # AGENT_EVAL_RUNS_DIR passed to eval-run is the runs base (parent of eval dir)
    assert rec[0]["env"]["AGENT_EVAL_RUNS_DIR"] == str(runs_dir.parent)


def test_replications_produce_distinct_run_ids(tmp_path):
    conds = MatrixBuilder.expand_full_factorial({"model": ["a"]})
    rec = []
    O.fan_out(NS(models=NS(skill=None)), "eval.yaml", conds, ["c1"],
              replications=3, runs_dir=tmp_path / "r" / "e", run_cell_fn=_stub(rec))
    ids = {r["run_id"] for r in rec}
    assert len(ids) == 3  # r1/r2/r3 suffixes keep them distinct


def test_nonmodel_factor_passed_as_input_override(tmp_path):
    conds = MatrixBuilder.expand_full_factorial({"model": ["a"], "temperature": ["0.0", "1.0"]})
    rec = []
    O.fan_out(NS(models=NS(skill=None)), "eval.yaml", conds, ["c1"],
              replications=1, runs_dir=tmp_path / "r" / "e", run_cell_fn=_stub(rec))
    # temperature is not a runner flag — it reaches the runner as an input override
    assert {r["overrides"]["temperature"] for r in rec} == {"0.0", "1.0"}
    assert all("model" not in r["overrides"] for r in rec)  # model uses its own flag


def test_cell_failure_is_skipped(tmp_path):
    conds = MatrixBuilder.expand_full_factorial({"model": ["a", "b"]})

    def flaky(*, model, output_dir, cases, run_id, **_):
        if model == "a":
            raise RuntimeError("boom")
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "summary.yaml").write_text(yaml.dump({"run_id": run_id, "per_case": {}}))

    produced = O.fan_out(NS(models=NS(skill=None)), "e.yaml", conds, ["c1"],
                         replications=1, runs_dir=tmp_path / "r" / "e", run_cell_fn=flaky)
    assert len(produced) == 1  # only the "b" cell survived


def test_cell_failing_after_scoring_keeps_its_condition(tmp_path):
    """A cell that writes summary.yaml then fails (score.py exits 1 on a
    threshold breach) is still rediscovered by analyze_runs — it must be
    grouped under its full {model, effort} condition, not by model alone."""
    from analyze import load_conditions_from_runs

    conds = MatrixBuilder.expand_full_factorial({"model": ["a", "b"], "effort": ["low", "high"]})
    rec = []
    write_run = _stub(rec)

    def breaching(**kw):
        write_run(**kw)
        if kw["model"] == "a" and kw["effort"] == "high":
            raise RuntimeError("score.py failed (exit 1): regression detected")

    runs_dir = tmp_path / "r" / "e"
    produced = O.fan_out(NS(models=NS(skill=None)), "e.yaml", conds, ["c1"],
                         replications=1, runs_dir=runs_dir, run_cell_fn=breaching)
    assert len(produced) == 3 and len(rec) == 4

    failed = next(p for p in runs_dir.iterdir() if p not in produced)
    cj = json.loads((failed / "condition.json").read_text())
    assert cj["levels"] == {"model": "a", "effort": "high"}

    rows, factors, _ = load_conditions_from_runs(runs_dir, NS(reward=None))
    assert set(factors) == {"model", "effort"}
    assert all(r["effort"] in ("low", "high") for r in rows)
    assert len({r["condition_id"] for r in rows}) == 4


_EVAL_YAML = """
execution:
  mode: case
  skill: myskill
  arguments: "{prompt}"
  max_budget_usd: 2.0
runner:
  type: claude-code
dataset:
  path: dataset
  schema: "cases"
judges:
  - name: quality
    check: |
      return True, "ok"
matrix:
  factors:
    model:
      - claude-opus-4-8
      - claude-sonnet-4-6
  replications: 1
"""


def _project(tmp_path):
    for c in ("c1", "c2"):
        (tmp_path / "dataset" / c).mkdir(parents=True)
        (tmp_path / "dataset" / c / "input.yaml").write_text("prompt: hi\n")
    (tmp_path / "eval.yaml").write_text(_EVAL_YAML)
    return str(tmp_path / "eval.yaml")


def test_main_dry_run_estimates_cost_without_executing(tmp_path, monkeypatch, capsys):
    cfg = _project(tmp_path)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(O, "_run_eval_for_condition", _stub([]))

    assert O.main(["--config", cfg, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Total runs: 4" in out
    assert "$0.00" not in out  # cost is estimated, not silently zero
    assert not (tmp_path / "runs").exists()  # nothing executed


def test_main_run_then_analyze_writes_artifact(tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(O, "_run_eval_for_condition", _stub([]))

    assert O.main(["--config", cfg, "--no-report"]) == 0
    artifact = tmp_path / "runs" / "myskill" / "anova.json"
    assert artifact.exists()

    # --analyze-only re-analyses existing runs (no execution needed)
    artifact.unlink()
    assert O.main(["--config", cfg, "--analyze-only", "--no-report"]) == 0
    assert artifact.exists()


def test_correction_plumbing_config_then_cli_override(tmp_path, monkeypatch):
    """matrix.analysis.correction reaches anova.json; --correction overrides it."""
    cfg = _project(tmp_path)
    raw = yaml.safe_load(Path(cfg).read_text())
    raw["matrix"]["analysis"] = {"correction": "fdr_bh"}
    Path(cfg).write_text(yaml.dump(raw))
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(O, "_run_eval_for_condition", _stub([]))

    assert O.main(["--config", cfg, "--no-report"]) == 0
    artifact = tmp_path / "runs" / "myskill" / "anova.json"
    stats = json.loads(artifact.read_text())
    assert stats["anova"]["correction"] == "bh"  # canonicalised from fdr_bh

    # The CLI flag wins over the config for a re-analysis
    assert O.main(["--config", cfg, "--analyze-only", "--no-report",
                   "--correction", "none"]) == 0
    stats = json.loads(artifact.read_text())
    assert stats["anova"]["correction"] == "none"


def test_per_judge_plumbing_flag_and_config(tmp_path, monkeypatch):
    """per_judge is opt-in: absent by default, enabled by --per-judge or
    matrix.analysis.per_judge (either turns the fan-out on)."""
    cfg = _project(tmp_path)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(O, "_run_eval_for_condition", _stub([]))

    assert O.main(["--config", cfg, "--no-report"]) == 0
    artifact = tmp_path / "runs" / "myskill" / "anova.json"
    stats = json.loads(artifact.read_text())
    assert "per_judge" not in stats  # off by default

    # the CLI flag enables it for a re-analysis
    assert O.main(["--config", cfg, "--analyze-only", "--no-report",
                   "--per-judge"]) == 0
    stats = json.loads(artifact.read_text())
    assert stats["per_judge"]["correction"] == "bh"
    # the stub's judge scores a constant 4 -> excluded with a reason, honestly
    assert stats["per_judge"]["family_size"] == 0
    assert stats["per_judge"]["excluded"][0]["judge"] == "quality"

    # matrix.analysis.per_judge enables it without the flag
    raw = yaml.safe_load(Path(cfg).read_text())
    raw["matrix"]["analysis"] = {"per_judge": True}
    Path(cfg).write_text(yaml.dump(raw))
    assert O.main(["--config", cfg, "--analyze-only", "--no-report"]) == 0
    stats = json.loads(artifact.read_text())
    assert "per_judge" in stats


def test_execute_input_override_helpers(tmp_path):
    """The eval-run --input-override plumbing that carries non-model factors."""
    import yaml as _yaml
    import execute  # eval-run script (on sys.path via conftest)

    assert execute._parse_input_overrides(["a=1", "b=x=y", "bad", "=nope"]) == {
        "a": "1", "b": "x=y"}

    p = tmp_path / "input.yaml"
    p.write_text(_yaml.safe_dump({"prompt": "hi", "context": "none"}))
    execute._merge_input_overrides(p, {"context": "cognee", "model": "m"})
    assert _yaml.safe_load(p.read_text()) == {
        "prompt": "hi", "context": "cognee", "model": "m"}
    # missing / empty are no-ops (must not raise)
    execute._merge_input_overrides(tmp_path / "missing.yaml", {"x": "1"})
    execute._merge_input_overrides(p, {})
