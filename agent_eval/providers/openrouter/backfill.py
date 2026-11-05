"""Offline ``/generation`` backfill for a finished run (spec 014).

``python3 -m agent_eval.providers.openrouter.backfill <run_dir> [--config eval.yaml]``
re-queries every transcript id of the run that has no priced ledger row — a
generation that was still materialising at run end, or a run interrupted
before its run-end pass — and re-reconciles the run's ``run_result.json``
files under the run's plan, so the routing audit and the budget verdict are
recomputed against the completed ledger, not carried over stale.

The plan is rebuilt from ``--config`` when given (the exact declaration,
validated by the loader), otherwise from the blocks the run recorded
(``provider``, ``routing.declared``, ``budget``). The inference key comes from
the environment. A recorded ``base_url`` other than the OpenRouter origin is
never trusted silently: the key is sent there only when ``--base-url`` names
it explicitly (https, or plain http on the loopback interface only).
"""

from __future__ import annotations

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv before 3p imports
import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from urllib.parse import urlsplit

from agent_eval.providers.base import PLAN_RUNNERS, ConfigError, ProviderPlan, parse_agent_model
from agent_eval.providers.ledger import Ledger
from agent_eval.providers.openrouter.catalog import ModelCatalog
from agent_eval.providers.openrouter.generation import Backfill, is_generation_id
from agent_eval.providers.openrouter.http import BASE_URL
from agent_eval.providers.openrouter.preflight import SNAPSHOT_RELPATH
from agent_eval.providers.openrouter.routing import RoutingTable
from agent_eval.providers.reconcile import reconcile_run_dir


def _load(path: Path) -> Optional[dict]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def collect_sightings(run_dir: Path) -> list:
    """``{gen_id, case_id}`` for every transcript id of the run: the per-case
    files carry their case, the run-level file covers batch mode."""
    seen: dict = {}
    for case_file in sorted(run_dir.glob("cases/*/run_result.json")):
        payload = _load(case_file) or {}
        for gen_id in _generation_ids(payload):
            if is_generation_id(gen_id):
                seen.setdefault(gen_id, {"gen_id": gen_id, "case_id": case_file.parent.name})
    run = _load(run_dir / "run_result.json") or {}
    for gen_id in _generation_ids(run):
        if is_generation_id(gen_id):
            seen.setdefault(gen_id, {"gen_id": gen_id, "case_id": None})
    return list(seen.values())


def _generation_ids(payload: dict) -> list:
    """Current ``generation_ids``, with legacy ``message_ids`` fallback.

    Older run results stored OpenRouter ids in ``message_ids``; current runs
    keep Claude ids there and OpenRouter ids in ``generation_ids``.
    """
    ids = payload.get("generation_ids")
    if ids is None:
        ids = payload.get("message_ids") or []
    return ids if isinstance(ids, list) else []


def checked_base_url(url: str, *, explicit: bool) -> str:
    """The loader's rule (https, or http on loopback; no ``/v1``) plus: an
    origin other than OpenRouter's is used only when the operator named it."""
    from agent_eval.config import _resolve_base_url  # call-time: providers never import config at load

    url = _resolve_base_url(url, "base_url")
    if not explicit and urlsplit(url).netloc.lower() != urlsplit(BASE_URL).netloc.lower():
        raise ConfigError(
            f"the run records base_url {url!r}; the inference key is sent only to "
            f"{BASE_URL} unless you pass --base-url {url} explicitly")
    return url


def plan_from_record(run: dict, *, key: str, key_env: str, run_id: str) -> ProviderPlan:
    """A plan rebuilt from what the run recorded — enough for the audit
    (``routing.declared``), the budget verdict and the provider block."""
    provider = run.get("provider") or {}
    routing = run.get("routing") if isinstance(run.get("routing"), dict) else {}
    declared = routing.get("declared") or {}
    table = RoutingTable.from_dict({"models": declared}) if declared else RoutingTable()
    object.__setattr__(table, "policy", routing.get("policy") or "strict")
    model = run.get("model") or provider.get("background_model") or "unknown/unknown"
    try:
        skill = parse_agent_model(model)
    except ValueError:
        skill = parse_agent_model("unknown/unknown")
    evb = (run.get("eval_params") or {}).get("budget") or {}
    cap, invocation = evb.get("cli_cap_usd"), evb.get("invocation_usd")
    inflation = cap / invocation if isinstance(cap, (int, float)) and isinstance(invocation, (int, float)) \
        and invocation > 0 else 50
    runner = provider.get("runner") if provider.get("runner") in PLAN_RUNNERS else "claude-code"
    return ProviderPlan(
        kind=provider.get("kind") or "openrouter", base_url=provider.get("base_url") or BASE_URL,
        key_scope=provider.get("key_scope") or "operator", key=key, key_env=key_env,
        skill=skill, subagent=skill, hook=None, background_model=provider.get("background_model"),
        routing=table, enforcement=routing.get("enforcement") or "audit", run_id=run_id,
        runner=runner, attribution=SimpleNamespace(referer=None, title=None, run_id_header=False),
        cli_budget_inflation=inflation, budget_run_usd=(run.get("budget") or {}).get("run_usd"))


def plan_from_config(config_path: Path, run: dict, *, run_id: str) -> ProviderPlan:
    """The keyless plan from the run's config (``require_key=False``): the
    caller selects the key variable (``--key-env`` or the config's
    ``api_key_env``) and attaches the key afterwards."""
    from agent_eval.config import EvalConfig  # call-time, see checked_base_url
    from agent_eval.providers.openrouter.plan import build_plan, effective_roles

    config = EvalConfig.from_yaml(config_path)
    recorded = run.get("model")
    override = recorded if isinstance(recorded, str) and recorded.startswith("openrouter:") else None
    runner = (run.get("provider") or {}).get("runner")
    return build_plan(config, effective_roles(config, {"skill": override}),
                      runner=runner if runner in PLAN_RUNNERS else "claude-code", run_id=run_id,
                      require_key=False)


def with_key(plan: ProviderPlan, *, key_env: str, base_url: Optional[str] = None) -> ProviderPlan:
    """``plan`` carrying the key read from ``key_env`` (and ``base_url`` when
    given). The value is never echoed."""
    key = os.environ.get(key_env)
    if not key:
        raise ConfigError(f"set {key_env} — the backfill authenticates with the run's inference key")
    fields = {**plan.__dict__, "key": key, "key_env": key_env, "session": None}
    if base_url:
        fields["base_url"] = base_url
    return ProviderPlan(**fields)


def run_backfill(run_dir: Path, plan: ProviderPlan, *, give_up_s: float = 30.0, fetch=None) -> dict:
    ledger = Ledger.for_run(run_dir)
    priced = {r.get("gen_id") for r in ledger.read()
              if r.get("source") == "generation" and r.get("status") == "ok"}
    todo = [s for s in collect_sightings(run_dir) if s["gen_id"] not in priced]
    snapshot = _load(run_dir / SNAPSHOT_RELPATH)
    frozen = (snapshot or {}).get("catalog") if isinstance(snapshot, dict) else None
    catalog = (ModelCatalog.from_snapshot(frozen) if frozen and frozen.get("endpoints")
               else ModelCatalog(fetch=fetch, base_url=plan.base_url) if fetch
               else ModelCatalog(base_url=plan.base_url))
    backfill = Backfill(ledger, key=plan.key, run_id=plan.run_id, base_url=plan.base_url, fetch=fetch,
                        catalog=catalog, first_poll_s=0.0, give_up_s=give_up_s)
    backfill.sight_many(todo)
    backfill.drain(timeout_s=give_up_s + 5)
    stats = backfill.close(retry=False)
    reconcile_run_dir(run_dir, plan=plan, ledger=ledger, catalog=catalog)
    return {"requeried": len(todo), "priced": stats.ok, "failed": stats.failed,
            "aborted": stats.aborted, "run_id": plan.run_id}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--config", type=Path, default=None,
                        help="the run's eval config: rebuilds the exact plan (routing, budget, base URL)")
    parser.add_argument("--base-url", default=None,
                        help="confirm a non-default OpenRouter origin recorded by the run")
    parser.add_argument("--key-env", default=None,
                        help="environment variable holding the inference key (default: the config's "
                             "api_key_env, else OPENROUTER_API_KEY; the value is never echoed)")
    parser.add_argument("--give-up-s", type=float, default=30.0)
    args = parser.parse_args(argv)
    run = _load(args.run_dir / "run_result.json")
    if run is None:
        print(f"ERROR: no run_result.json under {args.run_dir}", file=sys.stderr)
        return 2
    rows = Ledger.for_run(args.run_dir).read()
    run_id = next((r.get("run_id") for r in rows if r.get("run_id")), None) or args.run_dir.name
    try:
        if args.config is not None:
            # Keyless plan first, then the selected key: --key-env wins over the
            # config's api_key_env, and only the selected variable is read.
            plan = plan_from_config(args.config, run, run_id=run_id)
            base_url = checked_base_url(args.base_url, explicit=True) if args.base_url else plan.base_url
            plan = with_key(plan, key_env=args.key_env or plan.key_env, base_url=base_url)
        else:
            key_env = args.key_env or "OPENROUTER_API_KEY"
            plan = plan_from_record(run, key="", key_env=key_env, run_id=run_id)
            base_url = checked_base_url(args.base_url or plan.base_url, explicit=args.base_url is not None)
            plan = with_key(plan, key_env=key_env, base_url=base_url)
    except (ConfigError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    summary = run_backfill(args.run_dir, plan, give_up_s=args.give_up_s)
    print(f"backfill: re-queried {summary['requeried']} id(s), priced {summary['priced']}, "
          f"failed {summary['failed']}" + (f", aborted: {summary['aborted']}" if summary["aborted"] else ""))
    return 1 if summary["failed"] or summary["aborted"] else 0


if __name__ == "__main__":
    sys.exit(main())
