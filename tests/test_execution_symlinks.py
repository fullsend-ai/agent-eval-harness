"""Tests for ``execution.symlinks`` — extra project entries staged per workspace.

workspace.py links a fixed default list (``scripts``, ``.claude``,
``CLAUDE.md``, ``.context``, ``skills``) into every staged workspace.
``execution.symlinks`` adds top-level project entries to that list (a
``types/`` directory a script resolves next to itself); ``--symlinks`` on the
CLI replaces both. A configured entry missing from the project is skipped
with one warning, never a failure — a missing default stays silent.

All tests run under a temp HOME and a temp project as cwd.
"""

import os
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


# ── Loader: absent/null vs. wrong type ───────────────────────────────────


@pytest.mark.parametrize("value", ["false", "'types'", "0", "{a: b}"])
def test_non_list_value_is_rejected_not_defaulted(tmp_path, value):
    """Falsy-but-present values must reach ExecutionConfig's type check."""
    with pytest.raises(ValueError, match="list of names"):
        _config(tmp_path, f"  symlinks: {value}\n")


# ── Reserved names ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    sorted(
        {
            "batch.yaml",
            "case_order.yaml",
            "cases",
            "subagents",
            "repo_snapshot_before.txt",
            "input.yaml",
            "answers.yaml",
            "_metadata.yaml",
            "hooks",
            "tool_handlers.yaml",
            "output",
            "repo_modifications.txt",
            ".claude",
            ".fullsend-runs",
            # Runner staging directories.
            ".staged-plugins",
            ".agents",
            ".cursor",
            ".skills",
            # Post-run files.
            "stdout.log",
            "stderr.log",
            "events.json",
            "events.jsonl",
            "hook_outputs.yaml",
            "steps",
            "_modified",
        }
    ),
)
def test_static_reserved_names_rejected(tmp_path, name):
    with pytest.raises(ValueError, match="reserved") as exc:
        _config(tmp_path, f"  symlinks: [{name}]\n")
    assert repr(name) in str(exc.value)
    assert "execution.symlinks" in str(exc.value)


def test_output_dir_first_component_is_reserved(tmp_path):
    text = (
        "name: t\nexecution:\n  skill: s\n  symlinks: [results]\n"
        "outputs:\n  - path: results/nested\n"
    )
    with pytest.raises(ValueError, match="reserved") as exc:
        EvalConfig.from_yaml(_write(tmp_path, text))
    assert "outputs[0].path" in str(exc.value)
    assert "'results'" in str(exc.value)


def test_workspace_file_dest_first_component_is_reserved(tmp_path):
    text = (
        "name: t\nexecution:\n  skill: s\n  symlinks: [docs]\n"
        "dataset:\n  workspace:\n    files:\n"
        "      - {dest: docs/SKILL.md, source: SKILL.md}\n"
    )
    with pytest.raises(ValueError, match="reserved") as exc:
        EvalConfig.from_yaml(_write(tmp_path, text))
    assert "dataset.workspace.files[0].dest" in str(exc.value)


def test_unrelated_output_does_not_reserve(tmp_path):
    text = (
        "name: t\nexecution:\n  skill: s\n  symlinks: [types]\n"
        "outputs:\n  - path: results\n"
    )
    assert EvalConfig.from_yaml(_write(tmp_path, text)).execution.symlinks == ["types"]


# ── Staging never writes through into the project ────────────────────────

_INTERCEPT = (
    "inputs:\n  tools:\n    - match: AskUserQuestion\n      prompt: answer yes\n"
)


def _snapshot(root):
    """Every file under *root* (symlinks not followed) -> (bytes, mtime_ns)."""
    snap = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            snap[str(p.relative_to(root))] = (p.read_bytes(), p.stat().st_mtime_ns)
    return snap


def _project_with_claude(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    (project / ".claude" / "skills").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text('{"permissions": {}}\n')
    (project / "CLAUDE.md").write_text("# project\n")
    return project


def test_per_case_staging_with_interception_leaves_project_untouched(
    tmp_path, monkeypatch
):
    project = _project_with_claude(tmp_path, monkeypatch)
    cfg = _config(
        project,
        "  symlinks: [types]\n" + _INTERCEPT,
    )
    before = _snapshot(project)
    case_ws = _per_case(tmp_path, project, cfg)

    assert (case_ws / "types").is_symlink()
    assert (case_ws / ".claude" / "settings.json").is_file()
    assert not (case_ws / ".claude").is_symlink()
    assert (case_ws / "hooks").is_dir() and not (case_ws / "hooks").is_symlink()
    assert _snapshot(project) == before


def test_batch_staging_with_interception_leaves_project_untouched(
    tmp_path, monkeypatch, capsys
):
    project = _project_with_claude(tmp_path, monkeypatch)
    _write(
        project,
        "name: t\nexecution:\n  mode: batch\n  skill: s\n  symlinks: [types]\n"
        "dataset:\n  path: cases\n" + _INTERCEPT,
    )
    before = _snapshot(project)
    monkeypatch.setattr(
        sys,
        "argv",
        ["workspace.py", "--config", "eval.yaml", "--run-id", "symlinks-reserved-test"],
    )
    workspace.main()
    out = capsys.readouterr().out
    ws = Path(
        next(line for line in out.splitlines() if line.startswith("WORKSPACE:"))
        .split(":", 1)[1]
        .strip()
    )

    assert (ws / "types").is_symlink()
    assert (ws / ".claude" / "settings.json").is_file()
    assert not (ws / ".claude").is_symlink()
    assert (ws / "tool_handlers.yaml").is_file() or (ws / "hooks").is_dir()
    assert _snapshot(project) == before


# ── Existing destination ─────────────────────────────────────────────────


def test_colliding_copied_input_file_fails_staging(tmp_path, monkeypatch):
    """A per-case input file named like a configured entry must not be skipped."""
    project = _project(tmp_path, monkeypatch)
    (project / "cases" / "case-001" / "types").write_text("mine\n")
    cfg = _config(
        project,
        "  symlinks: [types]\ndataset:\n  workspace:\n    files: [types]\n",
    )
    with pytest.raises(workspace.StagingError) as exc:
        _per_case(tmp_path, project, cfg)
    msg = str(exc.value)
    assert "'types'" in msg
    assert str(tmp_path / "ws" / "cases" / "case-001" / "types") in msg


def test_existing_link_to_intended_path_is_left_alone(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    cfg = _config(project, "  symlinks: [types]\n")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "types").symlink_to(project / "types")
    workspace._link_project_entries(dest, project, ["types"], {"types"}, set(), cfg)
    assert (dest / "types").resolve() == (project / "types").resolve()


def test_existing_link_elsewhere_fails_staging(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    cfg = _config(project, "  symlinks: [types]\n")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "types").symlink_to(project / "scripts")
    with pytest.raises(workspace.StagingError, match="'types'"):
        workspace._link_project_entries(dest, project, ["types"], {"types"}, set(), cfg)


def test_existing_default_destination_is_skipped_silently(
    tmp_path, monkeypatch, capsys
):
    project = _project(tmp_path, monkeypatch)
    cfg = _config(project)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "scripts").write_text("copied\n")
    workspace._link_project_entries(dest, project, ["scripts"], set(), set(), cfg)
    assert (dest / "scripts").read_text() == "copied\n"
    assert capsys.readouterr().err == ""


# ── Source containment ───────────────────────────────────────────────────


def test_configured_symlink_escaping_project_is_skipped_with_warning(
    tmp_path, monkeypatch, capsys
):
    project = _project(tmp_path, monkeypatch, extra_dirs=())
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("s\n")
    (project / "types").symlink_to(outside)
    case_ws = _per_case(tmp_path, project, _config(project, "  symlinks: [types]\n"))

    assert not os.path.lexists(case_ws / "types")
    err = capsys.readouterr().err
    assert err.count("execution.symlinks entry 'types'") == 1
    assert "outside" in err


def test_configured_symlink_into_plugin_dir_is_linked(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch, extra_dirs=())
    plugin = tmp_path / "plugin"
    (plugin / "types").mkdir(parents=True)
    (project / "types").symlink_to(plugin / "types")
    cfg = _config(
        project,
        f"  symlinks: [types]\nrunner:\n  type: claude-code\n  plugin_dirs: ['{plugin}']\n",
    )
    case_ws = _per_case(tmp_path, project, cfg)
    assert (case_ws / "types").is_symlink()


def test_default_entry_escaping_project_is_still_linked(tmp_path, monkeypatch):
    """Containment applies to configured entries only; defaults are unchanged."""
    project = _project(tmp_path, monkeypatch, extra_dirs=())
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "skills").symlink_to(outside)
    case_ws = _per_case(tmp_path, project, _config(project))
    assert (case_ws / "skills").is_symlink()
    assert (case_ws / "skills").resolve() == outside.resolve()
