"""Config schema parsing tests."""

import copy
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Ensure agent_eval is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.config import DatasetConfig, EvalConfig, JudgeConfig, ModelsConfig
from score import _resolve_judge_model


def _write(tmp_path, body, name="eval.yaml"):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    return p


def test_execution_block_parses(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
  mode: batch
  arguments: "--in batch.yaml"
  timeout: 1800
  max_budget_usd: 25.5
"""))
    assert cfg.execution.mode == "batch"
    assert cfg.execution.arguments == "--in batch.yaml"
    assert cfg.execution.timeout == 1800
    assert cfg.execution.max_budget_usd == 25.5


def test_runner_block_parses(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
runner:
  type: claude-code
  plugin_dirs:
    - /tmp/p
  env:
    FOO: "$FOO"
  settings:
    a: 1
  system_prompt: "be careful"
"""))
    assert cfg.runner.type == "claude-code"
    assert cfg.runner.plugin_dirs == ["/tmp/p"]
    assert cfg.runner.env == {"FOO": "$FOO"}
    assert cfg.runner.settings == {"a": 1}
    assert cfg.runner.system_prompt == "be careful"


def test_runner_type_default(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, "name: t\nexecution:\n  skill: s\n"))
    assert cfg.runner.type == "claude-code"


def test_enabled_plugins_wildcard_parses(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
runner:
  settings:
    enabledPlugins:
      "*": false
      memsearch@user-marketplace: true
"""))
    assert cfg.runner.settings["enabledPlugins"]["*"] is False


def test_enabled_plugins_wildcard_rejects_non_boolean(tmp_path):
    with pytest.raises(ValueError, match=r'enabledPlugins."\*" must be a boolean'):
        EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
runner:
  settings:
    enabledPlugins:
      "*": "false"
"""))


def test_models_block_defaults(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
models:
  skill: claude-opus-4-7
  judge: claude-opus-4-7
"""))
    assert cfg.models.skill == "claude-opus-4-7"
    assert cfg.models.subagent is None
    assert cfg.models.judge == "claude-opus-4-7"


def test_mlflow_block_parses(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
mlflow:
  experiment: e1
  tracking_uri: sqlite:///x.db
  tags:
    team: ml
"""))
    assert cfg.mlflow.experiment == "e1"
    assert cfg.mlflow.tracking_uri == "sqlite:///x.db"
    assert cfg.mlflow.tags == {"team": "ml"}


def test_mlflow_experiment_defaults_to_name_when_block_present(tmp_path):
    """`mlflow:` block present but no `experiment:` → fall back to eval name."""
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: my-eval
execution:
  skill: s
mlflow:
  tracking_uri: sqlite:///x.db
"""))
    assert cfg.mlflow.experiment == "my-eval"


def test_mlflow_disabled_when_block_absent(tmp_path):
    """No `mlflow:` block → experiment empty, MLflow logging off."""
    cfg = EvalConfig.from_yaml(_write(tmp_path, "name: my-eval\nexecution:\n  skill: s\n"))
    assert cfg.mlflow.experiment == ""


def test_judge_model_resolution_precedence(tmp_path, monkeypatch):
    """Per-judge `model:` > config.models.judge > EVAL_JUDGE_MODEL > error."""
    cfg = EvalConfig(name="t", skill="s")

    # 1. Per-judge model wins
    jc = JudgeConfig(name="j", model="per-judge-model")
    cfg.models = ModelsConfig(judge="config-judge")
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "env-model")
    assert _resolve_judge_model(jc, cfg) == "per-judge-model"

    # 2. config.models.judge used when per-judge unset
    jc = JudgeConfig(name="j")
    assert _resolve_judge_model(jc, cfg) == "config-judge"

    # 3. env var used when both unset
    cfg.models = ModelsConfig()
    assert _resolve_judge_model(jc, cfg) == "env-model"

    # 4. error when nothing set
    monkeypatch.delenv("EVAL_JUDGE_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="No model configured"):
        _resolve_judge_model(jc, cfg)


def test_env_only_runner_judge_rejected_for_fullsend_runner(monkeypatch):
    """A `runner:/…` judge model supplied only via EVAL_JUDGE_MODEL is not
    visible to the config-load check, so judge build rejects it for a
    fullsend runner instead of every grading call failing later."""
    cfg = EvalConfig(name="t", skill="s")
    cfg.runner.type = "fullsend"
    jc = JudgeConfig(name="j")
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "runner:/opus")
    with pytest.raises(ValueError, match="SDK-backed"):
        _resolve_judge_model(jc, cfg)

    # An SDK-backed env model, an agent judge (always runner-executed) and a
    # non-fullsend runner are all fine.
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "anthropic:/claude-opus-4-1")
    assert _resolve_judge_model(jc, cfg) == "anthropic:/claude-opus-4-1"
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "runner:/opus")
    assert _resolve_judge_model(jc, cfg, grades_via_backend=False) == "runner:/opus"
    cfg.runner.type = "cursor"
    assert _resolve_judge_model(jc, cfg) == "runner:/opus"


def test_judge_model_backend_validated_at_load(tmp_path):
    """An unroutable judge model fails at config load, naming the source."""
    body = ("name: t\nexecution:\n  skill: s\n"
            "models:\n  judge: gemini:/gemini-2.5-flash\n"
            "judges:\n  - {name: j, prompt: rate it}\n")
    with pytest.raises(ValueError, match=r"models\.judge:.*Unsupported"):
        EvalConfig.from_yaml(_write(tmp_path, body))


def test_unsupported_per_judge_provider_rejected_at_load(tmp_path):
    body = ("name: t\nexecution:\n  skill: s\n"
            "judges:\n  - {name: j, prompt: rate it, model: 'mistral:/mistral-large'}\n")
    with pytest.raises(ValueError, match=r"judge 'j' model:.*Unsupported"):
        EvalConfig.from_yaml(_write(tmp_path, body))


def test_provider_and_runner_prefixed_judge_models_load(tmp_path):
    """openai:/, anthropic:/, runner:/, bare aliases and gateway ids all route."""
    for model in ("openai:/gpt-4o", "anthropic:/claude-sonnet-4-5",
                  "runner:/gpt-5.4-medium", "sonnet", "my-gateway-model"):
        body = (f"name: t\nexecution:\n  skill: s\n"
                f"models:\n  judge: {model}\n"
                f"judges:\n  - {{name: j, prompt: rate it}}\n")
        cfg = EvalConfig.from_yaml(_write(tmp_path, body))
        assert cfg.models.judge == model


def test_agent_judge_unsupported_provider_not_rejected_at_load(tmp_path):
    """Agent judges route through the runner (prefix stripped), so an explicit
    non-SDK provider on them must not be rejected at config load."""
    body = ("name: t\nexecution:\n  skill: s\n"
            "judges:\n  - {name: j, prompt: rate it, model: 'gemini:/x', "
            "agent: {allowed_tools: [Read]}}\n")
    cfg = EvalConfig.from_yaml(_write(tmp_path, body))
    assert cfg.judges[0].model == "gemini:/x"


def test_unsupported_feedback_type_rejected_at_load(tmp_path):
    """Categorical feedback_type is unsupported after the make_judge path was
    removed; it must fail at load rather than be silently graded as numeric."""
    body = ("name: t\nexecution:\n  skill: s\n"
            "judges:\n  - {name: j, prompt: rate it, feedback_type: str}\n")
    with pytest.raises(ValueError, match=r"unsupported feedback_type"):
        EvalConfig.from_yaml(_write(tmp_path, body))


# --- Path resolution tests (T009) ---

def test_config_dir_set_from_yaml(tmp_path):
    """config_dir is set to the parent of the loaded eval.yaml."""
    cfg = EvalConfig.from_yaml(_write(tmp_path, "name: t\nexecution:\n  skill: s\n"))
    assert cfg.config_dir == tmp_path.resolve()


def test_config_dir_subdirectory(tmp_path):
    """config_dir follows the eval.yaml location in subdirectories."""
    sub = tmp_path / "eval" / "my-eval"
    p = _write(tmp_path, "name: t\nexecution:\n  skill: s\n",
               name="eval/my-eval/eval.yaml")
    cfg = EvalConfig.from_yaml(p)
    assert cfg.config_dir == sub.resolve()


def test_config_dir_none_fallback():
    """resolve_path falls back to cwd when config_dir is None."""
    cfg = EvalConfig(name="t", skill="s")
    assert cfg.config_dir is None
    resolved = cfg.resolve_path("cases/")
    assert resolved == Path.cwd() / "cases/"


def test_resolve_path_relative(tmp_path):
    """Relative paths resolve against config_dir."""
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
dataset:
  path: cases/
"""))
    resolved = cfg.resolve_path(cfg.dataset.path)
    assert resolved == tmp_path.resolve() / "cases"


def test_resolve_path_absolute(tmp_path):
    """Absolute paths are returned as-is."""
    cfg = EvalConfig(name="t", skill="s", config_dir=tmp_path)
    abs_path = Path("/shared/datasets/common")
    resolved = cfg.resolve_path(abs_path)
    assert resolved == abs_path


def test_absolute_dataset_path_allowed(tmp_path):
    """Absolute dataset.path is accepted by the validator."""
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
dataset:
  path: /shared/datasets/my-cases
"""))
    assert cfg.dataset.path == "/shared/datasets/my-cases"


def test_parent_traversal_rejected(tmp_path):
    """Paths with '..' are rejected."""
    with pytest.raises(ValueError, match="must not contain"):
        EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
dataset:
  path: ../escape
"""))


def test_dataset_resolves_relative_to_nested_config(tmp_path):
    """dataset.path resolves relative to the config, not cwd."""
    config_dir = tmp_path / "eval" / "my-eval"
    cases_dir = config_dir / "cases"
    cases_dir.mkdir(parents=True)
    p = _write(tmp_path, """
name: t
execution:
  skill: s
dataset:
  path: cases/
""", name="eval/my-eval/eval.yaml")
    cfg = EvalConfig.from_yaml(p)
    resolved = cfg.resolve_path(cfg.dataset.path)
    assert resolved == cases_dir


def test_shared_dataset_two_configs(tmp_path):
    """Two configs with absolute dataset.path resolve to the same directory."""
    shared = tmp_path / "shared-cases"
    shared.mkdir()
    cfg_a = EvalConfig(name="a", skill="alpha",
                       config_dir=tmp_path / "eval" / "alpha",
                       dataset=DatasetConfig(path=str(shared.resolve())))
    cfg_b = EvalConfig(name="b", skill="beta",
                       config_dir=tmp_path / "eval" / "beta",
                       dataset=DatasetConfig(path=str(shared.resolve())))
    assert cfg_a.resolve_path(cfg_a.dataset.path) == shared.resolve()
    assert cfg_b.resolve_path(cfg_b.dataset.path) == shared.resolve()


def test_absolute_dataset_path_used_as_is(tmp_path):
    """Absolute dataset.path is used directly, ignoring config_dir."""
    abs_path = tmp_path / "global-cases"
    abs_path.mkdir()
    cfg = EvalConfig.from_yaml(_write(tmp_path, f"""
name: t
execution:
  skill: s
dataset:
  path: {abs_path}
""", name="eval/my-eval/eval.yaml"))
    resolved = cfg.resolve_path(cfg.dataset.path)
    assert resolved == abs_path


def test_batch_mode_warns_on_per_case_hooks(tmp_path):
    """Per-case hooks in batch mode emit a warning at config load time."""
    import warnings
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  mode: batch
  skill: s
hooks:
  before_each:
    - command: "echo setup"
  after_each:
    - command: "echo cleanup"
"""))
    assert len(w) == 1
    assert "before_each, after_each" in str(w[0].message)
    assert "batch mode" in str(w[0].message)


def test_case_mode_no_warning_on_per_case_hooks(tmp_path):
    """Per-case hooks in case mode do not emit a warning."""
    import warnings
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  mode: case
  skill: s
hooks:
  before_each:
    - command: "echo setup"
"""))
    assert len(w) == 0


def test_execution_skill_canonical_no_deprecation(tmp_path):
    """execution.skill is the canonical location — no deprecation warning."""
    import warnings
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  mode: case
  skill: rfe.create
"""))
    assert cfg.resolve_skill() == "rfe.create"
    assert cfg.is_prompt_mode() is False
    assert not [x for x in w if issubclass(x.category, DeprecationWarning)]


def test_top_level_skill_deprecated_but_normalized(tmp_path):
    """Top-level skill still works (auto-normalized) but warns."""
    import warnings
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
skill: rfe.create
"""))
    # Normalized into execution.skill and resolvable
    assert cfg.execution.skill == "rfe.create"
    assert cfg.resolve_skill() == "rfe.create"
    # Deprecation warning emitted
    dep = [x for x in w if issubclass(x.category, DeprecationWarning)
           and "Top-level 'skill:'" in str(x.message)]
    assert len(dep) == 1


def test_prompt_mode_resolution(tmp_path):
    """execution.prompt → prompt mode, no skill target."""
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  mode: case
  prompt: "{{ input.prompt }}"
"""))
    assert cfg.is_prompt_mode() is True
    assert cfg.resolve_skill() is None


# ---------------------------------------------------------------------------
# Agent judge config parsing/validation (specs/010-agent-judge §1, §7)
# ---------------------------------------------------------------------------

from agent_eval.config import RunnerConfig  # noqa: E402


def test_agent_judge_block_parses(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
judges:
  - name: architecture_score
    prompt_file: eval/prompts/arch.md
    feedback_type: int
    score_range: [0, 2]
    samples: 3
    agent:
      allowed_tools: [Read, Grep, Glob]
      context: [.context/architecture-context]
      inputs: [strat-tasks]
      timeout: 420
      max_budget_usd: 2.0
"""))
    assert len(cfg.judges) == 1
    jc = cfg.judges[0]
    assert jc.name == "architecture_score"
    assert isinstance(jc.agent, dict)
    assert jc.agent["allowed_tools"] == ["Read", "Grep", "Glob"]
    assert jc.agent["context"] == [".context/architecture-context"]
    assert jc.agent["inputs"] == ["strat-tasks"]
    assert jc.agent["timeout"] == 420
    assert jc.agent["max_budget_usd"] == 2.0
    assert jc.samples == 3


def test_agent_block_defaults_to_empty_dict_when_absent(tmp_path):
    """A plain LLM judge (no agent:) has agent == {} (falsy)."""
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
judges:
  - name: plain_llm
    prompt: grade it
"""))
    assert cfg.judges[0].agent == {}
    assert not cfg.judges[0].agent


def test_agent_non_dict_raises(tmp_path):
    with pytest.raises(ValueError, match="'agent' must be a mapping"):
        EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
judges:
  - name: bad
    prompt: grade
    agent: "not-a-mapping"
"""))


def test_agent_nested_runner_parses_into_runnerconfig(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
judges:
  - name: ow_judge
    prompt_file: eval/prompts/arch.md
    agent:
      runner:
        type: cli
        command: "bash run-judge.sh {workspace} {output_dir} {model}"
        effort: high
      context: [.context/architecture-context]
"""))
    jc = cfg.judges[0]
    runner = jc.agent["runner"]
    assert isinstance(runner, RunnerConfig)
    assert runner.type == "cli"
    assert runner.command == "bash run-judge.sh {workspace} {output_dir} {model}"
    assert runner.effort == "high"


def test_agent_nested_runner_type_defaults_to_claude_code(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
judges:
  - name: j
    prompt: grade
    agent:
      runner:
        effort: medium
"""))
    runner = cfg.judges[0].agent["runner"]
    assert isinstance(runner, RunnerConfig)
    assert runner.type == "claude-code"
    assert runner.effort == "medium"


def test_agent_nested_runner_non_dict_raises(tmp_path):
    with pytest.raises(ValueError, match="agent.runner' must be a mapping"):
        EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
judges:
  - name: bad
    prompt: grade
    agent:
      runner: "claude-code"
"""))


@pytest.mark.parametrize("key", ["allowed_tools", "denied_tools"])
@pytest.mark.parametrize("value", ["Bash", "[Bash, 1]"])
def test_agent_tool_list_must_be_list_of_strings(tmp_path, key, value):
    """A scalar would be iterated per character downstream (deny B,a,s,h)."""
    with pytest.raises(ValueError,
                       match=f"agent.{key}' must be a list of tool names"):
        EvalConfig.from_yaml(_write(tmp_path, f"""
name: t
execution:
  skill: s
judges:
  - name: bad
    prompt: grade
    agent:
      {key}: {value}
"""))


def _tool_list_config(tmp_path, key, value):
    return _write(tmp_path, f"""
name: t
execution:
  skill: s
judges:
  - name: j
    prompt: grade
    feedback_type: bool
    agent:
      {key}: {value}
""")


@pytest.mark.parametrize("key", ["allowed_tools", "denied_tools"])
def test_misspelled_tool_name_warns_with_the_right_spelling(tmp_path, key):
    """`denied_tools` replaces the default wholesale and a runner ignores a
    name it cannot map without a word, so the YAML-natural lowercase would
    load clean, forfeit the working default, and buy nothing in exchange."""
    with pytest.warns(UserWarning, match="did you mean 'Bash'"):
        EvalConfig.from_yaml(_tool_list_config(tmp_path, key, "[bash]"))


@pytest.mark.parametrize("key", ["allowed_tools", "denied_tools"])
def test_unknown_tool_name_warns_without_raising(tmp_path, key):
    """A warning, not an error: a CLI release adds tools faster than a harness
    release can list them, so an unrecognized name must stay usable."""
    with pytest.warns(UserWarning, match="is not a known tool name"):
        cfg = EvalConfig.from_yaml(
            _tool_list_config(tmp_path, key, "[BrandNewTool]"))
    assert cfg.judges[0].agent[key] == ["BrandNewTool"]


@pytest.mark.parametrize("value", [
    "[Read, Grep, Glob]",        # plain names
    "[Bash(pytest:*)]",          # scoped: the bare name is what's checked
    "[mcp__github__get_issue]",  # names a server, not a tool
])
def test_recognized_tool_rules_do_not_warn(tmp_path, value):
    import warnings
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        EvalConfig.from_yaml(
            _tool_list_config(tmp_path, "denied_tools", value))
    assert not [w for w in caught if "tool name" in str(w.message)]


def test_agent_nested_runner_invalid_command_raises(tmp_path):
    """The nested runner is validated by the SAME logic as the top-level
    runner (command must be str or list of str)."""
    with pytest.raises(ValueError, match="command must be a string or list"):
        EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
judges:
  - name: bad
    prompt: grade
    agent:
      runner:
        command: 123
"""))


def test_agent_raw_yaml_not_mutated(tmp_path):
    """Parsing the nested runner shallow-copies so the raw agent dict's runner
    is replaced on the JudgeConfig without leaving a half-parsed structure."""
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
judges:
  - name: j
    prompt: grade
    agent:
      runner:
        type: cli
        command: "x.sh"
      inputs: [a]
"""))
    jc = cfg.judges[0]
    # Other agent keys survive alongside the parsed runner.
    assert jc.agent["inputs"] == ["a"]
    assert isinstance(jc.agent["runner"], RunnerConfig)


# ---------------------------------------------------------------------------
# resolve_plugin_dir trust boundary (runtime path used by all runners)
# ---------------------------------------------------------------------------

from agent_eval.config import resolve_plugin_dir  # noqa: E402

_MINIMAL = "name: t\nexecution:\n  skill: s\n"


def _plugin_config(tmp_path, monkeypatch):
    project = tmp_path / "project"
    eval_dir = project / "eval"
    eval_dir.mkdir(parents=True)
    cfg = EvalConfig.from_yaml(_write(eval_dir, _MINIMAL))
    monkeypatch.chdir(project)
    return project, eval_dir, cfg


def test_resolve_plugin_dir_allows_declared_relative_external_path(
        tmp_path, monkeypatch):
    _project, _, cfg = _plugin_config(tmp_path, monkeypatch)
    (tmp_path / "outside-plugin").mkdir()
    assert resolve_plugin_dir(cfg, "../outside-plugin") == (
        tmp_path / "outside-plugin").resolve()


def test_resolve_plugin_dir_rejects_symlink_escape(tmp_path, monkeypatch):
    project, _, cfg = _plugin_config(tmp_path, monkeypatch)
    outside = tmp_path / "outside-plugin"
    outside.mkdir()
    (project / "plugin-link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="must not escape"):
        resolve_plugin_dir(cfg, "plugin-link")


def test_resolve_plugin_dir_prefers_project_root_candidate(tmp_path, monkeypatch):
    project, eval_dir, cfg = _plugin_config(tmp_path, monkeypatch)
    (project / "plugins").mkdir()
    (eval_dir / "plugins").mkdir()
    assert resolve_plugin_dir(cfg, "plugins") == (project / "plugins").resolve()


def test_resolve_plugin_dir_never_falls_back_to_config_dir(tmp_path, monkeypatch):
    _project, eval_dir, cfg = _plugin_config(tmp_path, monkeypatch)
    (eval_dir / "plugins").mkdir()
    with pytest.raises(FileNotFoundError, match="plugin directory not found"):
        resolve_plugin_dir(cfg, "plugins")


def test_resolve_plugin_dir_absolute_is_opt_in_but_must_exist(
        tmp_path, monkeypatch):
    _project, _, cfg = _plugin_config(tmp_path, monkeypatch)
    outside = tmp_path / "outside-plugin"
    outside.mkdir()
    assert resolve_plugin_dir(cfg, str(outside)) == outside.resolve()
    with pytest.raises(FileNotFoundError, match="plugin directory not found"):
        resolve_plugin_dir(cfg, str(tmp_path / "missing"))


def test_plugin_manifest_must_be_a_json_object(tmp_path):
    from agent_eval.config import resolve_plugin_skill_roots
    plugin = tmp_path / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text('["skills"]')
    with pytest.raises(ValueError, match="must be a JSON object"):
        resolve_plugin_skill_roots(plugin)


@pytest.mark.parametrize("entry", ["/etc", "../../outside", "escape-link"])
def test_plugin_manifest_skill_roots_cannot_escape_plugin_dir(tmp_path, entry):
    from agent_eval.config import resolve_plugin_skill_roots
    outside = tmp_path / "outside"
    (outside / "leak").mkdir(parents=True)
    (outside / "leak" / "SKILL.md").write_text("leak")
    plugin = tmp_path / "plugins" / "p"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / "escape-link").symlink_to(outside, target_is_directory=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"skills": [entry]}))
    with pytest.raises(ValueError, match="must stay beneath the plugin"):
        resolve_plugin_skill_roots(plugin)


def test_codex_rejects_unenforceable_tool_interception_and_repo_mode(tmp_path):
    with pytest.raises(ValueError, match=r"does not support inputs\.tools"):
        EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution: {skill: s}
runner: {type: codex}
inputs:
  tools:
    - {match: Bash, prompt: mock it}
"""))
    with pytest.raises(ValueError, match="answer-key protections"):
        EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution: {skill: s}
runner: {type: codex, workspace_mode: repo}
"""))


def test_fullsend_rejects_tool_interception(tmp_path):
    with pytest.raises(ValueError, match=r"does not support inputs\.tools"):
        EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution: {skill: s}
runner: {type: fullsend, agent: a}
inputs:
  tools:
    - {match: Bash, prompt: mock it}
"""))


def test_fullsend_rejects_repo_workspace_mode(tmp_path):
    with pytest.raises(ValueError, match="answer-key protections"):
        EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution: {skill: s}
runner: {type: fullsend, agent: a, workspace_mode: repo}
"""))


def test_per_judge_model_checked_only_on_judges_that_call_a_model(tmp_path):
    """A check judge never calls its `model:`, so a runner-backed one loads
    under fullsend; an LLM judge carrying the same model is still rejected."""
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution: {skill: s}
runner: {type: fullsend, agent: a}
judges:
  - {name: c, check: 'return True, ""', model: 'runner:/opus'}
"""))
    assert cfg.judges[0].model == "runner:/opus"
    with pytest.raises(ValueError, match=r"judge 'j' model:.*SDK-backed"):
        EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution: {skill: s}
runner: {type: fullsend, agent: a}
judges:
  - {name: j, prompt: rate it, model: 'runner:/opus'}
"""))


def test_discovery_skips_hidden_files_and_dirs(tmp_path):
    """Hidden entries under eval/ are working files, never configs — a
    git-ignored .entity-map.yaml surfacing as an eval config turns
    single-config auto-selection into a which-config prompt."""
    from agent_eval.config import discover_configs
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    (eval_dir / "my-skill.yaml").write_text("name: t\nexecution:\n  skill: s\n")
    (eval_dir / ".entity-map.yaml").write_text("SomeCorp: OtherCorp\n")
    hidden_dir = eval_dir / ".raw"
    hidden_dir.mkdir()
    (hidden_dir / "eval.yaml").write_text("name: h\nexecution:\n  skill: x\n")

    found = discover_configs(tmp_path)
    assert [c.path.name for c in found] == ["my-skill.yaml"]

def test_cursor_repo_mode_is_allowed(tmp_path):
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  prompt: "{{ input.prompt }}"
runner: {type: cursor, workspace_mode: repo}
"""))

    assert cfg.runner.type == "cursor"
    assert cfg.runner.workspace_mode == "repo"


# --- models.providers (spec 014) ---------------------------------------------

_OR_BASE = "name: t\nexecution:\n  skill: s\njudges:\n  - {name: j, prompt: rate it}\n"


def _or_yaml(tmp_path, models_body, extra=""):
    return _write(tmp_path, _OR_BASE + "models:\n" + models_body + extra)


def test_openrouter_judge_needs_no_providers_block(tmp_path):
    cfg = EvalConfig.from_yaml(_or_yaml(tmp_path, "  judge: openrouter:/z-ai/glm-5.2\n"))
    assert cfg.models.judge == "openrouter:/z-ai/glm-5.2"
    assert cfg.models.providers.openrouter is None


def test_models_block_without_providers_is_unchanged(tmp_path):
    cfg = EvalConfig.from_yaml(_or_yaml(tmp_path, "  judge: sonnet\n"))
    assert cfg.models.providers.openrouter is None
    assert cfg.judges[0].provider_options == {}


def test_openrouter_block_defaults(tmp_path):
    cfg = EvalConfig.from_yaml(_or_yaml(
        tmp_path, "  judge: openrouter:/z-ai/glm-5.2\n  providers:\n    openrouter: {}\n"))
    orc = cfg.models.providers.openrouter
    assert orc.kind == "openrouter"
    assert orc.api_key_env == "OPENROUTER_API_KEY"
    assert orc.base_url == "https://openrouter.ai/api"
    assert (orc.attribution.referer, orc.attribution.title, orc.attribution.run_id_header) == (
        None, "agent-eval-harness", False)
    assert orc.routing.is_empty
    assert (orc.judge.concurrency, orc.judge.max_retries, orc.judge.timeout_s,
            orc.judge.extra_body, orc.judge.inherit_pins) == (4, 3, 300.0, {}, False)


def test_openrouter_block_parses(tmp_path):
    cfg = EvalConfig.from_yaml(_or_yaml(tmp_path, """  judge: openrouter:/z-ai/glm-5.2
  providers:
    openrouter:
      kind: openrouter
      api_key_env: $MY_OR_KEY
      base_url: https://openrouter.ai/api/
      attribution: {referer: https://example.test, title: my-eval, run_id_header: true}
      routing:
        defaults: {allow_fallbacks: true, sort: throughput}
        models:
          z-ai/glm-5.2: {order: [Z.AI, novita], allow_fallbacks: false, quantizations: [fp8]}
      judge:
        concurrency: 2
        max_retries: 1
        timeout_s: 60
        inherit_pins: true
        extra_body: {reasoning: {effort: high}}
"""))
    orc = cfg.models.providers.openrouter
    assert orc.api_key_env == "MY_OR_KEY"
    assert orc.base_url == "https://openrouter.ai/api"
    assert (orc.attribution.referer, orc.attribution.title, orc.attribution.run_id_header) == (
        "https://example.test", "my-eval", True)
    spec = orc.routing.for_model("z-ai/glm-5.2:exacto")
    assert spec.order == ("z-ai", "novita") and spec.allow_fallbacks is False
    assert spec.quantizations == ("fp8",) and spec.sort == "throughput"
    assert (orc.judge.concurrency, orc.judge.max_retries, orc.judge.timeout_s,
            orc.judge.inherit_pins) == (2, 1, 60.0, True)
    assert orc.judge.extra_body == {"reasoning": {"effort": "high"}}


def test_openrouter_base_url_env_indirection(tmp_path, monkeypatch):
    monkeypatch.setenv("OR_BASE", "https://gw.example.test/api/")
    cfg = EvalConfig.from_yaml(_or_yaml(
        tmp_path, "  judge: openrouter:/z-ai/glm-5.2\n  providers:\n"
                  "    openrouter: {base_url: $OR_BASE}\n"))
    assert cfg.models.providers.openrouter.base_url == "https://gw.example.test/api"
    monkeypatch.delenv("OR_BASE")
    with pytest.raises(ValueError, match=r"base_url references \$OR_BASE, which is not set"):
        EvalConfig.from_yaml(_or_yaml(
            tmp_path, "  judge: openrouter:/z-ai/glm-5.2\n  providers:\n"
                      "    openrouter: {base_url: $OR_BASE}\n"))


@pytest.mark.parametrize("url, expected", [
    ("http://localhost:8080/api/", "http://localhost:8080/api"),
    ("http://127.0.0.1:4000", "http://127.0.0.1:4000"),
    ("http://[::1]:4000", "http://[::1]:4000"),
    ("https://gw.example.test/api", "https://gw.example.test/api"),
])
def test_openrouter_base_url_cleartext_only_on_loopback(tmp_path, url, expected):
    cfg = EvalConfig.from_yaml(_or_yaml(
        tmp_path, "  judge: openrouter:/z-ai/glm-5.2\n  providers:\n"
                  f"    openrouter: {{base_url: '{url}'}}\n"))
    assert cfg.models.providers.openrouter.base_url == expected


def test_top_level_providers_key_is_rejected(tmp_path):
    body = _OR_BASE + "providers:\n  openrouter: {}\n"
    with pytest.raises(ValueError, match=r"models\.providers.*Decision 17"):
        EvalConfig.from_yaml(_write(tmp_path, body))


@pytest.mark.parametrize("block, match", [
    ("    openrouter: {kind: openai}\n", r"kind must equal 'openrouter'"),
    ("    openrouter: {kind: openai-compatible}\n", "not implemented in this release"),
    ("    mygw: {kind: openai-compatible}\n", "not implemented in this release"),
    ("    together: {}\n", "unknown provider"),
    ("    openrouter: {api_key_env: 'sk-or-v1-0123456789abcdef'}\n", "must name an environment variable"),
    ("    openrouter: {api_key_env: ''}\n", "must name an environment variable"),
    ("    openrouter: {base_url: https://openrouter.ai/api/v1}\n", r"/v1"),
    ("    openrouter: {base_url: openrouter.ai}\n", r"http\(s\) URL"),
    ("    openrouter: {base_url: http://gw.example.test/api}\n", "must use https"),
    ("    openrouter: {base_url: http://10.0.0.5:8080}\n", "must use https"),
    ("    openrouter: {judge: {inherit_pins: true}}\n", "nothing to inherit"),
    ("    openrouter: {judge: {inherit_pins: false}}\n", "nothing to inherit"),
    ("    openrouter: {routing: {defaults: {}}, judge: {inherit_pins: 'yes'}}\n", "must be a boolean"),
    ("    openrouter: {judge: {concurrency: 0}}\n", ">= 1"),
    ("    openrouter: {judge: {max_retries: -1}}\n", ">= 0"),
    ("    openrouter: {judge: {timeout_s: 0}}\n", "> 0"),
    ("    openrouter: {judge: {extra_body: [1]}}\n", "must be a mapping"),
    ("    openrouter: {judge: {foo: 1}}\n", "unknown key"),
    ("    openrouter: {attribution: {foo: 1}}\n", "unknown key"),
    ("    openrouter: {attribution: {run_id_header: 'yes'}}\n", "boolean"),
    ("    openrouter: {routing: {defaults: {quantizations: [q4]}}}\n", "quantization"),
    ("    openrouter: {routing: {models: {glm: {}}}}\n", "<author>/<slug>"),
    ("    openrouter: {routing: {enforcement: proxy}}\n", "enforcement must be one of"),
    ("    openrouter: {routing: {policy: ignore}}\n", "policy must be one of"),
    ("    openrouter: {preflight: maybe}\n", "preflight must be one of"),
    ("    openrouter: {budget: {run_usd: 0}}\n", "run_usd must be a finite number > 0"),
    ("    openrouter: {budget: {dedicated_key: 'yes'}}\n", "dedicated_key must be a boolean"),
    ("    openrouter: {budget: {max_unpriced: 3}}\n", "Decision 1"),
    ("    openrouter: {cli_budget_inflation: 0.5}\n", "cli_budget_inflation must be a finite number >= 1"),
    ("    openrouter: {background_model: haiku}\n", "background_model must be an OpenRouter"),
    ("    openrouter: {background_model: 'z-ai/'}\n", "background_model must be an OpenRouter"),
    ("    openrouter: {background_model: '/glm'}\n", "background_model must be an OpenRouter"),
    ("    openrouter: {background_model: 'anthropic:/claude-haiku-4-5'}\n", "background_model must be an OpenRouter"),
    ("    openrouter: {budget: {run_usd: .nan}}\n", "run_usd must be a finite number"),
    ("    openrouter: {budget: {run_usd: .inf}}\n", "run_usd must be a finite number"),
    ("    openrouter: {cli_budget_inflation: .inf}\n", "cli_budget_inflation must be a finite number"),
    ("    openrouter: {routing: {guardrail: {settle_s: .nan}}}\n", "settle_s must be a finite number"),
    ("    openrouter: {judge: {timeout_s: .inf}}\n", "timeout_s must be a finite number"),
    ("    openrouter: {routing: {defaults: {max_price: {prompt: .inf}}}}\n", "finite non-negative"),
    ("    openrouter: {management_key_env: 'sk-or-v1-abc'}\n", "must name an environment variable"),
    ("    openrouter: {routing: {guardrail: {providers: []}}}\n", "non-empty list"),
    ("    openrouter: {routing: {guardrail: {providers: all}}}\n", "'pinned' or an explicit list"),
    ("    openrouter: {routing: {guardrail: {revoke_on_exit: false}}}\n", "not supported in this release"),
    ("    openrouter: {routing: {guardrail: {settle_s: -1}}}\n", "settle_s must be a finite number >= 0"),
    ("    openrouter: {routing: {guardrail: {foo: 1}}}\n", "unknown key"),
    ("    openrouter: {routing: {enforcement: key-guardrail}}\n", "run_usd must be set"),
    ("    openrouter: {budget: {run_usd: 5}, routing: {enforcement: key-guardrail}}\n",
     "explicit list or some routing key must carry pins"),
    ("    openrouter: {transport: proxy}\n", "Decision 1"),
    ("    openrouter: {direct: {}}\n", "Decision 1"),
    ("    openrouter: {foo: 1}\n", "unknown key"),
    ("    openrouter: 3\n", "must be a mapping"),
])
def test_openrouter_block_rejections(tmp_path, block, match):
    body = "  judge: openrouter:/z-ai/glm-5.2\n  providers:\n" + block
    with pytest.raises(ValueError, match=match):
        EvalConfig.from_yaml(_or_yaml(tmp_path, body))


def test_api_key_env_error_never_echoes_the_value(tmp_path):
    body = "  judge: openrouter:/z-ai/glm-5.2\n  providers:\n    openrouter: {api_key_env: 'sk-or-v1-SECRETVALUE'}\n"
    with pytest.raises(ValueError) as exc:
        EvalConfig.from_yaml(_or_yaml(tmp_path, body))
    assert "SECRETVALUE" not in str(exc.value)


def test_openrouter_judge_model_shape_checked_at_load(tmp_path):
    with pytest.raises(ValueError, match=r"models\.judge:.*<author>/<slug>"):
        EvalConfig.from_yaml(_or_yaml(tmp_path, "  judge: openrouter:/glm-5.2\n"))


# --- judges[].provider_options ----------------------------------------------

def test_provider_options_parsed_for_openrouter_judge(tmp_path):
    body = ("name: t\nexecution:\n  skill: s\njudges:\n"
            "  - name: j\n    prompt: rate it\n    model: openrouter:/z-ai/glm-5.2\n"
            "    provider_options:\n"
            "      routing: {order: [Z.AI], allow_fallbacks: false}\n"
            "      fallbacks: [deepseek/deepseek-v4]\n"
            "      max_tokens: 8192\n")
    cfg = EvalConfig.from_yaml(_write(tmp_path, body))
    assert cfg.judges[0].provider_options == {
        "routing": {"order": ["Z.AI"], "allow_fallbacks": False},
        "fallbacks": ["deepseek/deepseek-v4"], "max_tokens": 8192}


def test_provider_options_accepts_models_judge_as_the_static_model(tmp_path):
    body = ("name: t\nexecution:\n  skill: s\nmodels:\n  judge: openrouter:/z-ai/glm-5.2\n"
            "judges:\n  - {name: j, prompt: rate it, provider_options: {max_tokens: 4096}}\n")
    cfg = EvalConfig.from_yaml(_write(tmp_path, body))
    assert cfg.judges[0].provider_options == {"max_tokens": 4096}


@pytest.mark.parametrize("judge, match", [
    ("{name: j, prompt: rate it, model: sonnet, provider_options: {max_tokens: 1}}",
     r"requires an 'openrouter:/' judge model.*got 'sonnet'"),
    ("{name: j, prompt: rate it, model: 'openai:/gpt-4o', provider_options: {max_tokens: 1}}",
     r"requires an 'openrouter:/' judge model"),
    ("{name: j, prompt: rate it, provider_options: {max_tokens: 1}}",
     r"no static judge model is set"),
    ("{name: j, prompt: rate it, model: 'openrouter:/z-ai/glm-5.2', provider_options: {foo: 1}}",
     "unknown key"),
    ("{name: j, prompt: rate it, model: 'openrouter:/z-ai/glm-5.2', provider_options: {max_tokens: 0}}",
     ">= 1"),
    ("{name: j, prompt: rate it, model: 'openrouter:/z-ai/glm-5.2', provider_options: {routing: {order: []}}}",
     "non-empty list"),
    ("{name: j, prompt: rate it, model: 'openrouter:/z-ai/glm-5.2', provider_options: {fallbacks: [a/b, c/d, e/f, g/h]}}",
     "at most 3"),
    ("{name: j, prompt: rate it, model: 'openrouter:/z-ai/glm-5.2', provider_options: [1]}",
     "must be a mapping"),
])
def test_provider_options_rejections(tmp_path, judge, match):
    body = f"name: t\nexecution:\n  skill: s\njudges:\n  - {judge}\n"
    with pytest.raises(ValueError, match=match):
        EvalConfig.from_yaml(_write(tmp_path, body))


def test_empty_provider_options_on_any_judge_is_fine(tmp_path):
    body = ("name: t\nexecution:\n  skill: s\njudges:\n"
            "  - {name: j, prompt: rate it, model: sonnet, provider_options: {}}\n")
    assert EvalConfig.from_yaml(_write(tmp_path, body)).judges[0].provider_options == {}


def test_agent_judge_cannot_use_an_openrouter_model(tmp_path):
    body = ("name: t\nexecution:\n  skill: s\njudges:\n"
            "  - {name: j, prompt: rate it, model: 'openrouter:/z-ai/glm-5.2', "
            "agent: {allowed_tools: [Read]}}\n")
    with pytest.raises(ValueError, match=r"agent judges run through the runner"):
        EvalConfig.from_yaml(_write(tmp_path, body))
    # ... including when the model comes from models.judge
    body = ("name: t\nexecution:\n  skill: s\nmodels:\n  judge: openrouter:/z-ai/glm-5.2\n"
            "judges:\n  - {name: j, prompt: rate it, agent: {allowed_tools: [Read]}}\n")
    with pytest.raises(ValueError, match=r"agent judges run through the runner"):
        EvalConfig.from_yaml(_write(tmp_path, body))


def test_agent_judge_with_other_providers_still_loads(tmp_path):
    body = ("name: t\nexecution:\n  skill: s\njudges:\n"
            "  - {name: j, prompt: rate it, model: 'gemini:/x', agent: {allowed_tools: [Read]}}\n")
    assert EvalConfig.from_yaml(_write(tmp_path, body)).judges[0].model == "gemini:/x"


# --- extends: config overlay (spec 014 PR-3a) --------------------------------

import subprocess  # noqa: E402

from agent_eval.config import deep_merge, dump_with_provenance, load_raw  # noqa: E402

_BASE_YAML = """\
name: base
execution:
  skill: rfe.speedrun
  timeout: 100
dataset:
  path: eval/dataset/cases
permissions:
  allow: ["Skill", "Agent", "Edit(tmp/**)"]
models:
  judge: claude-opus-4-8
judges:
  - {name: a, prompt: rate a, score_range: [1, 5]}
  - {name: b, prompt: rate b, score_range: [1, 5]}
hooks:
  before_all:
    - {command: "echo base"}
"""

_PROFILE_YAML = """\
extends: ../eval.yaml
models:
  skill: openrouter:/z-ai/glm-5.2
execution:
  timeout: 36000
permissions:
  allow: ["Bash(python3 *)", "Skill"]
judges:
  - {name: a, model: "openrouter:/z-ai/glm-5.2"}
  - {name: c, prompt: rate c, score_range: [1, 5]}
hooks:
  before_all:
    - {command: "echo profile"}
    - {command: "echo base"}
"""


def _overlay_project(tmp_path, profile=_PROFILE_YAML, base=_BASE_YAML):
    (tmp_path / "eval.yaml").write_text(base)
    (tmp_path / "eval-profiles").mkdir()
    (tmp_path / "eval-profiles" / "glm.yaml").write_text(profile)
    return tmp_path / "eval-profiles" / "glm.yaml"


def test_load_raw_merges_overlay_over_base(tmp_path):
    profile = _overlay_project(tmp_path)
    raw, chain = load_raw(profile)
    assert [Path(c).name for c in chain] == ["eval.yaml", "glm.yaml"]
    assert "extends" not in raw
    # scalars override, untouched keys survive
    assert raw["execution"]["timeout"] == 36000
    assert raw["execution"]["skill"] == "rfe.speedrun"
    assert raw["models"] == {"judge": "claude-opus-4-8", "skill": "openrouter:/z-ai/glm-5.2"}
    # scalar lists extend with dedupe, base first
    assert raw["permissions"]["allow"] == ["Skill", "Agent", "Edit(tmp/**)", "Bash(python3 *)"]
    # judges merge by name: same key deep-merges, new keys append
    assert [j["name"] for j in raw["judges"]] == ["a", "b", "c"]
    assert raw["judges"][0] == {"name": "a", "prompt": "rate a", "score_range": [1, 5],
                                "model": "openrouter:/z-ai/glm-5.2"}
    # lists of unkeyed mappings extend by equality
    assert raw["hooks"]["before_all"] == [{"command": "echo base"}, {"command": "echo profile"}]


def test_steps_merge_by_id(tmp_path):
    (tmp_path / "eval.yaml").write_text(
        "name: t\nexecution:\n  steps:\n    - {id: create, prompt: create it}\n"
        "    - {id: assess, prompt: assess it, timeout: 10}\n")
    (tmp_path / "p.yaml").write_text(
        "extends: eval.yaml\nexecution:\n  steps:\n    - {id: assess, timeout: 99}\n"
        "    - {id: report, prompt: report it}\n")
    raw, _ = load_raw(tmp_path / "p.yaml")
    steps = raw["execution"]["steps"]
    assert [s["id"] for s in steps] == ["create", "assess", "report"]
    assert steps[1] == {"id": "assess", "prompt": "assess it", "timeout": 99}
    cfg = EvalConfig.from_yaml(tmp_path / "p.yaml")
    assert [s.id for s in cfg.execution.steps] == ["create", "assess", "report"]


def test_steps_with_both_id_and_name_merge_by_id(tmp_path):
    """`id` is a step's identity; a renamed step is the same step, and two
    steps that share a display name stay two steps."""
    (tmp_path / "eval.yaml").write_text(
        "name: t\nexecution:\n  steps:\n"
        "    - {id: create, name: Create, prompt: create it}\n"
        "    - {id: assess, name: Assess, prompt: assess it}\n")
    (tmp_path / "p.yaml").write_text(
        "extends: eval.yaml\nexecution:\n  steps:\n"
        "    - {id: assess, name: Assess deeply, timeout: 99}\n"
        "    - {id: recheck, name: Assess, prompt: assess again}\n")
    raw, _ = load_raw(tmp_path / "p.yaml")
    steps = raw["execution"]["steps"]
    assert [s["id"] for s in steps] == ["create", "assess", "recheck"]
    assert steps[1] == {"id": "assess", "name": "Assess deeply", "prompt": "assess it",
                        "timeout": 99}
    assert steps[2]["name"] == "Assess" and steps[2]["prompt"] == "assess again"
    cfg = EvalConfig.from_yaml(tmp_path / "p.yaml")
    assert [s.id for s in cfg.execution.steps] == ["create", "assess", "recheck"]


def test_from_yaml_takes_paths_and_name_from_the_root_of_the_chain(tmp_path, monkeypatch):
    profile = _overlay_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    cfg = EvalConfig.from_yaml(profile)
    assert cfg.config_chain == ["eval.yaml", "eval-profiles/glm.yaml"]
    assert cfg.config_path == (tmp_path / "eval.yaml").resolve()
    assert cfg.config_dir == tmp_path.resolve()
    assert cfg.resolve_path(cfg.dataset.path) == (tmp_path / "eval/dataset/cases").resolve()
    assert cfg.eval_name() == "rfe.speedrun"
    assert cfg.name == "base"
    assert cfg.models.skill == "openrouter:/z-ai/glm-5.2"
    assert [j.name for j in cfg.judges] == ["a", "b", "c"]
    assert cfg.judges[0].model == "openrouter:/z-ai/glm-5.2"


def test_plain_config_has_a_one_entry_chain(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = EvalConfig.from_yaml(_write(tmp_path, _BASE_YAML))
    assert cfg.config_chain == ["eval.yaml"]
    assert cfg.name == "base"


def test_profile_without_name_defaults_to_the_root_stem(tmp_path):
    profile = _overlay_project(tmp_path, base=_BASE_YAML.replace("name: base\n", ""))
    cfg = EvalConfig.from_yaml(profile)
    assert cfg.name == "eval"           # root stem, not "glm"
    assert cfg.eval_name() == "rfe.speedrun"


def test_replace_tag_replaces_a_list_outright(tmp_path):
    profile = _overlay_project(tmp_path, profile=(
        "extends: ../eval.yaml\n"
        "permissions:\n  allow: !replace [\"Bash(python3 *)\"]\n"
        "judges: !replace\n  - {name: only, prompt: p, score_range: [1, 5]}\n"))
    raw, _ = load_raw(profile)
    assert raw["permissions"]["allow"] == ["Bash(python3 *)"]
    assert [j["name"] for j in raw["judges"]] == ["only"]
    assert type(raw["judges"]) is list      # marker stripped


def test_replace_tag_on_a_mapping_is_rejected(tmp_path):
    profile = _overlay_project(tmp_path, profile="extends: ../eval.yaml\nmodels: !replace {judge: x}\n")
    with pytest.raises(Exception, match="list values only"):
        load_raw(profile)


def test_three_level_chain(tmp_path):
    _overlay_project(tmp_path)
    (tmp_path / "eval-profiles" / "glm-fast.yaml").write_text(
        "extends: glm.yaml\nexecution:\n  timeout: 5\npermissions:\n  allow: [Read]\n")
    raw, chain = load_raw(tmp_path / "eval-profiles" / "glm-fast.yaml")
    assert [Path(c).name for c in chain] == ["eval.yaml", "glm.yaml", "glm-fast.yaml"]
    assert raw["execution"]["timeout"] == 5
    assert raw["permissions"]["allow"][-2:] == ["Bash(python3 *)", "Read"]
    assert [j["name"] for j in raw["judges"]] == ["a", "b", "c"]


@pytest.mark.parametrize("profile, match", [
    ("extends: ../missing.yaml\n", "does not exist"),
    ("extends: 3\n", "path string"),
    ("extends: ''\n", "path string"),
    ("extends: /etc/eval.yaml\n", "must be relative"),
])
def test_extends_rejections(tmp_path, profile, match):
    path = _overlay_project(tmp_path, profile=profile)
    with pytest.raises((ValueError, FileNotFoundError), match=match):
        load_raw(path)


def test_extends_cycle_is_detected(tmp_path):
    (tmp_path / "a.yaml").write_text("extends: b.yaml\nname: a\n")
    (tmp_path / "b.yaml").write_text("extends: a.yaml\nname: b\n")
    with pytest.raises(ValueError, match="cycle"):
        load_raw(tmp_path / "a.yaml")


def test_extends_depth_limit(tmp_path):
    (tmp_path / "c0.yaml").write_text("name: root\nexecution:\n  skill: s\n")
    for i in range(1, 11):
        (tmp_path / f"c{i}.yaml").write_text(f"extends: c{i - 1}.yaml\n")
    with pytest.raises(ValueError, match="deeper than 8"):
        load_raw(tmp_path / "c10.yaml")
    raw, chain = load_raw(tmp_path / "c8.yaml")
    assert len(chain) == 9 and raw["name"] == "root"


def test_deep_merge_policies():
    base = {"a": [1, 2], "d": {"x": 1, "l": ["p"]}, "s": "old",
            "j": [{"name": "k", "v": 1}], "h": [{"cmd": "x"}]}
    over = {"a": [2, 3], "d": {"y": 2, "l": ["p", "q"]}, "s": "new",
            "j": [{"name": "k", "w": 2}, {"name": "n"}], "h": [{"cmd": "x"}, {"cmd": "y"}]}
    plain = deep_merge(copy.deepcopy(base), copy.deepcopy(over))
    assert plain["a"] == [1, 2, 2, 3]                       # runner.settings: plain extend
    assert plain["j"] == [{"name": "k", "v": 1}, {"name": "k", "w": 2}, {"name": "n"}]
    deduped = deep_merge(copy.deepcopy(base), copy.deepcopy(over), dedupe=True)
    assert deduped["a"] == [1, 2, 3]
    assert deduped["d"] == {"x": 1, "y": 2, "l": ["p", "q"]}
    assert deduped["s"] == "new"
    assert deduped["j"] == [{"name": "k", "v": 1, "w": 2}, {"name": "n"}]
    assert deduped["h"] == [{"cmd": "x"}, {"cmd": "y"}]


def test_dump_with_provenance_annotates_and_round_trips(tmp_path, monkeypatch):
    profile = _overlay_project(tmp_path, profile=_PROFILE_YAML.replace(
        "hooks:\n  before_all:\n", "hooks:\n  before_all: !replace\n"))
    monkeypatch.chdir(tmp_path)
    text = dump_with_provenance(profile)
    assert "# chain (root first): eval.yaml <- eval-profiles/glm.yaml" in text
    assert "- Skill  # from: eval.yaml, eval-profiles/glm.yaml" in text
    assert "- Bash(python3 *)  # from: eval-profiles/glm.yaml" in text
    assert "before_all:  # !replace from: eval-profiles/glm.yaml" in text
    import yaml as _yaml
    assert _yaml.safe_load(text) == load_raw(profile)[0]


def test_config_module_print_entry_point(tmp_path):
    profile = _overlay_project(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "agent_eval.config", "--print", str(profile)],
        cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    import yaml as _yaml
    assert _yaml.safe_load(proc.stdout)["execution"]["timeout"] == 36000
    bad = subprocess.run(
        [sys.executable, "-m", "agent_eval.config", "--print", str(tmp_path / "nope.yaml")],
        cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True)
    assert bad.returncode == 1 and "ERROR" in bad.stderr


def test_eval_params_record_the_config_chain(tmp_path, monkeypatch):
    """execute.py's run_result.eval_params carries the chain that defined the run."""
    import importlib.util
    from types import SimpleNamespace

    execute_path = Path(__file__).resolve().parent.parent / "skills" / "eval-run" / "scripts" / "execute.py"
    sys.path.insert(0, str(execute_path.parent))
    spec = importlib.util.spec_from_file_location("_execute_under_test", execute_path)
    execute = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(execute)

    profile = _overlay_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = EvalConfig.from_yaml(profile)
    args = SimpleNamespace(skill=None, mlflow_experiment=None)
    params = execute._build_eval_params(args, config, "", 1.0, 10)
    assert params["config_chain"] == ["eval.yaml", "eval-profiles/glm.yaml"]
    plain = EvalConfig.from_yaml(_write(tmp_path, _BASE_YAML))
    assert execute._build_eval_params(args, plain, "", 1.0, 10)["config_chain"] == ["eval.yaml"]


# --- models.providers.openrouter: full block (spec 014 PR-3b) ----------------

import warnings as _warnings  # noqa: E402

_OR_AGENT = """  skill: openrouter:/z-ai/glm-5.2:exacto
  providers:
    openrouter:
      routing:
        models:
          z-ai/glm-5.2: {order: [z-ai, novita], allow_fallbacks: false}
"""


def _load_with_warnings(path):
    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        cfg = EvalConfig.from_yaml(path)
    return cfg, [str(w.message) for w in caught if issubclass(w.category, UserWarning)]


def test_openrouter_later_keys_defaults(tmp_path):
    cfg = EvalConfig.from_yaml(_or_yaml(
        tmp_path, "  judge: openrouter:/z-ai/glm-5.2\n  providers:\n    openrouter: {}\n"))
    orc = cfg.models.providers.openrouter
    assert orc.management_key_env == "OPENROUTER_MANAGEMENT_KEY"
    assert orc.background_model is None
    assert orc.preflight == "strict"
    assert orc.cli_budget_inflation == 50
    assert (orc.budget.run_usd, orc.budget.dedicated_key) == (None, False)
    assert (orc.routing.policy, orc.routing.enforcement) == ("strict", "audit")
    g = orc.routing.guardrail
    assert (g.key_name, g.providers, g.revoke_on_exit, g.settle_s) == (
        "agent-eval {run_id}", "pinned", True, 20.0)


def test_openrouter_full_block_parses_and_warns_where_documented(tmp_path):
    path = _or_yaml(tmp_path, """  skill: openrouter:/z-ai/glm-5.2:exacto
  providers:
    openrouter:
      management_key_env: $MY_MGMT
      background_model: qwen/qwen3-8b
      preflight: warn
      cli_budget_inflation: 1
      budget: {run_usd: 12.5, dedicated_key: true}
      routing:
        defaults: {allow_fallbacks: true, sort: throughput}
        models:
          z-ai/glm-5.2: {order: [Z.AI, novita], allow_fallbacks: false, quantizations: [fp8]}
        policy: warn
        enforcement: key-guardrail
        guardrail: {key_name: "eval {run_id}", providers: [Z.AI, novita], settle_s: 5}
""")
    cfg, warned = _load_with_warnings(path)
    orc = cfg.models.providers.openrouter
    assert orc.management_key_env == "MY_MGMT"
    assert orc.background_model == "qwen/qwen3-8b"
    assert (orc.preflight, orc.cli_budget_inflation) == ("warn", 1)
    assert (orc.budget.run_usd, orc.budget.dedicated_key) == (12.5, True)
    assert (orc.routing.policy, orc.routing.enforcement) == ("warn", "key-guardrail")
    assert orc.routing.guardrail.providers == ("z-ai", "novita")
    assert orc.routing.guardrail.key_name == "eval {run_id}"
    assert orc.routing.pinned_keys() == ["z-ai/glm-5.2"]
    joined = "\n".join(warned)
    assert "cli_budget_inflation is 1" in joined
    assert "settle_s is 5" in joined
    assert "'Z.AI' is a display name" in joined
    assert "sort not sendable from Claude Code" in joined     # agent-path routing intent


def test_reserved_keys_are_listed_together_with_the_decision_pointer(tmp_path):
    with pytest.raises(ValueError) as exc:
        EvalConfig.from_yaml(_or_yaml(
            tmp_path, "  judge: openrouter:/z-ai/glm-5.2\n  providers:\n"
                      "    openrouter: {transport: proxy, direct: {}, key_exposure_ack: true}\n"))
    message = str(exc.value)
    for key in ("transport", "direct", "key_exposure_ack"):
        assert f"models.providers.openrouter.{key}" in message
    assert "Decision 1" in message


def test_key_guardrail_accepted_with_run_usd_and_pins(tmp_path):
    cfg = EvalConfig.from_yaml(_or_yaml(tmp_path, """  judge: openrouter:/z-ai/glm-5.2
  providers:
    openrouter:
      budget: {run_usd: 5}
      routing:
        models:
          z-ai/glm-5.2: {order: [z-ai]}
        enforcement: key-guardrail
"""))
    assert cfg.models.providers.openrouter.routing.enforcement == "key-guardrail"


# -- agent roles under a plan --------------------------------------------------

def test_agent_plan_config_loads_and_declares_the_chain_of_roles(tmp_path):
    cfg, warned = _load_with_warnings(_or_yaml(tmp_path, _OR_AGENT))
    assert cfg.models.skill == "openrouter:/z-ai/glm-5.2:exacto"
    # A clean plan config raises no provider warning (the judge's score_range
    # advisory is unrelated).
    assert not [w for w in warned if "models.providers" in w or "ANTHROPIC" in w]


def test_inert_block_with_anthropic_roles_loads(tmp_path):
    body = _OR_AGENT.replace("skill: openrouter:/z-ai/glm-5.2:exacto", "skill: sonnet\n  subagent: haiku")
    cfg = EvalConfig.from_yaml(_or_yaml(tmp_path, body))
    assert cfg.models.providers.openrouter is not None and cfg.models.skill == "sonnet"


@pytest.mark.parametrize("models, match", [
    ("  skill: openrouter:/\n", r"models\.skill: model id missing"),
    ("  skill: openrouter:/glm-5.2\n", r"models\.skill: openrouter model needs"),
    ("  skill: gemini:/x\n", r"models\.skill: Unsupported agent model provider 'gemini'"),
    ("  skill: openrouter:/z-ai/glm-5.2\n  subagent: sonnet\n", "agent roles must share the plan's provider kind"),
    ("  skill: openrouter:/z-ai/glm-5.2\n  hook: anthropic:/claude-haiku-4-5\n", r"models\.hook: 'anthropic:/claude-haiku-4-5'"),
    ("  skill: z-ai/glm-5.2\n  providers:\n    openrouter:\n      routing:\n        models:\n          z-ai/glm-5.2: {order: [z-ai]}\n",
     "bare model id next to models.providers.openrouter"),
    ("  skill: gpt-5.2\n  providers:\n    openrouter: {}\n", "bare model id next to models.providers.openrouter"),
])
def test_agent_role_rejections(tmp_path, models, match):
    with pytest.raises(ValueError, match=match):
        EvalConfig.from_yaml(_or_yaml(tmp_path, models))


def test_bare_anthropic_roles_next_to_an_unmatched_block_are_fine(tmp_path):
    cfg = EvalConfig.from_yaml(_or_yaml(
        tmp_path, "  skill: claude-opus-4-8\n  providers:\n    openrouter:\n"
                  "      routing:\n        models:\n          z-ai/glm-5.2: {order: [z-ai]}\n"))
    assert cfg.models.skill == "claude-opus-4-8"


@pytest.mark.parametrize("runner, match", [
    ("runner:\n  type: cursor\n", "cursor has no base-URL knob"),
    ("runner:\n  type: codex\n", "implemented for 'claude-code'"),
])
def test_agent_plan_requires_the_claude_code_runner(tmp_path, runner, match):
    body = "name: t\nexecution:\n  skill: s\n" + runner + "models:\n" + _OR_AGENT + \
           "judges:\n  - {name: j, prompt: rate it}\n"
    with pytest.raises(ValueError, match=match):
        EvalConfig.from_yaml(_write(tmp_path, body))


def _plan_body(exec_env="", runner_block="", steps=None):
    execution = "execution:\n"
    if steps is None:
        execution += "  skill: s\n"
    else:
        execution += "  steps:\n" + steps
    if exec_env:
        execution += "  env:\n" + exec_env
    return ("name: t\n" + execution + runner_block + "models:\n" + _OR_AGENT
            + "judges:\n  - {name: j, prompt: rate it}\n")


def test_managed_dynamic_keys_are_rejected_on_presence(tmp_path):
    body = _plan_body(exec_env="    ANTHROPIC_BASE_URL: https://api.anthropic.com\n",
                      runner_block="runner:\n  type: claude-code\n  env:\n"
                                   "    ANTHROPIC_AUTH_TOKEN: $ANTHROPIC_AUTH_TOKEN\n"
                                   "  settings:\n    env:\n      ANTHROPIC_CUSTOM_HEADERS: 'x: y'\n")
    with pytest.raises(ValueError) as exc:
        EvalConfig.from_yaml(_write(tmp_path, body))
    message = str(exc.value)
    assert message.startswith("remove ")
    for surface in ("execution.env.ANTHROPIC_BASE_URL", "runner.env.ANTHROPIC_AUTH_TOKEN",
                    "runner.settings.env.ANTHROPIC_CUSTOM_HEADERS"):
        assert surface in message
    assert "enforcement=audit" in message


def test_managed_static_keys_load_when_identical_and_fail_when_different(tmp_path):
    ok = _plan_body(exec_env="    CLAUDE_CODE_USE_VERTEX: ''\n    ANTHROPIC_VERTEX_PROJECT_ID: ''\n"
                             "    ANTHROPIC_DEFAULT_OPUS_MODEL: z-ai/glm-5.2:exacto\n")
    cfg = EvalConfig.from_yaml(_write(tmp_path, ok))
    assert cfg.execution.env["CLAUDE_CODE_USE_VERTEX"] == ""
    bad = _plan_body(exec_env="    CLAUDE_CODE_USE_VERTEX: '1'\n    ANTHROPIC_DEFAULT_OPUS_MODEL: opus\n")
    with pytest.raises(ValueError, match=r"remove execution\.env\.ANTHROPIC_DEFAULT_OPUS_MODEL, execution\.env\.CLAUDE_CODE_USE_VERTEX"):
        EvalConfig.from_yaml(_write(tmp_path, bad))


def test_managed_keys_are_checked_on_step_surfaces(tmp_path):
    steps = ("    - id: a\n      prompt: p\n      env: {ANTHROPIC_BASE_URL: x}\n"
             "    - id: b\n      prompt: q\n      runner:\n        type: claude-code\n"
             "        settings: {env: {ANTHROPIC_AUTH_TOKEN: t}}\n")
    with pytest.raises(ValueError) as exc:
        EvalConfig.from_yaml(_write(tmp_path, _plan_body(steps=steps)))
    message = str(exc.value)
    assert "execution.steps[0].env.ANTHROPIC_BASE_URL" in message
    assert "execution.steps[1].runner.settings.env.ANTHROPIC_AUTH_TOKEN" in message


def test_non_empty_anthropic_api_key_under_a_plan_warns(tmp_path):
    cfg, warned = _load_with_warnings(_write(
        tmp_path, _plan_body(exec_env="    ANTHROPIC_API_KEY: sk-ant-stale\n")))
    assert cfg.models.skill.startswith("openrouter:/")
    assert any("ANTHROPIC_API_KEY is non-empty" in w for w in warned)
    _, quiet = _load_with_warnings(_write(
        tmp_path, _plan_body(exec_env="    ANTHROPIC_API_KEY: ''\n")))
    assert not any("ANTHROPIC_API_KEY" in w for w in quiet)


@pytest.mark.parametrize("models, env", [
    ("  skill: sonnet\n", "    OPENROUTER_API_KEY: $OPENROUTER_API_KEY\n"),   # no plan at all
    (_OR_AGENT, "    MY_KEY: $OPENROUTER_MANAGEMENT_KEY\n"),
    ("  skill: sonnet\n  providers:\n    openrouter: {api_key_env: OR_KEY}\n", "    OR_KEY: literal\n"),
    # braced references resolve at runtime, so they are rejected like $NAME
    ("  skill: sonnet\n", "    MY_KEY: ${OPENROUTER_API_KEY}\n"),
    (_OR_AGENT, "    MY_KEY: ${OPENROUTER_MANAGEMENT_KEY}\n"),
    ("  skill: sonnet\n  providers:\n    openrouter: {api_key_env: OR_KEY}\n", "    MY_KEY: '${OR_KEY}'\n"),
    ("  skill: sonnet\n  providers:\n    openrouter: {management_key_env: OR_MGMT}\n", "    MY_KEY: '${OR_MGMT}'\n"),
])
def test_openrouter_keys_are_env_only_on_every_surface(tmp_path, models, env):
    body = ("name: t\nexecution:\n  skill: s\n  env:\n" + env + "models:\n" + models
            + "judges:\n  - {name: j, prompt: rate it}\n")
    with pytest.raises(ValueError, match=r"remove execution\.env\.\w+; owned by models\.providers\.openrouter"):
        EvalConfig.from_yaml(_write(tmp_path, body))


def test_quantizations_without_pins_warn_for_agent_roles(tmp_path):
    body = _OR_AGENT.replace("{order: [z-ai, novita], allow_fallbacks: false}", "{quantizations: [fp8]}")
    _, warned = _load_with_warnings(_or_yaml(tmp_path, body))
    assert any("quantizations without order/only" in w for w in warned)


def test_openrouter_keys_are_env_only_on_agent_judge_runner_surfaces(tmp_path):
    """`judges[].agent.runner.env` / `.settings.env` reach a subprocess too:
    the env-only rule covers them (the managed-key rule does not — agent
    judges do not run under the plan)."""
    body = ("name: t\nexecution:\n  skill: s\nmodels:\n  skill: sonnet\n"
            "judges:\n  - name: j\n    prompt: rate it\n    model: sonnet\n"
            "    agent:\n      allowed_tools: [Read]\n      runner:\n        type: claude-code\n"
            "        env: {OR_MGMT: $OPENROUTER_MANAGEMENT_KEY}\n"
            "        settings: {env: {OPENROUTER_API_KEY: literal}}\n")
    with pytest.raises(ValueError) as exc:
        EvalConfig.from_yaml(_write(tmp_path, body))
    message = str(exc.value)
    assert "judges[j].agent.runner.env.OR_MGMT" in message
    assert "judges[j].agent.runner.settings.env.OPENROUTER_API_KEY" in message
    assert "literal" not in message.split("owned by")[0].replace("OPENROUTER_API_KEY", "")


def test_agent_judge_runner_env_is_not_subject_to_managed_key_ownership(tmp_path):
    body = ("name: t\nexecution:\n  skill: s\nmodels:\n" + _OR_AGENT
            + "judges:\n  - name: j\n    prompt: rate it\n    model: sonnet\n"
            "    agent:\n      allowed_tools: [Read]\n      runner:\n        type: claude-code\n"
            "        env: {ANTHROPIC_BASE_URL: https://api.anthropic.com}\n")
    cfg = EvalConfig.from_yaml(_write(tmp_path, body))
    assert cfg.judges[0].agent["runner"].env["ANTHROPIC_BASE_URL"] == "https://api.anthropic.com"


@pytest.mark.parametrize("field", ["referer", "title"])
def test_attribution_values_must_be_single_line(tmp_path, field):
    body = ("  judge: openrouter:/z-ai/glm-5.2\n  providers:\n    openrouter:\n"
            f"      attribution: {{{field}: \"x\\nAuthorization: Bearer y\"}}\n")
    with pytest.raises(ValueError, match=f"attribution.{field} must be a single line"):
        EvalConfig.from_yaml(_or_yaml(tmp_path, body))


def test_background_model_accepts_variants_and_a_uri_form(tmp_path):
    for written, stored in (("qwen/qwen3-8b:nitro", "qwen/qwen3-8b:nitro"),
                            ("openrouter:/qwen/qwen3-8b", "qwen/qwen3-8b")):
        cfg = EvalConfig.from_yaml(_or_yaml(
            tmp_path, "  judge: openrouter:/z-ai/glm-5.2\n  providers:\n"
                      f"    openrouter: {{background_model: '{written}'}}\n"))
        assert cfg.models.providers.openrouter.background_model == stored


@pytest.mark.parametrize("key", ["allowed_tools", "denied_tools"])
def test_path_scoped_write_rule_warns(tmp_path, key):
    """`Write(<path>)` matches nothing on the Claude Code runner, as an allow
    or as a deny — the Write tool is governed by `Edit(<path>)`. An author who
    writes a scoped Write deny believes they confined the judge and did not,
    and nothing below this layer says a word."""
    with pytest.warns(UserWarning, match="governed by 'Edit"):
        EvalConfig.from_yaml(
            _tool_list_config(tmp_path, key, "['Write(/etc/**)']"))


@pytest.mark.parametrize("key", ["allowed_tools", "denied_tools"])
def test_scoped_edit_never_draws_the_dead_rule_warning(tmp_path, key):
    """Only the parenthesised `Write(...)` form is dead. `Edit(<path>)` is the
    recommended replacement and must never be flagged as one — in either list.

    (A BARE `Write` is a different matter: silent in a deny list, but widening
    in an allow list — see `test_bare_write_tool_in_an_allow_list_warns`.)"""
    import warnings
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        EvalConfig.from_yaml(
            _tool_list_config(tmp_path, key, "['Edit(output/**)']"))
    assert not [w for w in caught if "matches nothing" in str(w.message)]


@pytest.mark.parametrize("rule", ["Read", "Grep", "Glob"])
def test_bare_widening_tool_in_an_allow_list_warns(tmp_path, rule):
    """A bare `Read`/`Grep`/`Glob` ALLOW removes the CLI's working-directory
    confinement for that tool. Measured on claude 2.1.300: with
    `[Read(./**), Grep, Glob, Edit(./output/**)]` a read outside the workspace
    is denied 3/3 while a Grep of the same file returns its CONTENT 3/3, with
    `permission_denials` empty. The author believes the scoped neighbours
    confine the judge; one bare word opts out (CWE-1188)."""
    with pytest.warns(UserWarning, match="removes the Claude Code working-dir"):
        EvalConfig.from_yaml(
            _tool_list_config(tmp_path, "allowed_tools", f"[{rule}]"))


@pytest.mark.parametrize("rule", ["Read", "Grep", "Glob"])
def test_bare_widening_tool_in_a_deny_list_does_not_warn(tmp_path, rule):
    """A bare `Grep` DENY is the normal way to remove the tool; it widens
    nothing. The warning is allow-list-only."""
    import warnings
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        EvalConfig.from_yaml(
            _tool_list_config(tmp_path, "denied_tools", f"[{rule}]"))
    assert not [w for w in caught if "working-dir" in str(w.message)]


def test_scoped_widening_tool_does_not_warn(tmp_path):
    """`Read(./**)` is the fix the warning asks for, so it must stay quiet —
    a warning that fires on the corrected config is pure noise."""
    import warnings
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        EvalConfig.from_yaml(_tool_list_config(
            tmp_path, "allowed_tools", "['Read(./**)', 'Grep(./**)']"))
    assert not [w for w in caught if "working-dir" in str(w.message)]


@pytest.mark.parametrize("rule", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
def test_bare_write_tool_in_an_allow_list_warns(tmp_path, rule):
    """The mirror of the bare-read case, and the shape every agent judge had
    before the default became `Edit(output/**)`: under deny-by-default a bare
    write allow grants writes to any host path. Scope, do not drop — the
    verdict file needs a write rule."""
    with pytest.warns(UserWarning, match="write to any path on the host"):
        EvalConfig.from_yaml(
            _tool_list_config(tmp_path, "allowed_tools", f"[{rule}]"))


@pytest.mark.parametrize("rule", ["Write", "Edit"])
def test_bare_write_tool_in_a_deny_list_does_not_warn(tmp_path, rule):
    """A bare write DENY removes the tool; it widens nothing."""
    import warnings
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        EvalConfig.from_yaml(_tool_list_config(tmp_path, "denied_tools", f"[{rule}]"))
    assert not [w for w in caught if "any path on the host" in str(w.message)]


def _no_write_rule_warnings(caught):
    return [w for w in caught if "has no write rule" in str(w.message)]


@pytest.mark.parametrize("rules", ["[Read, Grep, Glob]", "['Read(**)']",
                                   "['Write(output/**)']"])
def test_allow_list_without_a_write_rule_warns(tmp_path, rules):
    """Under the forced deny-by-default mode the verdict file needs a write
    rule. A written list without one degrades the judge to the stdout fallback
    (2-3x the cost); warn and name `Edit(output/**)`, never rewrite the list.
    A path-scoped `Write(...)` is a dead rule and does not count."""
    with pytest.warns(UserWarning, match=r"no write rule.*Edit\(output/\*\*\)"):
        EvalConfig.from_yaml(
            _tool_list_config(tmp_path, "allowed_tools", rules))


@pytest.mark.parametrize("rules", ["['Read(**)', 'Edit(output/**)']",
                                   "[Read(**), Edit]", "[]"])
def test_allow_list_with_a_write_rule_or_empty_does_not_warn(tmp_path, rules):
    import warnings
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        EvalConfig.from_yaml(
            _tool_list_config(tmp_path, "allowed_tools", rules))
    assert not _no_write_rule_warnings(caught)


def test_no_write_rule_warning_is_not_raised_for_a_deny_list(tmp_path):
    import warnings
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        EvalConfig.from_yaml(
            _tool_list_config(tmp_path, "denied_tools", "[Bash]"))
    assert not _no_write_rule_warnings(caught)


@pytest.mark.parametrize("runner_type,warns", [("claude-code", True),
                                               ("cursor", False),
                                               ("codex", False)])
def test_no_write_rule_warning_is_scoped_to_claude_code_runner(
        tmp_path, runner_type, warns):
    """The forced deny-by-default mode is claude-code-only."""
    import warnings
    path = _write(tmp_path, f"""
name: t
execution:
  skill: s
judges:
  - name: j
    prompt: grade
    feedback_type: bool
    agent:
      runner: {{type: {runner_type}}}
      allowed_tools: ['Read(**)']
""")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        EvalConfig.from_yaml(path)
    assert bool(_no_write_rule_warnings(caught)) is warns


def test_judge_verdict_policy_defaults_validates_and_overrides(tmp_path):
    """Spec 015: `models.judge_verdict` and the per-judge `verdict`."""
    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
models:
  judge: claude-opus-5-5
judges:
  - name: a
    prompt: rate it
  - name: b
    prompt: rate it
    verdict: tool
"""))
    assert cfg.models.judge_verdict == "auto"
    assert [j.verdict for j in cfg.judges] == ["", "tool"]

    cfg = EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
models:
  judge: claude-opus-5-5
  judge_verdict: json_schema
"""))
    assert cfg.models.judge_verdict == "json_schema"

    with pytest.raises(ValueError, match="models.judge_verdict must be one of auto, json_schema, tool"):
        EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
models:
  judge_verdict: forced
"""))
    with pytest.raises(ValueError, match="Judge 'a': 'verdict' must be one of"):
        EvalConfig.from_yaml(_write(tmp_path, """
name: t
execution:
  skill: s
judges:
  - name: a
    prompt: rate it
    verdict: structured
"""))


class TestSharedHelpers:
    def test_resolve_env_value_forms(self, monkeypatch):
        from agent_eval.config import resolve_env_value
        monkeypatch.setenv("SHARED_VAR", "v")
        monkeypatch.delenv("UNSET_VAR_XYZ", raising=False)
        assert resolve_env_value("$SHARED_VAR") == "v"
        assert resolve_env_value("${SHARED_VAR}") == "v"
        assert resolve_env_value("$UNSET_VAR_XYZ") is None
        assert resolve_env_value("${UNSET_VAR_XYZ}") is None
        assert resolve_env_value("literal") == "literal"
        assert resolve_env_value(3) == "3"
        assert resolve_env_value(None) is None

    def test_env_ref_name_recognises_both_reference_forms(self):
        from agent_eval.config import env_ref_name
        assert env_ref_name("$NAME") == "NAME"
        assert env_ref_name("${NAME}") == "NAME"
        for not_a_ref in ("NAME", "$NAME suffix", "${NAME", "$", "${}", 3, None):
            assert env_ref_name(not_a_ref) is None

    def test_terminate_process_group_sigterm_then_reaps(self):
        from agent_eval.agent.base import terminate_process_group
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True)
        terminate_process_group(proc, grace_s=5)
        assert proc.poll() is not None

    def test_terminate_process_group_kills_sigterm_ignoring_descendant(self):
        from agent_eval.agent.base import terminate_process_group
        # The leader exits on SIGTERM; its child ignores it and holds the pipes.
        code = ("import subprocess, sys, time\n"
                "subprocess.Popen([sys.executable, '-c', "
                "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(60)'])\n"
                "time.sleep(60)\n")
        proc = subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True)
        start = time.monotonic()
        terminate_process_group(proc, grace_s=1)
        assert time.monotonic() - start < 10

    def test_terminate_process_group_without_capture_leaves_the_pipes_to_the_caller(self):
        from agent_eval.agent.base import terminate_process_group
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time; print('held'); sys.stdout.flush(); time.sleep(60)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True)
        assert proc.stdout.readline() == "held\n"
        assert terminate_process_group(proc, grace_s=1, capture_output=False) == ("", "")
        assert proc.poll() is not None
        # Not communicate()d: the caller's readers still own the open pipes.
        assert not proc.stdout.closed and not proc.stderr.closed
        assert proc.stdout.read() == ""
        proc.stdout.close()
        proc.stderr.close()

    def test_failed_link_copy_leaves_the_link(self, tmp_path, monkeypatch):
        from agent_eval.agent import fullsend as fullsend_mod
        from agent_eval.agent.fullsend import materialize_symlinks
        src = tmp_path / "src"
        src.write_text("data")
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "link").symlink_to(src)
        monkeypatch.setattr(fullsend_mod.shutil, "copy2",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
        with pytest.raises(OSError):
            materialize_symlinks(ws)
        assert (ws / "link").is_symlink()
        assert sorted(p.name for p in ws.iterdir()) == ["link"]
