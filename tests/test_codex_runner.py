"""Tests for the Codex CLI runner."""

import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from agent_eval.agent import RUNNERS
from agent_eval.agent.codex import CodexRunner, _extract_usage, _toml_value
from agent_eval.config import EvalConfig

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not on PATH")


def _plugin(tmp_path, *names):
    plugin = tmp_path / "plugin"
    for name in names or ("parent",):
        skill = plugin / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"# {name}\n")
    return plugin


def test_codex_is_registered():
    assert RUNNERS["codex"] is CodexRunner


def test_codex_from_config_accepts_effort_settings_and_stdin_prompt(
        tmp_path, monkeypatch):
    plugin = _plugin(tmp_path, "parent")
    config_path = tmp_path / "eval.yaml"
    config_path.write_text(yaml.safe_dump({
        "name": "codex-test",
        "execution": {"skill": "ci:parent"},
        "runner": {
            "type": "codex",
            "plugin_dirs": [str(plugin)],
            "effort": "xhigh",
            "settings": {"model_reasoning_summary": "concise"},
        },
    }))
    runner = CodexRunner.from_config(EvalConfig.from_yaml(config_path))
    captured = {}

    class FakeProcess:
        returncode = 0

        def communicate(self, input=None, timeout=None):
            captured["input"] = input
            return (json.dumps({
                "type": "turn.completed",
                "usage": {"input_tokens": 12, "output_tokens": 3,
                          "cached_input_tokens": 4},
            }) + "\n", "")

    def fake_popen(command, **kwargs):
        assert (tmp_path / "workspace" / ".agents" / "skills" /
                "parent" / "SKILL.md").is_file()
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr("agent_eval.agent.codex.subprocess.Popen", fake_popen)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = runner.execute(
        "ci:parent", "--flag value", workspace, "gpt-5.6-luna", timeout_s=5)

    assert result.exit_code == 0
    assert result.token_usage == {"input": 8, "output": 3, "cache_read": 4}
    assert not (workspace / ".agents").exists()  # staged copies are disposable
    assert captured["kwargs"]["stdin"] is subprocess.PIPE
    assert captured["input"] == "Use the parent skill with arguments: --flag value"
    command = captured["command"]
    assert command[:2] == ["codex", "exec"]
    assert "--ephemeral" in command
    assert "--skip-git-repo-check" in command
    assert command[command.index("-C") + 1] == str(workspace.resolve())
    assert ["--sandbox", "workspace-write"] == command[
        command.index("--sandbox"):command.index("--sandbox") + 2]
    assert ["--model", "gpt-5.6-luna"] == command[
        command.index("--model"):command.index("--model") + 2]
    assert "model_reasoning_effort=\"xhigh\"" in command
    assert "model_reasoning_summary=\"concise\"" in command
    assert command[-2:] == ["--", "-"]
    assert "--flag value" not in command


def test_codex_permission_mode_intent_mapping(tmp_path, monkeypatch):
    captured = []

    class FakeProcess:
        returncode = 0

        def communicate(self, input=None, timeout=None):
            return "", ""

    monkeypatch.setattr(
        "agent_eval.agent.codex.subprocess.Popen",
        lambda command, **kwargs: captured.append(command) or FakeProcess())
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    CodexRunner(permission_mode="plan").execute(None, "p", workspace, "m")
    CodexRunner(permission_mode="bypassPermissions").execute(
        None, "p", workspace, "m")

    assert ["--sandbox", "read-only"] == captured[0][
        captured[0].index("--sandbox"):captured[0].index("--sandbox") + 2]
    assert "--dangerously-bypass-approvals-and-sandbox" in captured[1]
    assert "--sandbox" not in captured[1]


def test_codex_warns_for_untranslatable_permissions_and_budget(
        tmp_path, monkeypatch):
    with pytest.warns(RuntimeWarning, match="tool-level permission"):
        runner = CodexRunner(permissions={"deny": ["WebFetch"]})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    proc = type("FakeProcess", (), {
        "returncode": 0,
        "communicate": lambda self, input=None, timeout=None: ("", ""),
    })()
    monkeypatch.setattr(
        "agent_eval.agent.codex.subprocess.Popen", lambda *a, **k: proc)
    with pytest.warns(RuntimeWarning, match="does not enforce"):
        result = runner.execute(None, "p", workspace, "m", max_budget_usd=25)
    assert result.exit_code == 0


@pytest.mark.parametrize("effort", ["minimal", "low", "medium", "high", "xhigh"])
def test_codex_effort_allowlist(effort):
    assert CodexRunner(effort=effort)._effort == effort


def test_codex_rejects_max_effort():
    with pytest.raises(ValueError, match="Invalid effort"):
        CodexRunner(effort="max")


def test_canonical_effort_overrides_legacy_setting():
    runner = CodexRunner(
        effort="xhigh", config_overrides={"model_reasoning_effort": "max"})
    assert runner._config_overrides["model_reasoning_effort"] == "xhigh"


def test_codex_stages_copies_all_sibling_skills_and_cleanup(tmp_path):
    plugin = _plugin(tmp_path, "parent", "dependency")
    runner = CodexRunner(plugin_dirs=[str(plugin)])
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    manifest = runner._stage_skills(workspace)
    staged = workspace / ".agents" / "skills"
    assert (staged / "parent" / "SKILL.md").is_file()
    assert (staged / "dependency" / "SKILL.md").is_file()
    assert not (staged / "parent").is_symlink()
    (staged / "parent" / "SKILL.md").write_text("mutated\n")
    assert (plugin / "skills" / "parent" / "SKILL.md").read_text() == "# parent\n"

    runner._cleanup_staged_skills(workspace, manifest)
    assert not (workspace / ".agents").exists()


def test_codex_staging_refreshes_interrupted_copy_and_skips_non_skills(tmp_path):
    plugin = _plugin(tmp_path, "parent")
    (plugin / "skills" / "README.md").write_text("not a skill")
    (plugin / "skills" / "empty").mkdir()
    runner = CodexRunner(plugin_dirs=[str(plugin)])
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    first = runner._stage_skills(workspace)
    (plugin / "skills" / "parent" / "SKILL.md").write_text("# refreshed\n")
    second = runner._stage_skills(workspace)
    assert len(first["created"]) == 1
    assert len(second["created"]) == 1
    assert (workspace / ".agents" / "skills" / "parent" /
            "SKILL.md").read_text() == "# refreshed\n"
    assert not (workspace / ".agents" / "skills" / "empty").exists()
    runner._cleanup_staged_skills(workspace, second)


def test_codex_rejects_dangling_staged_symlink(tmp_path):
    runner = CodexRunner(plugin_dirs=[str(_plugin(tmp_path, "parent"))])
    workspace = tmp_path / "workspace"
    staged = workspace / ".agents" / "skills"
    staged.mkdir(parents=True)
    (staged / "parent").symlink_to(tmp_path / "missing", target_is_directory=True)
    with pytest.raises(ValueError, match="not owned"):
        runner._stage_skills(workspace)


def test_codex_validates_plugin_root_before_execution(tmp_path):
    with pytest.raises(FileNotFoundError, match="plugin directory"):
        CodexRunner(plugin_dirs=[str(tmp_path / "missing")])
    commands_only = tmp_path / "commands-only"
    commands_only.mkdir()
    with pytest.raises(FileNotFoundError, match="skill directory"):
        CodexRunner(plugin_dirs=[str(commands_only)])


def test_codex_honors_manifest_declared_skill_roots(tmp_path):
    plugin = tmp_path / "plugin"
    skill = plugin / "custom-skills" / "parent"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# parent\n")
    manifest = plugin / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"skills": "custom-skills"}))
    runner = CodexRunner(plugin_dirs=[str(plugin)])
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    staged = runner._stage_skills(workspace)

    assert (workspace / ".agents" / "skills" / "parent" /
            "SKILL.md").is_file()
    runner._cleanup_staged_skills(workspace, staged)


def test_codex_without_plugins_does_not_touch_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = CodexRunner()

    manifest = runner._stage_skills(workspace)
    runner._cleanup_staged_skills(workspace, manifest)

    assert manifest["active"] is False
    assert not (workspace / ".agents").exists()


def test_staging_refusal_leaves_preexisting_empty_dirs_alone(tmp_path):
    # Refusing to replace an unowned tree and then rmdir'ing it in cleanup
    # would contradict the refusal — even when the directories are empty.
    plugin = _plugin(tmp_path, "parent")
    workspace = tmp_path / "workspace"
    (workspace / ".agents" / "skills").mkdir(parents=True)
    runner = CodexRunner(plugin_dirs=[str(plugin)])

    with pytest.raises(ValueError, match="not owned by agent-eval-harness"):
        runner._stage_skills(workspace)

    assert (workspace / ".agents" / "skills").is_dir()


def test_invalid_settings_values_fail_at_construction():
    with pytest.raises(ValueError, match="Invalid Codex settings value"):
        CodexRunner(config_overrides={"web_search": None})


def test_invalid_permission_mode_rejected():
    # permission_mode is the harness's runner-agnostic vocabulary (Claude
    # Code mode names). Codex CLI surface flags are not accepted here —
    # codex's --yolo capability is spelled "bypassPermissions", which the
    # runner translates to --dangerously-bypass-approvals-and-sandbox.
    with pytest.raises(ValueError, match="Invalid permission_mode"):
        CodexRunner(permission_mode="not-a-mode")


def test_duplicate_skill_name_across_plugin_roots_raises(tmp_path):
    plugin_a = _plugin(tmp_path / "a", "shared")
    plugin_b = _plugin(tmp_path / "b", "shared")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = CodexRunner(plugin_dirs=[str(plugin_a), str(plugin_b)])

    with pytest.raises(ValueError, match="Duplicate Codex skill name"):
        runner._stage_skills(workspace)

    # Cleanup ran: the partially staged tree is gone.
    assert not (workspace / ".agents").exists()


@requires_git
def test_repo_staging_does_not_dirty_git_status(tmp_path):
    plugin = _plugin(tmp_path, "parent")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run([
        "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "-c", "commit.gpgsign=false",
        "commit", "-q", "--allow-empty", "-m", "init",
    ], cwd=repo, check=True)
    runner = CodexRunner(plugin_dirs=[str(plugin)])

    manifest = runner._stage_skills(repo)
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo,
        check=True, capture_output=True, text=True).stdout
    assert status == ""
    runner._cleanup_staged_skills(repo, manifest)
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo,
        check=True, capture_output=True, text=True).stdout
    assert status == ""


@requires_git
def test_nested_workspace_staging_does_not_dirty_git_status(tmp_path):
    # Workspaces commonly live in nested repo paths (eval/runs/<case>/workspace);
    # the exclude rule must be anchored to the workspace, not the repo root.
    plugin = _plugin(tmp_path, "parent")
    repo = tmp_path / "repo"
    workspace = repo / "eval" / "runs" / "case-1" / "workspace"
    workspace.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run([
        "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "-c", "commit.gpgsign=false",
        "commit", "-q", "--allow-empty", "-m", "init",
    ], cwd=repo, check=True)
    runner = CodexRunner(plugin_dirs=[str(plugin)])

    manifest = runner._stage_skills(workspace)
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo,
        check=True, capture_output=True, text=True).stdout
    assert status == ""
    exclude = repo / ".git" / "info" / "exclude"
    assert "/eval/runs/case-1/workspace/.agents/" in exclude.read_text()

    runner._cleanup_staged_skills(workspace, manifest)
    assert ".agents" not in exclude.read_text()


def test_partial_copytree_failure_is_cleaned_up_and_restaged(
        tmp_path, monkeypatch):
    plugin = _plugin(tmp_path, "parent")
    (plugin / "skills" / "parent" / "extra.py").write_text("x = 1\n")
    runner = CodexRunner(plugin_dirs=[str(plugin)])
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    real_copytree = shutil.copytree

    def failing_copytree(src, dst, **kwargs):
        Path(dst).mkdir(parents=True)
        (Path(dst) / "SKILL.md").write_text("truncated")
        raise shutil.Error([(str(src), str(dst), "simulated copy failure")])

    monkeypatch.setattr(
        "agent_eval.agent.codex.shutil.copytree", failing_copytree)
    with pytest.raises(shutil.Error):
        runner._stage_skills(workspace)
    # The truncated destination must not survive to be treated as a complete
    # skill by later stagings.
    assert not (workspace / ".agents").exists()

    monkeypatch.setattr("agent_eval.agent.codex.shutil.copytree", real_copytree)
    manifest = runner._stage_skills(workspace)
    assert (workspace / ".agents" / "skills" / "parent" / "extra.py").is_file()
    runner._cleanup_staged_skills(workspace, manifest)


def test_toml_value_serializes_mappings_codex_can_parse():
    # codex-cli parses -c values as TOML; JSON objects would fall back to a
    # plain string.
    assert _toml_value("concise") == '"concise"'
    assert _toml_value(4) == "4"
    assert _toml_value(True) == "true"
    assert _toml_value(["a", 1]) == '["a", 1]'
    value = {"network access": True, "nested": {"x-y": ["/tmp"]}}
    encoded = _toml_value(value)
    assert tomllib.loads(f"value = {encoded}")["value"] == value
    assert encoded == '{"network access" = true, "nested" = {"x-y" = ["/tmp"]}}'
    with pytest.raises(ValueError, match="null"):
        _toml_value(None)
    with pytest.raises(ValueError, match="keys must be strings"):
        _toml_value({1: "bad"})


def test_codex_malformed_events_degrade_gracefully():
    events = [
        {"type": "turn.completed", "usage": "oops"},
        {"type": "turn.completed",
         "usage": {"input_tokens": "many", "output_tokens": 3}},
    ]
    usage = _extract_usage(events)
    assert usage["token_usage"] == {"input": 0, "output": 3, "cache_read": 0}
    assert usage["num_turns"] == 2
    assert CodexRunner._extract_progress(
        {"type": "item.started", "item": "oops"}) == ""


def test_codex_missing_binary_returns_failed_result(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_eval.agent.codex.subprocess.Popen",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("codex missing")))
    result = CodexRunner().execute(None, "p", tmp_path, "m")
    assert result.exit_code == -1
    assert "codex missing" in result.stderr


def _fake_litellm(monkeypatch, known_models, rate=0.001):
    import types
    calls = []

    def cost_per_token(*, model, prompt_tokens, completion_tokens,
                       cache_read_input_tokens):
        calls.append({"model": model, "prompt": prompt_tokens,
                      "completion": completion_tokens,
                      "cache_read": cache_read_input_tokens})
        return prompt_tokens * rate, completion_tokens * rate

    fake = types.ModuleType("litellm")
    fake.model_cost = {name: {"input_cost_per_token": rate}
                       for name in known_models}
    fake.cost_per_token = cost_per_token
    monkeypatch.setitem(sys.modules, "litellm", fake)
    return calls


def test_estimate_cost_prices_each_turn_with_harbor_field_mapping(monkeypatch):
    from agent_eval.agent.codex import _estimate_cost_usd
    calls = _fake_litellm(monkeypatch, {"gpt-5.6-luna"})
    events = [
        {"type": "turn.completed", "usage": {
            "input_tokens": 100, "cached_input_tokens": 40,
            "output_tokens": 10}},
        {"type": "turn.completed", "usage": {
            "input_tokens": 200, "cached_input_tokens": 0,
            "output_tokens": 20}},
        {"type": "item.completed", "item": {}},
    ]

    cost = _estimate_cost_usd(events, "openai/gpt-5.6-luna")

    # Pricing key falls back to the last path segment, per Harbor.
    assert [c["model"] for c in calls] == ["gpt-5.6-luna"] * 2
    # prompt_tokens is the superset including cached, cache passed separately.
    assert calls[0] == {"model": "gpt-5.6-luna", "prompt": 100,
                        "completion": 10, "cache_read": 40}
    assert cost == pytest.approx((100 + 10 + 200 + 20) * 0.001)
    # Multi-segment identifiers fall through to the bare last segment.
    assert _estimate_cost_usd(
        events[:1], "azure/openai/gpt-5.6-luna") is not None


def test_estimate_cost_degrades_to_none(monkeypatch):
    from agent_eval.agent.codex import _estimate_cost_usd
    events = [{"type": "turn.completed", "usage": {"input_tokens": 5}}]

    _fake_litellm(monkeypatch, set())  # model absent from pricing table
    assert _estimate_cost_usd(events, "unknown-model") is None

    monkeypatch.setitem(sys.modules, "litellm", None)  # import fails
    assert _estimate_cost_usd(events, "gpt-5.6-luna") is None

    _fake_litellm(monkeypatch, {"gpt-5.6-luna"})
    assert _estimate_cost_usd([], "gpt-5.6-luna") is None  # no turns
    assert _estimate_cost_usd(events, None) is None  # no model


def test_codex_interrupt_kills_process_group(tmp_path, monkeypatch):
    # start_new_session=True detaches codex from the terminal's foreground
    # group, so it never sees Ctrl-C itself; the runner must kill the group
    # before propagating the interrupt or the agent keeps running unattended.
    class FakeProcess:
        pid = 7777
        returncode = None

        def communicate(self, input=None, timeout=None):
            raise KeyboardInterrupt

    killed = []
    monkeypatch.setattr(
        "agent_eval.agent.codex.subprocess.Popen", lambda *a, **k: FakeProcess())
    monkeypatch.setattr("agent_eval.agent.codex.os.killpg",
                        lambda pid, sig: killed.append((pid, sig)))

    with pytest.raises(KeyboardInterrupt):
        CodexRunner().execute(None, "p", tmp_path, "m", timeout_s=1)

    assert killed and killed[0][0] == 7777


def test_codex_timeout_kills_group_bounds_reap_and_keeps_partial_usage(
        tmp_path, monkeypatch):
    calls = []

    class FakeProcess:
        pid = 4321
        returncode = -9

        def communicate(self, input=None, timeout=None):
            calls.append((input, timeout))
            event = json.dumps({
                "type": "turn.completed",
                "usage": {"input_tokens": 2, "output_tokens": 1},
            }) + "\n"
            if len(calls) == 1:
                raise subprocess.TimeoutExpired("codex", timeout, output=event)
            return event, "killed"

    killed = []
    monkeypatch.setattr(
        "agent_eval.agent.codex.subprocess.Popen", lambda *a, **k: FakeProcess())
    monkeypatch.setattr("agent_eval.agent.codex.os.killpg",
                        lambda pid, sig: killed.append((pid, sig)))
    result = CodexRunner().execute(None, "p", tmp_path, "m", timeout_s=1)

    assert killed and killed[0][0] == 4321
    assert calls[-1][1] == 5
    assert result.exit_code == -1
    assert result.token_usage == {"input": 2, "output": 1, "cache_read": 0}


def test_codex_post_kill_communicate_timeout_is_bounded(tmp_path, monkeypatch):
    class FakeProcess:
        pid = 4321
        returncode = -9

        def communicate(self, input=None, timeout=None):
            raise subprocess.TimeoutExpired("codex", timeout, output="partial\n")

    monkeypatch.setattr(
        "agent_eval.agent.codex.subprocess.Popen", lambda *a, **k: FakeProcess())
    monkeypatch.setattr("agent_eval.agent.codex.os.killpg", lambda *a: None)
    result = CodexRunner().execute(None, "p", tmp_path, "m", timeout_s=1)
    assert result.exit_code == -1
    assert result.stdout == "partial\n"
    assert "termination hung" in result.stderr


@pytest.mark.parametrize("stdout,events", [
    ('not-json\n{"type":"thread.started"}\n', 1),
    ('[]\n{"type":"turn.completed","usage":{}}\n', 1),
    ("", 0),
])
def test_codex_malformed_mixed_and_empty_output(tmp_path, monkeypatch, stdout, events):
    class FakeProcess:
        returncode = 0

        def communicate(self, input=None, timeout=None):
            return stdout, ""

    monkeypatch.setattr(
        "agent_eval.agent.codex.subprocess.Popen", lambda *a, **k: FakeProcess())
    result = CodexRunner().execute(None, "p", tmp_path, "m")
    assert len(result.raw_output["events"]) == events
    assert result.stdout == stdout
    if not stdout:
        assert result.token_usage is None


def test_codex_env_is_allowlisted_resolves_refs_and_skips_none(monkeypatch):
    monkeypatch.setenv("UNRELATED_HOST_SECRET", "must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "allowed")
    monkeypatch.setenv("SOURCE_VALUE", "resolved")
    runner = CodexRunner(env={
        "COPIED": "$SOURCE_VALUE", "MISSING": "$DOES_NOT_EXIST", "NULL": None})
    env = runner._build_env({"HOOK": 3, "HOOK_NULL": None})
    assert env["OPENAI_API_KEY"] == "allowed"
    assert env["COPIED"] == "resolved"
    assert env["HOOK"] == "3"
    assert "UNRELATED_HOST_SECRET" not in env
    # The harness names its own checkout for the agent's skill scripts
    # (staged copies of agent_eval find .eval-venv through it); an explicit
    # operator value is forwarded instead.
    from agent_eval._bootstrap import HARNESS_ROOT_ENV, harness_root
    monkeypatch.delenv(HARNESS_ROOT_ENV, raising=False)   # ambient in harness-launched sessions
    assert runner._build_env()[HARNESS_ROOT_ENV] == harness_root()
    monkeypatch.setenv(HARNESS_ROOT_ENV, "/elsewhere/harness")
    assert runner._build_env()[HARNESS_ROOT_ENV] == "/elsewhere/harness"
    monkeypatch.setenv(HARNESS_ROOT_ENV, "")                 # blank counts as unset
    assert runner._build_env()[HARNESS_ROOT_ENV] == harness_root()
    assert "MISSING" not in env
    assert "NULL" not in env
    assert "HOOK_NULL" not in env


def test_codex_progress_never_echoes_event_payload():
    secret = "progress-redaction-sentinel"
    assert CodexRunner._extract_progress({
        "type": "item.completed",
        "item": {"type": "command_execution", "command": f"echo {secret}"},
    }) == "Shell command completed"
    assert CodexRunner._extract_progress({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": secret},
    }) == "Agent message completed"


def test_extract_usage_ignores_non_turn_events():
    assert _extract_usage([
        {"type": "item.completed"},
        {"type": "turn.completed", "usage": {
            "input_tokens": 2, "output_tokens": 1,
        }},
    ]) == {
        "token_usage": {"input": 2, "output": 1, "cache_read": 0},
        "num_turns": 1,
    }
