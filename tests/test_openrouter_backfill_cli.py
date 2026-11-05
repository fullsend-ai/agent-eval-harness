"""The offline backfill command (spec 014): re-query the run's unpriced ids
and re-reconcile the run dir, over the loopback fake."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_eval.providers.openrouter import backfill as bf  # noqa: E402
from agent_eval.providers.openrouter.session import ProviderSession  # noqa: E402
from agent_eval.providers.reconcile import write_run_result  # noqa: E402
from openrouter_fakes import FAKE_KEY, GEN_COST, FakeOpenRouter, make_plan  # noqa: E402


def _interrupted_run(tmp_path, plan):
    """A run whose case was written but whose generations never landed."""
    case = write_run_result(tmp_path / "cases" / "c1" / "run_result.json",
                            {"exit_code": 0, "cost_usd": 0.4,
                             "message_ids": ["msg-fake-a", "msg-fake-b"],
                             "generation_ids": ["gen-fake-a", "gen-fake-b"]},
                            plan=plan, ledger=[])
    write_run_result(tmp_path / "run_result.json",
                     {"exit_code": 0, "cost_usd": None, "per_case": {"c1": case},
                      "message_ids": ["msg-fake-a", "msg-fake-b"],
                      "generation_ids": ["gen-fake-a", "gen-fake-b"],
                      "eval_params": {"max_budget_usd": 1}},
                     plan=plan, ledger=[])


def test_offline_backfill_prices_and_re_reconciles(tmp_path, monkeypatch, capsys):
    fake = FakeOpenRouter()
    base = fake.start()
    try:
        plan = make_plan(base)
        _interrupted_run(tmp_path, plan)
        before = json.loads((tmp_path / "run_result.json").read_text())
        assert before["cost_source"] == "unavailable" and before["routing"]["compliant"] == 0
        monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
        # a recorded non-default origin is not trusted silently
        assert bf.main([str(tmp_path), "--give-up-s", "1"]) == 2
        assert f"pass --base-url {base}" in capsys.readouterr().err
        assert fake.priced == []
        assert bf.main([str(tmp_path), "--give-up-s", "1", "--base-url", base]) == 0
    finally:
        fake.stop()
    assert "re-queried 2 id(s), priced 2, failed 0" in capsys.readouterr().out
    run = json.loads((tmp_path / "run_result.json").read_text())
    assert run["cost_source"] == "openrouter:generation" and run["cost_usd"] == pytest.approx(2 * GEN_COST, abs=1e-6)
    assert run["provider"]["base_url"] == base
    # the audit and the budget verdict are recomputed under the rebuilt plan, not carried over
    assert run["routing"]["compliant"] == 2 and run["routing"]["violations"] == []
    assert run["routing"]["declared"] == {"z-ai/glm-5.2": {"order": ["z-ai"] if False else ["novita"], "allow_fallbacks": False}}
    assert run["budget"]["run_usd"] is None and run["budget"]["exceeded"] is None
    case = json.loads((tmp_path / "cases" / "c1" / "run_result.json").read_text())
    assert case["cost_source"] == "openrouter:generation" and case["cost_usd_estimate"] == 0.4
    assert bf.collect_sightings(tmp_path) == [{"gen_id": "gen-fake-a", "case_id": "c1"},
                                              {"gen_id": "gen-fake-b", "case_id": "c1"}]


def test_offline_backfill_with_the_config_rebuilds_the_exact_plan(tmp_path, monkeypatch, capsys):
    import yaml

    fake = FakeOpenRouter()
    base = fake.start()
    try:
        plan = make_plan(base, budget_run_usd=0.001)
        _interrupted_run(tmp_path, plan)
        (tmp_path / "eval.yaml").write_text(yaml.safe_dump({
            "name": "t", "skill": "demo", "dataset": {"path": str(tmp_path)},
            "outputs": [{"path": "output"}], "runner": {"type": "claude-code"},
            "models": {"skill": "openrouter:/z-ai/glm-5.2:exacto", "providers": {"openrouter": {
                "api_key_env": "MY_OR_KEY", "base_url": base, "budget": {"run_usd": 0.001},
                "routing": {"models": {"z-ai/glm-5.2": {"only": ["z-ai"]}}}}}}}))
        monkeypatch.delenv("MY_OR_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        config_args = [str(tmp_path), "--config", str(tmp_path / "eval.yaml"), "--give-up-s", "1"]
        # the config names MY_OR_KEY: unset → a clear error, no request made
        assert bf.main(config_args) == 2
        assert "set MY_OR_KEY" in capsys.readouterr().err and fake.priced == []
        # --key-env wins over the config's api_key_env (only the selected variable is read)
        monkeypatch.setenv("OTHER_KEY", FAKE_KEY)
        monkeypatch.setenv("MY_OR_KEY", "sk-or-wrong")
        assert bf.main(config_args + ["--key-env", "OTHER_KEY"]) == 0
        assert all(auth for path, auth in fake.requests if path == "/api/v1/generation")
        # the config's base_url is the loader's business: no --base-url needed
        monkeypatch.setenv("MY_OR_KEY", FAKE_KEY)
        assert bf.main(config_args) == 0
    finally:
        fake.stop()
    run = json.loads((tmp_path / "run_result.json").read_text())
    assert run["cost_source"] == "openrouter:generation"
    # the config pins z-ai, the fake served Novita: the recomputed audit says so
    assert len(run["routing"]["violations"]) == 2 and run["routing"]["compliant"] == 0
    assert run["budget"]["exceeded"] == "run" and run["budget"]["exceeded_reason"] == "post-hoc"


def test_checked_base_url_rules():
    from agent_eval.providers.base import ConfigError

    assert bf.checked_base_url("https://openrouter.ai/api", explicit=False) == "https://openrouter.ai/api"
    with pytest.raises(ConfigError, match="pass --base-url"):
        bf.checked_base_url("https://gateway.example/api", explicit=False)
    assert bf.checked_base_url("https://gateway.example/api", explicit=True)
    with pytest.raises(ValueError, match="https"):
        bf.checked_base_url("http://gateway.example/api", explicit=True)
    assert bf.checked_base_url("http://127.0.0.1:9/api", explicit=True)


def test_collect_sightings_keeps_legacy_message_ids_as_fallback(tmp_path):
    (tmp_path / "run_result.json").write_text(json.dumps({
        "message_ids": ["gen-legacy"], "generation_ids": ["gen-current"]}))
    assert bf.collect_sightings(tmp_path) == [{"gen_id": "gen-current", "case_id": None}]

    (tmp_path / "run_result.json").write_text(json.dumps({"message_ids": ["gen-legacy"]}))
    assert bf.collect_sightings(tmp_path) == [{"gen_id": "gen-legacy", "case_id": None}]


def test_offline_backfill_needs_the_key_and_a_run(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert bf.main([str(tmp_path)]) == 2
    assert "no run_result.json" in capsys.readouterr().err
    (tmp_path / "run_result.json").write_text(json.dumps({"exit_code": 0, "message_ids": []}))
    assert bf.main([str(tmp_path)]) == 2
    assert "set OPENROUTER_API_KEY" in capsys.readouterr().err
    assert isinstance(ProviderSession, type)          # the session module is the online counterpart
