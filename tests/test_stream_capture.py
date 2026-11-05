"""Tests for agent_eval.agent.stream_capture — turn counting and deduplication."""

import json

from conftest import make_assistant, make_result, to_jsonl

from agent_eval.agent.stream_capture import (
    claude_session_generation_ids,
    _is_real_model, count_subagent_turns, count_subagent_turns_by_model,
    extract_session_id, extract_usage,
)


class TestExtractUsage:
    def test_returns_seen_ids(self, post_2_1_108_stream):
        """extract_usage returns a 7-tuple including the seen_msg_ids set."""
        result = extract_usage(post_2_1_108_stream["lines"])
        assert len(result) == 7
        _, _, num_turns, seen_msg_ids, _, _, _ = result
        assert isinstance(seen_msg_ids, set)
        assert len(seen_msg_ids) == num_turns

    def test_treats_null_usage_fields_as_zero(self):
        """OpenRouter streams may emit explicit null token fields."""
        event = make_assistant(
            "msg_001",
            input_tokens=None,
            output_tokens=3,
        )

        usage, _, _, _, _, _, _ = extract_usage(to_jsonl([event]))

        assert usage == {
            "input": 0,
            "output": 3,
            "cache_read": 0,
            "cache_create": 0,
        }


class TestClaudeSessionGenerationIds:
    def test_reads_root_and_subagent_transcripts(self, tmp_path):
        workspace = tmp_path / "work_space"
        workspace.mkdir()
        session_id = "session-123"
        projects = tmp_path / "projects"
        encoded = str(workspace).replace("/", "-").replace("_", "-")
        session_dir = projects / encoded
        session_dir.mkdir(parents=True)
        (session_dir / f"{session_id}.jsonl").write_text(
            json.dumps({"type": "assistant", "requestId": "gen-root"}) + "\n" +
            json.dumps({"type": "assistant",
                        "message": {"role": "assistant", "id": "gen-legacy"}}) + "\n")
        subagents = session_dir / session_id / "subagents"
        subagents.mkdir(parents=True)
        (subagents / "agent.jsonl").write_text(
            json.dumps({"type": "assistant", "requestId": "gen-sub"}) + "\n" +
            json.dumps({"type": "assistant", "requestId": "msg-not-generation"}) + "\n")

        ids = claude_session_generation_ids(workspace, session_id, projects)

        assert ids == {"gen-root", "gen-sub", "gen-legacy"}

    def test_requires_a_session_id(self, tmp_path):
        assert claude_session_generation_ids(tmp_path, None, tmp_path) == set()

    def test_rejects_unsafe_session_ids(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        projects = tmp_path / "projects"
        projects.mkdir()

        for session_id in ("../escape", "/tmp/escape", "..", "."):
            assert claude_session_generation_ids(workspace, session_id, projects) == set()

    def test_rejects_transcript_symlinks(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        session_id = "session-123"
        projects = tmp_path / "projects"
        encoded = str(workspace).replace("/", "-").replace("_", "-")
        session_dir = projects / encoded
        session_dir.mkdir(parents=True)
        outside = tmp_path / "outside.jsonl"
        outside.write_text(json.dumps({"requestId": "gen-outside"}) + "\n")
        (session_dir / f"{session_id}.jsonl").symlink_to(outside)

        assert claude_session_generation_ids(workspace, session_id, projects) == set()

    def test_extracts_the_latest_result_session_id(self):
        lines = [
            json.dumps({"type": "result", "session_id": "old"}),
            json.dumps({"type": "result", "session_id": "new"}),
        ]

        assert extract_session_id(lines) == "new"

    def test_falls_back_to_init_session_id_before_result(self):
        lines = [
            json.dumps({"type": "system", "subtype": "init", "session_id": "session-123"}),
        ]

        assert extract_session_id(lines) == "session-123"

    def test_includes_subagent_ids_post_2_1_108(self, post_2_1_108_stream):
        """Post-2.1.108: seen_msg_ids includes foreground subagent message IDs."""
        _, _, num_turns, seen_ids, _, _, _ = extract_usage(post_2_1_108_stream["lines"])
        assert num_turns == 7  # 5 root + 2 foreground subagent
        assert "msg_001" in seen_ids
        assert "msg_sub_001" in seen_ids
        assert "msg_sub_002" in seen_ids

    def test_prefers_model_usage(self):
        """When modelUsage is present in result, token_usage comes from it."""
        events = [
            make_assistant("msg_001", input_tokens=100, output_tokens=50),
            make_result(model_usage={
                "claude-sonnet-4-5": {
                    "inputTokens": 9000,
                    "outputTokens": 3000,
                    "cacheReadInputTokens": 500,
                    "cacheCreationInputTokens": 200,
                    "costUSD": 0.42,
                },
            }),
        ]
        token_usage, _, _, _, _, per_model, _ = extract_usage(to_jsonl(events))
        # Should use modelUsage totals, not assistant event usage
        assert token_usage["input"] == 9000
        assert token_usage["output"] == 3000
        assert per_model["claude-sonnet-4-5"]["cost_usd"] == 0.42

    def test_returns_per_model_msg_ids(self):
        """seen_msg_ids_by_model groups assistant message IDs by their model."""
        events = [
            make_assistant("msg_001", model="claude-opus-4-7"),
            make_assistant("msg_002", model="claude-opus-4-7"),
            make_assistant("msg_003", model="claude-sonnet-4-6"),
        ]
        _, _, num_turns, _, _, _, by_model = extract_usage(to_jsonl(events))
        assert by_model == {
            "claude-opus-4-7": {"msg_001", "msg_002"},
            "claude-sonnet-4-6": {"msg_003"},
        }
        assert num_turns == 3


class TestCountSubagentTurns:
    def test_deduplicates_with_already_seen(self, tmp_path, post_2_1_108_stream):
        """With already_seen, only genuinely new IDs are counted."""
        already_seen = {"msg_sub_001", "msg_sub_002"}
        new = count_subagent_turns(
            post_2_1_108_stream["subagent_dir"], already_seen=already_seen)
        assert new == 1  # only msg_sub_003

    def test_counts_all_without_already_seen(self, post_2_1_108_stream):
        """Without already_seen, counts all unique assistant IDs."""
        total = count_subagent_turns(post_2_1_108_stream["subagent_dir"])
        assert total == 3

    def test_returns_zero_for_missing_dir(self, tmp_path):
        """Non-existent directory returns 0."""
        assert count_subagent_turns(tmp_path / "nonexistent") == 0


class TestCountSubagentTurnsByModel:
    def test_returns_empty_for_missing_dir(self, tmp_path):
        """Non-existent directory returns empty dict."""
        assert count_subagent_turns_by_model(tmp_path / "nonexistent") == {}

    def test_groups_new_turns_by_model(self, post_2_1_108_stream):
        """Without already_seen, groups all subagent turns by their model."""
        per_model = count_subagent_turns_by_model(
            post_2_1_108_stream["subagent_dir"])
        # All subagent turns should be attributed to some model. Sum equals
        # the int returned by count_subagent_turns for the same dir.
        assert sum(per_model.values()) == 3

    def test_dedupes_against_already_seen_per_model(self, post_2_1_108_stream):
        """With already_seen_by_model, only NEW per-model IDs are counted."""
        # Pre-seed with all 3 subagent IDs under whatever model they belong to;
        # we don't know that model from the fixture, so use the subagent dir's
        # actual content to construct already_seen_by_model from a first pass.
        all_seen = count_subagent_turns_by_model(
            post_2_1_108_stream["subagent_dir"])
        # Re-pass with the already-counted IDs as "seen" per model — should be 0.
        # Use the by-model totals from a no-op call as a proxy.
        # (Simpler: pass an exhaustive set per model.)
        seeded = {m: {"msg_sub_001", "msg_sub_002", "msg_sub_003"} for m in all_seen}
        new = count_subagent_turns_by_model(
            post_2_1_108_stream["subagent_dir"], already_seen_by_model=seeded)
        assert new == {}


class TestCombinedTurnCount:
    def test_no_double_count_post_2_1_108(self, post_2_1_108_stream):
        """Post-2.1.108: combined count deduplicates overlapping IDs."""
        _, _, stream_turns, stream_ids, _, _, _ = extract_usage(
            post_2_1_108_stream["lines"])
        new_turns = count_subagent_turns(
            post_2_1_108_stream["subagent_dir"], already_seen=stream_ids)
        total = stream_turns + new_turns
        assert total == post_2_1_108_stream["expected_total"]  # 8, not 10

    def test_no_overlap_pre_2_1_108(self, pre_2_1_108_stream):
        """Pre-2.1.108: no overlap, sum is correct without dedup changing anything."""
        _, _, stream_turns, stream_ids, _, _, _ = extract_usage(
            pre_2_1_108_stream["lines"])
        new_turns = count_subagent_turns(
            pre_2_1_108_stream["subagent_dir"], already_seen=stream_ids)
        total = stream_turns + new_turns
        assert total == pre_2_1_108_stream["expected_total"]  # 8
        assert stream_turns == 5
        assert new_turns == 3


class TestPlaceholderModel:
    """`<synthetic>` (and other <bracketed> pseudo-models) are counted for
    dedup/totals but excluded from per-model accounting."""

    def test_is_real_model(self):
        assert _is_real_model("claude-opus-4-7") is True
        assert _is_real_model("<synthetic>") is False
        assert _is_real_model("") is False
        assert _is_real_model(None) is False

    def test_extract_usage_excludes_placeholder(self):
        events = [
            make_assistant("msg_real", model="claude-opus-4-7"),
            make_assistant("msg_synth", model="<synthetic>"),
            make_result(cost_usd=0.10),
        ]
        (_tok, _cost, num_turns, seen_ids, models_seen,
         _per_model, by_model) = extract_usage(to_jsonl(events))
        # Placeholder turn still counts toward totals and dedup ...
        assert num_turns == 2
        assert "msg_synth" in seen_ids
        # ... but never pollutes per-model accounting: both models_seen and the
        # per-model msg-id map exclude the pseudo-model.
        assert models_seen == {"claude-opus-4-7"}
        assert set(by_model) == {"claude-opus-4-7"}

    def test_subagent_turns_exclude_placeholder(self, tmp_path):
        subdir = tmp_path / "subagents"
        subdir.mkdir()
        lines = [
            {"message": {"id": "s1", "role": "assistant",
                         "model": "claude-opus-4-7"}},
            {"message": {"id": "s2", "role": "assistant",
                         "model": "<synthetic>"}},
        ]
        (subdir / "agent-x.jsonl").write_text(
            "\n".join(json.dumps(x) for x in lines) + "\n")
        per_model = count_subagent_turns_by_model(subdir)
        assert per_model == {"claude-opus-4-7": 1}
        assert "<synthetic>" not in per_model


class TestMessageIds:
    """Claude message ids and OpenRouter generation ids remain separate."""

    def test_subagent_ids_dedupe_and_ignore_non_assistant(self, tmp_path):
        from agent_eval.agent.stream_capture import subagent_message_ids
        from agent_eval.agent.stream_capture import subagent_generation_ids

        sub = tmp_path / "subagents"
        sub.mkdir()
        (sub / "a.jsonl").write_text("\n".join([
            json.dumps({"requestId": "gen-a1", "message": {"role": "assistant", "id": "msg-a1"}}),
            json.dumps({"requestId": "gen-a1", "message": {"role": "assistant", "id": "msg-a1"}}),
            json.dumps({"message": {"role": "assistant", "id": "gen-legacy"}}),
            json.dumps({"message": {"role": "user", "id": "u1"}}),
            "not json",
            json.dumps({"requestId": "msg-not-generation", "message": {
                "role": "assistant", "id": "msg-a2"}}),
        ]))
        (sub / "notes.txt").write_text("ignored")
        assert subagent_message_ids(sub) == {"msg-a1", "msg-a2", "gen-legacy"}
        assert subagent_generation_ids(sub) == {"gen-a1", "gen-legacy"}
        assert subagent_message_ids(tmp_path / "missing") == set()
        assert subagent_generation_ids(tmp_path / "missing") == set()

    def test_ids_for_run_merge_root_and_subagents(self, tmp_path):
        from agent_eval.agent.stream_capture import message_ids_for_run
        from agent_eval.agent.stream_capture import generation_ids_for_run

        sub = tmp_path / "subagents"
        sub.mkdir()
        (sub / "a.jsonl").write_text(json.dumps({
            "requestId": "gen-sub", "message": {"role": "assistant", "id": "msg-sub"}}) + "\n")
        assert message_ids_for_run({"msg-root", "msg-sub"}, sub) == ["msg-root", "msg-sub"]
        assert generation_ids_for_run({"gen-root", "gen-sub"}, sub) == ["gen-root", "gen-sub"]
        assert message_ids_for_run(None, tmp_path / "none") == []
        assert generation_ids_for_run(None, tmp_path / "none") == []


class TestProviderView:
    """spec 014: ids sighted per event; the run's visible provider errors."""

    def test_assistant_message_id(self):
        from agent_eval.agent.stream_capture import assistant_message_id
        from agent_eval.agent.stream_capture import assistant_event_ids
        from agent_eval.agent.stream_capture import extract_generation_ids

        event = make_assistant("msg-1", model="z-ai/glm-5.2", request_id="gen-1")
        legacy_event = make_assistant("gen-legacy", model="z-ai/glm-5.2")
        assert assistant_message_id(make_assistant("gen-1", model="z-ai/glm-5.2")) == ("gen-1", "z-ai/glm-5.2")
        assert assistant_event_ids(event) == ("msg-1", "gen-1", "z-ai/glm-5.2")
        assert assistant_event_ids(legacy_event) == ("gen-legacy", "gen-legacy", "z-ai/glm-5.2")
        assert assistant_event_ids(make_assistant("msg-2")) == ("msg-2", None, "claude-sonnet-4-5")
        assert assistant_message_id({"type": "result"}) == (None, None)
        assert assistant_message_id({"type": "assistant", "message": {"id": ""}}) == (None, None)
        assert extract_generation_ids(
            [json.dumps(event), json.dumps(make_assistant("msg-2")),
             json.dumps(legacy_event)]) == {"gen-1", "gen-legacy"}

    def test_classify_stream_errors(self):
        from agent_eval.agent.stream_capture import classify_stream_errors
        ok = [json.dumps(make_assistant("gen-1")), json.dumps(make_result())]
        assert classify_stream_errors(ok) == (None, None)
        limit = [json.dumps({"type": "result", "is_error": True, "api_error_status": 402,
                             "result": 'API Error: 402 {"error":{"message":"Key limit exceeded","code":402}}'})]
        assert classify_stream_errors(limit) == ("config", {"exceeded": "run", "exceeded_reason": "limit_usd"})
        routing = [json.dumps({"type": "assistant", "message": {"id": "x", "content": [
            {"type": "text", "text": 'API Error: 404 {"error":{"message":"No endpoints found for z-ai/glm-5.3-flash.","type":"not_found"}}'}]}})]
        assert classify_stream_errors(routing) == ("config", None)
        overloaded = [json.dumps({"type": "result", "is_error": True, "api_error_status": 529, "result": "API Error: 529 overloaded"})]
        assert classify_stream_errors(overloaded) == ("infra", None)
        agent_side = [json.dumps({"type": "result", "is_error": True, "subtype": "error_max_turns", "result": "max turns"})]
        assert classify_stream_errors(agent_side) == (None, None)

