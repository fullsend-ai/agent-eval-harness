"""Tests for ``execution.symlinks`` — extra project entries staged per workspace.

workspace.py links a fixed default list (``scripts``, ``.claude``,
``CLAUDE.md``, ``.context``, ``skills``) into every staged workspace.
``execution.symlinks`` adds top-level project entries to that list (a
``types/`` directory a script resolves next to itself); ``--symlinks`` on the
CLI replaces both. A configured entry missing from the project is skipped
with one warning, never a failure — a missing default stays silent.

All tests run under a temp HOME and a temp project as cwd.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.config import EvalConfig  # noqa: E402
import workspace  # noqa: E402  (skills/eval-run/scripts via conftest)


def _write(directory, text, name="eval.yaml"):
    p = directory / name
    p.write_text(text)
    return p


def _config(directory, execution_yaml=""):
    return EvalConfig.from_yaml(
        _write(directory, "name: t\nexecution:\n  skill: s\n" + execution_yaml)
    )


# ── Config parsing ───────────────────────────────────────────────────────


def test_default_is_empty(tmp_path):
    assert _config(tmp_path).execution.symlinks == []
    assert _config(tmp_path, "  symlinks:\n").execution.symlinks == []


def test_accepts_names_and_dedupes_in_order(tmp_path):
    cfg = _config(tmp_path, "  symlinks: [types, .context, types, docs]\n")
    assert cfg.execution.symlinks == ["types", ".context", "docs"]


@pytest.mark.parametrize(
    "value, match",
    [
        ("[types/registry]", "path separators"),
        ("['/abs/types']", "path separators"),
        ("['..']", "relative directory reference"),
        ("['.']", "relative directory reference"),
        ("['']", "non-empty string"),
        ("[3]", "list of names"),
        ("types", "list of names"),
    ],
)
def test_rejects_bad_entries(tmp_path, value, match):
    with pytest.raises(ValueError, match=match) as exc:
        _config(tmp_path, f"  symlinks: {value}\n")
    assert "execution.symlinks" in str(exc.value)


def test_extends_overlay_extends_with_dedupe(tmp_path):
    _write(tmp_path, "name: base\nexecution:\n  skill: s\n  symlinks: [types, docs]\n")
    profile = _write(
        tmp_path,
        "extends: eval.yaml\nexecution:\n  symlinks: [docs, fixtures]\n",
        name="profile.yaml",
    )
    assert EvalConfig.from_yaml(profile).execution.symlinks == [
        "types",
        "docs",
        "fixtures",
    ]


# ── Workspace staging ────────────────────────────────────────────────────


def _project(tmp_path, monkeypatch, extra_dirs=("types",)):
    """A temp project as cwd with the default scripts/ and the given extras."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    (project / "scripts" / "tool.py").write_text("print(1)\n")
    for name in extra_dirs:
        (project / name).mkdir()
        (project / name / "marker").write_text("x\n")
    case = project / "cases" / "case-001"
    case.mkdir(parents=True)
    (case / "input.yaml").write_text("prompt: hi\n")
    monkeypatch.chdir(project)
    return project


def _per_case(tmp_path, project, config, cli_symlinks=None):
    ws = tmp_path / "ws"
    ws.mkdir()
    workspace._create_per_case_workspace(
        ws,
        [project / "cases" / "case-001"],
        config,
        SimpleNamespace(symlinks=cli_symlinks),
    )
    return ws / "cases" / "case-001"


def test_resolve_merges_defaults_then_config_deduped(tmp_path):
    cfg = _config(tmp_path, "  symlinks: [types, scripts]\n")
    names, configured = workspace._resolve_symlink_names(
        cfg, SimpleNamespace(symlinks=None)
    )
    assert names == [*workspace.DEFAULT_SYMLINKS, "types"]
    assert configured == {"types"}


def test_cli_flag_replaces_defaults_and_config(tmp_path):
    cfg = _config(tmp_path, "  symlinks: [types]\n")
    names, configured = workspace._resolve_symlink_names(
        cfg, SimpleNamespace(symlinks="scripts, docs")
    )
    assert names == ["scripts", "docs"]
    assert configured == set()


def test_configured_dir_is_linked_into_case_workspace(tmp_path, monkeypatch, capsys):
    project = _project(tmp_path, monkeypatch)
    case_ws = _per_case(tmp_path, project, _config(project, "  symlinks: [types]\n"))

    assert (case_ws / "scripts").is_symlink(), "defaults still staged"
    assert (case_ws / "types").is_symlink()
    assert (case_ws / "types").resolve() == (project / "types").resolve()
    assert (case_ws / "types" / "marker").read_text() == "x\n"
    assert "WARNING: execution.symlinks" not in capsys.readouterr().err


def test_missing_configured_entry_warns_once_and_does_not_fail(
    tmp_path, monkeypatch, capsys
):
    project = _project(tmp_path, monkeypatch)
    for cid in ("case-002", "case-003"):
        (project / "cases" / cid).mkdir()
        (project / "cases" / cid / "input.yaml").write_text("prompt: hi\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    workspace._create_per_case_workspace(
        ws,
        sorted((project / "cases").iterdir()),
        _config(project, "  symlinks: [types, nope]\n"),
        SimpleNamespace(symlinks=None),
    )

    err = capsys.readouterr().err
    assert err.count("WARNING: execution.symlinks entry 'nope' not found") == 1
    for cid in ("case-001", "case-002", "case-003"):
        assert (ws / "cases" / cid / "types").is_symlink()
        assert not (ws / "cases" / cid / "nope").exists()


def test_missing_default_stays_silent(tmp_path, monkeypatch, capsys):
    """No CLAUDE.md / .context / skills in the project: skipped as before, no noise."""
    project = _project(tmp_path, monkeypatch, extra_dirs=())
    case_ws = _per_case(tmp_path, project, _config(project))
    assert (case_ws / "scripts").is_symlink()
    assert not (case_ws / "CLAUDE.md").exists()
    assert "WARNING" not in capsys.readouterr().err


def test_cli_flag_wins_over_config_when_staging(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch, extra_dirs=("types", "docs"))
    case_ws = _per_case(
        tmp_path,
        project,
        _config(project, "  symlinks: [types]\n"),
        cli_symlinks="docs",
    )
    assert (case_ws / "docs").is_symlink()
    assert not (case_ws / "types").exists()
    assert not (case_ws / "scripts").exists()


def test_batch_mode_links_configured_entry(tmp_path, monkeypatch, capsys):
    project = _project(tmp_path, monkeypatch)
    _write(
        project,
        "name: t\nexecution:\n  mode: batch\n  skill: s\n"
        "  symlinks: [types, nope]\ndataset:\n  path: cases\n",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["workspace.py", "--config", "eval.yaml", "--run-id", "symlinks-batch-test"],
    )
    workspace.main()

    out, err = capsys.readouterr()
    ws = Path(
        next(line for line in out.splitlines() if line.startswith("WORKSPACE:"))
        .split(":", 1)[1]
        .strip()
    )
    assert (ws / "types").is_symlink()
    assert (ws / "scripts").is_symlink()
    assert not (ws / "nope").exists()
    assert err.count("WARNING: execution.symlinks entry 'nope' not found") == 1
