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
from agent_eval.optimize import artifact, export, intake, rollout

# cap-evolve's real templates/project, when a local install carries one
# (an editable install, or $CAPEVOLVE_SKILLS_DIR next to a checkout).
_LOCAL_CAPEVOLVE_TEMPLATES = capevolve._cap_evolve_templates(os.environ.get("CAPEVOLVE_SKILLS_DIR"))
# The real implementations, for the tests that exercise them directly.
_REAL_FETCH_TREE = capevolve._fetch_tree
_REAL_REQUIRE_CAP_EVOLVE = capevolve.require_cap_evolve


@pytest.fixture(autouse=True)
def _offline_cap_evolve(tmp_path_factory, monkeypatch):
    """No test reaches the network or the user's plugin data dir: the managed
    skills tree is a local stub, and the install check is a no-op except in
    the tests that exercise it directly (CI installs .[all,test], which does
    not include the cap-evolve-core extra)."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path_factory.mktemp("plugin-data")))
    monkeypatch.delenv("CAPEVOLVE_SKILLS_DIR", raising=False)

    def stub_fetch(sha, dest):
        (dest / "skills" / "_registry").mkdir(parents=True)
        (dest / "skills" / "_registry" / "manifest.json").write_text("{}", encoding="utf-8")
        if _LOCAL_CAPEVOLVE_TEMPLATES is not None:
            shutil.copytree(_LOCAL_CAPEVOLVE_TEMPLATES, dest / "templates" / "project")
        (dest / capevolve._SHA_STAMP).write_text(sha + "\n", encoding="utf-8")

    monkeypatch.setattr(capevolve, "_fetch_tree", stub_fetch)
    monkeypatch.setattr(capevolve, "require_cap_evolve", lambda: None)


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
    substrate = {"runner": "local", "env": None, "image": None}
    (adapters_dir / "substrate.json").write_text(json.dumps(substrate), encoding="utf-8")
    manifest = {
        "harness_root": str(tmp_path / "harness"),
        "harness_python": sys.executable,
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

def test_snapshot_candidate_refuses_to_delete_a_protected_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    with pytest.raises(ValueError, match="protected directory"):
        artifact.snapshot_candidate([], tmp_path)


def test_snapshot_candidate_skips_symlinks_instead_of_refusing_the_skill(tmp_path):
    """The repo's own convention is a symlink inside every skill.

    CLAUDE.md mandates ``skills/<skill>/scripts/agent_eval -> ../../../agent_eval``
    so skill scripts can import the package. Hard-erroring on it meant the
    harness's own skills could not be optimization candidates at all. Skip the
    link, copy everything else, and report what was left behind — a snapshot
    travels into rollout workspaces and containers, where a link out of the
    tree would dangle anyway.
    """
    skill = tmp_path / "skills" / "demo"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("seed", encoding="utf-8")
    (skill / "scripts" / "run.py").write_text("print(1)", encoding="utf-8")
    (tmp_path / "agent_eval").mkdir()
    (skill / "scripts" / "agent_eval").symlink_to(tmp_path / "agent_eval", target_is_directory=True)

    destination = tmp_path / "seed_capability"
    skipped = artifact.snapshot_candidate(
        [{"dest": "demo", "source": str(skill), "is_dir": True}], destination)

    assert (destination / "demo/SKILL.md").read_text() == "seed"
    assert (destination / "demo/scripts/run.py").is_file()
    assert not (destination / "demo/scripts/agent_eval").exists()
    assert skipped == ["demo/scripts/agent_eval"]


def test_snapshot_candidate_still_refuses_a_symlinked_source_root(tmp_path):
    """Incidental plumbing is skipped; an explicit candidate source aimed at a
    link is a config mistake, and snapshotting nothing would be worse."""
    real = tmp_path / "real"
    real.mkdir()
    (real / "SKILL.md").write_text("seed", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="must not be a symlink"):
        artifact.snapshot_candidate(
            [{"dest": "demo", "source": str(link), "is_dir": True}], tmp_path / "seed")


def test_plumbing_links_follow_the_runtime_resource_rule(tmp_path, monkeypatch):
    """Which of a skill's symlinks come back as content in a rollout: the
    same rule runtime_resources applies to everything else entering the
    agent's view. Inside the project and outside measurement material —
    yes; the dataset, outside the project, dangling — no, with a warning."""
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    (tmp_path / "agent_eval").mkdir()
    (tmp_path / "agent_eval/__init__.py").write_text("", encoding="utf-8")
    skill = tmp_path / ".claude/skills/pipeline"
    (skill / "scripts").mkdir()
    (skill / "scripts/agent_eval").symlink_to("../../../../agent_eval")
    (skill / "gold").symlink_to("../../../eval/dataset")
    (skill / "outside").symlink_to(tmp_path.parent)
    (skill / "gone").symlink_to("nowhere")
    (skill / "loop").symlink_to("loop")
    (skill / "scripts/repo").symlink_to("../../../..")   # the project root itself
    from agent_eval.config import EvalConfig
    config = EvalConfig.from_yaml("eval.yaml")
    # A candidate of just SKILL.md: the links are the skill directory's, and
    # the skill still needs them to run, so they are keyed on apply_to.
    components = [{"dest": "SKILL.md", "source": str(skill / "SKILL.md"), "is_dir": False}]

    links, warnings = intake.plumbing_links(tmp_path, config, components, [".claude/skills/pipeline"])

    assert links == [{"path": ".claude/skills/pipeline/scripts/agent_eval",
                      "target": str((tmp_path / "agent_eval").resolve())}]
    dropped = {w.split(" ")[2] for w in warnings}
    assert dropped == {".claude/skills/pipeline/" + name
                       for name in ("gold", "outside", "gone", "loop", "scripts/repo")}
    assert any("pipeline/gold" in w and "measurement material" in w for w in warnings)
    assert any("scripts/repo" in w and "project root" in w for w in warnings)


def test_plumbing_keeps_shared_code_under_the_skill_tree_but_not_a_seed_home(tmp_path, monkeypatch):
    """The exclusion is by path, not top-level name: `.claude/skills/_shared`
    is exactly the kind of target a skill links to, and a normal /eval-run
    resolves it (the workspace's .claude/skills is a link to the project). A
    target inside a candidate component's own seed home is different: the
    candidate replaced that directory, so a link back to the project's copy
    would hand the agent the seed next to the live candidate."""
    monkeypatch.chdir(tmp_path)
    _plugin_benchmark(tmp_path)
    (tmp_path / "skills/_shared").mkdir()
    (tmp_path / "skills/_shared/lib.py").write_text("X = 1", encoding="utf-8")
    (tmp_path / "skills/decompose/scripts").mkdir()
    (tmp_path / "skills/decompose/scripts/shared").symlink_to("../../_shared")
    (tmp_path / "skills/review/scripts").mkdir()
    (tmp_path / "skills/review/scripts/peer").symlink_to("../../decompose/scripts")   # into a seed home
    from agent_eval.config import EvalConfig
    config = EvalConfig.from_yaml("eval/decompose.yaml")
    components = [{"dest": "decompose", "source": str(tmp_path / "skills/decompose"), "is_dir": True},
                  {"dest": "review", "source": str(tmp_path / "skills/review"), "is_dir": True}]

    links, warnings = intake.plumbing_links(tmp_path, config, components, ["skills"])

    assert [l["path"] for l in links] == ["skills/decompose/scripts/shared"]
    assert links[0]["target"] == str((tmp_path / "skills/_shared").resolve())
    assert any("skills/review/scripts/peer" in w and "seed skill home" in w for w in warnings)
    # And the dataset / eval config dir stay out, by path.
    (tmp_path / "skills/decompose/scripts/cases").symlink_to("../../../eval/cases")
    (tmp_path / "skills/decompose/scripts/cfg").symlink_to("../../../eval")
    links, warnings = intake.plumbing_links(tmp_path, config, components, ["skills"])
    assert [l["path"] for l in links] == ["skills/decompose/scripts/shared"]
    assert sum("measurement material" in w for w in warnings) >= 2


def test_plumbing_protects_judge_material_and_run_records_but_not_the_local_skill_tree(tmp_path, monkeypatch):
    """Three edges of the by-path rule. The judge material a config names
    (prompt_file, context) resolves against the project root, as score.py
    resolves it, and must stay out; so must the default eval/runs even when
    AGENT_EVAL_RUNS_DIR points elsewhere (earlier ordinary runs keep
    review.yaml and judge rationale there). `.claude/` itself is NOT
    measurement material: the project-local skill tree lives in it."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(tmp_path / "elsewhere-runs"))
    _bare_benchmark(tmp_path)
    (tmp_path / "eval/prompts").mkdir()
    (tmp_path / "eval/prompts/quality.md").write_text("Expected answer: 42", encoding="utf-8")
    (tmp_path / "rubrics").mkdir()
    (tmp_path / "rubrics/ctx.md").write_text("gold-ish", encoding="utf-8")
    eval_yaml = tmp_path / "eval.yaml"
    eval_yaml.write_text(eval_yaml.read_text() + """  - name: rubric
    prompt_file: eval/prompts/quality.md
    context: [rubrics/ctx.md]
    feedback_type: bool
""", encoding="utf-8")
    (tmp_path / "eval/runs/bench/r1").mkdir(parents=True)
    (tmp_path / "eval/runs/bench/r1/review.yaml").write_text("label: gold", encoding="utf-8")
    skills = tmp_path / ".claude/skills"
    (skills / "_shared").mkdir()
    (skills / "_shared/lib.py").write_text("X = 1", encoding="utf-8")
    skill = skills / "pipeline"
    (skill / "scripts").mkdir()
    (skill / "scripts/shared").symlink_to("../../_shared")
    (skill / "scripts/prompts").symlink_to("../../../../eval/prompts")
    (skill / "scripts/ctx").symlink_to("../../../../rubrics")
    (skill / "scripts/history").symlink_to("../../../../eval/runs")
    from agent_eval.config import EvalConfig
    config = EvalConfig.from_yaml("eval.yaml")
    components = [{"dest": "SKILL.md", "source": str(skill / "SKILL.md"), "is_dir": False}]

    links, warnings = intake.plumbing_links(tmp_path, config, components, [".claude/skills/pipeline"])

    assert [l["path"] for l in links] == [".claude/skills/pipeline/scripts/shared"]
    dropped = {w.split(" ")[2] for w in warnings}
    assert dropped == {".claude/skills/pipeline/scripts/" + n for n in ("prompts", "ctx", "history")}


def test_copy_mode_plumbing_skips_repositories_and_caches(tmp_path):
    """Like every other copy into a rollout (_copy_tree_local, the runner's
    plugin staging): a vendored checkout linked as plumbing must not drag its
    .git history and bytecode into a tree uploaded into every trial."""
    target = tmp_path / "vendor"
    (target / ".git").mkdir(parents=True)
    (target / ".git/config").write_text("x", encoding="utf-8")
    (target / "__pycache__").mkdir()
    (target / "__pycache__/m.cpython-311.pyc").write_text("x", encoding="utf-8")
    (target / "m.py").write_text("X = 1", encoding="utf-8")
    staged = tmp_path / "staged/skills/demo/scripts"
    staged.mkdir(parents=True)
    artifact.restore_plumbing(tmp_path / "staged", "", [{"path": "skills/demo/scripts/vendor", "target": str(target)}], "copy")
    copied = staged / "vendor"
    assert (copied / "m.py").is_file()
    assert not (copied / ".git").exists() and not (copied / "__pycache__").exists()


def test_staged_candidate_runs_with_its_plumbing_restored(tmp_path):
    """Skipping scripts/agent_eval in the snapshot is only half a fix: the
    staged copy of the skill still has to *run*. Its scripts import from
    sys.path[0], their own directory, so without the plumbing back in place
    every rollout measures a skill that cannot import — a broken skill, not
    the one in the checkout. Restored as a real directory, never a link."""
    project = tmp_path / "project"
    (project / "agent_eval").mkdir(parents=True)
    (project / "agent_eval/__init__.py").write_text("VALUE = 'plumbed'\n", encoding="utf-8")
    skill = project / "skills/demo"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("seed", encoding="utf-8")
    (skill / "scripts/run.py").write_text("import agent_eval\nprint(agent_eval.VALUE)\n", encoding="utf-8")
    (skill / "scripts/agent_eval").symlink_to("../../../agent_eval")
    components = [{"dest": "demo", "source": str(skill), "is_dir": True}]
    links = [{"path": "skills/demo/scripts/agent_eval", "target": str(project / "agent_eval")}]
    seed = tmp_path / "seed"
    artifact.snapshot_candidate(components, seed)
    assert not (seed / "demo/scripts/agent_eval").exists()

    def _run_staged(workspace, mode=None):
        workspace.mkdir()
        artifact.stage_candidate(workspace, seed, "skills", components)
        if mode:
            artifact.restore_plumbing(workspace, "", links, mode)
        return subprocess.run([sys.executable, str(workspace / "skills/demo/scripts/run.py")],
                              capture_output=True, text=True, cwd=tmp_path)

    # Without the plumbing: the script cannot run. This is what a rollout measured.
    assert _run_staged(tmp_path / "ws-bare").returncode != 0

    # Case workspace: a link, as a normal /eval-run workspace has (it symlinks
    # .claude/skills to the project). Also what agent_eval._bootstrap needs —
    # it finds the harness venv relative to its own REAL path.
    proc = _run_staged(tmp_path / "ws", "symlink")
    staged = tmp_path / "ws/skills/demo/scripts/agent_eval"
    assert staged.is_symlink() and staged.resolve() == (project / "agent_eval").resolve()
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "plumbed"

    # A plugin (re-staged by the runner with symlinks=False) or a container
    # upload: a real copy, never a link out of the tree.
    proc = _run_staged(tmp_path / "ws-plugin", "copy")
    staged = tmp_path / "ws-plugin/skills/demo/scripts/agent_eval"
    assert staged.is_dir() and not staged.is_symlink()
    assert proc.returncode == 0 and proc.stdout.strip() == "plumbed"

    # Only the mirrored prefix is this tree's business.
    other = tmp_path / "ws-other"
    other.mkdir()
    assert artifact.restore_plumbing(other, "elsewhere", links, "copy") == []

    # The candidate wins on collision: a proposer that put real content at
    # the link's path made an edit, and the edit is what gets measured.
    (seed / "demo/scripts/agent_eval").mkdir()
    (seed / "demo/scripts/agent_eval/__init__.py").write_text("VALUE = 'mine'\n", encoding="utf-8")
    proc = _run_staged(tmp_path / "ws-collide", "symlink")
    assert proc.returncode == 0 and proc.stdout.strip() == "mine"


def test_a_skill_md_only_candidate_still_gets_its_plumbing(tmp_path):
    """The documented shape — `candidate: [{dest: SKILL.md, ...}]` with
    apply_to the skill directory. The skill directory reaches the workspace
    through _materialize_runtime_resources, whose copy drops symlinks; the
    overlay then writes only SKILL.md. The plumbing has to come back all the
    same, or the rollout measures a skill whose scripts cannot import."""
    project = tmp_path / "project"
    (project / "agent_eval").mkdir(parents=True)
    (project / "agent_eval/__init__.py").write_text("VALUE = 'plumbed'\n", encoding="utf-8")
    skill = project / ".claude/skills/demo"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("seed", encoding="utf-8")
    (skill / "scripts/run.py").write_text("import agent_eval\nprint(agent_eval.VALUE)\n", encoding="utf-8")
    (skill / "scripts/agent_eval").symlink_to("../../../../agent_eval")
    components = [{"dest": "SKILL.md", "source": str(skill / "SKILL.md"), "is_dir": False}]
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "SKILL.md").write_text("candidate", encoding="utf-8")
    links = [{"path": ".claude/skills/demo/scripts/agent_eval", "target": str(project / "agent_eval")}]

    workspace = tmp_path / "ws"
    workspace.mkdir()
    rollout._materialize_runtime_resources({"project_root": str(project), "runtime_resources": []}, workspace)
    assert not (workspace / ".claude/skills/demo/scripts/agent_eval").exists()   # the copy dropped it
    artifact.stage_candidate(workspace, candidate, ".claude/skills/demo", components)
    artifact.restore_plumbing(workspace, "", links, "symlink")

    assert (workspace / ".claude/skills/demo/SKILL.md").read_text() == "candidate"
    proc = subprocess.run([sys.executable, str(workspace / ".claude/skills/demo/scripts/run.py")],
                          capture_output=True, text=True, cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "plumbed"


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


def test_a_gold_free_judges_rationale_reaches_the_proposer_bounded(tmp_path, monkeypatch):
    """astefanutti's feedback question: outcome-only feedback gives the
    proposer no gradient on rubric judges. The middle option he proposed is
    implemented here — per-judge opt-in (feedback_rationale: true), the eval
    author's assertion that this judge's rationale is gold-free. The excerpt
    is whitespace-collapsed and capped, so even a mis-marked judge leaks a
    bounded fragment rather than the whole answer key."""
    monkeypatch.chdir(tmp_path)
    _demo_eval_yaml(tmp_path)
    body = open("eval.yaml").read().replace(
        "  - name: quality\n", "  - name: quality\n    feedback_rationale: true\n", 1)
    open("eval.yaml", "w").write(body)
    from agent_eval.config import EvalConfig
    config = EvalConfig.from_yaml("eval.yaml")
    assert [j.name for j in config.judges if j.feedback_rationale] == ["quality"]

    long_rationale = "word " * 200  # 1000 chars once collapsed
    summary = {"per_case": {"case-1": {
        "quality": {"value": False, "rationale": long_rationale},
        "files_exist": {"value": True, "rationale": "Gold answer: secret-answer"},
    }}}
    error, reward, feedback = rollout.score_from_summary("case-1", summary, config, execution_error=False)

    assert error is None
    # Opted-in judge: rationale present, whitespace-collapsed, capped at 400.
    assert "quality_rationale=" in feedback
    excerpt = feedback.split("quality_rationale=")[1].split(",")[0]
    assert len(excerpt) == 400
    assert "  " not in excerpt
    # Not opted in: outcome only — rationale never reaches the proposer.
    assert "files_exist=pass" in feedback
    assert "files_exist_rationale" not in feedback
    assert "secret-answer" not in feedback


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


def test_batch_mode_rollouts_also_scrub_gold_from_the_workspace(tmp_path, monkeypatch):
    """astefanutti #5: the gold scrub ran only inside the case-mode branch;
    execution.mode: batch is a supported config and its workspace got no
    guard. The builders never copy annotations.yaml today — this pins the
    guard for the day one starts staging more of the case dir."""
    monkeypatch.chdir(tmp_path)
    runs_root = tmp_path / "runs"
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(runs_root))
    _demo_eval_yaml(tmp_path)

    manifest = {
        "harness_root": str(tmp_path),
        "project_root": str(tmp_path),
        "eval_config": str(tmp_path / "eval.yaml"),
        "eval_name": "demo-eval",
        "execution_mode": "batch",
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
            workspace = rollout._case_workspace(run_id)
            workspace.mkdir(parents=True, exist_ok=True)
            state["workspace"] = workspace
            # A stray gold file in the batch workspace — nothing should
            # carry it to the agent under test.
            (workspace / "annotations.yaml").write_text("gold answers", encoding="utf-8")
        elif name == "collect.py":
            run_dir = Path(args[args.index("--output") + 1])
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "run_result.json").write_text(json.dumps({"per_case": {}}), encoding="utf-8")
            state["run_dir"] = run_dir
        elif name == "score.py":
            (state["run_dir"] / "summary.yaml").write_text(
                "per_case:\n  case-1:\n    quality:\n      value: 1\n", encoding="utf-8",
            )
        return None

    monkeypatch.setattr(rollout, "_run_script", _fake_run_script)

    rollout.run_case(manifest, "case-1", candidate_dir, seed=0)

    assert not (state["workspace"] / "annotations.yaml").exists()


def test_batch_rollout_finds_the_run_level_transcript_but_withholds_it(tmp_path, monkeypatch):
    """Two halves of one contract.

    The lookup: batch mode writes stdout.log once per run, not under
    cases/<id>/, and _execute_local must still find it (caught by the live GLM
    smoke rollout, which came back with an empty one).

    The withholding: it does not travel onward in the Rollout. A search engine
    persists output/trace into the run record the proposer reads each
    iteration, and a target-agent transcript quotes the case input and
    whatever the agent read — which is the exact content redact.py,
    feedback_rationale and the out-of-tree manifest all exist to keep away
    from it. The generated INSTRUCTIONS.md promises transcripts are
    unavailable; this is the assertion behind that promise.
    """
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
    seen = {}
    real_execute_local = rollout._execute_local

    def _spy(*args, **kwargs):
        result = real_execute_local(*args, **kwargs)
        seen["transcript"] = result[1]
        return result

    monkeypatch.setattr(rollout, "_execute_local", _spy)
    result = rollout.run_case(manifest, "case-1", candidate_dir, seed=0)
    assert seen["transcript"] == "batch transcript"
    assert result["rollout"]["output"] is None
    assert result["rollout"]["trace"] is None
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
    # The directory itself (--run-dir), not an id to resolve under the
    # ordinary runs root: a rollout does not live there.
    assert Path(report_args[report_args.index("--run-dir") + 1]).name == result["rollout"]["metadata"]["harness_run_id"]
    score_args = dict(calls)["score.py"]
    assert "--run-id" not in score_args and "--run-dir" in score_args
    assert result["rollout"]["error"] is None
    assert result["rollout"]["metadata"]["reward"] == 1.0


def test_run_script_times_out_instead_of_hanging_forever(tmp_path, monkeypatch):
    """The local substrate's subprocess calls had no cap at all: a hung step
    (network stall, runaway agent loop) blocked the whole campaign forever,
    since max_usd never gets the chance to trigger if nothing returns."""
    script = tmp_path / "skills" / "eval-run" / "scripts" / "execute.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    manifest = {"harness_root": str(tmp_path), "project_root": str(tmp_path), "agent_timeout_s": 10}

    def _fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"), output=b"", stderr=b"stuck on tool call")

    monkeypatch.setattr(rollout.subprocess, "run", _fake_run)

    with pytest.raises(RuntimeError, match="timed out after 310s"):
        rollout._run_script(manifest, "execute.py", "--output", str(tmp_path))


def test_run_harness_times_out_instead_of_hanging_forever(tmp_path, monkeypatch):
    """The Harbor substrate's own subprocess.run (task generation, then
    agent_eval.harbor.run) had no cap either, unlike the per-agent
    --agent-timeout it passes INTO harbor.py — a hang in generation or
    container setup, outside any agent call, still blocked the campaign
    forever."""
    manifest = {"project_root": str(tmp_path), "agent_timeout_s": 10}

    def _fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"), output=b"", stderr=b"stuck on container setup")

    monkeypatch.setattr(rollout.subprocess, "run", _fake_run)

    with pytest.raises(RuntimeError, match="timed out after 910s"):
        rollout._run_harness(manifest, ["agent_eval.harbor.run"])


# ── agent_eval.engines.capevolve: scaffolding ────────────────────────────────

@pytest.mark.parametrize("bad_max_usd", [float("nan"), float("inf"), float("-inf")])
def test_prepare_project_rejects_non_finite_max_usd(tmp_path, monkeypatch, bad_max_usd):
    """nan compares False against everything (`<= 0` never rejects it) and
    inf passes a bare positivity check, so either would otherwise reach the
    generated spec and the launch command uncaught."""
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    with pytest.raises(ValueError, match="finite"):
        capevolve.prepare_project(max_usd=bad_max_usd)


def test_prepare_project_rejects_non_finite_split_ratios(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    with pytest.raises(ValueError, match="finite"):
        capevolve.prepare_project(max_usd=5, split_ratios=(float("nan"), 0.25, 0.25))


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
    manifest = json.loads((project.parent / "harness/candidate.json").read_text())
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
    # The eval config's path must not leak into PROJECT.md: it can hold judge
    # prompts/rubrics with gold content, and PROJECT.md lives inside the
    # directory the optimizer reads and edits every iteration.
    assert manifest["base_eval_config"] not in (project / "PROJECT.md").read_text()
    # The eval config already declares optimize: and reward: -> no overlay.
    assert result["overlay"] is None
    assert not intake.overlay_path(tmp_path).exists()
    assert substrate["runner"] == "local"
    # The venv's own interpreter when one exists (it must be able to import
    # cap_evolve, which sys.executable is not guaranteed to), else a safe
    # fallback to sys.executable (no venv to speak of). Lives in the manifest,
    # not substrate.json: it embeds harness_root, and substrate.json is
    # optimizer-visible.
    assert manifest["harness_python"] == capevolve._venv_python(
        Path(capevolve.__file__).resolve().parents[2]
    )
    assert "dataset_source: adapter" in spec
    assert "gate_k_se: 1.0" in spec
    assert "max_usd: 5" in spec
    assert "split_ids_file: split_ids.json" in spec
    # Tamper seal: omitting this leaves dataset/judge files editable by the
    # optimizer (verified against cap-evolve's own harness.py/integrity.py).
    assert "protected_paths: default" in spec
    # Project-relative (cap-evolve resolves it against the project dir —
    # specfile.resolve_instructions_file at the pin), never absolute:
    # capevolve.yaml is optimizer-visible and an absolute path would
    # disclose the checkout root.
    assert "optimizer_instructions_file: optimizer/INSTRUCTIONS.md" in spec
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
    # The observer backend ships in capevolve-telemetry, a distribution apart
    # from cap-evolve-core. Writing the observer without it is how a campaign
    # reaches its end against an empty experiment with nothing said, so the
    # gap is reported at scaffold time rather than discovered afterwards.
    expected = importlib.util.find_spec("capevolve_telemetry") is None
    assert any("capevolve_telemetry is not importable" in w
               for w in result["warnings"]) is expected


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
    manifest = json.loads((Path(result["project_dir"]).parent / "harness/candidate.json").read_text())
    assert manifest["plugin_dir"] == str(tmp_path)
    assert manifest["skill_root"] == str(tmp_path / "skills")
    assert manifest["task_ids"] == ["case-1", "case-2", "case-3", "case-4"]
    assert manifest["runtime_resources"] == ["scripts"]


def test_plugin_candidate_runs_from_a_private_plugin_copy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _plugin_benchmark(tmp_path)
    result = capevolve.prepare_project(max_usd=5, optimizer="mock")
    manifest = json.loads((Path(result["project_dir"]).parent / "harness/candidate.json").read_text())
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
    assert manifest["other_plugin_dirs"] == []
    assert config.eval_name() == "decompose"
    assert config.resolve_path(config.dataset.path) == tmp_path / "eval/cases"


def test_plumbing_links_travel_from_intake_to_the_private_plugin(tmp_path, monkeypatch):
    """The whole chain, not just its ends: prepare_project records the links
    in the harness manifest, _private_plugin hands them to stage_candidate,
    and the staged skill has its plumbing back — as a link, pointing at the
    project's real package."""
    monkeypatch.chdir(tmp_path)
    _plugin_benchmark(tmp_path)
    (tmp_path / "agent_eval").mkdir()
    (tmp_path / "agent_eval/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "skills/decompose/scripts").mkdir()
    (tmp_path / "skills/decompose/scripts/agent_eval").symlink_to("../../../agent_eval")

    result = capevolve.prepare_project(max_usd=5, optimizer="mock")
    manifest = json.loads((Path(result["project_dir"]).parent / "harness/candidate.json").read_text())
    assert manifest["plumbing_links"] == [
        {"path": "skills/decompose/scripts/agent_eval", "target": str((tmp_path / "agent_eval").resolve())}]
    seed = Path(result["project_dir"]) / "seed_capability"
    assert (seed / "decompose/scripts").is_dir()
    assert not (seed / "decompose/scripts/agent_eval").exists()

    # A candidate is an edit of the seed snapshot — scripts/ and all, minus
    # the link the snapshot never carried — not a bare SKILL.md.
    candidate = tmp_path / "candidate"
    shutil.copytree(seed, candidate)
    (candidate / "decompose/SKILL.md").write_text("candidate decompose", encoding="utf-8")
    private = tmp_path / "elsewhere/run.plugin"
    private.mkdir(parents=True)
    rollout._private_plugin(manifest, candidate, private / "plugin")

    # A copy, not a link: the runner re-stages every plugin into the
    # workspace with symlinks=False (claude_code.stage_plugin_dir), which
    # would drop a link to the checkout — and materializes an in-plugin one
    # anyway, so content is what a normal run of this layout gets.
    restored = private / "plugin/skills/decompose/scripts/agent_eval"
    assert restored.is_dir() and not restored.is_symlink()
    assert (restored / "__init__.py").is_file()
    assert (private / "plugin/skills/decompose/SKILL.md").read_text() == "candidate decompose"
    # And it survives the runner's own staging step unchanged.
    from agent_eval.agent.claude_code import stage_plugin_dir
    ws = tmp_path / "agent-ws"
    ws.mkdir()
    staged = stage_plugin_dir(private / "plugin", ws)
    assert (staged / "skills/decompose/scripts/agent_eval/__init__.py").is_file()


def test_sibling_skills_in_the_private_plugin_get_their_plumbing_too(tmp_path, monkeypatch):
    """apply_to naming ONE skill of a plugin: the private plugin still carries
    every sibling (the entry skill invokes them), copied without links. Their
    plumbing has to come back as well, or `/review` dies on its first import
    inside every rollout while nothing warns."""
    monkeypatch.chdir(tmp_path)
    _plugin_benchmark(tmp_path)
    (tmp_path / "agent_eval").mkdir()
    (tmp_path / "agent_eval/__init__.py").write_text("", encoding="utf-8")
    for name in ("decompose", "review"):
        (tmp_path / "skills" / name / "scripts").mkdir()
        (tmp_path / "skills" / name / "scripts/agent_eval").symlink_to("../../../agent_eval")
    config_path = tmp_path / "eval/decompose.yaml"
    config_path.write_text(config_path.read_text() + """optimize:
  apply_to: skills/decompose
  candidate:
    - {dest: SKILL.md, source: skills/decompose/SKILL.md}
""", encoding="utf-8")

    result = capevolve.prepare_project(max_usd=5, optimizer="mock")
    manifest = json.loads((Path(result["project_dir"]).parent / "harness/candidate.json").read_text())
    assert [link["path"] for link in manifest["plumbing_links"]] == [
        "skills/decompose/scripts/agent_eval", "skills/review/scripts/agent_eval"]

    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "SKILL.md").write_text("candidate", encoding="utf-8")
    private = tmp_path / "elsewhere/run.plugin"
    private.mkdir(parents=True)
    rollout._private_plugin(manifest, candidate, private / "plugin")
    for name in ("decompose", "review"):
        assert (private / "plugin/skills" / name / "scripts/agent_eval/__init__.py").is_file()


def test_sibling_plugins_survive_the_rollout_overlay(tmp_path, monkeypatch):
    """`!replace` displaces only the plugin that owns the candidate.

    The skill under test can call skills from another configured plugin. If
    the overlay replaced plugin_dirs with just the private copy, those would
    vanish for every rollout — so the campaign would be measuring a different
    skill from the one the baseline measured, and nothing would say so.
    """
    monkeypatch.chdir(tmp_path)
    _plugin_benchmark(tmp_path)
    sibling = tmp_path / "other-plugin"
    (sibling / ".claude-plugin").mkdir(parents=True)
    (sibling / ".claude-plugin/plugin.json").write_text('{"name": "sibling"}', encoding="utf-8")
    (sibling / "skills/helper").mkdir(parents=True)
    (sibling / "skills/helper/SKILL.md").write_text("Helper.", encoding="utf-8")
    # A plugin that exports no skills at all — commands only. Skill discovery
    # never sees it (resolve_plugin_skill_roots fails fast on a missing
    # skills/), but the runner loads its commands all the same, so it has to
    # come back too. The list must come from runner.plugin_dirs, not from
    # which plugins happen to have skills.
    commands_only = tmp_path / "cmd-plugin"
    (commands_only / ".claude-plugin").mkdir(parents=True)
    (commands_only / ".claude-plugin/plugin.json").write_text('{"name": "cmds"}', encoding="utf-8")
    (commands_only / "commands").mkdir()
    (commands_only / "commands/hello.md").write_text("Say hello.", encoding="utf-8")
    config_path = tmp_path / "eval/decompose.yaml"
    config_path.write_text(
        config_path.read_text().replace(
            '    - "."\n', '    - "."\n    - "other-plugin"\n    - "cmd-plugin"\n'),
        encoding="utf-8")

    result = capevolve.prepare_project(max_usd=5, optimizer="mock")
    manifest = json.loads((Path(result["project_dir"]).parent / "harness/candidate.json").read_text())
    assert manifest["other_plugin_dirs"] == [str(sibling), str(commands_only)]

    private = tmp_path / "elsewhere/run.plugin"
    private.mkdir(parents=True)
    from agent_eval.config import EvalConfig
    config = EvalConfig.from_yaml(rollout._rollout_config(manifest, private, private / "plugin"))
    assert [Path(d) for d in config.runner.plugin_dirs] == [private / "plugin", sibling, commands_only]


def test_a_symlinked_skill_directory_is_blocked_at_intake(tmp_path, monkeypatch):
    """`.claude/skills/<name> -> ../../skills/<name>` is a common layout, and a
    rollout cannot stage it: every copy on the way drops links, so the skill
    arrives empty and the baseline is broken from iteration 0. Say so at
    intake rather than let the campaign spend its budget measuring nothing."""
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    real = tmp_path / "real-skills"
    real.mkdir()
    shutil.move(str(tmp_path / ".claude/skills/pipeline"), str(real / "pipeline"))
    (tmp_path / ".claude/skills/pipeline").symlink_to("../../real-skills/pipeline")
    # A derived optimize: block never gets this far (intake does not list a
    # symlinked skill dir at all); an explicit one — the reviewer's
    # reproduction — does, and must be refused with the reason named.
    (tmp_path / "eval.yaml").write_text((tmp_path / "eval.yaml").read_text() + """optimize:
  apply_to: .claude/skills/pipeline
  candidate:
    - {dest: SKILL.md, source: real-skills/pipeline/SKILL.md}
""", encoding="utf-8")

    with pytest.raises(intake.IntakeBlocked) as excinfo:
        capevolve.prepare_project(max_usd=5, optimizer="mock")
    assert [b["input"] for b in excinfo.value.blocked] == ["skill directory"], excinfo.value.blocked
    assert "is a symlink" in excinfo.value.blocked[0]["why"]
    assert "BLOCKED" in (tmp_path / ".capevolve/project/PROJECT.md").read_text()

    # The helper itself, on the exact shapes it checks.
    from agent_eval.config import EvalConfig
    config = EvalConfig.from_yaml("eval.yaml")
    assert capevolve._symlinked_skill_dirs(
        tmp_path, config, {"apply_to": ".claude/skills/pipeline"},
        [{"dest": "SKILL.md", "source": str(real / "pipeline/SKILL.md"), "is_dir": False}],
    ) == [".claude/skills/pipeline"]
    assert capevolve._symlinked_skill_dirs(
        tmp_path, config, {"apply_to": ".claude/skills"},
        [{"dest": "create", "source": str(tmp_path / ".claude/skills/create"), "is_dir": True}],
    ) == []


def test_a_symlinked_skill_root_is_not_blocked(tmp_path, monkeypatch):
    """`.claude/skills -> ../skills` (the root itself a link) stages fine: the
    copies resolve the directory they are handed and drop only the links
    INSIDE it. Intake derives apply_to: .claude/skills for this layout on its
    own, so a block here would refuse a working project outright."""
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    shutil.move(str(tmp_path / ".claude/skills"), str(tmp_path / "skills"))
    (tmp_path / ".claude/skills").symlink_to("../skills", target_is_directory=True)
    (tmp_path / "agent_eval").mkdir()
    (tmp_path / "agent_eval/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "skills/pipeline/scripts").mkdir()
    (tmp_path / "skills/pipeline/scripts/agent_eval").symlink_to("../../../agent_eval")

    result = capevolve.prepare_project(max_usd=5, optimizer="mock")   # not blocked
    manifest = json.loads((Path(result["project_dir"]).parent / "harness/candidate.json").read_text())
    assert {link["path"] for link in manifest["plumbing_links"]} >= {".claude/skills/pipeline/scripts/agent_eval"}
    from agent_eval.config import EvalConfig
    config = EvalConfig.from_yaml(result["eval_config"])
    assert capevolve._symlinked_skill_dirs(
        tmp_path, config, {"apply_to": ".claude/skills"},
        [{"dest": "pipeline", "source": str(tmp_path / "skills/pipeline"), "is_dir": True}]) == []
    # An ENTRY of the root that is a link is still refused — that one the
    # copies do drop — whether the component is that directory or a file
    # nested beneath it (`extra/SKILL.md`: the path to the file passes
    # through the link). component["source"] is already resolved and says
    # nothing; the dest path under apply_to is what has to be walked.
    (tmp_path / "skills/extra").symlink_to("pipeline", target_is_directory=True)
    assert capevolve._symlinked_skill_dirs(
        tmp_path, config, {"apply_to": ".claude/skills"},
        [{"dest": "extra", "source": str(tmp_path / "skills/pipeline"), "is_dir": True}]) == [".claude/skills/extra"]
    assert capevolve._symlinked_skill_dirs(
        tmp_path, config, {"apply_to": ".claude/skills"},
        [{"dest": "extra/SKILL.md", "source": str(tmp_path / "skills/pipeline/SKILL.md"), "is_dir": False}],
    ) == [".claude/skills/extra"]
    assert capevolve._symlinked_skill_dirs(
        tmp_path, config, {"apply_to": ".claude/skills"},
        [{"dest": "pipeline/SKILL.md", "source": str(tmp_path / "skills/pipeline/SKILL.md"), "is_dir": False}],
    ) == []


def test_a_failed_scaffold_does_not_wedge_the_project_dir(tmp_path, monkeypatch):
    """The marker is the project dir's "we own this" claim, and it used to be
    written only at the very end. Anything that raised in between — an
    unreadable case, Ctrl-C — left the directory created and unmarked, after
    which every later attempt hit "refusing to overwrite an existing
    cap-evolve project without the marker". The only way out was deleting a
    directory the error never named.
    """
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    boom = RuntimeError("scaffold exploded")
    real_snapshot = artifact.snapshot_candidate
    monkeypatch.setattr(artifact, "snapshot_candidate",
                        lambda *a, **k: (_ for _ in ()).throw(boom))
    with pytest.raises(RuntimeError, match="scaffold exploded"):
        capevolve.prepare_project(max_usd=5, optimizer="mock")

    monkeypatch.setattr(artifact, "snapshot_candidate", real_snapshot)
    result = capevolve.prepare_project(max_usd=5, optimizer="mock")
    assert Path(result["spec_path"]).is_file()


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
    manifest = json.loads((Path(result["project_dir"]).parent / "harness/candidate.json").read_text())
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


@pytest.mark.parametrize("formula", [
    "files_exist * qulaity",   # typo inside an expression
    "qulaity",                 # typo'd bare-name formula
    "mean([files_exist, qulaity])",
])
def test_reward_expression_naming_an_undefined_judge_is_blocked(tmp_path, monkeypatch, formula):
    """astefanutti #7: only reward.weights names were validated, so a typo'd
    judge name inside an expression formula passed intake and every rollout
    composed a genuine 0.0 reward (the formula evaluator swallows the
    NameError into a 0.0, and the judges themselves all succeeded, so it is
    never recorded as missing data) — the campaign burned its whole budget
    with zero gradient and a clean-looking completion report."""
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    with open(tmp_path / "eval.yaml", "a", encoding="utf-8") as fh:
        fh.write(f'reward:\n  formula: "{formula}"\n')
    with pytest.raises(intake.IntakeBlocked) as excinfo:
        capevolve.prepare_project(max_usd=5, optimizer="mock")
    assert "qulaity" in excinfo.value.blocked[0]["why"]


def test_intake_refuses_to_rewrite_a_live_campaign(tmp_path, monkeypatch):
    """Qodo #7: rollouts re-read candidate.json/substrate.json mid-campaign."""
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    result = capevolve.prepare_project(max_usd=5, optimizer="mock")
    project = Path(result["project_dir"])
    before = (project.parent / "harness/candidate.json").read_text()
    capevolve._write_campaign_manifest(project, {
        "pid": os.getpid(), "create_time": capevolve._process_create_time(os.getpid()),
        "run_dir": str(tmp_path / ".capevolve/run_x"), "stop_hint": f"kill {os.getpid()}",
    })
    with pytest.raises(RuntimeError, match="campaign is still running"):
        capevolve.prepare_project(max_usd=5, optimizer="mock", max_iterations=3)
    assert (project.parent / "harness/candidate.json").read_text() == before


def test_candidate_manifest_lives_outside_the_optimizer_visible_tree(tmp_path, monkeypatch):
    """astefanutti #1 (blocking): adapters/candidate.json carried the
    eval-config path (judge prompts can hold gold) and every case's inputs —
    sealed test split included — inside the tree the optimizer reads and edits
    every iteration."""
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    result = capevolve.prepare_project(max_usd=5, optimizer="mock")
    project = Path(result["project_dir"])
    manifest_path = project.parent / "harness/candidate.json"
    assert manifest_path.is_file()
    assert not (project / "adapters/candidate.json").exists()
    substrate = json.loads((project / "adapters/substrate.json").read_text())
    # A RELATIVE hint, never an absolute signpost to the manifest.
    assert substrate["manifest_path"] == "../../harness/candidate.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["eval_config"]
    assert manifest["base_eval_config"]
    assert manifest["project_root"]
    assert manifest["harness_root"]
    assert manifest["task_inputs"]
    # Nothing the optimizer can read inside the project tree names the eval
    # config, the base config, the checkout root, the harness install root,
    # or carries a case input. The input needles use the JSON key/value
    # spelling — the values are serialized into any JSON file that leaks
    # them; the YAML spelling matched nothing (astefanutti confirmed the old
    # clause could not fire).
    needles = [manifest["eval_config"], manifest["base_eval_config"],
               manifest["project_root"], manifest["harness_root"]]
    for task_input in manifest["task_inputs"].values():
        for key, value in task_input.items():
            needles.append(f"{json.dumps(key)}: {json.dumps(value)}")

    def _leaks():
        return [(path, needle) for path in project.rglob("*") if path.is_file()
                for needle in needles
                if needle in path.read_text(encoding="utf-8", errors="replace")]

    assert _leaks() == []
    # The scan must actually guard: a canary carrying the manifest's own
    # serialization and the eval-config path is caught.
    canary = project / "leaky.json"
    canary.write_text(json.dumps(
        {"eval_config": manifest["eval_config"], "task_inputs": manifest["task_inputs"]}),
        encoding="utf-8")
    assert _leaks() != []
    canary.unlink()



def test_bridge_resolves_the_manifest_through_substrate_manifest_path(tmp_path, monkeypatch):
    """The bridge follows substrate.json's manifest_path instead of assuming
    the manifest is a sibling — covering all three resolution orders: the
    relative hint (what scaffolds write now — never an absolute signpost in
    the optimizer-visible tree), the AGENT_EVAL_CAPEVOLVE_MANIFEST env var
    (what the launch/check paths set; survives cap-evolve's copytree probe),
    and the legacy absolute hint from pre-fix scaffolds. The sibling
    fallback covers scaffolds older than the move."""
    adapters_dir = tmp_path / "project/adapters"
    adapters_dir.mkdir(parents=True)
    manifest_dir = tmp_path / "harness"
    manifest_dir.mkdir()
    manifest = {"project_root": str(tmp_path), "task_ids": ["case-9"],
                "task_inputs": {"case-9": {"request": "case-9"}},
                "harness_python": sys.executable}
    (manifest_dir / "candidate.json").write_text(json.dumps(manifest), encoding="utf-8")
    substrate = {"runner": "local", "env": None, "image": None,
                 "manifest_path": "../../harness/candidate.json"}
    (adapters_dir / "substrate.json").write_text(json.dumps(substrate), encoding="utf-8")
    # A stale sibling manifest must not be read.
    (adapters_dir / "candidate.json").write_text("{}", encoding="utf-8")

    # 1. Relative hint (resolved against substrate.json's own location).
    monkeypatch.delenv("AGENT_EVAL_CAPEVOLVE_MANIFEST", raising=False)
    bridge = bridge_template.Bridge(adapters_dir / "substrate.json")
    assert bridge.task_ids == ("case-9",)
    assert bridge.manifest_path == (manifest_dir / "candidate.json").resolve()

    # 2. Env var wins even when the hint points elsewhere (the copytree
    # probe's geometry: the copied project's relative hint misses, and the
    # env var carries the real location).
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "candidate.json").write_text(json.dumps(
        {"project_root": str(tmp_path), "task_ids": ["case-env"],
         "task_inputs": {"case-env": {}}}), encoding="utf-8")
    monkeypatch.setenv("AGENT_EVAL_CAPEVOLVE_MANIFEST", str(elsewhere / "candidate.json"))
    bridge = bridge_template.Bridge(adapters_dir / "substrate.json")
    assert bridge.task_ids == ("case-env",)

    # 3. Legacy absolute hint (pre-fix scaffold) still resolves.
    monkeypatch.delenv("AGENT_EVAL_CAPEVOLVE_MANIFEST")
    substrate["manifest_path"] = str(manifest_dir / "candidate.json")
    (adapters_dir / "substrate.json").write_text(json.dumps(substrate), encoding="utf-8")
    bridge = bridge_template.Bridge(adapters_dir / "substrate.json")
    assert bridge.task_ids == ("case-9",)

def test_re_intake_removes_a_pre_fix_scaffolds_stale_manifest(tmp_path, monkeypatch):
    """astefanutti's follow-up: nothing cleaned up the manifest's previous
    location, so a pre-fix scaffold's adapters/candidate.json — carrying the
    eval-config path and every task_inputs entry — survived a re-intake."""
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    result = capevolve.prepare_project(max_usd=5, optimizer="mock")
    project = Path(result["project_dir"])
    # Shape a pre-fix scaffold: manifest in adapters/, no manifest_path hint.
    stale = project / "adapters/candidate.json"
    shutil.copy2(project.parent / "harness/candidate.json", stale)
    substrate = json.loads((project / "adapters/substrate.json").read_text())
    del substrate["manifest_path"]
    (project / "adapters/substrate.json").write_text(json.dumps(substrate), encoding="utf-8")

    capevolve.prepare_project(max_usd=5, optimizer="mock")

    assert not stale.exists()
    assert (project.parent / "harness/candidate.json").is_file()


def test_run_dir_timeout_override_parses_defensively(monkeypatch, capsys):
    """astefanutti's follow-up: an unset-but-exported env var is the empty
    string in CI env blocks (float("") raises at launch), and 0/negative
    would turn the watchdog into an immediate kill — both must fall back or
    clamp instead."""
    monkeypatch.delenv("AGENT_EVAL_RUN_DIR_TIMEOUT_S", raising=False)
    assert capevolve._run_dir_timeout_s() == capevolve._RUN_DIR_POLL_TIMEOUT_S
    monkeypatch.setenv("AGENT_EVAL_RUN_DIR_TIMEOUT_S", "")
    assert capevolve._run_dir_timeout_s() == capevolve._RUN_DIR_POLL_TIMEOUT_S
    monkeypatch.setenv("AGENT_EVAL_RUN_DIR_TIMEOUT_S", "not-a-number")
    assert capevolve._run_dir_timeout_s() == capevolve._RUN_DIR_POLL_TIMEOUT_S
    assert "ignoring non-numeric" in capsys.readouterr().err
    monkeypatch.setenv("AGENT_EVAL_RUN_DIR_TIMEOUT_S", "0")
    assert capevolve._run_dir_timeout_s() == 1.0
    monkeypatch.setenv("AGENT_EVAL_RUN_DIR_TIMEOUT_S", "-5")
    assert capevolve._run_dir_timeout_s() == 1.0
    monkeypatch.setenv("AGENT_EVAL_RUN_DIR_TIMEOUT_S", "120")
    assert capevolve._run_dir_timeout_s() == 120.0


def test_outer_rollout_timeout_exceeds_the_inner_budgets_it_wraps(tmp_path):
    """astefanutti #3 (blocking): the outer rollout timeout was
    agent_timeout + 600, smaller than the inner budgets it wrapped —
    5 sequential steps of agent_timeout + 300 each on the local substrate,
    agent_timeout + 900 on Harbor. Healthy slow rollouts were SIGKILLed and
    recorded as missing data, biasing the search toward fail-fast
    candidates; on Harbor the inner cap was unreachable dead code."""
    bridge = _make_bridge(tmp_path)
    bridge.manifest["agent_timeout_s"] = 1800
    bridge.substrate["runner"] = "local"
    local = bridge._rollout_timeout_s()
    assert local > 5 * (1800 + 300)  # sum of the per-step budgets, plus margin
    bridge.substrate["runner"] = "harbor"
    harbor = bridge._rollout_timeout_s()
    # Harbor wraps TWO sequential inner budgets (task generation + run),
    # each capped at agent_timeout + 900.
    assert harbor > 2 * (1800 + 900)
    assert harbor < local            # two budgets, not five


def test_rollout_step_counts_match_the_bridges_mirrored_constants(tmp_path):
    """astefanutti: the bridge mirrors rollout.py's step counts as literals
    (it is copied into generated projects and cannot import that module),
    so neither _ROLLOUT_STEPS nor _HARBOR_CALLS is tied to the real call
    count by anything but this test — a third sequential _run_harness call,
    or a sixth _run_script, would otherwise silently reopen the timeout
    gap. Counts calls via ast rather than trusting the source comments."""
    import ast
    import inspect

    local_steps = sum(
        1 for node in ast.walk(ast.parse(inspect.getsource(rollout._execute_local_steps)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_run_script"
    )
    harbor_calls = sum(
        1 for node in ast.walk(ast.parse(inspect.getsource(rollout._execute_harbor)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_run_harness"
    )
    assert local_steps == bridge_template.Bridge._ROLLOUT_STEPS
    assert harbor_calls == bridge_template.Bridge._HARBOR_CALLS


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
    # astefanutti #10: the message must say the campaign was stopped, not
    # that it "may still be starting" — it was just SIGTERMed. And the
    # timeout monkeypatch above must be honored at call time (this test
    # would otherwise sleep the real default of 60s).
    with pytest.raises(RuntimeError, match="did not create a run directory.*stopped it to avoid an untracked"):
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
    # A past campaign's rollouts live in the parallel runs root. They are the
    # most representative sample there is (one case, one candidate), so the
    # walk has to reach them — not just the ordinary root.
    rollouts = tmp_path / "eval/runs/.optimize" / config.eval_name()
    (rollouts / "opt-1").mkdir(parents=True)
    (rollouts / "opt-1/run_result.json").write_text(json.dumps({"cost_usd": 12.0}), encoding="utf-8")
    (rollouts / "opt-1/condition.json").write_text(json.dumps(
        {"engine": "cap-evolve", "candidate_id": "cand_1", "harness_run_id": "opt-1"}), encoding="utf-8")
    (runs / "batch-unknown").mkdir()
    (runs / "batch-unknown/run_result.json").write_text(json.dumps({"cost_usd": 99.0}), encoding="utf-8")
    # An /eval-anova cell also carries a condition.json — factor levels, and
    # a whole per_case behind its cost. It must not be read as one rollout.
    (runs / "cell-a").mkdir()
    (runs / "cell-a/condition.json").write_text(json.dumps({"levels": {"model": "sonnet"}}), encoding="utf-8")
    (runs / "cell-a/run_result.json").write_text(json.dumps(
        {"cost_usd": 8.0, "per_case": {f"case-{i}": {} for i in range(4)}}), encoding="utf-8")
    assert capevolve._calibrate_cost_from_past_runs(config, tmp_path) == 8.0  # (10 + 12 + 2) / 3


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
    manifest = json.loads((Path(result["project_dir"]).parent / "harness/candidate.json").read_text())
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
    manifest = json.loads((project.parent / "harness/candidate.json").read_text())
    expected_python = capevolve._venv_python(Path(capevolve.__file__).resolve().parents[2])
    assert substrate == {"runner": "harbor", "env": "podman", "image": "localhost/aeh:latest",
                         "manifest_path": "../../harness/candidate.json"}
    assert manifest["harness_python"] == expected_python
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

    resources, warnings = intake.runtime_resources(tmp_path, config, components)

    # scripts (harness default) and types (read by a script) are carried;
    # the dataset (gold), eval profiles (judges), tests and unreferenced
    # docs never reach the agent's workspace.
    assert resources == ["scripts", "types"]
    assert warnings == []


def test_intake_runtime_resources_honors_execution_symlinks(tmp_path, monkeypatch):
    """A normal /eval-run links a project's own execution.symlinks entries on
    top of the harness defaults (workspace.py:_resolve_symlink_names, main
    #271). Before this fix, intake.runtime_resources only ever knew about the
    hardcoded DEFAULT_RUNTIME_RESOURCES tuple, so an eval config declaring
    execution.symlinks got that extra entry in a plain run but silently
    missing from every optimization rollout workspace — no error, just a
    possibly-wrong result against an incomplete workspace."""
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    (tmp_path / "project_templates").mkdir()
    (tmp_path / "project_templates" / "notes.md").write_text("extra", encoding="utf-8")
    (tmp_path / "eval.yaml").write_text(
        (tmp_path / "eval.yaml").read_text(encoding="utf-8").replace(
            "execution:\n  mode:", "execution:\n  symlinks: [project_templates]\n  mode:"
        ),
        encoding="utf-8",
    )
    from agent_eval.config import EvalConfig
    config = EvalConfig.from_yaml("eval.yaml")
    components = [{"source": str(tmp_path / ".claude/skills/pipeline")}]

    resources, warnings = intake.runtime_resources(tmp_path, config, components)

    assert "project_templates" in resources
    assert warnings == []


def test_intake_warns_once_for_a_missing_execution_symlinks_entry(tmp_path, monkeypatch):
    """A configured entry that isn't a default, and isn't found under the
    project root, is a configuration mistake (not the normal "project
    doesn't have this default" case) — it must warn, not fail silently,
    so a user sees it without re-discovering it from a bad rollout result."""
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    (tmp_path / "eval.yaml").write_text(
        (tmp_path / "eval.yaml").read_text(encoding="utf-8").replace(
            "execution:\n  mode:", "execution:\n  symlinks: [does_not_exist]\n  mode:"
        ),
        encoding="utf-8",
    )
    from agent_eval.config import EvalConfig
    config = EvalConfig.from_yaml("eval.yaml")
    components = [{"source": str(tmp_path / ".claude/skills/pipeline")}]

    resources, warnings = intake.runtime_resources(tmp_path, config, components)

    assert "does_not_exist" not in resources
    assert len(warnings) == 1
    assert "does_not_exist" in warnings[0]


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
    resources, _ = intake.runtime_resources(
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
    manifest = json.loads((project.parent / "harness/candidate.json").read_text())
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
    assert "generated: optimize, reward" in project_md
    # The overlay extends the base eval config, so its path is withheld from
    # the optimizer-visible PROJECT.md for the same reason the base's is.
    assert "not printed here either" in project_md


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
    manifest = json.loads((project.parent / "harness/candidate.json").read_text())
    assert manifest["eval_config"] == str(tmp_path / "eval.yaml")
    assert [c["dest"] for c in manifest["candidate"]] == ["create"]
    assert "the eval config already declares optimize: and reward:" in (project / "PROJECT.md").read_text()


def test_an_overlay_with_user_overrides_survives_eval_yaml_declaring_both_blocks(tmp_path, monkeypatch):
    """astefanutti #8: the overlay is the documented place for user
    overrides (_model_preflight tells users to put models:/providers: there),
    but the moment eval.yaml declared both optimize: and reward:, intake
    DELETED the overlay file — the overrides went with it, silently."""
    import yaml
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    capevolve.prepare_project(max_usd=5, optimizer="mock")
    overlay = intake.overlay_path(tmp_path)
    # The user follows the preflight's advice.
    body = yaml.safe_load(overlay.read_text(encoding="utf-8"))
    body["models"] = {"skill": "claude-haiku-5-5"}
    body["runner"] = {"type": "claude-code", "env": {"FOO": "bar"}}
    overlay.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")

    with open(tmp_path / "eval.yaml", "a", encoding="utf-8") as fh:
        fh.write(
            "reward:\n  judge: quality\n  normalize: true\n"
            "optimize:\n  candidate:\n    - {dest: create, source: .claude/skills/create}\n"
            "  apply_to: .claude/skills\n"
        )
    result = capevolve.prepare_project(max_usd=5, optimizer="mock")

    # The overlay survives, pruned to the user's own overrides; the
    # benchmark's blocks still win (they come from the base it extends).
    assert result["overlay"] == str(overlay)
    kept = yaml.safe_load(overlay.read_text(encoding="utf-8"))
    assert kept["models"] == {"skill": "claude-haiku-5-5"}
    assert kept["runner"] == {"type": "claude-code", "env": {"FOO": "bar"}}
    assert "optimize" not in kept and "reward" not in kept
    # And the effective config still sees the benchmark's declared blocks.
    from agent_eval.config import load_raw
    merged, _chain = load_raw(overlay)
    assert "optimize" in merged and "reward" in merged
    assert merged["models"] == {"skill": "claude-haiku-5-5"}


def test_an_overlay_with_user_overrides_survives_a_partial_shadows_rewrite(tmp_path, monkeypatch):
    """astefanutti #8, partial variant: adding only reward: to eval.yaml
    makes the earlier overlay's reward a shadow, so intake rewrites the
    overlay — which used to drop the user's models:/providers: overrides
    along with the shadowed block."""
    import yaml
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    capevolve.prepare_project(max_usd=5, optimizer="mock")
    overlay = intake.overlay_path(tmp_path)
    body = yaml.safe_load(overlay.read_text(encoding="utf-8"))
    body["models"] = {"skill": "claude-haiku-5-5"}
    overlay.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")

    with open(tmp_path / "eval.yaml", "a", encoding="utf-8") as fh:
        fh.write("reward:\n  judge: quality\n  normalize: true\n")
    result = capevolve.prepare_project(max_usd=5, optimizer="mock")

    assert result["overlay"] == str(overlay)
    kept = yaml.safe_load(overlay.read_text(encoding="utf-8"))
    assert kept["models"] == {"skill": "claude-haiku-5-5"}
    # The shadowed reward is gone from the overlay (the base's wins) but
    # optimize: is still proposed/kept there.
    assert "reward" not in kept
    assert "optimize" in kept
    from agent_eval.config import load_raw
    merged, _chain = load_raw(overlay)
    assert merged["reward"]["judge"] == "quality"


def test_an_overlay_hand_edit_using_the_replace_tag_round_trips(tmp_path, monkeypatch):
    """astefanutti's follow-up on the overlay prune: the overlay is exactly
    where someone would use the harness's own `!replace` list tag, and the
    prune round-tripped extras through safe_load/safe_dump — which raised a
    ConstructorError traceback on it. The tag now survives the prune."""
    import yaml as _yaml
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    capevolve.prepare_project(max_usd=5, optimizer="mock")
    overlay = intake.overlay_path(tmp_path)
    body = _yaml.safe_load(overlay.read_text(encoding="utf-8"))
    overlay.write_text(
        _yaml.safe_dump(body, sort_keys=False)
        + "models:\n  some_list: !replace\n  - a\n",
        encoding="utf-8")

    with open(tmp_path / "eval.yaml", "a", encoding="utf-8") as fh:
        fh.write("reward:\n  judge: quality\n  normalize: true\n")
    result = capevolve.prepare_project(max_usd=5, optimizer="mock")

    assert result["overlay"] == str(overlay)
    text = overlay.read_text(encoding="utf-8")
    assert "!replace" in text  # the tag survived the prune verbatim
    # ...and the pruned overlay still parses with the harness's own loader.
    from agent_eval.config import _ConfigLoader
    with open(overlay, encoding="utf-8") as fh:
        loader = _ConfigLoader(fh)
        kept = loader.get_single_data()
        loader.dispose()
    assert list(kept["models"]["some_list"]) == ["a"]


def test_an_overlay_with_an_unknown_tag_fails_loud_not_silently(tmp_path, monkeypatch):
    """The other half of the follow-up: an overlay construct the harness
    does not understand must tell the user, not traceback — and must NOT be
    silently regenerated (which would drop their hand-edits)."""
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    capevolve.prepare_project(max_usd=5, optimizer="mock")
    overlay = intake.overlay_path(tmp_path)
    body = overlay.read_text(encoding="utf-8")
    overlay.write_text(body + "mystery: !wat\n  - a\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="could not read the overlay") as excinfo:
        capevolve.prepare_project(max_usd=5, optimizer="mock")
    assert "!wat" in str(excinfo.value) and "--reintake" in str(excinfo.value)
    # The user's overlay file is untouched — not regenerated away.
    assert "!wat" in overlay.read_text(encoding="utf-8")


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
def test_cli_dashboard_flags_reach_the_generated_spec(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _demo_eval_yaml(tmp_path, mode="batch")
    exit_code = capevolve.main([
        "--config", "eval.yaml", "--max-usd", "5", "--max-iterations", "1",
        "--optimizer", "mock", "--dashboard", "off", "--dry-run",
    ])
    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result["scaffold"]["dashboard"] == "off"
    assert result["scaffold"]["dashboard_url"] is None

    exit_code = capevolve.main([
        "--config", "eval.yaml", "--max-usd", "5", "--max-iterations", "1",
        "--optimizer", "mock", "--dashboard-port", "9001", "--dry-run",
    ])
    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result["scaffold"]["dashboard"] == "auto"       # still the default
    assert result["scaffold"]["dashboard_url"] == "http://127.0.0.1:9001"

    with pytest.raises(SystemExit):
        capevolve.main([
            "--config", "eval.yaml", "--max-usd", "5", "--dashboard", "bogus", "--dry-run",
        ])


@pytest.mark.skipif(importlib.util.find_spec("cap_evolve") is None or _LOCAL_CAPEVOLVE_TEMPLATES is None,
                    reason="requires the optional cap-evolve-core extra and its templates/project")
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


def test_dry_run_reports_one_case_per_split_in_pilot_mode(tmp_path, monkeypatch):
    """astefanutti #11: scaffold.splits derived from the ratios even in
    pilot mode, so a 3-case pilot reported a 2/1/1 split (4 cases out of 3)
    while split_ids.json pinned 1/1/1 — and SKILL.md tells the agent to
    quote exactly these numbers."""
    monkeypatch.chdir(tmp_path)
    _demo_eval_yaml(tmp_path, mode="case")
    for case_id in ("case-2", "case-3"):
        case = tmp_path / "cases" / case_id
        case.mkdir(parents=True, exist_ok=True)
        (case / "input.yaml").write_text(f"request: {case_id}\n", encoding="utf-8")
    result = capevolve.prepare_project(
        max_usd=5, optimizer="mock", pilot_case_ids=("case-1", "case-2", "case-3"),
    )
    assert result["splits"] == {"train": 1, "val": 1, "test": 1}
    # astefanutti C2: with one validation case the gate cannot reject
    # anything (SE=0 → any positive delta accepted), so PROJECT.md must not
    # claim it "will correctly reject marginal gains".
    project_md = (Path(result["project_dir"]) / "PROJECT.md").read_text(encoding="utf-8")
    assert "plumbing check, not a measurement" in project_md
    assert "correctly reject marginal gains" not in project_md


def test_a_small_non_pilot_campaign_gets_the_honest_gate_note_too(tmp_path, monkeypatch):
    """astefanutti's follow-up on C2: the gate collapses whenever n < 2
    paired deltas, not only in pilot mode — a 3-case non-pilot campaign
    (2/1/0 split: one val case, and a sealed test split of ZERO cases) must
    not get the reassuring 'will correctly reject marginal gains' sentence."""
    monkeypatch.chdir(tmp_path)
    _demo_eval_yaml(tmp_path, mode="case")
    for case_id in ("case-2", "case-3"):
        case = tmp_path / "cases" / case_id
        case.mkdir(parents=True, exist_ok=True)
        (case / "input.yaml").write_text(f"request: {case_id}\n", encoding="utf-8")
    result = capevolve.prepare_project(max_usd=5, optimizer="mock")
    assert result["splits"] == {"train": 2, "val": 1, "test": 0}
    project_md = (Path(result["project_dir"]) / "PROJECT.md").read_text(encoding="utf-8")
    assert "plumbing check, not a measurement" in project_md
    assert "correctly reject marginal gains" not in project_md


def test_split_sizes_mirror_cap_evolve_s_splitter():
    """astefanutti's follow-up: the reported splits must be the split
    cap-evolve will actually build (round train, round val, remainder to
    test), not merely sum-preserving — a 3-case non-pilot run is 2/1/0, a
    sealed test split of zero cases, and the numbers must not hide that.
    10-case runs use banker's rounding on val (round(2.5) == 2)."""
    assert capevolve._split_sizes(3, (0.5, 0.25, 0.25)) == {"train": 2, "val": 1, "test": 0}
    assert capevolve._split_sizes(6, (0.5, 0.25, 0.25)) == {"train": 3, "val": 2, "test": 1}
    assert capevolve._split_sizes(10, (0.5, 0.25, 0.25)) == {"train": 5, "val": 2, "test": 3}
    assert capevolve._split_sizes(2, (0.5, 0.25, 0.25)) == {"train": 1, "val": 0, "test": 1}
    assert capevolve._split_sizes(4, (0.5, 0.25, 0.25)) == {"train": 2, "val": 1, "test": 1}


@pytest.mark.skipif(importlib.util.find_spec("cap_evolve") is None,
                    reason="cap_evolve not installed")
def test_split_sizes_match_cap_evolve_make_splits():
    """The mirror is pinned against the real splitter, not against itself:
    compare against cap_evolve.splits.make_splits at the pinned commit
    across a range of case counts."""
    from cap_evolve.splits import make_splits
    for n in range(1, 30):
        ids = [f"case-{i}" for i in range(n)]
        built = make_splits(ids, ratios=(0.5, 0.25, 0.25))
        assert capevolve._split_sizes(n, (0.5, 0.25, 0.25)) == {
            "train": len(built.train), "val": len(built.val), "test": len(built.test)}, n


def test_workers_is_wired_into_the_generated_spec(tmp_path, monkeypatch):
    """astefanutti C1: every campaign was strictly serial — the generated
    capevolve.yaml set no `workers`, so cap-evolve's rollout pool stayed at
    its default of 1, and max_usd cannot shorten wall-clock (it caps spend,
    not time). --workers now emits the spec key cap-evolve reads."""
    import yaml as _yaml
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)

    serial = capevolve.prepare_project(max_usd=5, optimizer="mock")
    spec = _yaml.safe_load((Path(serial["project_dir"]) / "capevolve.yaml").read_text())
    assert "workers" not in spec          # default keeps the serial shape
    assert serial["workers"] == 1

    wide = capevolve.prepare_project(max_usd=5, optimizer="mock", workers=4)
    spec = _yaml.safe_load((Path(wide["project_dir"]) / "capevolve.yaml").read_text())
    assert spec["workers"] == 4
    assert wide["workers"] == 4

    with pytest.raises(ValueError, match="--workers must be >= 1"):
        capevolve.prepare_project(max_usd=5, optimizer="mock", workers=0)


def test_workers_above_one_warns_on_the_harbor_substrate(tmp_path, monkeypatch):
    """astefanutti's follow-up: N workers on the Harbor substrate means N
    concurrent containers against one podman socket (or cluster) — nothing
    caps that today, so --dry-run must at least warn about it."""
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    real_find_spec = capevolve.importlib.util.find_spec
    monkeypatch.setattr(capevolve.importlib.util, "find_spec",
                        lambda name: object() if name == "harbor" else real_find_spec(name))
    monkeypatch.setattr(capevolve.shutil, "which", lambda name: "/usr/bin/" + name)

    result = capevolve.prepare_project(max_usd=5, optimizer="mock", runner="harbor",
                                       harbor_env="podman", image="localhost/aeh:latest",
                                       workers=4)
    assert any("4 concurrent" in w and "podman socket" in w for w in result["warnings"])

    serial = capevolve.prepare_project(max_usd=5, optimizer="mock", runner="harbor",
                                       harbor_env="podman", image="localhost/aeh:latest")
    assert not any("concurrent" in w for w in serial["warnings"])


def test_dashboard_defaults_to_auto_and_is_wired_into_the_generated_spec(tmp_path, monkeypatch):
    """The live dashboard cap-evolve launches at the top of `run` is on by
    default (--dashboard auto) so a user gets it without having to ask."""
    import yaml as _yaml
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)

    result = capevolve.prepare_project(max_usd=5, optimizer="mock")
    assert result["dashboard"] == "auto"
    assert result["dashboard_url"] == "http://127.0.0.1:7878"
    spec = _yaml.safe_load((Path(result["project_dir"]) / "capevolve.yaml").read_text())
    assert spec["dashboard"] == "auto"
    assert spec["dashboard_port"] == 7878

    off = capevolve.prepare_project(max_usd=5, optimizer="mock", dashboard="off")
    assert off["dashboard_url"] is None
    spec = _yaml.safe_load((Path(off["project_dir"]) / "capevolve.yaml").read_text())
    assert spec["dashboard"] == "off"

    custom_port = capevolve.prepare_project(max_usd=5, optimizer="mock", dashboard_port=9001)
    assert custom_port["dashboard_url"] == "http://127.0.0.1:9001"
    spec = _yaml.safe_load((Path(custom_port["project_dir"]) / "capevolve.yaml").read_text())
    assert spec["dashboard_port"] == 9001

    with pytest.raises(ValueError, match="--dashboard must be one of"):
        capevolve.prepare_project(max_usd=5, optimizer="mock", dashboard="bogus")


def test_dashboard_available_reports_whether_the_backend_package_is_installed(tmp_path, monkeypatch):
    """--dry-run should tell the user upfront whether the (optional)
    capevolve-dashboard package can actually be launched, rather than only
    discovering it from cap-evolve's own silent no-op after launch."""
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    real_find_spec = capevolve.importlib.util.find_spec

    monkeypatch.setattr(capevolve.importlib.util, "find_spec",
                        lambda name: None if name == "capevolve_dashboard" else real_find_spec(name))
    missing = capevolve.prepare_project(max_usd=5, optimizer="mock")
    assert missing["dashboard_available"] is False
    assert any("capevolve-dashboard isn't installed" in w for w in missing["warnings"])

    monkeypatch.setattr(capevolve.importlib.util, "find_spec",
                        lambda name: object() if name == "capevolve_dashboard" else real_find_spec(name))
    present = capevolve.prepare_project(max_usd=5, optimizer="mock")
    assert present["dashboard_available"] is True
    assert not any("capevolve-dashboard isn't installed" in w for w in present["warnings"])

    # --dashboard off never warns about a missing install — there's nothing
    # to launch, so nothing to warn about.
    monkeypatch.setattr(capevolve.importlib.util, "find_spec",
                        lambda name: None if name == "capevolve_dashboard" else real_find_spec(name))
    off = capevolve.prepare_project(max_usd=5, optimizer="mock", dashboard="off")
    assert not any("capevolve-dashboard isn't installed" in w for w in off["warnings"])


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
        raise RuntimeError("rollout subprocess failed: ModuleNotFoundError: no module named 'cap_evolve'")
    monkeypatch.setattr(bridge, "_invoke_rollout", _raise)

    result = bridge.run_target(task, str(tmp_path), seed=0)
    assert result.error is not None
    # The real cause (what _invoke_rollout put into str(exc)) must survive
    # into Rollout.error — this is the only place a crash is visible, and
    # /eval-optimize's SKILL.md tells users to find it in the campaign log.
    assert "ModuleNotFoundError" in result.error
    # score() must then mark this as missing data too.
    score = bridge.score(task, result)
    assert score.reward == 0.0
    assert score.raw.get("errored") is True


def test_invoke_rollout_times_out_instead_of_hanging_forever(tmp_path, monkeypatch):
    """Unlike the local substrate's _run_script, the bridge's own subprocess
    call had no cap at all: a hang anywhere in the rollout (workspace/
    execute/collect/score/report, or Harbor generation+execution) blocked
    the campaign forever, since max_usd never gets the chance to trigger
    if this call never returns."""
    bridge = _make_bridge(tmp_path)

    def _fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"), output=b"", stderr=b"stuck")

    monkeypatch.setattr(bridge_template.subprocess, "run", _fake_run)

    with pytest.raises(RuntimeError, match="timed out"):
        bridge._invoke_rollout("case-1", tmp_path, seed=0)


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
    assert status["rollouts_dir"] is None   # no harness manifest in this fixture
    assert status["best_id"] == "seed"
    assert status["spent"] == {"iterations": 3, "usd": 0.4}
    assert status["latest_event"]["candidate"] == "cand_2"
    assert status["iterations"] == {"accepted": 1, "rejected": 1, "indecisive": 1}
    assert status["final"] is None


def test_rollouts_dir_is_fixed_at_scaffold_time(tmp_path, monkeypatch):
    """The rollouts are written under the AGENT_EVAL_RUNS_DIR of the shell
    that launched the campaign. A later --status from a plain shell must
    name that place, not recompute one from its own environment."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(tmp_path / "data-runs"))
    _bare_benchmark(tmp_path)
    result = capevolve.prepare_project(max_usd=5, optimizer="mock")
    manifest = json.loads((Path(result["project_dir"]).parent / "harness/candidate.json").read_text())
    recorded = (tmp_path / "data-runs/.optimize" / manifest["eval_name"]).resolve()
    assert Path(manifest["rollouts_dir"]) == recorded

    monkeypatch.delenv("AGENT_EVAL_RUNS_DIR")
    assert export.runs_dir_for(manifest) == recorded          # the rollouts themselves
    run_dir = tmp_path / ".capevolve/run_x"
    run_dir.mkdir(parents=True)
    capevolve._write_campaign_manifest(Path(result["project_dir"]), {
        "pid": os.getpid(), "create_time": capevolve._process_create_time(os.getpid()),
        "run_dir": str(run_dir), "log_file": str(tmp_path / "campaign.log"),
    })
    assert capevolve.read_status(Path(result["project_dir"]))["rollouts_dir"] == str(recorded)


def test_status_names_the_rollout_directory(tmp_path, monkeypatch):
    """The rollouts sit in a hidden sibling of the ordinary runs, not under
    <runs>/<eval-name>/ where every other page sends people. --status and the
    n_scored: 0 warning name the place."""
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)
    result = capevolve.prepare_project(max_usd=5, optimizer="mock")
    project_dir = Path(result["project_dir"])
    run_dir = tmp_path / ".capevolve/run_x"
    run_dir.mkdir(parents=True)
    (run_dir / "final.json").write_text(json.dumps(
        {"best_id": "seed", "measurements": {"n_scored": 0, "n_tasks": 3}}), encoding="utf-8")
    capevolve._write_campaign_manifest(project_dir, {
        "pid": os.getpid(), "create_time": capevolve._process_create_time(os.getpid()),
        "run_dir": str(run_dir), "log_file": str(tmp_path / "campaign.log"),
    })

    status = capevolve.read_status(project_dir)
    from agent_eval.config import EvalConfig
    expected = str((tmp_path / "eval/runs/.optimize" / EvalConfig.from_yaml("eval.yaml").eval_name()).resolve())
    assert status["rollouts_dir"] == expected
    assert capevolve._coverage_warnings({"n_scored": 0, "n_tasks": 3}, "x.log", expected)[0].endswith(expected)


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


def test_apply_winner_still_warns_when_splits_json_is_missing(tmp_path):
    """astefanutti: the trim that dropped the exact-count caveat wording for
    a missing splits.json left the whole caveat as None — silently dropping
    the optimistic-bias warning instead of degrading it. The warning itself
    must survive even without exact split counts."""
    project_root, harness_manifest, campaign_manifest, _ = _apply_fixture(tmp_path)
    (Path(campaign_manifest["run_dir"]) / "splits.json").unlink()

    result = capevolve.apply_winner(
        project_root=project_root,
        harness_manifest=harness_manifest, campaign_manifest=campaign_manifest,
    )

    assert result["caveat"] is not None
    assert "honest number" in result["caveat"]
    assert "A full /eval-run" in result["caveat"]


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


def test_apply_winner_keeps_the_tracked_plumbing_symlink(tmp_path):
    """The other half of "the snapshot skips symlinks".

    No candidate ever contains one, so a winner missing ``scripts/agent_eval``
    is not the optimizer having decided to drop it — it is the snapshot never
    having carried it. Deleting tracked files absent from the winner is right
    for ordinary files and wrong for these: it would land on the branch as a
    removal of the import plumbing CLAUDE.md requires of every skill.
    """
    project_root, harness_manifest, campaign_manifest, skill_path, run_dir = _apply_fixture_in_checkout(tmp_path)
    skill_dir = skill_path.parent
    (project_root / "agent_eval").mkdir(exist_ok=True)
    (project_root / "agent_eval" / "__init__.py").write_text("", encoding="utf-8")
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "run.py").write_text("print(1)", encoding="utf-8")
    (skill_dir / "scripts" / "agent_eval").symlink_to("../../../agent_eval")
    (skill_dir / "stale.md").write_text("tracked, genuinely dropped", encoding="utf-8")
    _git_commit_all(project_root, "add the plumbing symlink")

    winner = run_dir / "candidates/cand_1/demo"
    (winner / "scripts").mkdir(parents=True)
    (winner / "SKILL.md").write_text("winner skill", encoding="utf-8")
    (winner / "scripts" / "run.py").write_text("print(2)", encoding="utf-8")
    harness_manifest["candidate"] = [{"dest": "demo", "source": str(skill_dir), "is_dir": True}]

    result = capevolve.apply_winner(project_root=project_root, harness_manifest=harness_manifest,
                                    campaign_manifest=campaign_manifest)

    files = subprocess.run(["git", "ls-tree", "-r", "--name-only", result["branch"], "--", "skills/demo"],
                           cwd=project_root, capture_output=True, text=True, check=True).stdout.split()
    assert "skills/demo/scripts/agent_eval" in files   # the symlink survives
    assert "skills/demo/stale.md" not in files         # an ordinary dropped file still goes
    assert (skill_dir / "scripts" / "agent_eval").is_symlink()


@pytest.mark.parametrize("winner_file, link", [
    ("scripts/agent_eval/__init__.py", "scripts/agent_eval"),   # through a directory link
    ("scripts/helper_link.py", "scripts/helper_link.py"),       # onto a file link
])
def test_apply_winner_refuses_to_write_through_a_tracked_symlink(tmp_path, winner_file, link):
    """The other edge of keeping tracked symlinks (previous test).

    copytree(dirs_exist_ok=True) and copy2 both follow a destination link, so
    a winner with ordinary content where the checkout has a link would write
    wherever the link points — into this repository's real agent_eval/ for
    the plumbing link — outside the candidate, outside the skill-scoped
    `git add`, beyond the rollback. The snapshot carries no symlinks, so a
    proposer that creates scripts/agent_eval/ as a directory produces exactly
    this shape. Refuse before the worktree is touched (both reviewers of
    #299 flagged this independently).
    """
    project_root, harness_manifest, campaign_manifest, skill_path, run_dir = _apply_fixture_in_checkout(tmp_path)
    skill_dir = skill_path.parent
    (project_root / "agent_eval").mkdir(exist_ok=True)
    (project_root / "agent_eval" / "__init__.py").write_text("REAL = 1\n", encoding="utf-8")
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "helper.py").write_text("x = 1\n", encoding="utf-8")
    (skill_dir / "scripts" / "helper_link.py").symlink_to("helper.py")
    (skill_dir / "scripts" / "agent_eval").symlink_to("../../../agent_eval")
    _git_commit_all(project_root, "add the links")
    original_branch = _current_branch(project_root)

    winner = run_dir / "candidates/cand_1/demo"
    (winner / Path(winner_file).parent).mkdir(parents=True)
    (winner / "SKILL.md").write_text("winner skill", encoding="utf-8")
    (winner / winner_file).write_text("REAL = 'overwritten'\n", encoding="utf-8")
    harness_manifest["candidate"] = [{"dest": "demo", "source": str(skill_dir), "is_dir": True}]

    with pytest.raises(RuntimeError, match=f"content at or beneath {link}"):
        capevolve.apply_winner(project_root=project_root, harness_manifest=harness_manifest,
                               campaign_manifest=campaign_manifest)

    # Nothing was written anywhere: not through the link, not on a branch.
    assert (project_root / "agent_eval" / "__init__.py").read_text() == "REAL = 1\n"
    assert (skill_dir / "scripts" / "helper.py").read_text() == "x = 1\n"
    assert _current_branch(project_root) == original_branch
    branches = subprocess.run(["git", "branch", "--list", "capevolve/*"], cwd=project_root,
                              capture_output=True, text=True, check=True).stdout.strip()
    assert branches == ""


def test_apply_winner_refuses_an_empty_winner_dir_at_a_tracked_link(tmp_path):
    """Files are not the only thing copytree writes through a link: with a
    winner directory (even empty) where the checkout has a directory link,
    dirs_exist_ok=True makedirs and copystats THROUGH the link, touching the
    real target. Any winner entry at or beneath a link is a collision."""
    project_root, harness_manifest, campaign_manifest, skill_path, run_dir = _apply_fixture_in_checkout(tmp_path)
    skill_dir = skill_path.parent
    (project_root / "agent_eval").mkdir(exist_ok=True)
    (project_root / "agent_eval" / "__init__.py").write_text("REAL = 1\n", encoding="utf-8")
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "agent_eval").symlink_to("../../../agent_eval")
    _git_commit_all(project_root, "add the link")
    winner = run_dir / "candidates/cand_1/demo"
    (winner / "scripts/agent_eval").mkdir(parents=True)   # empty
    (winner / "SKILL.md").write_text("winner skill", encoding="utf-8")
    harness_manifest["candidate"] = [{"dest": "demo", "source": str(skill_dir), "is_dir": True}]

    with pytest.raises(RuntimeError, match="content at or beneath scripts/agent_eval"):
        capevolve.apply_winner(project_root=project_root, harness_manifest=harness_manifest,
                               campaign_manifest=campaign_manifest)


def test_apply_winner_refuses_a_symlink_at_an_intermediate_segment(tmp_path):
    """A nested dest (`nested/demo`) whose intermediate segment in the
    snapshot is a link: everything beneath would be read from wherever it
    points and committed onto the branch. Rollouts refuse the shape
    (stage_candidate's intermediate-link check); so does --apply."""
    project_root, harness_manifest, campaign_manifest, skill_path, run_dir = _apply_fixture_in_checkout(tmp_path)
    nested = project_root / "skills/nested/demo"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("original", encoding="utf-8")
    _git_commit_all(project_root, "nested skill")
    elsewhere = tmp_path / "elsewhere/demo"
    elsewhere.mkdir(parents=True)
    (elsewhere / "id_rsa").write_text("PRIVATE", encoding="utf-8")
    (elsewhere / "SKILL.md").write_text("planted", encoding="utf-8")
    (run_dir / "candidates/cand_1/nested").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    harness_manifest["candidate"] = [{"dest": "nested/demo", "source": str(nested), "is_dir": True}]

    with pytest.raises(RuntimeError, match="nested is a symlink inside the candidate snapshot"):
        capevolve.apply_winner(project_root=project_root, harness_manifest=harness_manifest,
                               campaign_manifest=campaign_manifest)
    assert not (nested / "id_rsa").exists()
    branches = subprocess.run(["git", "branch", "--list", "capevolve/*"], cwd=project_root,
                              capture_output=True, text=True, check=True).stdout.strip()
    assert branches == ""


def test_apply_winner_refuses_a_winner_that_contains_symlinks(tmp_path):
    """Every rollout refuses a candidate with a symlink, so a winner carrying
    one was altered after measurement or never ran. copytree(symlinks=False)
    and copy2 would dereference it — a directory link through the checkout's
    plumbing link, a file link to a credential — onto the branch."""
    project_root, harness_manifest, campaign_manifest, skill_path, run_dir = _apply_fixture_in_checkout(tmp_path)
    skill_dir = skill_path.parent
    original_branch = _current_branch(project_root)
    winner = run_dir / "candidates/cand_1/demo"
    (winner / "scripts").mkdir(parents=True)
    (winner / "SKILL.md").write_text("winner skill", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("hunter2", encoding="utf-8")
    (winner / "scripts" / "notes.md").symlink_to(secret)
    harness_manifest["candidate"] = [{"dest": "demo", "source": str(skill_dir), "is_dir": True}]

    with pytest.raises(RuntimeError, match="contains symlink.*scripts/notes.md"):
        capevolve.apply_winner(project_root=project_root, harness_manifest=harness_manifest,
                               campaign_manifest=campaign_manifest)
    assert not (skill_dir / "scripts").exists()
    assert _current_branch(project_root) == original_branch


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


def test_apply_winner_deletes_its_branch_on_a_mid_apply_failure_so_retry_works(tmp_path):
    """Before this fix, `checkout -b` ran before the try/finally, so a
    failed apply left the branch behind; a retry then died at `checkout -b`
    with an opaque "branch already exists" CalledProcessError."""
    project_root, harness_manifest, campaign_manifest, skill_path = _apply_fixture(tmp_path)
    original_branch = _current_branch(project_root)
    harness_manifest["candidate"].append(
        {"dest": "references/missing.md", "source": str(project_root / "skills/demo/references/missing.md"),
         "is_dir": False}
    )

    with pytest.raises(RuntimeError, match="missing component"):
        capevolve.apply_winner(
            project_root=project_root,
            harness_manifest=harness_manifest, campaign_manifest=campaign_manifest,
        )

    branches = subprocess.run(["git", "branch", "--list", "capevolve/*"], cwd=project_root,
                              capture_output=True, text=True, check=True).stdout.strip()
    assert branches == ""
    assert _current_branch(project_root) == original_branch


def test_apply_winner_ignores_the_configured_runs_dir_when_checking_dirty(tmp_path, monkeypatch):
    """AGENT_EVAL_RUNS_DIR (default eval/runs) is populated by every rollout
    the campaign itself runs — a benchmark that doesn't gitignore it must
    not have --apply refuse as "not clean" over the campaign's own byproduct."""
    project_root, harness_manifest, campaign_manifest, skill_path = _apply_fixture(
        tmp_path, winning_skill_content="improved skill"
    )
    runs_dir = project_root / "eval" / "runs" / "some-eval" / "opt-1"
    runs_dir.mkdir(parents=True)
    (runs_dir / "run_result.json").write_text("{}", encoding="utf-8")

    result = capevolve.apply_winner(
        project_root=project_root,
        harness_manifest=harness_manifest, campaign_manifest=campaign_manifest,
    )

    assert result["best_id"] == "cand_1"


def test_apply_winner_tolerates_an_absolute_runs_dir_outside_the_repo(tmp_path, monkeypatch):
    """export.runs_dir_for supports an absolute AGENT_EVAL_RUNS_DIR outside
    the project (its own docstring). git's `:(exclude)` pathspec hard-errors
    on a path outside the worktree, so adding that exclude unconditionally
    turned every --apply into a fatal `git status` failure for such a
    setup, instead of just skipping the (inapplicable) exclude."""
    project_root, harness_manifest, campaign_manifest, skill_path = _apply_fixture(
        tmp_path, winning_skill_content="improved skill"
    )
    outside_runs_dir = tmp_path / "outside-runs"
    outside_runs_dir.mkdir()
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(outside_runs_dir))

    result = capevolve.apply_winner(
        project_root=project_root,
        harness_manifest=harness_manifest, campaign_manifest=campaign_manifest,
    )

    assert result["best_id"] == "cand_1"


def test_rollouts_land_in_a_parallel_runs_root(tmp_path, monkeypatch):
    """<base>/.optimize/<eval>/<run-id>: out of the ordinary runs dir, so a
    campaign's hundreds of rollouts never crowd it."""
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(tmp_path / "runs"))
    manifest = {"project_root": str(tmp_path), "eval_name": "demo-eval"}
    root = (tmp_path / "runs/.optimize").resolve()
    assert export.rollout_runs_root(manifest) == root
    assert export.runs_dir_for(manifest) == root / "demo-eval"
    # The children's environment is NOT re-pointed at the rollout root: that
    # variable reaches the agent under test (a plugin shipping this repo's
    # Stop guard would then block its own agent) and score.py's few-shot
    # harvest. The rollout's directory travels as --run-dir instead.
    assert rollout._child_env()["AGENT_EVAL_RUNS_DIR"] == str(tmp_path / "runs")
    # Relative (the default) resolves against the project, like everything else.
    monkeypatch.delenv("AGENT_EVAL_RUNS_DIR")
    assert export.rollout_runs_root(manifest) == (tmp_path / "eval/runs/.optimize").resolve()


def test_rollout_runs_are_hidden_from_a_sweep_but_not_from_a_direct_ask(tmp_path):
    """A campaign is |val| x num_trials x (iterations + 1) run dirs, every one
    of them a real summary.yaml. /eval-compare and /eval-anova rglob for those,
    so without a filter one campaign buries the two or three runs a person
    meant to compare. Asking for the rollouts directly still works: that is
    how you look at the search curve. Releases up to 1.62.x wrote rollouts
    flat into <runs>/<eval>/, so those are recognised by their condition.json."""
    runs = tmp_path / "runs"
    new = runs / ".optimize/demo-eval/opt-1"
    assert export.is_rollout_run(new, runs)
    assert export.is_rollout_run(new, runs / ".." / runs.name)  # unnormalised input
    assert not export.is_rollout_run(runs / "demo-eval/2026-01-01-baseline", runs)
    # Pointed at the rollouts, at any depth: never filtered.
    assert not export.is_rollout_run(new, runs / ".optimize")
    assert not export.is_rollout_run(new, runs / ".optimize/demo-eval")
    assert not export.is_rollout_run(new, new)
    # A legacy flat rollout: provenance keys, no factor levels.
    legacy = runs / "demo-eval/opt-legacy"
    legacy.mkdir(parents=True)
    (legacy / "condition.json").write_text(json.dumps(
        {"engine": "cap-evolve", "candidate_id": "cand_1", "harness_run_id": "opt-legacy"}), encoding="utf-8")
    assert export.is_rollout_run(legacy, runs)
    assert export.is_rollout_run(legacy, runs / "demo-eval")
    assert not export.is_rollout_run(legacy, legacy)
    # An /eval-anova cell's condition.json is factor levels, not a rollout.
    cell = runs / "demo-eval/cell-1"
    cell.mkdir()
    (cell / "condition.json").write_text(json.dumps({"levels": {"model": "sonnet"}}), encoding="utf-8")
    assert not export.is_rollout_run(cell, runs)
    # Unrelated trees are never "rollouts" just because they can't be related.
    assert not export.is_rollout_run(Path("/elsewhere/run"), runs)


def test_eval_compare_and_anova_skip_rollout_run_dirs(tmp_path, capsys):
    def _write_run(path, condition=None):
        path.mkdir(parents=True)
        (path / "summary.yaml").write_text("per_case: {}\n", encoding="utf-8")
        (path / "run_result.json").write_text(json.dumps({"model": "sonnet"}), encoding="utf-8")
        if condition is not None:
            (path / "condition.json").write_text(json.dumps(condition), encoding="utf-8")

    base = tmp_path / "runs"
    runs = base / "demo-eval"
    _write_run(runs / "2026-01-01-baseline", {"levels": {"model": "sonnet"}})
    for i in range(3):
        _write_run(base / ".optimize/demo-eval" / f"opt-case-{i}",
                   {"engine": "cap-evolve", "candidate_id": "cand_1", "task_id": f"case-{i}",
                    "seed": 0, "harness_run_id": f"opt-case-{i}"})
    # Left behind by a release up to 1.62.x, which wrote rollouts flat.
    _write_run(runs / "opt-legacy",
               {"engine": "cap-evolve", "candidate_id": "cand_0", "task_id": "case-9",
                "seed": 0, "harness_run_id": "opt-legacy"})

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/eval-compare/scripts"))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/eval-anova/scripts"))
    try:
        import compare
        analyze = pytest.importorskip("analyze", reason="eval-anova needs the anova extra")
        for root in (base, runs):
            assert [r["name"] for r in compare.discover_runs(root)] == ["2026-01-01-baseline"]
            assert "skipped" in capsys.readouterr().err
            assert [p.name for p in analyze._discover_run_dirs(root)] == ["2026-01-01-baseline"]
            # On stderr too — not through a logger nothing configures.
            assert "skipped" in capsys.readouterr().err
        # And the search curve is still reachable by asking for it.
        assert len(compare.discover_runs(base / ".optimize")) == 3
        assert len(compare.discover_runs(base / ".optimize/demo-eval")) == 3
    finally:
        sys.path[:2] = []


def test_a_rollout_condition_is_never_read_as_a_factorial_cell(tmp_path):
    """condition.json on a rollout records candidate/seed/substrate — not
    factor levels. _condition_levels_for_run's `data.get("levels", data)`
    would otherwise make every rollout its own one-off condition."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/eval-anova/scripts"))
    try:
        analyze = pytest.importorskip("analyze", reason="eval-anova needs the anova extra")
        run = tmp_path / "opt-case-1"
        run.mkdir()
        (run / "condition.json").write_text(json.dumps(
            {"engine": "cap-evolve", "candidate_id": "cand_1", "harness_run_id": "opt-case-1"}),
            encoding="utf-8")
        (run / "run_result.json").write_text(json.dumps({"model": "sonnet"}), encoding="utf-8")
        assert analyze._condition_levels_for_run(run) == {"model": "sonnet"}
    finally:
        sys.path[:1] = []


def test_local_rollout_scores_through_the_real_harness_scripts(tmp_path, monkeypatch):
    """The regression every other run_case test is blind to.

    They all mock _run_script, so nothing checks that the directory run_case
    writes is the directory score.py and report.py *read*. Those two get only
    --run-id and resolve it as $AGENT_EVAL_RUNS_DIR/<eval>/<run-id>; when the
    rollouts moved out of the ordinary runs dir, score.py looked beside them,
    found no cases, and every local rollout quietly scored as "no judge
    results" (caught by two independent reviewers on #299).

    Only execute.py is faked (it would invoke a real agent). workspace.py,
    collect.py, score.py and report.py run for real, as subprocesses, with
    the env run_case gives them.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(tmp_path / "runs"))
    _demo_eval_yaml(tmp_path)
    # The judge reads execution metadata, which score.py takes from the
    # run-level run_result.json at runs_dir / run_id — the second place a
    # mis-resolved run dir shows (the first is the cases dir). A judge that
    # just returned True would pass with cost=None exit=None.
    eval_yaml = tmp_path / "eval.yaml"
    eval_yaml.write_text(eval_yaml.read_text().replace(
        "    check: |\n      return True, 'ok'\n",
        "    check: |\n"
        "      ok = outputs.get('cost_usd') == 0.01 and outputs.get('exit_code') == 0\n"
        "      return ok, f\"cost={outputs.get('cost_usd')} exit={outputs.get('exit_code')}\"\n"),
        encoding="utf-8")
    from agent_eval.config import EvalConfig
    # As prepare_project does — the same eval_name() score.py will derive
    # from the config it is handed, which is the whole point of this test.
    eval_name = EvalConfig.from_yaml("eval.yaml").eval_name()
    manifest = {
        "harness_root": str(Path(__file__).resolve().parents[1]),
        "project_root": str(tmp_path),
        "eval_config": str(tmp_path / "eval.yaml"), "eval_name": eval_name,
        "execution_mode": "case", "apply_to": ".claude/skills/demo",
        "candidate": [{"dest": "SKILL.md", "source": str(tmp_path / "skills/demo/SKILL.md"), "is_dir": False}],
        "task_ids": ["case-1"], "runner_budget_usd": None, "runtime_resources": [],
    }
    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    (candidate_dir / "SKILL.md").write_text("candidate", encoding="utf-8")

    real_run_script = rollout._run_script

    def _fake_execute_only(manifest_, name, *args, allow_nonzero=False):
        if name != "execute.py":
            return real_run_script(manifest_, name, *args, allow_nonzero=allow_nonzero)
        run_dir = Path(args[args.index("--output") + 1])
        (run_dir / "cases" / "case-1").mkdir(parents=True, exist_ok=True)
        (run_dir / "cases/case-1/stdout.log").write_text("transcript", encoding="utf-8")
        (run_dir / "run_result.json").write_text(json.dumps({
            "run_id": args[args.index("--run-id") + 1], "model": "sonnet", "cost_usd": 0.01,
            "execution_mode": "case",
            "per_case": {"case-1": {"exit_code": 0, "cost_usd": 0.01, "num_turns": 3}},
        }), encoding="utf-8")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(rollout, "_run_script", _fake_execute_only)
    result = rollout.run_case(manifest, "case-1", candidate_dir, seed=0)["rollout"]

    run_dir = tmp_path / "runs/.optimize" / eval_name / result["metadata"]["harness_run_id"]
    assert (run_dir / "summary.yaml").is_file(), sorted(p.name for p in run_dir.iterdir())
    assert result["error"] is None, result["error"]
    assert result["metadata"]["reward"] == 1.0, result["metadata"]["feedback"]
    assert "quality=pass" in result["metadata"]["feedback"]
    # report.py is handed the same --run-dir; its failure is tolerated by
    # design, so the only proof it found the run is the report itself.
    assert (run_dir / "report.html").is_file()


def _nested_apply_fixture(tmp_path):
    """Like _apply_fixture, but the benchmark sits nested inside a larger
    repository (project_root != repo_root) — the shape astefanutti's #2/#4
    findings live in."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)
    project_root = repo_root / "bench"
    skill_path = project_root / "skills" / "demo" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("original skill", encoding="utf-8")
    _git_commit_all(repo_root, "initial")

    run_dir = tmp_path / "outside" / "run_20260101_000000"
    run_dir.mkdir(parents=True)
    (run_dir / "final.json").write_text(
        json.dumps({"best_id": "cand_1", "test_delta": 0.12}), encoding="utf-8"
    )
    (run_dir / "splits.json").write_text(json.dumps({
        "train": ["case-1", "case-2"], "val": ["case-3"], "test": ["case-4"],
    }), encoding="utf-8")
    candidate_dir = run_dir / "candidates" / "cand_1"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "SKILL.md").write_text("candidate skill", encoding="utf-8")

    harness_manifest = {
        "candidate": [{"dest": "SKILL.md", "source": str(skill_path), "is_dir": False}],
    }
    campaign_manifest = {"run_dir": str(run_dir)}
    return repo_root, project_root, harness_manifest, campaign_manifest, skill_path


def test_apply_winner_gate_covers_uncommitted_work_outside_a_nested_benchmark(tmp_path):
    """astefanutti #2 (blocking): the dirty-check was pathspec-scoped to the
    benchmark while the failure-path `git reset --hard HEAD` resets the
    whole worktree — a benchmark nested in a larger repository would wave
    through uncommitted work elsewhere that a failed apply then destroyed.
    The gate must cover everything the rollback can touch."""
    repo_root, project_root, harness_manifest, campaign_manifest, _ = _nested_apply_fixture(tmp_path)
    outside = repo_root / "src" / "work.txt"
    outside.parent.mkdir()
    outside.write_text("precious uncommitted work", encoding="utf-8")

    with pytest.raises(RuntimeError, match="not clean"):
        capevolve.apply_winner(
            project_root=project_root,
            harness_manifest=harness_manifest, campaign_manifest=campaign_manifest,
        )
    assert outside.read_text(encoding="utf-8") == "precious uncommitted work"
    assert _current_branch(project_root) in ("main", "master")


def test_apply_winner_excludes_the_recorded_rollouts_dir_without_the_launch_env(tmp_path, monkeypatch):
    """Rollouts land where the scaffold-time AGENT_EVAL_RUNS_DIR put them,
    recorded as rollouts_dir. A plain shell running --apply has a different
    (default) root, so the env-derived exclude misses them and the dirty gate
    would refuse a finished campaign over its own byproduct."""
    project_root, harness_manifest, campaign_manifest, skill_path = _apply_fixture(
        tmp_path, winning_skill_content="improved skill"
    )
    monkeypatch.delenv("AGENT_EVAL_RUNS_DIR", raising=False)
    rollouts = project_root / "data-runs" / ".optimize" / "demo"
    (rollouts / "opt-1").mkdir(parents=True)
    (rollouts / "opt-1/summary.yaml").write_text("per_case: {}\n", encoding="utf-8")
    harness_manifest["rollouts_dir"] = str(rollouts)

    result = capevolve.apply_winner(project_root=project_root, harness_manifest=harness_manifest,
                                    campaign_manifest=campaign_manifest)
    assert result["best_id"] == "cand_1"
    tracked = subprocess.run(["git", "ls-files", "--", "data-runs"], cwd=project_root,
                             capture_output=True, text=True, check=True).stdout.strip()
    assert tracked == ""


def test_apply_winner_tolerates_a_runs_dir_inside_the_repo_but_outside_the_benchmark(tmp_path, monkeypatch):
    """astefanutti #4: the runs-dir exclude guarded on repo_root but
    computed against project_root — an AGENT_EVAL_RUNS_DIR inside the repo
    but outside the benchmark raised an uncaught ValueError, so --apply
    could never run for that layout."""
    repo_root, project_root, harness_manifest, campaign_manifest, _ = _nested_apply_fixture(tmp_path)
    runs_dir = repo_root / "shared-eval-runs" / "opt-1"
    runs_dir.mkdir(parents=True)
    (runs_dir / "run_result.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(runs_dir))

    result = capevolve.apply_winner(
        project_root=project_root,
        harness_manifest=harness_manifest, campaign_manifest=campaign_manifest,
    )

    assert result["best_id"] == "cand_1"
    # The campaign's own run records stayed untracked — never committed
    # with the winner.
    tracked = subprocess.run(
        ["git", "ls-files", "--", "shared-eval-runs"], cwd=repo_root,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert tracked == ""


def test_a_failing_post_checkout_hook_does_not_hide_a_successful_apply(tmp_path, monkeypatch):
    """astefanutti #9: a post-checkout hook (git-lfs installs one; it exits
    non-zero when `git lfs` is missing, and git makes its status the
    checkout's status even though the switch worked) used to surface as a
    bare CalledProcessError after the winner was already committed — an
    opaque failure that dropped the whole result. The error must say the
    apply succeeded, name the branch, and tell the user how to get back."""
    project_root, harness_manifest, campaign_manifest, skill_path = _apply_fixture(
        tmp_path, winning_skill_content="improved skill"
    )
    real_git = capevolve._git
    original_branch = capevolve._git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=project_root).stdout.strip()

    def _fake_git(args, cwd, **kwargs):
        if args == ["checkout", original_branch]:
            raise subprocess.CalledProcessError(
                1, args, output="", stderr="post-checkout hook exited 3")
        return real_git(args, cwd=cwd, **kwargs)

    monkeypatch.setattr(capevolve, "_git", _fake_git)

    with pytest.raises(RuntimeError) as exc_info:
        capevolve.apply_winner(
            project_root=project_root,
            harness_manifest=harness_manifest, campaign_manifest=campaign_manifest,
        )
    message = str(exc_info.value)
    assert "applied candidate cand_1" in message
    assert "capevolve/run_20260101_000000" in message
    assert f"git checkout {original_branch}" in message
    assert "post-checkout hook exited 3" in message
    # The winner really did land on the branch.
    shown = subprocess.run(
        ["git", "show", "capevolve/run_20260101_000000:skills/demo/SKILL.md"], cwd=project_root,
        capture_output=True, text=True, check=True,
    ).stdout
    assert shown == "improved skill"


def test_apply_winner_chains_the_original_error_when_rollback_also_fails(tmp_path, monkeypatch):
    """A `finally` block that raises replaces whatever was propagating from
    the try body (CPython: the original is not even in __context__ unless
    the new error is chained `from` it) — a failing rollback step must not
    silently stand in for the real apply failure."""
    project_root, harness_manifest, campaign_manifest, skill_path = _apply_fixture(tmp_path)
    real_git = capevolve._git
    original_branch = capevolve._git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=project_root).stdout.strip()

    def _fake_git(args, cwd, **kwargs):
        # Fail the real apply (the `git commit` inside the try) AND the
        # rollback's checkout-back, to exercise both exception paths at once.
        if args[0] == "commit":
            raise subprocess.CalledProcessError(1, args, output="", stderr="commit rejected: hook failed")
        if args == ["checkout", original_branch]:
            raise subprocess.CalledProcessError(128, args, output="", stderr="checkout refused: dirty")
        return real_git(args, cwd=cwd, **kwargs)

    monkeypatch.setattr(capevolve, "_git", _fake_git)

    with pytest.raises(RuntimeError) as exc_info:
        capevolve.apply_winner(
            project_root=project_root,
            harness_manifest=harness_manifest, campaign_manifest=campaign_manifest,
        )

    # The original apply failure is still present in the message raised to
    # the caller (not silently replaced by the rollback's own
    # CalledProcessError), and the rollback failure is appended alongside
    # it, with recovery guidance.
    message = str(exc_info.value)
    assert "commit rejected: hook failed" in message
    assert "rollback could not finish" in message
    assert "checkout refused: dirty" in message


# ── cap-evolve install: pinned package + matching skills tree ────────────────

def _skills_tree(root: Path) -> Path:
    (root / "skills" / "_registry").mkdir(parents=True)
    (root / "skills" / "_registry" / "manifest.json").write_text("{}", encoding="utf-8")
    return root / "skills"


def test_pinned_sha_is_the_optimize_extras_commit():
    sha = capevolve.pinned_sha()
    assert len(sha) == 40
    assert f"cap-evolve.git@{sha}" in (capevolve._HARNESS_ROOT / "pyproject.toml").read_text()


def test_managed_tree_is_fetched_once_then_reused(monkeypatch):
    calls = []
    stub = capevolve._fetch_tree
    monkeypatch.setattr(capevolve, "_fetch_tree", lambda sha, dest: (calls.append(sha), stub(sha, dest)))

    first = capevolve.resolve_skills_dir()
    second = capevolve.resolve_skills_dir()

    sha = capevolve.pinned_sha()
    assert first["source"] == "managed" and first["fetched"] is True
    assert second["fetched"] is False and second["skills_dir"] == first["skills_dir"]
    assert Path(first["skills_dir"]) == Path(os.environ["CLAUDE_PLUGIN_DATA"]) / "cap-evolve" / sha / "skills"
    assert calls == [sha]


def test_a_pin_bump_fetches_a_separate_tree(tmp_path):
    harness = tmp_path / "harness"
    harness.mkdir()
    bumped = "b" * 40
    (harness / "pyproject.toml").write_text(
        f'optimize = ["cap-evolve-core @ git+https://github.com/skillberry-ai/cap-evolve.git@{bumped}#subdirectory=core"]\n',
        encoding="utf-8")

    current = capevolve.resolve_skills_dir()
    after_bump = capevolve.resolve_skills_dir(harness_root=harness)

    assert after_bump["fetched"] is True and after_bump["pinned_sha"] == bumped
    assert after_bump["skills_dir"] != current["skills_dir"]


def test_an_interrupted_fetch_is_redone(monkeypatch):
    sha = capevolve.pinned_sha()
    partial = Path(os.environ["CLAUDE_PLUGIN_DATA"]) / "cap-evolve" / sha
    (partial / "skills" / "phases").mkdir(parents=True)  # no _registry: never finished

    result = capevolve.resolve_skills_dir()

    assert result["fetched"] is True
    assert (partial / "skills" / "_registry" / "manifest.json").is_file()


def test_unfetched_tree_without_fetch_is_a_setup_step():
    with pytest.raises(capevolve.SetupRequired, match="not been fetched"):
        capevolve.resolve_skills_dir(fetch=False)


def test_override_at_the_pin_is_used(tmp_path):
    skills = _skills_tree(tmp_path / "checkout")
    (skills.parent / capevolve._SHA_STAMP).write_text(capevolve.pinned_sha(), encoding="utf-8")

    result = capevolve.resolve_skills_dir(str(skills))

    assert result == {"skills_dir": str(skills.resolve()), "source": "override",
                      "pinned_sha": capevolve.pinned_sha(), "fetched": False, "warnings": []}


def test_override_checkout_at_another_commit_is_rejected(tmp_path):
    """The drift a demo hit: a cap-evolve checkout on main next to the pinned
    cap-evolve-core package."""
    checkout = tmp_path / "cap-evolve"
    skills = _skills_tree(checkout)
    _init_git_repo(checkout)
    _git_commit_all(checkout, "main")

    with pytest.raises(ValueError, match=f"git checkout {capevolve.pinned_sha()[:12]}"):
        capevolve.resolve_skills_dir(str(skills))


def test_override_that_is_not_a_skills_tree_is_rejected(tmp_path):
    """A folder named cap-evolve holding only design docs is not a checkout."""
    docs = tmp_path / "cap-evolve"
    docs.mkdir()
    (docs / "integration.docx").write_bytes(b"doc")

    with pytest.raises(ValueError, match="not a cap-evolve skills tree"):
        capevolve.resolve_skills_dir(str(docs))


def test_unverifiable_override_is_used_with_a_warning(tmp_path):
    skills = _skills_tree(tmp_path / "copy")  # no stamp, not a git checkout

    result = capevolve.resolve_skills_dir(str(skills))

    assert result["source"] == "override"
    assert "could not verify" in result["warnings"][0]


def _tarball(path: Path, sha: str, members: dict) -> Path:
    import io
    import tarfile
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(f"cap-evolve-{sha}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def test_fetch_keeps_only_skills_and_templates(tmp_path, monkeypatch):
    sha = "c" * 40
    archive = _tarball(tmp_path / "src.tar.gz", sha, {
        "skills/_registry/manifest.json": b"{}",
        "skills/phases/run.md": b"phase",
        "templates/project/capevolve.yaml": b"spec",
        "core/cap_evolve/cli.py": b"code",
        "README.md": b"readme",
    })
    monkeypatch.setattr(capevolve.urllib.request, "urlopen", lambda url, timeout: open(archive, "rb"))
    dest = tmp_path / "data" / sha

    _REAL_FETCH_TREE(sha, dest)

    assert sorted(p.name for p in dest.iterdir()) == [capevolve._SHA_STAMP, "skills", "templates"]
    assert (dest / "skills/phases/run.md").read_text() == "phase"
    assert (dest / capevolve._SHA_STAMP).read_text().strip() == sha
    assert [p.name for p in dest.parent.iterdir()] == [sha]  # no staging dir left behind


def test_fetch_refuses_a_member_escaping_the_tree(tmp_path, monkeypatch):
    sha = "d" * 40
    archive = _tarball(tmp_path / "src.tar.gz", sha, {
        "skills/_registry/manifest.json": b"{}",
        "skills/../../../escaped.txt": b"x",
    })
    monkeypatch.setattr(capevolve.urllib.request, "urlopen", lambda url, timeout: open(archive, "rb"))
    dest = tmp_path / "data" / sha

    with pytest.raises(Exception):
        _REAL_FETCH_TREE(sha, dest)
    assert not dest.exists()
    assert not list(tmp_path.rglob("escaped.txt"))


def test_fetch_without_network_names_the_offline_fix(tmp_path, monkeypatch):
    def offline(url, timeout):
        raise OSError("network unreachable")
    monkeypatch.setattr(capevolve.urllib.request, "urlopen", offline)

    with pytest.raises(RuntimeError, match="--skills-dir"):
        _REAL_FETCH_TREE("e" * 40, tmp_path / "data" / ("e" * 40))


def test_missing_cap_evolve_core_is_a_setup_step(monkeypatch):
    # Pin Python >= 3.11 so the version guard doesn't fire first (on a 3.10
    # host the version check raises before find_spec is ever called).
    monkeypatch.setattr(capevolve.sys, "version_info", (3, 11, 0))
    monkeypatch.setattr(capevolve.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(capevolve.SetupRequired, match="cap-evolve-core is not installed"):
        _REAL_REQUIRE_CAP_EVOLVE()


def test_an_old_venv_python_is_a_setup_step(monkeypatch):
    monkeypatch.setattr(capevolve.sys, "version_info", (3, 10, 16))
    with pytest.raises(capevolve.SetupRequired, match="3.10"):
        _REAL_REQUIRE_CAP_EVOLVE()


def test_cli_reports_setup_required_as_json_before_intake(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)

    def missing():
        raise capevolve.SetupRequired("cap-evolve-core is not installed in the harness venv")
    monkeypatch.setattr(capevolve, "require_cap_evolve", missing)

    exit_code = capevolve.main(["--max-usd", "5", "--dry-run"])

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out) == {
        "error": "setup_required",
        "reason": "cap-evolve-core is not installed in the harness venv",
        "fix": "/eval-setup --optimize",
    }
    assert not (tmp_path / ".capevolve").exists()


def test_cli_setup_fetches_the_tree(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert capevolve.main(["--setup"]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["source"] == "managed" and result["fetched"] is True
    assert result["pinned_sha"] == capevolve.pinned_sha()


def test_venv_python_prefers_the_venv_interpreter_over_sys_executable(tmp_path, monkeypatch):
    """sys.executable is wrong for a subprocess: agent_eval._bootstrap only
    patches THIS process's sys.path (the common case, same major.minor as
    the venv) and never repoints sys.executable, so a child spawned with
    sys.executable cannot import cap_evolve even though the venv has it.
    Regression test for the "cap-evolve returned empty stdout" failure that
    resulted: see PR review for the live repro."""
    v = sys.version_info
    venv_bin = tmp_path / ".eval-venv" / "bin"
    venv_bin.mkdir(parents=True)
    venv_python = venv_bin / "python3"
    venv_python.write_text("#!/bin/sh\n", encoding="utf-8")
    site_packages = tmp_path / ".eval-venv" / "lib" / f"python{v.major}.{v.minor}" / "site-packages"
    site_packages.mkdir(parents=True)

    resolved = capevolve._venv_python(tmp_path)

    assert resolved == str(venv_python)
    assert resolved != sys.executable


def test_venv_python_falls_back_to_sys_executable_without_a_venv(tmp_path):
    assert capevolve._venv_python(tmp_path) == sys.executable


def test_cap_evolve_empty_stdout_error_includes_the_real_stderr(tmp_path, monkeypatch):
    """stderr is the only place a child crash (e.g. an import error) shows
    up; before this fix it was captured and silently discarded, so a user
    saw only "cap-evolve returned empty stdout" with no way to diagnose it."""
    monkeypatch.setattr(
        capevolve.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], 1, stdout="",
                                                     stderr="ModuleNotFoundError: no module named 'cap_evolve'"),
    )
    with pytest.raises(RuntimeError, match="ModuleNotFoundError"):
        capevolve._cap_evolve(["check"], cwd=tmp_path, harness_root=tmp_path)


def test_cap_evolve_nonzero_exit_error_includes_stderr_alongside_stdout(tmp_path, monkeypatch):
    monkeypatch.setattr(
        capevolve.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], 1, stdout='{"ok": false}',
                                                     stderr="Traceback: boom"),
    )
    with pytest.raises(RuntimeError, match="Traceback: boom"):
        capevolve._cap_evolve(["check"], cwd=tmp_path, harness_root=tmp_path)


def test_launch_stop_hint_signals_the_whole_process_group(tmp_path, monkeypatch):
    """proc.pid is also the process group id (start_new_session=True), so
    the hint must be `kill -- -<pid>`, not plain `kill <pid>` — the latter
    orphans every rollout/claude child still in that group."""
    project_dir = tmp_path / ".capevolve" / "project"
    project_dir.mkdir(parents=True)
    run_dir = project_dir.parent / "run_20260101_000000"
    run_dir.mkdir()

    class _FakeProc:
        pid = 12345
        def poll(self):
            return 0

    monkeypatch.setattr(capevolve.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(capevolve, "_wait_for_new_run_dir", lambda *a, **k: run_dir)
    monkeypatch.setattr(capevolve, "_process_create_time", lambda pid: "123")

    manifest = capevolve.launch_detached(
        project_root=tmp_path, project_dir=project_dir, spec_path="capevolve.yaml",
        max_usd=5, max_iterations=1, skills_dir=None, harness_root=tmp_path,
    )

    assert manifest["stop_hint"] == "kill -- -12345"


def test_stop_campaign_kills_the_process_group_not_just_the_leader(tmp_path, monkeypatch):
    project_dir = tmp_path / ".capevolve" / "project"
    project_dir.mkdir(parents=True)
    capevolve._write_campaign_manifest(project_dir, {
        "pid": 999, "create_time": "123", "run_dir": str(tmp_path / "run_x"),
    })
    # astefanutti's follow-up: without this, the guard calls the REAL
    # _process_create_time(999) — None only because pid 999 happens not to
    # exist on this host. Pin the dead-leader shape the test is named for.
    monkeypatch.setattr(capevolve, "_process_create_time", lambda pid: None)
    killed = {}
    monkeypatch.setattr(capevolve.os, "killpg", lambda pid, sig: killed.update(pid=pid, sig=sig))

    result = capevolve.stop_campaign(project_dir)

    assert killed == {"pid": 999, "sig": capevolve.signal.SIGTERM}
    assert result["status"] == "stopped"


def test_stop_campaign_reports_already_stopped_when_the_group_has_no_members(tmp_path, monkeypatch):
    """The leader exiting while a rollout/claude child is still in its
    process group is exactly the case --stop exists for, so this must not
    pre-check the leader's own liveness (_is_alive) and skip the kill —
    POSIX never reuses a pgid while it still has live members, so the
    kill itself (not a liveness pre-check) is the correct signal of
    whether anything is left to stop."""
    project_dir = tmp_path / ".capevolve" / "project"
    project_dir.mkdir(parents=True)
    capevolve._write_campaign_manifest(project_dir, {
        "pid": 999, "create_time": "123", "run_dir": str(tmp_path / "run_x"),
    })
    # Same host-dependence as above: pin the dead leader so the recycled-pid
    # guard passes through to the killpg the test exercises.
    monkeypatch.setattr(capevolve, "_process_create_time", lambda pid: None)

    def _raise(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(capevolve.os, "killpg", _raise)

    result = capevolve.stop_campaign(project_dir)

    assert result == {"status": "already_stopped", "pid": 999}


def test_stop_campaign_refuses_a_recycled_pid(tmp_path, monkeypatch):
    """astefanutti #6: campaign.json outlives its campaign (nothing deletes
    it), and once the group's last member exits the pid is reusable — a
    later --stop must not SIGTERM whatever unrelated process-group leader
    now owns that pid. The manifest's create_time marker distinguishes a
    recycled pid from a live campaign, and unlike an _is_alive pre-check it
    does not break the leader-exited-children-alive case above (a dead
    leader has no create_time to read)."""
    project_dir = tmp_path / ".capevolve" / "project"
    project_dir.mkdir(parents=True)
    capevolve._write_campaign_manifest(project_dir, {
        "pid": os.getpid(), "create_time": "recorded-at-launch",
        "run_dir": str(tmp_path / "run_x"),
    })
    # The pid is alive but its create_time no longer matches: recycled.
    monkeypatch.setattr(capevolve, "_process_create_time",
                        lambda pid: "a-different-process-now")
    killed = []
    monkeypatch.setattr(capevolve.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    result = capevolve.stop_campaign(project_dir)

    assert result == {"status": "already_stopped", "pid": os.getpid()}
    assert killed == []


def test_cap_evolve_children_get_the_resolved_skills_dir(tmp_path):
    env = capevolve._child_env(tmp_path, "/data/cap-evolve/sha/skills")
    assert env["CAPEVOLVE_SKILLS_DIR"] == "/data/cap-evolve/sha/skills"


# ── Dry-run model preflight + --status measurement coverage ──────────────────

def test_dry_run_reports_scope_and_models(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _bare_benchmark(tmp_path)

    result = capevolve.prepare_project(max_usd=5, optimizer="mock")

    assert set(result["models"]) == {"skill", "subagent", "judge", "hook"}
    assert len(result["candidate_sources"]) == len(result["candidate_components"])
    assert isinstance(result["warnings"], list)


def test_openrouter_model_without_a_key_is_flagged(tmp_path, monkeypatch):
    from agent_eval.config import EvalConfig
    _demo_eval_yaml(tmp_path)
    config = EvalConfig.from_yaml(tmp_path / "eval.yaml")
    config.models.skill = "openrouter:/z-ai/glm-5.3"
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MANAGEMENT_KEY", raising=False)

    models, warnings = capevolve._model_preflight(config)

    assert models["skill"] == "openrouter:/z-ai/glm-5.3"
    assert any("models.skill" in w and "OPENROUTER_API_KEY" in w for w in warnings)

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    _models, warnings = capevolve._model_preflight(config)
    assert not any("OPENROUTER" in w for w in warnings)


def _finished_campaign(tmp_path, final: dict) -> Path:
    project_dir = tmp_path / ".capevolve" / "project"
    run_dir = tmp_path / ".capevolve" / "run_1"
    project_dir.mkdir(parents=True)
    run_dir.mkdir()
    (run_dir / "final.json").write_text(json.dumps(final), encoding="utf-8")
    (project_dir / "campaign.json").write_text(json.dumps({
        "pid": 2 ** 22 + 12345, "create_time": "gone", "run_dir": str(run_dir),
        "log_file": str(project_dir / "campaign-1.log"),
    }), encoding="utf-8")
    return project_dir


def _split_summary(n_tasks: int, n_scored: int) -> dict:
    return {split: {"n_tasks": n_tasks, "n_scored": n_scored} for split in ("train", "val", "test")}


def test_status_flags_a_campaign_that_measured_nothing(tmp_path):
    """A wrong model id errors every rollout; errors are missing data, so the
    campaign still finishes — --status must not let that pass as a result."""
    project_dir = _finished_campaign(tmp_path, {
        "best_id": "seed", "seed": _split_summary(1, 0), "best": _split_summary(1, 0)})

    status = capevolve.read_status(project_dir)

    assert status["status"] == "finished"
    assert status["measurements"] == {"n_tasks": 6, "n_scored": 0}
    assert "meaningless" in status["warnings"][0]
    assert "campaign-1.log" in status["warnings"][0]


def test_status_flags_partial_measurement(tmp_path):
    project_dir = _finished_campaign(tmp_path, {
        "seed": _split_summary(2, 2), "best": _split_summary(2, 1)})

    status = capevolve.read_status(project_dir)

    assert status["measurements"] == {"n_tasks": 12, "n_scored": 9}
    assert status["warnings"] == [
        "3 of 12 evaluations produced no measurement (missing data, not 0): "
        "the gate decided on fewer cases than planned"]


def test_status_is_quiet_when_every_evaluation_measured(tmp_path):
    project_dir = _finished_campaign(tmp_path, {
        "seed": _split_summary(1, 1), "best": _split_summary(1, 1)})

    status = capevolve.read_status(project_dir)

    assert status["measurements"] == {"n_tasks": 6, "n_scored": 6}
    assert status["warnings"] == []


def test_status_warns_when_val_split_is_too_small_for_the_gate(tmp_path):
    """With fewer than 2 val cases the significance gate degenerates to
    'accept any positive delta' (SE=0). --status must surface this."""
    project_dir = _finished_campaign(tmp_path, {
        "seed": _split_summary(1, 1), "best": _split_summary(1, 1)})
    run_dir = tmp_path / ".capevolve" / "run_1"
    # The spec the harness itself wrote — num_trials comes from here, never
    # from cap-evolve's state.json (which carries no spec key).
    (project_dir / "capevolve.yaml").write_text(
        "num_trials: 1\n", encoding="utf-8")
    (run_dir / "splits.json").write_text(
        json.dumps({"train": ["c1", "c2"], "val": ["c3"], "test": ["c4"]}),
        encoding="utf-8")

    status = capevolve.read_status(project_dir)

    val_warnings = [w for w in status["warnings"] if "significance gate" in w]
    assert val_warnings, f"expected a val-size warning, got: {status['warnings']}"
    assert "SE=0" in val_warnings[0]


def test_status_is_quiet_about_val_size_with_enough_cases(tmp_path):
    """With >= 2 val cases the gate is meaningful; no extra warning."""
    project_dir = _finished_campaign(tmp_path, {
        "seed": _split_summary(1, 1), "best": _split_summary(1, 1)})
    run_dir = tmp_path / ".capevolve" / "run_1"
    (project_dir / "capevolve.yaml").write_text(
        "num_trials: 1\n", encoding="utf-8")
    (run_dir / "splits.json").write_text(
        json.dumps({"train": ["c1", "c2"], "val": ["c3", "c4"], "test": ["c5"]}),
        encoding="utf-8")

    status = capevolve.read_status(project_dir)

    val_warnings = [w for w in status["warnings"] if "significance gate" in w]
    assert not val_warnings, f"unexpected val-size warning: {val_warnings}"


def test_status_val_size_warning_fires_even_at_multiple_trials(tmp_path):
    """More trials do NOT rescue a one-case val split, so the warning must
    still fire at num_trials > 1.

    The per-trial SE floor only reaches the gate for a restricted footprint
    (harness.py: `if fp is not None and paired_deltas`), and footprint()
    returns None when the footprint covers every measured task — which a
    single val task always does. So `se` stays 0.0 and the gate takes its
    STRICT "accept any positive delta" branch whatever num_trials is.
    An earlier revision suppressed the note here; this pins that it must not."""
    project_dir = _finished_campaign(tmp_path, {
        "seed": _split_summary(1, 1), "best": _split_summary(1, 1)})
    run_dir = tmp_path / ".capevolve" / "run_1"
    (project_dir / "capevolve.yaml").write_text(
        "num_trials: 3\n", encoding="utf-8")
    (run_dir / "splits.json").write_text(
        json.dumps({"train": ["c1", "c2"], "val": ["c3"], "test": ["c4"]}),
        encoding="utf-8")

    status = capevolve.read_status(project_dir)

    val_warnings = [w for w in status["warnings"] if "significance gate" in w]
    assert val_warnings, "val-size warning must fire at num_trials=3 too"


def test_apply_winner_caveat_flags_a_val_split_too_small_for_the_gate(tmp_path):
    """astefanutti: the --apply half of the val-size note had no test (the
    block could be deleted with the suite green) and no num_trials
    condition of its own. It now shares _val_size_note with --status, so
    the same campaign yields the same verdict on both surfaces."""
    project_root, harness_manifest, campaign_manifest, _ = _apply_fixture(tmp_path)
    # The fixture freezes a 2/1/1 split — one val case — and the spec the
    # harness wrote lives at project_root/.capevolve/project/capevolve.yaml.
    spec_dir = project_root / ".capevolve" / "project"
    spec_dir.mkdir(parents=True)
    (spec_dir / "capevolve.yaml").write_text("num_trials: 1\n", encoding="utf-8")

    result = capevolve.apply_winner(
        project_root=project_root,
        harness_manifest=harness_manifest, campaign_manifest=campaign_manifest,
    )

    assert "significance gate" in result["caveat"]
    assert "SE=0" in result["caveat"]


def test_apply_winner_caveat_flags_val_size_at_multiple_trials_too(tmp_path):
    """--apply shares _val_size_note with --status, so it must reach the same
    verdict on the same campaign — including at num_trials > 1, where the
    gate still degenerates on a one-case val split."""
    project_root, harness_manifest, campaign_manifest, _ = _apply_fixture(tmp_path)
    spec_dir = project_root / ".capevolve" / "project"
    spec_dir.mkdir(parents=True)
    (spec_dir / "capevolve.yaml").write_text("num_trials: 3\n", encoding="utf-8")

    result = capevolve.apply_winner(
        project_root=project_root,
        harness_manifest=harness_manifest, campaign_manifest=campaign_manifest,
    )

    assert "significance gate" in result["caveat"]


def test_override_inside_an_unrelated_repo_is_not_misjudged(tmp_path):
    """Skills copied into some other git repo (a dotfiles ~/.claude, say) carry
    that repo's HEAD, which says nothing about the cap-evolve version."""
    dotfiles = tmp_path / "dotfiles"
    skills = _skills_tree(dotfiles / ".claude")
    _init_git_repo(dotfiles)
    _git_commit_all(dotfiles, "dotfiles")

    result = capevolve.resolve_skills_dir(str(skills))

    assert result["source"] == "override"
    assert "could not verify" in result["warnings"][0]


def test_override_without_git_on_path_is_unverified_not_a_crash(tmp_path, monkeypatch):
    skills = _skills_tree(tmp_path / "checkout")

    def no_git(*args, **kwargs):
        raise FileNotFoundError("git")
    monkeypatch.setattr(capevolve.subprocess, "run", no_git)

    assert capevolve.resolve_skills_dir(str(skills))["warnings"]


@pytest.mark.parametrize("member", [
    {"name": "skills/link", "type": "sym", "linkname": "../../outside"},
    {"name": "skills/abs", "type": "sym", "linkname": "/etc/passwd"},
    {"name": "skills/fifo", "type": "fifo"},
])
def test_fetch_refuses_unsafe_links_and_special_files(tmp_path, monkeypatch, member):
    import io
    import tarfile
    sha = "f" * 40
    archive = tmp_path / "src.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        manifest = tarfile.TarInfo(f"cap-evolve-{sha}/skills/_registry/manifest.json")
        manifest.size = 2
        tar.addfile(manifest, io.BytesIO(b"{}"))
        info = tarfile.TarInfo(f"cap-evolve-{sha}/{member['name']}")
        info.type = {"sym": tarfile.SYMTYPE, "fifo": tarfile.FIFOTYPE}[member["type"]]
        info.linkname = member.get("linkname", "")
        tar.addfile(info)
    monkeypatch.setattr(capevolve.urllib.request, "urlopen", lambda url, timeout: open(archive, "rb"))
    dest = tmp_path / "data" / sha

    with pytest.raises(RuntimeError, match="escapes|not a file"):
        _REAL_FETCH_TREE(sha, dest)
    assert not dest.exists()
