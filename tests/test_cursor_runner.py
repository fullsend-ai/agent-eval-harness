"""Tests for the Cursor Agent runner's common EvalRunner contract."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.agent import RUNNERS
from agent_eval.agent.cursor_agent import (
    CursorAgentRunner,
    _model_with_effort,
    _parse_cursor_stream,
)
from agent_eval.config import EvalConfig


def _write_config(tmp_path, body: str) -> Path:
    path = tmp_path / "eval.yaml"
    path.write_text(body)
    return path


def _runner(monkeypatch, **kwargs):
    monkeypatch.setattr(
        "agent_eval.agent.cursor_agent._discover_cursor_agent",
        lambda configured=None: configured or "/usr/bin/cursor-agent",
    )
    return CursorAgentRunner(**kwargs)


def test_cursor_is_registered():
    assert RUNNERS["cursor"] is CursorAgentRunner


def test_cursor_discovers_binary_and_uses_default_command(monkeypatch, tmp_path):
    runner = _runner(monkeypatch)
    command = runner._build_command(tmp_path, "gpt-5.4-medium")

    assert command[:5] == [
        "/usr/bin/cursor-agent", "--print", "--output-format", "stream-json",
        "--workspace",
    ]
    assert str(tmp_path) in command
    assert ["--model", "gpt-5.4-medium"] == command[command.index("--model"):command.index("--model") + 2]
    assert "--force" in command


def test_cursor_from_config_reads_common_fields_and_warns_for_native_settings(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        "agent_eval.agent.cursor_agent._discover_cursor_agent",
        lambda configured=None: configured or "/usr/bin/cursor-agent",
    )
    cfg = EvalConfig.from_yaml(_write_config(tmp_path, """
name: t
execution:
  prompt: "{{ input.prompt }}"
  env:
    CASE_FLAG: from-execution
runner:
  type: cursor
  effort: high
  permission_mode: plan
  env:
    RUNNER_FLAG: from-runner
  settings:
    binary: /custom/cursor-agent
    endpoint: https://cursor.example
"""))

    with pytest.warns(RuntimeWarning, match="unsupported runner.settings keys: endpoint"):
        runner = CursorAgentRunner.from_config(cfg, log_prefix="test")

    assert runner.name == "cursor"
    assert runner._binary == "/custom/cursor-agent"
    assert runner._effort == "high"
    assert runner._permission_mode == "plan"
    assert runner._env == {
        "CASE_FLAG": "from-execution",
        "RUNNER_FLAG": "from-runner",
    }


def test_cursor_supports_repo_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_eval.agent.cursor_agent._discover_cursor_agent",
        lambda configured=None: configured or "/usr/bin/cursor-agent",
    )
    cfg = EvalConfig.from_yaml(_write_config(tmp_path, """
name: t
execution:
  prompt: "{{ input.prompt }}"
runner:
  type: cursor
  workspace_mode: repo
"""))

    runner = CursorAgentRunner.from_config(cfg)

    assert runner._workspace_mode == "repo"


def test_cursor_rejects_unsupported_tool_interception(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        "agent_eval.agent.cursor_agent._discover_cursor_agent",
        lambda configured=None: configured or "/usr/bin/cursor-agent",
    )
    cfg = EvalConfig.from_yaml(_write_config(tmp_path, """
name: t
execution:
  prompt: "{{ input.prompt }}"
runner:
  type: cursor
"""))
    cfg.inputs.tools.append(object())

    with pytest.raises(ValueError, match="inputs.tools"):
        CursorAgentRunner.from_config(cfg)


def test_cursor_effort_uses_parameterized_model_syntax(tmp_path, monkeypatch):
    runner = _runner(monkeypatch, effort="high")
    command = runner._build_command(tmp_path, "gpt-5.4")

    assert command[command.index("--model") + 1] == "gpt-5.4[effort=high]"


def test_cursor_effort_preserves_legacy_effort_variant_id(tmp_path, monkeypatch):
    runner = _runner(monkeypatch, effort="medium")
    command = runner._build_command(tmp_path, "gpt-5.4-medium")

    assert command[command.index("--model") + 1] == "gpt-5.4-medium"


def test_cursor_effort_preserves_other_model_parameters():
    assert _model_with_effort(
        "claude-opus-4-8[context=1m]", "xhigh"
    ) == "claude-opus-4-8[context=1m,effort=xhigh]"
    assert _model_with_effort(
        "claude-opus-4-8[context=1m,effort=low]", "high"
    ) == "claude-opus-4-8[context=1m,effort=high]"


def test_cursor_permission_mode_maps_supported_common_modes(tmp_path, monkeypatch):
    runner = _runner(monkeypatch, permission_mode="plan")
    command = runner._build_command(tmp_path, "gpt-5.4-medium")
    assert command[command.index("--mode") + 1] == "plan"

    runner = _runner(monkeypatch, permission_mode="bypassPermissions")
    assert "--force" in runner._build_command(tmp_path, "gpt-5.4-medium")


def test_cursor_warns_for_permission_mode_without_exact_equivalent(monkeypatch):
    with pytest.warns(RuntimeWarning, match="no exact permission_mode='dontAsk'"):
        _runner(monkeypatch, permission_mode="dontAsk")


def test_cursor_builds_skill_prompt_instead_of_slash_command(tmp_path, monkeypatch):
    skill = tmp_path / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("First inspect the input, then answer.")

    prompt = _runner(monkeypatch)._build_prompt(
        "demo", "--input case.yaml", None, tmp_path, [])

    assert not prompt.startswith("/")
    assert "Use the demo skill" in prompt
    assert "First inspect the input, then answer." in prompt
    assert "with arguments: --input case.yaml" in prompt


def test_cursor_missing_skill_fails_closed(tmp_path, monkeypatch):
    with pytest.raises(FileNotFoundError, match="instructions not found"):
        _runner(monkeypatch)._build_prompt("missing", "", None, tmp_path, [])


def test_cursor_prompt_mode_keeps_raw_prompt_and_system_prompt(tmp_path, monkeypatch):
    prompt = _runner(monkeypatch, system_prompt="Use docs first")._build_prompt(
        None, "Reply with hello", None, tmp_path, [])
    assert prompt == (
        "--- HARNESS RUNTIME INSTRUCTIONS ---\n"
        "Use docs first\n"
        "--- END HARNESS RUNTIME INSTRUCTIONS ---\n\n"
        "Reply with hello"
    )


def test_cursor_plugin_skill_can_be_staged_and_embedded(tmp_path, monkeypatch):
    plugin = tmp_path / "plugin"
    skill = plugin / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("plugin instructions")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    runner = _runner(monkeypatch, plugin_dirs=[str(plugin)])
    staged = runner._staged_plugin_dirs(workspace)

    assert len(staged) == 1
    assert Path(staged[0]).is_dir()
    assert runner._find_skill_text("demo", workspace, staged) == "plugin instructions"


def test_cursor_plugin_staging_rejects_colliding_external_basenames(
    tmp_path, monkeypatch,
):
    first = tmp_path / "one" / "shared-plugin"
    second = tmp_path / "two" / "shared-plugin"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    runner = _runner(monkeypatch, plugin_dirs=[str(first), str(second)])
    with pytest.raises(ValueError, match="same directory name"):
        runner._staged_plugin_dirs(workspace)


def test_cursor_repo_mode_skips_plugin_staging(tmp_path, monkeypatch):
    plugin = tmp_path / "plugins" / "demo-plugin"
    skill = plugin / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("plugin instructions")
    workspace = tmp_path / "repo"
    workspace.mkdir()

    runner = _runner(
        monkeypatch,
        plugin_dirs=[str(plugin)],
        workspace_mode="repo",
    )
    staged = runner._staged_plugin_dirs(workspace)

    assert staged == [str(plugin.resolve())]
    assert not (workspace / ".staged-plugins").exists()


def test_cursor_namespaced_plugin_skill_lookup(tmp_path, monkeypatch):
    plugin = tmp_path / "plugin-dir"
    skill = plugin / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("namespaced instructions")
    manifest = plugin / ".claude-plugin"
    manifest.mkdir()
    (manifest / "plugin.json").write_text(json.dumps({"name": "manifest-name"}))

    runner = _runner(monkeypatch)
    assert runner._find_skill_text(
        "manifest-name:demo", tmp_path, [str(plugin)]
    ) == "namespaced instructions"


def test_cursor_find_skill_text_rejects_symlinked_skill_file(tmp_path, monkeypatch):
    """A workspace SKILL.md symlink pointing outside its root is not read into
    the prompt (CWE-59)."""
    secret = tmp_path / "secret.txt"
    secret.write_text("HOST SECRET")
    workspace = tmp_path / "ws"
    skill = workspace / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").symlink_to(secret)

    runner = _runner(monkeypatch)
    assert runner._find_skill_text("demo", workspace, []) == ""


def test_cursor_find_skill_text_reads_regular_skill_file(tmp_path, monkeypatch):
    """A regular (non-symlink) workspace SKILL.md is still read."""
    workspace = tmp_path / "ws"
    skill = workspace / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("real instructions")

    runner = _runner(monkeypatch)
    assert runner._find_skill_text("demo", workspace, []) == "real instructions"


def test_cursor_environment_matches_cli_runner_baseline(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "do-not-forward")
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-key")
    monkeypatch.setenv("LANG", "C.UTF-8")
    runner = _runner(
        monkeypatch,
        env={"STATIC_TOKEN": "keep", "EXPANDED": "$LANG"},
    )

    env = runner._build_env({"HOOK_FLAG": "set-by-hook"})

    assert env["CURSOR_API_KEY"] == "cursor-key"
    assert env["STATIC_TOKEN"] == "keep"
    assert env["EXPANDED"] == "C.UTF-8"
    assert env["HOOK_FLAG"] == "set-by-hook"
    assert "GITHUB_TOKEN" not in env


def test_cursor_execute_success_collects_output_and_metrics(tmp_path, monkeypatch):
    captured = {}

    class FakeProcess:
        returncode = 0

        def communicate(self, input=None, timeout=None):
            captured["input"] = input
            captured["timeout"] = timeout
            return "\n".join([
                '{"type":"system","subtype":"init","model":"GPT-5.4 Medium"}',
                '{"type":"assistant","message":{"role":"assistant",'
                '"model":"gpt-5.4-medium","content":[{"type":"text",'
                '"text":"hello"}]}}',
                '{"type":"result","subtype":"success","is_error":false,'
                '"usage":{"inputTokens":10,"outputTokens":4,'
                '"cacheReadTokens":2,"cacheWriteTokens":1}}',
            ]), ""

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr("agent_eval.agent.cursor_agent.subprocess.Popen", fake_popen)
    runner = _runner(monkeypatch, system_prompt="Use docs first")
    result = runner.execute(
        target=None,
        args="Reply with hello",
        workspace=tmp_path,
        model="gpt-5.4-medium",
        timeout_s=7,
    )

    assert result.exit_code == 0
    assert result.num_turns == 1
    assert result.resolved_model == "GPT-5.4 Medium"
    assert result.token_usage == {
        "input": 10, "output": 4, "cache_read": 2, "cache_create": 1,
    }
    # Per-model usage and turns key by the same (assistant) model identity, so
    # one run's tokens are not split from its turn count during aggregation.
    assert result.per_model_usage["gpt-5.4-medium"]["input"] == 10
    assert result.per_model_turns == {"gpt-5.4-medium": 1}
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert captured["kwargs"]["stdin"] is subprocess.PIPE
    assert captured["kwargs"]["stdout"] is subprocess.PIPE
    assert captured["kwargs"]["stderr"] is subprocess.PIPE
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["timeout"] == 7
    assert captured["input"].endswith("Reply with hello")
    assert "--api-key" not in captured["command"]


def test_cursor_execute_timeout_returns_partial_output(tmp_path, monkeypatch):
    class FakeProcess:
        pid = 12345
        returncode = -9

        def __init__(self):
            self.calls = 0

        def communicate(self, input=None, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(
                    "cursor-agent", timeout, output="partial")
            return "partial", "killed"

        def kill(self):
            return None

    monkeypatch.setattr(
        "agent_eval.agent.cursor_agent.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    monkeypatch.setattr(
        "agent_eval.agent.cursor_agent.os.killpg",
        lambda *args, **kwargs: None,
    )

    result = _runner(monkeypatch).execute(
        None, "p", tmp_path, "gpt-5.4-medium", timeout_s=1)

    assert result.exit_code == -1
    assert "Timed out after 1s" in result.stderr
    assert result.stdout == "partial"


def test_cursor_result_error_forces_failure(tmp_path, monkeypatch):
    class FakeProcess:
        returncode = 0

        def communicate(self, input=None, timeout=None):
            return '{"type":"result","is_error":true,"result":"API Error"}', ""

    monkeypatch.setattr(
        "agent_eval.agent.cursor_agent.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    result = _runner(monkeypatch).execute(None, "p", tmp_path, "gpt-5.4-medium")
    assert result.exit_code == 1


def test_cursor_permission_rules_are_scoped_and_restored(tmp_path, monkeypatch):
    captured = {}

    class FakeProcess:
        returncode = 0

        def communicate(self, input=None, timeout=None):
            captured["config"] = json.loads(
                (tmp_path / ".cursor" / "cli.json").read_text())
            return '{"type":"result","is_error":false}', ""

    monkeypatch.setattr(
        "agent_eval.agent.cursor_agent.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    result = _runner(
        monkeypatch,
        workspace_mode="repo",
        permissions={"allow": ["Read", "Grep", "Glob"]},
    ).execute(None, "read", tmp_path, "gpt-5.4-medium")

    assert result.exit_code == 0
    assert captured["config"]["permissions"]["allow"] == ["Read(**)"]
    assert set(captured["config"]["permissions"]["deny"]) == {
        "Write(**)", "Shell(**)", "WebFetch(*)", "Mcp(*:*)",
    }
    assert not (tmp_path / ".cursor" / "cli.json").exists()


def test_cursor_empty_allowlist_is_unrestricted(tmp_path, monkeypatch):
    runner = _runner(monkeypatch, permissions={"allow": []})

    assert runner._prepare_permissions(tmp_path) is None
    assert not (tmp_path / ".cursor").exists()


def test_cursor_deny_only_permissions_keep_default_allowance(
    tmp_path, monkeypatch,
):
    runner = _runner(monkeypatch, permissions={
        "deny": [{"path": "eval/", "tools": ["Read", "Edit"]}],
    })
    snapshot = runner._prepare_permissions(tmp_path)
    try:
        config = json.loads(snapshot.path.read_text())
        assert config["permissions"]["allow"] == [
            "Read(**)", "Write(**)", "Shell(**)", "WebFetch(*)", "Mcp(*:*)",
        ]
        assert config["permissions"]["deny"] == [
            "Read(eval/**)", "Write(eval/**)",
        ]
    finally:
        CursorAgentRunner._restore_permissions(snapshot)


def test_cursor_glob_allow_does_not_grant_read(tmp_path, monkeypatch):
    runner = _runner(monkeypatch, permissions={"allow": ["Glob"]})
    snapshot = runner._prepare_permissions(tmp_path)
    try:
        config = json.loads(snapshot.path.read_text())
        assert config["permissions"]["allow"] == []
        assert "Read(**)" in config["permissions"]["deny"]
    finally:
        CursorAgentRunner._restore_permissions(snapshot)


def test_cursor_glob_does_not_grant_read_and_websearch_is_scoped(
    tmp_path, monkeypatch,
):
    runner = _runner(monkeypatch, permissions={"allow": ["Glob", "WebSearch"]})
    snapshot = runner._prepare_permissions(tmp_path)
    try:
        config = json.loads(snapshot.path.read_text())
        assert config["permissions"]["allow"] == ["WebFetch(*)"]
        assert "Read(**)" in config["permissions"]["deny"]
        assert "WebFetch(*)" not in config["permissions"]["deny"]
    finally:
        CursorAgentRunner._restore_permissions(snapshot)


def test_cursor_existing_permission_config_is_restored(tmp_path, monkeypatch):
    cursor_dir = tmp_path / ".cursor"
    cursor_dir.mkdir()
    config_path = cursor_dir / "cli.json"
    original = b'{"permissions": {"allow": ["Read(**)"]}}\n'
    config_path.write_bytes(original)

    snapshot = _runner(
        monkeypatch, permissions={"allow": ["Read"]}
    )._prepare_permissions(tmp_path)
    CursorAgentRunner._restore_permissions(snapshot)

    assert config_path.read_bytes() == original


def test_cursor_permission_translation_warns_for_unknown_tool(tmp_path, monkeypatch):
    runner = _runner(monkeypatch, permissions={"allow": ["Skill"]})
    with pytest.raises(ValueError, match="no permission mapping"):
        runner._prepare_permissions(tmp_path)


def test_cursor_denied_agent_tool_is_a_quiet_no_op(tmp_path, monkeypatch):
    """The harness's default judge deny list includes Agent and the Claude-only
    editor spellings, none of which Cursor has a permission for.  Denying them
    must neither warn nor leak a pattern."""
    import warnings
    runner = _runner(
        monkeypatch,
        permissions={"allow": ["Read"],
                     "deny": ["Bash", "WebFetch", "WebSearch", "Agent",
                              "Edit", "MultiEdit", "NotebookEdit"]},
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        snapshot = runner._prepare_permissions(tmp_path)
    try:
        config = json.loads(snapshot.path.read_text())
        assert "Shell(**)" in config["permissions"]["deny"]
        assert "WebFetch(*)" in config["permissions"]["deny"]
        assert not any("Agent" in p for p in config["permissions"]["deny"])
    finally:
        CursorAgentRunner._restore_permissions(snapshot)


@pytest.mark.parametrize("allowed", [
    ["Read", "Grep", "Glob", "Write"],
    ["Read", "Grep", "Glob", "Write(./output/**)"],
])
def test_cursor_default_editor_denies_do_not_revoke_an_allowed_write(
        tmp_path, monkeypatch, allowed):
    """Cursor has one `Write` capability for every file mutation, so an `Edit`
    deny is a `Write(**)` deny.  Left in the default judge deny list it would
    beat the allow and take the verdict file away from a judge that asked for
    `Write`; `_judge_tool_policy` lifts the whole write family instead.

    The policy comes from the harness, not a hand-written list: a test that
    spells the deny list out itself cannot see that regression.
    """
    import warnings

    from score import _judge_tool_policy

    runner = _runner(monkeypatch, permissions=_judge_tool_policy(allowed))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        snapshot = runner._prepare_permissions(tmp_path)
    try:
        config = json.loads(snapshot.path.read_text())
        assert "Write(**)" not in config["permissions"]["deny"]
        assert any(p.startswith("Write(") for p in config["permissions"]["allow"])
        assert "Shell(**)" in config["permissions"]["deny"]   # still confined
    finally:
        CursorAgentRunner._restore_permissions(snapshot)


def test_cursor_default_judge_policy_scopes_writes_to_the_verdict_dir(
        tmp_path, monkeypatch):
    """The default judge policy must reach Cursor as a SCOPED write, not a
    blanket one and not a blanket deny. Cursor honours path scoping, so
    `Edit(output/**)` becomes `Write(output/**)`: the verdict file is
    writable and nothing else is."""
    import warnings

    from score import _judge_tool_policy

    runner = _runner(monkeypatch, permissions=_judge_tool_policy())
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        snapshot = runner._prepare_permissions(tmp_path)
    try:
        config = json.loads(snapshot.path.read_text())
        allow, deny = (config["permissions"]["allow"],
                       config["permissions"]["deny"])
        assert "Write(output/**)" in allow
        assert "Write(**)" not in allow     # scoped, not blanket
        assert "Write(**)" not in deny      # and not revoked outright
        assert "Shell(**)" in deny and "WebFetch(*)" in deny
    finally:
        CursorAgentRunner._restore_permissions(snapshot)


def test_cursor_allowed_agent_tool_still_rejected(tmp_path, monkeypatch):
    runner = _runner(monkeypatch, permissions={"allow": ["Read", "Agent"]})
    with pytest.raises(ValueError, match="no permission mapping for 'Agent'"):
        runner._prepare_permissions(tmp_path)


@pytest.mark.parametrize("tool", ["MultiEdit", "NotebookEdit"])
def test_cursor_maps_the_claude_only_editor_spellings(tmp_path, monkeypatch,
                                                      tool):
    """Cursor's single `Write` permission is the faithful translation, so
    these map rather than warn — which also keeps a deliberate top-level
    `permissions.deny: [NotebookEdit]` from being silently dropped."""
    runner = _runner(monkeypatch, permissions={"allow": ["Read", tool]})
    snapshot = runner._prepare_permissions(tmp_path)
    try:
        config = json.loads(snapshot.path.read_text())
        assert "Write(**)" in config["permissions"]["allow"]
    finally:
        CursorAgentRunner._restore_permissions(snapshot)


def test_cursor_permission_translation_keeps_mcp_wildcards(tmp_path, monkeypatch):
    runner = _runner(monkeypatch, permissions={"allow": ["mcp__*"]})
    snapshot = runner._prepare_permissions(tmp_path)
    try:
        config = json.loads(snapshot.path.read_text())
        assert config["permissions"]["allow"] == ["Mcp(*:*)"]
        assert "Mcp(*:*)" not in config["permissions"]["deny"]
    finally:
        CursorAgentRunner._restore_permissions(snapshot)


def test_cursor_path_scoped_allow_does_not_become_global_permission(
    tmp_path, monkeypatch,
):
    runner = _runner(monkeypatch, permissions={
        "allow": [{"path": "eval/", "tools": ["Mcp", "Bash"]}],
    })
    with pytest.warns(RuntimeWarning, match="cannot path-scope"):
        snapshot = runner._prepare_permissions(tmp_path)
    try:
        config = json.loads(snapshot.path.read_text())
        assert config["permissions"]["allow"] == []
        assert "Mcp(*:*)" in config["permissions"]["deny"]
        assert "Shell(**)" in config["permissions"]["deny"]
    finally:
        CursorAgentRunner._restore_permissions(snapshot)


def test_cursor_permission_file_links_are_safe(tmp_path, monkeypatch):
    cursor_dir = tmp_path / ".cursor"
    cursor_dir.mkdir()
    target = tmp_path / "outside.json"
    target.write_bytes(b"must survive")
    config_path = cursor_dir / "cli.json"
    config_path.symlink_to(target)

    with pytest.raises(ValueError, match="safely inspect|symlink"):
        _runner(monkeypatch, permissions={"allow": ["Read"]})._prepare_permissions(
            tmp_path)
    assert target.read_bytes() == b"must survive"

    config_path.unlink()
    original = b'{"permissions": {"allow": ["Read(**)"]}}\n'
    config_path.write_bytes(original)

    snapshot = _runner(
        monkeypatch, permissions={"allow": ["Read"]}
    )._prepare_permissions(tmp_path)
    config_path.unlink()
    config_path.symlink_to(target)
    CursorAgentRunner._restore_permissions(snapshot)

    assert target.read_bytes() == b"must survive"
    assert not config_path.is_symlink()
    assert config_path.read_bytes() == original


def test_cursor_stream_parser_ignores_malformed_metrics():
    summary = _parse_cursor_stream("\n".join([
        '{"type":"system","subtype":"init","model":[]}',
        '{"type":"assistant","message":{"model":{},"content":[]}}',
        '{"type":"result","total_cost_usd":"secret",'
        '"usage":{"inputTokens":"huge"}}',
    ]))

    assert summary.resolved_model is None
    assert summary.models_used is None
    assert summary.cost_usd is None
    assert summary.token_usage == {
        "input": 0, "output": 0, "cache_read": 0, "cache_create": 0,
    }
