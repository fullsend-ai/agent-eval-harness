"""One run directory, resolved once (issue #309).

execute.py, collect.py, score.py and discover.py all resolve the run directory
through agent_eval.run_location from --config + --run-id. Before, execute.py
and collect.py wrote wherever the orchestrator's --output pointed while
score.py derived $AGENT_EVAL_RUNS_DIR/<eval_name>/<run-id> itself, and the two
disagreed whenever the orchestrator built --output from the config `name`.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_eval.config import EvalConfig
from agent_eval.run_location import (RunLocationError, default_run_dir,
                                     resolve_run_dir)

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "eval-run" / "scripts"

_SKILL_AND_NAME = """\
name: foo-eval
execution:
  skill: plugin:foo
dataset:
  path: cases
"""

_PROMPT_NAME_ONLY = """\
name: foo-eval
execution:
  prompt: "{{ input.prompt }}"
dataset:
  path: cases
"""


def _config(tmp_path, text, name="eval.yaml"):
    p = tmp_path / name
    p.write_text(text)
    return p


@pytest.fixture
def runs(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(root))
    return root


def test_default_is_runs_root_eval_name_run_id(tmp_path, runs):
    config = EvalConfig.from_yaml(_config(tmp_path, _SKILL_AND_NAME))
    # skill wins over name: precedence is unchanged, only computed in one place
    assert default_run_dir(config, "r1") == runs / "plugin:foo" / "r1"
    assert resolve_run_dir(config, "r1") == (runs / "plugin:foo" / "r1", "r1")


def test_prompt_mode_uses_the_name(tmp_path, runs):
    config = EvalConfig.from_yaml(_config(tmp_path, _PROMPT_NAME_ONLY))
    assert resolve_run_dir(config, "r1")[0] == runs / "foo-eval" / "r1"


def test_run_dir_override_basename_is_the_run_id(tmp_path, runs):
    config = EvalConfig.from_yaml(_config(tmp_path, _SKILL_AND_NAME))
    target = tmp_path / "ci" / "job-7"
    assert resolve_run_dir(config, None, target) == (target, "job-7")
    assert resolve_run_dir(config, "job-7", target) == (target, "job-7")


def test_conflicting_run_id_and_run_dir_fail(tmp_path, runs):
    config = EvalConfig.from_yaml(_config(tmp_path, _SKILL_AND_NAME))
    with pytest.raises(RunLocationError, match="conflicts"):
        resolve_run_dir(config, "other", tmp_path / "ci" / "job-7")


@pytest.mark.parametrize("bad", ["", None, "..", "a/b", "a\\b", "x\ny"])
def test_missing_or_unsafe_run_id_fails(tmp_path, runs, bad):
    config = EvalConfig.from_yaml(_config(tmp_path, _SKILL_AND_NAME))
    with pytest.raises(RunLocationError):
        resolve_run_dir(config, bad)


def _run(script, *args, cwd):
    return subprocess.run([sys.executable, str(script), *map(str, args)],
                          cwd=cwd, capture_output=True, text=True, timeout=120)


def test_discover_prints_the_directory_the_scripts_use(tmp_path, runs):
    cfg = _config(tmp_path, _SKILL_AND_NAME)
    proc = _run(ROOT / "scripts" / "discover.py", "--config", cfg, "--run-id", "r1",
                cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    config = EvalConfig.from_yaml(cfg)
    assert Path(proc.stdout.strip()) == resolve_run_dir(config, "r1")[0]


def test_discover_requires_both_config_and_run_id(tmp_path, runs):
    cfg = _config(tmp_path, _SKILL_AND_NAME)
    proc = _run(ROOT / "scripts" / "discover.py", "--config", cfg, cwd=tmp_path)
    assert proc.returncode != 0


def _batch_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "case_order.yaml").write_text("- case-1\n")
    return ws


def test_collect_writes_to_the_resolved_directory(tmp_path, runs):
    cfg = _config(tmp_path, _SKILL_AND_NAME.replace(
        "execution:\n", "execution:\n  mode: batch\n"))
    ws = _batch_workspace(tmp_path)
    proc = _run(SCRIPTS / "collect.py", "--config", cfg, "--workspace", ws,
                "--run-id", "r1", cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert (runs / "plugin:foo" / "r1" / "collection.json").is_file()
    # ...which is exactly where score.py looks
    sys.path.insert(0, str(SCRIPTS))
    import score
    args = SimpleNamespace(run_id="r1", run_dir=None)
    runs_dir, run_id = score._run_location(args, EvalConfig.from_yaml(cfg))
    assert runs_dir / run_id == runs / "plugin:foo" / "r1"


def test_collect_rejects_a_conflicting_run_dir(tmp_path, runs):
    cfg = _config(tmp_path, _SKILL_AND_NAME)
    ws = _batch_workspace(tmp_path)
    proc = _run(SCRIPTS / "collect.py", "--config", cfg, "--workspace", ws,
                "--run-id", "r1", "--run-dir", tmp_path / "elsewhere" / "r2",
                cwd=tmp_path)
    assert proc.returncode != 0
    assert "conflicts" in proc.stderr
    assert not (tmp_path / "elsewhere").exists()


def test_execute_rejects_a_conflicting_output(tmp_path, runs):
    cfg = _config(tmp_path, _SKILL_AND_NAME)
    proc = _run(SCRIPTS / "execute.py", "--config", cfg, "--workspace", tmp_path,
                "--model", "m", "--run-id", "r1",
                "--output", runs / "foo-eval" / "r2", cwd=tmp_path)
    assert proc.returncode != 0
    assert "conflicts" in proc.stderr


def test_execute_requires_a_run_id_or_run_dir(tmp_path, runs):
    cfg = _config(tmp_path, _SKILL_AND_NAME)
    proc = _run(SCRIPTS / "execute.py", "--config", cfg, "--workspace", tmp_path,
                "--model", "m", cwd=tmp_path)
    assert proc.returncode != 0
    assert "--run-id" in proc.stderr


def _load_execute():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("_execute_run_location",
                                                  SCRIPTS / "execute.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prompt_mode_records_no_stray_skill(tmp_path, runs):
    execute = _load_execute()
    config = EvalConfig.from_yaml(_config(tmp_path, _PROMPT_NAME_ONLY))
    args = SimpleNamespace(skill="address-ci-failures", mlflow_experiment=None)
    params = execute._build_eval_params(args, config, "", 1.0, 10)
    assert "skill" not in params
    skill_cfg = EvalConfig.from_yaml(_config(tmp_path, _SKILL_AND_NAME, "skill.yaml"))
    args = SimpleNamespace(skill=None, mlflow_experiment=None)
    assert execute._build_eval_params(args, skill_cfg, "", 1.0, 10)["skill"] == "plugin:foo"


def test_eval_run_skill_md_never_builds_a_run_path():
    text = (ROOT / "skills" / "eval-run" / "SKILL.md").read_text()
    assert "<eval-name>" not in text
    assert "read its `skill` field" not in text
    assert json.dumps("discover.py --config <config> --run-id <id>")[1:-1] in text


def test_preflight_checks_the_resolved_directory(tmp_path, runs):
    cfg = _config(tmp_path, _SKILL_AND_NAME)
    run = runs / "plugin:foo" / "r1"
    run.mkdir(parents=True)
    (run / "summary.yaml").write_text("{}\n")
    proc = _run(SCRIPTS / "preflight.py", "--config", cfg, "--run-id", "r1", cwd=tmp_path)
    assert proc.returncode == 1 and "DIRTY" in proc.stdout, proc.stderr
    elsewhere = tmp_path / "ci" / "r1"
    proc = _run(SCRIPTS / "preflight.py", "--config", cfg, "--run-dir", elsewhere,
                cwd=tmp_path)
    assert proc.returncode == 0 and "CLEAN" in proc.stdout, proc.stderr
    proc = _run(SCRIPTS / "preflight.py", "--config", cfg, "--run-id", "r2",
                "--run-dir", elsewhere, cwd=tmp_path)
    assert proc.returncode != 0 and "conflicts" in proc.stderr


def test_regression_accepts_run_dir(tmp_path, runs):
    cfg = _config(tmp_path, _SKILL_AND_NAME)
    run = tmp_path / "ci" / "r1"
    run.mkdir(parents=True)
    (run / "summary.yaml").write_text("judges: {}\n")
    proc = _run(SCRIPTS / "score.py", "regression", "--config", cfg, "--run-dir", run,
                cwd=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    missing = _run(SCRIPTS / "score.py", "regression", "--config", cfg, "--run-id", "r1",
                   cwd=tmp_path)
    assert missing.returncode != 0


def test_preflight_never_cleans_the_project_itself(tmp_path, runs):
    project = tmp_path / "proj"
    project.mkdir()
    cfg = _config(project, _SKILL_AND_NAME)
    proc = _run(SCRIPTS / "preflight.py", "--config", cfg, "--run-dir", ".",
                "--clean", "--force", cwd=project)
    assert "SKIPPED (dangerous)" in proc.stderr, proc.stdout + proc.stderr
    assert cfg.is_file()


def _render_params(eval_params):
    sys.path.insert(0, str(SCRIPTS))
    try:
        from report import _render_eval_params
        return _render_eval_params({"eval_params": eval_params})
    finally:
        sys.path.remove(str(SCRIPTS))


def test_report_never_invents_a_skill_name(tmp_path):
    """Prompt-mode runs record no `skill`; the report must not print `/skill`."""
    html = _render_params({"execution_mode": "case", "skill_args": "do the thing"})
    assert "/skill" not in html
    assert "do the thing" in html
    assert "Skill Args" not in html and "Arguments" in html


def test_report_shows_the_skill_invocation_when_recorded(tmp_path):
    html = _render_params({"skill": "plugin:foo", "skill_args": "--x 1"})
    assert "/plugin:foo --x 1" in html
    assert "Skill Args" in html
