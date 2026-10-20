"""Per-case hook results: recorded in run_result.json, case-scoped
``on_failure: fail`` for after_each, after_each outputs and hook results
exposed to judges."""

import json
import os
import sys
import textwrap
import types
import unittest.mock
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

import execute as ex  # noqa: E402  (skills/eval-run/scripts via conftest)
from agent_eval.agent.base import RunResult  # noqa: E402
from agent_eval.config import (  # noqa: E402
    DatasetConfig, EvalConfig, HookEntry, HooksConfig,
)
from agent_eval.hooks import (  # noqa: E402
    HookError, build_hook_env, hook_results_record, run_hooks, run_hooks_safe,
)
from score import _make_inline_check, load_case_record  # noqa: E402


# ---------------------------------------------------------------------------
# hooks.py helpers
# ---------------------------------------------------------------------------

def test_run_hooks_results_survive_hook_error(tmp_path):
    collected = []
    entries = [HookEntry(command="true"),
               HookEntry(command="exit 3", on_failure="fail"),
               HookEntry(command="true")]
    with pytest.raises(HookError):
        run_hooks(entries, dict(os.environ), tmp_path, tmp_path / "hooks",
                  "before_each", results=collected)
    assert [r.exit_code for r in collected] == [0, 3]


def test_run_hooks_safe_keeps_configured_entry(tmp_path):
    entries = [HookEntry(command="exit 1", on_failure="fail",
                         description="capture"),
               HookEntry(command="echo ran > second.txt",
                         on_failure="fail")]
    results = run_hooks_safe(entries, dict(os.environ), tmp_path,
                             tmp_path / "hooks", "after_each")
    assert len(results) == 2  # never raises, every entry runs
    assert (tmp_path / "second.txt").exists()
    assert results[0].hook is entries[0]
    assert results[0].hook.on_failure == "fail"


def test_hook_results_record_shape(tmp_path):
    entries = [HookEntry(command="true", description="ok"),
               HookEntry(command="true", condition="false",
                         description="skipped"),
               HookEntry(command="exit 2", on_failure="continue",
                         description="Save the PR diff")]
    results = run_hooks_safe(entries, dict(os.environ), tmp_path,
                             tmp_path / "hooks", "after_each", case_id="c1")
    rec = hook_results_record({"before_each": [], "after_each": results},
                              tmp_path)
    assert set(rec) == {"hooks", "hook_failures"}
    assert list(rec["hooks"]) == ["after_each"]  # empty phases omitted
    first, skipped, failed = rec["hooks"]["after_each"]
    assert first["exit_code"] == 0 and first["log"] == "hooks/after_each.c1.0.log"
    assert skipped["skipped"] is True and skipped["log"] is None
    assert failed["exit_code"] == 2 and failed["timed_out"] is False
    assert failed["on_failure"] == "continue"
    assert rec["hook_failures"] == [
        "after_each[2]: Save the PR diff (exit code 2)"]
    assert hook_results_record({}, tmp_path) == {}


# ---------------------------------------------------------------------------
# Config default
# ---------------------------------------------------------------------------

def test_after_each_on_failure_defaults_to_continue(tmp_path):
    p = tmp_path / "eval.yaml"
    p.write_text(textwrap.dedent("""\
        name: t
        execution:
          skill: s
        hooks:
          before_each:
            - command: "true"
          after_each:
            - command: "true"
            - command: "true"
              on_failure: fail
    """))
    cfg = EvalConfig.from_yaml(p)
    assert cfg.hooks.before_each[0].on_failure == "fail"
    assert cfg.hooks.after_each[0].on_failure == "continue"
    assert cfg.hooks.after_each[1].on_failure == "fail"


# ---------------------------------------------------------------------------
# _run_single_case
# ---------------------------------------------------------------------------

def _runner(exit_code=0, side_effect=None):
    runner = unittest.mock.MagicMock()
    runner.execute.return_value = RunResult(
        exit_code=exit_code, stdout="ok", stderr="", duration_s=1.0,
        cost_usd=0.01, num_turns=1)
    if side_effect is not None:
        runner.execute.side_effect = side_effect
    return runner


def _single(tmp_path, before_each=None, after_each=None, runner=None):
    case_ws = tmp_path / "workspace" / "cases" / "case-001"
    case_ws.mkdir(parents=True)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    dataset = tmp_path / "dataset"
    (dataset / "case-001").mkdir(parents=True)
    config = EvalConfig()
    config.skill = "test-skill"
    config.hooks = HooksConfig(before_each=before_each or [],
                               after_each=after_each or [])
    config.dataset = DatasetConfig(path=str(dataset))
    config.config_dir = tmp_path
    hook_env = build_hook_env(
        workspace=str(tmp_path / "workspace"), run_id="r",
        config_path="eval.yaml", project_root=str(Path.cwd()),
        model="sonnet")
    _, result = ex._run_single_case(
        runner or _runner(), "test-skill", "case-001", case_ws, output_dir,
        "", "sonnet", None, None, None, 5.0, 600, 1, 1,
        config=config, hook_env=hook_env)
    case_output = output_dir / "cases" / "case-001"
    on_disk = json.loads((case_output / "run_result.json").read_text())
    return result, on_disk, case_output


def test_after_each_failure_continue_is_recorded(tmp_path):
    result, on_disk, _ = _single(tmp_path, after_each=[
        HookEntry(command="exit 1", on_failure="continue",
                  description="capture")])
    assert on_disk["exit_code"] == 0  # case unchanged
    assert "error" not in on_disk
    assert on_disk["hooks"]["after_each"][0]["exit_code"] == 1
    assert on_disk["hook_failures"] == ["after_each[0]: capture (exit code 1)"]
    assert result["hook_failures"] == on_disk["hook_failures"]


def test_after_each_failure_fail_fails_case_and_runs_rest(tmp_path):
    marker = tmp_path / "teardown.txt"
    _, on_disk, _ = _single(tmp_path, after_each=[
        HookEntry(command="exit 4", on_failure="fail", description="capture"),
        HookEntry(command=f"echo done > {marker}", on_failure="continue")])
    assert marker.exists(), "remaining after_each hooks must still run"
    assert on_disk["exit_code"] == 1
    assert "capture" in on_disk["error"]
    assert len(on_disk["hooks"]["after_each"]) == 2


def test_after_each_timeout_is_recorded(tmp_path):
    _, on_disk, _ = _single(tmp_path, after_each=[
        HookEntry(command="sleep 30", timeout=1, on_failure="continue",
                  description="slow capture")])
    entry = on_disk["hooks"]["after_each"][0]
    assert entry["timed_out"] is True
    assert on_disk["hook_failures"] == [
        "after_each[0]: slow capture (timed out)"]
    assert on_disk["exit_code"] == 0


def test_runner_crash_keeps_error_and_records_hooks(tmp_path):
    _, on_disk, _ = _single(
        tmp_path,
        after_each=[HookEntry(command="exit 1", on_failure="fail",
                              description="capture")],
        runner=_runner(side_effect=RuntimeError("boom")))
    assert on_disk["exit_code"] == 1
    assert on_disk["error"].startswith("boom")
    assert "capture" in on_disk["error"]
    assert on_disk["hooks"]["after_each"][0]["exit_code"] == 1


def test_before_each_failure_is_recorded(tmp_path):
    _, on_disk, _ = _single(
        tmp_path,
        before_each=[HookEntry(command="true"),
                     HookEntry(command="exit 2", on_failure="fail",
                               description="seed")],
        after_each=[HookEntry(command="true", on_failure="continue")])
    assert on_disk["exit_code"] == 1
    assert [h["exit_code"] for h in on_disk["hooks"]["before_each"]] == [0, 2]
    assert on_disk["hook_failures"] == ["before_each[1]: seed (exit code 2)"]
    assert on_disk["hooks"]["after_each"][0]["exit_code"] == 0


def test_no_hooks_no_new_fields(tmp_path):
    _, on_disk, _ = _single(tmp_path)
    assert "hooks" not in on_disk and "hook_failures" not in on_disk


def test_after_each_outputs_merged_and_visible_to_judges(tmp_path):
    before = ('printf "data:\\n  seeded: 1\\n  shared: before\\n"'
              ' > .hook-outputs.yaml')
    after = ('printf "data:\\n  pr_url: https://example.test/pr/1\\n'
             '  shared: after\\n" > .hook-outputs.yaml')
    _, _, case_output = _single(
        tmp_path,
        before_each=[HookEntry(command=before)],
        after_each=[HookEntry(command=after, on_failure="continue"),
                    HookEntry(command="exit 1", on_failure="continue",
                              description="run tests")])
    data = yaml.safe_load((case_output / "hook_outputs.yaml").read_text())
    assert data == {"seeded": 1, "pr_url": "https://example.test/pr/1",
                    "shared": "after"}  # later phase wins

    record = load_case_record(case_output, EvalConfig())
    assert record["hook_outputs"]["pr_url"] == "https://example.test/pr/1"
    assert record["hook_failures"] == ["after_each[1]: run tests (exit code 1)"]
    assert record["hooks"]["after_each"][1]["exit_code"] == 1

    jc = types.SimpleNamespace(
        name="gate", arguments=None,
        check=textwrap.dedent("""\
            if outputs.get("hook_failures"):
                return False, "; ".join(outputs["hook_failures"])
            return True, "clean"
        """))
    value, rationale = _make_inline_check(jc)(outputs=record)
    assert value is False and "run tests" in rationale
    assert eval("not outputs.get('hook_failures')", {"__builtins__": {}},
                {"outputs": record}) is False


def test_record_without_hooks_has_no_hook_keys(tmp_path):
    _, _, case_output = _single(tmp_path)
    record = load_case_record(case_output, EvalConfig())
    assert "hooks" not in record and "hook_failures" not in record


# ---------------------------------------------------------------------------
# _run_multi_step_case
# ---------------------------------------------------------------------------

class _StepRunner:
    name = "fake"

    def execute(self, target=None, workspace=None, **kw):
        return RunResult(exit_code=0, stdout="", stderr="", duration_s=1.0,
                         token_usage={"input": 1, "output": 1}, cost_usd=0.01,
                         num_turns=1, per_model_usage={}, per_model_turns={})


def _multi_step(tmp_path, hooks_yaml):
    p = tmp_path / "eval.yaml"
    p.write_text(textwrap.dedent("""\
        name: t
        execution:
          mode: case
          steps:
            - {id: a, skill: s1, arguments: x}
        dataset: {path: __TMP__/cases}
        outputs:
          - path: output
    """).replace("__TMP__", str(tmp_path)) + hooks_yaml)
    cfg = EvalConfig.from_yaml(p)
    cw = tmp_path / "cases" / "case-1"
    cw.mkdir(parents=True)
    (cw / "input.yaml").write_text("id: '1'\n")
    out = tmp_path / "run"
    out.mkdir()
    hook_env = build_hook_env(
        workspace=str(tmp_path), run_id="r", config_path=str(p),
        project_root=str(Path.cwd()), model="opus")
    _, res = ex._run_multi_step_case(
        _StepRunner(), "case-1", cw, out, "opus", None, None, "",
        5.0, 600, 1, 1, cfg, hook_env=hook_env)
    on_disk = json.loads(
        (out / "cases" / "case-1" / "run_result.json").read_text())
    return res, on_disk, out / "cases" / "case-1"


def test_multi_step_after_each_fail_fails_case(tmp_path):
    _, on_disk, case_output = _multi_step(tmp_path, textwrap.dedent("""\
        hooks:
          after_each:
            - command: 'printf "data:\\n  captured: pr-1\\n" > .hook-outputs.yaml'
            - command: "exit 1"
              on_failure: fail
              description: capture
    """))
    assert on_disk["exit_code"] == 1
    assert "capture" in on_disk["error"]
    assert on_disk["hook_failures"] == ["after_each[1]: capture (exit code 1)"]
    data = yaml.safe_load((case_output / "hook_outputs.yaml").read_text())
    assert data == {"captured": "pr-1"}


def test_multi_step_after_each_default_continue(tmp_path):
    _, on_disk, _ = _multi_step(tmp_path, textwrap.dedent("""\
        hooks:
          after_each:
            - command: "exit 1"
    """))
    assert on_disk["exit_code"] == 0
    assert "error" not in on_disk
    assert len(on_disk["hook_failures"]) == 1
