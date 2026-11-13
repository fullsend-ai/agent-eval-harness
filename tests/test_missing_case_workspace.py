"""A selected case whose workspace is missing must fail the run (#313).

The missing-workspace branch used to return no result, so the case was dropped
from ``case_results`` and the run exit code was aggregated from the surviving
cases only: a run could exit 0 with one fewer result than cases selected.
"""

import json
import shutil
import threading
import types

import execute as ex
import pytest
from agent_eval.agent.base import RunResult
from agent_eval.config import EvalConfig

CASE_IDS = ["case-000", "case-001", "case-002"]


class _FakeRunner:
    """Runner whose ``execute`` can remove another case's workspace, the way a
    case sharing a writable sandbox could."""

    name = "fake"
    version = "0"

    def __init__(self, workspace, victim=None, deleted=None):
        self.workspace = workspace
        self.victim = victim
        self.deleted = deleted or threading.Event()

    @classmethod
    def from_config(cls, config, **kw):
        return cls._instance

    def execute(self, *, workspace, **kw):
        case_id = workspace.name
        if self.victim and case_id == "case-000":
            shutil.rmtree(self.workspace / "cases" / self.victim)
            self.deleted.set()
        elif self.victim:
            # Hold the other worker until the deletion lands, so the victim
            # case cannot have started before its workspace disappeared.
            assert self.deleted.wait(10)
        return RunResult(exit_code=0, stdout=f"{case_id} ok\n", stderr="",
                         duration_s=1.0, cost_usd=0.0, num_turns=1)


def _run(tmp_path, victim=None, parallelism=None):
    workspace = tmp_path / "workspace"
    output_dir = tmp_path / "output"
    for cid in CASE_IDS:
        (workspace / "cases" / cid).mkdir(parents=True)
    output_dir.mkdir()
    (workspace / "case_order.yaml").write_text(
        "".join(f"- {cid}\n" for cid in CASE_IDS))

    runner = _FakeRunner(workspace, victim=victim)
    runner_cls = type("FakeRunnerCls", (_FakeRunner,), {"_instance": runner})
    config = EvalConfig()
    config.skill = "test-skill"
    args = types.SimpleNamespace(
        workspace=str(workspace), run_id="r", config=str(tmp_path / "eval.yaml"),
        input_override=None)
    with pytest.raises(SystemExit) as exc:
        ex._execute_per_case(
            args, config, runner, runner_cls, output_dir, 5.0, 600, "sonnet",
            None, skill_args_template="", parallelism=parallelism,
            target="test-skill")
    run_result = json.loads((output_dir / "run_result.json").read_text())
    return exc.value.code, run_result, output_dir


@pytest.mark.parametrize("victim, parallelism", [
    ("case-001", None),   # sequential
    ("case-002", 2),      # parallel: case-002 is queued behind both workers
])
def test_missing_workspace_fails_the_run(tmp_path, victim, parallelism):
    code, run_result, output_dir = _run(tmp_path, victim, parallelism)

    assert code != 0
    assert run_result["exit_code"] != 0
    # The missing case is recorded, not dropped.
    assert run_result["num_cases"] == len(CASE_IDS)
    assert sorted(run_result["per_case"]) == CASE_IDS
    missing = run_result["per_case"][victim]
    assert missing["exit_code"] != 0
    assert "workspace missing" in missing["error"]
    on_disk = json.loads(
        (output_dir / "cases" / victim / "run_result.json").read_text())
    assert on_disk["exit_code"] != 0
    assert "workspace missing" in on_disk["error"]
    # The cases that ran keep their results and artifacts.
    for cid in CASE_IDS:
        if cid == victim:
            continue
        assert run_result["per_case"][cid]["exit_code"] == 0
        assert (output_dir / "cases" / cid / "stdout.log").read_text() == f"{cid} ok\n"


@pytest.mark.parametrize("parallelism", [None, 2])
def test_all_workspaces_present_passes(tmp_path, parallelism):
    code, run_result, _ = _run(tmp_path, victim=None, parallelism=parallelism)
    assert code == 0
    assert run_result["exit_code"] == 0
    assert sorted(run_result["per_case"]) == CASE_IDS


def test_in_repo_missing_workspace_is_a_failed_result(tmp_path):
    output_dir = tmp_path / "output"
    case_ws = tmp_path / "workspace" / "cases" / "case-007"
    case_id, result = ex._run_single_case_in_repo(
        None, "test-skill", case_ws, output_dir, "", "sonnet", None, None,
        "", 5.0, 600, 1, 1)
    assert case_id == "case-007"
    assert result["exit_code"] != 0
    assert "workspace missing" in result["error"]
    assert (output_dir / "cases" / "case-007" / "run_result.json").exists()


def test_in_repo_missing_metadata_is_a_failed_result(tmp_path):
    output_dir = tmp_path / "output"
    case_ws = tmp_path / "workspace" / "cases" / "case-007"
    case_ws.mkdir(parents=True)
    _, result = ex._run_single_case_in_repo(
        None, "test-skill", case_ws, output_dir, "", "sonnet", None, None,
        "", 5.0, 600, 1, 1)
    assert result["exit_code"] != 0
    assert "metadata missing" in result["error"]
