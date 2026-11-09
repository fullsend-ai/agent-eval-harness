"""Tests for ``workspace.py --json`` — machine-readable workspace handoff.

Without ``--json`` the script keeps its text interface (``WORKSPACE:``,
``CASES:``, ...). With it, stdout carries exactly one JSON object describing
the prepared workspace and every progress/hook line goes to stderr. Failures
stay nonzero and print no JSON.

All tests run under a temp HOME and a temp project as cwd.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import workspace  # noqa: E402  (skills/eval-run/scripts via conftest)

_INTERCEPT = (
    "inputs:\n  tools:\n    - match: AskUserQuestion\n      prompt: answer yes\n"
)


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A temp project as cwd with three cases under ``cases/``."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    for cid in ("case-001", "case-002", "case-003"):
        case = project / "cases" / cid
        case.mkdir(parents=True)
        (case / "input.yaml").write_text(f"prompt: {cid}\n")
    monkeypatch.chdir(project)
    return project


@pytest.fixture
def run_id(request):
    rid = f"ws-json-{request.node.name}".replace("[", "-").replace("]", "")
    yield rid
    shutil.rmtree(Path(tempfile.gettempdir()) / "agent-eval" / rid, ignore_errors=True)


def _run(monkeypatch, run_id, *extra):
    monkeypatch.setattr(
        sys,
        "argv",
        ["workspace.py", "--config", "eval.yaml", "--run-id", run_id, *extra],
    )
    workspace.main()


def _expected_ws(run_id):
    return (Path(tempfile.gettempdir()) / "agent-eval" / run_id).resolve()


def test_case_mode_json_with_hooks_and_case_subset(
    project, run_id, monkeypatch, capsys
):
    (project / "eval.yaml").write_text(
        "name: t\nexecution:\n  skill: s\ndataset:\n  path: cases\n" + _INTERCEPT
    )
    _run(monkeypatch, run_id, "--cases", "case-001", "case-003", "--json")

    out, err = capsys.readouterr()
    result = json.loads(out)  # stdout is exactly one JSON document
    ws = _expected_ws(run_id)
    assert result["workspace"] == str(ws)
    assert ws.is_dir()
    assert result["mode"] == "case"
    assert result["case_count"] == 2
    assert [c["case_id"] for c in result["cases"]] == ["case-001", "case-003"]
    for case in result["cases"]:
        assert case["workspace"] == str(ws / "cases" / case["case_id"])
        assert Path(case["workspace"]).is_dir()
    # Hook/progress text is diagnostics, not stdout.
    assert "HOOKS:" in err
    assert "HOOKS:" not in out
    assert "WORKSPACE:" not in out


def test_batch_mode_json(project, run_id, monkeypatch, capsys):
    (project / "eval.yaml").write_text(
        "name: t\nexecution:\n  mode: batch\n  skill: s\n"
        "dataset:\n  path: cases\n"
    )
    _run(monkeypatch, run_id, "--json")

    out, err = capsys.readouterr()
    result = json.loads(out)
    ws = _expected_ws(run_id)
    assert result["workspace"] == str(ws)
    assert result["mode"] == "batch"
    assert result["case_count"] == 3
    assert result["cases"] == [
        {"case_id": cid, "entry_count": 1}
        for cid in ("case-001", "case-002", "case-003")
    ]
    assert result["batch_file"] == str(ws / "batch.yaml")
    assert Path(result["batch_file"]).is_file()
    assert "HOOKS: SubagentStop configured" in err


def test_in_repo_mode_json(project, run_id, monkeypatch, capsys):
    # In-repo mode snapshots `git status` of the project, so it must be a repo.
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    (project / "eval.yaml").write_text(
        "name: t\nexecution:\n  prompt: '{{ input.prompt }}'\n"
        "runner:\n  workspace_mode: repo\ndataset:\n  path: cases\n"
    )
    _run(monkeypatch, run_id, "--cases", "case-002", "--json")

    out, _ = capsys.readouterr()
    result = json.loads(out)
    ws = _expected_ws(run_id)
    assert result["workspace"] == str(ws)
    assert result["mode"] == "in-repo"
    assert result["repo"] == str(project)
    assert result["case_count"] == 1
    assert result["cases"] == [
        {"case_id": "case-002", "workspace": str(ws / "cases" / "case-002")}
    ]
    assert (ws / "cases" / "case-002" / "_metadata.yaml").is_file()


def test_text_output_unchanged_without_flag(project, run_id, monkeypatch, capsys):
    (project / "eval.yaml").write_text(
        "name: t\nexecution:\n  skill: s\ndataset:\n  path: cases\n"
    )
    _run(monkeypatch, run_id, "--cases", "case-001")

    out, _ = capsys.readouterr()
    ws = _expected_ws(run_id)
    assert out.splitlines() == [
        "HOOKS: SubagentStop configured (subagent capture)",
        f"WORKSPACE: {ws}",
        "MODE: case",
        "CASES: 1",
        f"  case-001: {ws / 'cases' / 'case-001'}",
    ]


@pytest.mark.parametrize(
    "eval_yaml, extra",
    [
        ("name: t\nexecution:\n  skill: s\ndataset:\n  path: missing\n", ()),
        ("name: t\nexecution:\n  skill: s\ndataset:\n  path: cases\n",
         ("--cases", "nope")),
    ],
    ids=["missing-dataset", "no-matching-cases"],
)
def test_failure_exits_nonzero_without_json(
    project, run_id, monkeypatch, capsys, eval_yaml, extra
):
    (project / "eval.yaml").write_text(eval_yaml)
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, run_id, *extra, "--json")

    assert exc.value.code != 0
    out, err = capsys.readouterr()
    assert out == ""
    assert "ERROR:" in err


def test_invalid_run_id_exits_nonzero_without_json(project, monkeypatch, capsys):
    (project / "eval.yaml").write_text(
        "name: t\nexecution:\n  skill: s\ndataset:\n  path: cases\n"
    )
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, "bad/id", "--json")

    assert exc.value.code != 0
    out, err = capsys.readouterr()
    assert out == ""
    assert "ERROR: run-id" in err
