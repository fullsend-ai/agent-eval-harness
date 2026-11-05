"""Routing audit (spec 014): did every billed generation land on a declared
provider? A pure join of the ledger rows against the routing table and the
endpoint catalog; it never repairs anything — a violation is a billed, kept
generation whose provider was outside the declared set.
"""

from __future__ import annotations

from typing import Iterable, Optional

from agent_eval.providers.base import routing_key
from agent_eval.providers.openrouter.routing import routing_sha


def _table_for(plan):
    return getattr(plan, "routing", None)


def pinned_providers(spec) -> Optional[set]:
    """The provider slugs a declaration pins to, or None when unpinned.
    ``only`` restricts, ``order`` + ``allow_fallbacks: false`` restricts; an
    ``order`` with fallbacks allowed is a preference, not a pin."""
    if spec is None:
        return None
    if spec.only:
        return set(spec.only)
    if spec.order and spec.allow_fallbacks is False:
        return set(spec.order)
    return None


def routing_audit(rows: Iterable[dict], plan, catalog=None, *, policy: str = "strict",
                  enforcement: str = "audit", snapshot: Optional[str] = None) -> dict:
    """The Reconcile ``routing`` block for ``rows`` (agent/hook generation rows).

    For each ``status: ok`` row under a pinned routing key the served
    provider (slug via the catalog) is compared with the pinned set;
    quantization/endpoint tag are recovered from the catalog when it can
    answer unambiguously. ``backfill_failed`` rows are unattributed — never
    inferred compliant or violating. The audit is complete when no row is
    unattributed or pending.
    """
    table = _table_for(plan)
    served: dict = {}
    violations = []
    audited = compliant = unattributed = 0
    keys_seen = set()
    for row in rows:
        if row.get("source") != "generation" or row.get("role") not in ("agent", "hook"):
            continue
        key = routing_key(row.get("model") or row.get("model_requested") or "")
        spec = table.for_model(key) if (table is not None and key) else None
        pins = pinned_providers(spec)
        if row.get("status") != "ok":
            if pins is not None:
                unattributed += 1
            continue
        provider = row.get("provider")
        if not provider and row.get("provider_name"):
            provider = (catalog.provider_slug(row["provider_name"]) if catalog is not None
                        else row["provider_name"].lower())
        quant, tag = row.get("quantization"), row.get("endpoint_tag")
        if catalog is not None and provider and (quant is None or tag is None):
            # The endpoint catalog is keyed by the routing key; the dated
            # permaslug maps back to it through /models when the echo differs.
            candidates = [key] if key else []
            served_model = row.get("model_served")
            if served_model:
                try:
                    mapped = catalog.canonical_to_id(served_model)
                except Exception:
                    mapped = None
                for cand in (routing_key(mapped) if mapped else None, routing_key(served_model)):
                    if cand and cand not in candidates:
                        candidates.append(cand)
            q = t = None
            for cand in candidates:
                try:
                    q, t = catalog.quantization_for(cand, provider)
                except Exception:
                    q, t = None, None
                if q is not None or t is not None:
                    break
            quant = quant if quant is not None else q
            tag = tag if tag is not None else t
        label = tag or (f"{provider}/{quant}" if provider and quant else provider or "unknown")
        served[label] = served.get(label, 0) + 1
        if pins is None:
            continue
        keys_seen.add(key)
        audited += 1
        if not provider:
            unattributed += 1
            continue
        allowed = provider in pins
        if allowed and spec.quantizations and quant is not None and quant not in spec.quantizations:
            allowed = False
        if allowed:
            compliant += 1
        else:
            violations.append({
                "gen_id": row.get("gen_id"), "case_id": row.get("case_id"),
                "provider": provider, "quantization": quant,
                "expected": sorted(pins),
                "expected_quantizations": list(spec.quantizations) if spec.quantizations else None,
            })
    degraded_reason = None
    if violations:
        degraded_reason = "violations"
    elif unattributed:
        degraded_reason = "unattributed"
    declared = {}
    if table is not None:
        for key in sorted(set(getattr(table, "models", {}) or {}) | keys_seen):
            spec = table.for_model(key)
            if spec is None or spec.is_empty:
                continue
            declared[key] = {k: v for k, v in spec.to_dict().items()
                             if k in ("order", "only", "ignore", "allow_fallbacks", "quantizations")}
    # A pinned run with zero generation rows means collection or backfill
    # failed, not that every request was compliant.
    audit_complete = unattributed == 0 and (audited > 0 or not declared)
    return {
        # Nothing pinned means nothing to enforce, whatever the configured level.
        "enforcement": enforcement if declared else "none",
        "policy": policy,
        "sha": routing_sha({k: v for k, v in declared.items()}) if declared else None,
        "snapshot": snapshot,
        "declared": declared,
        "audited": audited,
        "compliant": compliant,
        "violations": violations,
        "unattributed": unattributed,
        "degraded": bool(degraded_reason) and policy == "strict",
        "degraded_reason": degraded_reason,
        "served": served,
        "audit_complete": audit_complete,
    }
