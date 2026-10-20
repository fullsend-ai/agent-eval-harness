"""The claude-code runner under a provider plan (spec 014), against a fake
``claude`` binary that records what it was launched with: the settings
overlay (plan wins, 0600, removed after the run), the managed-key strip of the
process env, the bare id on the wire, the budget-flag mapping, ids sighted as
the stream is read, the hook's ids, error classification, cost-source labels.
The live-CLI form of these checks is the spec-local probe_claude_cli.py."""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_eval.agent.claude_code import ClaudeCodeRunner, _cli_budget_flag, _runner_cost_source  # noqa: E402
from agent_eval.providers.env import MANAGED_ENV_KEYS, settings_env_block  # noqa: E402
from openrouter_fakes import FAKE_KEY, fake_claude_records, install_fake_claude, make_plan  # noqa: E402


class _Binding:
    def __init__(self, tmp_path):
        self.sighted = []
        self.after = []
        self.hook_ids_path = tmp_path / "provider" / "hook-ids-c1.jsonl"

    def sight(self, gen_id, *, message_index=None, model_echo=None, role="agent"):
        self.sighted.append((gen_id, message_index, model_echo, role))
        return True

    def after_run(self, message_ids):
        self.after.append(list(message_ids))
        return 0


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    install_fake_claude(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    (ws / ".claude" / "settings.json").write_text(json.dumps({
        "env": {"ANTHROPIC_BASE_URL": "https://elsewhere.example", "KEEP_ME": "1"},
        "permissions": {"deny": ["Read(secret/**)"]}}))
    # a hostile host environment: Vertex forced, real Anthropic + OpenRouter keys around
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "host-project")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-host-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "host-oauth-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", "sk-or-mgmt-secret")
    return ws


def _run(ws, plan, binding=None, prompt_args="hello", **kw):
    runner = ClaudeCodeRunner(log_prefix="t", provider_plan=plan, run_id="run-1", **kw)
    if binding is not None:
        runner.bind_provider(binding)
    return runner.execute(target=None, args=prompt_args, workspace=ws,
                          model="openrouter:/z-ai/glm-5.2:exacto",
                          settings_path=ws / ".claude" / "settings.json",
                          max_budget_usd=5.0, timeout_s=60), runner


def test_plan_run_uses_the_overlay_and_a_clean_process_env(workspace):
    plan = make_plan()
    binding = _Binding(workspace.parent)
    result, _ = _run(workspace, plan, binding, prompt_args="HOOKCALL please")
    assert result.exit_code == 0
    rec = fake_claude_records(workspace)[-1]
    argv = rec["argv"]
    assert argv[argv.index("--model") + 1] == "z-ai/glm-5.2:exacto"          # bare id on the wire
    assert argv[argv.index("--max-budget-usd") + 1] == "250.0"               # 5 × 50
    overlay = Path(argv[argv.index("--settings") + 1])
    assert overlay.name == ".eval-overlay.json" and overlay.parent == workspace / ".claude"
    assert not overlay.exists()                                              # removed in the finally
    assert rec["settings_mode"] == "0o600"
    env_block = rec["settings"]["env"]
    expected = settings_env_block(plan, secrets="literal")
    assert {k: env_block[k] for k in expected} == expected                    # plan wins for managed keys
    assert env_block["KEEP_ME"] == "1"                                       # non-managed keys kept
    assert rec["settings"]["permissions"]["deny"] == ["Read(secret/**)"]      # existing rules kept
    assert rec["token_sha"] == plan.key_hash
    # process env: no managed key, no provider key variable, the hook-ids path
    assert not (set(rec["env_keys"]) & MANAGED_ENV_KEYS)
    assert "OPENROUTER_API_KEY" not in rec["env_keys"] and "OPENROUTER_MANAGEMENT_KEY" not in rec["env_keys"]
    assert rec["env"]["AGENT_EVAL_HOOK_IDS"] == str(binding.hook_ids_path)
    # ids were sighted as the stream was read (before the process exited), then the rest
    assert [s[0] for s in binding.sighted] == rec["ids"]
    assert binding.sighted[0][1:] == (1, "z-ai/glm-5.2", "agent")
    assert binding.after == [sorted(rec["ids"])] and result.message_ids == sorted(rec["ids"])
    assert json.loads(binding.hook_ids_path.read_text())["id"].endswith("-hook")
    assert result.cost_source == "runner:estimate" and result.cost_usd_estimate == 0.5
    assert result.error_class is None and result.budget is None


def test_no_plan_is_unchanged(workspace, monkeypatch):
    result, runner = _run(workspace, None)
    rec = fake_claude_records(workspace)[-1]
    argv = rec["argv"]
    assert argv[argv.index("--model") + 1] == "openrouter:/z-ai/glm-5.2:exacto"   # as given
    assert argv[argv.index("--max-budget-usd") + 1] == "5.0"
    assert "--settings" in argv and argv[argv.index("--settings") + 1].endswith("settings.json")
    assert "CLAUDE_CODE_USE_VERTEX" in rec["env_keys"] and rec["env"]["ANTHROPIC_API_KEY"] == "sk-ant-host-secret"
    assert result.cost_source == "runner:reported" and result.cost_usd_estimate is None
    assert result.message_ids == sorted(rec["ids"])


def test_budget_flag_mapping_and_cost_source_labels():
    plan = make_plan()
    assert _cli_budget_flag(5.0, None) == "5.0" and _cli_budget_flag(0, None) == "0"
    assert _cli_budget_flag(5.0, plan) == "250.0"
    assert _cli_budget_flag(0, plan) is None and _cli_budget_flag(None, plan) is None
    assert _runner_cost_source({}, plan) == "runner:estimate"
    assert _runner_cost_source({"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}, None) == "runner:reported"
    assert _runner_cost_source({"ANTHROPIC_BASE_URL": "http://localhost:4000"}, None) == "runner:estimate"
    assert _runner_cost_source({}, None) == "runner:reported"


def test_zero_cap_omits_the_flag_under_a_plan(workspace):
    runner = ClaudeCodeRunner(log_prefix="t", provider_plan=make_plan())
    result = runner.execute(target=None, args="x", workspace=workspace, model="openrouter:/z-ai/glm-5.2",
                            settings_path=None, max_budget_usd=0, timeout_s=60)
    assert result.exit_code == 0
    assert "--max-budget-usd" not in fake_claude_records(workspace)[-1]["argv"]
    assert not (workspace / ".eval-overlay.json").exists()


def test_overlay_never_follows_a_planted_symlink(workspace, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    victim.chmod(0o644)
    mode_before = victim.stat().st_mode
    link = workspace / ".claude" / ".eval-overlay.json"
    link.symlink_to(victim)
    runner = ClaudeCodeRunner(log_prefix="t", provider_plan=make_plan())
    with pytest.raises(RuntimeError, match="is a symlink"):
        runner._write_settings_overlay(workspace, workspace / ".claude" / "settings.json", [], [], make_plan())
    assert victim.read_text() == "precious" and link.is_symlink()
    assert victim.stat().st_mode == mode_before                    # never chmod'ed through the link


@pytest.mark.parametrize("plan_mode", [True, False])
def test_overlay_never_writes_through_a_planted_hardlink(workspace, tmp_path, plan_mode):
    """A hardlink at the overlay path (either name) is a pre-existing entry
    with two links: refused, and the linked target keeps content and mode."""
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    victim.chmod(0o644)
    mode_before = victim.stat().st_mode
    name = ".eval-overlay.json" if plan_mode else ".eval-permissions.json"
    os.link(victim, workspace / ".claude" / name)
    runner = ClaudeCodeRunner(log_prefix="t", provider_plan=make_plan() if plan_mode else None)
    with pytest.raises(RuntimeError, match="pre-existing entry with 2 link"):
        runner._write_settings_overlay(workspace, workspace / ".claude" / "settings.json",
                                       [{"path": "secret/**", "tools": ["Read"]}] if not plan_mode else [],
                                       [], make_plan() if plan_mode else None)
    assert victim.read_text() == "precious" and victim.stat().st_mode == mode_before
    assert victim.stat().st_nlink == 2                               # the planted link is left alone


def test_a_stale_overlay_from_a_killed_run_is_replaced(workspace):
    stale = workspace / ".claude" / ".eval-overlay.json"
    stale.write_text("{\"env\": {\"ANTHROPIC_AUTH_TOKEN\": \"old\"}}")
    runner = ClaudeCodeRunner(log_prefix="t", provider_plan=make_plan())
    overlay = runner._write_settings_overlay(workspace, workspace / ".claude" / "settings.json", [], [], make_plan())
    assert overlay == stale and json.loads(stale.read_text())["env"]["ANTHROPIC_AUTH_TOKEN"] == make_plan().key
    assert stale.stat().st_mode & 0o777 == 0o600


def test_key_limit_402_is_classified(workspace):
    result, _ = _run(workspace, make_plan(), prompt_args="FAIL402")
    assert result.error_class == "config"
    assert result.budget == {"exceeded": "run", "exceeded_reason": "limit_usd"}


def test_build_env_without_a_binding_or_plan(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    env = ClaudeCodeRunner(provider_plan=make_plan(), subagent_model="openrouter:/z-ai/glm-5.2")._build_env()
    assert not (set(env) & MANAGED_ENV_KEYS) and "AGENT_EVAL_HOOK_IDS" not in env
    # nothing merged later may put a managed key back: runner.env or a hook's runtime env
    runner = ClaudeCodeRunner(provider_plan=make_plan(), env={"ANTHROPIC_BASE_URL": "https://x", "RUNNER_OK": "1"})
    env = runner._build_env(extra_env={"CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_API_KEY": "sk", "HOOK_OK": "1"})
    assert not (set(env) & MANAGED_ENV_KEYS) and env["RUNNER_OK"] == "1" and env["HOOK_OK"] == "1"
    env = ClaudeCodeRunner(subagent_model="claude-sonnet-4-5")._build_env()
    assert env["CLAUDE_CODE_USE_VERTEX"] == "1" and env["CLAUDE_CODE_SUBAGENT_MODEL"] == "claude-sonnet-4-5"


def test_vertex_credential_locations_stay_home_under_a_plan(monkeypatch):
    """The plan blanks Vertex, so the operator's GCP credential locations are not
    forwarded to the agent (the K8s runner already skips the mount); without a
    plan they travel as before, and an explicit runner.env entry still wins."""
    for k in ("GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CONFIG", "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"):
        monkeypatch.setenv(k, f"/home/op/{k.lower()}")
    with_plan = ClaudeCodeRunner(provider_plan=make_plan())._build_env()
    assert not (set(with_plan) & ClaudeCodeRunner._VERTEX_CREDENTIAL_KEYS)
    without = ClaudeCodeRunner()._build_env()
    assert ClaudeCodeRunner._VERTEX_CREDENTIAL_KEYS <= set(without)
    explicit = ClaudeCodeRunner(provider_plan=make_plan(),
                                env={"GOOGLE_APPLICATION_CREDENTIALS": "/explicit/sa.json"})._build_env()
    assert explicit["GOOGLE_APPLICATION_CREDENTIALS"] == "/explicit/sa.json"
    assert "CLOUDSDK_CONFIG" not in explicit


def test_openshell_vertex_placeholder_switches_travel_without_a_plan(monkeypatch):
    """OpenShell >= 0.1.2: agentic-ci's env script sets CLAUDE_CODE_SKIP_VERTEX_AUTH
    and ANTHROPIC_VERTEX_BASE_URL so Claude Code sends the placeholder bearer
    token instead of looking for Application Default Credentials; both must reach
    the CLI. Under a plan Vertex is blanked and they stay home."""
    monkeypatch.setenv("CLAUDE_CODE_SKIP_VERTEX_AUTH", "1")
    monkeypatch.setenv("ANTHROPIC_VERTEX_BASE_URL", "https://aiplatform.googleapis.com/v1")
    monkeypatch.setenv("CLAUDE_CODE_MAX_RETRIES", "25")
    without = ClaudeCodeRunner()._build_env()
    assert without["CLAUDE_CODE_SKIP_VERTEX_AUTH"] == "1"
    assert without["ANTHROPIC_VERTEX_BASE_URL"] == "https://aiplatform.googleapis.com/v1"
    assert without["CLAUDE_CODE_MAX_RETRIES"] == "25"
    with_plan = ClaudeCodeRunner(provider_plan=make_plan())._build_env()
    assert not (set(with_plan) & ClaudeCodeRunner._VERTEX_PLACEHOLDER_KEYS)
    assert with_plan["CLAUDE_CODE_MAX_RETRIES"] == "25"


def test_overlay_is_removed_when_env_setup_fails(workspace, tmp_path):
    """The overlay holds the key before the process env is built: a failure
    there (the hook-ids directory cannot be created) still removes it."""
    binding = _Binding(workspace.parent)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    binding.hook_ids_path = blocker / "hook-ids-c1.jsonl"          # parent is a file: mkdir raises
    result, _ = _run(workspace, make_plan(), binding)
    assert result.exit_code == -1 and "not-a-dir" in result.stderr
    assert not (workspace / ".claude" / ".eval-overlay.json").exists()
    assert fake_claude_records(workspace) == []                    # the CLI was never launched


def test_a_failure_mid_stream_keeps_the_sighted_ids(workspace, tmp_path, monkeypatch):
    """Ids streamed before (and at) a stream-processing failure are billed:
    the CLI is reaped, the failed result still carries every id it produced
    and the binding still gets its after-run pass."""
    import agent_eval.agent.claude_code as cc

    class _Flaky(_Binding):
        def sight(self, gen_id, **kw):
            if len(self.sighted) == 1:
                raise RuntimeError("boom")
            return super().sight(gen_id, **kw)

    procs = []
    real_popen = cc.subprocess.Popen

    def recording_popen(*a, **kw):
        procs.append(real_popen(*a, **kw))
        return procs[-1]

    monkeypatch.setattr(cc.subprocess, "Popen", recording_popen)
    binding = _Flaky(workspace.parent)
    result, _ = _run(workspace, make_plan(), binding)
    assert result.exit_code == -1 and "boom" in result.stderr
    ids = sorted(fake_claude_records(workspace)[-1]["ids"])
    assert result.message_ids == ids                              # the id whose sight raised included
    assert binding.after == [ids]                                 # after_run still ran, with all of them
    assert procs and procs[-1].returncode is not None             # the CLI was reaped, not left running
    assert result.cost_source == "runner:estimate" and result.cost_usd_estimate is None
    assert not (workspace / ".claude" / ".eval-overlay.json").exists()


def test_reap_drain_is_bounded_when_a_descendant_holds_stdout():
    """A background descendant keeping the CLI's stdout open must not make
    the failure-path drain block: it returns by the deadline with what was
    read, and the child itself is reaped."""
    import subprocess
    import time

    from agent_eval.agent.claude_code import _reap

    child = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess, sys; print('{\"type\": \"assistant\", \"message\": {\"id\": \"gen-fake-drain\"}}', flush=True); "
         "subprocess.Popen(['sleep', '5']); sys.exit(0)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    child.wait(timeout=10)                  # the CLI is gone; only the descendant holds stdout
    started = time.monotonic()
    lines = _reap(child, timeout_s=1.0)
    assert time.monotonic() - started < 4
    assert any("gen-fake-drain" in line for line in lines)
    assert child.returncode is not None



def test_permission_prompts_is_validated_and_emitted(tmp_path, monkeypatch):
    """`--permission-prompts none` is the flag that makes an allow list
    authoritative under `--print`; a typo must not silently fall back to the
    permissive default."""
    import os
    import pytest
    from agent_eval.agent.claude_code import ClaudeCodeRunner

    with pytest.raises(ValueError, match="Invalid permission_prompts 'nope'"):
        ClaudeCodeRunner(permission_prompts="nope")
    with pytest.raises(ValueError, match="Invalid permission_prompts"):
        ClaudeCodeRunner(permission_prompts=1)
    # Valid values construct; None leaves the flag off entirely.
    for value in ("host", "none", None):
        ClaudeCodeRunner(permission_prompts=value)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "claude"
    stub.write_text(
        "#!/bin/sh\n"
        "printf '%s\\0' \"$@\" > argv.txt\n"
        "cat > /dev/null\n"
        "printf '%s\\n' '{\"type\":\"result\",\"subtype\":\"success\","
        "\"is_error\":false,\"num_turns\":1,\"total_cost_usd\":0.0,"
        "\"result\":\"ok\"}'\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = ClaudeCodeRunner(permission_mode="manual",
                              permission_prompts="none")
    runner.execute(target=None, args="hi", workspace=ws, model="m",
                   timeout_s=60)
    argv = (ws / "argv.txt").read_text().split("\0")[:-1]
    assert "--permission-prompts" in argv
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert argv[argv.index("--permission-mode") + 1] == "manual"
