"""Tests for the fullsend runner (``runner.type: fullsend``).

Every execution test drives the runner against a fake ``fullsend`` script on
PATH (no OpenShell gateway in CI). The fake records its argv, its environment
and the state of the target repository (including the files an upload of it
would carry), then writes a run directory shaped like a real ``fullsend run``:
``agent-result.json`` under ``iteration-N/output/`` and, unless
``--no-post-script`` is passed, the artifacts copied into the target
repository by the project's post-script.
"""

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.agent import RUNNERS
from agent_eval.agent import fullsend as fullsend_mod
from agent_eval.agent.base import EvalRunner
from agent_eval.agent.fullsend import FullsendRunner, materialize_symlinks
from agent_eval.config import EvalConfig

SCRIPTS = Path(__file__).parent.parent / "skills" / "eval-run" / "scripts"

FAKE_FULLSEND = textwrap.dedent('''\
    import json, os, sys, time
    from pathlib import Path

    argv = sys.argv[1:]

    # Like the real CLI: no `version` subcommand (unknown-command error, exit
    # 1); the version is only available as `--version`.
    if argv == ["version"]:
        print('Error: unknown command "version" for "fullsend"', file=sys.stderr)
        sys.exit(1)
    if argv == ["--version"]:
        print("fullsend version 0.49.0")
        sys.exit(0)

    def flag(name):
        return argv[argv.index(name) + 1]

    target = Path(flag("--target-repo"))
    out = Path(flag("--output-dir"))
    symlinks = sorted(
        str(p.relative_to(target))
        for base in (target, target / ".claude") if base.is_dir()
        for p in base.iterdir() if p.is_symlink())
    # Like fullsend, leave the --output-dir tree out of the target-repo upload
    # only when it is a top-level child of the target.
    excluded = None
    try:
        excluded = out.relative_to(target).parts[0]
    except ValueError:
        pass
    uploaded = sorted(
        str(p.relative_to(target)) for p in target.rglob("*")
        if p.is_file() and (excluded is None
                            or p.relative_to(target).parts[0] != excluded))
    settings_file = target / ".claude" / "settings.json"
    record = {
        "argv": argv,
        "cwd": os.getcwd(),
        "env": dict(os.environ),
        "env_task": os.environ.get("FULLSEND_TASK"),
        "uploaded": uploaded,
        "settings": settings_file.read_text() if settings_file.is_file() else None,
        "symlinks": symlinks,
        "scripts_is_dir": (target / "scripts").is_dir(),
        "scripts_files": sorted(p.name for p in (target / "scripts").iterdir())
                         if (target / "scripts").is_dir() else [],
    }
    Path(os.environ["FAKE_FULLSEND_RECORD"]).write_text(json.dumps(record))

    print("-> Running agent: " + argv[1])
    if not os.environ.get("FAKE_FULLSEND_NO_IMAGE"):
        # fullsend prints the sandbox image it resolved (after any
        # FULLSEND_SANDBOX_IMAGE override) and the sandbox architecture.
        print("    Image: " + os.environ.get(
            "FAKE_FULLSEND_IMAGE", "ghcr.io/fullsend-ai/sandbox@sha256:" + "a" * 64))
        print("  Arch: arm64")
    print("fullsend console stderr", file=sys.stderr)
    if os.environ.get("FAKE_FULLSEND_SLEEP"):
        time.sleep(float(os.environ["FAKE_FULLSEND_SLEEP"]))
    if os.environ.get("FAKE_FULLSEND_NO_RUN"):
        sys.exit(1)

    out.mkdir(parents=True, exist_ok=True)
    run = out / f"fs-sandbox-{len(list(out.iterdir())) + 1}"
    it = run / "iteration-1"
    (it / "output").mkdir(parents=True)
    (it / "transcripts").mkdir()
    (run / "logs").mkdir()
    (run / "security").mkdir()
    (run / "metrics.json").write_text(json.dumps({
        "num_turns": 7, "total_cost_usd": 0.42,
        "token_usage": {"input": 1200, "output": 340, "reasoning": 0,
                        "cache_creation": 50, "cache_read": 900},
        "iterations": 1, "tool_calls": 4, "model": "claude-opus-4-6",
        "runtime": "claude", "requested_model": "opus", "override_source": "flag",
    }))
    events = [
        {"type": "system", "subtype": "init", "model": "claude-opus-4-6"},
        {"type": "assistant", "message": {
            "id": "msg_1", "model": "claude-opus-4-6", "role": "assistant",
            "content": [{"type": "text", "text": "Working"}],
            "usage": {"input_tokens": 10, "output_tokens": 5}}},
        {"type": "assistant", "message": {
            "id": "msg_2", "model": "claude-opus-4-6", "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "Read",
                         "input": {"file_path": "batch.yaml"}}],
            "usage": {"input_tokens": 20, "output_tokens": 8}}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}},
        {"type": "result", "subtype": "success", "num_turns": 2,
         "total_cost_usd": 0.11, "result": "done",
         "modelUsage": {"claude-opus-4-6": {
             "inputTokens": 100, "outputTokens": 50, "costUSD": 0.11}}},
    ]
    (it / "output.jsonl").write_text(
        "\\n".join(json.dumps(e) for e in events) + "\\n")
    (it / "output" / "agent-result.json").write_text(json.dumps({"ok": True}))
    (it / "transcripts" / "s1-agent-a1.jsonl").write_text(json.dumps(
        {"message": {"role": "assistant", "id": "msg_sub_1",
                     "model": "claude-haiku-4-5"}}) + "\\n")
    (it / "transcripts" / "main.jsonl").write_text("{}\\n")
    (run / "run-telemetry.jsonl").write_text("\\n".join([
        json.dumps({"event": "tool", "attributes": {"gen_ai.tool.name": "Read"}}),
        json.dumps({"event": "hook", "attributes": {
            "decision": "block", "gen_ai.tool.name": "Bash",
            "gen_ai.tool.call.id": "toolu_9", "reason": "Tirith: curl | sh"}}),
    ]) + "\\n")
    (run / "security" / "findings.jsonl").write_text("{}\\n")

    for i in range(1, int(os.environ.get("FAKE_FULLSEND_ARTIFACTS", "0")) + 1):
        # The project's post-script copies the agent's outputs into the target
        # repository; --no-post-script skips it.
        if "--no-post-script" in argv:
            continue
        (target / "artifacts").mkdir(exist_ok=True)
        (target / "artifacts" / f"report-{i}.md").write_text(f"report {i}")

    if os.environ.get("FAKE_FULLSEND_ITERATIONS") == "2":
        it2 = run / "iteration-2"
        (it2 / "output").mkdir(parents=True)
        (it2 / "transcripts").mkdir()
        (it2 / "output.jsonl").write_text(json.dumps(
            {"type": "assistant", "message": {
                "id": "msg_3", "model": "claude-opus-4-6", "role": "assistant",
                "content": [{"type": "text", "text": "Second iteration"}],
                "usage": {"input_tokens": 4, "output_tokens": 2}}}) + "\\n")
        (it2 / "transcripts" / "s1-agent-a1.jsonl").write_text(json.dumps(
            {"message": {"role": "assistant", "id": "msg_sub_2",
                         "model": "claude-haiku-4-5"}}) + "\\n")

    if os.environ.get("FAKE_FULLSEND_HANG_AFTER_RUN"):
        sys.stdout.flush()
        sys.stderr.flush()
        time.sleep(float(os.environ["FAKE_FULLSEND_HANG_AFTER_RUN"]))

    sys.exit(int(os.environ.get("FAKE_FULLSEND_EXIT", "0")))
''')


@pytest.fixture
def fake_bin(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "fullsend"
    script.write_text(f"#!{sys.executable}\n" + FAKE_FULLSEND)
    script.chmod(0o755)
    return bin_dir


@pytest.fixture
def fake_fullsend(fake_bin, tmp_path, monkeypatch):
    """Fake binary on PATH; returns the path its run record is written to."""
    record = tmp_path / "record.json"
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_FULLSEND_RECORD", str(record))
    for var in ("FAKE_FULLSEND_SLEEP", "FAKE_FULLSEND_EXIT", "FAKE_FULLSEND_ARTIFACTS",
                "FAKE_FULLSEND_NO_RUN", "FAKE_FULLSEND_HANG_AFTER_RUN",
                "FAKE_FULLSEND_ITERATIONS",
                "FAKE_FULLSEND_NO_IMAGE", "FAKE_FULLSEND_IMAGE"):
        monkeypatch.delenv(var, raising=False)
    return record


def _run_dirs(ws):
    """Per-run (sandbox) directories fullsend wrote under the workspace."""
    return sorted((ws / ".fullsend-runs").glob("fs-sandbox-*"))


def _runner(tmp_path, **kwargs):
    fs_dir = tmp_path / "project" / ".fullsend"
    fs_dir.mkdir(parents=True, exist_ok=True)
    defaults = dict(agent="rfe-creator-eval", fullsend_dir=str(fs_dir))
    defaults.update(kwargs)
    return FullsendRunner(**defaults)


def _workspace(tmp_path):
    return _workspace_n(tmp_path, "ws")


def _workspace_n(tmp_path, name):
    ws = tmp_path / name
    ws.mkdir()
    return ws


class TestFullsendConfig:
    def _load(self, tmp_path, runner_block, extra=""):
        p = tmp_path / "eval.yaml"
        p.write_text(textwrap.dedent("""\
            name: t
            execution:
              skill: s
            """) + runner_block + extra)
        return EvalConfig.from_yaml(p)

    def test_fullsend_with_agent_loads(self, tmp_path):
        cfg = self._load(tmp_path, textwrap.dedent("""\
            runner:
              type: fullsend
              agent: rfe-creator-eval
              fullsend_dir: eval/.fullsend
              binary: /opt/fullsend
              extra_args: [--keep-sandbox, --no-post-script]
            """))
        assert cfg.runner.type == "fullsend"
        assert cfg.runner.agent == "rfe-creator-eval"
        assert cfg.runner.fullsend_dir == "eval/.fullsend"
        assert cfg.runner.binary == "/opt/fullsend"
        assert cfg.runner.extra_args == ["--keep-sandbox", "--no-post-script"]

    def test_defaults(self, tmp_path):
        cfg = self._load(tmp_path, "runner:\n  type: fullsend\n  agent: a\n")
        assert cfg.runner.fullsend_dir is None
        assert cfg.runner.binary is None
        assert cfg.runner.extra_args == []

    def test_missing_agent_names_the_field(self, tmp_path):
        with pytest.raises(ValueError, match=r"runner\.agent is required"):
            self._load(tmp_path, "runner:\n  type: fullsend\n")

    def test_unknown_runner_type_names_the_field(self, tmp_path):
        with pytest.raises(ValueError, match=r"runner\.type: unknown runner type 'nope'"):
            self._load(tmp_path, "runner:\n  type: nope\n")

    def test_non_string_runner_type_names_the_field(self, tmp_path):
        with pytest.raises(ValueError, match=r"runner\.type: unknown runner type"):
            self._load(tmp_path, "runner:\n  type: [a, b]\n")

    def test_unknown_step_runner_type_names_the_field(self, tmp_path):
        p = tmp_path / "eval.yaml"
        p.write_text(textwrap.dedent("""\
            name: t
            execution:
              steps:
                - id: one
                  skill: s
                  runner:
                    type: nope
            """))
        with pytest.raises(ValueError, match=r"execution\.steps\[0\]\.runner\.type"):
            EvalConfig.from_yaml(p)

    def test_fullsend_rejected_for_judge_agent_runner(self, tmp_path):
        with pytest.raises(ValueError, match=r"agent judge's runner"):
            self._load(tmp_path, "judges:\n  - name: j\n    prompt: grade\n"
                                 "    agent:\n      runner:\n        type: fullsend\n"
                                 "        agent: a\n")

    def test_fullsend_rejected_with_runner_backed_judge_model(self, tmp_path):
        with pytest.raises(ValueError, match=r"models\.judge.*SDK-backed"):
            self._load(tmp_path, "runner:\n  type: fullsend\n  agent: a\n"
                                 "models:\n  judge: runner:/opus\n"
                                 "judges:\n  - name: j\n    prompt: grade\n")

    def test_fullsend_rejected_with_runner_backed_per_judge_model(self, tmp_path):
        with pytest.raises(ValueError, match=r"judge 'j' model.*SDK-backed"):
            self._load(tmp_path, "runner:\n  type: fullsend\n  agent: a\n"
                                 "judges:\n  - name: j\n    prompt: grade\n"
                                 "    model: runner:/opus\n")

    def test_fullsend_accepts_sdk_backed_judge_model(self, tmp_path):
        cfg = self._load(tmp_path, "runner:\n  type: fullsend\n  agent: a\n"
                                   "models:\n  judge: anthropic:/claude-opus-4-1\n"
                                   "judges:\n  - name: j\n    prompt: grade\n")
        assert cfg.runner.type == "fullsend"

    def test_runner_backed_default_overridden_by_every_llm_judge_loads(self, tmp_path):
        cfg = self._load(tmp_path, "runner:\n  type: fullsend\n  agent: a\n"
                                   "models:\n  judge: runner:/opus\n"
                                   "judges:\n"
                                   "  - name: j\n    prompt: grade\n"
                                   "    model: anthropic:/claude-opus-4-1\n"
                                   "  - name: c\n    check: return True, 'ok'\n")
        assert cfg.runner.type == "fullsend"
        assert cfg.models.judge == "runner:/opus"

    def test_runner_backed_default_with_only_check_judges_loads(self, tmp_path):
        cfg = self._load(tmp_path, "runner:\n  type: fullsend\n  agent: a\n"
                                   "models:\n  judge: runner:/opus\n"
                                   "judges:\n  - name: c\n    check: return True, 'ok'\n")
        assert cfg.runner.type == "fullsend"

    def test_runner_backed_default_rejected_when_one_judge_inherits(self, tmp_path):
        with pytest.raises(ValueError, match=r"models\.judge.*SDK-backed"):
            self._load(tmp_path, "runner:\n  type: fullsend\n  agent: a\n"
                                 "models:\n  judge: runner:/opus\n"
                                 "judges:\n"
                                 "  - name: j\n    prompt: grade\n"
                                 "    model: anthropic:/claude-opus-4-1\n"
                                 "  - name: k\n    prompt: grade again\n")

    def test_fullsend_rejected_with_execution_prompt(self, tmp_path):
        p = tmp_path / "eval.yaml"
        p.write_text("name: t\nexecution:\n  prompt: hello\n"
                     "runner:\n  type: fullsend\n  agent: a\n")
        with pytest.raises(ValueError, match=r"execution\.prompt"):
            EvalConfig.from_yaml(p)

    def test_relative_binary_with_separator_resolves_against_project_root(
            self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cfg = self._load(tmp_path, "runner:\n  type: fullsend\n  agent: a\n"
                                   "  binary: bin/fullsend\n")
        runner = FullsendRunner.from_config(cfg)
        assert runner._binary == str((tmp_path / "bin" / "fullsend").resolve())

    def test_term_grace_configurable(self, tmp_path):
        cfg = self._load(tmp_path, "runner:\n  type: fullsend\n  agent: a\n"
                                   "  term_grace_s: 45\n")
        assert FullsendRunner.from_config(cfg)._term_grace_s == 45.0
        assert FullsendRunner(agent="a")._term_grace_s == fullsend_mod._DEFAULT_TERM_GRACE_S
        assert FullsendRunner(agent="a", term_grace_s=0)._term_grace_s == 0.0
        with pytest.raises(ValueError, match="term_grace_s"):
            self._load(tmp_path, "runner:\n  type: fullsend\n  agent: a\n"
                                 "  term_grace_s: 0\n")

    def test_extra_args_must_be_list_of_strings(self, tmp_path):
        with pytest.raises(ValueError, match=r"runner\.extra_args must be a list"):
            self._load(tmp_path, "runner:\n  type: fullsend\n  agent: a\n"
                                 "  extra_args: --debug\n")

    def test_from_config_resolves_fullsend_dir_against_project_root(
            self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cfg = self._load(tmp_path, "runner:\n  type: fullsend\n  agent: a\n"
                                   "  effort: high\n",
                         extra="execution:\n  skill: s\n  env:\n    K: v\n")
        runner = FullsendRunner.from_config(cfg, subagent_model="sonnet")
        assert runner._fullsend_dir == str((tmp_path / ".fullsend").resolve())
        assert runner._binary == "fullsend"
        assert runner._effort == "high"
        assert runner._subagent_model == "sonnet"
        assert runner._execution_env == {"K": "v"}

    def test_registered(self):
        assert RUNNERS["fullsend"] is FullsendRunner
        assert issubclass(FullsendRunner, EvalRunner)
        assert FullsendRunner(agent="a").name == "fullsend"

    def test_no_harbor_equivalent(self):
        from agent_eval.harbor.run import _RUNNER_TO_HARBOR_AGENT
        assert set(RUNNERS) == set(_RUNNER_TO_HARBOR_AGENT)
        assert "fullsend" in _RUNNER_TO_HARBOR_AGENT
        assert _RUNNER_TO_HARBOR_AGENT["fullsend"] is None

    def test_runner_type_registry_matches_runners(self):
        from agent_eval.config import RUNNER_TYPES
        assert set(RUNNER_TYPES) == set(RUNNERS)

    def test_constructor_requires_agent(self):
        with pytest.raises(ValueError, match="runner.agent"):
            FullsendRunner(agent=None)


class TestFullsendExecute:
    def test_command_line(self, tmp_path, fake_fullsend):
        ws = _workspace(tmp_path)
        runner = _runner(tmp_path, extra_args=["--keep-sandbox", "--debug"])
        result = runner.execute(target="rfe.create", args="--headless", workspace=ws,
                                model="opus", timeout_s=60)
        assert result.exit_code == 0, result.stderr
        rec = json.loads(fake_fullsend.read_text())
        argv = rec["argv"]
        assert argv == [
            "run", "rfe-creator-eval",
            "--fullsend-dir", str((tmp_path / "project" / ".fullsend").resolve()),
            "--target-repo", str(ws.resolve()),
            "--model", "opus",
            "--output-dir", str(ws.resolve() / ".fullsend-runs"),
            "--keep-sandbox", "--debug",
        ]
        assert "--env-file" not in argv
        assert rec["cwd"] == str(ws.resolve())

    def test_environment_content(self, tmp_path, fake_fullsend, monkeypatch):
        monkeypatch.setenv("CALLER_SECRET", "from-caller")
        ws = _workspace(tmp_path)
        runner = _runner(
            tmp_path,
            execution_env={"JIRA_URL": "https://jira.example", "FWD": "$CALLER_SECRET",
                           "MISSING": "$NOT_SET_ANYWHERE_123"},
            runner_env={"GOOGLE_CLOUD_PROJECT": "proj"},
            subagent_model="claude-sonnet-4-6", effort="high")
        args = '--priority high "a $quoted\\ prompt"\nsecond line'
        runner.execute(target="rfe.create", args=args, workspace=ws, model="opus",
                       timeout_s=60, extra_env={"HOOK_VAR": "hooked"})
        rec = json.loads(fake_fullsend.read_text())
        env = rec["env"]
        assert env["FULLSEND_TASK"] == f"/rfe.create {args}"
        assert env["AGENT_EVAL_SUBAGENT_MODEL"] == "claude-sonnet-4-6"
        assert "AGENT_EVAL_EFFORT" not in env
        assert rec["argv"][rec["argv"].index("--effort") + 1] == "high"
        assert env["JIRA_URL"] == "https://jira.example"
        assert env["FWD"] == "from-caller"
        assert "MISSING" not in env
        assert env["GOOGLE_CLOUD_PROJECT"] == "proj"
        assert env["HOOK_VAR"] == "hooked"
        assert rec["env_task"] == f"/rfe.create {args}"

    def test_unset_model_and_effort_pass_nothing(self, tmp_path, fake_fullsend,
                                                 monkeypatch):
        monkeypatch.delenv("AGENT_EVAL_SUBAGENT_MODEL", raising=False)
        ws = _workspace(tmp_path)
        _runner(tmp_path).execute(target="s", args="", workspace=ws, model="",
                                  timeout_s=60)
        rec = json.loads(fake_fullsend.read_text())
        assert rec["env"]["FULLSEND_TASK"] == "/s"
        assert "AGENT_EVAL_SUBAGENT_MODEL" not in rec["env"]
        assert "AGENT_EVAL_EFFORT" not in rec["env"]
        assert "--effort" not in rec["argv"]
        assert "--model" not in rec["argv"]

    def test_braced_env_reference_resolved(self, tmp_path, fake_fullsend, monkeypatch):
        monkeypatch.setenv("CALLER_SECRET", "from-caller")
        ws = _workspace(tmp_path)
        _runner(tmp_path, execution_env={"FWD": "${CALLER_SECRET}"}).execute(
            target="s", args="", workspace=ws, model="m", timeout_s=60)
        assert json.loads(fake_fullsend.read_text())["env"]["FWD"] == "from-caller"

    def test_subagent_model_warns_when_agent_does_not_reference_it(
            self, tmp_path, fake_fullsend, capsys):
        ws = _workspace(tmp_path)
        runner = _runner(tmp_path, subagent_model="sonnet")
        runner.execute(target="s", args="", workspace=ws, model="m", timeout_s=60)
        assert "AGENT_EVAL_SUBAGENT_MODEL" in capsys.readouterr().out
        (tmp_path / "project" / ".fullsend" / "agent.md").write_text(
            "CLAUDE_CODE_SUBAGENT_MODEL=${AGENT_EVAL_SUBAGENT_MODEL}")
        runner.execute(target="s", args="", workspace=_workspace_n(tmp_path, "ws2"),
                       model="m", timeout_s=60)
        assert "WARNING" not in capsys.readouterr().out

    def test_workspace_settings_stripped_of_host_only_keys(self, tmp_path, fake_fullsend):
        ws = _workspace(tmp_path)
        (ws / ".claude").mkdir()
        (ws / ".claude" / "settings.json").write_text(json.dumps({
            "env": {"SECRET": "x"},
            "hooks": {"PreToolUse": [{"hooks": [{
                "command": f"python3 '{ws}/hooks/tools.py'"}]}]},
            "permissions": {"allow": ["Bash(ls)"],
                            "additionalDirectories": ["/host/dir"]},
            "model": "opus",
        }))
        _runner(tmp_path).execute(target="s", args="", workspace=ws, model="m",
                                  timeout_s=60)
        uploaded = json.loads(json.loads(fake_fullsend.read_text())["settings"])
        assert "env" not in uploaded and "hooks" not in uploaded
        assert uploaded["permissions"] == {"allow": ["Bash(ls)"]}
        assert uploaded["model"] == "opus"

    def test_workspace_settings_restored_after_run(self, tmp_path, fake_fullsend):
        """A later non-fullsend pipeline step shares the workspace and still
        needs the hooks, env and directory grants the sanitiser strips."""
        ws = _workspace(tmp_path)
        (ws / ".claude").mkdir()
        original = json.dumps({
            "env": {"SECRET": "x"},
            "hooks": {"PreToolUse": [{"hooks": [{
                "command": f"python3 '{ws}/hooks/tools.py'"}]}]},
            "permissions": {"allow": ["Bash(ls)"],
                            "additionalDirectories": ["/host/dir"]},
        }, indent=4)
        settings = ws / ".claude" / "settings.json"
        settings.write_text(original)
        _runner(tmp_path).execute(target="s", args="", workspace=ws, model="m",
                                  timeout_s=60)
        uploaded = json.loads(json.loads(fake_fullsend.read_text())["settings"])
        assert "hooks" not in uploaded  # the run itself saw the sanitised file
        assert settings.read_text() == original

    def test_settings_alias_is_materialised_sanitised(self, tmp_path, fake_fullsend):
        """A workspace link to .claude/settings.json is copied as a regular file
        (the workspace is an allowed root); sanitising runs first, so the copy
        never carries the host-only env, hooks or directory grants."""
        ws = _workspace(tmp_path)
        (ws / ".claude").mkdir()
        original = json.dumps({
            "env": {"SECRET": "x"},
            "hooks": {"PreToolUse": [{"hooks": [{
                "command": f"python3 '{ws}/hooks/tools.py'"}]}]},
            "permissions": {"allow": ["Bash(ls)"],
                            "additionalDirectories": ["/host/dir"]},
            "model": "opus",
        }, indent=4)
        settings = ws / ".claude" / "settings.json"
        settings.write_text(original)
        (ws / "backup.json").symlink_to(Path(".claude") / "settings.json")
        (ws / ".claude" / "alias.json").symlink_to("settings.json")

        result = _runner(tmp_path).execute(target="s", args="", workspace=ws,
                                           model="m", timeout_s=60)

        assert result.exit_code == 0
        for copy in (ws / "backup.json", ws / ".claude" / "alias.json"):
            assert copy.is_file() and not copy.is_symlink()
            uploaded = json.loads(copy.read_text())
            assert "env" not in uploaded and "hooks" not in uploaded
            assert uploaded["permissions"] == {"allow": ["Bash(ls)"]}
            assert uploaded["model"] == "opus"
        # Only the canonical file gets its host-only keys back after the run.
        assert settings.read_text() == original

    def test_workspace_settings_restored_when_launch_fails(self, tmp_path):
        ws = _workspace(tmp_path)
        (ws / ".claude").mkdir()
        original = json.dumps({"hooks": {"PreToolUse": []}, "model": "opus"})
        settings = ws / ".claude" / "settings.json"
        settings.write_text(original)
        result = _runner(tmp_path, binary=str(tmp_path / "no-such-fullsend")).execute(
            target="s", args="", workspace=ws, model="m", timeout_s=60)
        assert result.exit_code == -1
        assert settings.read_text() == original

    def test_restore_reapplies_original_mode_not_post_run_mode(
            self, tmp_path, monkeypatch):
        """The run leaves a world-readable settings file; the restored one
        must carry the mode it had before the run, not the loosened one."""
        ws = _workspace(tmp_path)
        (ws / ".claude").mkdir()
        settings = ws / ".claude" / "settings.json"
        settings.write_text(json.dumps({"env": {"SECRET": "x"}}))
        settings.chmod(0o600)
        original_mode = settings.stat().st_mode & 0o777

        def loosen(self, *args, **kwargs):
            settings.chmod(0o644)
            return fullsend_mod.RunResult(
                exit_code=0, stdout="", stderr="", duration_s=0.0)

        monkeypatch.setattr(FullsendRunner, "_launch", loosen)
        _runner(tmp_path).execute(target="s", args="", workspace=ws, model="m",
                                  timeout_s=60)
        assert json.loads(settings.read_text()) == {"env": {"SECRET": "x"}}
        assert settings.stat().st_mode & 0o777 == original_mode == 0o600

    def test_restore_falls_back_to_owner_only_when_mode_unknown(self, tmp_path):
        ws = _workspace(tmp_path)
        (ws / ".claude").mkdir()
        settings = ws / ".claude" / "settings.json"
        settings.write_text("{}")
        settings.chmod(0o644)  # post-run mode must not leak into the restore
        fullsend_mod._restore_settings(ws, (b'{"env": {}}', None))
        assert settings.stat().st_mode & 0o777 == 0o600

    def test_restore_does_not_write_through_a_settings_symlink(self, tmp_path):
        ws = _workspace(tmp_path)
        (ws / ".claude").mkdir()
        artifact = ws / "artifact.txt"
        artifact.write_text("artifact")
        settings = ws / ".claude" / "settings.json"
        settings.symlink_to(artifact)
        fullsend_mod._restore_settings(ws, (b'{"env": {"K": "literal-secret"}}', 0o600))
        assert artifact.read_text() == "artifact"
        assert not settings.is_symlink()
        assert settings.read_bytes() == b'{"env": {"K": "literal-secret"}}'

    def test_restore_refuses_a_claude_dir_symlink(self, tmp_path, capsys):
        ws = _workspace(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (ws / ".claude").symlink_to(outside)
        fullsend_mod._restore_settings(ws, (b'{"env": {"K": "literal-secret"}}', 0o600))
        assert list(outside.iterdir()) == []
        assert "could not restore" in capsys.readouterr().out

    def test_restore_leaves_no_temp_file_behind(self, tmp_path):
        ws = _workspace(tmp_path)
        (ws / ".claude").mkdir()
        fullsend_mod._restore_settings(ws, (b"{}", 0o600))
        assert [p.name for p in (ws / ".claude").iterdir()] == ["settings.json"]

    def test_second_run_does_not_upload_first_runs_files(self, tmp_path, fake_fullsend):
        ws = _workspace(tmp_path)
        runner = _runner(tmp_path)
        runner.execute(target="s", args="", workspace=ws, model="m", timeout_s=60)
        assert any(ws.glob(".fullsend-runs/*/metrics.json"))
        runner.execute(target="s", args="", workspace=ws, model="m", timeout_s=60)
        uploaded = json.loads(fake_fullsend.read_text())["uploaded"]
        assert not [f for f in uploaded if f.startswith(".fullsend-runs")]

    def test_no_post_script_is_a_failed_case(self, tmp_path, fake_fullsend):
        ws = _workspace(tmp_path)
        result = _runner(tmp_path, extra_args=["--no-post-script"]).execute(
            target="s", args="", workspace=ws, model="m", timeout_s=60)
        assert result.exit_code == -1
        assert "post-script" in result.stderr
        assert not fake_fullsend.exists()

    def test_no_post_script_with_value_is_a_failed_case(self, tmp_path, fake_fullsend):
        """``--no-post-script=true`` skips the post-script just like the bare
        flag; the exact-membership guard used to let it through."""
        ws = _workspace(tmp_path)
        result = _runner(tmp_path, extra_args=["--no-post-script=true"]).execute(
            target="s", args="", workspace=ws, model="m", timeout_s=60)
        assert result.exit_code == -1
        assert "post-script" in result.stderr
        assert not fake_fullsend.exists()

    def test_no_post_script_false_is_accepted(self, tmp_path, fake_fullsend):
        ws = _workspace(tmp_path)
        result = _runner(tmp_path, extra_args=["--no-post-script=false"]).execute(
            target="s", args="", workspace=ws, model="m", timeout_s=60)
        assert result.exit_code == 0
        assert "--no-post-script=false" in json.loads(fake_fullsend.read_text())["argv"]

    def test_project_hooks_kept_harness_hooks_stripped(self, tmp_path, fake_fullsend):
        """The sanitiser removes only the harness's own hook entries (commands
        that run a script from the workspace's hooks/ directory) and keeps
        the project's hooks workspace.py carried over, as production loads
        them."""
        ws = _workspace(tmp_path)
        (ws / ".claude").mkdir()
        project_stop = {"hooks": [{"type": "command",
                                   "command": "python3 scripts/pipeline_state.py stop-guard"}]}
        project_start = {"matcher": "compact",
                         "hooks": [{"type": "command",
                                    "command": "python3 scripts/pipeline_state.py post-compact-hook"}]}
        original = json.dumps({
            "env": {"SECRET": "x"},
            "hooks": {
                "SubagentStop": [{"hooks": [{
                    "type": "command",
                    "command": f"python3 '{ws}/hooks/subagent_stop.py'"}]}],
                "PreToolUse": [{"matcher": "WebFetch", "hooks": [{
                    "type": "command",
                    "command": f"python3 '{ws}/hooks/tools.py'"}]}],
                "Stop": [project_stop],
                "SessionStart": [project_start],
            },
            "permissions": {"allow": ["Bash(ls)"],
                            "additionalDirectories": ["/host/dir"]},
            "model": "opus",
        }, indent=4)
        settings = ws / ".claude" / "settings.json"
        settings.write_text(original)
        settings.chmod(0o600)

        result = _runner(tmp_path).execute(target="s", args="", workspace=ws,
                                           model="m", timeout_s=60)

        assert result.exit_code == 0
        uploaded = json.loads(json.loads(fake_fullsend.read_text())["settings"])
        assert uploaded["hooks"] == {"Stop": [project_stop],
                                     "SessionStart": [project_start]}
        assert "env" not in uploaded
        assert uploaded["permissions"] == {"allow": ["Bash(ls)"]}
        assert uploaded["model"] == "opus"
        assert settings.read_text() == original
        assert settings.stat().st_mode & 0o777 == 0o600

    def test_all_harness_hooks_means_no_hooks_key(self, tmp_path, fake_fullsend):
        ws = _workspace(tmp_path)
        (ws / ".claude").mkdir()
        (ws / ".claude" / "settings.json").write_text(json.dumps({
            "hooks": {
                "SubagentStop": [{"hooks": [{
                    "type": "command",
                    "command": f"python3 '{ws}/hooks/subagent_stop.py'"}]}],
                "PreToolUse": [
                    {"matcher": "WebFetch", "hooks": [{
                        "type": "command", "command": f"python3 {ws}/hooks/tools.py"}]},
                    {"matcher": "Bash", "hooks": [{
                        "type": "command", "command": f"python3 {ws}/hooks/tools.py"}]},
                ],
            },
            "model": "opus",
        }))
        _runner(tmp_path).execute(target="s", args="", workspace=ws, model="m",
                                  timeout_s=60)
        uploaded = json.loads(json.loads(fake_fullsend.read_text())["settings"])
        assert "hooks" not in uploaded
        assert uploaded == {"model": "opus"}

    def test_project_hooks_only_leave_settings_untouched(self, tmp_path, fake_fullsend):
        """Nothing host-only to strip: the file is uploaded as written and the
        sanitiser reports no change (no restore write)."""
        ws = _workspace(tmp_path)
        (ws / ".claude").mkdir()
        content = {"hooks": {"Stop": [{"hooks": [{
            "type": "command", "command": "python3 scripts/pipeline_state.py stop-guard"}]}]}}
        (ws / ".claude" / "settings.json").write_text(json.dumps(content))
        assert fullsend_mod._sanitize_settings(ws) is None
        _runner(tmp_path).execute(target="s", args="", workspace=ws, model="m",
                                  timeout_s=60)
        uploaded = json.loads(json.loads(fake_fullsend.read_text())["settings"])
        assert uploaded == content

    @pytest.mark.parametrize("command, harness", [
        ("python3 '{ws}/hooks/subagent_stop.py'", True),
        ("python3 {ws}/hooks/tools.py", True),
        ("python3 'unterminated {ws}/hooks/tools.py", True),
        ("/usr/bin/python3 scripts/guard.py", False),
        ("python3 scripts/guard.py /tmp/state", False),
        ("python3 scripts/x.py > /dev/null 2>&1", False),
        ("python3 scripts/pipeline_state.py stop-guard", False),
        ("$CLAUDE_PROJECT_DIR/scripts/guard.sh", False),
        ("bash -c 'echo a/b'", False),
        ("python3 'unterminated /abs/path", False),
        ("python3 {ws}/scripts/hooks/tools.py", False),
        ("python3 {ws}/hooksy/tools.py", False),
    ])
    def test_harness_hook_rule(self, tmp_path, command, harness):
        ws = tmp_path / "ws"
        ws.mkdir()
        group = {"hooks": [{"type": "command",
                            "command": command.format(ws=ws)}]}
        assert fullsend_mod._is_harness_hook(group, ws) is harness
        # A prompt-type handler (no command) is never the harness's.
        assert fullsend_mod._is_harness_hook(
            {"hooks": [{"type": "prompt", "prompt": "check"}]}, ws) is False

    def test_harness_hook_under_unresolved_workspace_path(self, tmp_path):
        """workspace.py may have written the path before symlink resolution:
        a command naming the workspace through a link is still the harness's."""
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        group = {"hooks": [{"type": "command",
                            "command": f"python3 '{link}/hooks/tools.py'"}]}
        assert fullsend_mod._is_harness_hook(group, real) is True
        assert fullsend_mod._is_harness_hook(group, link) is True

    def test_project_hooks_with_absolute_tokens_kept(self, tmp_path, fake_fullsend):
        ws = _workspace(tmp_path)
        (ws / ".claude").mkdir()
        kept = [
            {"hooks": [{"type": "command",
                        "command": "/usr/bin/python3 scripts/guard.py"}]},
            {"hooks": [{"type": "command",
                        "command": "python3 scripts/guard.py /tmp/state"}]},
            {"hooks": [{"type": "command",
                        "command": "python3 scripts/x.py > /dev/null 2>&1"}]},
        ]
        (ws / ".claude" / "settings.json").write_text(json.dumps({"hooks": {
            "Stop": [*kept, {"hooks": [{
                "type": "command",
                "command": f"python3 '{ws}/hooks/subagent_stop.py'"}]}]}}))
        _runner(tmp_path).execute(target="s", args="", workspace=ws, model="m",
                                  timeout_s=60)
        uploaded = json.loads(json.loads(fake_fullsend.read_text())["settings"])
        assert uploaded["hooks"] == {"Stop": kept}

    def test_symlink_copy_failure_becomes_run_result(
            self, tmp_path, fake_fullsend, monkeypatch):
        def boom(*a, **k):
            raise OSError("disk full")
        monkeypatch.setattr(fullsend_mod, "materialize_symlinks", boom)
        result = _runner(tmp_path).execute(
            target="s", args="", workspace=_workspace(tmp_path), model="m",
            timeout_s=60)
        assert result.exit_code == -1
        assert "disk full" in result.stderr

    def test_symlinks_materialized_before_run(self, tmp_path, fake_fullsend):
        outside = tmp_path / "project" / "scripts"
        outside.mkdir(parents=True)
        (outside / "helper.sh").write_text("echo hi")
        (outside / ".git").mkdir()
        (outside / ".git" / "HEAD").write_text("ref")
        skills = tmp_path / "project" / ".claude" / "skills"
        (skills / "my-skill").mkdir(parents=True)
        (skills / "my-skill" / "SKILL.md").write_text("# skill")
        (tmp_path / "project" / "CLAUDE.md").write_text("# project")

        ws = _workspace(tmp_path)
        (ws / "scripts").symlink_to(outside.resolve())
        (ws / "CLAUDE.md").symlink_to((tmp_path / "project" / "CLAUDE.md").resolve())
        (ws / "dangling").symlink_to(tmp_path / "does-not-exist")
        (ws / ".claude").mkdir()
        (ws / ".claude" / "settings.json").write_text("{}")
        (ws / ".claude" / "skills").symlink_to(skills.resolve())

        _runner(tmp_path, allowed_roots=[tmp_path / "project"]).execute(
            target="s", args="", workspace=ws, model="m", timeout_s=60)
        rec = json.loads(fake_fullsend.read_text())
        assert rec["symlinks"] == []
        assert rec["scripts_is_dir"] is True
        assert rec["scripts_files"] == ["helper.sh"]
        assert not (ws / "scripts").is_symlink()
        assert not (ws / "scripts" / ".git").exists()
        assert (ws / "CLAUDE.md").read_text() == "# project"
        assert not (ws / "dangling").exists() and not (ws / "dangling").is_symlink()
        assert (ws / ".claude" / "skills" / "my-skill" / "SKILL.md").is_file()
        assert not (ws / ".claude" / "skills").is_symlink()
        # The project root is untouched.
        assert (outside / ".git" / "HEAD").exists()
        assert sorted(p.name for p in outside.iterdir()) == [".git", "helper.sh"]

    def test_relative_symlink_inside_isolated_workspace_survives(
            self, tmp_path, fake_fullsend):
        ws = _workspace(tmp_path)
        (ws / "data").mkdir()
        (ws / "data" / "note.txt").write_text("note")
        (ws / "alias.txt").symlink_to("data/note.txt")

        _runner(tmp_path, allowed_roots=[tmp_path / "project"]).execute(
            target="s", args="", workspace=ws, model="m", timeout_s=60)
        assert not (ws / "alias.txt").is_symlink()
        assert (ws / "alias.txt").read_text() == "note"

    def test_no_allowed_roots_is_an_empty_allowlist(self, tmp_path, fake_fullsend):
        outside = tmp_path / "outside.txt"
        outside.write_text("host file")
        ws = _workspace(tmp_path)
        (ws / "leak.txt").symlink_to(outside)

        _runner(tmp_path).execute(
            target="s", args="", workspace=ws, model="m", timeout_s=60)
        assert not (ws / "leak.txt").exists()
        assert not (ws / "leak.txt").is_symlink()

    def test_unrestricted_symlink_copy_is_an_explicit_opt_in(
            self, tmp_path, fake_fullsend):
        outside = tmp_path / "outside.txt"
        outside.write_text("host file")
        ws = _workspace(tmp_path)
        (ws / "leak.txt").symlink_to(outside)

        _runner(tmp_path, unrestricted_symlink_copy=True).execute(
            target="s", args="", workspace=ws, model="m", timeout_s=60)
        assert (ws / "leak.txt").read_text() == "host file"
        assert not (ws / "leak.txt").is_symlink()

    def test_run_result_mapping(self, tmp_path, fake_fullsend):
        ws = _workspace(tmp_path)
        result = _runner(tmp_path).execute(target="s", args="", workspace=ws,
                                           model="opus", timeout_s=60)
        (run_dir,) = _run_dirs(ws)
        stream = (run_dir / "iteration-1" / "output.jsonl").read_text()
        assert result.exit_code == 0
        assert result.stdout == stream
        assert "Running agent: rfe-creator-eval" in result.stderr
        assert "fullsend console stderr" in result.stderr
        # metrics.json wins for the headline numbers.
        assert result.cost_usd == 0.42
        assert result.token_usage == {
            "input": 1200, "output": 340, "cache_read": 900, "cache_create": 50}
        assert result.num_turns == 7
        assert result.resolved_model == "claude-opus-4-6"
        # extract_usage over the stream.
        assert result.models_used == ["claude-opus-4-6"]
        assert result.per_model_usage["claude-opus-4-6"]["cost_usd"] == 0.11
        assert result.per_model_turns == {"claude-opus-4-6": 2, "claude-haiku-4-5": 1}
        assert result.message_ids == ["msg_1", "msg_2", "msg_sub_1"]
        # Subagent transcripts copied; non-agent transcripts are not.
        assert sorted(p.name for p in (ws / "subagents").iterdir()) == ["s1-agent-a1.jsonl"]
        # Hook block events from run-telemetry.jsonl.
        assert result.permission_denials == [{
            "tool_name": "Bash", "tool_use_id": "toolu_9", "tool_input": {},
            "reason": "Tirith: curl | sh"}]
        raw = result.raw_output
        assert raw["token_usage"]["cache_read"] == 900
        assert raw["token_usage"]["cache_creation"] == 50
        assert raw["tool_calls"] == 4
        assert raw["iterations"] == 1
        assert raw["security_files"] == 1

    def test_all_iterations_mapped(self, tmp_path, fake_fullsend, monkeypatch):
        monkeypatch.setenv("FAKE_FULLSEND_ITERATIONS", "2")
        ws = _workspace(tmp_path)
        result = _runner(tmp_path).execute(target="s", args="", workspace=ws,
                                           model="m", timeout_s=60)
        assert "Working" in result.stdout and "Second iteration" in result.stdout
        assert result.message_ids == ["msg_1", "msg_2", "msg_3", "msg_sub_1", "msg_sub_2"]
        assert sorted(p.name for p in (ws / "subagents").iterdir()) == [
            "it2-s1-agent-a1.jsonl", "s1-agent-a1.jsonl"]

    def test_nonzero_exit_propagates(self, tmp_path, fake_fullsend, monkeypatch):
        monkeypatch.setenv("FAKE_FULLSEND_EXIT", "3")
        ws = _workspace(tmp_path)
        result = _runner(tmp_path).execute(target="s", args="", workspace=ws,
                                           model="m", timeout_s=60)
        assert result.exit_code == 3
        assert result.cost_usd == 0.42

    def test_timeout(self, tmp_path, fake_fullsend, monkeypatch):
        monkeypatch.setenv("FAKE_FULLSEND_SLEEP", "30")
        ws = _workspace(tmp_path)
        result = _runner(tmp_path, term_grace_s=2).execute(
            target="s", args="", workspace=ws, model="m", timeout_s=1)
        assert result.exit_code == -1
        assert "Timed out" in result.stderr
        assert result.duration_s < 20

    def test_timeout_keeps_run_data_already_on_disk(
            self, tmp_path, fake_fullsend, monkeypatch):
        monkeypatch.setenv("FAKE_FULLSEND_HANG_AFTER_RUN", "30")
        monkeypatch.setenv("FAKE_FULLSEND_ARTIFACTS", "1")
        ws = _workspace(tmp_path)
        runner = _runner(tmp_path, term_grace_s=2)
        result = runner.execute(target="s", args="", workspace=ws,
                                model="m", timeout_s=3)
        assert result.exit_code == -1
        assert "Timed out" in result.stderr
        assert result.cost_usd == 0.42
        assert result.num_turns == 7
        assert "Working" in result.stdout
        assert (ws / "artifacts" / "report-1.md").read_text() == "report 1"
        assert (ws / "subagents" / "s1-agent-a1.jsonl").is_file()

    def test_missing_metrics_counts_transcript_only_subagent_turns(self, tmp_path):
        ws = _workspace(tmp_path)
        out = ws / ".fullsend-runs"
        it = out / "fs-sandbox-1" / "iteration-1"
        (it / "transcripts").mkdir(parents=True)
        (it / "output.jsonl").write_text(json.dumps(
            {"type": "assistant", "message": {
                "id": "msg_1", "model": "claude-opus-4-6", "role": "assistant",
                "content": [{"type": "text", "text": "Working"}],
                "usage": {"input_tokens": 10, "output_tokens": 5}}}) + "\n")
        (it / "transcripts" / "s1-agent-a1.jsonl").write_text(json.dumps(
            {"message": {"role": "assistant", "id": "msg_sub_1",
                         "model": "claude-haiku-4-5"}}) + "\n")
        result = _runner(tmp_path)._map_run_dir(
            out, ws, exit_code=-1, stderr="", duration=1.0)
        assert result.per_model_turns == {"claude-opus-4-6": 1, "claude-haiku-4-5": 1}
        assert result.num_turns == 2

    def test_missing_binary(self, tmp_path):
        ws = _workspace(tmp_path)
        result = _runner(tmp_path, binary=str(tmp_path / "no-such-fullsend")).execute(
            target="s", args="", workspace=ws, model="m", timeout_s=10)
        assert result.exit_code == -1
        assert result.stderr

    def test_no_run_dir(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin2"
        bin_dir.mkdir()
        script = bin_dir / "fullsend"
        script.write_text("#!/bin/sh\necho 'Error: resolving agent' >&2\nexit 1\n")
        script.chmod(0o755)
        ws = _workspace(tmp_path)
        result = _runner(tmp_path, binary=str(script)).execute(
            target="s", args="", workspace=ws, model="m", timeout_s=10)
        assert result.exit_code == 1
        assert result.stdout == ""
        assert "resolving agent" in result.stderr
        assert result.cost_usd is None
        assert result.permission_denials == []

    def test_prompt_mode_unsupported(self, tmp_path):
        with pytest.raises(ValueError, match="prompt mode"):
            _runner(tmp_path).execute(target=None, args="hello",
                                      workspace=_workspace(tmp_path), model="m")


class TestInvocationScoping:
    def test_failed_invocation_does_not_inherit_previous_run(
            self, tmp_path, fake_fullsend, monkeypatch):
        ws = _workspace(tmp_path)
        runner = _runner(tmp_path)
        first = runner.execute(target="s", args="", workspace=ws, model="m", timeout_s=60)
        assert first.cost_usd == 0.42
        assert first.per_model_turns == {"claude-opus-4-6": 2, "claude-haiku-4-5": 1}

        monkeypatch.setenv("FAKE_FULLSEND_NO_RUN", "1")
        second = runner.execute(target="s", args="", workspace=ws, model="m", timeout_s=60)
        assert second.exit_code == 1
        assert second.stdout == ""
        assert second.cost_usd is None
        assert second.num_turns is None
        assert second.models_used is None
        assert second.message_ids == []
        assert len(_run_dirs(ws)) == 1

    def test_message_ids_do_not_accumulate_across_invocations(
            self, tmp_path, fake_fullsend):
        ws = _workspace(tmp_path)
        runner = _runner(tmp_path)
        runner.execute(target="s", args="", workspace=ws, model="m", timeout_s=60)
        again = runner.execute(target="s", args="", workspace=ws, model="m", timeout_s=60)
        assert again.message_ids == ["msg_1", "msg_2", "msg_sub_1"]


class TestArtifactsViaPostScript:
    def test_artifacts_reach_workspace_through_post_script(
            self, tmp_path, fake_fullsend, monkeypatch):
        monkeypatch.setenv("FAKE_FULLSEND_ARTIFACTS", "2")
        ws = _workspace(tmp_path)
        _runner(tmp_path).execute(target="s", args="", workspace=ws, model="m",
                                  timeout_s=60)
        assert (ws / "artifacts" / "report-1.md").read_text() == "report 1"
        assert (ws / "artifacts" / "report-2.md").read_text() == "report 2"
        # agent-result.json stays in fullsend's own export, not the workspace.
        (sandbox,) = _run_dirs(ws)
        assert (sandbox / "iteration-1" / "output" / "agent-result.json").is_file()
        assert not (ws / "agent-result.json").exists()

    def test_iteration_output_is_not_read_as_the_repository(
            self, tmp_path, fake_fullsend):
        ws = _workspace(tmp_path)
        _runner(tmp_path).execute(target="s", args="", workspace=ws, model="m",
                                  timeout_s=60)
        assert not (ws / "agent-result.json").exists()
        assert not hasattr(FullsendRunner, "_restore_outputs")


class TestTranscriptCopyContainment:
    def _transcripts(self, tmp_path):
        src = tmp_path / "transcripts"
        src.mkdir()
        (src / "s1-agent-a1.jsonl").write_text("new")
        return src

    def test_destination_file_symlink_not_followed(self, tmp_path):
        src = self._transcripts(tmp_path)
        ws = tmp_path / "ws"
        (ws / "subagents").mkdir(parents=True)
        victim = tmp_path / "victim"
        victim.write_text("keep")
        (ws / "subagents" / "s1-agent-a1.jsonl").symlink_to(victim)
        fullsend_mod._copy_subagent_transcripts(src, ws / "subagents", ws)
        assert victim.read_text() == "keep"

    def test_destination_directory_with_nested_symlink_not_followed(self, tmp_path):
        src = self._transcripts(tmp_path)
        ws = tmp_path / "ws"
        dest_dir = ws / "subagents" / "s1-agent-a1.jsonl"
        dest_dir.mkdir(parents=True)
        victim = tmp_path / "victim"
        victim.write_text("keep")
        (dest_dir / "s1-agent-a1.jsonl").symlink_to(victim)
        fullsend_mod._copy_subagent_transcripts(src, ws / "subagents", ws)
        assert victim.read_text() == "keep"

    def test_destination_directory_symlink_not_followed(self, tmp_path):
        src = self._transcripts(tmp_path)
        ws = tmp_path / "ws"
        ws.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (ws / "subagents").symlink_to(outside)
        fullsend_mod._copy_subagent_transcripts(src, ws / "subagents", ws)
        assert list(outside.iterdir()) == []

    def test_source_directory_symlink_not_followed(self, tmp_path):
        real = self._transcripts(tmp_path)
        link = tmp_path / "linked"
        link.symlink_to(real)
        ws = tmp_path / "ws"
        ws.mkdir()
        fullsend_mod._copy_subagent_transcripts(link, ws / "subagents", ws)
        assert not (ws / "subagents").exists()

    def test_regular_copy_still_works(self, tmp_path):
        src = self._transcripts(tmp_path)
        ws = tmp_path / "ws"
        ws.mkdir()
        fullsend_mod._copy_subagent_transcripts(src, ws / "subagents", ws)
        assert (ws / "subagents" / "s1-agent-a1.jsonl").read_text() == "new"


class TestMaterializeSymlinks:
    def test_nested_relative_symlinks_preserved(self, tmp_path):
        project = tmp_path / "project"
        (project / "agent_eval").mkdir(parents=True)
        (project / "agent_eval" / "mod.py").write_text("x = 1")
        scripts = project / "skills" / "eval-run" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "run.py").write_text("print()")
        (scripts / "agent_eval").symlink_to("../../../agent_eval")
        (scripts / "dangling").symlink_to("../../../nope")

        ws = _workspace(tmp_path)
        (ws / "skills").symlink_to(project / "skills")
        materialize_symlinks(ws, [project.resolve()])

        copied = ws / "skills" / "eval-run" / "scripts"
        assert not (ws / "skills").is_symlink()
        assert not (copied / "agent_eval").is_symlink()
        assert (copied / "agent_eval" / "mod.py").read_text() == "x = 1"
        assert (copied / "run.py").is_file()
        assert not (copied / "dangling").exists()

    def test_symlink_cycle_terminates(self, tmp_path):
        project = tmp_path / "project"
        (project / "skills" / "a").mkdir(parents=True)
        (project / "skills" / "a" / "f.txt").write_text("f")
        (project / "skills" / "a" / "loop").symlink_to("..")
        ws = _workspace(tmp_path)
        (ws / "skills").symlink_to(project / "skills")
        materialize_symlinks(ws, [project.resolve()])
        assert (ws / "skills" / "a" / "f.txt").read_text() == "f"

    def test_links_outside_allowed_roots_dropped(self, tmp_path):
        secret = tmp_path / "home" / ".aws"
        secret.mkdir(parents=True)
        (secret / "credentials").write_text("hunter2")
        project = tmp_path / "project"
        (project / "scripts").mkdir(parents=True)
        (project / "scripts" / "ok.sh").write_text("ok")
        (project / "scripts" / "creds").symlink_to(secret)
        (project / "scripts" / "cred-file").symlink_to(secret / "credentials")

        ws = _workspace(tmp_path)
        (ws / "scripts").symlink_to(project / "scripts")
        (ws / "toplevel").symlink_to(secret)
        materialize_symlinks(ws, [project.resolve()])

        assert (ws / "scripts" / "ok.sh").is_file()
        assert not (ws / "scripts" / "creds").exists()
        assert not (ws / "scripts" / "cred-file").exists()
        assert not (ws / "toplevel").exists() and not (ws / "toplevel").is_symlink()
        assert not list(ws.rglob("credentials"))

    def test_self_referential_link_dropped(self, tmp_path, capsys):
        """``alias -> .`` with the workspace in allowed_roots: the copy would
        be staged inside its own target and grow without end."""
        project = tmp_path / "project"
        (project / "scripts").mkdir(parents=True)
        (project / "scripts" / "ok.sh").write_text("ok")
        ws = _workspace(tmp_path)
        (ws / "alias").symlink_to(".")
        (ws / "scripts").symlink_to(project / "scripts")

        materialize_symlinks(ws, [project.resolve(), ws.resolve()])

        assert not (ws / "alias").exists() and not (ws / "alias").is_symlink()
        assert not list(ws.glob(".alias.fullsend-*"))
        assert not (ws / "scripts").is_symlink()
        assert (ws / "scripts" / "ok.sh").is_file()
        assert "not copying symlink 'alias'" in capsys.readouterr().out

    def test_nested_link_back_into_workspace_skips_staging_dir(self, tmp_path):
        """A nested link back into the workspace must not descend into the
        copy being staged there."""
        project = tmp_path / "project"
        (project / "scripts").mkdir(parents=True)
        (project / "scripts" / "ok.sh").write_text("ok")
        ws = _workspace(tmp_path)
        (ws / "note.txt").write_text("n")
        (project / "scripts" / "back").symlink_to(ws)
        (ws / "scripts").symlink_to(project / "scripts")

        materialize_symlinks(ws, [project.resolve(), ws.resolve()])

        copied = ws / "scripts"
        assert (copied / "ok.sh").is_file()
        assert (copied / "back" / "note.txt").read_text() == "n"
        assert not list(copied.rglob(".scripts.fullsend-*"))
        assert not list(ws.glob(".scripts.fullsend-*"))

    def test_nested_link_to_settings_file_not_copied(self, tmp_path, capsys):
        """A committed link such as ``skills/x/host.json ->
        ../../.claude/settings.json`` would upload the project's own host
        settings unsanitised: the link is skipped, a sibling regular file is
        copied, and the warning names the link."""
        project = tmp_path / "project"
        (project / ".claude").mkdir(parents=True)
        (project / ".claude" / "settings.json").write_text(
            json.dumps({"env": {"API_TOKEN": "literal-secret-value"}}))
        (project / ".claude" / "settings.local.json").write_text(
            json.dumps({"env": {"LOCAL_TOKEN": "literal-local-secret"}}))
        skill = project / "skills" / "x"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# x")
        (skill / "host.json").symlink_to(Path("..") / ".." / ".claude" / "settings.json")
        (skill / "local.json").symlink_to(
            Path("..") / ".." / ".claude" / "settings.local.json")
        ws = _workspace(tmp_path)
        (ws / "skills").symlink_to(project / "skills")

        materialize_symlinks(ws, [project.resolve()])

        copied = ws / "skills" / "x"
        assert (copied / "SKILL.md").read_text() == "# x"
        assert not (copied / "host.json").exists()
        assert not (copied / "host.json").is_symlink()
        assert not (copied / "local.json").exists()
        out = capsys.readouterr().out
        assert "not copying symlink 'host.json'" in out
        assert "not copying symlink 'local.json'" in out
        assert "literal-secret-value" not in out

    def test_top_level_link_to_other_settings_file_dropped(self, tmp_path, capsys):
        """A top-level link to a settings file other than the workspace's own
        canonical one (e.g. the project's settings.local.json) is dropped; a
        link to the canonical file is still copied (it is sanitised first)."""
        project = tmp_path / "project"
        (project / ".claude").mkdir(parents=True)
        (project / ".claude" / "settings.local.json").write_text(
            json.dumps({"env": {"SECRET": "host-only"}}))
        ws = _workspace(tmp_path)
        (ws / ".claude").mkdir()
        (ws / ".claude" / "settings.json").write_text(json.dumps({"model": "opus"}))
        (ws / "local.json").symlink_to(project / ".claude" / "settings.local.json")
        (ws / "backup.json").symlink_to(Path(".claude") / "settings.json")

        materialize_symlinks(ws, [project.resolve(), ws.resolve()])

        assert not (ws / "local.json").exists()
        assert (ws / "backup.json").is_file() and not (ws / "backup.json").is_symlink()
        assert json.loads((ws / "backup.json").read_text()) == {"model": "opus"}
        assert "local.json" in capsys.readouterr().out
        assert json.loads((project / ".claude" / "settings.local.json").read_text()) == {
            "env": {"SECRET": "host-only"}}

    def test_nested_regular_settings_file_not_copied(self, tmp_path, capsys):
        """A ``.claude/settings*.json`` file is never copied, regular or not:
        the decision is by destination, not by how the tree was reached."""
        project = tmp_path / "project"
        (project / "fixtures" / ".claude").mkdir(parents=True)
        (project / "fixtures" / ".claude" / "settings.json").write_text('{"model": "x"}')
        (project / "fixtures" / ".claude" / "settings.local.json").write_text('{}')
        (project / "fixtures" / ".claude" / "other.json").write_text('{}')
        ws = _workspace(tmp_path)
        (ws / "fixtures").symlink_to(project / "fixtures")

        materialize_symlinks(ws, [project.resolve()])

        # The nested ``.claude`` directory is a settings container: not copied.
        assert not (ws / "fixtures" / ".claude").exists()
        assert not list(ws.rglob("settings.json"))
        assert not list(ws.rglob("settings.local.json"))
        assert "not copying '.claude'" in capsys.readouterr().out

    def test_link_to_nested_claude_dir_renamed_uploads_nothing(self, tmp_path, capsys):
        """``skills/x/host-config -> <project>/components/app/.claude``: the
        destination is named ``host-config``, but the source is a ``.claude``
        directory, so nothing under it is copied and the warning names the
        link."""
        project = tmp_path / "project"
        nested = project / "components" / "app" / ".claude"
        nested.mkdir(parents=True)
        (nested / "settings.local.json").write_text(
            json.dumps({"env": {"LOCAL_TOKEN": "literal-local-secret"}}))
        (nested / "other.json").write_text("{}")
        skill = project / "skills" / "x"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# x")
        (skill / "host-config").symlink_to(
            Path("..") / ".." / "components" / "app" / ".claude")
        ws = _workspace(tmp_path)
        (ws / "skills").symlink_to(project / "skills")

        materialize_symlinks(ws, [project.resolve()])

        assert (ws / "skills" / "x" / "SKILL.md").is_file()
        assert not (ws / "skills" / "x" / "host-config").exists()
        assert not list(ws.rglob("settings.local.json"))
        assert not list(ws.rglob("other.json"))
        out = capsys.readouterr().out
        assert "not copying 'host-config'" in out
        assert "literal-local-secret" not in out

    def test_top_level_link_to_nested_claude_dir_dropped(self, tmp_path, capsys):
        project = tmp_path / "project"
        nested = project / "components" / "app" / ".claude"
        nested.mkdir(parents=True)
        (nested / "settings.local.json").write_text('{"env": {"C": "d"}}')
        ws = _workspace(tmp_path)
        (ws / "host-config").symlink_to(nested)

        materialize_symlinks(ws, [project.resolve()])

        assert not (ws / "host-config").exists()
        assert not (ws / "host-config").is_symlink()
        assert "not copying 'host-config'" in capsys.readouterr().out

    def test_nested_claude_dir_reached_directly_skipped(self, tmp_path, capsys):
        """A ``.claude`` directory inside a followed tree (no link to it) is
        skipped as well."""
        project = tmp_path / "project"
        nested = project / "components" / "app" / ".claude"
        nested.mkdir(parents=True)
        (nested / "settings.local.json").write_text('{"env": {"C": "d"}}')
        (project / "components" / "app" / "main.py").write_text("print(1)")
        ws = _workspace(tmp_path)
        (ws / "components").symlink_to(project / "components")

        materialize_symlinks(ws, [project.resolve()])

        assert (ws / "components" / "app" / "main.py").is_file()
        assert not (ws / "components" / "app" / ".claude").exists()
        assert not list(ws.rglob("settings.local.json"))
        assert "not copying '.claude'" in capsys.readouterr().out

    def test_directory_link_to_project_claude_uploads_no_settings(self, tmp_path):
        project = tmp_path / "project"
        (project / ".claude").mkdir(parents=True)
        (project / ".claude" / "settings.json").write_text('{"env": {"A": "b"}}')
        (project / ".claude" / "settings.local.json").write_text('{"env": {"C": "d"}}')
        skill = project / "skills" / "x"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# x")
        (skill / "host-config").symlink_to(Path("..") / ".." / ".claude")
        ws = _workspace(tmp_path)
        (ws / "skills").symlink_to(project / "skills")

        materialize_symlinks(ws, [project.resolve()])

        assert (ws / "skills" / "x" / "SKILL.md").is_file()
        assert not list(ws.rglob("settings.json"))
        assert not list(ws.rglob("settings.local.json"))

    def test_top_level_link_to_project_root_skips_settings(self, tmp_path):
        project = tmp_path / "project"
        (project / ".claude" / "skills").mkdir(parents=True)
        (project / ".claude" / "skills" / "s.md").write_text("s")
        (project / ".claude" / "settings.json").write_text('{"env": {"A": "b"}}')
        (project / ".claude" / "settings.local.json").write_text('{"env": {"C": "d"}}')
        (project / "README.md").write_text("r")
        ws = _workspace(tmp_path)
        (ws / "proj").symlink_to(project)

        materialize_symlinks(ws, [project.resolve()])

        assert (ws / "proj" / "README.md").is_file()
        assert not (ws / "proj" / ".claude").exists()
        assert not list(ws.rglob("settings.json"))
        assert not list(ws.rglob("settings.local.json"))

    def test_claude_dir_link_to_differently_named_directory_excluded(self, tmp_path):
        project = tmp_path / "project"
        (project / "cfg").mkdir(parents=True)
        (project / "cfg" / "settings.json").write_text('{"env": {"A": "b"}}')
        (project / "cfg" / "keep.txt").write_text("k")
        (project / ".claude").symlink_to("cfg")
        (project / "README.md").write_text("r")
        ws = _workspace(tmp_path)
        (ws / "proj").symlink_to(project)
        (ws / "direct").symlink_to(project / "cfg")

        materialize_symlinks(ws, [project.resolve()])

        assert (ws / "proj" / "README.md").is_file()
        assert not (ws / "proj" / ".claude").exists()
        assert not list(ws.rglob("settings.json"))
        assert not (ws / "direct").exists() and not (ws / "direct").is_symlink()

    def test_settings_local_link_to_other_file_excluded(self, tmp_path):
        project = tmp_path / "project"
        (project / ".claude").mkdir(parents=True)
        (project / "private").mkdir()
        (project / "private" / "local.json").write_text('{"env": {"C": "d"}}')
        (project / ".claude" / "settings.local.json").symlink_to(
            Path("..") / "private" / "local.json")
        (project / "README.md").write_text("r")
        ws = _workspace(tmp_path)
        (ws / "proj").symlink_to(project)
        (ws / "local-copy").symlink_to(project / "private" / "local.json")

        materialize_symlinks(ws, [project.resolve()])

        assert (ws / "proj" / "README.md").is_file()
        assert not list(ws.rglob("settings.local.json"))
        assert not (ws / "local-copy").exists() and not (ws / "local-copy").is_symlink()
        assert not (ws / "proj" / "private" / "local.json").exists()

    def test_nested_link_to_workspace_canonical_settings_dropped(self, tmp_path):
        """The canonical-file exception covers only a top-level link."""
        project = tmp_path / "project"
        (project / "skills" / "x").mkdir(parents=True)
        (project / "skills" / "x" / "SKILL.md").write_text("# x")
        ws = _workspace(tmp_path)
        (ws / ".claude").mkdir()
        (ws / ".claude" / "settings.json").write_text(json.dumps({"model": "opus"}))
        (project / "skills" / "x" / "own.json").symlink_to(
            ws / ".claude" / "settings.json")
        (ws / "skills").symlink_to(project / "skills")
        (ws / "backup.json").symlink_to(Path(".claude") / "settings.json")

        materialize_symlinks(ws, [project.resolve(), ws.resolve()])

        assert (ws / "skills" / "x" / "SKILL.md").is_file()
        assert not (ws / "skills" / "x" / "own.json").exists()
        assert json.loads((ws / "backup.json").read_text()) == {"model": "opus"}

    def test_alias_to_nested_settings_target_excluded(self, tmp_path, capsys):
        """A link to the file a nested ``.claude/settings.local.json`` link
        points at is skipped like a direct link to that settings file."""
        project = tmp_path / "project"
        (project / "components" / "app" / ".claude").mkdir(parents=True)
        (project / "components" / "app" / "private.json").write_text('{"env": {"K": "v"}}')
        (project / "components" / "app" / ".claude" / "settings.local.json").symlink_to(
            Path("..") / "private.json")
        (project / "skills" / "x").mkdir(parents=True)
        (project / "skills" / "x" / "SKILL.md").write_text("# x")
        (project / "skills" / "x" / "host.json").symlink_to(
            Path("..") / ".." / "components" / "app" / "private.json")
        ws = _workspace(tmp_path)
        (ws / "skills").symlink_to(project / "skills")

        materialize_symlinks(ws, [project.resolve()])

        assert (ws / "skills" / "x" / "SKILL.md").is_file()
        assert not (ws / "skills" / "x" / "host.json").exists()
        assert "host.json" in capsys.readouterr().out

    def _linked_claude_project(self, tmp_path):
        project = tmp_path / "project"
        app = project / "components" / "app"
        (app / "cfg").mkdir(parents=True)
        (app / "cfg" / "settings.local.json").write_text('{"env": {"K": "literal"}}')
        (app / ".claude").symlink_to("cfg")
        (project / "skills" / "x").mkdir(parents=True)
        (project / "skills" / "x" / "SKILL.md").write_text("# x")
        return project

    def test_link_to_settings_file_behind_linked_claude_dir_skipped(
            self, tmp_path, capsys):
        project = self._linked_claude_project(tmp_path)
        (project / "skills" / "x" / "host.json").symlink_to(
            Path("..") / ".." / "components" / "app" / "cfg" / "settings.local.json")
        ws = _workspace(tmp_path)
        (ws / "skills").symlink_to(project / "skills")

        materialize_symlinks(ws, [project.resolve()])

        assert (ws / "skills" / "x" / "SKILL.md").is_file()
        assert not (ws / "skills" / "x" / "host.json").exists()
        out = capsys.readouterr().out
        assert "host.json" in out
        assert "settings file" in out

    def test_link_to_dir_behind_linked_claude_dir_skipped_as_container(
            self, tmp_path, capsys):
        project = self._linked_claude_project(tmp_path)
        (project / "skills" / "x" / "cfgdir").symlink_to(
            Path("..") / ".." / "components" / "app" / "cfg")
        ws = _workspace(tmp_path)
        (ws / "skills").symlink_to(project / "skills")

        materialize_symlinks(ws, [project.resolve()])

        assert (ws / "skills" / "x" / "SKILL.md").is_file()
        assert not (ws / "skills" / "x" / "cfgdir").exists()
        assert "cfgdir" in capsys.readouterr().out


def _run_script(script_name, args, cwd, env):
    cmd = [sys.executable, str(SCRIPTS / script_name)] + args
    return subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd), env=env)


def test_execute_py_batch_end_to_end(tmp_path, fake_bin):
    """workspace.py → execute.py → collect.py with the fake binary on PATH."""
    project = tmp_path / "project"
    for case_id in ("case-001", "case-002"):
        (project / "cases" / case_id).mkdir(parents=True)
        (project / "cases" / case_id / "input.yaml").write_text(
            yaml.dump({"prompt": f"idea from {case_id}"}))
    (project / "scripts").mkdir()
    (project / "scripts" / "helper.sh").write_text("echo hi")
    (project / ".claude" / "skills" / "rfe.speedrun").mkdir(parents=True)
    (project / ".claude" / "skills" / "rfe.speedrun" / "SKILL.md").write_text("# s")
    (project / ".fullsend").mkdir()
    (project / ".fullsend" / "config.yaml").write_text("agents: []\n")
    (project / "eval.yaml").write_text(yaml.dump({
        "name": "fullsend-batch",
        "execution": {"mode": "batch", "skill": "rfe.speedrun",
                      "arguments": "--input batch.yaml --headless",
                      "env": {"EVAL_FLAG": "on"}},
        "runner": {"type": "fullsend", "agent": "rfe-creator-eval"},
        "models": {"skill": "opus"},
        "dataset": {"path": "cases", "schema": "Each case has a prompt"},
        "outputs": [{"path": "artifacts", "schema": "Reports",
                     "batch_pattern": "report-{n}.md"}],
    }))

    record = tmp_path / "record.json"
    runs_dir = tmp_path / "runs"
    env = {k: v for k, v in os.environ.items()
           if k not in ("_AGENT_EVAL_BOOTSTRAP_DONE", "FAKE_FULLSEND_EXIT",
                        "FAKE_FULLSEND_SLEEP")}
    env.update({
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        "AGENT_EVAL_RUNS_DIR": str(runs_dir),
        "FAKE_FULLSEND_RECORD": str(record),
        "FAKE_FULLSEND_ARTIFACTS": "2",
    })

    ws_result = _run_script("workspace.py", ["--config", "eval.yaml", "--run-id", "r1"],
                            cwd=project, env=env)
    assert ws_result.returncode == 0, ws_result.stderr
    workspace = next(Path(line.split(":", 1)[1].strip())
                     for line in ws_result.stdout.splitlines()
                     if line.startswith("WORKSPACE:"))
    assert (workspace / "scripts").is_symlink()

    output_dir = runs_dir / "fullsend-batch" / "r1"
    output_dir.mkdir(parents=True)
    exec_result = _run_script("execute.py", [
        "--workspace", str(workspace), "--model", "opus",
        "--output", str(output_dir), "--config", str(project / "eval.yaml"),
        "--run-id", "r1",
    ], cwd=project, env=env)
    assert exec_result.returncode == 0, exec_result.stderr

    rec = json.loads(record.read_text())
    assert rec["argv"][:2] == ["run", "rfe-creator-eval"]
    assert rec["symlinks"] == []
    envf = rec["env"]
    assert envf["FULLSEND_TASK"] == "/rfe.speedrun --input batch.yaml --headless"
    assert envf["EVAL_FLAG"] == "on"

    run_result = json.loads((output_dir / "run_result.json").read_text())
    assert run_result["agent"] == "fullsend"
    assert run_result["exit_code"] == 0
    assert run_result["cost_usd"] == 0.42
    assert run_result["num_turns"] == 7
    assert run_result["model"] == "claude-opus-4-6"
    params = run_result["eval_params"]
    assert params["fullsend_agent"] == "rfe-creator-eval"
    assert params["fullsend_version"] == "0.49.0"
    assert params["sandbox_image"] == "ghcr.io/fullsend-ai/sandbox@sha256:" + "a" * 64
    assert params["sandbox_image_source"] == "fullsend-output"
    assert params["sandbox_arch"] == "arm64"
    # Keys always present for fullsend, null when the tool/checkout is absent.
    assert "openshell_version" in params
    assert "agent_harness_commit" in params
    (run_dir,) = _run_dirs(workspace)
    stream = (run_dir / "iteration-1" / "output.jsonl").read_text()
    assert (output_dir / "stdout.log").read_text() == stream
    assert (output_dir / "subagents" / "s1-agent-a1.jsonl").is_file()

    collect = _run_script("collect.py", [
        "--config", str(project / "eval.yaml"), "--workspace", str(workspace),
        "--output", str(output_dir)], cwd=project, env=env)
    assert collect.returncode == 0, collect.stderr
    for n, case_id in enumerate(("case-001", "case-002"), start=1):
        report = output_dir / "cases" / case_id / "artifacts" / f"report-{n}.md"
        assert report.read_text() == f"report {n}"


# ── Run provenance ───────────────────────────────────────────────────

_IMAGE = "ghcr.io/fullsend-ai/sandbox@sha256:" + "a" * 64


def _git_init(path):
    git = ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.com",
           "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "init", "--allow-empty"], check=True)
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


def _no_openshell(monkeypatch):
    real_which = fullsend_mod.shutil.which
    monkeypatch.setattr(fullsend_mod.shutil, "which",
                        lambda name, *a, **k: None if name == "openshell"
                        else real_which(name, *a, **k))


class TestParseSandboxImage:
    def test_image_and_arch_lines(self):
        console = f"-> Running agent: a\n  Image: {_IMAGE}\n  Arch: arm64\n"
        assert fullsend_mod.parse_sandbox_image(console) == (_IMAGE, "arm64")

    def test_arch_on_the_image_line_and_ansi_codes(self):
        console = f"\x1b[32m✓\x1b[0m Image: {_IMAGE} (amd64)\n"
        assert fullsend_mod.parse_sandbox_image(console) == (_IMAGE, "amd64")

    def test_first_image_line_wins(self):
        console = f"Image: {_IMAGE}\nImage: other:latest\n"
        assert fullsend_mod.parse_sandbox_image(console)[0] == _IMAGE

    def test_absent(self):
        console = ("Image override via FULLSEND_SANDBOX_IMAGE: a -> b\n"
                   "WARNING: FULLSEND_SANDBOX_ARCH=\"x\" is not a supported "
                   "architecture (amd64, arm64), using host arch amd64\n")
        assert fullsend_mod.parse_sandbox_image(console) == (None, None)


class TestProbeVersion:
    def test_falls_back_to_version_flag(self, fake_bin, fake_fullsend):
        """The real CLI has no `version` subcommand; `--version` answers
        `fullsend version <v>`."""
        assert fullsend_mod.probe_version("fullsend", ("fullsend",)) == "0.49.0"

    def test_version_subcommand_is_an_unknown_command(self, fake_bin, fake_fullsend):
        proc = subprocess.run(["fullsend", "version"], capture_output=True, text=True,
                              check=False)
        assert proc.returncode == 1
        assert "unknown command" in proc.stderr

    def test_missing_binary_is_none(self, tmp_path):
        assert fullsend_mod.probe_version(str(tmp_path / "nope"), ("fullsend",)) is None
        assert fullsend_mod.probe_version(None) is None


class TestProvenance:
    def test_fields_from_console_output(self, tmp_path, fake_fullsend, monkeypatch):
        _no_openshell(monkeypatch)
        runner = _runner(tmp_path)
        ws = _workspace(tmp_path)
        result = runner.execute("rfe.speedrun", "", ws, "opus")
        prov = runner.provenance([result.stderr])
        assert set(prov) == set(fullsend_mod.PROVENANCE_KEYS)
        assert prov["fullsend_agent"] == "rfe-creator-eval"
        assert prov["fullsend_version"] == "0.49.0"
        assert prov["sandbox_image"] == _IMAGE
        assert prov["sandbox_image_source"] == "fullsend-output"
        assert prov["sandbox_arch"] == "arm64"
        assert prov["openshell_version"] is None

    def test_console_image_overrides_harness_file(self, tmp_path, fake_fullsend,
                                                  monkeypatch):
        """An Image: line (e.g. after FULLSEND_SANDBOX_IMAGE) beats the pin."""
        runner = _runner(tmp_path)
        harness = Path(runner._fullsend_dir) / "harness"
        harness.mkdir()
        (harness / "rfe-creator-eval.yaml").write_text("image: pinned:1\n")
        monkeypatch.setenv("FAKE_FULLSEND_IMAGE", "override:2")
        result = runner.execute("rfe.speedrun", "", _workspace(tmp_path), "opus")
        prov = runner.provenance([result.stderr])
        assert prov["sandbox_image"] == "override:2"
        assert prov["sandbox_image_source"] == "fullsend-output"

    def test_harness_file_fallback(self, tmp_path, fake_fullsend, monkeypatch, capsys):
        runner = _runner(tmp_path)
        harness = Path(runner._fullsend_dir) / "harness"
        harness.mkdir()
        (harness / "rfe-creator-eval.yaml").write_text(
            "agent: agents/rfe.md\nimage: ghcr.io/x/sandbox@sha256:" + "b" * 64 + "\n")
        monkeypatch.setenv("FAKE_FULLSEND_NO_IMAGE", "1")
        result = runner.execute("rfe.speedrun", "", _workspace(tmp_path), "opus")
        prov = runner.provenance([result.stderr])
        assert prov["sandbox_image"] == "ghcr.io/x/sandbox@sha256:" + "b" * 64
        assert prov["sandbox_image_source"] == "harness-file"
        assert prov["sandbox_arch"] is None
        assert "harness file" in capsys.readouterr().out

    def test_registered_source_harness_file_without_harness_dir(
            self, tmp_path, fake_fullsend, monkeypatch):
        """A per-repo install registers the agent under agents[].source and has
        no harness/ directory; the image comes from that file."""
        monkeypatch.setenv("FAKE_FULLSEND_NO_IMAGE", "1")
        runner = _runner(tmp_path)
        fs_dir = Path(runner._fullsend_dir)
        (fs_dir / "sub").mkdir()
        (fs_dir / "sub" / "agent.yaml").write_text(
            "agent: agents/a.md\nimage: ghcr.io/x/sandbox@sha256:" + "c" * 64 + "\n")
        (fs_dir / "config.yaml").write_text(yaml.dump({"agents": [
            {"name": "code", "model": "opus"},
            {"source": "sub/agent.yaml#v1", "name": "Rfe-Creator-Eval"}]}))
        assert not (fs_dir / "harness").exists()
        result = runner.execute("rfe.speedrun", "", _workspace(tmp_path), "opus")
        assert "Image:" not in result.stderr
        prov = runner.provenance([result.stderr])
        assert prov["sandbox_image"] == "ghcr.io/x/sandbox@sha256:" + "c" * 64
        assert prov["sandbox_image_source"] == "harness-file"

    def test_registered_source_matches_stem_and_last_enabled_entry(self, tmp_path):
        fs_dir = Path(_runner(tmp_path)._fullsend_dir)
        for name, image in (("first", "one:1"), ("second", "two:2"), ("third", "three:3")):
            (fs_dir / name).mkdir()
            (fs_dir / name / "rfe-creator-eval.yaml").write_text(f"image: {image}\n")
        (fs_dir / "config.yaml").write_text(yaml.dump({"agents": [
            {"source": "first/rfe-creator-eval.yaml"},
            {"source": "second/RFE-Creator-Eval.yaml"},
            {"source": "third/rfe-creator-eval.yaml", "enabled": False}]}))
        (fs_dir / "second" / "RFE-Creator-Eval.yaml").write_text("image: two:2\n")
        assert fullsend_mod.harness_file_image(fs_dir, "rfe-creator-eval") == "two:2"

    @pytest.mark.parametrize("entry", [
        {"source": "https://example.com/org/repo/rfe-creator-eval.yaml"},
        {"name": "rfe-creator-eval", "model": "opus"},
        {"source": "../outside/rfe-creator-eval.yaml"},
        {"source": "/abs/rfe-creator-eval.yaml"},
    ])
    def test_url_override_only_and_escaping_entries_have_no_image(
            self, tmp_path, entry):
        """A matching entry decides the answer: harness/<agent>.yaml is not
        consulted when fullsend would not load it."""
        fs_dir = Path(_runner(tmp_path)._fullsend_dir)
        (fs_dir / "harness").mkdir()
        (fs_dir / "harness" / "rfe-creator-eval.yaml").write_text("image: pinned:1\n")
        (fs_dir.parent / "outside").mkdir()
        (fs_dir.parent / "outside" / "rfe-creator-eval.yaml").write_text("image: out:1\n")
        (fs_dir / "config.yaml").write_text(yaml.dump({"agents": [entry]}))
        assert fullsend_mod.harness_file_image(fs_dir, "rfe-creator-eval") is None

    def test_unregistered_agent_falls_back_to_harness_dir(self, tmp_path):
        fs_dir = Path(_runner(tmp_path)._fullsend_dir)
        (fs_dir / "harness").mkdir()
        (fs_dir / "harness" / "rfe-creator-eval.yaml").write_text("image: pinned:1\n")
        (fs_dir / "config.yaml").write_text(yaml.dump({"agents": [{"name": "code"}]}))
        assert fullsend_mod.harness_file_image(fs_dir, "rfe-creator-eval") == "pinned:1"

    def test_config_yaml_symlinked_outside_is_not_read(self, tmp_path):
        """config.yaml that resolves outside fullsend_dir is ignored like an
        escaping source: the harness/ fallback answers instead."""
        fs_dir = Path(_runner(tmp_path)._fullsend_dir)
        (fs_dir / "harness").mkdir()
        (fs_dir / "harness" / "rfe-creator-eval.yaml").write_text("image: pinned:1\n")
        (fs_dir / "sub").mkdir()
        (fs_dir / "sub" / "agent.yaml").write_text("image: registered:1\n")
        outside = tmp_path / "outside-config.yaml"
        outside.write_text(yaml.dump({"agents": [
            {"name": "rfe-creator-eval", "source": "sub/agent.yaml"}]}))
        (fs_dir / "config.yaml").symlink_to(outside)
        assert fullsend_mod.harness_file_image(fs_dir, "rfe-creator-eval") == "pinned:1"

    @pytest.mark.parametrize("suffix", [".yaml", ".yml"])
    def test_legacy_harness_file_symlinked_outside_is_not_read(self, tmp_path, suffix):
        fs_dir = Path(_runner(tmp_path)._fullsend_dir)
        (fs_dir / "harness").mkdir()
        outside = tmp_path / "outside-harness.yaml"
        outside.write_text("image: out:1\n")
        (fs_dir / "harness" / f"rfe-creator-eval{suffix}").symlink_to(outside)
        assert fullsend_mod.harness_file_image(fs_dir, "rfe-creator-eval") is None

    def test_legacy_harness_file_symlinked_inside_is_read(self, tmp_path):
        fs_dir = Path(_runner(tmp_path)._fullsend_dir)
        (fs_dir / "harness").mkdir()
        (fs_dir / "shared.yaml").write_text("image: shared:1\n")
        (fs_dir / "harness" / "rfe-creator-eval.yaml").symlink_to(fs_dir / "shared.yaml")
        assert fullsend_mod.harness_file_image(fs_dir, "rfe-creator-eval") == "shared:1"

    def test_child_harness_image_from_local_base(
            self, tmp_path, fake_fullsend, monkeypatch):
        """A child harness that leaves image: to its base: (resolved relative
        to the child file) records the base's pin as the harness-file image."""
        monkeypatch.setenv("FAKE_FULLSEND_NO_IMAGE", "1")
        runner = _runner(tmp_path)
        fs_dir = Path(runner._fullsend_dir)
        (fs_dir / "sub").mkdir()
        (fs_dir / "sub" / "rfe-creator.yaml").write_text(
            "agent: agents/rfe.md\nimage: ghcr.io/x/sandbox@sha256:" + "d" * 64 + "\n")
        (fs_dir / "sub" / "rfe-creator-eval.yaml").write_text(
            "base: rfe-creator.yaml\nagent: agents/rfe-eval.md\n")
        (fs_dir / "config.yaml").write_text(yaml.dump({"agents": [
            {"source": "sub/rfe-creator-eval.yaml"}]}))
        result = runner.execute("rfe.speedrun", "", _workspace(tmp_path), "opus")
        assert "Image:" not in result.stderr
        prov = runner.provenance([result.stderr])
        assert prov["sandbox_image"] == "ghcr.io/x/sandbox@sha256:" + "d" * 64
        assert prov["sandbox_image_source"] == "harness-file"

    @pytest.mark.parametrize("child_image, expected", [
        ("image: child:1\n", "child:1"),
        ("image: ''\n", "base:1"),
        ("image: '  '\n", "base:1"),
        ("", "base:1"),
    ], ids=["child-wins", "empty-falls-through", "blank-falls-through", "unset"])
    def test_base_chain_child_image_overrides_only_when_non_empty(
            self, tmp_path, child_image, expected):
        fs_dir = Path(_runner(tmp_path)._fullsend_dir)
        (fs_dir / "harness").mkdir()
        (fs_dir / "harness" / "base.yaml").write_text("image: base:1\n")
        (fs_dir / "harness" / "rfe-creator-eval.yaml").write_text(
            "base: base.yaml\n" + child_image)
        assert fullsend_mod.harness_file_image(fs_dir, "rfe-creator-eval") == expected

    def test_base_chain_three_levels_reaches_root_image(self, tmp_path):
        fs_dir = Path(_runner(tmp_path)._fullsend_dir)
        (fs_dir / "harness").mkdir()
        (fs_dir / "common").mkdir()
        (fs_dir / "common" / "root.yaml").write_text("image: root:1\n")
        (fs_dir / "harness" / "middle.yaml").write_text(
            "base: ../common/root.yaml\nimage: ''\n")
        (fs_dir / "harness" / "rfe-creator-eval.yaml").write_text("base: middle.yaml\n")
        assert fullsend_mod.harness_file_image(fs_dir, "rfe-creator-eval") == "root:1"

    def test_base_chain_cycle_is_none(self, tmp_path):
        fs_dir = Path(_runner(tmp_path)._fullsend_dir)
        (fs_dir / "harness").mkdir()
        (fs_dir / "harness" / "a.yaml").write_text("base: rfe-creator-eval.yaml\n")
        (fs_dir / "harness" / "rfe-creator-eval.yaml").write_text("base: a.yaml\n")
        assert fullsend_mod.harness_file_image(fs_dir, "rfe-creator-eval") is None

    def test_base_chain_self_reference_is_none(self, tmp_path):
        fs_dir = Path(_runner(tmp_path)._fullsend_dir)
        (fs_dir / "harness").mkdir()
        (fs_dir / "harness" / "rfe-creator-eval.yaml").write_text(
            "base: ./rfe-creator-eval.yaml\n")
        assert fullsend_mod.harness_file_image(fs_dir, "rfe-creator-eval") is None

    def test_base_chain_longer_than_bound_is_none(self, tmp_path):
        fs_dir = Path(_runner(tmp_path)._fullsend_dir)
        harness = fs_dir / "harness"
        harness.mkdir()
        depth = fullsend_mod._MAX_BASE_CHAIN
        (harness / "rfe-creator-eval.yaml").write_text("base: b1.yaml\n")
        for i in range(1, depth):
            (harness / f"b{i}.yaml").write_text(f"base: b{i + 1}.yaml\n")
        (harness / f"b{depth}.yaml").write_text("image: deep:1\n")
        assert fullsend_mod.harness_file_image(fs_dir, "rfe-creator-eval") is None
        (harness / f"b{depth - 1}.yaml").write_text("image: deep:1\n")
        assert fullsend_mod.harness_file_image(fs_dir, "rfe-creator-eval") == "deep:1"

    @pytest.mark.parametrize("base", [
        "https://example.com/org/repo/base.yaml",
        "../../outside/base.yaml",
        "ABSOLUTE",
        "missing.yaml",
        "broken.yaml",
        "[base.yaml]",
    ], ids=["url", "escaping", "absolute", "missing", "unparsable", "not-a-string"])
    def test_base_chain_unfollowable_base_is_none(self, tmp_path, base):
        fs_dir = Path(_runner(tmp_path)._fullsend_dir)
        (fs_dir / "harness").mkdir()
        (fs_dir.parent / "outside").mkdir()
        (fs_dir.parent / "outside" / "base.yaml").write_text("image: out:1\n")
        (fs_dir / "harness" / "broken.yaml").write_text("image: [unclosed\n")
        if base == "ABSOLUTE":
            base = str(fs_dir.parent / "outside" / "base.yaml")
        (fs_dir / "harness" / "rfe-creator-eval.yaml").write_text(f"base: {base}\n")
        assert fullsend_mod.harness_file_image(fs_dir, "rfe-creator-eval") is None

    @pytest.mark.parametrize("raises", [False, True], ids=["real-loop", "resolve-raises"])
    def test_looping_symlink_in_fullsend_dir_nulls_only_the_image(
            self, tmp_path, fake_fullsend, monkeypatch, capsys, raises):
        """A self-referential symlink under fullsend_dir must not escape
        provenance(): only sandbox_image is null; version fields survive.
        Path.resolve() raises RuntimeError/OSError on a loop before Python 3.13,
        so ``resolve-raises`` forces that for every Python."""
        _no_openshell(monkeypatch)
        monkeypatch.setenv("FAKE_FULLSEND_NO_IMAGE", "1")
        runner = _runner(tmp_path)
        fs_dir = Path(runner._fullsend_dir)
        loop = fs_dir / "loop.yaml"
        loop.symlink_to(loop)
        (fs_dir / "config.yaml").write_text(yaml.dump({"agents": [
            {"name": "rfe-creator-eval", "source": "loop.yaml"}]}))
        if raises:
            real_resolve = Path.resolve

            def resolve(self, *args, **kwargs):
                if self.name == "loop.yaml":
                    raise RuntimeError("Symlink loop from " + str(self))
                return real_resolve(self, *args, **kwargs)

            monkeypatch.setattr(Path, "resolve", resolve)
            assert fullsend_mod._contained(fs_dir, loop) is None
        result = runner.execute("rfe.speedrun", "", _workspace(tmp_path), "opus")
        assert "Image:" not in result.stderr
        prov = runner.provenance([result.stderr])
        assert prov["fullsend_agent"] == "rfe-creator-eval"
        assert prov["fullsend_version"] == "0.49.0"
        assert prov["sandbox_image"] is None
        assert prov["sandbox_image_source"] is None
        assert "sandbox_image is null" in capsys.readouterr().out

    def test_looping_symlink_keeps_other_fields_in_run_params(
            self, tmp_path, fake_fullsend, monkeypatch):
        import execute
        _no_openshell(monkeypatch)
        monkeypatch.setenv("FAKE_FULLSEND_NO_IMAGE", "1")
        runner = _runner(tmp_path)
        fs_dir = Path(runner._fullsend_dir)
        loop = fs_dir / "loop.yaml"
        loop.symlink_to(loop)
        (fs_dir / "config.yaml").write_text(yaml.dump({"agents": [
            {"name": "rfe-creator-eval", "source": "loop.yaml"}]}))
        real_resolve = Path.resolve

        def resolve(self, *args, **kwargs):
            if self.name == "loop.yaml":
                raise RuntimeError("Symlink loop from " + str(self))
            return real_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve)
        params = execute._with_runner_provenance(
            {"execution_mode": "case"}, runner, [])
        assert params["execution_mode"] == "case"
        assert params["fullsend_agent"] == "rfe-creator-eval"
        assert params["fullsend_version"] == "0.49.0"
        assert params["sandbox_image"] is None

    def test_unparseable_yaml_warning_names_path_not_parser_text(
            self, tmp_path, capsys):
        fs_dir = Path(_runner(tmp_path)._fullsend_dir)
        (fs_dir / "config.yaml").write_text(
            "agents: [SECRET-VALUE\n  - {unclosed: \"SECRET-VALUE\n")
        assert fullsend_mod.harness_file_image(fs_dir, "rfe-creator-eval") is None
        out = capsys.readouterr().out
        assert f"{fs_dir / 'config.yaml'} could not be parsed" in out
        assert "SECRET-VALUE" not in out
        assert "line" not in out.replace("could not be parsed", "")

    def test_everything_missing_is_null_with_warnings(self, tmp_path, monkeypatch,
                                                      capsys):
        _no_openshell(monkeypatch)
        runner = _runner(tmp_path, binary=str(tmp_path / "missing-fullsend"))
        prov = runner.provenance(["no image here", None])
        assert prov["fullsend_agent"] == "rfe-creator-eval"
        for key in ("fullsend_version", "openshell_version", "sandbox_image",
                    "sandbox_image_source", "sandbox_arch", "agent_harness_commit"):
            assert prov[key] is None, key
        out = capsys.readouterr().out
        assert "openshell is not on PATH" in out
        assert "not inside a git checkout" in out
        assert "sandbox_image is null" in out

    def test_openshell_version_and_harness_commit(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "osbin"
        bin_dir.mkdir()
        openshell = bin_dir / "openshell"
        openshell.write_text(f"#!{sys.executable}\nimport sys\n"
                             "sys.exit(2) if sys.argv[1:] == ['version'] "
                             "else print('openshell 0.0.36')\n")
        openshell.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        runner = _runner(tmp_path)
        sha = _git_init(tmp_path / "project")
        prov = runner.provenance([])
        assert prov["openshell_version"] == "0.0.36"
        assert prov["agent_harness_commit"] == sha


class TestProvenanceUsesInvocationEnvironment:
    def test_runner_env_path_selects_the_probed_installs(
            self, tmp_path, fake_fullsend, monkeypatch):
        """runner.env.PATH pins a different fullsend/OpenShell than the
        caller's PATH; provenance records the pinned ones."""
        pinned = tmp_path / "pinned"
        pinned.mkdir()
        for name, text in (("fullsend", "fullsend version 9.9.9"),
                           ("openshell", "openshell 7.7.7")):
            exe = pinned / name
            exe.write_text(f"#!{sys.executable}\nimport sys\n"
                           f"print({text!r}) if sys.argv[1:] == ['version'] "
                           "else sys.exit(2)\n")
            exe.chmod(0o755)
        # The caller's PATH has the fake fullsend (0.49.0), not these installs.
        runner = _runner(tmp_path, runner_env={"PATH": str(pinned)})
        prov = runner.provenance([])
        assert prov["fullsend_version"] == "9.9.9"
        assert prov["openshell_version"] == "7.7.7"

    def test_without_override_the_caller_path_is_used(self, tmp_path, fake_fullsend):
        assert _runner(tmp_path).provenance([])["fullsend_version"] == "0.49.0"


class TestCaseConsoleLogs:
    def test_multi_step_case_collects_every_step_log(self, tmp_path):
        """The case-root stderr.log holds only the final step; an earlier
        fullsend step's Image: line (an override) must still be found."""
        import execute
        case_dir = tmp_path / "cases" / "case-001"
        for step, text in (("a", f"Image: override:2\nArch: arm64\n"),
                           ("b", "no image line here\n")):
            (case_dir / "steps" / step).mkdir(parents=True)
            (case_dir / "steps" / step / "stderr.log").write_text(text)
        (case_dir / "stderr.log").write_text("no image line here\n")
        texts = execute._case_console_logs(
            tmp_path, {"case-001": {"steps": {"a": {}, "b": {}}}})
        assert len(texts) == 2
        assert fullsend_mod.parse_sandbox_image(texts[0]) == ("override:2", "arm64")

    def test_single_step_case_reads_root_log(self, tmp_path):
        import execute
        case_dir = tmp_path / "cases" / "case-001"
        case_dir.mkdir(parents=True)
        (case_dir / "stderr.log").write_text("Image: x:1\n")
        assert execute._case_console_logs(
            tmp_path, {"case-001": {"exit_code": 0}}) == ["Image: x:1\n"]

    def test_provenance_keeps_override_from_earlier_step(
            self, tmp_path, fake_fullsend, monkeypatch):
        import execute
        case_dir = tmp_path / "out" / "cases" / "case-001"
        for step, text in (("fs", "Image: override:2\n"), ("other", "plain\n")):
            (case_dir / "steps" / step).mkdir(parents=True)
            (case_dir / "steps" / step / "stderr.log").write_text(text)
        (case_dir / "stderr.log").write_text("plain\n")
        texts = execute._case_console_logs(
            tmp_path / "out", {"case-001": {"steps": {"fs": {}, "other": {}}}})
        prov = _runner(tmp_path).provenance(texts)
        assert prov["sandbox_image"] == "override:2"
        assert prov["sandbox_image_source"] == "fullsend-output"


def test_execute_py_case_mode_records_provenance_in_report(tmp_path, fake_bin):
    """A case-mode run writes the provenance fields into the run-level
    run_result.json, and the report's run header shows them."""
    project = tmp_path / "project"
    for case_id in ("case-001", "case-002"):
        (project / "cases" / case_id).mkdir(parents=True)
        (project / "cases" / case_id / "input.yaml").write_text(
            yaml.dump({"prompt": f"idea from {case_id}"}))
    (project / ".claude" / "skills" / "rfe.create").mkdir(parents=True)
    (project / ".claude" / "skills" / "rfe.create" / "SKILL.md").write_text("# s")
    (project / ".fullsend").mkdir()
    (project / ".fullsend" / "config.yaml").write_text("agents: []\n")
    (project / "eval.yaml").write_text(yaml.dump({
        "name": "fullsend-case",
        "execution": {"mode": "case", "skill": "rfe.create",
                      "arguments": "{prompt}"},
        "runner": {"type": "fullsend", "agent": "rfe-creator-eval"},
        "models": {"skill": "opus"},
        "dataset": {"path": "cases", "schema": "Each case has a prompt"},
        "outputs": [{"path": "artifacts", "schema": "Reports"}],
    }))
    sha = _git_init(project)

    runs_dir = tmp_path / "runs"
    env = {k: v for k, v in os.environ.items()
           if k not in ("_AGENT_EVAL_BOOTSTRAP_DONE", "FAKE_FULLSEND_EXIT",
                        "FAKE_FULLSEND_SLEEP", "FAKE_FULLSEND_NO_IMAGE",
                        "FAKE_FULLSEND_IMAGE")}
    env.update({
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        "AGENT_EVAL_RUNS_DIR": str(runs_dir),
        "FAKE_FULLSEND_RECORD": str(tmp_path / "record.json"),
    })
    ws_result = _run_script("workspace.py", ["--config", "eval.yaml", "--run-id", "r1"],
                            cwd=project, env=env)
    assert ws_result.returncode == 0, ws_result.stderr
    workspace = next(Path(line.split(":", 1)[1].strip())
                     for line in ws_result.stdout.splitlines()
                     if line.startswith("WORKSPACE:"))
    output_dir = runs_dir / "fullsend-case" / "r1"
    output_dir.mkdir(parents=True)
    exec_result = _run_script("execute.py", [
        "--workspace", str(workspace), "--model", "opus",
        "--output", str(output_dir), "--config", str(project / "eval.yaml"),
        "--run-id", "r1",
    ], cwd=project, env=env)
    assert exec_result.returncode == 0, exec_result.stderr + exec_result.stdout

    run_result = json.loads((output_dir / "run_result.json").read_text())
    params = run_result["eval_params"]
    assert params["fullsend_agent"] == "rfe-creator-eval"
    assert params["fullsend_version"] == "0.49.0"
    assert params["sandbox_image"] == _IMAGE
    assert params["sandbox_image_source"] == "fullsend-output"
    assert params["sandbox_arch"] == "arm64"
    assert params["agent_harness_commit"] == sha
    assert "openshell_version" in params
    # Run-level only: per-case results do not carry the provenance.
    for case in run_result["per_case"].values():
        assert "sandbox_image" not in case

    from report import _render_run_config
    html = _render_run_config(run_result)
    assert "<dt>fullsend Version</dt><dd>0.49.0</dd>" in html
    assert f"<dt>Sandbox Image</dt><dd>{_IMAGE}</dd>" in html
    assert "<dt>Sandbox Image Source</dt><dd>fullsend-output</dd>" in html
    assert f"<dt>Agent Harness Commit</dt><dd>{sha}</dd>" in html
    assert "<dt>fullsend Agent</dt><dd>rfe-creator-eval</dd>" in html
    assert "<dt>OpenShell Version</dt>" in html


class TestProvenanceNeverFailsTheRun:
    class _Runner:
        name = "fullsend"

        def provenance(self, console_texts):
            return {"fullsend_agent": "a", "texts": len(console_texts)}

    def test_log_collection_failure_is_guarded(self, capsys):
        import execute

        def boom():
            raise PermissionError("unsearchable case directory")

        params = execute._with_runner_provenance({"execution_mode": "case"},
                                                 self._Runner(), boom)
        assert params == {"execution_mode": "case"}
        assert "could not record fullsend provenance" in capsys.readouterr().err

    @pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0,
                        reason="permission bits do not restrict root")
    def test_unreadable_case_dir_still_yields_params(self, tmp_path):
        import execute
        case_dir = tmp_path / "cases" / "case-001"
        case_dir.mkdir(parents=True)
        (case_dir / "stderr.log").write_text("Image: x:1\n")
        case_dir.chmod(0o000)
        try:
            params = execute._with_runner_provenance(
                {"execution_mode": "case"}, self._Runner(),
                lambda: execute._case_console_logs(tmp_path, {"case-001": {}}))
        finally:
            case_dir.chmod(0o755)
        assert params["execution_mode"] == "case"

    def test_log_collection_skipped_without_provenance(self):
        import execute

        class _Plain:
            name = "claude-code"

        def boom():
            raise AssertionError("logs must not be collected")

        assert execute._with_runner_provenance({"a": 1}, _Plain(), boom) == {"a": 1}

    def test_callable_texts_are_passed_to_provenance(self):
        import execute
        params = execute._with_runner_provenance({}, self._Runner(), lambda: ["x", "y"])
        assert params["texts"] == 2


class TestProvenanceKeysOnRunnerAbstraction:
    def test_only_fullsend_declares_keys(self):
        assert EvalRunner.PROVENANCE_KEYS == ()
        assert RUNNERS["fullsend"].PROVENANCE_KEYS == fullsend_mod.PROVENANCE_KEYS
        for name, cls in RUNNERS.items():
            if name != "fullsend":
                assert cls.PROVENANCE_KEYS == (), name


class TestOtherRunnersUnchanged:
    def test_runner_without_provenance_adds_no_keys(self):
        import execute

        class _Runner:
            name = "claude-code"

        params = {"execution_mode": "case"}
        merged = execute._with_runner_provenance(params, _Runner(), ["Image: x:1"])
        assert merged == params
        assert not set(fullsend_mod.PROVENANCE_KEYS) & set(merged)

    def test_report_header_has_no_fullsend_rows(self):
        from report import _render_run_config
        html = _render_run_config({"model": "m", "agent": "claude-code",
                                   "eval_params": {"execution_mode": "case"}})
        for label in ("fullsend Version", "OpenShell Version", "Sandbox Image",
                      "Agent Harness Commit", "fullsend Agent"):
            assert label not in html

    def test_report_header_shows_recorded_null_as_dash(self):
        from report import _render_run_config
        html = _render_run_config({"model": "m", "eval_params": {
            "fullsend_agent": "a", "openshell_version": None}})
        assert "<dt>OpenShell Version</dt><dd>—</dd>" in html


# ── Review hardening ─────────────────────────────────────────────────

def _assistant(msg_id, model="claude-opus-4-6", inp=1, out=1):
    return {"type": "assistant", "message": {
        "id": msg_id, "model": model, "role": "assistant",
        "content": [{"type": "text", "text": "x"}],
        "usage": {"input_tokens": inp, "output_tokens": out}}}


def _result(cost, model_usage):
    return {"type": "result", "subtype": "success", "total_cost_usd": cost,
            "modelUsage": model_usage}


def _write_iteration(run, number, events):
    it = run / f"iteration-{number}"
    it.mkdir(parents=True)
    (it / "output.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return it


def _killpg_permitted():
    """False in sandboxes that refuse group signalling (the helper then falls
    back to signalling the leader only)."""
    probe = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             start_new_session=True)
    try:
        os.killpg(probe.pid, 0)
        return True
    except OSError:
        return False
    finally:
        probe.kill()
        probe.wait()


def _alive(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


class TestPerIterationUsage:
    def test_two_completed_iterations_are_summed(self, tmp_path):
        ws = _workspace(tmp_path)
        run = ws / ".fullsend-runs" / "fs-sandbox-1"
        _write_iteration(run, 1, [
            _assistant("m1"),
            _result(0.10, {"claude-opus-4-6": {
                "inputTokens": 100, "outputTokens": 50,
                "cacheReadInputTokens": 7, "costUSD": 0.10}})])
        _write_iteration(run, 2, [
            _assistant("m2", "claude-haiku-4-5"),
            _result(0.20, {
                "claude-opus-4-6": {"inputTokens": 10, "outputTokens": 5, "costUSD": 0.15},
                "claude-haiku-4-5": {"inputTokens": 3, "outputTokens": 2, "costUSD": 0.05}})])
        result = _runner(tmp_path)._map_run_dir(
            ws / ".fullsend-runs", ws, exit_code=0, stderr="", duration=1.0)
        assert result.cost_usd == pytest.approx(0.30)
        assert result.token_usage == {
            "input": 113, "output": 57, "cache_read": 7, "cache_create": 0}
        opus = result.per_model_usage["claude-opus-4-6"]
        assert (opus["input"], opus["output"]) == (110, 55)
        assert opus["cost_usd"] == pytest.approx(0.25)
        assert result.per_model_usage["claude-haiku-4-5"]["cost_usd"] == pytest.approx(0.05)
        assert result.num_turns == 2
        assert result.models_used == ["claude-haiku-4-5", "claude-opus-4-6"]

    def test_timed_out_partial_iteration_still_counts_its_tokens(self, tmp_path):
        ws = _workspace(tmp_path)
        run = ws / ".fullsend-runs" / "fs-sandbox-1"
        _write_iteration(run, 1, [
            _assistant("m1"),
            _result(0.10, {"claude-opus-4-6": {
                "inputTokens": 100, "outputTokens": 50, "costUSD": 0.10}})])
        # The later iteration never reached a result event.
        _write_iteration(run, 2, [_assistant("m2", inp=4, out=2)])
        result = _runner(tmp_path)._map_run_dir(
            ws / ".fullsend-runs", ws, exit_code=-1, stderr="", duration=1.0)
        assert result.cost_usd == pytest.approx(0.10)
        assert result.token_usage["input"] == 104
        assert result.token_usage["output"] == 52
        assert result.per_model_usage["claude-opus-4-6"]["input"] == 100
        assert result.num_turns == 2


class TestInterruptCleanup:
    def test_keyboard_interrupt_stops_group_and_reraises(self, tmp_path, monkeypatch):
        ws = _workspace(tmp_path)
        pidfile = tmp_path / "pids.json"
        code = (
            "import json, os, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"open({str(pidfile)!r}, 'w').write(json.dumps([os.getpid(), child.pid]))\n"
            "time.sleep(60)\n")

        real_popen = subprocess.Popen
        interrupted = []

        class InterruptingPopen(real_popen):
            def communicate(self, *args, **kwargs):
                if not interrupted:
                    interrupted.append(True)
                    deadline = time.monotonic() + 10
                    while not pidfile.exists() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    raise KeyboardInterrupt
                return super().communicate(*args, **kwargs)

        monkeypatch.setattr(fullsend_mod.subprocess, "Popen", InterruptingPopen)
        runner = _runner(tmp_path, term_grace_s=2)
        with pytest.raises(KeyboardInterrupt):
            runner._run([sys.executable, "-c", code], ws, dict(os.environ), 60)
        leader, child = json.loads(pidfile.read_text())
        deadline = time.monotonic() + 5
        while (_alive(leader) or _alive(child)) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(leader)
        if _killpg_permitted():
            assert not _alive(child)


class TestSettingsFailClosed:
    def test_malformed_settings_with_env_secret_refuses_the_run(
            self, tmp_path, fake_fullsend):
        ws = _workspace(tmp_path)
        (ws / ".claude").mkdir()
        (ws / ".claude" / "settings.json").write_text(
            '{"env": {"API_TOKEN": "literal-secret-value"}, ')
        result = _runner(tmp_path).execute(
            target="s", args="", workspace=ws, model="m", timeout_s=60)
        assert result.exit_code == -1
        assert "settings.json" in result.stderr
        assert "literal-secret-value" not in result.stderr
        assert not fake_fullsend.exists()  # fullsend never launched

    def test_symlinked_settings_file_refuses_the_run(self, tmp_path, fake_fullsend):
        """A symlinked canonical settings.json cannot be sanitised in place, and
        its materialised copy would be uploaded unsanitised: refuse the run."""
        ws = _workspace(tmp_path)
        (ws / ".claude").mkdir()
        secret = ws / "secret-settings.json"
        secret.write_text(json.dumps({"env": {"API_TOKEN": "literal-secret-value"}}))
        link = ws / ".claude" / "settings.json"
        link.symlink_to(Path("..") / "secret-settings.json")
        result = _runner(tmp_path).execute(
            target="s", args="", workspace=ws, model="m", timeout_s=60)
        assert result.exit_code == -1
        assert "settings.json" in result.stderr
        assert "literal-secret-value" not in result.stderr
        assert not fake_fullsend.exists()  # fullsend never launched
        assert link.is_symlink()  # the link and its target are left alone
        assert json.loads(secret.read_text()) == {
            "env": {"API_TOKEN": "literal-secret-value"}}

    def test_symlinked_claude_dir_refuses_the_run(self, tmp_path, fake_fullsend):
        ws = _workspace(tmp_path)
        real = ws / "claude-elsewhere"
        real.mkdir()
        (real / "settings.json").write_text(
            json.dumps({"env": {"API_TOKEN": "literal-secret-value"}}))
        (ws / ".claude").symlink_to("claude-elsewhere")
        result = _runner(tmp_path).execute(
            target="s", args="", workspace=ws, model="m", timeout_s=60)
        assert result.exit_code == -1
        assert ".claude" in result.stderr
        assert "literal-secret-value" not in result.stderr
        assert not fake_fullsend.exists()
        assert (ws / ".claude").is_symlink()
        assert json.loads((real / "settings.json").read_text()) == {
            "env": {"API_TOKEN": "literal-secret-value"}}


class TestRunDirContainment:
    def _run_dir(self, tmp_path):
        ws = _workspace(tmp_path)
        run = ws / ".fullsend-runs" / "fs-sandbox-1"
        (run / "iteration-1").mkdir(parents=True)
        return ws, run

    def _map(self, tmp_path, ws):
        return _runner(tmp_path)._map_run_dir(
            ws / ".fullsend-runs", ws, exit_code=0, stderr="", duration=1.0)

    def test_symlinked_metrics_json_not_read(self, tmp_path):
        ws, run = self._run_dir(tmp_path)
        outside = tmp_path / "outside-metrics.json"
        outside.write_text(json.dumps({"total_cost_usd": 9.9, "num_turns": 99}))
        (run / "metrics.json").symlink_to(outside)
        result = self._map(tmp_path, ws)
        assert result.cost_usd is None
        assert result.num_turns is None

    def test_oversize_metrics_json_not_read(self, tmp_path, monkeypatch):
        ws, run = self._run_dir(tmp_path)
        (run / "metrics.json").write_text(json.dumps({"total_cost_usd": 9.9}))
        monkeypatch.setattr(fullsend_mod, "_MAX_JSON_BYTES", 5)
        assert self._map(tmp_path, ws).cost_usd is None

    def test_symlinked_telemetry_not_read(self, tmp_path):
        ws, run = self._run_dir(tmp_path)
        outside = tmp_path / "outside-telemetry.jsonl"
        outside.write_text(json.dumps({"attributes": {
            "decision": "block", "gen_ai.tool.name": "Bash"}}) + "\n")
        (run / "run-telemetry.jsonl").symlink_to(outside)
        assert self._map(tmp_path, ws).permission_denials == []

    def test_oversize_telemetry_read_is_bounded(self, tmp_path, monkeypatch):
        ws, run = self._run_dir(tmp_path)
        line = json.dumps({"attributes": {"decision": "block", "gen_ai.tool.name": "Bash"}})
        (run / "run-telemetry.jsonl").write_text((line + "\n") * 3)
        monkeypatch.setattr(fullsend_mod, "_MAX_TELEMETRY_BYTES", len(line) + 1)
        assert len(self._map(tmp_path, ws).permission_denials) == 1

    def test_symlinked_security_dir_not_walked(self, tmp_path):
        ws, run = self._run_dir(tmp_path)
        outside = tmp_path / "outside-security"
        outside.mkdir()
        (outside / "a.jsonl").write_text("{}\n")
        (run / "security").symlink_to(outside, target_is_directory=True)
        assert self._map(tmp_path, ws).raw_output["security_files"] == 0

    def test_symlinked_files_inside_security_not_counted(self, tmp_path):
        ws, run = self._run_dir(tmp_path)
        (run / "security").mkdir()
        (run / "security" / "real.jsonl").write_text("{}\n")
        outside = tmp_path / "outside.jsonl"
        outside.write_text("{}\n")
        (run / "security" / "link.jsonl").symlink_to(outside)
        assert self._map(tmp_path, ws).raw_output["security_files"] == 1
