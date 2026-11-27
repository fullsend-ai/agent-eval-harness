"""Cross-writer conformance (spec 014): one plan rendered by the claude-code
overlay, Harbor's ``--agent-env`` carriers and the interception task package
agrees everywhere — identical non-secret env, ``$VAR`` never baked, no managed
key in a task package, value-free carrier argv, the child-env scrub."""

import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_eval.agent.claude_code import ClaudeCodeRunner  # noqa: E402
from agent_eval.config import EvalConfig  # noqa: E402
from agent_eval.harbor import run as run_mod  # noqa: E402
from agent_eval.providers.env import MANAGED_ENV_KEYS, settings_env_block  # noqa: E402
from agent_eval.tools.interception import generate_interception  # noqa: E402
from openrouter_fakes import FAKE_KEY, make_plan  # noqa: E402


def _config(tmp_path, *, execution_env=None, judge=None):
    raw = {"name": "t", "skill": "demo", "runner": {"type": "claude-code"},
           "execution": {"skill": "demo", "env": execution_env or {}},
           "dataset": {"path": str(tmp_path)}, "outputs": [{"path": "output"}],
           "models": {"skill": "openrouter:/z-ai/glm-5.2:exacto"},
           "inputs": {"tools": [{"name": "AskUserQuestion", "prompt": "pick the first"}]}}
    if judge:
        raw["models"]["judge"] = judge
    p = tmp_path / "eval.yaml"
    p.write_text(yaml.safe_dump(raw, sort_keys=False))
    return EvalConfig.from_yaml(p)


def test_overlay_carrier_and_package_agree(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    monkeypatch.setenv("DATA_DIR_VALUE", "/data")
    # workspace.py main() exports this into os.environ; an earlier in-process call must not leak in
    monkeypatch.delenv("EVAL_RUN_HEADER", raising=False)
    plan = make_plan()
    config = _config(tmp_path, execution_env={"DATA_DIR": "$DATA_DIR_VALUE", "STATIC": "1",
                                              "EVAL_RUN_HEADER": "$EVAL_RUN_HEADER"})
    block = settings_env_block(plan, secrets="literal")

    # 1. the claude-code overlay: plan wins over a conflicting workspace settings env
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    (ws / ".claude" / "settings.json").write_text(json.dumps({"env": {
        "ANTHROPIC_BASE_URL": "https://elsewhere", "ANTHROPIC_AUTH_TOKEN": "stale",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-8", "KEEP": "y"}}))
    overlay = ClaudeCodeRunner(provider_plan=plan)._write_settings_overlay(
        ws, ws / ".claude" / "settings.json", [], [], plan)
    env = json.loads(overlay.read_text())["env"]
    assert {k: env[k] for k in block} == block and env["KEEP"] == "y"
    assert overlay.stat().st_mode & 0o777 == 0o600

    # 2. Harbor carriers: the plan's block merged last, key resolved from the host, argv value-free
    resolved = run_mod._resolve_harbor_agent_env(config, make_plan(runner="harbor-podman"))
    assert resolved["ANTHROPIC_AUTH_TOKEN"] == FAKE_KEY
    assert {k: resolved[k] for k in block if k != "ANTHROPIC_AUTH_TOKEN"} == \
        {k: v for k, v in block.items() if k != "ANTHROPIC_AUTH_TOKEN"}
    assert resolved["DATA_DIR"] == "/data" and resolved["STATIC"] == "1" and "EVAL_RUN_HEADER" not in resolved
    args, child_env = run_mod._harbor_agent_env_args(config, make_plan(runner="harbor-podman"))
    assert FAKE_KEY not in " ".join(args) and all("${AGENT_EVAL_HARBOR_AGENT_ENV_" in a for a in args[1::2])
    assert {a.split("=")[0] for a in args[1::2]} == set(resolved)
    assert FAKE_KEY in child_env.values()

    # 3. the Kubernetes pod: the same block as plain env[] (the plan wins over a stale
    #    envFrom value), the token only as a secretKeyRef — never a literal
    from agent_eval.harbor import k8s_plan

    k8s_plan_obj = make_plan(runner="harbor-k8s")
    environ = {k8s_plan.PLAN_ENV_VAR: json.dumps(k8s_plan_obj.agent_env()),
               k8s_plan.TOKEN_SECRET_VAR: "model-keys"}
    pod = k8s_plan.pod_env({"ANTHROPIC_BASE_URL": "https://stale", "ANTHROPIC_AUTH_TOKEN": "stale"}, environ)
    assert pod == {k: v for k, v in block.items() if k != "ANTHROPIC_AUTH_TOKEN"}
    assert set(pod) == set(env_managed := {k for k in block}) - {"ANTHROPIC_AUTH_TOKEN"} and env_managed
    assert k8s_plan.token_secret_ref(environ)["valueFrom"]["secretKeyRef"] == {"name": "model-keys",
                                                                              "key": "OPENROUTER_API_KEY"}
    assert FAKE_KEY not in json.dumps(pod)

    # 4. the task package: no `$VAR`, no managed key, nothing provider-related
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    generate_interception(pkg, config, "python3 /workspace/hooks/tools.py")
    baked = json.loads((pkg / ".claude" / "settings.json").read_text()).get("env", {})
    assert baked == {"STATIC": "1"}
    assert not (set(baked) & MANAGED_ENV_KEYS)


def test_child_env_scrub_set(tmp_path):
    plan = make_plan(runner="harbor-podman")
    excluded = run_mod._plan_child_env_exclusions(plan, keep_api_key=False)
    assert excluded == set(MANAGED_ENV_KEYS) | {"OPENROUTER_MANAGEMENT_KEY", "OPENROUTER_API_KEY"}
    assert "OPENROUTER_API_KEY" not in run_mod._plan_child_env_exclusions(plan, keep_api_key=True)
    # a custom api_key_env: the default-named host variable is still kept out,
    # only the configured name survives for the verifier
    custom = make_plan(runner="harbor-podman", key_env="MY_OR_KEY")
    kept = run_mod._plan_child_env_exclusions(custom, keep_api_key=True)
    assert "OPENROUTER_API_KEY" in kept and "MY_OR_KEY" not in kept
    assert "MY_OR_KEY" in run_mod._plan_child_env_exclusions(custom, keep_api_key=False)
    assert not run_mod._openrouter_judge_configured(_config(tmp_path))
    assert run_mod._openrouter_judge_configured(_config(tmp_path, judge="openrouter:/z-ai/glm-5.2"))
    assert run_mod._openrouter_judge_configured(_config(tmp_path), judge_model="openrouter:/x/y")
