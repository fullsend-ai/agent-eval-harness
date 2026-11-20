"""Tests for how eval.yaml ``permissions`` reach the local runner's workspace settings.

Two defects this pins closed: a path-based rule (``{path: ..., tools: [...]}``,
the documented second form) in ``permissions.allow`` crashed every local
workspace builder, because the rules were spliced into the settings uncompiled
and the symlink expansion regex-matched a dict; and without ``inputs.tools`` the
``deny`` list never reached the settings at all (a path-based deny alone did not
crash, it was silently ignored) — only the interception generator wrote it, and
it runs only for tool-intercepting evals.  The runner still passed both lists
on the command line; the settings are the one file to inspect and the one the
absolute Bash twins are added to.  Now every builder (batch without and with
tool interception, repo mode) compiles both lists like the Harbor task packages
do and merges them, with dedupe, into what the settings already hold.

All tests run under a temp HOME and a temp project; none reads ``~/.claude``.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.config import EvalConfig
import workspace  # skills/eval-run/scripts (sys.path via conftest)


EVAL_PERMISSIONS = (
    "permissions:\n"
    "  allow:\n"
    "    - { path: 'scripts/', tools: ['Read', 'Bash'] }\n"   # Bash: no path pattern, no hardening on allow
    "    - Bash(git status)\n"
    "  deny:\n"
    "    - { path: 'eval/', tools: ['Bash'] }\n"
    "    - Read(eval/answers.yaml)\n"
    "    - Bash(python3 scripts/foo.py --purge *)\n"          # relative script rule: gets its twin
)
COMPILED_ALLOW = ["Read(scripts/**)", "Bash(git status)"]
COMPILED_DENY = ["Read(eval/**)", "Edit(eval/**)", "Read(eval/answers.yaml)",
                 "Bash(python3 scripts/foo.py --purge *)"]  # Bash hardened
PROJECT_PERMS = {"allow": ["Bash(ls)"], "deny": ["Bash(rm *)"]}
TOOLS = "  arguments: ''\ninputs:\n  tools:\n    - match: AskUserQuestion\n      prompt: answer yes\n"


def _project(tmp_path, monkeypatch, project_perms):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    (project / "scripts" / "foo.py").write_text("print(1)\n")
    (project / "eval").mkdir()
    if project_perms:
        (project / ".claude").mkdir()
        (project / ".claude" / "settings.json").write_text(json.dumps({"permissions": project_perms}))
    monkeypatch.chdir(project)
    return project


def _config(directory, extra_yaml, runner_yaml="  type: claude-code\n"):
    p = directory / "eval.yaml"
    p.write_text("name: t\nexecution:\n  skill: s\n" + extra_yaml + "runner:\n" + runner_yaml)
    return EvalConfig.from_yaml(p)


def _build(builder, tmp_path, project, config):
    """``(permissions, root)`` — root is the directory the agent runs in (the
    twin base): the project checkout in repo mode, the workspace otherwise."""
    if builder == "repo":
        case_ws = tmp_path / "run" / "cases" / "case-001"
        case_ws.mkdir(parents=True)
        workspace._create_repo_mode_settings(case_ws, project, config)
        return json.loads((case_ws / ".claude" / "settings.json").read_text())["permissions"], project
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "scripts").symlink_to((project / "scripts").resolve())
    (workspace._setup_tool_hooks if builder == "tools" else workspace._setup_subagent_only_hook)(ws, config)
    return json.loads((ws / ".claude" / "settings.json").read_text())["permissions"], ws


BUILDERS = ["subagent-only", "tools", "repo"]


def _runner_yaml(builder):
    return "  type: claude-code\n" + ("  workspace_mode: repo\n" if builder == "repo" else "")


@pytest.mark.parametrize("builder", BUILDERS)
@pytest.mark.parametrize("project_perms", [None, PROJECT_PERMS], ids=["no-project-perms", "project-perms"])
def test_both_forms_compile_and_deny_lands_in_every_builder(tmp_path, monkeypatch, builder, project_perms):
    project = _project(tmp_path, monkeypatch, project_perms)
    config = _config(project, (TOOLS if builder == "tools" else "") + EVAL_PERMISSIONS, _runner_yaml(builder))
    perms, root = _build(builder, tmp_path, project, config)

    for rule in perms["allow"] + perms["deny"]:
        assert isinstance(rule, str), rule
    assert set(COMPILED_ALLOW) <= set(perms["allow"]), perms["allow"]
    assert set(COMPILED_DENY) <= set(perms["deny"]), perms["deny"]
    # allow is compiled WITHOUT Bash hardening: a Bash path rule adds no Read/Edit grant
    assert "Edit(scripts/**)" not in perms["allow"]
    # the eval.yaml deny rule on a project script gets its absolute twin in this builder
    assert f"Bash(python3 {root}/scripts/foo.py --purge *)" in perms["deny"], perms["deny"]
    if project_perms:
        assert "Bash(ls)" in perms["allow"] and "Bash(rm *)" in perms["deny"]
    assert len(perms["allow"]) == len(set(perms["allow"]))
    assert len(perms["deny"]) == len(set(perms["deny"]))


@pytest.mark.parametrize("builder", BUILDERS)
def test_no_eval_yaml_permissions_adds_nothing(tmp_path, monkeypatch, builder):
    project = _project(tmp_path, monkeypatch, None)
    config = _config(project, TOOLS if builder == "tools" else "", _runner_yaml(builder))
    perms, _ = _build(builder, tmp_path, project, config)
    assert not [r for r in perms.get("allow", []) if r.startswith(("Read(", "Bash(git"))]
    # repo mode writes its own Write/Edit guards for the checkout; the others write no deny
    if builder != "repo":
        assert not perms.get("deny")


def test_project_deny_survives_next_to_the_eval_yaml_deny_without_interception(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch, PROJECT_PERMS)
    perms, ws = _build("subagent-only", tmp_path, project, _config(project, EVAL_PERMISSIONS))
    assert perms["deny"] == ["Bash(rm *)"] + COMPILED_DENY + [f"Bash(python3 {ws}/scripts/foo.py --purge *)"]


def test_symlink_expansion_applies_to_allow_and_deny_alike(tmp_path, monkeypatch):
    """The resolved-path variant for rules under a symlinked absolute prefix
    (macOS /tmp -> /private/tmp) is derived after compilation for allow — and
    for deny, from eval.yaml and from the project, or a deny on the link would
    leave the resolved path open that the widened allow grants."""
    real = tmp_path / "real-out"
    real.mkdir()
    (real / "private").mkdir()
    link = tmp_path / "link-out"
    link.symlink_to(real)
    project = _project(tmp_path, monkeypatch, {"deny": [f"Edit({link}/project-private/**)"]})
    perms, _ = _build("subagent-only", tmp_path, project, _config(
        project, f"permissions:\n  allow:\n    - Edit({link}/**)\n  deny:\n    - Edit({link}/private/**)\n"))
    assert {f"Edit({link}/**)", f"Edit({link.resolve()}/**)"} <= set(perms["allow"])
    assert {f"Edit({link}/private/**)", f"Edit({link.resolve()}/private/**)"} <= set(perms["deny"])
    assert {f"Edit({link}/project-private/**)", f"Edit({link.resolve()}/project-private/**)"} <= set(perms["deny"])
