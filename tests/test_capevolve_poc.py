import json
import importlib.util
import os
import shutil
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_eval.engines import capevolve
from agent_eval.engines import _bridge_template as bridge_template
from agent_eval.optimize import artifact, intake, rollout


def _init_git_repo(path: Path) -> None:
    for args in (["init", "-q"], ["config", "user.email", "test@example.com"],
                 ["config", "user.name", "Test"]):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


def _git_commit_all(path: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=path, check=True, capture_output=True)


def _current_branch(path: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path,
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _make_bridge(tmp_path, task_ids=("case-1",)):
    """A minimal Bridge for unit-testing score()/run_target() in isolation —
    no real rollout subprocess is ever invoked by these tests."""
    adapters_dir = tmp_path / "adapters"
    adapters_dir.mkdir()
    substrate = {"runner": "local", "env": None, "image": None, "harness_python": sys.executable}
    (adapters_dir / "substrate.json").write_text(json.dumps(substrate), encoding="utf-8")
    manifest = {
        "harness_root": str(tmp_path / "harness"),
        "project_root": str(tmp_path / "project"),
        "eval_config": str(tmp_path / "project" / "eval.yaml"),
        "eval_name": "demo",
        "execution_mode": "case",
        "runner_type": "claude-code",
        "target_model": "",
        "task_ids": list(task_ids),
        "task_inputs": {task_id: {"request": task_id} for task_id in task_ids},
        "apply_to": ".claude/skills/demo",
        "candidate": [{"dest": "SKILL.md", "source": str(tmp_path / "skill.md"), "is_dir": False}],
        "runner_budget_usd": None,
        "feedback": "redacted",
        "on_error": "noise",
    }
    (adapters_dir / "candidate.json").write_text(json.dumps(manifest), encoding="utf-8")
    return bridge_template.Bridge(adapters_dir / "substrate.json")


# ── agent_eval.optimize.artifact: candidate overlay (engine-agnostic) ───────

def test_candidate_overlay_detaches_project_symlink(tmp_path):
    project_skills = tmp_path / "project" / "skills"
    skill = project_skills / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("seed", encoding="utf-8")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "skills").symlink_to(project_skills, target_is_directory=True)
    candidate = tmp_path / "candidate"
    candidate_skill = candidate / "SKILL.md"
    candidate_skill.parent.mkdir()
    candidate_skill.write_text("candidate", encoding="utf-8")

    artifact.stage_candidate(
        workspace,
        candidate,
        "skills/demo",
        [{"dest": "SKILL.md", "is_dir": False}],
    )

    assert not (workspace / "skills").is_symlink()
    assert (workspace / "skills/demo/SKILL.md").read_text() == "candidate"
    assert (skill / "SKILL.md").read_text() == "seed"


def test_candidate_overlay_rejects_symlink_in_candidate(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (tmp_path / "outside.md").write_text("outside", encoding="utf-8")
    (candidate / "SKILL.md").symlink_to(tmp_path / "outside.md")

    with pytest.raises(ValueError, match="must not contain symlinks"):
        artifact.stage_candidate(
            workspace,
            candidate,
            "skills/demo",
            [{"dest": "SKILL.md", "is_dir": False}],
        )


# ── agent_eval.optimize.rollout: plugin layout + reward composition ────────

def test_runtime_resources_resolved_by_intake_are_copied_into_runtime_workspace(tmp_path):
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    (project / "scripts/bootstrap.sh").write_text("bootstrap", encoding="utf-8")
    (project / "types/rfe").mkdir(parents=True)
    (project / "types/rfe/type.yaml").write_text("name: rfe", encoding="utf-8")
    (project / "docs").mkdir()
    (project / "docs/notes.md").write_text("not a runtime resource", encoding="utf-8")
    (project / "CLAUDE.md").write_text("project context", encoding="utf-8")
    (project / ".claude/skills/rfe-create").mkdir(parents=True)
    (project / ".claude/skills/rfe-create/SKILL.md").write_text("skill", encoding="utf-8")
    (project / ".claude/agents").mkdir()
    (project / ".claude/agents/rfe-scorer.md").write_text("agent", encoding="utf-8")

    workspace = tmp_path / "workspace"
    (workspace / ".claude").mkdir(parents=True)
    (workspace / ".claude/settings.json").write_text("{}", encoding="utf-8")
    (workspace / ".claude/skills").symlink_to(
        project / ".claude/skills", target_is_directory=True
    )

    rollout._materialize_runtime_resources(
        {"project_root": str(project), "runtime_resources": ["scripts", "types", "CLAUDE.md"]},
        workspace,
    )

    assert not (workspace / "scripts").is_symlink()
    assert (workspace / "scripts/bootstrap.sh").read_text() == "bootstrap"
    assert (workspace / "types/rfe/type.yaml").read_text() == "name: rfe"
    assert not (workspace / "docs").exists()
    assert (workspace / ".claude/skills/rfe-create/SKILL.md").read_text() == "skill"
    assert (workspace / ".claude/agents/rfe-scorer.md").read_text() == "agent"
    assert (workspace / ".claude/settings.json").read_text() == "{}"


def test_runtime_resource_names_cannot_escape_the_project(tmp_path):
    with pytest.raises(ValueError, match="runtime resource"):
        rollout._materialize_runtime_resources(
            {"project_root": str(tmp_path), "runtime_resources": ["../outside"]}, tmp_path / "ws",
        )


def _demo_eval_yaml(tmp_path, *, mode="case"):
    case = tmp_path / "cases" / "case-1"
    case.mkdir(parents=True)
    (case / "input.yaml").write_text("request: hello\n", encoding="utf-8")
    skill = tmp_path / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("seed skill", encoding="utf-8")
    (tmp_path / "eval.yaml").write_text(
        f"""name: demo-eval
execution:
  mode: {mode}
  skill: demo
runner:
  type: claude-code
models:
  skill: sonnet
dataset:
  path: cases
  schema: A request
judges:
  - name: quality
    check: |
      return True, 'ok'
reward:
  judge: quality
optimize:
  candidate:
    - {{dest: SKILL.md, source: skills/demo/SKILL.md}}
  apply_to: .claude/skills/demo
""",
        encoding="utf-8",
    )


def test_score_from_summary_uses_harness_reward_and_redacts_judge_rationale(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _demo_eval_yaml(tmp_path)
    from agent_eval.config import EvalConfig
    config = EvalConfig.from_yaml("eval.yaml")
    summary = {"per_case": {"case-1": {"quality": {"value": True, "rationale": "Gold answer: secret-answer"}}}}

    error, reward, feedback = rollout.score_from_summary("case-1", summary, config, execution_error=False)

    assert error is None
    assert reward == 1.0
    assert "quality=pass" in feedback
    assert "secret-answer" not in feedback

    # Must not re-derive or second-guess the reward: it has to equal an
    # independent compose_reward() call on the same per-judge dict — the same
    # function Harbor's reward bridge and /eval-anova consume. There is no
    # other reward path anywhere in this integration.
    from agent_eval.harbor.reward import compose_reward, judge_ranges
    expected_reward, _metrics = compose_reward(
        summary["per_case"]["case-1"], judge_ranges=judge_ranges(config), reward_cfg=config.reward,
    )
    assert reward == expected_reward


def test_score_from_summary_treats_execution_error_as_missing_data(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _demo_eval_yaml(tmp_path)
    from agent_eval.config import EvalConfig
    config = EvalConfig.from_yaml("eval.yaml")

    error, reward, feedback = rollout.score_from_summary("case-1", {}, config, execution_error=True)
    assert error == "harness runner failed before producing a measurement"
    assert reward == 0.0


def test_score_from_summary_treats_missing_judge_results_as_missing_data(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _demo_eval_yaml(tmp_path)
    from agent_eval.config import EvalConfig
    config = EvalConfig.from_yaml("eval.yaml")

    error, reward, _ = rollout.score_from_summary("case-1", {"per_case": {}}, config, execution_error=False)
    assert "no judge results" in error
    assert reward == 0.0


def test_score_from_summary_treats_judge_error_as_missing_data(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _demo_eval_yaml(tmp_path)
    from agent_eval.config import EvalConfig
    config = EvalConfig.from_yaml("eval.yaml")
    summary = {"per_case": {"case-1": {"quality": {"error": "judge crashed"}}}}

    error, reward, _ = rollout.score_from_summary("case-1", summary, config, execution_error=False)
    assert "judge failed" in error
    assert reward == 0.0


def test_run_case_recovers_summary_after_score_script_nonzero_exit(tmp_path, monkeypatch):
    """score.py's own regression-detection gate (eval.yaml `thresholds:`) can
    exit nonzero even when it wrote a usable summary.yaml — e.g. one judge
    erroring trips a configured min_mean. run_case() must not let that
    nonzero exit crash the whole rollout: it has to read whatever
    summary.yaml score.py actually produced and let score_from_summary()
    apply its own graceful judge-failure handling.

    Caught live: a real pilot run against rfe-creator hit exactly this —
    two LLM judges failed for an unrelated reason (missing jinja2 in that
    venv), which tripped their eval.yaml min_mean thresholds, which made
    score.py exit 1, which (before this fix) crashed run_case() with a bare
    RuntimeError instead of the intended "a judge failed" message.
    """
    monkeypatch.chdir(tmp_path)
    runs_root = tmp_path / "runs"
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(runs_root))
    _demo_eval_yaml(tmp_path)

    manifest = {
        "harness_root": str(tmp_path),
        "project_root": str(tmp_path),
        "eval_config": str(tmp_path / "eval.yaml"),
        "eval_name": "demo-eval",
        "execution_mode": "case",
        "apply_to": ".claude/skills/demo",
        "candidate": [{"dest": "SKILL.md", "is_dir": False}],
        "task_ids": ["case-1"],
        "runner_budget_usd": None,
    }
    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    (candidate_dir / "SKILL.md").write_text("candidate", encoding="utf-8")

    state = {}

    def _fake_run_script(manifest_, name, *args, allow_nonzero=False, env_overrides=None):
        if name == "workspace.py":
            run_id = args[args.index("--run-id") + 1]
            (rollout._case_workspace(run_id) / "cases" / "case-1").mkdir(parents=True, exist_ok=True)
        elif name == "collect.py":
            run_dir = Path(args[args.index("--output") + 1])
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "run_result.json").write_text(json.dumps({"per_case": {}}), encoding="utf-8")
            state["run_dir"] = run_dir
        elif name == "score.py":
            # Usable data on disk, but a nonzero exit — exactly the
            # regression-detection-gate shape.
            (state["run_dir"] / "summary.yaml").write_text(
                "per_case:\n  case-1:\n    quality:\n      error: 'No module named foo'\n",
                encoding="utf-8",
            )
            raise RuntimeError("score.py failed (1): regressions detected")
        return None

    monkeypatch.setattr(rollout, "_run_script", _fake_run_script)

    result = rollout.run_case(manifest, "case-1", candidate_dir, seed=0)

    assert result["rollout"]["error"] == (
        "at least one harness judge failed; treat this evaluation as missing data"
    )
    # Not the old, information-free crash message this bug produced.
    assert "RuntimeError" not in (result["rollout"]["error"] or "")


def test_batch_rollout_returns_the_run_level_transcript(tmp_path, monkeypatch):
    """Caught by the live GLM smoke rollout: batch mode writes stdout.log once
    per run, not under cases/<id>/, so the proposer got an empty trace."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(tmp_path / "runs"))
    _demo_eval_yaml(tmp_path, mode="batch")
    manifest = {
        "harness_root": str(tmp_path), "project_root": str(tmp_path),
        "eval_config": str(tmp_path / "eval.yaml"), "eval_name": "demo-eval",
        "execution_mode": "batch", "apply_to": ".claude/skills/demo",
        "candidate": [{"dest": "SKILL.md", "is_dir": False}], "task_ids": ["case-1"],
        "runner_budget_usd": None, "runtime_resources": [],
    }
    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    (candidate_dir / "SKILL.md").write_text("candidate", encoding="utf-8")

    def _fake_run_script(manifest_, name, *args, allow_nonzero=False):
        if name == "workspace.py":
            rollout._case_workspace(args[args.index("--run-id") + 1]).mkdir(parents=True, exist_ok=True)
        elif name == "execute.py":
            run_dir = Path(args[args.index("--output") + 1])
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "stdout.log").write_text("batch transcript", encoding="utf-8")
            (run_dir / "run_result.json").write_text("{}", encoding="utf-8")
            (run_dir / "summary.yaml").write_text(
                "per_case:\n  case-1:\n    quality:\n      value: true\n", encoding="utf-8")

    monkeypatch.setattr(rollout, "_run_script", _fake_run_script)
    result = rollout.run_case(manifest, "case-1", candidate_dir, seed=0)
    assert result["rollout"]["output"] == "batch transcript"
    assert result["rollout"]["error"] is None


def test_batch_infra_error_is_missing_data_not_a_low_score(tmp_path, monkeypatch):
    """Qodo #2: batch run_result.json has no per_case; a timeout/budget stop is a
    top-level error_class and must not be scored as a real (low) reward."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(tmp_path / "runs"))
    _demo_eval_yaml(tmp_path, mode="batch")
    manifest = {
        "harness_root": str(tmp_path), "project_root": str(tmp_path),
        "eval_config": str(tmp_path / "eval.yaml"), "eval_name": "demo-eval",
        "execution_mode": "batch", "apply_to": ".claude/skills/demo",
        "candidate": [{"dest": "SKILL.md", "is_dir": False}], "task_ids": ["case-1"],
        "runner_budget_usd": None, "runtime_resources": [],
    }
    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    (candidate_dir / "SKILL.md").write_text("candidate", encoding="utf-8")

    def _fake_run_script(manifest_, name, *args, allow_nonzero=False):
        if name == "workspace.py":
            rollout._case_workspace(args[args.index("--run-id") + 1]).mkdir(parents=True, exist_ok=True)
        elif name == "execute.py":
            run_dir = Path(args[args.index("--output") + 1])
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "run_result.json").write_text(
                json.dumps({"exit_code": 1, "error_class": "timeout"}), encoding="utf-8")
            (run_dir / "summary.yaml").write_text(
                "per_case:\n  case-1:\n    quality:\n      value: false\n", encoding="utf-8")

    monkeypatch.setattr(rollout, "_run_script", _fake_run_script)
    result = rollout.run_case(manifest, "case-1", candidate_dir, seed=0)
    assert result["rollout"]["error"] == "harness runner failed before producing a measurement"


def test_harbor_transcript_is_confined_to_the_run(tmp_path):
    """Qodo #3: harbor_job_dir comes from run_result.json and is not trusted."""
    run_dir = tmp_path / "run"
    outside = tmp_path / "elsewhere/trial/agent"
    outside.mkdir(parents=True)
    (outside / "claude-code.txt").write_text("not ours", encoding="utf-8")
    assert rollout._harbor_transcript(str(tmp_path / "elsewhere"), run_dir) == ""
    assert rollout._harbor_transcript(str(run_dir / "harbor-jobs/../../elsewhere"), run_dir) == ""
    inside = run_dir / "harbor-jobs/job/trial/agent"
    inside.mkdir(parents=True)
    (inside / "claude-code.txt").write_text("ours", encoding="utf-8")
    assert rollout._harbor_transcript(str(run_dir / "harbor-jobs/job"), run_dir) == "ours"


def test_boolean_costs_are_not_spend(tmp_path):
    """Qodo #4: bool is an int subclass; True must not read as $1.00."""
    from agent_eval.optimize import export
    (tmp_path / "run_result.json").write_text(json.dumps({"cost_usd": True}), encoding="utf-8")
    assert export.read_usage(tmp_path, {"judge_usage": {"judge_cost_usd": True}}) == (0.0, 0, "unavailable")


def test_candidate_overlay_rejects_a_symlinked_intermediate_directory(tmp_path):
    """Qodo #14: the check must cover directories between the candidate root
    and a multi-part component, not just the component itself."""
    (tmp_path / "outside/refs").mkdir(parents=True)
    (tmp_path / "outside/refs/a.md").write_text("outside", encoding="utf-8")
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "nested").symlink_to(tmp_path / "outside", target_is_directory=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ValueError, match="must not contain symlinks"):
        artifact.stage_candidate(workspace, candidate, "skills/demo",
                                 [{"dest": "nested/refs", "is_dir": True}])


def test_local_rollout_writes_the_standard_report_and_tolerates_its_failure(tmp_path, monkeypatch):
    """Each rollout run dir gets the same report.html a plain /eval-run leaves;
    a failing report never costs the measurement."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(tmp_path / "runs"))
    _demo_eval_yaml(tmp_path, mode="batch")
    manifest = {
        "harness_root": str(tmp_path), "project_root": str(tmp_path),
        "eval_config": str(tmp_path / "eval.yaml"), "eval_name": "demo-eval",
        "execution_mode": "batch", "apply_to": ".claude/skills/demo",
        "candidate": [{"dest": "SKILL.md", "is_dir": False}], "task_ids": ["case-1"],
        "runner_budget_usd": None, "runtime_resources": [],
    }
    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    (candidate_dir / "SKILL.md").write_text("candidate", encoding="utf-8")
    calls = []

    def _fake_run_script(manifest_, name, *args, allow_nonzero=False):
        calls.append((name, args))
        if name == "workspace.py":
            rollout._case_workspace(args[args.index("--run-id") + 1]).mkdir(parents=True, exist_ok=True)
        elif name == "execute.py":
            run_dir = Path(args[args.index("--output") + 1])
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "run_result.json").write_text("{}", encoding="utf-8")
            (run_dir / "summary.yaml").write_text(
                "per_case:\n  case-1:\n    quality:\n      value: true\n", encoding="utf-8")
        elif name == "report.py":
            raise RuntimeError("report.py failed (1): template error")

    monkeypatch.setattr(rollout, "_run_script", _fake_run_script)
    result = rollout.run_case(manifest, "case-1", candidate_dir, seed=0)

    report_args = dict(calls)["report.py"]
    assert report_args[report_args.index("--run-id") + 1] == result["rollout"]["metadata"]["harness_run_id"]
    assert result["rollout"]["error"] is None
    assert result["rollout"]["metadata"]["reward"] == 1.0


# ── agent_eval.engines.capevolve: scaffolding ────────────────────────────────

def test_prepare_project_generates_adapter_seed_and_search_spec(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for case_id in ("case-1", "case-2", "case-3"):
        case = tmp_path / "cases" / case_id
        case.mkdir(parents=True)
        (case / "input.yaml").write_text(f"request: {case_id}\n", encoding="utf-8")
    skill = tmp_path / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("seed skill", encoding="utf-8")
    (tmp_path / "eval.yaml").write_text(
        """name: demo-eval
execution:
  mode: case
  skill: demo
runner:
  type: claude-code
models:
  skill: sonnet
dataset:
  path: cases
  schema: A request
judges:
  - name: quality
    check: |
      return True, 'ok'
reward:
  judge: quality
optimize:
  candidate:
    - dest: SKILL.md
      source: skills/demo/SKILL.md
  apply_to: .claude/skills/demo
  feedback: redacted
  on_error: noise
""",
        encoding="utf-8",
    )

    result = capevolve.prepare_project(
        "eval.yaml",
        max_usd=5,
        max_iterations=1,
        split_ratios=(0.5, 0.25, 0.25),
        pilot_case_ids=("case-1", "case-2", "case-3"),
        runner_budget_usd=0.5,
    )
    project = Path(result["project_dir"])
    manifest = json.loads((project / "adapters/candidate.json").read_text())
    substrate = json.loads((project / "adapters/substrate.json").read_text())
    spec = (project / "capevolve.yaml").read_text()

    assert result["task_count"] == 3
    assert (project / "seed_capability/SKILL.md").read_text() == "seed skill"
    assert manifest["task_ids"] == ["case-1", "case-2", "case-3"]
    assert manifest["task_inputs"]["case-1"] == {"request": "case-1"}
    assert manifest["execution_mode"] == "case"
    assert manifest["runner_budget_usd"] == 0.5
    assert manifest["apply_to"] == ".claude/skills/demo"
    assert manifest["base_eval_config"] == str(tmp_path / "eval.yaml")
    # The eval config already declares optimize: and reward: -> no overlay.
    assert result["overlay"] is None
    assert not intake.overlay_path(tmp_path).exists()
    assert substrate["runner"] == "local"
    assert substrate["harness_python"] == sys.executable
    assert "dataset_source: adapter" in spec
    assert "gate_k_se: 1.0" in spec
    assert "max_usd: 5" in spec
    assert "split_ids_file: split_ids.json" in spec
    # Tamper seal: omitting this leaves dataset/judge files editable by the
    # optimizer (verified against cap-evolve's own harness.py/integrity.py).
    assert "protected_paths: default" in spec
    # Absolute, or `cap-evolve run` silently falls back to the generic
    # template (cap-evolve issue #252).
    assert f"optimizer_instructions_file: {project / 'optimizer/INSTRUCTIONS.md'}" in spec
    # Never the benchmark checkout: it holds gold answers and judge prompts.
    assert "runner_repo_path: ''" in spec
    assert "memory_skill: md-files" in spec
    project_md = (project / "PROJECT.md").read_text()
    assert "NEEDED inputs resolved" in project_md
    assert "BLOCKED" not in project_md
    intake_json = json.loads((project / "intake.json").read_text())
    assert intake_json["usd"] == 0.0
    assert "Benchmark facts" in (project / "optimizer/INSTRUCTIONS.md").read_text()
    # No mlflow: block in this eval.yaml -> no observers generated.
    assert "observers" not in spec
    splits = json.loads((project / "split_ids.json").read_text())
    assert splits == {"train": ["case-1"], "val": ["case-2"], "test": ["case-3"]}
    assert (project / "adapters/adapter.py").is_file()
    assert (project / "adapters/aeh_bridge.py").read_text() == (
        capevolve._BRIDGE_TEMPLATE.read_text()
    )


def test_prepare_project_generates_observers_when_mlflow_configured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    case = tmp_path / "cases" / "case-1"
    case.mkdir(parents=True)
    (case / "input.yaml").write_text("request: hello\n", encoding="utf-8")
    skill = tmp_path / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("seed skill", encoding="utf-8")
    (tmp_path / "eval.yaml").write_text(
        """name: demo-eval
execution:
  mode: case
  skill: demo
runner:
  type: claude-code
models:
  skill: sonnet
dataset:
  path: cases
  schema: A request
judges:
  - name: quality
    check: |
      return True, 'ok'
reward:
  judge: quality
mlflow:
  experiment: my-experiment
  tracking_uri: http://127.0.0.1:5000
optimize:
  candidate:
    - {dest: SKILL.md, source: skills/demo/SKILL.md}
  apply_to: .claude/skills/demo
""",
        encoding="utf-8",
    )

    result = capevolve.prepare_project("eval.yaml", max_usd=5, max_iterations=1)
    spec = (Path(result["project_dir"]) / "capevolve.yaml").read_text()

    assert "observers" in spec
    assert "mlflow" in spec
    assert "my-experiment" in spec


def _bare_benchmark(tmp_path, *, mode="batch", entry="pipeline"):
    """A benchmark shaped like rfe-creator: an eval.yaml with no optimize:/reward:,
    an entry skill that invokes other project skills, plugin-relative scripts
    reading a types/ registry, and measurement material next to them."""
    for case_id in ("case-1", "case-2", "case-3", "case-4"):
        case = tmp_path / "eval/dataset/cases" / case_id
        case.mkdir(parents=True)
        (case / "input.yaml").write_text(f"request: {case_id}\n", encoding="utf-8")
        (case / "reference.md").write_text("gold", encoding="utf-8")
    skills = tmp_path / ".claude/skills"
    for name, body in {
        "pipeline": 'Run /create first, then Skill(skill: "review", args: "--all").\n'
                    "Run `python3 ../../../scripts/state.py`.",
        "create": "Create the item.",
        "review": "Review the item.",
        "unrelated": "Never invoked by the pipeline.",
    }.items():
        (skills / name).mkdir(parents=True)
        (skills / name / "SKILL.md").write_text(body, encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/state.py").write_text('ROOT / "types" / "rfe"\n', encoding="utf-8")
    (tmp_path / "types/rfe").mkdir(parents=True)
    (tmp_path / "types/rfe/type.yaml").write_text("name: rfe", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_state.py").write_text('open("types/rfe")', encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/guide.md").write_text("docs", encoding="utf-8")
    (tmp_path / "eval-profiles").mkdir()
    (tmp_path / "eval-profiles/fast.yaml").write_text("extends: ../eval.yaml\n", encoding="utf-8")
    eval_yaml = f"""name: bench
execution:
  mode: {mode}
  skill: {entry}
runner:
  type: claude-code
models:
  skill: sonnet
dataset:
  path: eval/dataset/cases
  schema: A request
judges:
  - name: files_exist
    check: |
      return True, 'ok'
  - name: quality
    check: |
      return 4, 'ok'
    score_range: [1, 5]
"""
    (tmp_path / "eval.yaml").write_text(eval_yaml, encoding="utf-8")
    return eval_yaml


# ── Layout independence: a plugin-resolved benchmark ─────────────────────────

def _plugin_benchmark(tmp_path):
    """Shaped like epic-creator, the opposite layout to rfe-creator: the eval
    config is eval/<name>.yaml (no root eval.yaml), the skills load as a
    plugin (runner.plugin_dirs: ["."], skills/ at the plugin root), and
    plugin-relative scripts live next to them."""
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin/plugin.json").write_text('{"name": "bench-plugin"}', encoding="utf-8")
    for name, body in {"decompose": 'Run `python3 ../../scripts/check.py`, then /review.',
                       "review": "Review the epics."}.items():
        (tmp_path / "skills" / name).mkdir(parents=True)
        (tmp_path / "skills" / name / "SKILL.md").write_text(body, encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/check.py").write_text("print('ok')", encoding="utf-8")
    for case_id in ("case-1", "case-2", "case-3", "case-4"):
        case = tmp_path / "eval/cases" / case_id
        case.mkdir(parents=True)
        (case / "input.yaml").write_text(f"strat_id: {case_id}\n", encoding="utf-8")
    (tmp_path / "eval/decompose.yaml").write_text("""name: decompose
execution:
  mode: case
  skill: decompose
  arguments: "{strat_id} --headless"
runner:
  type: claude-code
  plugin_dirs:
    - "."
models:
  skill: sonnet
dataset:
  path: cases
  schema: A strategy id
judges:
  - name: dag_valid
    check: |
      return True, 'ok'
  - name: quality
    check: |
      return 4, 'ok'
    score_range: [1, 5]
""", encoding="utf-8")


def test_intake_finds_a_plugin_resolved_benchmark_without_any_layout_assumption(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _plugin_benchmark(tmp_path)

    result = capevolve.prepare_project(max_usd=5, optimizer="mock")

    assert result["eval_config"].endswith(".capevolve/eval.optimize.yaml")
    import yaml
    layered = yaml.safe_load(intake.overlay_path(tmp_path).read_text())
    assert layered["extends"] == "../eval/decompose.yaml"
    assert layered["optimize"]["apply_to"] == "skills"
    assert [c["dest"] for c in layered["optimize"]["candidate"]] == ["decompose", "review"]
    manifest = json.loads((Path(result["project_dir"]) / "adapters/candidate.json").read_text())
    assert manifest["plugin_dir"] == str(tmp_path)
    assert manifest["skill_root"] == str(tmp_path / "skills")
    assert manifest["task_ids"] == ["case-1", "case-2", "case-3", "case-4"]
    assert manifest["runtime_resources"] == ["scripts"]


def test_plugin_candidate_runs_from_a_private_plugin_copy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _plugin_benchmark(tmp_path)
    result = capevolve.prepare_project(max_usd=5, optimizer="mock")
    manifest = json.loads((Path(result["project_dir"]) / "adapters/candidate.json").read_text())
    candidate = tmp_path / "candidate"
    for name in ("decompose", "review"):
        (candidate / name).mkdir(parents=True)
        (candidate / name / "SKILL.md").write_text(f"candidate {name}", encoding="utf-8")
    private = tmp_path / "elsewhere/run.plugin"
    private.mkdir(parents=True)

    rollout._private_plugin(manifest, candidate, private / "plugin")
    from agent_eval.config import EvalConfig
    config = EvalConfig.from_yaml(rollout._rollout_config(manifest, private, private / "plugin"))

    assert (private / "plugin/skills/decompose/SKILL.md").read_text() == "candidate decompose"
    assert (private / "plugin/.claude-plugin/plugin.json").is_file()
    assert (private / "plugin/scripts/check.py").is_file()  # ../../scripts resolves in the copy
    assert not (private / "plugin/eval").exists()           # never the dataset
    assert "candidate" not in (tmp_path / "skills/decompose/SKILL.md").read_text()
    # The private copy REPLACES the real plugin rather than loading next to it.
    assert [Path(d) for d in config.runner.plugin_dirs] == [private / "plugin"]
    assert config.eval_name() == "decompose"
    assert config.resolve_path(config.dataset.path) == tmp_path / "eval/cases"


def test_overlay_is_re_pointed_when_the_eval_config_changes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    capevolve.prepare_project(max_usd=5, optimizer="mock")
    overlay = intake.overlay_path(tmp_path)
    edited = overlay.read_text().replace("quality: 1.0", "quality: 3.0")
    overlay.write_text(edited, encoding="utf-8")
    (tmp_path / "eval-profiles/fast.yaml").write_text(
        "extends: ../eval.yaml\nmodels:\n  skill: haiku\n", encoding="utf-8")

    result = capevolve.prepare_project("eval-profiles/fast.yaml", max_usd=5, optimizer="mock")

    import yaml
    layered = yaml.safe_load(overlay.read_text())
    assert layered["extends"] == "../eval-profiles/fast.yaml"
    # Qodo #1: another config has its own judges and skills, so the blocks are
    # proposed afresh for it (and reported as defaults), never carried over.
    assert layered["reward"]["weights"]["quality"] == 1.0
    assert result["defaulted"] == ["optimize", "reward"]
    manifest = json.loads((Path(result["project_dir"]) / "adapters/candidate.json").read_text())
    assert manifest["target_model"] == "haiku"


def test_reward_naming_an_undefined_judge_is_blocked(tmp_path, monkeypatch):
    """Qodo #1: compose_reward() skips a weight with no judge value, so a stale
    judge name would silently drop out of every measurement."""
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    with open(tmp_path / "eval.yaml", "a", encoding="utf-8") as fh:
        fh.write("reward:\n  formula: weighted\n  weights: {files_exist: 1.0, rfe_quality: 1.0}\n  gate: false\n")
    with pytest.raises(intake.IntakeBlocked) as excinfo:
        capevolve.prepare_project(max_usd=5, optimizer="mock")
    assert "rfe_quality" in excinfo.value.blocked[0]["why"]


def test_intake_refuses_to_rewrite_a_live_campaign(tmp_path, monkeypatch):
    """Qodo #7: rollouts re-read candidate.json/substrate.json mid-campaign."""
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    result = capevolve.prepare_project(max_usd=5, optimizer="mock")
    project = Path(result["project_dir"])
    before = (project / "adapters/candidate.json").read_text()
    capevolve._write_campaign_manifest(project, {
        "pid": os.getpid(), "create_time": capevolve._process_create_time(os.getpid()),
        "run_dir": str(tmp_path / ".capevolve/run_x"), "stop_hint": f"kill {os.getpid()}",
    })
    with pytest.raises(RuntimeError, match="campaign is still running"):
        capevolve.prepare_project(max_usd=5, optimizer="mock", max_iterations=3)
    assert (project / "adapters/candidate.json").read_text() == before


def test_launch_stops_a_campaign_that_never_creates_a_run_dir(tmp_path, monkeypatch):
    """Qodo #12: a slow start must not leave an untracked, spending process."""
    project_dir = tmp_path / ".capevolve/project"
    project_dir.mkdir(parents=True)
    monkeypatch.setattr(capevolve.sys, "executable", "/bin/sh")
    monkeypatch.setattr(capevolve, "_RUN_DIR_POLL_TIMEOUT_S", 0.3)
    real_popen = capevolve.subprocess.Popen
    seen = {}

    def _popen(cmd, **kwargs):
        seen["proc"] = real_popen(["sleep", "30"], **kwargs)
        return seen["proc"]

    monkeypatch.setattr(capevolve.subprocess, "Popen", _popen)
    with pytest.raises(RuntimeError, match="did not create a run directory"):
        capevolve.launch_detached(project_root=tmp_path, project_dir=project_dir, spec_path="s.yaml",
                                  max_usd=1, max_iterations=1, skills_dir=None, harness_root=tmp_path)
    assert seen["proc"].wait(timeout=5) is not None
    assert capevolve.read_campaign_manifest(project_dir) is None


def test_cost_hint_is_per_case_runner_cost(tmp_path, monkeypatch):
    """Qodo #13: a run's cost_usd covers all its cases; divide before averaging."""
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    from agent_eval.config import EvalConfig
    config = EvalConfig.from_yaml("eval.yaml")
    runs = tmp_path / "eval/runs" / config.eval_name()
    (runs / "full").mkdir(parents=True)
    (runs / "full/run_result.json").write_text(json.dumps(
        {"cost_usd": 40.0, "per_case": {f"case-{i}": {} for i in range(4)}}), encoding="utf-8")
    (runs / "opt-1").mkdir()
    (runs / "opt-1/run_result.json").write_text(json.dumps({"cost_usd": 12.0}), encoding="utf-8")
    (runs / "opt-1/condition.json").write_text("{}", encoding="utf-8")
    (runs / "batch-unknown").mkdir()
    (runs / "batch-unknown/run_result.json").write_text(json.dumps({"cost_usd": 99.0}), encoding="utf-8")
    assert capevolve._calibrate_cost_from_past_runs(config, tmp_path) == 11.0  # (10 + 12) / 2


@pytest.mark.skipif(importlib.util.find_spec("cap_evolve") is None,
                    reason="requires the optional cap-evolve-core extra")
def test_cli_dry_run_intakes_a_plugin_resolved_benchmark_end_to_end(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _plugin_benchmark(tmp_path)
    exit_code = capevolve.main(["--max-usd", "5", "--optimizer", "mock", "--dry-run"])
    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0, result
    assert result["check"]["ok"]
    assert result["scaffold"]["candidate_components"] == ["decompose", "review"]


# ── Harbor substrate ─────────────────────────────────────────────────────────

def _harbor_manifest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks/session.sh").write_text("echo hi", encoding="utf-8")
    result = capevolve.prepare_project(max_usd=5, optimizer="mock")
    manifest = json.loads((Path(result["project_dir"]) / "adapters/candidate.json").read_text())
    manifest["runtime_resources"] = ["scripts", "types", "hooks"]
    candidate = tmp_path / "candidate"
    for name in ("pipeline", "create", "review"):
        (candidate / name).mkdir(parents=True)
        (candidate / name / "SKILL.md").write_text(f"candidate {name}", encoding="utf-8")
    return manifest, candidate


def test_harbor_plugin_overlays_the_candidate_on_the_project_skills(tmp_path, monkeypatch):
    manifest, candidate = _harbor_manifest(tmp_path, monkeypatch)
    plugin = tmp_path / "private/plugin"

    rollout._skills_plugin(manifest, candidate, plugin)

    assert (plugin / "skills/pipeline/SKILL.md").read_text() == "candidate pipeline"
    # Non-candidate project skills stay available to orchestrator skills.
    assert (plugin / "skills/unrelated/SKILL.md").read_text() == "Never invoked by the pipeline."
    assert "candidate" not in (tmp_path / ".claude/skills/pipeline/SKILL.md").read_text()


def test_harbor_rollout_config_loads_as_an_eval_config_overlay(tmp_path, monkeypatch):
    manifest, candidate = _harbor_manifest(tmp_path, monkeypatch)
    private = tmp_path / "elsewhere/private"
    private.mkdir(parents=True)
    plugin = private / "plugin"
    rollout._skills_plugin(manifest, candidate, plugin)

    from agent_eval.config import EvalConfig, resolve_plugin_skill_roots
    config = EvalConfig.from_yaml(rollout._rollout_config(manifest, private, plugin, rollout._harbor_files(manifest)))

    assert config.eval_name() == "pipeline"
    assert config.resolve_path(config.dataset.path) == (tmp_path / "eval/dataset/cases")
    assert [Path(d) for d in config.runner.plugin_dirs] == [plugin]
    assert resolve_plugin_skill_roots(plugin) == [(plugin / "skills").resolve()]
    shared = {f.dest: f.source for f in config.dataset.workspace.files if hasattr(f, "dest")}
    # hooks is a reserved workspace name (tool interception owns it).
    assert shared == {"scripts": str(tmp_path / "scripts"), "types": str(tmp_path / "types")}
    assert config.reward is not None


def test_execute_harbor_generates_a_scrubbed_private_package_and_reads_its_summary(tmp_path, monkeypatch):
    manifest, candidate = _harbor_manifest(tmp_path, monkeypatch)
    manifest["agent_timeout_s"] = 10800
    run_dir = tmp_path / "runs/run-1"
    calls = []

    def _fake_run_harness(manifest_, cmd, *, allow_nonzero=False):
        cmd = [str(c) for c in cmd]
        calls.append(cmd)
        if cmd[0].endswith("harbor.py"):
            package = Path(cmd[cmd.index("--out") + 1]) / "case-1"
            (package / "environment").mkdir(parents=True)
            (package / "environment/answers.yaml").write_text("interceptor guidance", encoding="utf-8")
            (package / "environment/annotations.yaml").write_text("gold", encoding="utf-8")
            state["package"] = package
        else:
            # answers.yaml is the AskUserQuestion interceptor's guidance (kept,
            # as in a plain /eval-run); annotations.yaml is the judges' key.
            assert (state["package"] / "environment/answers.yaml").is_file()
            assert not (state["package"] / "environment/annotations.yaml").exists()
            job = run_dir / "harbor-jobs/job-1/trial-1/agent"
            job.mkdir(parents=True)
            (job / "claude-code.txt").write_text("transcript", encoding="utf-8")
            (run_dir / "run_result.json").write_text(json.dumps({
                "harbor_job_dir": str(run_dir / "harbor-jobs/job-1"),
                "trial_errors": [], "infra_errors": [],
            }), encoding="utf-8")
        return None

    state = {}
    monkeypatch.setattr(rollout, "_run_harness", _fake_run_harness)
    substrate = {"runner": "harbor", "env": "podman", "image": "localhost/aeh:latest"}

    error, output = rollout._execute_harbor(manifest, substrate, "case-1", candidate, "opt-x", run_dir)

    generate, run = calls
    assert generate[generate.index("--cases") + 1] == "case-1"
    assert generate[generate.index("--image") + 1] == "localhost/aeh:latest"
    assert generate[generate.index("--agent-timeout") + 1] == "10800"
    assert run[:2] == ["-m", "agent_eval.harbor.run"]
    assert run[run.index("--env") + 1] == "podman"
    assert run[run.index("--model") + 1] == "sonnet"
    assert "--cases" not in run  # a pre-generated package refuses --cases
    assert (error, output) == (False, "transcript")
    assert not rollout._case_workspace("opt-x").exists()


def test_harbor_trial_error_is_missing_data(tmp_path, monkeypatch):
    manifest, candidate = _harbor_manifest(tmp_path, monkeypatch)
    run_dir = tmp_path / "runs/run-2"

    def _fake_run_harness(manifest_, cmd, *, allow_nonzero=False):
        if str(cmd[0]).endswith("harbor.py"):
            (Path(str(cmd[cmd.index("--out") + 1])) / "case-1/environment").mkdir(parents=True)
        else:
            run_dir.mkdir(parents=True)
            (run_dir / "run_result.json").write_text(json.dumps(
                {"trial_errors": [["case-1", "pod never Ready"]]}), encoding="utf-8")

    monkeypatch.setattr(rollout, "_run_harness", _fake_run_harness)
    substrate = {"runner": "harbor", "env": "podman", "image": "img"}
    error, _ = rollout._execute_harbor(manifest, substrate, "case-1", candidate, "opt-y", run_dir)
    assert error is True


def test_harbor_errored_judge_is_missing_data_but_a_skipped_one_is_not(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    with open(tmp_path / "eval.yaml", "a", encoding="utf-8") as fh:
        fh.write("  - name: conditional\n    check: |\n      return True, 'ok'\n"
                 "    if: annotations.kind == 'x'\n"
                 "reward:\n  formula: weighted\n  weights: {files_exist: 1.0, quality: 1.0}\n  gate: false\n")
    from agent_eval.config import EvalConfig
    config = EvalConfig.from_yaml("eval.yaml")
    skipped = {"per_case": {"case-1": {
        "files_exist": {"value": True}, "quality": {"value": 4},
        "conditional": {"value": None, "rationale": ""},
    }}}
    rollout._mark_unconditional_judge_errors(skipped, "case-1", config)
    error, reward, _ = rollout.score_from_summary("case-1", skipped, config, execution_error=False)
    assert error is None and reward > 0

    errored = {"per_case": {"case-1": {
        "files_exist": {"value": True}, "quality": {"value": None, "rationale": "ScoreRangeError"},
    }}}
    rollout._mark_unconditional_judge_errors(errored, "case-1", config)
    error, _, _ = rollout.score_from_summary("case-1", errored, config, execution_error=False)
    assert "judge failed" in error


def test_harbor_substrate_blocks_on_missing_inputs(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    monkeypatch.setattr(capevolve.importlib.util, "find_spec", lambda name: None)
    exit_code = capevolve.main(["--max-usd", "5", "--dry-run", "--runner", "harbor"])
    result = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert {b["input"] for b in result["blocked"]} >= {"harbor task image", "harbor"}


def test_prepare_project_records_the_harbor_substrate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    real_find_spec = capevolve.importlib.util.find_spec
    monkeypatch.setattr(capevolve.importlib.util, "find_spec",
                        lambda name: object() if name == "harbor" else real_find_spec(name))
    monkeypatch.setattr(capevolve.shutil, "which", lambda name: "/usr/bin/" + name)

    result = capevolve.prepare_project(max_usd=5, optimizer="mock", runner="harbor",
                                       harbor_env="podman", image="localhost/aeh:latest")

    project = Path(result["project_dir"])
    substrate = json.loads((project / "adapters/substrate.json").read_text())
    assert substrate == {"runner": "harbor", "env": "podman", "image": "localhost/aeh:latest",
                         "harness_python": sys.executable}
    assert "on Harbor (`--env podman`" in (project / "PROJECT.md").read_text()


def test_intake_mines_the_skill_chain_from_slash_commands_and_skill_tool_calls(tmp_path):
    _bare_benchmark(tmp_path)
    sources = [{"root": tmp_path / ".claude/skills", "plugin_dir": None}]
    assert [p.name for p in intake.skill_chain(sources, "pipeline")] == ["pipeline", "create", "review"]
    assert intake.skill_chain(sources, "missing") == []


def test_intake_runtime_resources_follow_code_paths_and_skip_measurement(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    from agent_eval.config import EvalConfig
    config = EvalConfig.from_yaml("eval.yaml")
    components = [{"source": str(tmp_path / ".claude/skills/pipeline")}]

    resources = intake.runtime_resources(tmp_path, config, components)

    # scripts (harness default) and types (read by a script) are carried;
    # the dataset (gold), eval profiles (judges), tests and unreferenced
    # docs never reach the agent's workspace.
    assert resources == ["scripts", "types"]


def test_intake_never_ships_a_harness_reserved_workspace_name(tmp_path, monkeypatch):
    """Caught by the first live GLM smoke rollout on rfe-creator: its settings.json
    references hooks/, intake shipped it, and the copy replaced the harness's own
    tool-interception hooks/ — every Bash call then failed its PreToolUse hook."""
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks/context.sh").write_text("echo", encoding="utf-8")
    (tmp_path / ".claude/settings.json").write_text(
        '{"hooks": {"SessionStart": [{"command": "bash hooks/context.sh"}]}}', encoding="utf-8")
    from agent_eval.config import EvalConfig
    resources = intake.runtime_resources(
        tmp_path, EvalConfig.from_yaml("eval.yaml"), [{"source": str(tmp_path / ".claude/skills/pipeline")}])
    assert "hooks" not in resources


def test_runtime_resource_copy_never_replaces_the_harness_hooks(tmp_path):
    project = tmp_path / "project"
    (project / "hooks").mkdir(parents=True)
    (project / "hooks/context.sh").write_text("project", encoding="utf-8")
    workspace = tmp_path / "workspace"
    (workspace / "hooks").mkdir(parents=True)
    (workspace / "hooks/tools.py").write_text("harness interceptor", encoding="utf-8")

    rollout._materialize_runtime_resources(
        {"project_root": str(project), "runtime_resources": ["hooks"]}, workspace)

    assert (workspace / "hooks/tools.py").read_text() == "harness interceptor"
    assert not (workspace / "hooks/context.sh").exists()


def test_prepare_project_runs_intake_on_a_bare_benchmark_without_touching_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    original = _bare_benchmark(tmp_path)

    result = capevolve.prepare_project(max_usd=5, optimizer="mock")

    # The benchmark's eval.yaml is never modified; intake layers an overlay.
    assert (tmp_path / "eval.yaml").read_text() == original
    overlay = intake.overlay_path(tmp_path)
    assert result["overlay"] == str(overlay)
    assert result["defaulted"] == ["optimize", "reward"]
    import yaml
    layered = yaml.safe_load(overlay.read_text())
    assert layered["extends"] == "../eval.yaml"
    assert layered["optimize"]["apply_to"] == ".claude/skills"
    assert [c["dest"] for c in layered["optimize"]["candidate"]] == ["pipeline", "create", "review"]
    assert layered["reward"] == {
        "formula": "weighted", "weights": {"files_exist": 1.0, "quality": 1.0}, "gate": False,
    }

    project = Path(result["project_dir"])
    manifest = json.loads((project / "adapters/candidate.json").read_text())
    assert manifest["eval_config"] == str(overlay.resolve())
    assert manifest["base_eval_config"] == str(tmp_path / "eval.yaml")
    assert manifest["eval_name"] == "pipeline"  # skill evals are named after the skill
    assert manifest["task_ids"] == ["case-1", "case-2", "case-3", "case-4"]
    assert manifest["runtime_resources"] == ["scripts", "types"]
    assert (project / "seed_capability/pipeline/SKILL.md").is_file()
    assert (project / "seed_capability/review/SKILL.md").is_file()
    assert not (project / "seed_capability/unrelated").exists()
    project_md = (project / "PROJECT.md").read_text()
    assert "every judge weighted equally" in project_md
    assert "(generated: optimize, reward)" in project_md


def test_prepare_project_reuses_an_existing_overlay(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    capevolve.prepare_project(max_usd=5, optimizer="mock")
    overlay = intake.overlay_path(tmp_path)
    edited = overlay.read_text().replace("quality: 1.0", "quality: 3.0")
    overlay.write_text(edited, encoding="utf-8")

    result = capevolve.prepare_project(max_usd=5, optimizer="mock")
    assert overlay.read_text() == edited
    assert result["defaulted"] == []

    capevolve.prepare_project(max_usd=5, optimizer="mock", reintake=True)
    assert "quality: 1.0" in overlay.read_text()


def test_blocks_declared_in_eval_yaml_win_over_an_earlier_overlay(tmp_path, monkeypatch):
    """The usual flow: optimize:/reward: are written into eval.yaml before the
    optimization runs. An overlay left by an earlier intake must never shadow
    them — it extends eval.yaml, so its blocks would otherwise override."""
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    capevolve.prepare_project(max_usd=5, optimizer="mock")
    overlay = intake.overlay_path(tmp_path)
    assert overlay.is_file()

    with open(tmp_path / "eval.yaml", "a", encoding="utf-8") as fh:
        fh.write(
            "reward:\n  judge: quality\n  normalize: true\n"
            "optimize:\n  candidate:\n    - {dest: create, source: .claude/skills/create}\n"
            "  apply_to: .claude/skills\n"
        )
    result = capevolve.prepare_project(max_usd=5, optimizer="mock")

    assert result["overlay"] is None and result["defaulted"] == []
    assert not overlay.exists()
    project = Path(result["project_dir"])
    manifest = json.loads((project / "adapters/candidate.json").read_text())
    assert manifest["eval_config"] == str(tmp_path / "eval.yaml")
    assert [c["dest"] for c in manifest["candidate"]] == ["create"]
    assert "the eval config already declares optimize: and reward:" in (project / "PROJECT.md").read_text()


def test_overlay_only_supplies_the_block_eval_yaml_still_lacks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    capevolve.prepare_project(max_usd=5, optimizer="mock")
    overlay = intake.overlay_path(tmp_path)
    # The user narrowed the proposed candidate in the overlay...
    import yaml
    layered = yaml.safe_load(overlay.read_text())
    layered["optimize"]["candidate"] = layered["optimize"]["candidate"][:1]
    overlay.write_text(yaml.safe_dump(layered), encoding="utf-8")
    # ...then declared only reward: in eval.yaml.
    with open(tmp_path / "eval.yaml", "a", encoding="utf-8") as fh:
        fh.write("reward:\n  judge: quality\n  normalize: true\n")

    result = capevolve.prepare_project(max_usd=5, optimizer="mock")

    rewritten = yaml.safe_load(overlay.read_text())
    assert "reward" not in rewritten
    assert [c["dest"] for c in rewritten["optimize"]["candidate"]] == ["pipeline"]
    assert result["defaulted"] == []


def test_prepare_project_blocks_instead_of_guessing_a_missing_entry_skill(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path, entry="does-not-exist")
    project = tmp_path / ".capevolve/project"
    project.mkdir(parents=True)
    (project / ".agent-eval-generated.json").write_text("{}")
    (project / "capevolve.yaml").write_text("stale: spec\n")

    with pytest.raises(intake.IntakeBlocked) as excinfo:
        capevolve.prepare_project(max_usd=5)

    assert excinfo.value.blocked[0]["input"] == "capability artifact"
    assert "BLOCKED: capability artifact" in (project / "PROJECT.md").read_text()
    assert json.loads((project / "intake.json").read_text())["blocked"]
    # Nothing launchable is left behind.
    assert not (project / "capevolve.yaml").exists()


def test_cli_refuses_evalhub_substrate_at_argparse(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        capevolve.main(["--max-usd", "5", "--dry-run", "--runner", "evalhub"])
    assert excinfo.value.code == 2
    assert "structurally incompatible" in capsys.readouterr().err


def test_cli_reports_blocked_intake_as_nonzero_json(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    exit_code = capevolve.main(["--max-usd", "5", "--dry-run"])
    result = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert result["blocked"][0]["input"] == "eval config"


@pytest.mark.skipif(importlib.util.find_spec("cap_evolve") is None,
                    reason="requires the optional cap-evolve-core extra")
def test_cli_dry_run_scaffolds_checks_and_estimates(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _demo_eval_yaml(tmp_path, mode="batch")
    exit_code = capevolve.main([
        "--config", "eval.yaml", "--max-usd", "5", "--max-iterations", "1",
        "--optimizer", "mock", "--dry-run",
    ])

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result["check"]["ok"]
    assert result["scaffold"]["task_count"] == 1
    assert result["estimate"]["spec_path"] == result["scaffold"]["spec_path"]


@pytest.mark.skipif(importlib.util.find_spec("cap_evolve") is None,
                    reason="requires the optional cap-evolve-core extra")
def test_cli_dry_run_intakes_a_bare_benchmark_end_to_end(tmp_path, monkeypatch, capsys):
    """The full flow on an unmodified benchmark: intake -> scaffold from
    cap-evolve's own templates -> `cap-evolve check` green -> estimate."""
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    exit_code = capevolve.main(["--max-usd", "5", "--optimizer", "mock", "--dry-run"])

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0, result
    assert result["check"]["ok"]
    project = Path(result["scaffold"]["project_dir"])
    assert result["scaffold"]["cap_evolve_templates"]
    instructions = (project / "optimizer/INSTRUCTIONS.md").read_text()
    # cap-evolve's template placeholders survive (its pipeline self-test
    # fails otherwise), followed by the benchmark facts.
    assert "{{FAILURES}}" in instructions
    assert instructions.rstrip().endswith(
        "`scripts`, `types`."
    )


def test_pilot_rejects_more_than_two_iterations(tmp_path, monkeypatch, capsys):
    """A pilot is a 3-case smoke launch, not a real campaign — capped at
    _MAX_PILOT_ITERATIONS so each extra iteration can't silently multiply real
    spend across all three cases again. This is argparse-level validation, so
    it must reject before any scaffolding/subprocess work happens."""
    monkeypatch.chdir(tmp_path)
    _demo_eval_yaml(tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        capevolve.main([
            "--config", "eval.yaml", "--max-usd", "5",
            "--max-iterations", "3", "--num-trials", "1",
            "--pilot-case-ids", "case-1", "case-1", "case-1",
            "--runner-budget-usd", "1",
        ])
    assert excinfo.value.code == 2
    assert "between 1 and 2" in capsys.readouterr().err


def test_optimize_block_redirects_search_controls(tmp_path):
    (tmp_path / "skill.md").write_text("seed", encoding="utf-8")
    config = type("Config", (), {
        "reward": object(),
        "execution": type("Execution", (), {"mode": "case", "steps": []})(),
        "resolve_skill": lambda self: "demo",
        "runner": type("Runner", (), {"type": "claude-code"})(),
        "dataset": type("Dataset", (), {"path": "cases"})(),
        "project_root": tmp_path,
    })()
    raw = {
        "reward": {"judge": "quality"},
        "optimize": {
            "candidate": [{"dest": "SKILL.md", "source": "skill.md"}],
            "apply_to": ".claude/skills/demo",
            "max_iterations": 4,
        },
    }

    with pytest.raises(ValueError, match="owned by cap-evolve"):
        capevolve.validate_optimize_block(raw, config)


# ── agent_eval.engines._bridge_template: Bridge ──────────────────────────────

def test_bridge_score_is_a_pure_read_with_raw_errored_marker(tmp_path):
    """cap-evolve never catches a score() exception (verified against
    harness.py/trials.py/loop.py/gepa.py — unlike run_target, whose exceptions
    trials.py converts into an errored Rollout). A Score returned without
    raw["errored"] is averaged in as a real reward and used in the gate. Every
    non-measurement return from score() must therefore carry the marker —
    and the success path must be a pure read of run_target()'s own output,
    no subprocess call."""
    bridge = _make_bridge(tmp_path)
    task = bridge_template.Task(id="case-1")

    # 1. run_target already recorded an infrastructure or judging failure.
    errored_rollout = bridge_template.Rollout(task_id="case-1", error="harness rollout failed")
    result = bridge.score(task, errored_rollout)
    assert result.reward == 0.0
    assert result.raw.get("errored") is True

    # 2. No harness_run_id in the rollout metadata — cap-evolve's own `check`
    # probes score() with a synthetic rollout shaped exactly like this.
    probe_rollout = bridge_template.Rollout(task_id="case-1")
    result = bridge.score(task, probe_rollout)
    assert result.reward == 0.0
    assert result.raw.get("errored") is True

    # 3. Success: score() must read reward/feedback straight out of
    # run_target()'s own rollout, not recompute or re-invoke anything.
    good_rollout = bridge_template.Rollout(
        task_id="case-1",
        metadata={"harness_run_id": "run-1", "reward": 0.82, "feedback": "quality=pass"},
    )
    result = bridge.score(task, good_rollout)
    assert result.reward == 0.82
    assert result.feedback == "quality=pass"
    assert result.raw == {}


def test_invoke_rollout_raises_for_malformed_rollout_output(tmp_path, monkeypatch):
    """Unit-test _invoke_rollout's own parsing in isolation, independent of
    run_target()'s exception handling, for the three failure shapes a rollout
    subprocess can actually produce."""
    bridge = _make_bridge(tmp_path)

    class _FakeProc:
        def __init__(self, stdout, stderr, returncode):
            self.stdout, self.stderr, self.returncode = stdout, stderr, returncode

    cases = [
        _FakeProc("", "", 0),                              # empty stdout
        _FakeProc("not json", "", 0),                       # malformed JSON
        _FakeProc(json.dumps({"ok": True}), "boom", 1),     # nonzero exit
    ]
    for fake in cases:
        monkeypatch.setattr(
            "agent_eval.engines._bridge_template.subprocess.run",
            lambda *a, _fake=fake, **k: _fake,
        )
        with pytest.raises(RuntimeError):
            bridge._invoke_rollout("case-1", tmp_path, 0)


def test_run_target_converts_invoke_rollout_failures_to_errored_rollout(tmp_path, monkeypatch):
    bridge = _make_bridge(tmp_path)
    task = bridge_template.Task(id="case-1")

    def _raise(*args, **kwargs):
        raise RuntimeError("rollout subprocess failed")
    monkeypatch.setattr(bridge, "_invoke_rollout", _raise)

    result = bridge.run_target(task, str(tmp_path), seed=0)
    assert result.error is not None
    # score() must then mark this as missing data too.
    score = bridge.score(task, result)
    assert score.reward == 0.0
    assert score.raw.get("errored") is True


def test_run_target_is_thread_safe_across_concurrent_calls(tmp_path, monkeypatch):
    """cap-evolve pools run_target() across CAPEVOLVE_WORKERS threads against
    one Adapter instance (verified against harness.py's trial pool: run_target
    is thread-pooled, score is always called serially afterward). The bridge
    mutates nothing on self per call, so concurrent calls must never cross-talk
    regardless of how many workers cap-evolve configures."""
    bridge = _make_bridge(tmp_path, task_ids=tuple(f"case-{i}" for i in range(20)))
    calls = []
    lock = threading.Lock()

    def _fake_invoke_rollout(task_id, candidate_dir, seed):
        with lock:
            calls.append((task_id, seed))
        run_id = f"ce-{task_id}-{seed}-{uuid.uuid4().hex[:6]}"
        return {"rollout": {
            "task_id": task_id, "output": "", "trace": "",
            "cost_usd": 0.0, "tokens": 0, "error": None,
            "metadata": {"harness_run_id": run_id, "reward": 0.5, "feedback": ""},
        }}

    monkeypatch.setattr(bridge, "_invoke_rollout", _fake_invoke_rollout)

    tasks = [bridge_template.Task(id=f"case-{i}") for i in range(20)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(bridge.run_target, task, str(tmp_path / f"candidate-{i % 4}"), seed=i)
            for i, task in enumerate(tasks)
        ]
        rollouts = [future.result() for future in futures]

    assert len(rollouts) == 20
    assert all(rollout_.error is None for rollout_ in rollouts)
    run_ids = {rollout_.metadata["harness_run_id"] for rollout_ in rollouts}
    assert len(run_ids) == 20  # no collisions despite a single shared bridge instance

    # Every call reached _invoke_rollout with its own task id and seed — never
    # another thread's — confirming no shared-state race inside run_target.
    assert set(calls) == {(f"case-{i}", i) for i in range(20)}


# ── agent_eval.engines.capevolve: liveness ──────────────────────────────────

def test_is_alive_true_for_the_current_process():
    pid = os.getpid()
    create_time = capevolve._process_create_time(pid)
    assert capevolve._is_alive(pid, create_time) is True


def test_is_alive_false_once_a_process_has_exited():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert capevolve._is_alive(proc.pid, "anything") is False


def test_is_alive_false_when_create_time_does_not_match():
    # Same pid as the live current process, but a marker that cannot be
    # right — this is the pid-reuse defense: a stale manifest pointing at a
    # pid the OS has since recycled for an unrelated process must not read
    # as "still our campaign."
    pid = os.getpid()
    assert capevolve._is_alive(pid, "definitely-not-the-real-create-time") is False


# ── agent_eval.engines.capevolve: campaign manifest (pidfile) ───────────────

def test_campaign_manifest_roundtrip(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    assert capevolve.read_campaign_manifest(project_dir) is None

    manifest = {"pid": 4242, "create_time": "abc", "run_dir": str(tmp_path / "run_1")}
    capevolve._write_campaign_manifest(project_dir, manifest)
    assert capevolve.read_campaign_manifest(project_dir) == manifest


# ── agent_eval.engines.capevolve: run-dir discovery ─────────────────────────

class _FakeProc:
    def __init__(self, returncode=None):
        self.returncode = returncode

    def poll(self):
        return self.returncode


def test_wait_for_new_run_dir_picks_the_one_new_directory(tmp_path):
    runs_root = tmp_path / ".capevolve"
    runs_root.mkdir()
    (runs_root / "run_old").mkdir()
    before = {p.name for p in runs_root.glob("run_*")}

    def _create_new_dir():
        (runs_root / "run_new").mkdir()

    threading.Timer(0.05, _create_new_dir).start()
    result = capevolve._wait_for_new_run_dir(
        runs_root, before, _FakeProc(), tmp_path / "log", timeout_s=2.0, interval_s=0.02,
    )
    assert result == runs_root / "run_new"


def test_wait_for_new_run_dir_raises_if_process_exits_first(tmp_path):
    runs_root = tmp_path / ".capevolve"
    runs_root.mkdir()
    with pytest.raises(RuntimeError, match="exited immediately"):
        capevolve._wait_for_new_run_dir(
            runs_root, set(), _FakeProc(returncode=1), tmp_path / "log",
            timeout_s=2.0, interval_s=0.02,
        )


def test_wait_for_new_run_dir_raises_on_timeout(tmp_path):
    runs_root = tmp_path / ".capevolve"
    runs_root.mkdir()
    with pytest.raises(RuntimeError, match="did not create a run directory"):
        capevolve._wait_for_new_run_dir(
            runs_root, set(), _FakeProc(), tmp_path / "log", timeout_s=0.1, interval_s=0.02,
        )


def test_wait_for_new_run_dir_raises_if_ambiguous(tmp_path):
    runs_root = tmp_path / ".capevolve"
    runs_root.mkdir()
    (runs_root / "run_a").mkdir()
    (runs_root / "run_b").mkdir()
    with pytest.raises(RuntimeError, match="ambiguous"):
        capevolve._wait_for_new_run_dir(
            runs_root, set(), _FakeProc(), tmp_path / "log", timeout_s=2.0, interval_s=0.02,
        )


# ── agent_eval.engines.capevolve: read_status ───────────────────────────────

def test_read_status_not_launched(tmp_path):
    assert capevolve.read_status(tmp_path) == {"status": "not_launched"}


def test_read_status_running_with_iteration_breakdown(tmp_path):
    run_dir = tmp_path / "run_1"
    run_dir.mkdir()
    (run_dir / "state.json").write_text(json.dumps({
        "best_id": "seed", "budget": {"max_usd": 5}, "spent": {"iterations": 3, "usd": 0.4},
    }), encoding="utf-8")
    (run_dir / "events.jsonl").write_text(
        json.dumps({"kind": "baseline"}) + "\n"
        + json.dumps({"kind": "step", "candidate": "cand_1", "accept": True, "val": 0.6}) + "\n"
        + json.dumps({"kind": "step_indecisive", "candidate": "cand_2"}) + "\n",
        encoding="utf-8",
    )
    # A line in history.jsonl means accepted, a line in rejected.jsonl means
    # rejected — verified against cap-evolve's memory.py; neither file carries
    # a verdict field, membership IS the verdict.
    (run_dir / "history.jsonl").write_text(json.dumps({"candidate_id": "cand_1"}) + "\n", encoding="utf-8")
    (run_dir / "rejected.jsonl").write_text(
        json.dumps({"candidate_id": "cand_0", "reason": "no gain"}) + "\n", encoding="utf-8",
    )
    capevolve._write_campaign_manifest(tmp_path, {
        "pid": os.getpid(), "create_time": capevolve._process_create_time(os.getpid()),
        "run_dir": str(run_dir), "log_file": str(tmp_path / "campaign.log"),
    })

    status = capevolve.read_status(tmp_path)
    assert status["status"] == "running"
    assert status["alive"] is True
    assert status["best_id"] == "seed"
    assert status["spent"] == {"iterations": 3, "usd": 0.4}
    assert status["latest_event"]["candidate"] == "cand_2"
    assert status["iterations"] == {"accepted": 1, "rejected": 1, "indecisive": 1}
    assert status["final"] is None


def test_read_status_finished(tmp_path):
    run_dir = tmp_path / "run_1"
    run_dir.mkdir()
    final = {"best_id": "cand_3", "test_delta": 0.08}
    (run_dir / "final.json").write_text(json.dumps(final), encoding="utf-8")
    capevolve._write_campaign_manifest(tmp_path, {
        "pid": os.getpid(), "create_time": capevolve._process_create_time(os.getpid()),
        "run_dir": str(run_dir), "log_file": str(tmp_path / "campaign.log"),
    })

    status = capevolve.read_status(tmp_path)
    assert status["status"] == "finished"
    assert status["final"] == final


def test_read_status_stopped_when_process_is_dead_and_unfinished(tmp_path):
    run_dir = tmp_path / "run_1"
    run_dir.mkdir()
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    capevolve._write_campaign_manifest(tmp_path, {
        "pid": proc.pid, "create_time": "stale",
        "run_dir": str(run_dir), "log_file": str(tmp_path / "campaign.log"),
    })

    status = capevolve.read_status(tmp_path)
    assert status["status"] == "stopped"
    assert status["alive"] is False


# ── agent_eval.engines.capevolve: apply_winner ──────────────────────────────

def _apply_fixture(tmp_path, *, winning_skill_content="candidate skill"):
    """A tmp git repo (the "project") with one committed SKILL.md, plus a
    finished campaign's run_dir with a winning candidate snapshot and a
    frozen train/val/test split."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    _init_git_repo(project_root)
    skill_path = project_root / "skills" / "demo" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("original skill", encoding="utf-8")
    _git_commit_all(project_root, "initial")

    run_dir = tmp_path / ".capevolve" / "run_20260101_000000"
    run_dir.mkdir(parents=True)
    (run_dir / "final.json").write_text(
        json.dumps({"best_id": "cand_1", "test_delta": 0.12}), encoding="utf-8"
    )
    (run_dir / "splits.json").write_text(json.dumps({
        "train": ["case-1", "case-2"], "val": ["case-3"], "test": ["case-4"],
    }), encoding="utf-8")
    candidate_dir = run_dir / "candidates" / "cand_1"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "SKILL.md").write_text(winning_skill_content, encoding="utf-8")

    harness_manifest = {
        "candidate": [{"dest": "SKILL.md", "source": str(skill_path), "is_dir": False}],
    }
    campaign_manifest = {"run_dir": str(run_dir)}
    return project_root, harness_manifest, campaign_manifest, skill_path


def test_apply_winner_requires_final_json(tmp_path):
    project_root, harness_manifest, campaign_manifest, _ = _apply_fixture(tmp_path)
    (Path(campaign_manifest["run_dir"]) / "final.json").unlink()

    with pytest.raises(RuntimeError, match="not finished"):
        capevolve.apply_winner(
            project_root=project_root,
            harness_manifest=harness_manifest, campaign_manifest=campaign_manifest,
        )


def test_apply_winner_requires_clean_working_tree(tmp_path):
    project_root, harness_manifest, campaign_manifest, skill_path = _apply_fixture(tmp_path)
    skill_path.write_text("uncommitted local edit", encoding="utf-8")

    with pytest.raises(RuntimeError, match="not clean"):
        capevolve.apply_winner(
            project_root=project_root,
            harness_manifest=harness_manifest, campaign_manifest=campaign_manifest,
        )
    # The guard must fire before anything is touched.
    assert skill_path.read_text(encoding="utf-8") == "uncommitted local edit"
    assert _current_branch(project_root) in ("main", "master")


def test_apply_winner_creates_branch_and_restores_original_branch(tmp_path):
    project_root, harness_manifest, campaign_manifest, skill_path = _apply_fixture(
        tmp_path, winning_skill_content="improved skill"
    )
    original_branch = _current_branch(project_root)

    result = capevolve.apply_winner(
        project_root=project_root,
        harness_manifest=harness_manifest, campaign_manifest=campaign_manifest,
    )

    assert result["branch"] == "capevolve/run_20260101_000000"
    assert result["best_id"] == "cand_1"
    assert result["test_delta"] == 0.12
    assert str(skill_path) in result["changed_files"]
    # 2 train + 1 val of 4 total cases were seen during the search.
    assert "3 of those 4 cases were seen during the search" in result["caveat"]
    assert result["suggested_next_step"].startswith("/eval-run")

    # The user is back on their original branch, tree clean, original content.
    assert _current_branch(project_root) == original_branch
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=project_root,
        capture_output=True, text=True, check=True,
    ).stdout.strip() == ""
    assert skill_path.read_text(encoding="utf-8") == "original skill"

    # The winning content landed on the new branch's commit.
    shown = subprocess.run(
        ["git", "show", f"{result['branch']}:skills/demo/SKILL.md"], cwd=project_root,
        capture_output=True, text=True, check=True,
    ).stdout
    assert shown == "improved skill"


def test_apply_winner_restores_clean_tree_after_a_mid_apply_failure(tmp_path):
    project_root, harness_manifest, campaign_manifest, skill_path = _apply_fixture(tmp_path)
    original_branch = _current_branch(project_root)
    # A second, missing component — the copy loop will fail partway through.
    harness_manifest["candidate"].append(
        {"dest": "references/missing.md", "source": str(project_root / "skills/demo/references/missing.md"),
         "is_dir": False}
    )

    with pytest.raises(RuntimeError, match="missing component"):
        capevolve.apply_winner(
            project_root=project_root,
            harness_manifest=harness_manifest, campaign_manifest=campaign_manifest,
        )

    assert _current_branch(project_root) == original_branch
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=project_root,
        capture_output=True, text=True, check=True,
    ).stdout.strip() == ""
    assert skill_path.read_text(encoding="utf-8") == "original skill"


def _apply_fixture_in_checkout(tmp_path):
    """The real layout (found by the live GLM pilot): the campaign's run dir is
    untracked under the benchmark checkout's own .capevolve/, which the
    benchmark does not gitignore."""
    project_root, harness_manifest, _, skill_path = _apply_fixture(tmp_path, winning_skill_content="improved skill")
    run_dir = project_root / ".capevolve" / "run_20260101_000000"
    shutil.copytree(tmp_path / ".capevolve" / "run_20260101_000000", run_dir)
    return project_root, harness_manifest, {"run_dir": str(run_dir)}, skill_path, run_dir


def test_apply_winner_ignores_and_never_commits_the_untracked_capevolve_dir(tmp_path):
    project_root, harness_manifest, campaign_manifest, skill_path, run_dir = _apply_fixture_in_checkout(tmp_path)

    result = capevolve.apply_winner(
        project_root=project_root, harness_manifest=harness_manifest, campaign_manifest=campaign_manifest,
    )

    committed = subprocess.run(
        ["git", "show", "--name-only", "--format=", result["branch"]], cwd=project_root,
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert committed == ["skills/demo/SKILL.md"]
    assert (run_dir / "final.json").is_file()


def test_apply_winner_rollback_never_deletes_the_campaign_state(tmp_path):
    project_root, harness_manifest, campaign_manifest, skill_path, run_dir = _apply_fixture_in_checkout(tmp_path)
    harness_manifest["candidate"].append(
        {"dest": "references/missing.md", "source": str(project_root / "skills/demo/references/missing.md"),
         "is_dir": False}
    )

    with pytest.raises(RuntimeError, match="missing component"):
        capevolve.apply_winner(
            project_root=project_root, harness_manifest=harness_manifest, campaign_manifest=campaign_manifest,
        )

    assert (run_dir / "candidates" / "cand_1" / "SKILL.md").is_file()
    assert skill_path.read_text(encoding="utf-8") == "original skill"


def test_apply_winner_refuses_when_the_seed_won(tmp_path):
    project_root, harness_manifest, campaign_manifest, skill_path, run_dir = _apply_fixture_in_checkout(tmp_path)
    (run_dir / "final.json").write_text(json.dumps({"best_id": "seed", "test_delta": 0.0}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="nothing to apply"):
        capevolve.apply_winner(
            project_root=project_root, harness_manifest=harness_manifest, campaign_manifest=campaign_manifest,
        )
    assert _current_branch(project_root) in ("main", "master")


@pytest.mark.parametrize("forged", ["../../escape", "/etc", "cand_1/../../x"])
def test_apply_winner_rejects_a_forged_winner_id(tmp_path, forged):
    """Qodo #6: best_id comes from final.json and must name a candidates/ entry."""
    project_root, harness_manifest, campaign_manifest, skill_path, run_dir = _apply_fixture_in_checkout(tmp_path)
    (run_dir / "final.json").write_text(json.dumps({"best_id": forged}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid winner id"):
        capevolve.apply_winner(project_root=project_root, harness_manifest=harness_manifest,
                               campaign_manifest=campaign_manifest)
    assert skill_path.read_text(encoding="utf-8") == "original skill"


@pytest.mark.parametrize("where", ["outside", "repo_root", "git_dir"])
def test_apply_winner_refuses_destinations_git_cannot_restore(tmp_path, where):
    """Qodo #9/#11: validated before anything is touched — a plugin outside the
    repository, the repository root itself, or .git are never written."""
    project_root, harness_manifest, campaign_manifest, skill_path, run_dir = _apply_fixture_in_checkout(tmp_path)
    outside = tmp_path / "plugin/skills/demo"
    outside.mkdir(parents=True)
    (outside / "SKILL.md").write_text("plugin original", encoding="utf-8")
    dest = {"outside": outside, "repo_root": project_root, "git_dir": project_root / ".git"}[where]
    (run_dir / "candidates/cand_1/demo").mkdir()
    (run_dir / "candidates/cand_1/demo/SKILL.md").write_text("winner", encoding="utf-8")
    harness_manifest["candidate"] = [{"dest": "demo", "source": str(dest), "is_dir": True}]

    with pytest.raises(RuntimeError, match="outside the repository"):
        capevolve.apply_winner(project_root=project_root, harness_manifest=harness_manifest,
                               campaign_manifest=campaign_manifest)
    assert (outside / "SKILL.md").read_text(encoding="utf-8") == "plugin original"
    assert (project_root / ".git" / "HEAD").is_file()
    assert _current_branch(project_root) in ("main", "master")


def test_apply_winner_keeps_ignored_files_and_drops_only_tracked_ones(tmp_path):
    """Qodo #5: a directory component is replaced file-by-file, so ignored or
    untracked files inside it survive; tracked files the winner removed go."""
    project_root, harness_manifest, campaign_manifest, skill_path, run_dir = _apply_fixture_in_checkout(tmp_path)
    skill_dir = skill_path.parent
    (skill_dir / "old.md").write_text("tracked, dropped by the winner", encoding="utf-8")
    (project_root / ".gitignore").write_text("*.local\n", encoding="utf-8")
    _git_commit_all(project_root, "add old.md and ignore rule")
    (skill_dir / "notes.local").write_text("ignored local data", encoding="utf-8")
    winner = run_dir / "candidates/cand_1/demo"
    winner.mkdir()
    (winner / "SKILL.md").write_text("winner skill", encoding="utf-8")
    harness_manifest["candidate"] = [{"dest": "demo", "source": str(skill_dir), "is_dir": True}]

    result = capevolve.apply_winner(project_root=project_root, harness_manifest=harness_manifest,
                                    campaign_manifest=campaign_manifest)

    files = subprocess.run(["git", "ls-tree", "-r", "--name-only", result["branch"], "--", "skills/demo"],
                           cwd=project_root, capture_output=True, text=True, check=True).stdout.split()
    assert files == ["skills/demo/SKILL.md"]
    assert (skill_dir / "notes.local").read_text(encoding="utf-8") == "ignored local data"


def test_apply_winner_returns_to_a_detached_head(tmp_path):
    """Qodo #15: from a detached HEAD, --apply returns to the same commit."""
    project_root, harness_manifest, campaign_manifest, skill_path, run_dir = _apply_fixture_in_checkout(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project_root, capture_output=True,
                         text=True, check=True).stdout.strip()
    subprocess.run(["git", "checkout", "-q", "--detach", sha], cwd=project_root, check=True)

    capevolve.apply_winner(project_root=project_root, harness_manifest=harness_manifest,
                           campaign_manifest=campaign_manifest)

    assert _current_branch(project_root) == "HEAD"
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=project_root, capture_output=True,
                          text=True, check=True).stdout.strip() == sha
