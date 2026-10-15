import json
import importlib.util
import os
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_eval.engines import capevolve
from agent_eval.engines import _bridge_template as bridge_template
from agent_eval.optimize import artifact, rollout


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
        "judge_budget_usd": None,
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

def test_plugin_bootstrap_assets_are_copied_into_runtime_workspace(tmp_path):
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    (project / "scripts/bootstrap.sh").write_text("bootstrap", encoding="utf-8")
    (project / "types/rfe").mkdir(parents=True)
    (project / "types/rfe/type.yaml").write_text("name: rfe", encoding="utf-8")
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

    rollout._materialize_plugin_layout({"project_root": str(project)}, workspace)

    assert not (workspace / "scripts").is_symlink()
    assert (workspace / "scripts/bootstrap.sh").read_text() == "bootstrap"
    assert (workspace / "types/rfe/type.yaml").read_text() == "name: rfe"
    assert (workspace / ".claude/skills/rfe-create/SKILL.md").read_text() == "skill"
    assert (workspace / ".claude/agents/rfe-scorer.md").read_text() == "agent"
    assert (workspace / ".claude/settings.json").read_text() == "{}"


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
        judge_budget_usd=0.15,
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
    assert manifest["judge_budget_usd"] == 0.15
    assert manifest["apply_to"] == ".claude/skills/demo"
    assert substrate["runner"] == "local"
    assert substrate["harness_python"] == sys.executable
    assert "dataset_source: adapter" in spec
    assert "gate_k_se: 1.0" in spec
    assert "max_usd: 5" in spec
    assert "split_ids_file: split_ids.json" in spec
    # Tamper seal: omitting this leaves dataset/judge files editable by the
    # optimizer (verified against cap-evolve's own harness.py/integrity.py).
    assert "protected_paths: default" in spec
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


def test_prepare_project_reports_needs_setup_when_optimize_block_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    case = tmp_path / "cases" / "case-1"
    case.mkdir(parents=True)
    (case / "input.yaml").write_text("request: hello\n", encoding="utf-8")
    (tmp_path / "eval.yaml").write_text(
        """name: demo-eval
execution:
  mode: case
  skill: demo
runner:
  type: claude-code
dataset:
  path: cases
  schema: A request
judges:
  - name: quality
    check: |
      return True, 'ok'
""",
        encoding="utf-8",
    )

    with pytest.raises(capevolve.NeedsSetupError) as excinfo:
        capevolve.prepare_project("eval.yaml", max_usd=5)
    assert any("optimize.candidate" in item for item in excinfo.value.missing)
    assert any("reward" in item for item in excinfo.value.missing)


@pytest.mark.skipif(importlib.util.find_spec("cap_evolve") is None,
                    reason="requires the optional cap-evolve-core extra")
def test_cli_dry_run_scaffolds_checks_and_estimates(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _demo_eval_yaml(tmp_path, mode="batch")
    exit_code = capevolve.main([
        "--config", "eval.yaml", "--max-usd", "5", "--max-iterations", "1", "--dry-run",
    ])

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result["check"]["ok"]
    assert result["scaffold"]["task_count"] == 1
    assert result["estimate"]["spec_path"] == result["scaffold"]["spec_path"]


def test_launch_rejects_more_than_two_pilot_iterations(tmp_path, monkeypatch, capsys):
    """A live pilot is a 3-case smoke launch, not a real campaign — capped at
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
            "--runner-budget-usd", "1", "--judge-budget-usd", "1",
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
