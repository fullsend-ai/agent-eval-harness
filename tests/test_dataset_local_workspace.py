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
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.agent.fullsend import materialize_symlinks  # noqa: E402
from agent_eval.config import EvalConfig  # noqa: E402
from agent_eval.harbor.run import _podman_project_hide_paths  # noqa: E402
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

    def test_other_runners_keep_the_unfiltered_copy(self, tmp_path, monkeypatch):
        project, config = _project(tmp_path, monkeypatch, shared=SHARED)
        case_ws = _stage(tmp_path, project, config)
        assert (case_ws / "demo" / "evals" / "cases" / "case-001"
                / "annotations.yaml").is_file()

    def test_empty_dataset_path_protects_nothing(self, tmp_path, monkeypatch):
        project, config = _project(
            tmp_path, monkeypatch, runner="fullsend", shared=SHARED, dataset="")
        case_ws = _stage(tmp_path, project, config)
        assert (case_ws / "demo" / "SKILL.md").is_file()


class TestEmptyDatasetPath:
    def test_podman_hides_nothing(self, tmp_path, monkeypatch):
        project, config = _project(tmp_path, monkeypatch, dataset="")
        assert _podman_project_hide_paths(
            config, {"AGENT_EVAL_PODMAN_PROJECT_DIR": str(project)}) == []


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

    def test_no_dataset_path_adds_no_rules(self, tmp_path, monkeypatch):
        project, config = _project(tmp_path, monkeypatch, dataset="")
        assert workspace._dataset_deny_rules(config) == []

    def test_rules_are_not_duplicated(self, tmp_path, monkeypatch):
        project, config = _project(tmp_path, monkeypatch)
        settings = {}
        workspace._add_deny(settings, workspace._dataset_deny_rules(config))
        workspace._add_deny(settings, workspace._dataset_deny_rules(config))
        deny = settings["permissions"]["deny"]
        assert len(deny) == len(set(deny))
