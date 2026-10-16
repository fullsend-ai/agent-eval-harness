"""Tests for llm_rubric field in scoring infrastructure."""

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import yaml

# Import config
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_eval.config import EvalConfig

# Import scoring functions
sys.path.insert(0, str(Path(__file__).parent.parent / "skills/eval-run/scripts"))


class TestLLMRubricJudgeLoading:
    """Test that llm_rubric field is correctly loaded and used in scoring."""

    @pytest.fixture
    def config_with_llm_rubric(self):
        """Config with llm_rubric judge."""
        config_data = {
            "name": "test-eval",
            "execution": {"mode": "case", "prompt": "{{ input.prompt }}"},
            "dataset": {"path": "eval/dataset", "schema": "test"},
            "outputs": [{"path": "output", "schema": "test"}],
            "models": {"judge": "claude-opus-4-6"},
            "judges": [
                {
                    "name": "llm-rubric-judge",
                    "llm_rubric": "Evaluate if the agent found relevant documentation."
                }
            ]
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump(config_data, f)
            config_path = f.name

        try:
            config = EvalConfig.from_yaml(config_path)
            yield config
        finally:
            Path(config_path).unlink()

    @pytest.fixture
    def config_with_both_fields(self):
        """Config with both llm_rubric and prompt (rubric should take precedence)."""
        config_data = {
            "name": "test-eval",
            "execution": {"mode": "case", "prompt": "{{ input.prompt }}"},
            "dataset": {"path": "eval/dataset", "schema": "test"},
            "outputs": [{"path": "output", "schema": "test"}],
            "models": {"judge": "claude-opus-4-6"},
            "judges": [
                {
                    "name": "precedence-judge",
                    "llm_rubric": "Use this rubric",
                    "prompt": "This should be ignored"
                }
            ]
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump(config_data, f)
            config_path = f.name

        try:
            config = EvalConfig.from_yaml(config_path)
            yield config
        finally:
            Path(config_path).unlink()

    def test_llm_rubric_judge_is_recognized(self, config_with_llm_rubric):
        """Test that judge with llm_rubric field is loaded as LLM judge."""
        import score

        # Mock the Anthropic client to avoid actual API calls
        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            with patch('score._get_anthropic_client'):
                judges = score.load_judges(config_with_llm_rubric)

        assert len(judges) == 1
        name, scorer, condition, judge_type, samples = judges[0]
        assert name == "llm-rubric-judge"
        assert callable(scorer)
        assert condition == ""

    def test_llm_rubric_prompt_is_used(self, config_with_llm_rubric):
        """Test that llm_rubric content is used as the prompt."""
        import score

        judge_config = config_with_llm_rubric.judges[0]

        # Mock the Anthropic client
        mock_client = Mock()
        mock_response = Mock()
        mock_response.content = [Mock()]
        mock_response.content[0].type = "text"
        mock_response.content[0].text = '{"score": 3, "rationale": "ok"}'
        mock_client.messages.create.return_value = mock_response

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            with patch('score._get_anthropic_client', return_value=mock_client):
                scorer = score._load_llm_judge(judge_config, config_with_llm_rubric)

                # Run scorer with sample outputs
                result = scorer(outputs={"files": {"output.txt": "test content"}})

        # Verify the API was called with the llm_rubric prompt
        mock_client.messages.create.assert_called_once()
        call_args = mock_client.messages.create.call_args
        messages = call_args[1]['messages']

        # The prompt should contain the llm_rubric text
        assert any("relevant documentation" in msg['content'] for msg in messages)

    def test_llm_rubric_takes_precedence_over_prompt(self, config_with_both_fields):
        """Test that llm_rubric is used instead of prompt when both are present."""
        import score

        judge_config = config_with_both_fields.judges[0]

        mock_client = Mock()
        mock_response = Mock()
        mock_response.content = [Mock()]
        mock_response.content[0].type = "text"
        mock_response.content[0].text = '{"score": 3, "rationale": "ok"}'
        mock_client.messages.create.return_value = mock_response

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            with patch('score._get_anthropic_client', return_value=mock_client):
                scorer = score._load_llm_judge(judge_config, config_with_both_fields)

                result = scorer(outputs={"files": {"output.txt": "test"}})

        # Verify the rubric prompt was used, not the regular prompt
        call_args = mock_client.messages.create.call_args
        messages = call_args[1]['messages']

        # Should contain "Use this rubric" not "This should be ignored"
        message_text = str(messages)
        assert "Use this rubric" in message_text
        assert "This should be ignored" not in message_text

    def test_llm_rubric_in_judge_type_detection(self):
        """Test that judge type detection includes llm_rubric."""
        import score
        from agent_eval.config import JudgeConfig

        # Judge with only llm_rubric
        jc = JudgeConfig(name="test", llm_rubric="Test rubric")

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            with patch('score._get_anthropic_client'):
                # Should be detected as LLM judge
                config_data = {
                    "name": "test",
                    "execution": {"mode": "case", "prompt": "{{ input.prompt }}"},
                    "dataset": {"path": ".", "schema": "test"},
                    "outputs": [{"path": "output", "schema": "test"}],
                    "models": {"judge": "claude-opus-4-6"},
                    "judges": [
                        {"name": "test", "llm_rubric": "Test rubric"}
                    ]
                }

                with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
                    yaml.dump(config_data, f)
                    config_path = f.name

                try:
                    config = EvalConfig.from_yaml(config_path)
                    judges = score.load_judges(config)
                    assert len(judges) == 1
                finally:
                    Path(config_path).unlink()

    def test_llm_rubric_auto_appends_conversation(self, config_with_llm_rubric):
        """Test that llm_rubric automatically gets {{ conversation }} appended."""
        import score

        judge_config = config_with_llm_rubric.judges[0]

        mock_client = Mock()
        mock_response = Mock()
        mock_response.content = [Mock()]
        mock_response.content[0].type = "text"
        mock_response.content[0].text = '{"score": 5, "rationale": "test"}'
        mock_client.messages.create.return_value = mock_response

        # Create outputs with events for conversation extraction
        outputs = {
            "events": [
                {
                    "type": "assistant",
                    "text": "I found the documentation at /ai-docs/domain/machineconfig.md"
                }
            ],
            "files": {}
        }

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            with patch('score._get_anthropic_client', return_value=mock_client):
                scorer = score._load_llm_judge(judge_config, config_with_llm_rubric)
                result = scorer(outputs=outputs)

        # Verify the API was called with both rubric and conversation
        call_args = mock_client.messages.create.call_args
        messages = call_args[1]['messages']
        message_content = messages[0]['content']

        # Should contain both the rubric and the agent's response
        assert "relevant documentation" in message_content
        assert "Agent Response to Evaluate" in message_content
        assert "I found the documentation" in message_content

    def test_llm_rubric_no_duplicate_conversation_template(self):
        """Test that {{ conversation }} is not appended if already present."""
        import score
        from agent_eval.config import JudgeConfig, EvalConfig, ExecutionConfig, ModelsConfig

        config = EvalConfig(
            name="test",
            execution=ExecutionConfig(mode="case", prompt="{{ input.prompt }}"),
            models=ModelsConfig(judge="claude-opus-4-6"),
        )

        jc = JudgeConfig(
            name="test",
            llm_rubric="Evaluate based on {{ conversation }}"
        )

        mock_client = Mock()
        mock_response = Mock()
        mock_response.content = [Mock()]
        mock_response.content[0].type = "text"
        mock_response.content[0].text = '{"score": 5, "rationale": "test"}'
        mock_client.messages.create.return_value = mock_response

        outputs = {
            "events": [{"type": "assistant", "text": "Test response"}],
            "files": {}
        }

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            with patch('score._get_anthropic_client', return_value=mock_client):
                scorer = score._load_llm_judge(jc, config)
                result = scorer(outputs=outputs)

        call_args = mock_client.messages.create.call_args
        message_content = call_args[1]['messages'][0]['content']

        # Should contain the conversation template result, but not duplicated
        assert message_content.count("Test response") == 1
        # Should not have "Agent Response to Evaluate" heading (not auto-appended)
        assert "Agent Response to Evaluate" not in message_content

    def test_openai_judge_model_not_forced_through_anthropic(self, config_with_llm_rubric):
        """Anthropic creds must not force a GPT-family judge through Anthropic; it
        grades via the OpenAI backend regardless of the runner."""
        import score

        config_with_llm_rubric.models.judge = "openai:/gpt-4o"
        judge_config = config_with_llm_rubric.judges[0]

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("score._call_structured_judge") as direct_call, \
                    patch("score._call_structured_judge_via_runner") as runner_call, \
                    patch("score._call_structured_judge_openai",
                          return_value=(3, "ok")) as openai_call:
                scorer = score._load_llm_judge(judge_config, config_with_llm_rubric)
                result = scorer(outputs={"files": {"output.txt": "test"}})

        assert result == (3, "ok")
        direct_call.assert_not_called()
        runner_call.assert_not_called()
        openai_call.assert_called_once()
        assert openai_call.call_args.args[1] == "gpt-4o"

    def test_runner_prefixed_judge_model_uses_runner_backend(self, config_with_llm_rubric):
        """A runner:/ model grades through the configured runner (opt-in), even
        when Anthropic creds are present."""
        import score

        config_with_llm_rubric.models.judge = "runner:/gpt-5.4-medium"
        judge_config = config_with_llm_rubric.judges[0]

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("score._call_structured_judge") as direct_call, \
                    patch("score._call_structured_judge_openai") as openai_call, \
                    patch("score._call_structured_judge_via_runner",
                          return_value=(3, "ok")) as runner_call:
                scorer = score._load_llm_judge(judge_config, config_with_llm_rubric)
                result = scorer(outputs={"files": {"output.txt": "test"}})

        assert result == (3, "ok")
        direct_call.assert_not_called()
        openai_call.assert_not_called()
        runner_call.assert_called_once()
        assert runner_call.call_args.args[1] == "gpt-5.4-medium"

    def test_versioned_claude_alias_uses_anthropic_sdk(
            self, config_with_llm_rubric, monkeypatch):
        """#5: a versioned alias like `sonnet-4-5` classifies as Anthropic and
        uses the direct SDK, not the runner."""
        import score

        config_with_llm_rubric.models.judge = "sonnet-4-5"
        judge_config = config_with_llm_rubric.judges[0]
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        with patch("score._call_structured_judge",
                   return_value=(3, "ok")) as direct_call, \
                patch("score._call_structured_judge_via_runner") as runner_call:
            scorer = score._load_llm_judge(judge_config, config_with_llm_rubric)
            result = scorer(outputs={"files": {"output.txt": "test"}})

        assert result == (3, "ok")
        runner_call.assert_not_called()
        direct_call.assert_called_once()
        assert direct_call.call_args.args[1] == "sonnet-4-5"

    def test_runner_judge_uses_stdout_only_json_contract(self,
                                                         config_with_llm_rubric,
                                                         tmp_path):
        """Runner-backed judges must not be told to write an inaccessible file."""
        import score

        judge_config = config_with_llm_rubric.judges[0]
        judge_config.feedback_type = "int"
        judge_config.score_range = [1, 5]
        workspace = tmp_path / "runner-workspace"
        workspace.mkdir()
        captured = {}

        def fake_runner(config, prompt, model, **kwargs):
            captured["prompt"] = prompt
            captured["kwargs"] = kwargs
            stdout = '{"score": 4, "rationale": "The response is accurate."}'
            return (SimpleNamespace(exit_code=0, stdout=stdout, stderr=""),
                    stdout, workspace)

        with patch("score.run_prompt_via_runner", side_effect=fake_runner):
            result = score._call_structured_judge_via_runner(
                "Evaluate the response.",
                "gpt-5.4-medium",
                "int",
                config_with_llm_rubric,
                judge_config,
                bounds=(1, 5, True),
            )

        assert result == (4, "The response is accurate.")
        assert "Return exactly one JSON object and nothing else." in captured["prompt"]
        assert "output/score.json" not in captured["prompt"]
        assert "write your verdict" not in captured["prompt"].lower()
        # Read-only means denying the escape hatches too; the allow list by
        # itself does not stop a headless Claude Code judge calling Bash.
        assert captured["kwargs"]["permissions"] == {
            "allow": ["Read", "Grep", "Glob"],
            "deny": ["Bash", "WebFetch", "WebSearch", "Agent",
                     "Edit", "MultiEdit", "NotebookEdit"],
        }


class TestLLMRubricErrorHandling:
    """Test error handling for llm_rubric judges."""

    def test_empty_llm_rubric_falls_back_to_prompt(self):
        """Test that empty llm_rubric falls back to prompt field."""
        import score
        from agent_eval.config import JudgeConfig, EvalConfig, ExecutionConfig, ModelsConfig

        config = EvalConfig(
            name="test",
            execution=ExecutionConfig(mode="case", prompt="{{ input.prompt }}"),
            models=ModelsConfig(judge="claude-opus-4-6"),
        )

        jc = JudgeConfig(
            name="test",
            llm_rubric="",  # Empty
            prompt="Use this prompt instead"
        )

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            with patch('score._get_anthropic_client'):
                scorer = score._load_llm_judge(jc, config)
                assert scorer is not None

    def test_missing_both_llm_rubric_and_prompt_raises_error(self):
        """Test that missing both llm_rubric and prompt raises error."""
        import score
        from agent_eval.config import JudgeConfig, EvalConfig, ExecutionConfig, ModelsConfig

        config = EvalConfig(
            name="test",
            execution=ExecutionConfig(mode="case", prompt="{{ input.prompt }}"),
            models=ModelsConfig(judge="claude-opus-4-6"),
        )

        jc = JudgeConfig(
            name="test",
            llm_rubric="",
            prompt="",
            prompt_file=""
        )

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            with pytest.raises(ValueError, match="requires prompt, llm_rubric, or prompt_file"):
                score._load_llm_judge(jc, config)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def test_openrouter_judge_model_binds_its_client_config():
    """An openrouter:/ judge takes the OpenAI arm with its client config
    (spec 014): token parameter pinned to max_tokens, extra_body from routing,
    never the Anthropic or runner path."""
    from unittest.mock import patch

    import score
    from agent_eval.config import EvalConfig, JudgeConfig, ModelsConfig

    config = EvalConfig(name="t", skill="s")
    config.models = ModelsConfig(judge="openrouter:/z-ai/glm-5.2")
    jc = JudgeConfig(name="j", llm_rubric="Response is helpful",
                     provider_options={"fallbacks": ["deepseek/deepseek-v4"]})
    config.judges = [jc]

    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
        with patch("score._call_structured_judge") as direct_call, \
                patch("score._call_structured_judge_via_runner") as runner_call, \
                patch("score._call_structured_judge_openai",
                      return_value=(3, "ok")) as openai_call:
            scorer = score._load_llm_judge(jc, config)
            result = scorer(outputs={"files": {"output.txt": "test"}})

    assert result == (3, "ok")
    direct_call.assert_not_called()
    runner_call.assert_not_called()
    assert openai_call.call_args.args[1] == "z-ai/glm-5.2"
    kwargs = openai_call.call_args.kwargs
    assert kwargs["client_cfg"].name == "openrouter"
    assert kwargs["token_param"] == "max_tokens"
    assert kwargs["extra_body"] == {"models": ["z-ai/glm-5.2", "deepseek/deepseek-v4"]}


def test_anthropic_judge_returns_usage_side_channel():
    """The Anthropic path hands back a JudgeOutcome: the verdict tuple every
    caller already unpacks, plus the token usage (no cost — Anthropic prices
    nothing inline) for summary.judge_usage."""
    import score
    from agent_eval.config import EvalConfig, JudgeConfig, ModelsConfig

    block = SimpleNamespace(type="tool_use", name="submit_score",
                            input={"rationale": "fine", "score": 4})
    response = SimpleNamespace(
        id="msg_01", model="claude-opus-4-8", content=[block],
        usage=SimpleNamespace(input_tokens=120, output_tokens=15,
                              cache_creation_input_tokens=0,
                              cache_read_input_tokens=80))
    client = Mock()
    client.messages.create.return_value = response
    config = EvalConfig(name="t", skill="s")
    config.models = ModelsConfig(judge="claude-opus-4-8", judge_verdict="tool")
    jc = JudgeConfig(name="j", llm_rubric="Response is helpful")
    config.judges = [jc]

    with patch("score._get_anthropic_client", return_value=client):
        scorer = score._load_llm_judge(jc, config)
        outcome = scorer(outputs={"files": {"output.txt": "test"}})

    value, rationale = outcome
    assert (value, rationale) == (4, "fine")
    assert outcome.usage == {"model": "claude-opus-4-8", "provider": "anthropic",
                             "id": "msg_01", "prompt_tokens": 200,
                             "completion_tokens": 15, "reasoning_tokens": None,
                             "cost_usd": None, "cost_source": "none"}
    assert score._normalize_result(outcome) == (4, "fine", outcome.usage)
