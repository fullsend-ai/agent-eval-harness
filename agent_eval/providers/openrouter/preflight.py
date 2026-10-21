"""Preflight (spec 014): the checks that run before any spend.

The agent path carries no routing body (Decision 1), so this is the only place
the declared pins are checked before the first request. Per routing key used
by an agent role (skill, subagent, hook, background model) or by an
``openrouter:/`` judge: the slug exists in the public catalog; each pinned
provider serves it, at a declared quantization when ``quantizations`` is set,
with tools/``tool_choice: auto`` for agents (Claude Code sends ``auto``) and
``tool_choice: function`` for judges that carry pins (the forced tool call
404s otherwise — probe #5, Decision 25); endpoints with ``status < 0`` are
degraded and excluded; ``max_completion_tokens < 32000`` warns. The key is
valid (``GET /key``) and the account can use the model (``GET /models/user``,
the paid-training filter). A catalog *fetch* failure degrades the run to
``warn`` with the reason; key validity, slug existence and eligibility stay
strict under ``strict``. The frozen catalog view is written to
``<run_dir>/provider/routing_snapshot.json`` for the post-hoc audit.

``python3 -m agent_eval.providers.openrouter.preflight --config eval.yaml``
runs the same checks from the command line (no spend, no per-run key).
"""

from __future__ import annotations

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv before 3p imports
import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from agent_eval.providers.base import ConfigError, parse_agent_model, routing_key
from agent_eval.providers.ledger import utc_now
from agent_eval.providers.openrouter.audit import pinned_providers
from agent_eval.providers.openrouter.catalog import ModelCatalog
from agent_eval.providers.openrouter.http import OpenRouterHTTPError, get_json
from agent_eval.providers.openrouter.routing import routing_sha

SNAPSHOT_RELPATH = Path("provider") / "routing_snapshot.json"
LEVELS = ("strict", "warn", "off")
CLAUDE_CODE_MAX_TOKENS = 32000


@dataclass
class PreflightResult:
    level: str
    failures: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    degraded_reason: Optional[str] = None
    snapshot: Optional[dict] = None
    snapshot_path: Optional[Path] = None


def table_sha(table) -> Optional[str]:
    """SHA of the declared routing table (defaults + models); None when empty."""
    if table is None or getattr(table, "is_empty", True):
        return None
    body = {"defaults": table.defaults.to_dict(),
            "models": {k: v.to_dict() for k, v in sorted(table.models.items())}}
    return routing_sha(body)


def plan_models(plan) -> dict:
    """``{routing key: variant}`` for every id the agent path can send."""
    ids = [plan.skill, plan.subagent] + ([plan.hook] if plan.hook is not None else [])
    if plan.background_model:
        ids.append(parse_agent_model(plan.background_model))
    out: dict = {}
    for m in ids:
        out.setdefault(m.key, ":".join(m.variants) if m.variants else None)
    return out


def _tag(slug, e) -> str:
    return e.get("tag") or f"{slug}/{e.get('quantization')}" if e.get("quantization") else (e.get("tag") or slug)


def _check_endpoint(key, slug, e, roles, quants, res) -> list:
    """Why a pinned endpoint is excluded from the eligible set (empty = eligible)."""
    reasons = []
    q = e.get("quantization")
    if quants and q not in quants:
        reasons.append(f"quantization {q} not in {list(quants)}")
    status = e.get("status")
    if isinstance(status, (int, float)) and not isinstance(status, bool) and status < 0:
        reasons.append(f"status {status} (degraded)")
    params = e.get("supported_parameters")
    if "agent" in roles and isinstance(params, list) and not {"tools", "tool_choice"} <= set(params):
        reasons.append("no tools/tool_choice support")
    stc = e.get("supports_tool_choice")
    if "agent" in roles and isinstance(stc, dict) and stc.get("auto") is False:
        reasons.append("tool_choice: auto unsupported (Claude Code sends auto)")
    mct = e.get("max_completion_tokens")
    if isinstance(mct, (int, float)) and not isinstance(mct, bool) and mct < CLAUDE_CODE_MAX_TOKENS:
        res.warnings.append(f"model {key}: endpoint {_tag(slug, e)} max_completion_tokens {int(mct)} "
                            f"< {CLAUDE_CODE_MAX_TOKENS} (Claude Code requests 32k)")
    return reasons


def _check_key(key, roles, spec, judge_pinned, variant, known, catalog, snapshot_catalog,
               pricing, res, verdicts=("auto",)) -> dict:
    """Check one routing key against one declaration; returns its snapshot
    entry and records failures/warnings on ``res``."""
    pins = pinned_providers(spec)
    quants = getattr(spec, "quantizations", None) if spec is not None else None
    entry = {"variant": variant, "roles": sorted(roles), "pinned_set": [],
             "eligible": [], "excluded": [], "catalog": {"providers": [], "endpoints": []}}
    if known is None:
        return entry
    if key not in known:
        res.failures.append(f"model {key}: not in the OpenRouter catalog")
        return entry
    permaslug = known[key].get("canonical_slug") or known[key].get("id")
    try:
        eps = catalog.endpoints(key)
    except OpenRouterHTTPError as exc:
        res.warnings.append(f"model {key}: endpoints unavailable ({exc}); pin check skipped")
        res.degraded_reason = res.degraded_reason or "catalog"
        return entry
    snapshot_catalog["endpoints"][key] = eps
    served: dict = {}
    for e in eps:
        if isinstance(e, dict):
            served.setdefault(catalog.provider_slug(e.get("provider_name")), []).append(e)
    entry["catalog"] = {"providers": sorted(s for s in served if s),
                        "endpoints": [_tag(s, e) for s, lst in served.items() for e in lst]}
    pricing[key] = {s: [e.get("pricing") for e in lst if e.get("pricing")] for s, lst in served.items()
                    if any(e.get("pricing") for e in lst)}
    if not pins:
        return entry
    label = f"model {key}" + (" (judge declaration)" if roles == {"judge"} else "")
    missing = sorted(p for p in pins if p not in served)
    if missing:
        res.failures.append(f"{label}: pinned provider(s) {', '.join(missing)} do not serve it "
                            f"(served by: {', '.join(entry['catalog']['providers']) or 'nobody'})")
        return entry
    function_capable, function_reported = [], False
    structured_capable, params_reported = [], False
    for slug in sorted(pins):
        for e in served.get(slug, []):
            reasons = _check_endpoint(key, slug, e, roles, quants, res)
            tag = _tag(slug, e)
            if reasons:
                entry["excluded"].append({"tag": tag, "reasons": reasons})
                continue
            entry["eligible"].append(tag)
            entry["pinned_set"].append([slug, permaslug, e.get("quantization")])
            stc = e.get("supports_tool_choice")
            if isinstance(stc, dict) and "function" in stc:
                function_reported = True
                if stc.get("function"):
                    function_capable.append(tag)
            params = e.get("supported_parameters")
            if isinstance(params, list):
                params_reported = True
                if "structured_outputs" in params:
                    structured_capable.append(tag)
    if not entry["eligible"]:
        detail = "; ".join(f"{x['tag']}: {', '.join(x['reasons'])}" for x in entry["excluded"])
        res.failures.append(f"{label}: no eligible pinned endpoint ({detail})")
        return entry
    if entry["excluded"]:
        res.warnings.append(f"{label}: excluded pinned endpoint(s) " + "; ".join(
            f"{x['tag']} ({', '.join(x['reasons'])})" for x in entry["excluded"]))
    if "judge" in roles and judge_pinned:
        # Spec 015: the `json_schema` transport needs `structured_outputs` on the
        # endpoint; the `tool` transport (the fallback under `auto`, the only
        # rung under `tool`) needs `tool_choice: function` (Decision 25). One
        # pass per policy the key's judges actually run under; a judge with
        # its own `verdict:` is not held to the run-level policy.
        policies = sorted(set(verdicts or ("auto",)))
        for verdict in policies:
            who = f" (judges on verdict: {verdict})" if len(policies) > 1 else ""
            if verdict in ("auto", "json_schema") and params_reported:
                lacking = [t for t in entry["eligible"] if t not in structured_capable]
                if not structured_capable and verdict == "json_schema":
                    res.failures.append(f"{label}{who}: no pinned endpoint advertises "
                                        "structured_outputs and the verdict policy pins "
                                        "json_schema (spec 015)")
                elif not structured_capable:
                    res.warnings.append(f"{label}{who}: no pinned endpoint advertises "
                                        "structured_outputs; the judge will run on the "
                                        "forced-tool transport (spec 015)")
                elif lacking:
                    res.warnings.append(f"{label}{who}: pinned endpoint(s) {', '.join(lacking)} "
                                        "lack structured_outputs; the judge falls back to the "
                                        "forced tool there (spec 015)")
            if verdict in ("auto", "tool") and function_reported:
                lacking = [t for t in entry["eligible"] if t not in function_capable]
                if not function_capable and verdict == "auto" and structured_capable:
                    # `auto` sends json_schema first; an endpoint that serves it
                    # keeps the run viable without forced-tool support.
                    res.warnings.append(f"{label}{who}: no pinned endpoint supports "
                                        "tool_choice: function; the judge runs on the "
                                        "json_schema transport only (a forced-tool "
                                        "fallback would answer 404, Decision 25)")
                elif not function_capable:
                    res.failures.append(f"{label}{who}: no pinned endpoint supports tool_choice: "
                                        "function — a pinned judge's forced tool call answers 404 "
                                        "(Decision 25)")
                elif lacking:
                    res.warnings.append(f"{label}{who}: pinned endpoint(s) {', '.join(lacking)} lack "
                                        "tool_choice: function; the judge falls back per the "
                                        "tool_choice ladder")
    return entry


def run_preflight(plan, *, level: str = "strict", run_dir=None, catalog=None, fetch=None,
                  judges: Optional[dict] = None, verdict: str = "auto",
                  judge_verdicts: Optional[dict] = None) -> PreflightResult:
    """Run the checks for ``plan`` at ``level``; ``judges`` is
    ``{routing key: pinned}`` for the run's ``openrouter:/`` judges (see
    ``plan.judge_routing_keys``: the judge's effective RoutingSpec, ``True`` for
    the table's entry, falsy when it sends no pins); ``judge_verdicts`` is
    ``{routing key: [(declaration, verdict policy), ...]}``
    (``plan.judge_verdict_policies``) and runs the judge checks once per
    distinct pair, each against its own declaration — absent, ``judges`` with
    ``verdict`` stand in. ``strict`` raises :class:`ConfigError` listing
    every failure; ``warn`` prints them and marks the result degraded; ``off``
    does nothing. ``fetch`` (tests) serves every GET."""
    if level not in LEVELS:
        raise ConfigError(f"preflight level must be one of {LEVELS}; got {level!r}")
    res = PreflightResult(level=level)
    if level == "off":
        return res
    base = plan.base_url.rstrip("/")
    keyed = fetch or (lambda url: get_json(url, key=plan.key, timeout=15))
    catalog = catalog or (ModelCatalog(fetch=fetch, base_url=plan.base_url) if fetch
                          else ModelCatalog(base_url=plan.base_url))
    table = getattr(plan, "routing", None)
    keys: dict = {k: {"variant": v, "roles": {"agent"}, "judge_decls": []}
                  for k, v in plan_models(plan).items()}

    def resolved(declared, key):
        # a judge's effective RoutingSpec, ``True`` for "the table's entry",
        # falsy for a judge that sends no pins (→ None)
        if declared is True:
            return table.for_model(key) if table is not None else None
        return declared if declared and not isinstance(declared, bool) else None

    for key, declared in (judges or {}).items():
        # ``judge_verdicts`` (plan.judge_verdict_policies) lists the distinct
        # (declaration, spec 015 policy) pairs the key's judges run under; each
        # pair is checked on its own. Absent (or the bare policy strings of the
        # older shape) → ``judges[key]``'s declaration with the run-level
        # ``verdict``.
        entry = keys.setdefault(routing_key(key), {"variant": None, "roles": set(), "judge_decls": []})
        entry["roles"].add("judge")
        decls = entry["judge_decls"]
        pairs = (judge_verdicts or {}).get(key) or [(declared, verdict)]
        for pair in pairs:
            spec, policy = (resolved(declared, key), pair) if isinstance(pair, str) \
                else (resolved(pair[0], key), pair[1])
            for d in decls:
                if d[0] == spec:
                    d[1].add(policy)
                    break
            else:
                decls.append([spec, {policy}])
    snapshot_catalog: dict = {"providers": [], "models": [], "endpoints": {}}
    known: Optional[dict] = None
    try:
        models = catalog.models()
        known = {}
        for m in models:
            if isinstance(m, dict):
                for f in ("id", "canonical_slug"):
                    if m.get(f):
                        known[routing_key(m[f])] = m
        snapshot_catalog["providers"] = catalog.providers()
        snapshot_catalog["models"] = [m for m in models if isinstance(m, dict) and
                                      {routing_key(m.get("id")), routing_key(m.get("canonical_slug"))} & set(keys)]
    except OpenRouterHTTPError as exc:
        res.warnings.append(f"catalog unavailable ({exc}); slug and pin checks skipped")
        res.degraded_reason = "catalog"
    snap_keys: dict = {}
    pricing: dict = {}
    for key, info in keys.items():
        roles = info["roles"]
        agent_spec = table.for_model(key) if table is not None else None
        agent_pins = pinned_providers(agent_spec) if "agent" in roles else None
        # One check per declaration actually sent: the agent path sends the
        # table's entry; each distinct judge declaration (Decision 25) is
        # checked on its own — against its own pins and under the verdict
        # policies of the judges that send it — unless it pins what the agent
        # does, when it shares the agent's check. A judge sending no pins is
        # not held to anyone else's.
        checks = []   # [roles, spec, judge_pinned, verdict policies]
        if "agent" in roles:
            checks.append([{"agent"}, agent_spec, False, set()])
        for judge_spec, policies in info["judge_decls"]:
            judge_pins = pinned_providers(judge_spec)
            if "agent" in roles and judge_pins == agent_pins:
                checks[0][0].add("judge")
                checks[0][2] = checks[0][2] or bool(judge_pins)
                checks[0][3] |= policies
            else:
                checks.append([{"judge"}, judge_spec, bool(judge_pins), set(policies)])
        for check_roles, spec, judge_pinned, policies in checks:
            entry = _check_key(key, check_roles, spec, judge_pinned, info["variant"], known, catalog,
                               snapshot_catalog, pricing, res,
                               verdicts=policies or {verdict})
            if key not in snap_keys:
                snap_keys[key] = entry
            else:
                snap_keys[key]["roles"] = sorted(set(snap_keys[key]["roles"]) | check_roles)
                # the snapshot's `judge` slot keeps the first judge check, a
                # pinned one taking it over from an unpinned one
                if "judge" not in snap_keys[key] or (
                        entry["eligible"] and not snap_keys[key]["judge"]["eligible"]):
                    snap_keys[key]["judge"] = entry
    try:
        keyed(f"{base}/v1/key")
    except OpenRouterHTTPError as exc:
        if exc.status in (401, 403):
            res.failures.append(f"inference key ({plan.key_env}) rejected: HTTP {exc.status}")
        else:
            res.warnings.append(f"key check unavailable ({exc})")
            res.degraded_reason = res.degraded_reason or "key-check"
    try:
        eligible = keyed(f"{base}/v1/models/user")
        data = eligible.get("data") if isinstance(eligible, dict) else eligible
        elig = {routing_key(m.get("id")) for m in (data or []) if isinstance(m, dict) and m.get("id")}
        for key in keys:
            if elig and key not in elig:
                reasons = [m.get("ineligibility_reasons") for m in data if isinstance(m, dict)
                           and routing_key(m.get("id")) == key and m.get("ineligibility_reasons")]
                why = f": {reasons[0]}" if reasons else " (account privacy/paid-training settings)"
                res.failures.append(f"model {key}: not available to this key{why}")
    except OpenRouterHTTPError as exc:
        res.warnings.append(f"eligibility check unavailable ({exc})")
        res.degraded_reason = res.degraded_reason or "eligibility"
    providers_map = sorted(p.get("slug") for p in snapshot_catalog["providers"] if isinstance(p, dict) and p.get("slug"))
    res.snapshot = {"ts": utc_now(), "routing_sha": table_sha(table), "enforcement": plan.enforcement,
                    "key_scope": plan.key_scope, "preflight": level,
                    "providers_map_sha": routing_sha(providers_map) if providers_map else None,
                    "keys": snap_keys, "pricing": pricing, "catalog": snapshot_catalog}
    if run_dir is not None:
        res.snapshot_path = Path(run_dir) / SNAPSHOT_RELPATH
        res.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        res.snapshot_path.write_text(json.dumps(res.snapshot, indent=2) + "\n")
    if res.failures:
        if level == "strict":
            raise ConfigError("preflight failed:\n  - " + "\n  - ".join(res.failures))
        res.degraded_reason = "preflight"
        for f in res.failures:
            print(f"WARNING: preflight: {f}", file=sys.stderr)
    return res


def main(argv=None) -> int:
    """``--config eval.yaml [--model URI] [--level strict|warn] [--run-dir DIR]``:
    the run's preflight without a run — no spend, and no per-run key even at
    ``key-guardrail`` (the operator key is used for the key/eligibility checks)."""
    from agent_eval.config import EvalConfig, OpenRouterConfig  # call-time: providers never import config at load
    from agent_eval.providers.base import ProviderPlan
    from agent_eval.providers.openrouter.plan import (
        build_plan, effective_roles, judge_routing_keys, judge_verdict_policies, plan_is_active)

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--model", default=None, help="skill model override (openrouter:/…)")
    parser.add_argument("--level", default=None, choices=("strict", "warn"))
    parser.add_argument("--run-dir", type=Path, default=None, help="where to write routing_snapshot.json")
    args = parser.parse_args(argv)
    try:
        config = EvalConfig.from_yaml(args.config)
        roles = effective_roles(config, {"skill": args.model})
        if not plan_is_active(roles):
            print("nothing to preflight: the effective skill model is not an openrouter:/ URI", file=sys.stderr)
            return 2
        orc = getattr(getattr(config.models, "providers", None), "openrouter", None) or OpenRouterConfig()
        plan = build_plan(config, roles, require_key=False)
        key = os.environ.get(orc.api_key_env)
        if not key:
            raise ConfigError(f"set {orc.api_key_env}: the key and eligibility checks need the inference key")
        plan = ProviderPlan(**{**plan.__dict__, "key": key, "session": None})
        res = run_preflight(plan, level=args.level or orc.preflight, run_dir=args.run_dir,
                            judges=judge_routing_keys(config),
                            verdict=getattr(config.models, "judge_verdict", "auto"),
                            judge_verdicts=judge_verdict_policies(config))
    except (ConfigError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    for w in res.warnings:
        print(f"WARNING: {w}")
    checked = ", ".join(sorted(res.snapshot["keys"])) if res.snapshot else "-"
    print(f"preflight {res.level}: {'degraded (' + res.degraded_reason + ')' if res.degraded_reason else 'ok'} "
          f"| keys: {checked}" + (f" | snapshot: {res.snapshot_path}" if res.snapshot_path else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
