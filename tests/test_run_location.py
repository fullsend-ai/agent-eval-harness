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
import yaml

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


def test_eval_run_skill_md_launches_execute_with_run_dir():
    """The default launch must hand execute.py the resolved --run-dir.

    Only an explicit --run-dir/--output lets execute.py write its Stop-guard
    pidfile before the heavy imports; --config + --run-id alone would leave the
    startup window unprotected.
    """
    text = (ROOT / "skills" / "eval-run" / "SKILL.md").read_text()
    block = text.split("scripts/execute.py", 1)[1].split("```", 1)[0]
    assert "--run-dir <run-dir>" in block
    assert "--run-id" not in block


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
    assert "must be below the current directory" in proc.stderr, proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "CLEAN" not in proc.stdout
    assert cfg.is_file()


@pytest.mark.parametrize("spec", [".", "..", "cwd"])
def test_run_dir_must_be_below_the_current_directory(tmp_path, runs, monkeypatch, spec):
    """``.``/``..``/the cwd would write run artifacts into the project or above it."""
    config = EvalConfig.from_yaml(_config(tmp_path, _SKILL_AND_NAME))
    cwd = tmp_path / "proj" / "sub"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    target = str(cwd) if spec == "cwd" else spec
    with pytest.raises(RunLocationError, match="must be below the current directory"):
        resolve_run_dir(config, None, target)
    # A directory below the cwd is still fine.
    assert resolve_run_dir(config, None, "r1") == (Path("r1"), "r1")


def test_execute_does_not_write_a_pidfile_for_run_dir_dot(tmp_path, runs):
    project = tmp_path / "proj"
    project.mkdir()
    cfg = _config(project, _SKILL_AND_NAME)
    proc = _run(SCRIPTS / "execute.py", "--config", cfg, "--workspace", "ws",
                "--model", "m", "--run-dir", ".", cwd=project)
    assert proc.returncode != 0
    assert "must be below the current directory" in proc.stderr
    assert not (project / "execute.pid").exists()
    assert not (project / "cases").exists()


def _mk_run(path):
    path.mkdir(parents=True)
    (path / "run_result.json").write_text("{}\n")


def _clean(cfg, run_dir, cwd):
    return _run(SCRIPTS / "preflight.py", "--config", cfg, "--run-dir", run_dir,
                "--clean", "--force", cwd=cwd)


def test_preflight_never_cleans_a_runs_root(tmp_path, runs):
    project = tmp_path / "proj"
    project.mkdir()
    cfg = _config(project, _SKILL_AND_NAME)
    made = [project / "runs" / ev / rid for ev in ("plugin:foo", "other")
            for rid in ("r1", "r2")]
    for d in made:
        _mk_run(d)
    proc = _clean(cfg, project / "runs", project)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "CLEAN" not in proc.stdout
    assert "not a run directory" in proc.stderr
    assert all(d.is_dir() and (d / "run_result.json").is_file() for d in made)


def test_preflight_never_cleans_an_eval_name_directory(tmp_path, runs):
    project = tmp_path / "proj"
    project.mkdir()
    # A plain eval name: "plugin:foo" fails the run-id pattern in the resolver
    # before the marker rule is reached, so it would not exercise the rule.
    cfg = _config(project, _PROMPT_NAME_ONLY)
    evaldir = project / "runs" / "foo-eval"
    _mk_run(evaldir / "r1")
    _mk_run(evaldir / "baseline")
    proc = _clean(cfg, evaldir, project)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "CLEAN" not in proc.stdout
    assert (evaldir / "baseline" / "run_result.json").is_file()
    assert (evaldir / "r1" / "run_result.json").is_file()


def test_preflight_still_cleans_a_marked_run_directory(tmp_path, runs):
    project = tmp_path / "proj"
    project.mkdir()
    cfg = _config(project, _SKILL_AND_NAME)
    run = project / "runs" / "plugin:foo" / "r1"
    _mk_run(run)
    sibling = project / "runs" / "plugin:foo" / "baseline"
    _mk_run(sibling)
    proc = _clean(cfg, run, project)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.rstrip().endswith("CLEAN")
    assert not run.exists()
    assert sibling.is_dir()


def test_preflight_cleans_a_run_inside_a_state_directory(tmp_path, runs):
    project = tmp_path / "proj"
    run = project / "tmp" / "r1"
    run.mkdir(parents=True)
    (run / "summary.yaml").write_text("judges: {}\n")
    cfg = _config(project, _SKILL_AND_NAME)
    proc = _run(SCRIPTS / "preflight.py", "--config", cfg, "--run-dir", run,
                "--clean", "--force", cwd=project)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stderr
    assert proc.stdout.rstrip().endswith("CLEAN")
    assert not run.exists()


def test_preflight_refuses_to_clean_a_symlinked_run_dir(tmp_path, runs):
    project = tmp_path / "proj"
    real = tmp_path / "real" / "r1"
    real.mkdir(parents=True)
    (real / "summary.yaml").write_text("judges: {}\n")
    project.mkdir()
    link = project / "r1"
    link.symlink_to(real, target_is_directory=True)
    state = project / "tmp"
    state.mkdir()
    (state / "stale.txt").write_text("x")
    cfg = _config(project, _SKILL_AND_NAME)
    proc = _run(SCRIPTS / "preflight.py", "--config", cfg, "--run-dir", link,
                "--clean", "--force", cwd=project)
    assert proc.returncode == 1 and "symlink" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert (real / "summary.yaml").exists()
    assert (state / "stale.txt").exists()  # nothing cleaned before refusing


def _step0_state(project):
    """What /eval-optimize and /eval-run Step 0 leave in tmp/ (issue #339)."""
    state_py = ROOT / "agent_eval" / "state.py"
    for args in (("tmp/optimize-config.yaml", "target_judge=done"),
                 ("tmp/eval-config.yaml", "model=opus", "run_id=r1")):
        proc = _run(state_py, "init", *args, cwd=project)
        assert proc.returncode == 0, proc.stderr


def _read_state(project, name):
    proc = _run(ROOT / "agent_eval" / "state.py", "read", f"tmp/{name}", cwd=project)
    assert proc.returncode == 0, proc.stderr
    return yaml.safe_load(proc.stdout)


def test_preflight_ignores_the_skills_own_state_files(tmp_path, runs):
    """Step 0's own tmp/eval-config.yaml is not a stale artifact."""
    project = tmp_path / "proj"
    project.mkdir()
    cfg = _config(project, _SKILL_AND_NAME)
    _step0_state(project)
    proc = _run(SCRIPTS / "preflight.py", "--config", cfg, "--run-id", "r1",
                cwd=project)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "CLEAN"


def test_preflight_clean_keeps_the_skills_own_state_files(tmp_path, runs):
    """--clean removes what it reported, never a (parent) skill's state."""
    project = tmp_path / "proj"
    project.mkdir()
    cfg = _config(project, _SKILL_AND_NAME)
    _step0_state(project)
    stale = project / "tmp" / "old" / "stale.txt"
    stale.parent.mkdir()
    stale.write_text("x")
    check = _run(SCRIPTS / "preflight.py", "--config", cfg, "--run-id", "r1",
                 cwd=project)
    assert check.returncode == 1 and "DIRTY" in check.stdout
    assert "tmp: 1 files — stale.txt" in check.stdout
    proc = _run(SCRIPTS / "preflight.py", "--config", cfg, "--run-id", "r1",
                "--clean", "--force", cwd=project)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.rstrip().endswith("CLEAN")
    assert not stale.exists() and not stale.parent.exists()
    assert (project / "tmp").is_dir()
    assert _read_state(project, "eval-config.yaml") == {"model": "opus", "run_id": "r1"}
    assert _read_state(project, "optimize-config.yaml") == {"target_judge": "done"}


@pytest.mark.parametrize("target", ["external", "project"])
def test_preflight_refuses_a_symlinked_state_directory(tmp_path, runs, target):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "keep.txt").write_text("x")
    external = tmp_path / "external"
    external.mkdir()
    (external / "other.txt").write_text("x")
    (project / "tmp").symlink_to(
        external if target == "external" else project, target_is_directory=True)
    cfg = _config(project, _SKILL_AND_NAME)
    for extra in ((), ("--clean", "--force")):
        proc = _run(SCRIPTS / "preflight.py", "--config", cfg, *extra, cwd=project)
        assert proc.returncode == 1 and "symlink" in proc.stderr
        assert "CLEAN" not in proc.stdout and "Traceback" not in proc.stderr
    assert (external / "other.txt").exists()
    assert (project / "keep.txt").exists()


def test_preflight_refuses_to_clean_a_run_dir_that_is_a_state_directory(tmp_path, runs):
    """A run dir equal to tmp/ must not be removed along with the owned state."""
    project = tmp_path / "proj"
    project.mkdir()
    cfg = _config(project, _SKILL_AND_NAME)
    _step0_state(project)
    (project / "tmp" / "summary.yaml").write_text("judges: {}\n")
    proc = _run(SCRIPTS / "preflight.py", "--config", cfg, "--run-dir", "tmp",
                "--clean", "--force", cwd=project)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "CLEAN" not in proc.stdout
    assert "Traceback" not in proc.stderr
    assert "state directory" in proc.stderr and "tmp" in proc.stderr
    assert _read_state(project, "eval-config.yaml") == {"model": "opus", "run_id": "r1"}
    assert _read_state(project, "optimize-config.yaml") == {"target_judge": "done"}


def test_preflight_cleans_a_state_run_with_an_empty_subdirectory(tmp_path, runs):
    project = tmp_path / "proj"
    run = project / "tmp" / "r1"
    (run / "artifacts").mkdir(parents=True)
    (run / "summary.yaml").write_text("judges: {}\n")
    cfg = _config(project, _SKILL_AND_NAME)
    proc = _run(SCRIPTS / "preflight.py", "--config", cfg, "--run-dir", run,
                "--clean", "--force", cwd=project)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "not a run directory" not in proc.stderr
    assert proc.stdout.rstrip().endswith("CLEAN")
    assert not run.exists()


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


def _skill_md():
    return (ROOT / "skills" / "eval-run" / "SKILL.md").read_text()


def test_explicit_run_dir_is_authoritative_in_eval_run_skill_md():
    """--run-dir wins outright: discovery only runs when no override is given."""
    text = _skill_md()
    section = text.split("### Run Directory", 1)[1].split("Persist parsed flags", 1)[0]
    assert "authoritative" in section
    assert "do not run discovery" in section
    assert "`--run-id`" in section and "differs from that basename" in section
    assert "stop with an error" in section
    assert "`--run-dir` alone is valid" in section
    assert "Otherwise" in section and "discover.py" in section


def test_eval_run_resolves_run_dir_only_after_config_exists():
    """The run directory needs the config, so it comes after the bootstrap."""
    text = _skill_md()
    assert (text.index("bootstrap via `/eval-analyze`")
            < text.index("### Run Directory")
            < text.index("Persist parsed flags"))


def test_eval_run_hands_the_run_dir_to_every_later_script():
    text = _skill_md()
    for script in ("preflight.py", "collect.py", "report.py"):
        block = text.split(f"scripts/{script}", 1)[1].split("```", 1)[0]
        assert "--run-dir <run-dir>" in block, script
        assert "--run-id" not in block, script
    for sub in ("judges", "pairwise"):
        block = text.split(f"scripts/score.py {sub}", 1)[1].split("```", 1)[0]
        assert "--run-dir <run-dir>" in block, sub
        assert "--run-id" not in block, sub
    assert "/eval-mlflow --action log-results --run-dir <run-dir>" in text


def test_eval_mlflow_skill_documents_run_dir():
    text = (ROOT / "skills" / "eval-mlflow" / "SKILL.md").read_text()
    assert "| `--run-dir <path>`" in text
    assert text.count("<run-selector>") >= 3


@pytest.mark.parametrize("script", ["log_results.py", "attach_feedback.py"])
def test_mlflow_scripts_resolve_through_the_shared_resolver(script):
    src = (ROOT / "skills" / "eval-mlflow" / "scripts" / script).read_text()
    assert "resolve_run_dir(config, args.run_id, args.run_dir)" in src
    assert "os.environ" not in src


def test_log_results_uses_the_explicit_run_dir(tmp_path, runs):
    pytest.importorskip("mlflow")
    cfg = _config(tmp_path, _SKILL_AND_NAME)
    elsewhere = tmp_path / "elsewhere" / "my-run"
    elsewhere.mkdir(parents=True)
    proc = _run(ROOT / "skills" / "eval-mlflow" / "scripts" / "log_results.py",
                "--config", cfg, "--run-dir", elsewhere, cwd=tmp_path)
    out = proc.stdout + proc.stderr
    assert str(elsewhere.resolve()) in out
    assert str(runs) not in out


@pytest.mark.parametrize("bad", ["my run", "run:1", "run#1", "résumé", "a$b"])
def test_run_dir_basename_must_be_a_valid_run_id(tmp_path, runs, bad):
    """The basename is the run id, so workspace.py's rule applies up front."""
    config = EvalConfig.from_yaml(_config(tmp_path, _SKILL_AND_NAME))
    with pytest.raises(RunLocationError, match=r"\[A-Za-z0-9\._-\]"):
        resolve_run_dir(config, None, tmp_path / "ci" / bad)


@pytest.mark.parametrize("bad", ["my run", "run:1", "run#1", "résumé", "a$b"])
def test_run_id_must_be_a_valid_run_id(tmp_path, runs, bad):
    """--run-id gets the same rule as a --run-dir basename (workspace.py's)."""
    config = EvalConfig.from_yaml(_config(tmp_path, _SKILL_AND_NAME))
    with pytest.raises(RunLocationError, match=r"\[A-Za-z0-9\._-\]"):
        resolve_run_dir(config, bad)
    with pytest.raises(RunLocationError, match=r"\[A-Za-z0-9\._-\]"):
        default_run_dir(config, bad)


def test_execute_rejects_an_invalid_run_id_without_creating_a_directory(tmp_path, runs):
    cfg = _config(tmp_path, _SKILL_AND_NAME)
    proc = _run(SCRIPTS / "execute.py", "--config", cfg, "--workspace", "ws",
                "--model", "m", "--run-id", "my run", cwd=tmp_path)
    assert proc.returncode != 0
    assert "must match [A-Za-z0-9._-]+" in proc.stderr
    assert not runs.exists()
    assert not (tmp_path / "my run").exists()


@pytest.mark.parametrize("script,extra", [
    ("execute.py", ["--workspace", "ws", "--model", "m"]),
    ("preflight.py", []),
])
def test_scripts_reject_an_invalid_run_dir_basename(tmp_path, runs, script, extra):
    cfg = _config(tmp_path, _SKILL_AND_NAME)
    proc = _run(SCRIPTS / script, "--config", cfg, *extra,
                "--run-dir", tmp_path / "bad name", cwd=tmp_path)
    assert proc.returncode != 0
    assert "must match [A-Za-z0-9._-]+" in proc.stderr
    assert not (tmp_path / "bad name").exists()


def _review_md():
    return (ROOT / "skills" / "eval-review" / "SKILL.md").read_text()


def test_eval_review_accepts_an_authoritative_run_dir():
    text = _review_md()
    assert "| `--run-dir <path>`" in text
    section = text.split("### Run Directory", 1)[1].split("## Step 1", 1)[0]
    assert "authoritative" in section
    assert "do not run discovery" in section
    assert "differs from that basename" in section
    assert "stop with an error" in section
    assert "discover.py" in section


def test_eval_review_reads_and_writes_inside_the_run_dir():
    text = _review_md()
    assert "$AGENT_EVAL_RUNS_DIR/<eval-name>" not in text
    assert "Write `<run-dir>/review.yaml`" in text
    assert "<run-dir>/summary.yaml" in text
    assert "--run-dir <run-dir> --action push-feedback" in text


def test_eval_run_hands_the_run_dir_to_eval_review():
    text = _skill_md()
    assert "/eval-review --run-dir <run-dir>" in text
    assert "/eval-review --run-id" not in text


def test_run_id_rule_matches_workspace_and_early_pidfile():
    """One run-id rule: the resolver, workspace.py and execute.py's early write."""
    from agent_eval.run_location import RUN_ID_PATTERN
    for script in ("workspace.py", "execute.py"):
        assert f'r"{RUN_ID_PATTERN.pattern}"' in (SCRIPTS / script).read_text(), script
