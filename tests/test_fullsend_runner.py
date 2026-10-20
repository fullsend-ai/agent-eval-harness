"""Tests for the fullsend runner (``runner.type: fullsend``).

Every execution test drives the runner against a fake ``fullsend`` script on
PATH (no OpenShell gateway in CI). The fake records its argv, the env file it
was handed and the state of the target repository, then writes a run
directory shaped like a real fullsend run.
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
from agent_eval.agent.fullsend import FullsendRunner, format_env_file
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
    record = {
        "argv": argv,
        "cwd": os.getcwd(),
        "env_file": Path(flag("--env-file")).read_text(),
        "env_task": os.environ.get("FULLSEND_TASK"),
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

    run = out / "fs-sandbox-1"
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
        (target / "artifacts").mkdir(exist_ok=True)
        (target / "artifacts" / f"report-{i}.md").write_text(f"report {i}")

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
    for var in ("FAKE_FULLSEND_SLEEP", "FAKE_FULLSEND_EXIT", "FAKE_FULLSEND_ARTIFACTS"):
        monkeypatch.delenv(var, raising=False)
    return record


def _parse_env_file(text):
    """Inverse of format_env_file for the assertions."""
    out = {}
    for line in text.splitlines():
        key, _, quoted = line.partition("=")
        assert quoted.startswith('"') and quoted.endswith('"'), line
        body, value, i = quoted[1:-1], "", 0
        while i < len(body):
            if body[i] == "\\":
                nxt = body[i + 1]
                value += {"n": "\n", "r": "\r"}.get(nxt, nxt)
                i += 2
            else:
                value += body[i]
                i += 1
        out[key] = value
    return out


def _runner(tmp_path, **kwargs):
    fs_dir = tmp_path / "project" / ".fullsend"
    fs_dir.mkdir(parents=True, exist_ok=True)
    defaults = dict(agent="rfe-creator-eval", fullsend_dir=str(fs_dir))
    defaults.update(kwargs)
    return FullsendRunner(**defaults)


def _workspace(tmp_path):
    ws = tmp_path / "ws"
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
        assert "fullsend" in _RUNNER_TO_HARBOR_AGENT
        assert _RUNNER_TO_HARBOR_AGENT["fullsend"] is None

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
        env_file = argv[argv.index("--env-file") + 1]
        assert argv == [
            "run", "rfe-creator-eval",
            "--fullsend-dir", str((tmp_path / "project" / ".fullsend").resolve()),
            "--target-repo", str(ws.resolve()),
            "--model", "opus",
            "--env-file", env_file,
            "--output-dir", str(ws.resolve() / "output" / "fullsend"),
            "--keep-sandbox", "--debug",
        ]
        assert os.path.isabs(env_file)
        assert rec["cwd"] == str(ws.resolve())
        # The env file lives outside the uploaded workspace and is removed.
        assert not Path(env_file).is_relative_to(ws.resolve())
        assert not Path(env_file).exists()

    def test_env_file_content(self, tmp_path, fake_fullsend, monkeypatch):
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
        env = _parse_env_file(rec["env_file"])
        assert env["FULLSEND_TASK"] == f"/rfe.create {args}"
        assert env["AGENT_EVAL_SUBAGENT_MODEL"] == "claude-sonnet-4-6"
        assert env["AGENT_EVAL_EFFORT"] == "high"
        assert env["JIRA_URL"] == "https://jira.example"
        assert env["FWD"] == "from-caller"
        assert "MISSING" not in env
        assert env["GOOGLE_CLOUD_PROJECT"] == "proj"
        assert env["HOOK_VAR"] == "hooked"
        # The process sees the same variables.
        assert rec["env_task"] == f"/rfe.create {args}"

    def test_env_file_empty_model_and_effort(self, tmp_path, fake_fullsend):
        ws = _workspace(tmp_path)
        _runner(tmp_path).execute(target="s", args="", workspace=ws, model="m",
                                  timeout_s=60)
        env = _parse_env_file(json.loads(fake_fullsend.read_text())["env_file"])
        assert env["FULLSEND_TASK"] == "/s"
        assert env["AGENT_EVAL_SUBAGENT_MODEL"] == ""
        assert env["AGENT_EVAL_EFFORT"] == ""

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
        stream = (ws / "output" / "fullsend" / "fs-sandbox-1" / "iteration-1"
                  / "output.jsonl").read_text()
        assert result.exit_code == 0
        assert result.stdout == stream
        assert "Running agent: rfe-creator-eval" in result.stderr
        assert "fullsend console stderr" in result.stderr
        # metrics.json wins for the headline numbers.
        assert result.cost_usd == 0.42
        assert result.token_usage == {"input": 1200, "output": 340}
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

    def test_nonzero_exit_propagates(self, tmp_path, fake_fullsend, monkeypatch):
        monkeypatch.setenv("FAKE_FULLSEND_EXIT", "3")
        ws = _workspace(tmp_path)
        result = _runner(tmp_path).execute(target="s", args="", workspace=ws,
                                           model="m", timeout_s=60)
        assert result.exit_code == 3
        assert result.cost_usd == 0.42

    def test_timeout(self, tmp_path, fake_fullsend, monkeypatch):
        monkeypatch.setenv("FAKE_FULLSEND_SLEEP", "30")
        monkeypatch.setattr(fullsend_mod, "_TERM_GRACE_S", 2)
        ws = _workspace(tmp_path)
        result = _runner(tmp_path).execute(target="s", args="", workspace=ws,
                                           model="m", timeout_s=1)
        assert result.exit_code == -1
        assert "Timed out" in result.stderr
        assert result.duration_s < 20

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


def test_format_env_file_escapes():
    text = format_env_file({"A": 'x "y" $Z \\ w\nnext', "B": ""})
    assert text == 'A="x \\"y\\" \\$Z \\\\ w\\nnext"\nB=""\n'


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
    envf = _parse_env_file(rec["env_file"])
    assert envf["FULLSEND_TASK"] == "/rfe.speedrun --input batch.yaml --headless"
    assert envf["EVAL_FLAG"] == "on"

    run_result = json.loads((output_dir / "run_result.json").read_text())
    assert run_result["agent"] == "fullsend"
    assert run_result["exit_code"] == 0
    assert run_result["cost_usd"] == 0.42
    assert run_result["num_turns"] == 7
    assert run_result["model"] == "claude-opus-4-6"
    stream = (workspace / "output" / "fullsend" / "fs-sandbox-1" / "iteration-1"
              / "output.jsonl").read_text()
    assert (output_dir / "stdout.log").read_text() == stream
    assert (output_dir / "subagents" / "s1-agent-a1.jsonl").is_file()

    collect = _run_script("collect.py", [
        "--config", str(project / "eval.yaml"), "--workspace", str(workspace),
        "--output", str(output_dir)], cwd=project, env=env)
    assert collect.returncode == 0, collect.stderr
    for n, case_id in enumerate(("case-001", "case-002"), start=1):
        report = output_dir / "cases" / case_id / "artifacts" / f"report-{n}.md"
        assert report.read_text() == f"report {n}"
