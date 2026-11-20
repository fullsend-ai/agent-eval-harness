"""The eval dataset (answer keys) in locally staged workspaces (#345).

Two layers:

* The case workspace the fullsend runner uploads must hold no dataset content,
  even when a shared ``workspace.files`` source is a real directory that
  contains the dataset (the normal provisioning path, not a link).
* Local runners keep their project-root grant; the settings the harness writes
  also deny ``Read``/``Edit``/``Grep``/``Glob`` on the dataset as defense in
  depth. Bash is not path-scoped, so isolation has to come from the runner or
  its environment.

All tests run under a temp HOME and a temp project as cwd.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.agent.fullsend import FullsendRunner, materialize_symlinks  # noqa: E402
from agent_eval.config import EvalConfig  # noqa: E402
from agent_eval.harbor.run import _podman_project_hide_paths  # noqa: E402
from agent_eval.workspace_provisioning import (  # noqa: E402
    dataset_protected_paths,
    materialize_shared_files,
)
import workspace  # noqa: E402  (skills/eval-run/scripts via conftest)

KEY = "ANSWER-KEY-9c1d"
DATASET = "skills/demo/evals/cases"
TOOLS = [{"match": "AskUserQuestion", "prompt": "answer yes"}]


def _project(tmp_path, monkeypatch, *, runner="claude-code", shared=None,
             dataset=DATASET, tools=False):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project = (tmp_path / "project").resolve()
    case = project / DATASET / "case-001"
    case.mkdir(parents=True)
    (case / "input.yaml").write_text("prompt: hi\n")
    (case / "annotations.yaml").write_text(f"expected: {KEY}\n")
    (project / "skills" / "demo" / "SKILL.md").write_text("# demo\n")
    raw = {
        "name": "t",
        "execution": {"skill": "demo", "arguments": ""},
        "runner": {"type": runner, **({"agent": "a"} if runner == "fullsend" else {})},
        "dataset": {"path": dataset},
    }
    if shared:
        raw["dataset"]["workspace"] = {"files": shared}
    if tools:
        raw["inputs"] = {"tools": TOOLS}
    cfg_path = project / "eval.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    monkeypatch.chdir(project)
    return project, EvalConfig.from_yaml(cfg_path)


def _stage(tmp_path, project, config):
    ws = tmp_path / "ws"
    ws.mkdir()
    workspace._create_per_case_workspace(
        ws, [project / DATASET / "case-001"], config, SimpleNamespace(symlinks=None))
    return ws / "cases" / "case-001"


def _bash_finds_key(root):
    out = subprocess.run(
        ["bash", "-c", f"grep -rIl {KEY} . ; find . -name annotations.yaml"],
        cwd=root, capture_output=True, text=True)
    return out.stdout.strip()


SHARED = [{"dest": "demo", "source": "skills/demo"}]


class TestFullsendSharedFiles:
    def test_real_directory_source_holding_dataset_is_filtered(
            self, tmp_path, monkeypatch):
        project, config = _project(
            tmp_path, monkeypatch, runner="fullsend", shared=SHARED)
        case_ws = _stage(tmp_path, project, config)
        # The same pass the runner makes before upload.
        materialize_symlinks(case_ws, [project], [(project / DATASET).resolve()])

        assert (case_ws / "demo" / "SKILL.md").is_file()
        assert not (case_ws / "demo" / "evals" / "cases").exists()
        assert _bash_finds_key(case_ws) == ""

    def test_dataset_file_source_is_skipped(self, tmp_path, monkeypatch):
        project, config = _project(
            tmp_path, monkeypatch, runner="fullsend",
            shared=[{"dest": "gold.yaml",
                     "source": f"{DATASET}/case-001/annotations.yaml"}])
        case_ws = _stage(tmp_path, project, config)
        assert not (case_ws / "gold.yaml").exists()

    @pytest.mark.parametrize("runner", ["claude-code", "codex"])
    def test_filter_does_not_depend_on_the_declared_runner(
            self, tmp_path, monkeypatch, runner):
        # ``--agent fullsend`` (or a fullsend pipeline step) can hand a workspace
        # staged for another runner to fullsend: staging filters for every runner.
        project, config = _project(
            tmp_path, monkeypatch, runner=runner, shared=SHARED)
        case_ws = _stage(tmp_path, project, config)

        assert (case_ws / "demo" / "SKILL.md").is_file()
        assert not (case_ws / "demo" / "evals" / "cases").exists()
        assert _bash_finds_key(case_ws) == ""

    def test_upload_drops_a_real_copy_staged_by_another_route(
            self, tmp_path, monkeypatch):
        # A runner built without config knowledge of how the tree was staged:
        # the upload boundary itself removes byte-identical dataset copies.
        project, config = _project(tmp_path, monkeypatch, runner="fullsend")
        case_ws = tmp_path / "ws"
        copied = case_ws / "copied" / "evals"
        copied.mkdir(parents=True)
        shutil.copytree(project / DATASET, copied / "cases")
        (case_ws / "keep.txt").write_text("not an answer key\n")
        runner = FullsendRunner.from_config(config)

        runner._drop_dataset_copies(case_ws)

        assert _bash_finds_key(case_ws) == ""
        assert (case_ws / "keep.txt").is_file()
        # The case input a sandbox is meant to receive is not an answer key.
        assert (copied / "cases" / "case-001" / "input.yaml").is_file()


def _empty_path_project(tmp_path, monkeypatch, *, shared=None):
    """Cases sit next to eval.yaml: ``dataset.path`` is empty."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project = (tmp_path / "project").resolve()
    evals = project / "evals"
    for name in ("case-001", "case-002"):
        (evals / name).mkdir(parents=True)
        (evals / name / "input.yaml").write_text("prompt: hi\n")
        (evals / name / "annotations.yaml").write_text(f"expected: {KEY}\n")
    (project / "skills" / "demo").mkdir(parents=True)
    (project / "skills" / "demo" / "SKILL.md").write_text("# demo\n")
    raw = {
        "name": "t",
        "execution": {"skill": "demo", "arguments": ""},
        "dataset": {"path": ""},
    }
    if shared:
        raw["dataset"]["workspace"] = {"files": shared}
    cfg_path = evals / "eval.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    monkeypatch.chdir(project)
    return project, EvalConfig.from_yaml(cfg_path)


class TestEmptyDatasetPath:
    def test_protection_covers_the_cases_discovery_finds(
            self, tmp_path, monkeypatch):
        project, config = _empty_path_project(tmp_path, monkeypatch)
        # What /eval-run discovers (every subdirectory of the config dir) that
        # holds case files is what protection covers.
        found = sorted(d for d in config.resolve_path(config.dataset.path).iterdir()
                       if d.is_dir())
        assert dataset_protected_paths(config) == [d.resolve() for d in found]

    def test_shared_source_inside_a_case_is_skipped(self, tmp_path, monkeypatch):
        project, config = _empty_path_project(
            tmp_path, monkeypatch, shared=[
                {"dest": "demo", "source": "skills/demo"},
                {"dest": "gold.yaml", "source": "evals/case-001/annotations.yaml"},
                {"dest": "evals", "source": "evals"},
            ])
        ws = tmp_path / "ws"
        ws.mkdir()
        materialize_shared_files(
            ws, config, protected_paths=dataset_protected_paths(config))

        assert (ws / "demo" / "SKILL.md").is_file()
        assert not (ws / "gold.yaml").exists()
        assert not list((ws / "evals").glob("case-*"))
        assert _bash_finds_key(ws) == ""

    def test_podman_masks_the_cases_next_to_eval_yaml(self, tmp_path, monkeypatch):
        project, config = _empty_path_project(tmp_path, monkeypatch)
        assert _podman_project_hide_paths(
            config, {"AGENT_EVAL_PODMAN_PROJECT_DIR": str(project)}) == [
                "evals/case-001", "evals/case-002"]

    def test_podman_project_inside_a_case_is_refused(self, tmp_path, monkeypatch):
        project, config = _empty_path_project(tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="answer keys"):
            _podman_project_hide_paths(
                config, {"AGENT_EVAL_PODMAN_PROJECT_DIR":
                         str(project / "evals" / "case-001")})

    def test_local_deny_rules_cover_the_cases(self, tmp_path, monkeypatch):
        project, config = _empty_path_project(tmp_path, monkeypatch)
        rules = workspace._dataset_deny_rules(config)
        for case in ("case-001", "case-002"):
            for tool in ("Read", "Edit", "Grep", "Glob"):
                assert f"{tool}(/{project / 'evals' / case}/**)" in rules
        # The project root is not denied wholesale.
        assert not any(r.startswith(f"Read(/{project}/**)") for r in rules)


class TestPodmanProjectInsideDataset:
    def test_project_dir_inside_dataset_is_refused(self, tmp_path, monkeypatch):
        project, config = _project(tmp_path, monkeypatch)
        inside = project / DATASET / "case-001"
        with pytest.raises(ValueError, match="answer keys"):
            _podman_project_hide_paths(
                config, {"AGENT_EVAL_PODMAN_PROJECT_DIR": str(inside)})


def _deny_rules(dataset):
    return [f"{tool}(/{dataset}/**)" for tool in ("Read", "Edit", "Grep", "Glob")]


class TestDatasetDenyRules:
    @pytest.mark.parametrize("tools", [False, True], ids=["subagent-only", "tools"])
    def test_isolated_workspace_keeps_grant_and_denies_dataset(
            self, tmp_path, monkeypatch, tools):
        project, config = _project(tmp_path, monkeypatch, tools=tools)
        ws = tmp_path / "ws"
        ws.mkdir()
        if tools:
            workspace._setup_tool_hooks(ws, config)
        else:
            workspace._setup_subagent_only_hook(ws, config)
        perms = json.loads((ws / ".claude" / "settings.json").read_text())["permissions"]

        assert str(project) in perms["additionalDirectories"]
        for rule in _deny_rules(project / DATASET):
            assert rule in perms["deny"]

    def test_repo_mode_denies_dataset(self, tmp_path, monkeypatch):
        project, config = _project(tmp_path, monkeypatch)
        case_ws = tmp_path / "case"
        case_ws.mkdir()
        workspace._create_repo_mode_settings(case_ws, project, config)
        perms = json.loads(
            (case_ws / ".claude" / "settings.json").read_text())["permissions"]

        assert str(project) in perms["additionalDirectories"]
        for rule in _deny_rules(project / DATASET):
            assert rule in perms["deny"]

    def test_rules_are_not_duplicated(self, tmp_path, monkeypatch):
        project, config = _project(tmp_path, monkeypatch)
        settings = {}
        workspace._add_deny(settings, workspace._dataset_deny_rules(config))
        workspace._add_deny(settings, workspace._dataset_deny_rules(config))
        deny = settings["permissions"]["deny"]
        assert len(deny) == len(set(deny))
