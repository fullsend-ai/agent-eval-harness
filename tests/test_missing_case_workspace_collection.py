"""A selected case whose workspace is missing is lost evidence (#321).

#314 made such a case fail the run, but collection enumerates
``workspace/cases/*``, so the case produced no `collection.json` entry, and
`score.py::_require_no_collection_loss` reads a case id absent from the
manifest as "nothing lost". The case was judged on an empty directory instead
of counting toward ``incomplete_cases``.

Every test drives `collect.main()` and the scoring entry points, not the
helper alone.
"""

import json
import os
import sys

import collect
import pytest
import score
import yaml
from score import UNLISTABLE_KEY, detect_regressions, load_judges

from agent_eval.config import EvalConfig, JudgeConfig, OutputConfig


def _workspace(tmp_path, present=("c0", "c1"), case_order=("c0", "c1", "c2")):
    ws = tmp_path / "ws"
    for cid in present:
        (ws / "cases" / cid / "out").mkdir(parents=True)
        (ws / "cases" / cid / "out" / "ok.md").write_text("fine")
    (ws / "cases").mkdir(parents=True, exist_ok=True)
    if case_order is not None:
        # The shape `workspace.py` writes.
        (ws / "case_order.yaml").write_text(yaml.dump(
            [{"case_id": cid, "entry_count": 1} for cid in case_order],
            default_flow_style=False))
    return ws


def _collect(tmp_path, monkeypatch, ws):
    cfg = tmp_path / "eval.yaml"
    cfg.write_text("name: t\nexecution:\n  mode: case\n  skill: s\n"
                   "outputs:\n  - path: out\n"
                   "traces:\n  events: false\n")
    run = tmp_path / "run"
    monkeypatch.setattr(sys, "argv", [
        "collect", "--config", str(cfg), "--workspace", str(ws),
        "--output", str(run)])
    collect.main()
    return run, json.loads((run / "collection.json").read_text())


def _config():
    config = EvalConfig(name="test", skill="test")
    config.judges = [JudgeConfig(name="shape", check="return True, 'ok'")]
    config.outputs = [OutputConfig(path="out")]
    return config


def _score(run, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(tmp_path / "runs"))
    config = _config()
    cases = sorted((run / "cases").iterdir())
    res = score.score_cases(load_judges(config, project_root=str(tmp_path)),
                            cases, config, run_id="run")
    return res["aggregated"]["shape"], res


def test_a_missing_case_workspace_is_recorded_as_lost(tmp_path, monkeypatch):
    run, recorded = _collect(tmp_path, monkeypatch, _workspace(tmp_path))
    assert recorded["c2"][UNLISTABLE_KEY] == ["c2"]
    # The case stays in the run, so `_get_case_dirs` finds it.
    assert (run / "cases" / "c2").is_dir()
    # The cases that are present are collected as before.
    for cid in ("c0", "c1"):
        assert UNLISTABLE_KEY not in recorded[cid]
        assert (run / "cases" / cid / "out" / "ok.md").exists()
    with pytest.raises(OSError):
        score.load_case_record(run / "cases" / "c2", _config())


def test_a_missing_case_workspace_reaches_the_gate(tmp_path, monkeypatch):
    run, _ = _collect(tmp_path, monkeypatch, _workspace(tmp_path))
    agg, res = _score(run, monkeypatch, tmp_path)
    assert agg["incomplete_cases"] == 1
    assert agg["scored_cases"] == 2
    assert "incomplete_staging" in [r.metric for r in detect_regressions(
        res["aggregated"], {"shape": {"min_pass_rate": 0.9}})]


def test_a_case_listed_twice_is_recorded_once(tmp_path, monkeypatch, capsys):
    ws = _workspace(tmp_path, case_order=("c0", "c2", "c1", "c2"))
    _, recorded = _collect(tmp_path, monkeypatch, ws)
    assert recorded["c2"][UNLISTABLE_KEY] == ["c2"]
    assert capsys.readouterr().err.count("c2: cannot collect c2") == 1


def test_a_refused_case_directory_is_recorded_once(tmp_path, monkeypatch,
                                                   capsys):
    """`cases/c2 -> /outside` is a directory to the enumeration, so the
    existing refused-directory branch records it; the selected-case check
    must not record it a second time."""
    outside = tmp_path / "outside"
    (outside / "out").mkdir(parents=True)
    (outside / "out" / "secret.md").write_text("NOT THE AGENT'S TO SHOW")
    ws = _workspace(tmp_path)
    os.symlink(outside, ws / "cases" / "c2")
    run, recorded = _collect(tmp_path, monkeypatch, ws)
    assert recorded["c2"][UNLISTABLE_KEY] == ["c2"]
    assert capsys.readouterr().err.count("c2: cannot collect c2") == 1
    assert not [p for p in run.rglob("*") if p.name == "secret.md"]


def test_a_dangling_case_link_is_recorded_as_lost(tmp_path, monkeypatch):
    """Not a directory to the enumeration, so it was skipped silently."""
    ws = _workspace(tmp_path)
    os.symlink(tmp_path / "nowhere", ws / "cases" / "c2")
    _, recorded = _collect(tmp_path, monkeypatch, ws)
    assert recorded["c2"][UNLISTABLE_KEY] == ["c2"]


def test_a_hostile_case_id_cannot_forge_a_log_line(tmp_path, monkeypatch,
                                                   capsys):
    """`case_order.yaml` is a source of ids reaching the per-case summary
    print; a newline or a leading `::` must not forge a log line or a
    workflow command, in the id or in the counts that repeat it."""
    hostile = "::error::forged\nFORGED LINE"
    ws = _workspace(tmp_path, case_order=("c0", "c1", hostile))
    _, recorded = _collect(tmp_path, monkeypatch, ws)
    assert recorded[hostile][UNLISTABLE_KEY] == [hostile]
    captured = capsys.readouterr()
    for stream in (captured.out, captured.err):
        lines = stream.splitlines()
        assert not [ln for ln in lines if ln.startswith("::")]
        assert "FORGED LINE" not in lines
        assert not [ln for ln in lines if ln.lstrip().startswith("FORGED")]
    assert "\\nFORGED LINE" in captured.out


@pytest.mark.parametrize("content", [
    None,                    # no case_order.yaml
    "- [unclosed\n",         # unparseable
    "just a string\n",       # not a list
    b"- c2\n- \xff\xfe\n",   # not UTF-8
])
def test_no_usable_case_order_falls_back_to_the_workspace(
        tmp_path, monkeypatch, content):
    ws = _workspace(tmp_path, case_order=None)
    if isinstance(content, bytes):
        (ws / "case_order.yaml").write_bytes(content)
    elif content is not None:
        (ws / "case_order.yaml").write_text(content)
    run, recorded = _collect(tmp_path, monkeypatch, ws)
    assert sorted(recorded) == ["c0", "c1"]
    assert all(UNLISTABLE_KEY not in info for info in recorded.values())
    assert sorted(p.name for p in (run / "cases").iterdir()) == ["c0", "c1"]


def test_case_order_ids_that_are_not_a_plain_name_are_skipped(
        tmp_path, monkeypatch):
    ws = _workspace(tmp_path, case_order=None)
    (ws / "case_order.yaml").write_text(yaml.dump(
        ["c0", "../escape", "a/b", "", ".", "..", 7, {"entry_count": 1},
         {"case_id": "c3"}]))
    run, recorded = _collect(tmp_path, monkeypatch, ws)
    assert sorted(recorded) == ["c0", "c1", "c3"]
    assert recorded["c3"][UNLISTABLE_KEY] == ["c3"]
    assert not (tmp_path / "escape").exists()
    assert sorted(p.name for p in (run / "cases").iterdir()) == [
        "c0", "c1", "c3"]
