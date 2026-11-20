"""Tests for keeping the eval's dataset — its answer keys — away from the agent (#345).

Every isolated workspace used to get the project root in
``permissions.additionalDirectories``. The dataset lives under the project root,
the grant discloses that path in the session's init event, and Bash is not
path-scoped, so an agent under test could find its case's ``annotations.yaml``
and reference output and copy them. Now the grant is opt-in
(``execution.project_access: true``, which also denies the dataset to the file
tools); a project-carried grant that contains the dataset is dropped; and
``workspace_mode: repo`` keeps its grant but denies the dataset.

All tests run under a temp HOME and a temp project; none reads ``~/.claude``.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.config import EvalConfig
import workspace  # skills/eval-run/scripts (sys.path via conftest)


TOOLS = "  arguments: ''\ninputs:\n  tools:\n    - match: AskUserQuestion\n      prompt: answer yes\n"


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch):
    monkeypatch.setattr(workspace, "_WARNED_GRANTS", set())


def _project(tmp_path, monkeypatch, project_settings=None):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project = (tmp_path / "project").resolve()
    (project / "eval" / "cases" / "case-001").mkdir(parents=True)
    (project / "eval" / "cases" / "case-001" / "annotations.yaml").write_text("answer: 42\n")
    if project_settings is not None:
        (project / ".claude").mkdir()
        (project / ".claude" / "settings.json").write_text(json.dumps(project_settings))
    monkeypatch.chdir(project)
    return project


def _config(project, execution_yaml="", runner_yaml="  type: claude-code\n",
            dataset_path="eval/cases"):
    p = project / "eval.yaml"
    p.write_text(
        "name: t\nexecution:\n  skill: s\n" + execution_yaml
        + f"dataset:\n  path: {dataset_path}\n"
        + "runner:\n" + runner_yaml)
    return EvalConfig.from_yaml(p)


def _isolated_settings(tmp_path, builder, config):
    ws = tmp_path / "ws"
    ws.mkdir()
    if builder == "tools":
        workspace._setup_tool_hooks(ws, config)
    else:
        workspace._setup_subagent_only_hook(ws, config)
    return json.loads((ws / ".claude" / "settings.json").read_text())


def _deny_rules(dataset):
    return [f"{tool}(/{dataset}/**)" for tool in ("Read", "Edit", "Grep", "Glob")]


ISOLATED = ["subagent-only", "tools"]


@pytest.mark.parametrize("builder", ISOLATED)
def test_project_root_not_granted_by_default(tmp_path, monkeypatch, builder):
    project = _project(tmp_path, monkeypatch)
    extra = TOOLS if builder == "tools" else ""
    settings = _isolated_settings(tmp_path, builder, _config(project, extra))
    perms = settings.get("permissions", {})
    assert "additionalDirectories" not in perms
    # The dataset path appears nowhere in the workspace settings.
    assert str(project / "eval") not in json.dumps(settings)


@pytest.mark.parametrize("builder", ISOLATED)
def test_project_access_opt_in_grants_root_and_denies_dataset(tmp_path, monkeypatch, builder):
    project = _project(tmp_path, monkeypatch)
    extra = "  project_access: true\n" + (TOOLS if builder == "tools" else "")
    perms = _isolated_settings(tmp_path, builder, _config(project, extra))["permissions"]
    assert perms["additionalDirectories"] == [str(project)]
    for rule in _deny_rules(project / "eval" / "cases"):
        assert rule in perms["deny"]


def test_project_grant_containing_dataset_is_dropped(tmp_path, monkeypatch, capsys):
    other = (tmp_path / "shared").resolve()
    other.mkdir()
    project = _project(tmp_path, monkeypatch, {"permissions": {
        "additionalDirectories": [str(tmp_path.resolve()), str(other), "relative/dir"]}})
    perms = _isolated_settings(tmp_path, "subagent-only", _config(project))["permissions"]
    # The ancestor of the dataset is dropped; unrelated and relative grants
    # (with the carry-over's resolved twin) stay.
    assert perms["additionalDirectories"] == [
        str(other), "relative/dir", str(project / "relative" / "dir")]
    assert "contains the eval dataset" in capsys.readouterr().err


def test_project_dot_grant_resolving_to_root_is_dropped(tmp_path, monkeypatch):
    # '.' is carried verbatim (relative to the session) plus its resolution
    # against the project cwd — the project root, which holds the dataset.
    project = _project(tmp_path, monkeypatch, {"permissions": {
        "additionalDirectories": ["."]}})
    perms = _isolated_settings(tmp_path, "subagent-only", _config(project))["permissions"]
    assert perms["additionalDirectories"] == ["."]


def test_dataset_outside_project_root(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    outside = (tmp_path / "datasets" / "cases").resolve()
    outside.mkdir(parents=True)
    config = _config(project, "  project_access: true\n", dataset_path=str(outside))
    perms = _isolated_settings(tmp_path, "subagent-only", config)["permissions"]
    assert perms["additionalDirectories"] == [str(project)]
    for rule in _deny_rules(outside):
        assert rule in perms["deny"]


def test_repo_mode_keeps_grant_and_denies_dataset(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    config = _config(project, runner_yaml="  type: claude-code\n  workspace_mode: repo\n")
    case_ws = tmp_path / "run" / "cases" / "case-001"
    case_ws.mkdir(parents=True)
    workspace._create_repo_mode_settings(case_ws, project, config)
    perms = json.loads((case_ws / ".claude" / "settings.json").read_text())["permissions"]
    assert str(project) in perms["additionalDirectories"]
    for rule in _deny_rules(project / "eval" / "cases"):
        assert rule in perms["deny"]


def test_project_access_must_be_boolean(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="execution.project_access must be a boolean"):
        _config(project, "  project_access: 'yes'\n")


def test_project_access_defaults_false(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    assert _config(project).execution.project_access is False
