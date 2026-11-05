"""Reconcile (spec 014): cost from a truth source or null, never the estimate;
coverage-driven source selection and confidence; per-model join; routing
audit; provider/budget blocks; the null-cost arithmetic; and the single
writer of run_result.json."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_eval.providers.base import ProviderPlan, parse_agent_model  # noqa: E402
from agent_eval.providers.ledger import Ledger, make_record  # noqa: E402
from agent_eval.providers.openrouter.catalog import ModelCatalog  # noqa: E402
from agent_eval.providers.openrouter.generation import KeyUsageDelta  # noqa: E402
from agent_eval.providers.openrouter.routing import RoutingTable  # noqa: E402
from agent_eval.providers.reconcile import (  # noqa: E402
    aggregate_case_costs, is_real_cost_source, normalize_cost_source, reconcile, write_run_result)

SLUG = "z-ai/glm-5.2"
PERMASLUG = "z-ai/glm-5.2-20260616"


def _plan(*, pins=True, enforcement="audit", key_scope="operator", run_usd=None,
          dedicated=False, runner="claude-code"):
    table = RoutingTable.from_dict(
        {"models": {SLUG: {"order": ["z-ai", "novita"], "allow_fallbacks": False,
                          "quantizations": ["fp8"]}}} if pins else {})
    if dedicated:
        table = SimpleNamespace(for_model=table.for_model, has_entry=table.has_entry,
                                models=table.models, policy="strict")
    plan = ProviderPlan(
        kind="openrouter", base_url="https://openrouter.ai/api", key_scope=key_scope,
        key="sk-test", key_env="OPENROUTER_API_KEY",
        skill=parse_agent_model(f"openrouter:/{SLUG}:exacto"),
        subagent=parse_agent_model(f"openrouter:/{SLUG}"), hook=None, background_model=None,
        routing=table, enforcement=enforcement, run_id="run-1", runner=runner,
        attribution=SimpleNamespace(referer=None, title="t", run_id_header=False),
        cli_budget_inflation=50, budget_run_usd=run_usd)
    if dedicated:
        object.__setattr__(plan, "budget", SimpleNamespace(dedicated_key=True))
    return plan


def _catalog():
    return ModelCatalog.from_snapshot({
        "providers": [{"slug": "novita", "name": "Novita"}, {"slug": "z-ai", "name": "Z.AI"},
                      {"slug": "deepinfra", "name": "DeepInfra"}],
        "models": [{"id": SLUG, "canonical_slug": PERMASLUG}],
        "endpoints": {SLUG: [
            {"provider_name": "Novita", "tag": "novita/fp8", "quantization": "fp8"},
            {"provider_name": "Z.AI", "tag": "z-ai", "quantization": "fp8"},
            {"provider_name": "DeepInfra", "tag": "deepinfra/bf16", "quantization": "bf16"}]}})


def _row(gen_id, cost, provider, *, status="ok", role="agent", case_id="c1",
         requested=f"{SLUG}:exacto", echo=SLUG, served=PERMASLUG):
    return make_record(role=role, source="generation", status=status, run_id="run-1",
                       case_id=case_id, gen_id=gen_id, model_requested=requested,
                       model_echo=echo, model_served=served, provider=provider,
                       provider_name=provider.title() if provider else None, cost_usd=cost)


def _payload(ids, estimate=1.5, per_model=True):
    p = {"exit_code": 0, "cost_usd": estimate, "message_ids": list(ids),
         "eval_params": {"max_budget_usd": 0.5}}
    if per_model:
        p["per_model_usage"] = {SLUG: {"input": 10, "output": 5, "cost_usd": estimate}}
    return p


# --- no-op cases -------------------------------------------------------------------

def test_provider_inactive_and_no_ledger_leaves_the_payload_untouched():
    payload = {"cost_usd": 2.0, "per_model_usage": {"m": {"cost_usd": 2.0}}}
    assert reconcile(payload, [], None) is payload


def test_plain_execution_env_run_without_a_plan_is_untouched():
    """An operator-run Anthropic-compatible endpoint through execution.env has
    no plan and no ledger: reconcile does nothing (the runner labels its own
    estimate)."""
    payload = {"cost_usd": 0.4, "cost_source": "runner:estimate"}
    assert reconcile(payload, [], None) == payload


# --- source selection ---------------------------------------------------------------

@pytest.mark.parametrize("enforcement", ["audit", "key-guardrail"])
@pytest.mark.parametrize("runner", ["claude-code", "harbor-podman", "harbor-k8s"])
def test_full_coverage_sums_generation_rows_never_the_estimate(enforcement, runner):
    rows = [_row("gen-1", 0.01, "novita"), _row("gen-2", 0.02, "z-ai")]
    out = reconcile(_payload(["gen-1", "gen-2"]), rows,
                    _plan(enforcement=enforcement, runner=runner), catalog=_catalog())
    assert out["cost_usd"] == pytest.approx(0.03)
    assert out["cost_source"] == "openrouter:generation"
    assert out["cost_confidence"] == "high"
    assert out["cost_usd_estimate"] == 1.5                 # kept, never summed
    assert out["cost_coverage"]["coverage"] == 1.0
    assert out["provider"]["transport"] == "direct" and out["provider"]["runner"] == runner
    assert out["provider"]["key_hash"].startswith("sha256:") and len(out["provider"]["key_hash"]) == 15
    assert out["provider"]["key_exposed_to_agent"] is True
    assert out["budget"]["enforcement"] == ("key-guardrail" if enforcement == "key-guardrail" else "cli-estimate")
    assert out["budget"]["cli_cap_usd"] == 25.0            # 0.5 × 50


def test_estimate_is_set_once_and_idempotent():
    rows = [_row("gen-1", 0.01, "novita")]
    first = reconcile(_payload(["gen-1"], estimate=1.5), rows, _plan())
    second = reconcile(first, rows, _plan())
    assert first["cost_usd_estimate"] == second["cost_usd_estimate"] == 1.5
    assert second["cost_usd"] == 0.01


def test_low_coverage_uses_the_key_usage_delta():
    rows = [_row("gen-1", 0.01, "novita")]
    ids = ["gen-1", "gen-2", "gen-3", "gen-4", "gen-5"]          # 20 % coverage
    out = reconcile(_payload(ids), rows, _plan(), key_usage=KeyUsageDelta(1.0, 1.07, 21.0))
    assert out["cost_usd"] == pytest.approx(0.07)
    assert out["cost_source"] == "openrouter:key-usage"
    assert out["cost_confidence"] == "low"                      # shared key
    assert out["cost_coverage"]["key_usage_settle_s"] == 21.0
    dedicated = reconcile(_payload(ids), rows, _plan(key_scope="per-run"),
                          key_usage=KeyUsageDelta(1.0, 1.07, 21.0))
    assert dedicated["cost_confidence"] == "medium"


def test_key_usage_row_in_the_ledger_serves_as_the_delta():
    rows = [_row("gen-1", 0.01, "novita"),
            make_record(role="key-usage", source="key-usage", run_id="run-1", cost_usd=0.2)]
    out = reconcile(_payload(["gen-1", "gen-2", "gen-3"]), rows, _plan())
    assert out["cost_source"] == "openrouter:key-usage" and out["cost_usd"] == 0.2


def test_neither_source_is_unavailable_unless_estimate_is_allowed():
    out = reconcile(_payload(["gen-1", "gen-2"]), [], _plan())
    assert out["cost_usd"] is None and out["cost_source"] == "unavailable"
    assert out["cost_confidence"] is None
    assert out["routing"]["audit_complete"] is False
    allowed = reconcile(_payload(["gen-1", "gen-2"], estimate=0.9), [], _plan(), allow_estimate=True)
    assert allowed["cost_usd"] == 0.9 and allowed["cost_source"] == "runner:estimate"


def test_current_generation_ids_drive_coverage_not_message_ids():
    payload = {"exit_code": 0, "cost_usd": 1.5,
               "message_ids": ["msg-1", "msg-2"],
               "generation_ids": ["gen-1", "gen-2"],
               "per_model_usage": {SLUG: {"input": 10, "output": 5, "cost_usd": 1.5}}}
    rows = [_row("gen-1", 0.01, "novita"), _row("gen-2", 0.02, "z-ai")]
    out = reconcile(payload, rows, _plan())
    assert out["cost_coverage"]["requests"] == 2
    assert out["cost_usd"] == pytest.approx(0.03)


def test_cross_check_deviation_lowers_confidence_and_warns():
    rows = [_row("gen-1", 0.01, "novita"), _row("gen-2", 0.02, "z-ai")]
    out = reconcile(_payload(["gen-1", "gen-2"]), rows, _plan(),
                    key_usage=KeyUsageDelta(0.0, 0.05, 20.0))      # 40 % off
    assert out["cost_source"] == "openrouter:generation"          # still the ledger sum
    assert out["cost_confidence"] == "low"
    assert any("differs from key-usage delta" in w for w in out["cost_warnings"])
    close = reconcile(_payload(["gen-1", "gen-2"]), rows, _plan(),
                      key_usage=KeyUsageDelta(0.0, 0.0305, 20.0))  # 1.7 % off
    assert close["cost_confidence"] == "high"


def test_medium_confidence_band_and_backfill_failed_accounting():
    rows = [_row(f"gen-{i}", 0.01, "novita") for i in range(9)] + \
           [_row("gen-9", None, None, status="backfill_failed")]
    out = reconcile(_payload([f"gen-{i}" for i in range(10)]), rows, _plan())
    assert out["cost_coverage"]["coverage"] == 0.9 and out["cost_confidence"] == "medium"
    assert out["cost_coverage"]["requests_missing_cost"] == 1
    assert out["providers"]["unknown"] == {"requests": 1, "cost_usd": None}
    assert out["routing"]["unattributed"] == 1 and out["routing"]["audit_complete"] is False
    assert out["routing"]["degraded"] is True and out["routing"]["degraded_reason"] == "unattributed"
    assert any("unpriced" in w for w in out["cost_warnings"])


def test_retried_generation_rows_are_collapsed_per_id():
    """The ledger is append-only: a backfill_failed row followed by a later ok
    row (run-end retry, offline pass) is one generation — counted once, never
    unattributed, never double-billed."""
    rows = [_row("gen-1", None, None, status="backfill_failed"),
            _row("gen-1", 0.01, "novita"),
            _row("gen-2", 0.02, "z-ai"), _row("gen-2", 0.02, "z-ai"),        # offline re-append
            _row("gen-3", 0.03, "novita"), _row("gen-3", None, None, status="backfill_failed")]
    out = reconcile(_payload(["gen-1", "gen-2", "gen-3"]), rows, _plan(), catalog=_catalog())
    assert out["cost_usd"] == pytest.approx(0.06)
    assert out["cost_coverage"]["requests_priced"] == 3
    assert out["cost_coverage"]["requests_missing_cost"] == 0
    assert out["routing"]["unattributed"] == 0 and out["routing"]["audit_complete"] is True
    assert out["providers"] == {"novita": {"requests": 2, "cost_usd": 0.04},
                                "z-ai": {"requests": 1, "cost_usd": 0.02}}
    assert "unknown" not in out["providers"]


# --- hook cost, providers, per-model join ------------------------------------------

def test_hook_cost_is_separate_and_excluded_from_cost_usd():
    rows = [_row("gen-1", 0.01, "novita"), _row("gen-h", 0.004, "novita", role="hook")]
    out = reconcile(_payload(["gen-1", "gen-h"]), rows, _plan(), catalog=_catalog())
    assert out["cost_usd"] == 0.01 and out["hook_cost_usd"] == 0.004
    assert out["providers"]["novita"] == {"requests": 2, "cost_usd": 0.014}
    assert out["per_model_usage"][SLUG]["cost_usd"] == 0.01       # agent rows only


@pytest.mark.parametrize("requested, echo", [
    (SLUG, SLUG), (f"{SLUG}:exacto", SLUG), (f"{SLUG}[1m]", SLUG)])
def test_per_model_join_matches_the_bare_slug_echo(requested, echo):
    rows = [_row("gen-1", 0.03, "z-ai", requested=requested, echo=echo)]
    out = reconcile(_payload(["gen-1"]), rows, _plan(), catalog=_catalog())
    stats = out["per_model_usage"][SLUG]
    assert stats["cost_usd"] == 0.03 and stats["cost_usd_estimate"] == 1.5
    assert stats["providers"] == ["z-ai"]


def test_per_model_join_via_permaslug_catalog_and_single_model_fallback():
    rows = [_row("gen-1", 0.03, "z-ai", requested=PERMASLUG, echo=PERMASLUG)]
    via_catalog = reconcile(_payload(["gen-1"]), rows, _plan(), catalog=_catalog())
    assert via_catalog["per_model_usage"][SLUG]["cost_usd"] == 0.03
    payload = _payload(["gen-1"], per_model=False)
    payload["per_model_usage"] = {"something/else": {"cost_usd": 1.5}}
    fallback = reconcile(payload, rows, _plan())          # one ledger key, one usage key
    assert fallback["per_model_usage"]["something/else"]["cost_usd"] == 0.03


def test_per_model_join_unmatched_key_is_null_with_a_warning():
    rows = [_row("gen-1", 0.03, "z-ai"), _row("gen-2", 0.01, "novita", requested="qwen/qwen3-8b", echo="qwen/qwen3-8b", served=None)]
    payload = _payload(["gen-1", "gen-2"])
    payload["per_model_usage"]["other/model"] = {"input": 1, "output": 1, "cost_usd": 0.2}
    out = reconcile(payload, rows, _plan(), catalog=_catalog())
    assert out["per_model_usage"]["other/model"]["cost_usd"] is None
    assert out["per_model_usage"]["other/model"]["cost_usd_estimate"] == 0.2
    assert any("no ledger rows for modelUsage key 'other/model'" in w for w in out["cost_warnings"])
    assert any("match no modelUsage key" in w for w in out["cost_warnings"])


# --- routing audit ------------------------------------------------------------------

def test_routing_audit_marks_violations_and_recovers_quantization():
    rows = [_row("gen-1", 0.01, "novita"), _row("gen-2", 0.02, "deepinfra")]
    out = reconcile(_payload(["gen-1", "gen-2"]), rows, _plan(), catalog=_catalog())
    routing = out["routing"]
    assert (routing["audited"], routing["compliant"]) == (2, 1)
    assert routing["violations"] == [{"gen_id": "gen-2", "case_id": "c1", "provider": "deepinfra",
                                      "quantization": "bf16", "expected": ["novita", "z-ai"],
                                      "expected_quantizations": ["fp8"]}]
    assert routing["served"] == {"novita/fp8": 1, "deepinfra/bf16": 1}
    assert routing["degraded"] is True and routing["degraded_reason"] == "violations"
    assert routing["enforcement"] == "audit" and routing["policy"] == "strict"
    assert routing["declared"][SLUG]["order"] == ["z-ai", "novita"]
    assert routing["sha"]
    assert out["cost_confidence"] == "high"                 # audit and confidence are independent


def test_unpinned_key_is_never_audited():
    rows = [_row("gen-1", 0.01, "deepinfra")]
    out = reconcile(_payload(["gen-1"]), rows, _plan(pins=False), catalog=_catalog())
    assert out["routing"]["audited"] == 0 and out["routing"]["violations"] == []
    assert out["routing"]["degraded"] is False and out["routing"]["audit_complete"] is True
    assert out["routing"]["enforcement"] == "none"           # nothing declared, nothing enforced


def test_policy_warn_flags_but_does_not_degrade():
    plan = _plan()
    table = RoutingTable.from_dict({"models": {SLUG: {"order": ["z-ai"], "allow_fallbacks": False}}})
    warn_table = SimpleNamespace(for_model=table.for_model, has_entry=table.has_entry,
                                 models=table.models, policy="warn")
    object.__setattr__(plan, "routing", warn_table)
    out = reconcile(_payload(["gen-1"]), [_row("gen-1", 0.01, "novita")], plan, catalog=_catalog())
    assert out["routing"]["violations"] and out["routing"]["degraded"] is False


# --- budget ---------------------------------------------------------------------------

def test_post_hoc_run_budget_exceeded():
    rows = [_row("gen-1", 0.06, "novita")]
    out = reconcile(_payload(["gen-1"]), rows, _plan(run_usd=0.05))
    assert out["budget"]["run_usd"] == 0.05
    assert out["budget"]["exceeded"] == "run" and out["budget"]["exceeded_reason"] == "post-hoc"
    assert out["budget"]["overshoot_usd"] == pytest.approx(0.01)
    within = reconcile(_payload(["gen-1"]), rows, _plan(run_usd=1.0))
    assert within["budget"]["exceeded"] is None


# --- legacy literals and aggregates ---------------------------------------------------

def test_legacy_cost_source_literals_are_recognised():
    assert normalize_cost_source("openrouter-reconciled") == "openrouter:generation"
    assert normalize_cost_source("runner-reported") == "runner:reported"
    assert normalize_cost_source("harness-estimate") == "harness:estimate"
    assert is_real_cost_source("openrouter-reconciled") and is_real_cost_source("openrouter:key-usage")
    assert not is_real_cost_source("runner:estimate") and not is_real_cost_source(None)


def test_case_aggregate_null_cost_arithmetic():
    assert aggregate_case_costs({"a": {"cost_usd": 1.0}, "b": {"cost_usd": 2.5}}) == (3.5, 2)
    assert aggregate_case_costs({"a": {"cost_usd": 1.0}, "b": {"cost_usd": None}}) == (None, 1)
    assert aggregate_case_costs({}) == (None, 0)


# --- the writer ------------------------------------------------------------------------

def test_write_run_result_no_plan_no_ledger_writes_the_payload_unchanged(tmp_path):
    path = tmp_path / "run_result.json"
    payload = {"exit_code": 0, "cost_usd": 1.25, "per_case": {}}
    assert write_run_result(path, payload) == payload
    assert json.loads(path.read_text()) == payload
    assert path.read_text().endswith("\n")


def test_write_run_result_scopes_case_rows_and_reads_the_run_ledger(tmp_path, capsys):
    ledger = Ledger.for_run(tmp_path)
    ledger.append(_row("gen-1", 0.01, "novita", case_id="c1"))
    ledger.append(_row("gen-2", 0.02, "novita", case_id="c2"))
    plan = _plan(pins=False)
    c1 = write_run_result(tmp_path / "cases" / "c1" / "run_result.json",
                          {"cost_usd": 3.0, "message_ids": ["gen-1"]}, plan=plan)
    assert c1["cost_usd"] == 0.01 and c1["cost_source"] == "openrouter:generation"
    c3 = write_run_result(tmp_path / "cases" / "c3" / "run_result.json",
                          {"cost_usd": 3.0, "message_ids": ["gen-9"]}, plan=plan)
    assert c3["cost_usd"] is None and c3["cost_source"] == "unavailable"
    # gen-9 has no row yet: still inside the backfill window, so no warning —
    # the run-end pass converges it. A case with nothing pending does warn.
    assert "cost_source unavailable" not in capsys.readouterr().err
    write_run_result(tmp_path / "cases" / "c4" / "run_result.json",
                     {"cost_usd": 3.0, "message_ids": []}, plan=plan)
    assert "cost_source unavailable" in capsys.readouterr().err
    # run-level aggregate follows the per-case arithmetic: c2 is unpriced, so
    # the run total is not a partial sum — null (no key-usage delta landed).
    agg = write_run_result(tmp_path / "run_result.json",
                           {"cost_usd": None, "cost_usd_estimate": 9.0,
                            "per_case": {"c1": c1, "c2": {"cost_usd": None}},
                            "message_ids": ["gen-1", "gen-2"]}, plan=plan)
    assert agg["cost_usd"] is None and agg["cost_source"] == "unavailable"
    assert agg["cost_usd_estimate"] == 9.0                         # never derived from a reconciled value
    assert any("1 of 2 cases are unpriced" in w for w in agg["cost_warnings"])
    c2 = write_run_result(tmp_path / "cases" / "c2" / "run_result.json",
                          {"cost_usd": 3.0, "message_ids": ["gen-2"]}, plan=plan)
    priced = write_run_result(tmp_path / "run_result.json",
                              {"cost_usd": None, "per_case": {"c1": c1, "c2": c2},
                               "message_ids": ["gen-1", "gen-2"]}, plan=plan)
    assert priced["cost_usd"] == pytest.approx(0.03) and priced["cost_source"] == "openrouter:generation"


def test_aggregate_with_an_unpriced_case_falls_back_to_the_key_usage_delta():
    per_case = {"c1": {"cost_usd": 0.01, "cost_source": "openrouter:generation"},
                "c2": {"cost_usd": None, "cost_source": "unavailable"}}
    out = reconcile({"cost_usd": None, "per_case": per_case, "message_ids": ["gen-1", "gen-2"]},
                    [_row("gen-1", 0.01, "novita")], _plan(), key_usage=KeyUsageDelta(0.0, 0.05, 20.0),
                    is_aggregate=True)
    assert out["cost_usd"] == pytest.approx(0.05) and out["cost_source"] == "openrouter:key-usage"


def test_write_run_result_with_a_ledger_but_no_plan_still_reconciles_cost(tmp_path):
    """An offline re-reconcile: rows exist, no plan object — cost fields are
    derived, provider/routing blocks are not (they need the plan)."""
    ledger = Ledger.for_run(tmp_path)
    ledger.append(_row("gen-1", 0.01, "novita", case_id="c1"))
    out = write_run_result(tmp_path / "cases" / "c1" / "run_result.json",
                           {"cost_usd": 3.0, "message_ids": ["gen-1"]})
    assert out["cost_usd"] == 0.01 and out["cost_source"] == "openrouter:generation"
    assert "provider" not in out and "routing" not in out
