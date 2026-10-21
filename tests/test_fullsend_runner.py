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
                "FAKE_FULLSEND_ITERATIONS"):
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
            "hooks": {"PreToolUse": [{"hooks": [{"command": "/host/tools.py"}]}]},
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
            "hooks": {"PreToolUse": [{"hooks": [{"command": "/host/tools.py"}]}]},
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

        _runner(tmp_path).execute(target="s", args="", workspace=ws, model="m",
                                  timeout_s=60)
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
        start = __import__("time").monotonic()
        terminate_process_group(proc, grace_s=1)
        assert __import__("time").monotonic() - start < 10

    def test_failed_link_copy_leaves_the_link(self, tmp_path, monkeypatch):
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
                    deadline = __import__("time").monotonic() + 10
                    while not pidfile.exists() and __import__("time").monotonic() < deadline:
                        __import__("time").sleep(0.05)
                    raise KeyboardInterrupt
                return super().communicate(*args, **kwargs)

        monkeypatch.setattr(fullsend_mod.subprocess, "Popen", InterruptingPopen)
        runner = _runner(tmp_path, term_grace_s=2)
        with pytest.raises(KeyboardInterrupt):
            runner._run([sys.executable, "-c", code], ws, dict(os.environ), 60)
        leader, child = json.loads(pidfile.read_text())
        deadline = __import__("time").monotonic() + 5
        while (_alive(leader) or _alive(child)) and __import__("time").monotonic() < deadline:
            __import__("time").sleep(0.05)
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
