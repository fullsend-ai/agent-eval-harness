"""The preflight minimum (spec 014, PR-5) against the loopback fake."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_eval.providers.base import ConfigError, parse_agent_model  # noqa: E402
from agent_eval.providers.openrouter.preflight import (  # noqa: E402
    SNAPSHOT_RELPATH, plan_models, run_preflight, table_sha)
from agent_eval.providers.openrouter.routing import RoutingTable  # noqa: E402
from openrouter_fakes import FakeOpenRouter, make_plan  # noqa: E402


@pytest.fixture
def fake():
    srv = FakeOpenRouter()
    srv.base_url = srv.start()
    yield srv
    srv.stop()


def test_strict_pass_writes_the_snapshot(fake, tmp_path):
    plan = make_plan(fake.base_url, run_id="r1")
    res = run_preflight(plan, level="strict", run_dir=tmp_path)
    assert res.failures == [] and res.warnings == [] and res.degraded_reason is None
    snap = json.loads((tmp_path / SNAPSHOT_RELPATH).read_text())
    assert snap["routing_sha"] == table_sha(plan.routing) and snap["ts"]
    entry = snap["keys"]["z-ai/glm-5.2"]
    assert entry["variant"] == "exacto" and entry["roles"] == ["agent"]
    assert entry["pinned_set"] == [["novita", "z-ai/glm-5.2-20260616", "fp8"],
                                   ["novita", "z-ai/glm-5.2-20260616", "bf16"]]
    assert entry["eligible"] == ["novita/fp8", "novita/bf16"] and entry["excluded"] == []
    assert entry["catalog"] == {"providers": ["novita", "z-ai"],
                                "endpoints": ["novita/fp8", "novita/bf16", "z-ai"]}
    assert snap["enforcement"] == "audit" and snap["key_scope"] == "operator" and snap["providers_map_sha"]
    assert "novita" in snap["pricing"]["z-ai/glm-5.2"]
    assert snap["catalog"]["endpoints"]["z-ai/glm-5.2"][0]["provider_name"] == "Novita"
    # the key-authenticated GETs carried the bearer, the catalog GETs did not need it
    assert ("/api/v1/key", True) in fake.requests and ("/api/v1/models/user", True) in fake.requests
    assert ("/api/v1/models", False) in fake.requests


def test_plan_models_covers_every_agent_path_id():
    plan = make_plan(background_model="qwen/qwen3-8b", hook=parse_agent_model("openrouter:/qwen/qwen3-8b"))
    assert plan_models(plan) == {"z-ai/glm-5.2": "exacto", "qwen/qwen3-8b": None}


def test_unknown_slug_and_unserved_pin_fail_strict(fake, tmp_path):
    plan = make_plan(fake.base_url, skill=parse_agent_model("openrouter:/nobody/none"),
                     subagent=parse_agent_model("openrouter:/nobody/none"))
    with pytest.raises(ConfigError, match="nobody/none: not in the OpenRouter catalog"):
        run_preflight(plan, level="strict", run_dir=tmp_path)
    table = RoutingTable.from_dict({"models": {"z-ai/glm-5.2": {"only": ["deepinfra"]}}})
    with pytest.raises(ConfigError, match="pinned provider\\(s\\) deepinfra do not serve it"):
        run_preflight(make_plan(fake.base_url, routing=table), level="strict")
    table = RoutingTable.from_dict({"models": {"z-ai/glm-5.2": {"only": ["novita"], "quantizations": ["int4"]}}})
    with pytest.raises(ConfigError, match="no eligible pinned endpoint .*not in \\['int4'\\]"):
        run_preflight(make_plan(fake.base_url, routing=table), level="strict")
    # every endpoint of a pinned provider counts, not only the last one listed
    for quant in ("fp8", "bf16"):
        table = RoutingTable.from_dict({"models": {"z-ai/glm-5.2": {"only": ["novita"], "quantizations": [quant]}}})
        assert run_preflight(make_plan(fake.base_url, routing=table), level="strict").failures == []


def test_bad_key_and_ineligible_model_fail_strict(fake, tmp_path):
    with pytest.raises(ConfigError, match="inference key \\(OPENROUTER_API_KEY\\) rejected: HTTP 401"):
        run_preflight(make_plan(fake.base_url, key="sk-or-wrong"), level="strict")
    plan = make_plan(fake.base_url, skill=parse_agent_model("openrouter:/deepseek/deepseek-v4.1-flash"),
                     subagent=parse_agent_model("openrouter:/deepseek/deepseek-v4.1-flash"), pins=False)
    with pytest.raises(ConfigError, match="deepseek-v4.1-flash: not available to this key"):
        run_preflight(plan, level="strict")


def test_warn_level_degrades_instead_of_failing(fake, tmp_path, capsys):
    res = run_preflight(make_plan(fake.base_url, key="sk-or-wrong"), level="warn", run_dir=tmp_path)
    assert res.failures and res.degraded_reason == "preflight"
    assert "WARNING: preflight: inference key" in capsys.readouterr().err
    assert (tmp_path / SNAPSHOT_RELPATH).exists()


def test_catalog_failure_degrades_but_key_checks_stay_strict(tmp_path):
    srv = FakeOpenRouter(catalog_down=True)
    base = srv.start()
    try:
        res = run_preflight(make_plan(base), level="strict", run_dir=tmp_path)
        assert res.degraded_reason == "catalog" and res.failures == []
        assert any("catalog unavailable" in w for w in res.warnings)
        with pytest.raises(ConfigError, match="rejected: HTTP 401"):
            run_preflight(make_plan(base, key="nope"), level="strict")
    finally:
        srv.stop()


def test_off_does_nothing():
    res = run_preflight(make_plan("http://127.0.0.1:9/api"), level="off")
    assert res.snapshot is None and res.failures == []
    with pytest.raises(ConfigError, match="preflight level"):
        run_preflight(make_plan(), level="loud")


def test_degraded_and_incapable_endpoints_are_excluded_per_role(fake, tmp_path):
    """qwen/qwen3-8b: DeepInfra is status -2 (degraded) and Novita cannot force
    a tool call and caps completions at 16k."""
    table = RoutingTable.from_dict({"models": {"qwen/qwen3-8b": {"only": ["deepinfra", "novita"]}}})
    plan = make_plan(fake.base_url, routing=table, skill=parse_agent_model("openrouter:/qwen/qwen3-8b"),
                     subagent=parse_agent_model("openrouter:/qwen/qwen3-8b"))
    res = run_preflight(plan, level="strict", run_dir=tmp_path)
    entry = res.snapshot["keys"]["qwen/qwen3-8b"]
    assert entry["eligible"] == ["novita/qwen"]
    assert entry["excluded"] == [{"tag": "deepinfra", "reasons": ["status -2 (degraded)"]}]
    assert any("max_completion_tokens 16384" in w for w in res.warnings)
    assert any("excluded pinned endpoint(s) deepinfra" in w for w in res.warnings)
    # the same slug as a pinned judge: no eligible endpoint supports tool_choice: function → FAIL
    with pytest.raises(ConfigError, match="no pinned endpoint supports tool_choice: function"):
        run_preflight(plan, level="strict", judges={"qwen/qwen3-8b": True})
    # an unpinned judge (no forced call under pins) is not held to it
    assert run_preflight(plan, level="strict", judges={"qwen/qwen3-8b": False}).failures == []
    # only the degraded endpoint pinned: nothing eligible → FAIL naming the reason
    table = RoutingTable.from_dict({"models": {"qwen/qwen3-8b": {"only": ["deepinfra"]}}})
    with pytest.raises(ConfigError, match="no eligible pinned endpoint \\(deepinfra: status -2"):
        run_preflight(make_plan(fake.base_url, routing=table, skill=plan.skill, subagent=plan.subagent), level="strict")


def test_judge_keys_join_the_check_set(fake, tmp_path):
    plan = make_plan(fake.base_url)
    res = run_preflight(plan, level="strict", run_dir=tmp_path,
                        judges={"z-ai/glm-5.2": True, "qwen/qwen3-8b": False})
    assert res.snapshot["keys"]["z-ai/glm-5.2"]["roles"] == ["agent", "judge"]
    assert res.snapshot["keys"]["qwen/qwen3-8b"]["roles"] == ["judge"]
    assert res.failures == []                    # z-ai/glm-5.2's Novita endpoints support function


def test_preflight_cli_runs_without_a_run_or_a_per_run_key(fake, tmp_path, monkeypatch, capsys):
    import yaml

    from agent_eval.providers.openrouter.preflight import main
    from openrouter_fakes import FAKE_KEY

    cfg = {"name": "t", "execution": {"skill": "s"}, "judges": [{"name": "j", "check": "return True"}],
           "models": {"skill": "openrouter:/z-ai/glm-5.2:exacto", "providers": {"openrouter": {
               "base_url": fake.base_url, "budget": {"run_usd": 1},
               "routing": {"enforcement": "key-guardrail",
                           "models": {"z-ai/glm-5.2": {"order": ["novita"], "allow_fallbacks": False}}}}}}}
    (tmp_path / "eval.yaml").write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    monkeypatch.delenv("OPENROUTER_MANAGEMENT_KEY", raising=False)
    assert main(["--config", str(tmp_path / "eval.yaml"), "--run-dir", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    assert "preflight strict: ok | keys: z-ai/glm-5.2" in out
    assert (tmp_path / "out" / "provider" / "routing_snapshot.json").exists()
    assert fake.issued == {}                     # no per-run key was provisioned by a dry run
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-wrong")
    assert main(["--config", str(tmp_path / "eval.yaml")]) == 2
    assert "rejected: HTTP 401" in capsys.readouterr().err


def test_a_judge_is_checked_against_its_own_declaration(fake, tmp_path):
    """The agent pins Novita; a judge on the same slug may pin someone else
    (provider_options.routing, Decision 25) and is checked apart."""
    import yaml

    from agent_eval.config import EvalConfig
    from agent_eval.providers.openrouter.plan import judge_routing_keys

    def cfg(judge_routing):
        raw = {"name": "t", "execution": {"skill": "s"},
               "models": {"skill": "openrouter:/z-ai/glm-5.2:exacto", "judge": "openrouter:/z-ai/glm-5.2",
                          "providers": {"openrouter": {"base_url": fake.base_url, "routing": {
                              "models": {"z-ai/glm-5.2": {"order": ["novita"], "allow_fallbacks": False}}}}}},
               "judges": [{"name": "q", "prompt": "score it", "provider_options": {"routing": judge_routing}}]}
        (tmp_path / "eval.yaml").write_text(yaml.safe_dump(raw))
        return EvalConfig.from_yaml(tmp_path / "eval.yaml")

    # the judge pins a provider that does not serve the model: the judge check fails, not the agent's
    judges = judge_routing_keys(cfg({"only": ["deepinfra"]}))
    assert judges["z-ai/glm-5.2"].only == ("deepinfra",)
    with pytest.raises(ConfigError, match="\\(judge declaration\\): pinned provider\\(s\\) deepinfra do not serve it"):
        run_preflight(make_plan(fake.base_url), level="strict", judges=judges)
    # the judge pins Z.AI (function-capable) while the agent pins Novita: both pass, checked apart
    judges = judge_routing_keys(cfg({"only": ["z-ai"]}))
    res = run_preflight(make_plan(fake.base_url), level="strict", run_dir=tmp_path, judges=judges)
    entry = res.snapshot["keys"]["z-ai/glm-5.2"]
    assert entry["roles"] == ["agent", "judge"] and entry["eligible"] == ["novita/fp8", "novita/bf16"]
    assert entry["judge"]["roles"] == ["judge"] and entry["judge"]["eligible"] == ["z-ai"]
    # no override and no inherit_pins: the judge sends no pins and nothing extra is checked
    assert judge_routing_keys(cfg(None)) == {"z-ai/glm-5.2": None}



def test_pinned_judges_are_checked_for_structured_outputs_per_verdict_policy(fake, tmp_path):
    """Spec 015: under `auto` a pinned set without `structured_outputs` is a
    WARN (the judge takes the forced-tool rung); under `json_schema` a FAIL;
    under `tool` the check is skipped and only Decision 25's applies."""
    glm = make_plan(fake.base_url)
    res = run_preflight(glm, level="strict", run_dir=tmp_path, judges={"z-ai/glm-5.2": True})
    assert res.failures == []            # novita/fp8 advertises structured_outputs
    assert not any("no pinned endpoint advertises structured_outputs" in w for w in res.warnings)
    # qwen: no endpoint advertises structured_outputs, Novita cannot force a tool either
    table = RoutingTable.from_dict({"models": {"qwen/qwen3-8b": {"only": ["novita"]}}})
    qwen = make_plan(fake.base_url, routing=table, skill=parse_agent_model("openrouter:/qwen/qwen3-8b"),
                     subagent=parse_agent_model("openrouter:/qwen/qwen3-8b"))
    with pytest.raises(ConfigError) as exc:
        run_preflight(qwen, level="strict", judges={"qwen/qwen3-8b": True})
    assert "no pinned endpoint supports tool_choice: function" in str(exc.value)
    res = run_preflight(qwen, level="warn", judges={"qwen/qwen3-8b": True})
    assert any("no pinned endpoint advertises structured_outputs" in w for w in res.warnings)
    with pytest.raises(ConfigError, match="pins json_schema"):
        run_preflight(qwen, level="strict", judges={"qwen/qwen3-8b": True}, verdict="json_schema")
    res = run_preflight(qwen, level="warn", judges={"qwen/qwen3-8b": True}, verdict="json_schema")
    assert not any("tool_choice: function" in w for w in res.warnings + res.failures)


def test_auto_accepts_a_structured_outputs_endpoint_that_cannot_force_a_tool(fake, tmp_path, monkeypatch):
    """Under `auto` the first transport is json_schema, so an endpoint that
    advertises structured_outputs keeps the run viable even though it cannot
    force a tool call (WARN); `tool` still fails, `json_schema` passes."""
    import openrouter_fakes

    monkeypatch.setitem(openrouter_fakes.ENDPOINTS, "qwen/qwen3-8b", [
        {"provider_name": "Novita", "tag": "novita/qwen", "quantization": "bf16", "status": 0,
         "supported_parameters": ["tools", "tool_choice", "response_format", "structured_outputs"],
         "supports_tool_choice": {"none": True, "auto": True, "required": False, "function": False},
         "max_completion_tokens": 16384}])
    table = RoutingTable.from_dict({"models": {"qwen/qwen3-8b": {"only": ["novita"]}}})
    qwen = make_plan(fake.base_url, routing=table, skill=parse_agent_model("openrouter:/qwen/qwen3-8b"),
                     subagent=parse_agent_model("openrouter:/qwen/qwen3-8b"))
    res = run_preflight(qwen, level="strict", judges={"qwen/qwen3-8b": True})
    assert res.failures == []
    assert any("json_schema transport only" in w for w in res.warnings)
    res = run_preflight(qwen, level="strict", judges={"qwen/qwen3-8b": True}, verdict="json_schema")
    assert res.failures == []
    with pytest.raises(ConfigError, match="tool_choice: function"):
        run_preflight(qwen, level="strict", judges={"qwen/qwen3-8b": True}, verdict="tool")


def test_preflight_checks_each_judge_under_its_own_verdict_policy(fake, tmp_path):
    """A judge's own `verdict:` decides which capability its pinned endpoints need
    (spec 015): a `json_schema` judge fails on a set without structured_outputs
    even when the run-level policy is `auto`, a `tool` judge is not held to
    structured_outputs under a run-level `json_schema`, and two judges on one
    key with different policies are each checked."""
    import yaml

    from agent_eval.config import EvalConfig
    from agent_eval.providers.openrouter.plan import judge_routing_keys, judge_verdict_policies

    def cfg(judges, run_policy="auto"):
        raw = {"name": "t", "execution": {"skill": "s"},
               "models": {"skill": "openrouter:/qwen/qwen3-8b", "judge": "openrouter:/qwen/qwen3-8b",
                          "judge_verdict": run_policy,
                          "providers": {"openrouter": {"base_url": fake.base_url,
                                                       "judge": {"inherit_pins": True},
                                                       "routing": {"models": {"qwen/qwen3-8b": {"only": ["novita"]}}}}}},
               "judges": judges}
        (tmp_path / "eval.yaml").write_text(yaml.safe_dump(raw))
        return EvalConfig.from_yaml(tmp_path / "eval.yaml")

    plan = make_plan(fake.base_url, skill=parse_agent_model("openrouter:/qwen/qwen3-8b"),
                     subagent=parse_agent_model("openrouter:/qwen/qwen3-8b"),
                     routing=RoutingTable.from_dict({"models": {"qwen/qwen3-8b": {"only": ["novita"]}}}))

    # run-level auto, one judge pinned to json_schema → FAIL for that judge
    c = cfg([{"name": "strict", "prompt": "p", "verdict": "json_schema"}])
    assert judge_verdict_policies(c) == {"qwen/qwen3-8b": {"json_schema"}}
    with pytest.raises(ConfigError, match="pins json_schema"):
        run_preflight(plan, level="strict", judges=judge_routing_keys(c),
                      verdict="auto", judge_verdicts=judge_verdict_policies(c))

    # run-level json_schema, the only judge pinned to tool → no structured-output
    # failure; Decision 25's forcing check applies instead (novita cannot force)
    c = cfg([{"name": "legacy", "prompt": "p", "verdict": "tool"}], run_policy="json_schema")
    assert judge_verdict_policies(c) == {"qwen/qwen3-8b": {"tool"}}
    res = run_preflight(plan, level="warn", judges=judge_routing_keys(c),
                        verdict="json_schema", judge_verdicts=judge_verdict_policies(c))
    assert not any("structured_outputs" in m for m in res.failures + res.warnings)
    assert any("tool_choice: function" in m for m in res.failures)

    # two judges on the key, auto and json_schema: both policies are reported
    c = cfg([{"name": "a", "prompt": "p"}, {"name": "b", "prompt": "p", "verdict": "json_schema"}])
    assert judge_verdict_policies(c) == {"qwen/qwen3-8b": {"auto", "json_schema"}}
    res = run_preflight(plan, level="warn", judges=judge_routing_keys(c),
                        verdict="auto", judge_verdicts=judge_verdict_policies(c))
    assert any("(judges on verdict: json_schema)" in m and "pins json_schema" in m for m in res.failures)
    assert any("(judges on verdict: auto)" in m and "forced-tool transport" in m for m in res.warnings)

    # a non-openrouter judge contributes no policy
    c = cfg([{"name": "x", "prompt": "p", "model": "claude-opus-5-5", "verdict": "json_schema"}])
    assert judge_verdict_policies(c) == {}
