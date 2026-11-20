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
import execute  # skills/eval-run/scripts (sys.path via conftest)


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
    assert "overlaps the eval dataset" in capsys.readouterr().err


def test_project_dot_grant_resolving_to_root_is_dropped(tmp_path, monkeypatch):
    # '.' is carried verbatim (relative to the session) plus its resolution
    # against the project cwd — the project root, which holds the dataset.
    # Both spellings go: removing only the absolute twin would leave the grant.
    project = _project(tmp_path, monkeypatch, {"permissions": {
        "additionalDirectories": ["."]}})
    perms = _isolated_settings(tmp_path, "subagent-only", _config(project))["permissions"]
    assert "additionalDirectories" not in perms


def test_relative_parent_grant_is_dropped(tmp_path, monkeypatch):
    # '..' from the project is the tmp dir, an ancestor of the dataset.
    project = _project(tmp_path, monkeypatch, {"permissions": {
        "additionalDirectories": [".."]}})
    perms = _isolated_settings(tmp_path, "subagent-only", _config(project))["permissions"]
    assert "additionalDirectories" not in perms


def test_relative_grant_resolving_against_workspace_is_dropped(tmp_path, monkeypatch):
    # The agent runs in <tmp>/ws, so 'project' names the project root there.
    project = _project(tmp_path, monkeypatch, {"permissions": {
        "additionalDirectories": ["../project"]}})
    perms = _isolated_settings(tmp_path, "subagent-only", _config(project))["permissions"]
    assert "additionalDirectories" not in perms


def test_grant_inside_dataset_is_dropped(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    case = project / "eval" / "cases" / "case-001"
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.json").write_text(json.dumps({"permissions": {
        "additionalDirectories": [str(case)]}}))
    perms = _isolated_settings(tmp_path, "subagent-only", _config(project))["permissions"]
    assert "additionalDirectories" not in perms


def test_symlinked_dataset_caught_by_configured_path(tmp_path, monkeypatch):
    # The dataset lives outside the project but is reached through a link in
    # it: a carried project-root grant exposes it through the link.
    outside = (tmp_path / "datasets").resolve()
    (outside / "case-001").mkdir(parents=True)
    project = _project(tmp_path, monkeypatch, {"permissions": {
        "additionalDirectories": [str(tmp_path / "project")]}})
    (project / "linked").symlink_to(outside)
    config = _config(project, dataset_path="linked")
    perms = _isolated_settings(tmp_path, "subagent-only", config)["permissions"]
    assert "additionalDirectories" not in perms


def test_empty_dataset_path_protects_config_dir(tmp_path, monkeypatch):
    # Case preparation reads an empty dataset.path as the config directory.
    project = _project(tmp_path, monkeypatch, {"permissions": {
        "additionalDirectories": [str(tmp_path / "project")]}})
    config = _config(project, dataset_path="''")
    perms = _isolated_settings(tmp_path, "subagent-only", config)["permissions"]
    assert "additionalDirectories" not in perms
    (tmp_path / "again").mkdir()
    opted = _config(project, "  project_access: true\n", dataset_path="''")
    perms = _isolated_settings(tmp_path / "again", "subagent-only", opted)["permissions"]
    for rule in _deny_rules(project):
        assert rule in perms["deny"]


def test_file_tool_allow_rule_over_dataset_is_dropped(tmp_path, monkeypatch):
    elsewhere = f"Read(/{tmp_path / 'elsewhere'}/**)"
    project = _project(tmp_path, monkeypatch, {"permissions": {"allow": [
        "Bash(ls)", f"Read(/{tmp_path / 'project'}/**)",
        f"Grep(/{tmp_path / 'project' / 'eval'}/**)", elsewhere]}})
    perms = _isolated_settings(tmp_path, "subagent-only", _config(project))["permissions"]
    assert perms["allow"] == ["Bash(ls)", elsewhere]


def test_runner_settings_cannot_readd_dataset_grant(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    config = _config(project, runner_yaml=(
        "  type: claude-code\n  settings:\n    permissions:\n"
        f"      additionalDirectories: ['{project}']\n"))
    perms = _isolated_settings(tmp_path, "subagent-only", config)["permissions"]
    assert "additionalDirectories" not in perms


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


def _allow_config(project, rules_yaml, execution_yaml="", runner_yaml="  type: claude-code\n"):
    p = project / "eval.yaml"
    p.write_text(
        "name: t\nexecution:\n  skill: s\n" + execution_yaml
        + "dataset:\n  path: eval/cases\n"
        + "permissions:\n  allow:\n" + rules_yaml
        + "runner:\n" + runner_yaml)
    return EvalConfig.from_yaml(p)


def test_runner_allow_rules_drop_dataset_overlap(tmp_path, monkeypatch):
    """The rules the runner passes (--allowed-tools) get the same filter as the staged file."""
    project = _project(tmp_path, monkeypatch)
    elsewhere = f"Read(/{tmp_path / 'elsewhere'}/**)"
    config = _allow_config(project, (
        f"    - 'Read(/{project}/**)'\n    - 'Grep(/{project / 'eval' / 'cases'}/**)'\n"
        f"    - '{elsewhere}'\n    - Skill\n"))
    assert execute._resolve_permissions(config)["allow"] == [elsewhere, "Skill"]
    # the eval.yaml object itself is not mutated
    assert len(config.permissions["allow"]) == 4


def test_runner_allow_rules_kept_with_project_access(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    rule = f"Read(/{project}/**)"
    config = _allow_config(project, f"    - '{rule}'\n", "  project_access: true\n")
    assert execute._resolve_permissions(config)["allow"] == [rule]


def test_runner_allow_rules_untouched_in_repo_mode(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    rule = f"Read(/{project}/**)"
    config = _allow_config(project, f"    - '{rule}'\n",
                           runner_yaml="  type: claude-code\n  workspace_mode: repo\n")
    assert execute._resolve_permissions(config)["allow"] == [rule]


def test_path_based_allow_rule_over_dataset_dropped_from_overlay(tmp_path, monkeypatch):
    """A path-based rule compiling to a dataset-overlapping pattern never reaches the overlay."""
    project = _project(tmp_path, monkeypatch)
    config = _allow_config(project, (
        f"    - path: '/{project}/eval/cases/'\n      tools: [Read]\n"
        "    - path: notes/\n      tools: [Read]\n"))
    from agent_eval.agent.claude_code import ClaudeCodeRunner
    runner = ClaudeCodeRunner.from_config(
        config, permissions=execute._resolve_permissions(config))
    ws = tmp_path / "ws"
    ws.mkdir()
    overlay = runner._write_settings_overlay(
        ws, None, [], runner._permissions["allow"], None)
    allow = json.loads(overlay.read_text())["permissions"]["allow"]
    assert allow == ["Read(notes/**)"]


# --- allow-rule spellings and partial-component wildcards -------------------

@pytest.mark.parametrize("pattern", ["ev*/**", "ev?l/**", "ev[a-z]l/**", "e*"])
def test_wildcard_inside_a_directory_name_is_dropped(tmp_path, monkeypatch, pattern):
    """``Read(//project/ev*/**)`` reaches ``eval/``: a wildcard that starts
    partway through a directory name must be checked against its container."""
    keep = f"Read(/{tmp_path / 'elsewhere'}/ev*/**)"
    rule = f"Read(/{tmp_path / 'project'}/{pattern})"
    project = _project(tmp_path, monkeypatch, {"permissions": {"allow": [rule, keep]}})
    perms = _isolated_settings(tmp_path, "subagent-only", _config(project))["permissions"]
    assert perms["allow"] == [keep]
    config = _allow_config(project, f"    - '{rule}'\n    - '{keep}'\n")
    assert execute._resolve_permissions(config)["allow"] == [keep]


def test_wildcard_that_cannot_match_dataset_still_drops_by_container(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    rule = f"Read(/{tmp_path / 'project'}/src*/**)"
    config = _allow_config(project, f"    - '{rule}'\n")
    # 'src*' cannot name 'eval', but the container (the project root) holds
    # the dataset; the filter errs toward dropping.
    assert execute._resolve_permissions(config)["allow"] == []


@pytest.mark.parametrize("spelling", [
    "~/project/eval/cases/**",
    "~/project/ev*/**",
    "~/project/**",
    "~/**",
])
def test_home_relative_allow_rule_over_dataset_is_dropped(tmp_path, monkeypatch, spelling):
    elsewhere = "Read(~/other/**)"
    rule = f"Read({spelling})"
    project = _project(tmp_path, monkeypatch, {"permissions": {"allow": [rule, elsewhere]}})
    monkeypatch.setenv("HOME", str(tmp_path))  # the project sits under $HOME
    perms = _isolated_settings(tmp_path, "subagent-only", _config(project))["permissions"]
    assert perms["allow"] == [elsewhere]
    config = _allow_config(project, f"    - '{rule}'\n    - '{elsewhere}'\n")
    assert execute._resolve_permissions(config)["allow"] == [elsewhere]


@pytest.mark.parametrize("spelling", [
    "/eval/cases/**",         # relative to the settings file / project root
    "/eval/**",
    "./eval/cases/**",
    "../project/eval/**",     # from the workspace, back into the project
])
def test_relative_allow_rule_over_dataset_is_dropped(tmp_path, monkeypatch, spelling):
    rule = f"Read({spelling})"
    notes = "Read(notes/**)"
    project = _project(tmp_path, monkeypatch, {"permissions": {"allow": [rule, notes]}})
    perms = _isolated_settings(tmp_path, "subagent-only", _config(project))["permissions"]
    assert perms["allow"] == [notes]
    config = _allow_config(project, f"    - '{rule}'\n    - '{notes}'\n")
    assert execute._resolve_permissions(config)["allow"][-1:] == [notes]
    assert rule not in execute._resolve_permissions(config)["allow"]


def test_equivalent_dataset_grants_kept_with_project_access(tmp_path, monkeypatch):
    rules = ["Read(~/project/eval/cases/**)", "Read(/eval/cases/**)", "Read(ev*/**)"]
    project = _project(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    config = _allow_config(project, "".join(f"    - '{r}'\n" for r in rules),
                           "  project_access: true\n")
    assert execute._resolve_permissions(config)["allow"] == rules
