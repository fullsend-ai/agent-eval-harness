"""Reconcile (spec 014): the pure function every ``run_result.json`` write
goes through.

It joins the run's ledger rows against the transcript's generation ids and
writes the cost-provenance fields — ``cost_usd`` from a truth source or
``null``, never the runner's estimate; ``cost_source``; ``cost_confidence`` by
coverage; ``cost_coverage``; ``cost_warnings``; ``hook_cost_usd``;
``providers``; per-model joins; the ``routing`` audit; the ``provider`` and
``budget`` blocks. With no active plan and no ledger rows it leaves the
payload untouched, so a run without OpenRouter reads exactly as before.

Null-cost arithmetic: no reader re-inflates a ``null`` from the estimate.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterable, Optional

from agent_eval.providers.base import routing_key
from agent_eval.providers.ledger import Ledger, rows_for_case

COVERAGE_SOURCE_MIN = 0.8        # below: key-usage delta becomes the source
COVERAGE_HIGH = 0.95
CROSS_CHECK_TOLERANCE = 0.05     # ledger sum vs key-usage delta

LEGACY_COST_SOURCES = {
    "openrouter-reconciled": "openrouter:generation",
    "runner-reported": "runner:reported",
    "harness-estimate": "harness:estimate",
}
REAL_COST_PREFIXES = ("openrouter:",)


def normalize_cost_source(value) -> Optional[str]:
    """Canonical ``<origin>:<method>`` form; legacy literals recognised."""
    if not value:
        return None
    text = str(value)
    return LEGACY_COST_SOURCES.get(text, text)


def is_real_cost_source(value) -> bool:
    text = normalize_cost_source(value) or ""
    return text.startswith(REAL_COST_PREFIXES)


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _sum(values):
    vals = [v for v in values if _num(v) is not None]
    return round(sum(vals), 6) if vals else None


def _latest_by_gen_id(rows):
    """One row per generation id. The ledger is append-only, so a retried id
    can carry a `backfill_failed` row and a later `ok` row (or two `ok` rows
    from an offline pass): an ok row beats a failed one, otherwise the last
    row wins. Rows without a generation id (key-usage) pass through."""
    out, index = [], {}
    for r in rows:
        gid = r.get("gen_id") if r.get("source") == "generation" else None
        if not gid:
            out.append(r)
            continue
        if gid in index:
            prev = out[index[gid]]
            if prev.get("status") == "ok" and r.get("status") != "ok":
                continue
            out[index[gid]] = r
        else:
            index[gid] = len(out)
            out.append(r)
    return out


def _generation_rows(rows, roles=("agent",)):
    return [r for r in rows if r.get("source") == "generation" and r.get("role") in roles]


def _coverage(message_ids, rows) -> dict:
    from agent_eval.providers.openrouter.generation import coverage

    return coverage(message_ids or [], rows)


def _confidence(cov: dict, key_delta, ledger_sum, *, dedicated: bool) -> Optional[str]:
    coverage_ratio = cov.get("coverage")
    if coverage_ratio is None:
        return None
    deviation = None
    if key_delta is not None and ledger_sum is not None and key_delta > 0:
        deviation = abs(ledger_sum - key_delta) / key_delta
    if coverage_ratio >= COVERAGE_HIGH and (deviation is None or deviation <= CROSS_CHECK_TOLERANCE):
        return "high"
    if coverage_ratio >= COVERAGE_SOURCE_MIN:
        if deviation is not None and deviation > CROSS_CHECK_TOLERANCE:
            return "low"
        return "medium"
    return "medium" if dedicated else "low"


def _per_model_join(per_model_usage: dict, rows: list, catalog, warnings: list) -> dict:
    """Attach reconciled ``cost_usd`` and ``providers`` to each per-model entry
    per the per-model join rule (bare-slug echo, permaslug via the catalog,
    single-model fallback). No proportional split is ever performed."""
    if not per_model_usage:
        return per_model_usage
    cost_by_key: dict = {}
    providers_by_key: dict = {}
    row_keys_for_row = {}
    for row in rows:
        if row.get("status") != "ok":
            continue
        keys = set()
        for field in ("model_requested", "model", "model_echo", "model_served"):
            value = row.get(field)
            if value:
                keys.add(routing_key(value))
        served = row.get("model_served")
        if served and catalog is not None:
            try:
                mapped = catalog.canonical_to_id(served)
            except Exception:
                mapped = None
            if mapped:
                keys.add(routing_key(mapped))
        row_keys_for_row[id(row)] = keys
        primary = routing_key(row.get("model") or row.get("model_requested") or row.get("model_echo") or "")
        cost_by_key.setdefault(primary, 0.0)
        if _num(row.get("cost_usd")) is not None:
            cost_by_key[primary] += row["cost_usd"]
        if row.get("provider"):
            providers_by_key.setdefault(primary, set()).add(row["provider"])
    out = {}
    matched_primaries = set()
    for m, stats in per_model_usage.items():
        stats = dict(stats or {})
        estimate = _num(stats.get("cost_usd"))
        if "cost_usd_estimate" not in stats and estimate is not None:
            stats["cost_usd_estimate"] = estimate
        key = routing_key(m)
        primaries = {routing_key(r.get("model") or r.get("model_requested") or r.get("model_echo") or "")
                     for r in rows if r.get("status") == "ok" and key in row_keys_for_row.get(id(r), set())}
        if not primaries and len(cost_by_key) == 1 and len(per_model_usage) == 1:
            primaries = set(cost_by_key)          # deterministic single-model fallback
        if primaries:
            stats["cost_usd"] = round(sum(cost_by_key.get(p, 0.0) for p in primaries), 6)
            provs = set()
            for p in primaries:
                provs |= providers_by_key.get(p, set())
            stats["providers"] = sorted(provs)
            matched_primaries |= primaries
        else:
            stats["cost_usd"] = None
            stats["providers"] = []
            if cost_by_key:
                warnings.append(f"per-model cost: no ledger rows for modelUsage key {m!r} "
                                f"(candidates: {sorted(cost_by_key)})")
        out[m] = stats
    for primary, cost in cost_by_key.items():
        if primary not in matched_primaries and cost:
            warnings.append(f"per-model cost: ledger rows for {primary!r} (${cost:.4f}) match no "
                            "modelUsage key")
    return out


def _providers_block(rows: list) -> dict:
    out: dict = {}
    for row in rows:
        if row.get("source") != "generation":
            continue
        slug = row.get("provider") or ("unknown" if row.get("status") != "ok" or not row.get("provider_name")
                                       else row.get("provider_name").lower())
        entry = out.setdefault(slug, {"requests": 0, "cost_usd": None})
        entry["requests"] += 1
        cost = _num(row.get("cost_usd"))
        if cost is not None:
            entry["cost_usd"] = round((entry["cost_usd"] or 0.0) + cost, 6)
    return out


def _provider_block(plan) -> dict:
    return {
        "name": plan.kind, "kind": plan.kind, "transport": plan.transport,
        "runner": plan.runner, "base_url": plan.base_url,
        "key_exposed_to_agent": True,
        "key_scope": plan.key_scope,
        "key_hash": f"sha256:{plan.key_hash}" if plan.key_hash else None,
        "background_model": plan.background_model,
    }


def _budget_block(run_result: dict, plan, cost_usd) -> dict:
    invocation = _num((run_result.get("eval_params") or {}).get("max_budget_usd"))
    existing = run_result.get("budget") if isinstance(run_result.get("budget"), dict) else {}
    declared = (run_result.get("eval_params") or {}).get("budget")
    declared = declared if isinstance(declared, dict) else {}
    # The host records the cap it actually passed to the CLI (null = no flag,
    # cap <= 0); derive it only for payloads written without that record.
    cli_cap = existing.get("cli_cap_usd", declared.get("cli_cap_usd"))
    if cli_cap is None and "cli_cap_usd" not in existing and "cli_cap_usd" not in declared \
            and invocation is not None and invocation > 0:
        cli_cap = round(invocation * float(plan.cli_budget_inflation), 4)
    run_usd = plan.budget_run_usd
    block = {
        "invocation_usd": invocation,
        "run_usd": run_usd,
        "cli_cap_usd": cli_cap,
        "enforcement": "key-guardrail" if plan.enforcement == "key-guardrail" else "cli-estimate",
        "exceeded": existing.get("exceeded"),
        "exceeded_reason": existing.get("exceeded_reason"),
        "overshoot_usd": existing.get("overshoot_usd"),
    }
    if run_usd is not None and cost_usd is not None and cost_usd > run_usd:
        block.update({"exceeded": "run", "exceeded_reason": "post-hoc",
                      "overshoot_usd": round(cost_usd - run_usd, 6)})
    return block


def reconcile(run_result: dict, ledger_rows: Iterable[dict], plan=None, *, key_usage=None,
              catalog=None, allow_estimate: bool = False, message_ids: Optional[list] = None,
              case_id: Optional[str] = None, is_aggregate: bool = False) -> dict:
    """Return the reconciled payload (a new dict). Provider inactive and no
    rows → the payload unchanged."""
    rows = _latest_by_gen_id(list(ledger_rows or []))
    if plan is None and not rows:
        return run_result
    out = dict(run_result)
    warnings = list(out.get("cost_warnings") or [])

    # The runner's own number is an estimate on OpenRouter: keep it, once.
    if "cost_usd_estimate" not in out and _num(out.get("cost_usd")) is not None:
        out["cost_usd_estimate"] = out["cost_usd"]
    estimate = _num(out.get("cost_usd_estimate"))

    generation_ids = out.get("generation_ids")
    if generation_ids is None:
        generation_ids = out.get("message_ids") or []
    ids = message_ids if message_ids is not None else generation_ids
    agent_rows = _generation_rows(rows, roles=("agent",))
    hook_rows = _generation_rows(rows, roles=("hook",))
    ok_agent = [r for r in agent_rows if r.get("status") == "ok"]
    ledger_sum = _sum(r.get("cost_usd") for r in ok_agent)
    hook_sum = _sum(r.get("cost_usd") for r in hook_rows if r.get("status") == "ok")
    # The key counts every request on it: the cross-check compares the delta
    # with agent + hook spend, not with the agent rows alone.
    ledgered = None if ledger_sum is None and hook_sum is None else (ledger_sum or 0) + (hook_sum or 0)
    cov = _coverage(ids, agent_rows + hook_rows)
    coverage_ratio = cov.get("coverage")

    key_delta = getattr(key_usage, "delta_usd", None) if key_usage is not None else None
    if key_delta is None and not is_aggregate:
        key_rows = [r for r in rows if r.get("role") == "key-usage"]
        if key_rows:
            key_delta = _num(key_rows[-1].get("cost_usd"))
    dedicated = bool(plan is not None and (plan.key_scope == "per-run"
                                           or getattr(getattr(plan, "budget", None), "dedicated_key", False)))
    # A zero/negative delta while requests happened is inconsistent (read too
    # early, or the counter never moved): a warning, never a small number
    # standing in for spend.
    usable_delta = key_delta if (key_delta is not None and key_delta > 0) else None

    per_case = out.get("per_case") if is_aggregate else None
    if isinstance(per_case, dict) and per_case:
        # The run aggregate follows the per-case arithmetic: every case must be
        # priced for the sum to be spend; otherwise the run-level key-usage
        # delta is the only number that covers the gaps, else null.
        case_costs = [_num((c or {}).get("cost_usd")) for c in per_case.values()]
        case_sources = {(c or {}).get("cost_source") for c in per_case.values()
                        if isinstance(c, dict) and c.get("cost_source")}
        out["cases_priced"] = sum(1 for c in case_costs if c is not None)
        if all(c is not None for c in case_costs):
            out["cost_usd"] = round(sum(case_costs), 6)
            out["cost_source"] = (case_sources.pop() if len(case_sources) == 1
                                  else "openrouter:generation")
        elif usable_delta is not None:
            out["cost_usd"], out["cost_source"] = usable_delta, "openrouter:key-usage"
        else:
            out["cost_usd"] = None
            out["cost_source"] = "runner:estimate" if (allow_estimate and estimate is not None) else "unavailable"
            if out["cost_source"] == "runner:estimate":
                out["cost_usd"] = estimate
        unpriced = sum(1 for c in case_costs if c is None)
        if unpriced:
            warnings.append(f"{unpriced} of {len(case_costs)} cases are unpriced; the run total is "
                            "not a sum of partial spend")
    elif coverage_ratio is not None and coverage_ratio >= COVERAGE_SOURCE_MIN and ledger_sum is not None:
        out["cost_usd"], out["cost_source"] = ledger_sum, "openrouter:generation"
        if key_delta is not None and key_delta > 0 and not is_aggregate:
            deviation = abs(ledgered - key_delta) / key_delta
            if deviation > CROSS_CHECK_TOLERANCE:
                warnings.append(f"ledger sum ${ledgered:.4f} differs from key-usage delta "
                                f"${key_delta:.4f} by {deviation:.1%}")
    elif usable_delta is not None and not is_aggregate:
        out["cost_usd"], out["cost_source"] = usable_delta, "openrouter:key-usage"
    elif ledger_sum is not None and coverage_ratio is None:
        # No transcript ids to measure coverage against (offline re-reconcile);
        # the ledger is all there is.
        out["cost_usd"], out["cost_source"] = ledger_sum, "openrouter:generation"
    else:
        out["cost_usd"] = None
        out["cost_source"] = "runner:estimate" if (allow_estimate and estimate is not None) else "unavailable"
        if out["cost_source"] == "runner:estimate":
            out["cost_usd"] = estimate
    if key_delta is not None and key_delta <= 0 and (cov.get("requests") or 0) > 0:
        warnings.append("key-usage delta is not positive; the key saw no spend or was read too early")
    if out.get("cost_source") in ("openrouter:generation", "openrouter:key-usage") and coverage_ratio is not None \
            and coverage_ratio < 1.0:
        warnings.append(f"{cov['requests_missing_cost'] + cov.get('requests_pending', 0)} of "
                        f"{cov['requests']} requests are unpriced (coverage {coverage_ratio:.0%})")
    out["cost_confidence"] = _confidence(cov, key_delta, ledgered, dedicated=dedicated) \
        if out.get("cost_source", "").startswith("openrouter:") else None
    cov_block = {k: v for k, v in cov.items() if k != "requests_pending"}
    cov_block["key_usage_delta_usd"] = key_delta
    cov_block["key_usage_settle_s"] = getattr(key_usage, "settle_s", None) if key_usage is not None else None
    out["cost_coverage"] = cov_block
    out["hook_cost_usd"] = hook_sum
    out["providers"] = _providers_block(agent_rows + hook_rows)
    if isinstance(out.get("per_model_usage"), dict):
        # modelUsage is the agent process's own view; the hook is a separate
        # harness process and stays out of the per-model join (hook_cost_usd).
        out["per_model_usage"] = _per_model_join(out["per_model_usage"], agent_rows,
                                                 catalog, warnings)
    if plan is not None:
        from agent_eval.providers.openrouter.audit import routing_audit

        routing = getattr(plan, "routing", None)
        out["routing"] = routing_audit(
            agent_rows + hook_rows, plan, catalog,
            policy=getattr(routing, "policy", "strict"), enforcement=plan.enforcement,
            snapshot=out.get("routing", {}).get("snapshot") if isinstance(out.get("routing"), dict) else None)
        if out["routing"]["violations"] or out["routing"]["unattributed"]:
            warnings.append(f"routing audit: {len(out['routing']['violations'])} violation(s), "
                            f"{out['routing']['unattributed']} unattributed")
        out["provider"] = _provider_block(plan)
        out["budget"] = _budget_block(out, plan, _num(out.get("cost_usd")))
    out["cost_warnings"] = list(dict.fromkeys(warnings))
    return out


def aggregate_case_costs(case_results: dict) -> tuple:
    """Case-aggregate cost under the null-cost arithmetic: ``None`` when any
    case is ``None`` (a partial sum is not spend) plus how many were priced."""
    values = [(_num(r.get("cost_usd")) if isinstance(r, dict) else None) for r in case_results.values()]
    priced = sum(1 for v in values if v is not None)
    if not values or priced < len(values):
        return None, priced
    return round(sum(values), 6), priced


def _load_json(path: Path) -> Optional[dict]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def reconcile_run_dir(run_dir, *, plan=None, ledger=None, key_usage=None, catalog=None,
                      allow_estimate: bool = False, warnings=None) -> Optional[dict]:
    """The final reconcile pass over a run dir: every ``cases/<id>/run_result.json``
    (rows that landed after the case was written), then the run-level file with
    the refreshed per-case values and the key-usage delta. Stale ``cost_warnings``
    are recomputed. Returns the run payload, or None without a run-level file."""
    run_dir = Path(run_dir)
    if ledger is None:
        ledger = Ledger.for_run(run_dir)
    cases: dict = {}
    for case_file in sorted(run_dir.glob("cases/*/run_result.json")):
        payload = _load_json(case_file)
        if payload is None:
            continue
        payload.pop("cost_warnings", None)
        cases[case_file.parent.name] = write_run_result(
            case_file, payload, plan=plan, ledger=ledger, catalog=catalog, allow_estimate=allow_estimate)
    run_file = run_dir / "run_result.json"
    payload = _load_json(run_file)
    if payload is None:
        return None
    payload.pop("cost_warnings", None)
    if warnings:
        payload["cost_warnings"] = list(warnings)            # the host's own findings (e.g. a failed revoke)
    if isinstance(payload.get("per_case"), dict):
        payload["per_case"] = {cid: cases.get(cid, rec) for cid, rec in payload["per_case"].items()}
    return write_run_result(run_file, payload, plan=plan, ledger=ledger, key_usage=key_usage,
                            catalog=catalog, allow_estimate=allow_estimate)


def write_run_result(path, payload: dict, *, plan=None, ledger=None, key_usage=None,
                     catalog=None, allow_estimate: bool = False) -> dict:
    """The one writer of ``run_result.json``: reconcile, then dump.

    ``ledger`` is a :class:`Ledger`, an iterable of rows, or ``None`` (then
    ``<run_dir>/provider/ledger.jsonl`` is read when it exists, where the run
    dir is the path's parent or, for ``cases/<id>/run_result.json``, its
    grandparent). No plan and no rows → the payload is written unchanged.
    """
    path = Path(path)
    case_id = None
    run_dir = path.parent
    if run_dir.parent.name == "cases":
        case_id = run_dir.name
        run_dir = run_dir.parent.parent
    if ledger is None:
        ledger_obj = Ledger.for_run(run_dir)
        rows = ledger_obj.read() if ledger_obj.exists() else []
    elif isinstance(ledger, Ledger):
        rows = ledger.read()
    else:
        rows = list(ledger)
    if case_id is not None:
        rows = rows_for_case(rows, case_id)
    is_aggregate = case_id is None and isinstance(payload.get("per_case"), dict)
    reconciled = reconcile(payload, rows, plan, key_usage=key_usage, catalog=catalog,
                           allow_estimate=allow_estimate, case_id=case_id,
                           is_aggregate=is_aggregate)
    if plan is not None and reconciled.get("cost_source") == "unavailable":
        cov = reconciled.get("cost_coverage") if isinstance(reconciled.get("cost_coverage"), dict) else {}
        pending = ((cov.get("requests") or 0) - (cov.get("requests_priced") or 0)
                   - (cov.get("requests_missing_cost") or 0))
        # A per-case write normally lands inside the /generation lag: ids still
        # pending are not a failure yet (the run-end pass converges them).
        if pending <= 0:
            print(f"WARNING: {path}: cost_source unavailable — no /generation row and no "
                  "key-usage delta landed for this write", file=sys.stderr)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(reconciled, f, indent=2)
        f.write("\n")
    return reconciled
