"""run_result.json must not silently drop RunResult telemetry.

The runner has carried permission_denials since the CLI-side union fix, but
execute.py never serialized it: on a real run, the only permission denial (a
workspace-escape attempt by a subagent) survived solely in raw stdout.log —
invisible to run_result.json, summary.yaml and every downstream reader, which
is exactly how an earlier escape stayed hidden until a manual log audit.

Pinned structurally (like test_venv_activation pins import order): every dict
literal in execute.py that serializes per-RunResult telemetry (identified by a
"per_model_turns" key fed from a result attribute) must also carry
"permission_denials". This guards future result-dict sites too.
"""

import ast
from pathlib import Path

from agent_eval.agent.base import RunResult

EXECUTE = (Path(__file__).resolve().parent.parent
           / "skills" / "eval-run" / "scripts" / "execute.py")


def _dict_keys(node):
    keys = []
    for k in node.keys:
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            keys.append(k.value)
    return keys


def _serializes_runresult(node):
    """True for any dict literal carrying a per_model_turns key — direct
    RunResult serialization sites AND aggregation dicts. The first version
    excluded aggregation, which is exactly where the multi-step case-level
    dict silently dropped permission_denials."""
    return "per_model_turns" in _dict_keys(node)


def test_every_runresult_dict_carries_permission_denials():
    tree = ast.parse(EXECUTE.read_text(), filename=str(EXECUTE))
    sites = [n for n in ast.walk(tree)
             if isinstance(n, ast.Dict) and _serializes_runresult(n)]
    assert sites, "no RunResult serialization sites found — did execute.py move?"
    missing = [n.lineno for n in sites
               if "permission_denials" not in _dict_keys(n)]
    assert not missing, (
        f"RunResult-serializing dict(s) at execute.py line(s) {missing} drop "
        f"permission_denials — denial telemetry would silently vanish from "
        f"run_result.json again.")


def test_multi_step_aggregate_carries_denials():
    """Case-level dicts for multi-step cases must surface the union of their
    steps' denials — not hide them under steps[*].permission_denials."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("eval_run_execute", EXECUTE)
    execute = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(execute)
    d1 = {"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "x"}}
    d2 = {"tool_name": "Read", "tool_use_id": "t2", "tool_input": {"path": "y"}}
    agg = execute._aggregate_step_metrics({
        "step-a": {"exit_code": 0, "permission_denials": [d1]},
        "step-b": {"exit_code": 0, "permission_denials": [d1, d2]},
    })
    assert agg["permission_denials"] == [d1, d2]


def test_to_case_result_defaults_empty_fields():
    result = RunResult(exit_code=1, stdout="", stderr="", duration_s=0.04)
    assert result.to_case_result() == {
        "exit_code": 1,
        "duration_s": 0.0,
        "token_usage": None,
        "cost_usd": None,
        "num_turns": None,
        "per_model_usage": None,
        "per_model_turns": None,
        "permission_denials": [],
        "message_ids": [],
        "generation_ids": [],
        "cost_source": None,
        "error_class": None,
        "budget": None,
    }


def test_to_case_result_serializes_present_fields():
    result = RunResult(
        exit_code=0, stdout="s", stderr="e", duration_s=12.34,
        token_usage={"input": 1200, "output": 340},
        cost_usd=0.42, num_turns=7,
        per_model_usage={"claude-opus-4-6": {"cost_usd": 0.11}},
        per_model_turns={"claude-opus-4-6": 2, "claude-haiku-4-5": 1},
        permission_denials=[{"tool_name": "Bash", "tool_use_id": "t1",
                             "tool_input": {}, "reason": "blocked"}],
        message_ids=["msg_1"], generation_ids=["gen-1"],
        cost_source="ledger", error_class=None, budget={"exceeded": False})
    assert result.to_case_result() == {
        "exit_code": 0,
        "duration_s": 12.3,
        "token_usage": {"input": 1200, "output": 340},
        "cost_usd": 0.42,
        "num_turns": 7,
        "per_model_usage": {"claude-opus-4-6": {"cost_usd": 0.11}},
        "per_model_turns": {"claude-opus-4-6": 2, "claude-haiku-4-5": 1},
        "permission_denials": [{"tool_name": "Bash", "tool_use_id": "t1",
                                "tool_input": {}, "reason": "blocked"}],
        "message_ids": ["msg_1"],
        "generation_ids": ["gen-1"],
        "cost_source": "ledger",
        "error_class": None,
        "budget": {"exceeded": False},
    }
