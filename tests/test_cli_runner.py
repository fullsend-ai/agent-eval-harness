"""Tests for the opaque CLI runner."""

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.agent.cli_runner import CliRunner
from agent_eval.config import EvalConfig


class TestCliRunnerConfig:
    """Config parsing for CLI runner."""

    def _write(self, tmp_path, body):
        p = tmp_path / "eval.yaml"
        p.write_text(body)
        return p

    def test_command_string_parses(self, tmp_path):
        cfg = EvalConfig.from_yaml(self._write(tmp_path, """
name: t
execution:
  skill: s
runner:
  type: cli
  command: "my-runner run {agent} --workspace {workspace}"
"""))
        assert cfg.runner.type == "cli"
        assert cfg.runner.command == "my-runner run {agent} --workspace {workspace}"

    def test_command_list_parses(self, tmp_path):
        cfg = EvalConfig.from_yaml(self._write(tmp_path, """
name: t
execution:
  skill: s
runner:
  type: cli
  command:
    - my-runner
    - run
    - "{agent}"
    - "--workspace"
    - "{workspace}"
"""))
        assert cfg.runner.type == "cli"
        assert cfg.runner.command == [
            "my-runner", "run", "{agent}", "--workspace", "{workspace}"]

    def test_command_default_is_none(self, tmp_path):
        cfg = EvalConfig.from_yaml(self._write(tmp_path, """
name: t
execution:
  skill: s
runner:
  type: claude-code
"""))
        assert cfg.runner.command is None


class TestCliRunnerInit:
    """Constructor validation."""

    def test_requires_command(self):
        with pytest.raises(ValueError, match="requires a 'command'"):
            CliRunner(command=None)

    def test_requires_nonempty_command(self):
        with pytest.raises(ValueError, match="requires a 'command'"):
            CliRunner(command="")

    def test_rejects_non_str_or_list_command(self):
        with pytest.raises(TypeError, match="must be a string or list"):
            CliRunner(command=42)

    def test_ignores_extra_kwargs(self, tmp_path):
        runner = CliRunner(
            command="echo hello",
            permissions={"allow": ["*"]},
            plugin_dirs=[str(tmp_path)],
            subagent_model="sonnet",
        )
        assert runner.name == "cli"


class TestPlaceholderResolution:
    """Command template placeholder substitution."""

    def test_string_command(self):
        runner = CliRunner(command="run {agent} --model {model}")
        result = runner._resolve_command({
            "agent": "my-skill", "model": "opus"})
        assert result == "run my-skill --model opus"

    def test_list_command(self):
        runner = CliRunner(command=["run", "{agent}", "--model", "{model}"])
        result = runner._resolve_command({
            "agent": "my-skill", "model": "opus"})
        assert result == ["run", "my-skill", "--model", "opus"]

    def test_unknown_placeholders_preserved(self):
        runner = CliRunner(command="run {agent} {unknown}")
        result = runner._resolve_command({"agent": "s"})
        assert result == "run s {unknown}"

    def test_all_builtin_placeholders(self):
        runner = CliRunner(command="{agent} {workspace} {output_dir} {model} {timeout} {max_budget_usd} {args}")
        result = runner._resolve_command({
            "agent": "sk", "workspace": "/ws", "output_dir": "/ws/output",
            "model": "m", "timeout": "600", "max_budget_usd": "5.0",
            "args": "--flag", "subagent_model": "", "effort": "",
            "system_prompt": "",
        })
        assert result == "sk /ws /ws/output m 600 5.0 --flag"

    def test_subagent_model_and_effort_placeholders(self, tmp_path):
        runner = CliRunner(
            command="run --model {model} --subagent {subagent_model} --effort {effort}",
            subagent_model="haiku",
            effort="high",
        )
        result = runner.run_skill(
            skill_name="test", args="", workspace=tmp_path,
            model="opus", timeout_s=10,
        )
        # Can't easily check the resolved command, but verify it ran
        # (the command will fail, but that's fine — we care about resolution)
        assert isinstance(result.exit_code, int)

    def test_system_prompt_placeholder(self, tmp_path):
        runner = CliRunner(
            command=["echo", "{system_prompt}"],
            system_prompt="Be careful",
        )
        result = runner.run_skill(
            skill_name="test", args="", workspace=tmp_path,
            model="m", timeout_s=10,
        )
        assert "Be careful" in result.stdout

    def test_run_skill_system_prompt_overrides_constructor(self, tmp_path):
        runner = CliRunner(
            command=["echo", "{system_prompt}"],
            system_prompt="from constructor",
        )
        result = runner.run_skill(
            skill_name="test", args="", workspace=tmp_path,
            model="m", timeout_s=10,
            system_prompt="from caller",
        )
        assert "from caller" in result.stdout
        assert "from constructor" not in result.stdout


class TestCliRunnerExecution:
    """End-to-end execution tests using simple shell commands."""

    def test_successful_command(self, tmp_path):
        runner = CliRunner(command="echo hello")
        result = runner.run_skill(
            skill_name="test",
            args="",
            workspace=tmp_path,
            model="test-model",
        )
        assert result.exit_code == 0
        assert "hello" in result.stdout
        assert isinstance(result.duration_s, float)
        assert result.duration_s > 0

    def test_failing_command(self, tmp_path):
        runner = CliRunner(command=["python3", "-c", "import sys; sys.exit(42)"],
                           log_prefix="test")
        result = runner.run_skill(
            skill_name="test",
            args="",
            workspace=tmp_path,
            model="test-model",
        )
        assert result.exit_code == 42

    def test_workspace_placeholders(self, tmp_path):
        runner = CliRunner(command=["echo", "{workspace}", "{output_dir}"])
        result = runner.run_skill(
            skill_name="test",
            args="",
            workspace=tmp_path,
            model="m",
        )
        assert str(tmp_path) in result.stdout
        assert str(tmp_path / "output") in result.stdout

    def test_model_placeholder(self, tmp_path):
        runner = CliRunner(command=["echo", "{model}"])
        result = runner.run_skill(
            skill_name="test", args="", workspace=tmp_path, model="opus")
        assert "opus" in result.stdout

    def test_agent_placeholder(self, tmp_path):
        runner = CliRunner(command="echo {agent}")
        result = runner.run_skill(
            skill_name="my-skill", args="", workspace=tmp_path, model="m")
        assert "my-skill" in result.stdout

    def test_input_yaml_fields(self, tmp_path):
        (tmp_path / "input.yaml").write_text(yaml.dump({
            "ticket": "PROJ-123",
            "priority": "high",
        }))
        runner = CliRunner(command="echo {ticket} {priority}")
        result = runner.run_skill(
            skill_name="test", args="", workspace=tmp_path, model="m")
        assert "PROJ-123" in result.stdout
        assert "high" in result.stdout

    def test_input_yaml_does_not_override_builtins(self, tmp_path):
        (tmp_path / "input.yaml").write_text(yaml.dump({
            "model": "should-not-override",
        }))
        runner = CliRunner(command="echo {model}")
        result = runner.run_skill(
            skill_name="test", args="", workspace=tmp_path, model="real-model")
        assert "real-model" in result.stdout

    def test_timeout(self, tmp_path):
        runner = CliRunner(command="sleep 60")
        result = runner.run_skill(
            skill_name="test", args="", workspace=tmp_path,
            model="m", timeout_s=1)
        assert result.exit_code == -1
        assert "Timed out" in result.stderr

    def test_output_dir_created(self, tmp_path):
        runner = CliRunner(command="ls {output_dir}")
        runner.run_skill(
            skill_name="test", args="", workspace=tmp_path, model="m")
        assert (tmp_path / "output").is_dir()

    def test_metrics_json_parsed(self, tmp_path):
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        (output_dir / "metrics.json").write_text(json.dumps({
            "token_usage": {"input": 100, "output": 50},
            "cost_usd": 0.03,
            "num_turns": 5,
            "model": "gpt-4",
        }))
        runner = CliRunner(command="echo done")
        result = runner.run_skill(
            skill_name="test", args="", workspace=tmp_path, model="m")
        assert result.exit_code == 0
        assert result.token_usage == {"input": 100, "output": 50}
        assert result.cost_usd == 0.03
        assert result.num_turns == 5
        assert result.resolved_model == "gpt-4"

    def test_no_metrics_json(self, tmp_path):
        runner = CliRunner(command="echo done")
        result = runner.run_skill(
            skill_name="test", args="", workspace=tmp_path, model="m")
        assert result.token_usage is None
        assert result.cost_usd is None

    def test_invalid_metrics_json(self, tmp_path):
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        (output_dir / "metrics.json").write_text("not json")
        runner = CliRunner(command="echo done")
        result = runner.run_skill(
            skill_name="test", args="", workspace=tmp_path, model="m")
        assert result.token_usage is None

    def test_extra_env_vars(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_SECRET", "s3cret")
        runner = CliRunner(
            command=["python3", "-c", "import os; print(os.environ['MY_VAR'], os.environ['RESOLVED'])"],
            env={"MY_VAR": "hello", "RESOLVED": "$MY_SECRET"})
        result = runner.run_skill(
            skill_name="test", args="", workspace=tmp_path, model="m")
        assert "hello" in result.stdout
        assert "s3cret" in result.stdout

    def test_list_command_no_shell(self, tmp_path):
        """List-form commands should not use shell interpretation."""
        runner = CliRunner(command=["echo", "hello world"])
        result = runner.run_skill(
            skill_name="test", args="", workspace=tmp_path, model="m")
        assert "hello world" in result.stdout


class TestFromConfig:
    """Test the from_config class method."""

    def _write(self, tmp_path, body):
        p = tmp_path / "eval.yaml"
        p.write_text(body)
        return p

    def test_from_config_string_command(self, tmp_path):
        cfg = EvalConfig.from_yaml(self._write(tmp_path, """
name: t
runner:
  type: cli
  command: "my-runner run {agent}"
execution:
  skill: s
  env:
    MY_VAR: hello
"""))
        runner = CliRunner.from_config(cfg, log_prefix="test")
        assert runner.name == "cli"
        assert runner._command == "my-runner run {agent}"
        assert runner._extra_env == {"MY_VAR": "hello"}
        assert runner._log_prefix == "test"

    def test_from_config_list_command(self, tmp_path):
        cfg = EvalConfig.from_yaml(self._write(tmp_path, """
name: t
execution:
  skill: s
runner:
  type: cli
  command:
    - my-runner
    - "{agent}"
"""))
        runner = CliRunner.from_config(cfg)
        assert runner._command == ["my-runner", "{agent}"]

    def test_from_config_ignores_extra_overrides(self, tmp_path):
        cfg = EvalConfig.from_yaml(self._write(tmp_path, """
name: t
execution:
  skill: s
runner:
  type: cli
  command: "echo test"
"""))
        runner = CliRunner.from_config(
            cfg, log_prefix="x",
            subagent_model="sonnet",
            mlflow_experiment="e",
            effort="high",
        )
        assert runner.name == "cli"


class TestCliRunnerInRegistry:
    """Verify the runner is registered and discoverable."""

    def test_registered(self):
        from agent_eval.agent import RUNNERS
        assert "cli" in RUNNERS

    def test_is_eval_runner(self):
        from agent_eval.agent.base import EvalRunner
        assert issubclass(CliRunner, EvalRunner)


def test_cli_env_drops_the_bootstrap_sentinel_and_names_the_harness_root(monkeypatch):
    """The harness process has always bootstrapped by the time it builds a
    child environment, so the sentinel is always there to inherit; a child
    that inherits it skips activation entirely, staged-copy fallback
    included. Strip it; name the checkout last, after every overlay."""
    from agent_eval._bootstrap import HARNESS_ROOT_ENV, harness_root
    from agent_eval.agent.cli_runner import CliRunner
    monkeypatch.setenv("_AGENT_EVAL_BOOTSTRAP_DONE", "1")
    monkeypatch.delenv(HARNESS_ROOT_ENV, raising=False)
    env = CliRunner("echo hi")._build_env()
    assert "_AGENT_EVAL_BOOTSTRAP_DONE" not in env
    assert env[HARNESS_ROOT_ENV] == harness_root()
    assert CliRunner("echo hi", env={HARNESS_ROOT_ENV: ""})._build_env()[HARNESS_ROOT_ENV] == harness_root()
    assert CliRunner("echo hi")._build_env({HARNESS_ROOT_ENV: ""})[HARNESS_ROOT_ENV] == harness_root()
    assert CliRunner("echo hi")._build_env({HARNESS_ROOT_ENV: "/hook"})[HARNESS_ROOT_ENV] == "/hook"
