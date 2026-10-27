"""Tests for per-model cost attribution in trace_builder.

Covers:
- Root span must NOT have mlflow.llm.cost or mlflow.llm.model (prevents
  double-counting in the "Cost Over Time" chart).
- Main LLM spans get the orchestrator model.
- Subagent LLM spans get the subagent model.
- Per-model costs from run_result.per_model_usage are distributed evenly
  across each model's LLM spans.
- Model name normalization: "@" in per_model_usage keys matches "-" in
  span model names (e.g. claude-sonnet-4-5@20250929 vs -20250929).
- Trace-level mlflow.trace.cost metadata includes per-model breakdown.
"""

import json

import pytest

from conftest import (
    make_assistant, make_result, make_system_init, make_user,
)

from agent_eval.mlflow.trace_builder import build_trace


def _write_stream(tmp_path, events):
    stdout = tmp_path / "stdout.log"
    stdout.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return stdout


def _hybrid_run_result():
    """Run result with opus main + sonnet subagents and per-model usage."""
    return {
        "exit_code": 0,
        "duration_s": 30.0,
        "cost_usd": 5.00,
        "model": "claude-opus-4-6",
        "subagent_model": "claude-sonnet-4-5-20250929",
        "token_usage": {"input": 500, "output": 200,
                        "cache_read": 0, "cache_create": 0},
        "per_model_usage": {
            "claude-opus-4-6": {
                "input": 200, "output": 80,
                "cache_read": 0, "cache_create": 0,
                "cost_usd": 2.00,
            },
            # Note: "@" in key — must match "-" on spans
            "claude-sonnet-4-5@20250929": {
                "input": 300, "output": 120,
                "cache_read": 0, "cache_create": 0,
                "cost_usd": 3.00,
            },
        },
    }


def _single_model_run_result():
    """Run result with a single model (no subagent)."""
    return {
        "exit_code": 0,
        "duration_s": 10.0,
        "cost_usd": 2.00,
        "model": "claude-opus-4-6",
        "token_usage": {"input": 200, "output": 80,
                        "cache_read": 0, "cache_create": 0},
        "per_model_usage": {
            "claude-opus-4-6": {
                "input": 200, "output": 80,
                "cache_read": 0, "cache_create": 0,
                "cost_usd": 2.00,
            },
        },
    }


def _hybrid_stream_events():
    """Stream with main orchestrator text + Agent tool call with inline subagent children."""
    return [
        make_system_init(model="claude-opus-4-6"),
        # Main orchestrator reasoning (top-level LLM span → opus)
        make_assistant("msg_001", text="Planning the work.",
                       model="claude-opus-4-6"),
        # Main orchestrator launches an Agent
        make_assistant("msg_002",
                       tools=[("Agent", "tu_agent_1",
                               {"description": "create RFE"})],
                       model="claude-opus-4-6"),
        # Inline foreground subagent messages (post-2.1.108)
        make_assistant("msg_sub_001", parent_tool_use_id="tu_agent_1",
                       text="Creating the RFE now.",
                       model="claude-sonnet-4-5-20250929"),
        make_assistant("msg_sub_002", parent_tool_use_id="tu_agent_1",
                       text="RFE content written.",
                       model="claude-sonnet-4-5-20250929"),
        # Agent tool result
        make_user(tool_results=[("tu_agent_1", "RFE created")]),
        # Main orchestrator summarizes
        make_assistant("msg_003", text="Done.",
                       model="claude-opus-4-6"),
        make_result(cost_usd=5.00, num_turns=5),
    ]


def _get_span_attr(span, key):
    """Extract a JSON attribute from a span."""
    raw = span["attributes"].get(key)
    if raw is None:
        return None
    return json.loads(raw)


def _get_span_type(span):
    return _get_span_attr(span, "mlflow.spanType")


def _get_llm_spans(spans):
    return [s for s in spans if _get_span_type(s) == "LLM"]


class TestRootSpanNoCost:
    """Root AGENT span must not have mlflow.llm.cost or mlflow.llm.model."""

    def test_root_has_no_llm_cost(self, tmp_path):
        events = _hybrid_stream_events()
        stdout = _write_stream(tmp_path, events)
        trace = build_trace(stdout, _hybrid_run_result(),
                            run_id="test", experiment_id="exp-1")
        root = next(s for s in trace["data"]["spans"]
                    if s["parent_span_id"] is None)
        assert "mlflow.llm.cost" not in root["attributes"]

    def test_root_has_no_llm_model(self, tmp_path):
        events = _hybrid_stream_events()
        stdout = _write_stream(tmp_path, events)
        trace = build_trace(stdout, _hybrid_run_result(),
                            run_id="test", experiment_id="exp-1")
        root = next(s for s in trace["data"]["spans"]
                    if s["parent_span_id"] is None)
        assert "mlflow.llm.model" not in root["attributes"]

    def test_root_has_no_llm_cost_single_model(self, tmp_path):
        """Even single-model runs should not set cost on root."""
        events = [
            make_system_init(),
            make_assistant("msg_001", text="Hello."),
            make_result(cost_usd=2.00, num_turns=1),
        ]
        stdout = _write_stream(tmp_path, events)
        trace = build_trace(stdout, _single_model_run_result(),
                            run_id="test", experiment_id="exp-1")
        root = next(s for s in trace["data"]["spans"]
                    if s["parent_span_id"] is None)
        assert "mlflow.llm.cost" not in root["attributes"]


class TestSubagentModelAttribution:
    """LLM spans inside Agent scopes use the subagent model."""

    def test_main_llm_spans_use_main_model(self, tmp_path):
        events = _hybrid_stream_events()
        stdout = _write_stream(tmp_path, events)
        trace = build_trace(stdout, _hybrid_run_result(),
                            run_id="test", experiment_id="exp-1")
        spans = trace["data"]["spans"]
        root = next(s for s in spans if s["parent_span_id"] is None)

        # Find LLM spans that are direct children of step spans
        # (which are direct children of root) — these are main model
        step_ids = {s["span_id"] for s in spans
                    if s["parent_span_id"] == root["span_id"]
                    and _get_span_type(s) == "AGENT"}
        main_llm = [s for s in _get_llm_spans(spans)
                    if s["parent_span_id"] in step_ids]
        assert len(main_llm) > 0
        for s in main_llm:
            assert _get_span_attr(s, "mlflow.llm.model") == "claude-opus-4-6"

    def test_subagent_llm_spans_use_subagent_model(self, tmp_path):
        events = _hybrid_stream_events()
        stdout = _write_stream(tmp_path, events)
        trace = build_trace(stdout, _hybrid_run_result(),
                            run_id="test", experiment_id="exp-1")
        spans = trace["data"]["spans"]

        # Find Agent tool spans
        agent_spans = [s for s in spans
                       if s["name"] == "Agent"
                       and _get_span_type(s) == "AGENT"]
        agent_ids = {s["span_id"] for s in agent_spans}

        # LLM spans under Agent spans are subagent LLM calls
        sub_llm = [s for s in _get_llm_spans(spans)
                   if s["parent_span_id"] in agent_ids]
        assert len(sub_llm) > 0
        for s in sub_llm:
            assert _get_span_attr(s, "mlflow.llm.model") == \
                "claude-sonnet-4-5-20250929"

    def test_subagent_model_from_run_result(self, tmp_path):
        """subagent_model is picked up from run_result if not passed explicitly."""
        events = [
            make_system_init(),
            make_assistant("msg_001", text="Hello."),
            make_assistant("msg_002",
                           tools=[("Agent", "tu_a", {"description": "work"})]),
            # Inline subagent message
            make_assistant("msg_sub", parent_tool_use_id="tu_a",
                           text="Subagent working."),
            make_user(tool_results=[("tu_a", "done")]),
            make_result(cost_usd=5.00, num_turns=3),
        ]
        stdout = _write_stream(tmp_path, events)
        run_result = _hybrid_run_result()
        # Don't pass subagent_model explicitly — should read from run_result
        trace = build_trace(stdout, run_result,
                            run_id="test", experiment_id="exp-1")
        spans = trace["data"]["spans"]
        agent_spans = [s for s in spans if s["name"] == "Agent"
                       and _get_span_type(s) == "AGENT"]
        agent_ids = {s["span_id"] for s in agent_spans}
        sub_llm = [s for s in _get_llm_spans(spans)
                   if s["parent_span_id"] in agent_ids]
        assert len(sub_llm) > 0
        for s in sub_llm:
            assert _get_span_attr(s, "mlflow.llm.model") == \
                "claude-sonnet-4-5-20250929"


class TestPerModelCostDistribution:
    """per_model_usage costs are distributed evenly across LLM spans."""

    def test_costs_sum_to_per_model_total(self, tmp_path):
        events = _hybrid_stream_events()
        stdout = _write_stream(tmp_path, events)
        trace = build_trace(stdout, _hybrid_run_result(),
                            run_id="test", experiment_id="exp-1")
        spans = trace["data"]["spans"]

        # Aggregate cost per model from LLM spans
        model_costs = {}
        for s in _get_llm_spans(spans):
            m = _get_span_attr(s, "mlflow.llm.model")
            cost = _get_span_attr(s, "mlflow.llm.cost")
            tc = cost["total_cost"] if cost else 0
            model_costs[m] = model_costs.get(m, 0) + tc

        assert model_costs.get("claude-opus-4-6") == pytest.approx(2.00)
        assert model_costs.get("claude-sonnet-4-5-20250929") == \
            pytest.approx(3.00)

    def test_total_cost_matches_run_result(self, tmp_path):
        events = _hybrid_stream_events()
        stdout = _write_stream(tmp_path, events)
        trace = build_trace(stdout, _hybrid_run_result(),
                            run_id="test", experiment_id="exp-1")
        spans = trace["data"]["spans"]

        total = sum(_get_span_attr(s, "mlflow.llm.cost")["total_cost"]
                    for s in _get_llm_spans(spans)
                    if _get_span_attr(s, "mlflow.llm.cost"))
        assert total == pytest.approx(5.00)

    def test_cost_evenly_distributed_within_model(self, tmp_path):
        """Each LLM span of a given model gets the same cost share."""
        events = _hybrid_stream_events()
        stdout = _write_stream(tmp_path, events)
        trace = build_trace(stdout, _hybrid_run_result(),
                            run_id="test", experiment_id="exp-1")
        spans = trace["data"]["spans"]

        # Group costs by model
        per_model_spans = {}
        for s in _get_llm_spans(spans):
            m = _get_span_attr(s, "mlflow.llm.model")
            cost = _get_span_attr(s, "mlflow.llm.cost")
            if cost:
                per_model_spans.setdefault(m, []).append(cost["total_cost"])

        # Within each model, all spans should have equal cost
        for m, costs in per_model_spans.items():
            assert len(set(round(c, 10) for c in costs)) == 1, \
                f"Uneven cost distribution for {m}: {costs}"


class TestModelNameNormalization:
    """@ in per_model_usage keys matches - in span model names."""

    def test_at_sign_normalized_to_dash(self, tmp_path):
        """per_model_usage key 'X@Y' matches spans with model 'X-Y'."""
        events = _hybrid_stream_events()
        stdout = _write_stream(tmp_path, events)
        run_result = _hybrid_run_result()

        # Verify the test setup: per_model_usage has "@", subagent_model has "-"
        assert "claude-sonnet-4-5@20250929" in run_result["per_model_usage"]
        assert run_result["subagent_model"] == "claude-sonnet-4-5-20250929"

        trace = build_trace(stdout, run_result,
                            run_id="test", experiment_id="exp-1")
        spans = trace["data"]["spans"]

        # Sonnet spans must have cost despite the @ vs - mismatch
        sonnet_costs = [
            _get_span_attr(s, "mlflow.llm.cost")
            for s in _get_llm_spans(spans)
            if _get_span_attr(s, "mlflow.llm.model") ==
               "claude-sonnet-4-5-20250929"
        ]
        assert len(sonnet_costs) > 0
        assert all(c is not None and c["total_cost"] > 0
                   for c in sonnet_costs)

    def test_no_per_model_usage_cost_distributed_evenly(self, tmp_path):
        """Without per_model_usage, cost_usd is distributed evenly across LLM spans."""
        events = [
            make_system_init(),
            make_assistant("msg_001", text="Hello."),
            make_result(cost_usd=1.00, num_turns=1),
        ]
        stdout = _write_stream(tmp_path, events)
        run_result = {
            "exit_code": 0, "duration_s": 5.0, "cost_usd": 1.00,
            "model": "claude-opus-4-6",
            "token_usage": {"input": 50, "output": 20,
                            "cache_read": 0, "cache_create": 0},
        }
        trace = build_trace(stdout, run_result,
                            run_id="test", experiment_id="exp-1")
        spans = trace["data"]["spans"]
        llm_spans = _get_llm_spans(spans)
        assert len(llm_spans) > 0, "expected at least one LLM span"
        total_cost = sum(
            json.loads(s["attributes"]["mlflow.llm.cost"])["total_cost"]
            for s in llm_spans
            if "mlflow.llm.cost" in s["attributes"]
        )
        assert total_cost == pytest.approx(1.00)


class TestTraceCostMetadata:
    """Trace-level mlflow.trace.cost includes per-model breakdown."""

    def test_trace_cost_has_per_model_keys(self, tmp_path):
        events = _hybrid_stream_events()
        stdout = _write_stream(tmp_path, events)
        trace = build_trace(stdout, _hybrid_run_result(),
                            run_id="test", experiment_id="exp-1")
        meta = trace["info"]["trace_metadata"]
        cost = json.loads(meta["mlflow.trace.cost"])
        assert cost["total_cost"] == pytest.approx(5.00)
        assert cost["claude-opus-4-6"] == pytest.approx(2.00)
        assert cost["claude-sonnet-4-5@20250929"] == pytest.approx(3.00)

    def test_trace_cost_without_per_model(self, tmp_path):
        """Without per_model_usage, trace cost has only total_cost."""
        events = [
            make_system_init(),
            make_assistant("msg_001", text="Hi."),
            make_result(cost_usd=1.00, num_turns=1),
        ]
        stdout = _write_stream(tmp_path, events)
        run_result = {
            "exit_code": 0, "duration_s": 5.0, "cost_usd": 1.00,
            "model": "claude-opus-4-6",
            "token_usage": {"input": 50, "output": 20,
                            "cache_read": 0, "cache_create": 0},
        }
        trace = build_trace(stdout, run_result,
                            run_id="test", experiment_id="exp-1")
        meta = trace["info"]["trace_metadata"]
        cost = json.loads(meta["mlflow.trace.cost"])
        assert cost == {"total_cost": 1.00}
