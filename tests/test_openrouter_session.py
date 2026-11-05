"""The run-scoped provider session (spec 014): sighting, backfill, key usage,
the final reconcile pass and the plan's close hook — over the loopback fake."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_eval.providers.openrouter.session import ProviderSession  # noqa: E402
from agent_eval.providers.reconcile import write_run_result  # noqa: E402
from openrouter_fakes import GEN_COST, FakeOpenRouter, make_plan  # noqa: E402


class FastSession(ProviderSession):
    """Real HTTP to the fake, no waiting."""

    def __init__(self, *a, **kw):
        kw.setdefault("first_poll_s", 0.0)
        kw.setdefault("poll_s", 0.01)
        kw.setdefault("give_up_s", 0.3)
        kw.setdefault("settle_s", 0.0)
        kw.setdefault("key_poll_s", 0.0)
        kw.setdefault("key_max_s", 2.0)
        super().__init__(*a, **kw)


@pytest.fixture
def fake():
    srv = FakeOpenRouter()
    srv.base_url = srv.start()
    yield srv
    srv.stop()


def test_bind_sight_finish_and_reconcile(fake, tmp_path):
    plan = make_plan(fake.base_url, run_id="r1")
    session = FastSession(plan, tmp_path, parallelism=2).start()
    plan.attach(session)
    assert session.key_usage_before == 0.5
    b = session.bind("c1")
    assert b.hook_ids_path == tmp_path / "provider" / "hook-ids-c1.jsonl"
    assert b.sight("gen-fake-1", message_index=1, model_echo="z-ai/glm-5.2")
    assert not b.sight("gen-fake-1")                       # once
    assert not b.sight("msg_anthropic_1")                  # not a generation id
    b.hook_ids_path.parent.mkdir(parents=True, exist_ok=True)
    b.hook_ids_path.write_text(json.dumps({"id": "gen-fake-hook", "model": "z-ai/glm-5.2"}) + "\n")
    assert b.after_run(["gen-fake-1", "gen-fake-2"]) == 2   # gen-fake-2 + the hook id
    # per-case write inside the lag: pending, no warning; then the run-end pass
    case = write_run_result(tmp_path / "cases" / "c1" / "run_result.json",
                            {"exit_code": 0, "cost_usd": 0.5,
                             "message_ids": ["msg-fake-1", "msg-fake-2"],
                             "generation_ids": ["gen-fake-1", "gen-fake-2"]},
                            plan=plan, ledger=session.ledger)
    run = write_run_result(tmp_path / "run_result.json",
                           {"exit_code": 0, "cost_usd": None, "per_case": {"c1": case},
                            "message_ids": ["msg-fake-1", "msg-fake-2"],
                            "generation_ids": ["gen-fake-1", "gen-fake-2"],
                            "eval_params": {"max_budget_usd": 5}},
                           plan=plan, ledger=session.ledger)
    assert run["cost_source"] in ("unavailable", "openrouter:generation")
    delta = session.finish()
    assert delta is not None and delta.delta_usd == pytest.approx(3 * GEN_COST, abs=1e-6)
    final = session.reconcile_run()
    assert final["cost_usd"] == pytest.approx(2 * GEN_COST, abs=1e-6) and final["cost_source"] == "openrouter:generation"
    assert final["per_case"]["c1"]["cost_usd"] == pytest.approx(2 * GEN_COST, abs=1e-6)
    assert final["per_case"]["c1"]["cost_usd_estimate"] == 0.5
    assert final["hook_cost_usd"] == pytest.approx(GEN_COST, abs=1e-6)
    assert final["cost_coverage"]["key_usage_delta_usd"] == pytest.approx(3 * GEN_COST, abs=1e-6)
    assert final["routing"]["compliant"] == 3 and final["routing"]["violations"] == []
    assert not [w for w in final["cost_warnings"] if "unpriced" in w]
    on_disk = json.loads((tmp_path / "cases" / "c1" / "run_result.json").read_text())
    assert on_disk["cost_source"] == "openrouter:generation"
    rows = session.ledger.read()
    assert {r["role"] for r in rows} == {"agent", "hook"} and all(r["status"] == "ok" for r in rows)
    assert all(r["provider"] == "novita" for r in rows)
    plan.close()                                            # idempotent after finish + reconcile
    assert session.closed and session.finish() is delta


def test_close_on_a_crash_path_does_finish_and_reconcile(fake, tmp_path):
    plan = make_plan(fake.base_url)
    session = FastSession(plan, tmp_path).start()
    plan.attach(session)
    session.bind("c1").sight("gen-fake-9")
    write_run_result(tmp_path / "run_result.json", {"exit_code": -1,
                     "message_ids": ["msg-fake-9"], "generation_ids": ["gen-fake-9"]},
                     plan=plan, ledger=session.ledger)
    plan.close()
    final = json.loads((tmp_path / "run_result.json").read_text())
    assert final["cost_source"] == "openrouter:generation" and final["cost_usd"] == pytest.approx(GEN_COST, abs=1e-6)
    assert session.finished and session.reconciled


def test_snapshot_catalog_is_used_offline(tmp_path):
    from agent_eval.providers.openrouter.preflight import run_preflight

    srv = FakeOpenRouter()
    base = srv.start()
    try:
        plan = make_plan(base)
        pre = run_preflight(plan, level="strict", run_dir=tmp_path)
    finally:
        srv.stop()
    session = FastSession(plan, tmp_path, snapshot=pre.snapshot)     # server gone: frozen view answers
    assert session.snapshot_ref == pre.snapshot["ts"]
    assert session.catalog.provider_slug("Novita") == "novita"
    assert session.catalog.quantization_for("z-ai/glm-5.2", "z-ai") == ("fp8", "z-ai")
    assert session.catalog.quantization_for("z-ai/glm-5.2", "novita") == (None, None)   # two Novita endpoints: ambiguous


def test_key_usage_failure_is_not_fatal(tmp_path):
    plan = make_plan("http://127.0.0.1:9/api")           # nothing listens
    session = FastSession(plan, tmp_path).start()
    assert session.key_usage_before is None
    assert session.finish() is None


def test_cleanups_run_even_when_the_revoke_is_interrupted(fake, tmp_path, monkeypatch):
    """A Ctrl-C during the key revoke must not orphan a host resource (the
    per-run K8s Secret) registered on the session."""
    from agent_eval.providers.openrouter import session as session_mod

    plan = make_plan(fake.base_url)
    session = FastSession(plan, tmp_path)
    session.finished = session.reconciled = True
    cleaned = []
    session.cleanups.append(lambda: cleaned.append("secret"))

    def interrupted(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(session_mod, "revoke_plan_key", interrupted)
    plan.attach(session)
    object.__setattr__(plan, "provisioned", type("PK", (), {"revoked_at": None, "hash": "h"})())
    with pytest.raises(KeyboardInterrupt):
        session.close()
    assert cleaned == ["secret"] and session.cleanups == []

