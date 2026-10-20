"""Tests for prompt-only runner backends (judges, synthetic generation)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.agent.base import RunResult
from agent_eval.config import EvalConfig
from agent_eval.prompt_backends import (
    RUNNERS,
    extract_runner_text,
    is_anthropic_model,
    resolve_judge_backend,
    run_prompt_via_runner,
    split_model_uri,
)
import pytest


def _write_config(tmp_path, body: str) -> Path:
    path = tmp_path / "eval.yaml"
    path.write_text(body)
    return path


class _FakeRunner:
    captured = None

    @classmethod
    def from_config(cls, config, **kwargs):
        cls.captured = {
            "permission_mode": config.runner.permission_mode,
            "settings": dict(config.runner.settings or {}),
            "system_prompt": config.runner.system_prompt,
            "workspace_mode": config.runner.workspace_mode,
        }
        return cls()

    def execute(self, **kwargs):
        return RunResult(exit_code=0, stdout='{"score": 5}', stderr="", duration_s=0)


def test_run_prompt_via_runner_clears_plan_mode(tmp_path, monkeypatch):
    monkeypatch.setitem(RUNNERS, "cursor", _FakeRunner)
    _FakeRunner.captured = None

    cfg = EvalConfig.from_yaml(_write_config(tmp_path, """
name: t
execution:
  prompt: "{{ input.prompt }}"
runner:
  type: cursor
  permission_mode: plan
  system_prompt: Read AGENTS.md first
  settings:
    mode: plan
    endpoint: https://cursor.example
    add_dirs: [/outside]
"""))

    # Exercise the helper's defensive copy without making the persisted eval
    # config itself invalid for Cursor.
    cfg.runner.workspace_mode = "repo"

    run_prompt_via_runner(cfg, "score this", "gpt-5.4-medium", workspace=tmp_path)

    captured = _FakeRunner.captured
    assert captured["permission_mode"] is None
    assert captured["workspace_mode"] is None
    assert captured["system_prompt"] == ""
    assert captured["settings"]["mode"] == "plan"
    # add_dirs is stripped for prompt-only judge/generation calls: they grade
    # untrusted output in an isolated workspace and must not inherit host-dir
    # grants from the skill runner's settings.
    assert "add_dirs" not in captured["settings"]
    assert captured["settings"]["endpoint"] == "https://cursor.example"


def test_run_prompt_via_runner_keeps_non_plan_settings_mode(tmp_path, monkeypatch):
    monkeypatch.setitem(RUNNERS, "cursor", _FakeRunner)
    _FakeRunner.captured = None

    cfg = EvalConfig.from_yaml(_write_config(tmp_path, """
name: t
execution:
  prompt: "{{ input.prompt }}"
runner:
  type: cursor
  permission_mode: plan
  settings:
    mode: ask
"""))

    run_prompt_via_runner(cfg, "hello", "gpt-5.4-medium", workspace=tmp_path)

    captured = _FakeRunner.captured
    assert captured["permission_mode"] is None
    assert captured["settings"]["mode"] == "ask"


@pytest.mark.parametrize("runner_type", ["cursor", "codex"])
def test_deny_by_default_mode_does_not_reach_runners_that_reject_it(
        tmp_path, monkeypatch, runner_type):
    """Cursor and Codex raise ValueError on `permission_mode='manual'`. Go
    through their real `from_config` so the constructor validation runs
    (a mocked `run_prompt_via_runner` would hide it)."""
    import agent_eval.agent.cursor_agent as cursor_agent
    from agent_eval.agent.codex import CodexRunner
    from agent_eval.agent.cursor_agent import CursorAgentRunner

    monkeypatch.setattr(cursor_agent, "_discover_cursor_agent",
                        lambda configured=None: "cursor-agent")
    seen = {}
    runner_cls = CursorAgentRunner if runner_type == "cursor" else CodexRunner

    def fake_execute(self, **kwargs):
        seen["permission_mode"] = self._permission_mode
        return RunResult(exit_code=0, stdout='{"score": 5}', stderr="",
                         duration_s=0)

    monkeypatch.setattr(runner_cls, "execute", fake_execute)

    cfg = EvalConfig.from_yaml(_write_config(tmp_path, f"""
name: t
execution:
  prompt: "{{{{ input.prompt }}}}"
runner:
  type: {runner_type}
"""))

    result, _, _ = run_prompt_via_runner(
        cfg, "score this", "gpt-5.4-medium", workspace=tmp_path,
        permission_mode="manual", permission_prompts="none")

    assert result.exit_code == 0
    assert seen["permission_mode"] is None


def test_deny_by_default_mode_is_applied_on_claude_code(tmp_path, monkeypatch):
    class _Capture(_FakeRunner):
        pass

    monkeypatch.setitem(RUNNERS, "claude-code", _Capture)
    _Capture.captured = None
    cfg = EvalConfig.from_yaml(_write_config(tmp_path, """
name: t
execution:
  prompt: "{{ input.prompt }}"
"""))
    run_prompt_via_runner(cfg, "score this", "claude-sonnet-4-6",
                          workspace=tmp_path, permission_mode="manual")
    assert _Capture.captured["permission_mode"] == "manual"


def test_extract_runner_text_prefers_cursor_terminal_result():
    stdout = "\n".join([
        '{"type":"assistant","session_id":"s","message":{"content":[{"type":"text","text":"{\\"sc"}]}}',
        '{"type":"assistant","session_id":"s","message":{"content":[{"type":"text","text":"ore\\": 2}"}]}}',
        '{"type":"result","session_id":"s","result":"{\\"score\\": 2}"}',
    ])
    result = RunResult(exit_code=0, stdout=stdout, stderr="", duration_s=0)
    assert extract_runner_text(result) == '{"score": 2}'


# ---------------------------------------------------------------------------
# Provider routing (judge backend decoupled from the runner)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model,expected", [
    ("openai:/gpt-4o", ("openai", "gpt-4o")),
    ("openai://gpt-4o", ("openai", "gpt-4o")),
    ("anthropic:/claude-sonnet-4-5", ("anthropic", "claude-sonnet-4-5")),
    ("runner:/gpt-5.4-medium", ("runner", "gpt-5.4-medium")),
    ("sonnet", (None, "sonnet")),
    ("gpt-4o", (None, "gpt-4o")),
    ("  Sonnet  ", (None, "Sonnet")),
])
def test_split_model_uri(model, expected):
    assert split_model_uri(model) == expected


@pytest.mark.parametrize("model,expected", [
    ("sonnet", True),
    ("sonnet-4-5", True),          # versioned alias (regression: was misclassified)
    ("opus[1m]", True),            # bracketed alias
    ("haiku-4-5", True),
    ("claude-3-5-sonnet", True),
    ("anthropic:/some-model", True),
    ("anthropic/claude", True),    # LiteLLM single-slash form
    ("openai:/gpt-4o", False),
    ("gpt-4o", False),
    ("gemini-2.5-flash", False),
    ("", False),
    (None, False),
])
def test_is_anthropic_model(model, expected):
    assert is_anthropic_model(model) is expected


@pytest.mark.parametrize("model,expected", [
    # Anthropic: bare alias, full id, explicit URI (prefix stripped).
    ("sonnet", ("anthropic", "sonnet")),
    ("claude-sonnet-4-5", ("anthropic", "claude-sonnet-4-5")),
    ("sonnet-4-5", ("anthropic", "sonnet-4-5")),
    ("anthropic:/claude-x", ("anthropic", "claude-x")),
    # OpenAI: bare family id, o-series, explicit URI, gateway-served id.
    ("gpt-4o", ("openai", "gpt-4o")),
    ("o3-mini", ("openai", "o3-mini")),
    ("openai:/gpt-4o", ("openai", "gpt-4o")),
    ("openai:/gemini-2.5-flash", ("openai", "gemini-2.5-flash")),
    # Runner: explicit opt-in for runner-managed ids.
    ("runner:/gpt-5.4-medium", ("runner", "gpt-5.4-medium")),
    # A bare custom/gateway id defaults to OpenAI (served via OPENAI_BASE_URL).
    ("mistral-large", ("openai", "mistral-large")),
    ("gpt-5.4-medium", ("openai", "gpt-5.4-medium")),
])
def test_resolve_judge_backend(model, expected):
    assert resolve_judge_backend(model) == expected


@pytest.mark.parametrize("model", [
    "gemini:/gemini-2.5-flash",   # unsupported explicit provider
    "mistral:/mistral-large",     # unsupported explicit provider
    "runner:/",                   # runner opt-in with no model name
])
def test_resolve_judge_backend_rejects(model):
    with pytest.raises(ValueError):
        resolve_judge_backend(model)


# --- openrouter:/ judge routing (spec 014) -----------------------------------

@pytest.mark.parametrize("model, expected", [
    ("openrouter:/z-ai/glm-5.2", ("openai", "z-ai/glm-5.2")),
    ("openrouter:/openai/gpt-5.2:exacto", ("openai", "openai/gpt-5.2:exacto")),
    ("openrouter:/anthropic/claude-opus-4-8", ("openai", "anthropic/claude-opus-4-8")),
    ("OpenRouter://z-ai/glm-5.2", ("openai", "z-ai/glm-5.2")),
])
def test_resolve_judge_backend_openrouter(model, expected):
    assert resolve_judge_backend(model) == expected


@pytest.mark.parametrize("model", ["openrouter:/", "openrouter://", "openrouter:/glm-5.2"])
def test_resolve_judge_backend_openrouter_needs_author_and_slug(model):
    with pytest.raises(ValueError, match="<author>/<slug>"):
        resolve_judge_backend(model)


def test_unknown_prefix_is_still_unknown_and_the_hint_names_openrouter():
    with pytest.raises(ValueError, match=r"Unsupported judge model provider 'other'.*openrouter:/"):
        resolve_judge_backend("other:/x")


def test_is_anthropic_model_openrouter_claude_is_false():
    assert is_anthropic_model("openrouter:/anthropic/claude-opus-4-8") is False


@pytest.mark.parametrize("model", [
    "sonnet", "gpt-4o", "anthropic:/claude-sonnet-4-5", "openai:/gpt-4o",
    "runner:/gpt-5.4-medium", "my-gateway-model", "vendor/model", "", None,
])
def test_resolve_judge_client_is_none_for_every_existing_path(model):
    from agent_eval.prompt_backends import resolve_judge_client
    assert resolve_judge_client(model, None) is None


def test_resolve_judge_client_defaults_without_a_providers_block():
    from agent_eval.prompt_backends import JudgeClientConfig, resolve_judge_client

    cfg = resolve_judge_client("openrouter:/z-ai/glm-5.2:exacto", None)
    assert isinstance(cfg, JudgeClientConfig)
    assert cfg.name == "openrouter"
    assert cfg.base_url == "https://openrouter.ai/api"
    assert cfg.api_key_env == "OPENROUTER_API_KEY"
    assert cfg.token_param == "max_tokens"
    assert cfg.default_headers == {"X-OpenRouter-Title": "agent-eval-harness"}
    assert (cfg.max_retries, cfg.timeout_s, cfg.concurrency, cfg.inherit_pins) == (3, 300.0, 4, False)
    assert cfg.judge_extra_body("z-ai/glm-5.2:exacto") == {}


def test_resolve_judge_client_reads_models_providers(tmp_path):
    from agent_eval.prompt_backends import resolve_judge_client

    path = _write_config(tmp_path, """
name: t
execution:
  skill: s
models:
  judge: openrouter:/z-ai/glm-5.2
  providers:
    openrouter:
      attribution: {referer: https://example.test/eval, title: my-eval}
      routing:
        defaults: {sort: throughput}
        models:
          z-ai/glm-5.2: {order: [Z.AI, novita], allow_fallbacks: false}
      judge:
        concurrency: 2
        max_retries: 1
        timeout_s: 60
        inherit_pins: true
        extra_body: {reasoning: {effort: high}}
judges:
  - {name: j, prompt: rate it}
""")
    config = EvalConfig.from_yaml(path)
    cfg = resolve_judge_client(config.models.judge, config.models.providers)
    assert cfg.default_headers == {"HTTP-Referer": "https://example.test/eval",
                                   "X-OpenRouter-Title": "my-eval"}
    assert (cfg.max_retries, cfg.timeout_s, cfg.concurrency, cfg.inherit_pins) == (1, 60.0, 2, True)
    body = cfg.judge_extra_body("z-ai/glm-5.2")
    assert body["provider"] == {"order": ["z-ai", "novita"], "allow_fallbacks": False,
                                "sort": "throughput", "require_parameters": True}
    assert body["reasoning"] == {"effort": "high"}
    # The memoisation key names the variable, never a key value.
    assert "OPENROUTER_API_KEY" in cfg.client_key()
