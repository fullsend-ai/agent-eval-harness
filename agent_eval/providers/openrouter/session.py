"""The run-scoped provider session (spec 014): what a host holds while a plan
is active — the ledger, the ``/generation`` backfill worker, the key-usage
before/after reads and the catalog view the reconcile joins against.

``ProviderSession.bind(case_id, step_id)`` hands a runner a :class:`CaseBinding`
that sights generation ids as the stream is read (and drains the hook's own ids
after the run). ``finish()`` is the run-end step every host performs before the
final ``run_result.json`` is judged (drain + retry the backfill, settle and read
the key usage) and ``reconcile_run()`` is the last reconcile pass;
``close()`` (reached through ``plan.close()`` in the host's ``finally``) does
both when the run did not get that far.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from agent_eval.providers.ledger import Ledger
from agent_eval.providers.openrouter.catalog import ModelCatalog
from agent_eval.providers.openrouter.generation import (
    FIRST_POLL_S, GIVE_UP_S, KEY_USAGE_MAX_S, KEY_USAGE_POLL_S, KEY_USAGE_SETTLE_S, POLL_S,
    Backfill, KeyUsageDelta, is_generation_id, read_key_usage, read_key_usage_settled)
from agent_eval.providers.openrouter.http import OpenRouterHTTPError, get_json
from agent_eval.providers.openrouter.keys import revoke_plan_key, write_key_record
from agent_eval.providers.openrouter.preflight import table_sha
from agent_eval.providers.reconcile import reconcile_run_dir


def _log(message: str) -> None:
    print(f"provider: {message}", file=sys.stderr, flush=True)


class CaseBinding:
    """One case (or step) of the run, as seen by its runner."""

    def __init__(self, session: "ProviderSession", case_id: Optional[str] = None,
                 step_id: Optional[str] = None):
        self.session = session
        self.case_id = case_id
        self.step_id = step_id

    @property
    def hook_ids_path(self) -> Path:
        """The JSONL a tool-interception hook appends its response ids to
        (``$AGENT_EVAL_HOOK_IDS``); per case, under the run's provider dir."""
        name = "-".join(p for p in (self.case_id, self.step_id) if p) or "run"
        return self.session.run_dir / "provider" / f"hook-ids-{name}.jsonl"

    def sight(self, gen_id: str, *, message_index: Optional[int] = None,
              model_echo: Optional[str] = None, role: str = "agent") -> bool:
        return self.session.backfill.sight(
            gen_id, case_id=self.case_id, step_id=self.step_id, role=role,
            message_index=message_index, model_requested=self.session.plan.skill.id,
            model_echo=model_echo, routing_sha=self.session.routing_sha)

    def after_run(self, generation_ids: Iterable[str]) -> int:
        """Called once the agent process exited: sight every generation id
        the stream did not show (subagent transcripts, ``json`` output mode)
        and the hook's own ids. Returns how many new ids were queued."""
        n = sum(1 for i in (generation_ids or []) if self.sight(i))
        for rec in _read_hook_ids(self.hook_ids_path):
            if self.sight(rec.get("id"), model_echo=rec.get("model"), role="hook"):
                n += 1
        return n


def _read_hook_ids(path: Path) -> list:
    out = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and is_generation_id(rec.get("id")):
                    out.append(rec)
    except OSError:
        pass
    return out


class ProviderSession:
    """See the module docstring. ``fetch`` (tests) serves every GET; the
    timing knobs default to the probe-derived cadence."""

    def __init__(self, plan, run_dir, *, parallelism: int = 1, catalog=None,
                 snapshot: Optional[dict] = None, fetch: Optional[Callable] = None,
                 first_poll_s: float = FIRST_POLL_S, poll_s: float = POLL_S,
                 give_up_s: float = GIVE_UP_S, settle_s: float = KEY_USAGE_SETTLE_S, key_poll_s: float = KEY_USAGE_POLL_S,
                 key_max_s: float = KEY_USAGE_MAX_S,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.plan = plan
        self.run_dir = Path(run_dir)
        self.ledger = Ledger.for_run(self.run_dir)
        self.snapshot = snapshot
        if catalog is None:
            frozen = (snapshot or {}).get("catalog") if isinstance(snapshot, dict) else None
            if frozen and frozen.get("endpoints"):
                catalog = ModelCatalog.from_snapshot(frozen)
            else:
                catalog = ModelCatalog(fetch=fetch, base_url=plan.base_url) if fetch \
                    else ModelCatalog(base_url=plan.base_url)
        self.catalog = catalog
        self.routing_sha = table_sha(getattr(plan, "routing", None))
        self._fetch_key = fetch or (lambda url: get_json(url, key=plan.key, timeout=15))
        self.backfill = Backfill(
            self.ledger, key=plan.key, run_id=plan.run_id, catalog=self.catalog, fetch=fetch,
            base_url=plan.base_url, first_poll_s=first_poll_s, poll_s=poll_s, give_up_s=give_up_s,
            max_workers=min(8, max(1, int(parallelism or 1)) * 2), clock=clock, sleep=sleep)
        self._settle = (settle_s, key_poll_s, key_max_s)
        self._clock, self._sleep = clock, sleep
        self.key_usage_before: Optional[float] = None
        self.key_usage: Optional[KeyUsageDelta] = None
        self.cleanups: list = []          # host resources tied to the key (e.g. a per-run K8s Secret)
        self.finished = False
        self.reconciled = False
        self.closed = False

    @property
    def snapshot_ref(self) -> Optional[str]:
        return (self.snapshot or {}).get("ts") if isinstance(self.snapshot, dict) else None

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> "ProviderSession":
        if getattr(self.plan, "provisioned", None) is not None:
            write_key_record(self.run_dir, self.plan.provisioned)      # hash, limit, providers — never the key
        try:
            self.key_usage_before = read_key_usage(self._fetch_key, base_url=self.plan.base_url)
        except OpenRouterHTTPError as exc:
            _log(f"key-usage read failed at start ({exc}); the key-usage cross-check is off")
        self.backfill.start()
        return self

    def bind(self, case_id: Optional[str] = None, step_id: Optional[str] = None) -> CaseBinding:
        return CaseBinding(self, case_id, step_id)

    def sight_trial(self, generation_ids: Iterable[str], *, case_id: Optional[str],
                    step_id: Optional[str] = None) -> int:
        return self.bind(case_id, step_id).after_run(generation_ids)

    def pending(self) -> int:
        return self.backfill.pending()

    def finish(self) -> Optional[KeyUsageDelta]:
        """Run end: drain and retry the backfill, then settle and read the key
        usage. Idempotent; returns the key-usage delta (None when unread)."""
        if self.finished:
            return self.key_usage
        self.finished = True
        pending = self.backfill.pending()
        if pending:
            _log(f"waiting for /generation backfill ({pending} id(s) pending)")
        stats = self.backfill.close(retry=True)
        if stats.failed:
            _log(f"{stats.failed} generation(s) could not be backfilled; rerun "
                 f"`python3 -m agent_eval.providers.openrouter.backfill {self.run_dir}` later")
        if stats.aborted:
            _log(f"backfill aborted: {stats.aborted}")
        if self.key_usage_before is not None:
            settle_s, poll_s, max_s = self._settle
            _log(f"reading key usage (settle {settle_s:g} s)")
            try:
                self.key_usage = read_key_usage_settled(
                    self._fetch_key, before_usd=self.key_usage_before, settle_s=settle_s,
                    max_s=max_s, poll_s=poll_s, clock=self._clock, sleep=self._sleep,
                    base_url=self.plan.base_url)
            except OpenRouterHTTPError as exc:
                _log(f"key-usage read failed at run end ({exc})")
        return self.key_usage

    def reconcile_run(self, *, allow_estimate: bool = False, warnings=None) -> Optional[dict]:
        """The final reconcile pass over the run dir (``reconcile_run_dir``)
        with this session's ledger, catalog and key-usage delta. Returns the
        run payload, or None when no run-level file exists yet."""
        self.reconciled = True
        return reconcile_run_dir(self.run_dir, plan=self.plan, ledger=self.ledger,
                                 key_usage=self.key_usage, catalog=self.catalog,
                                 allow_estimate=allow_estimate, warnings=warnings)

    def close(self) -> None:
        """The host's ``finally``: nothing left to do after ``finish()`` +
        ``reconcile_run()``; on a crash path it performs both (a second
        Ctrl-C skips the key-usage settle). At ``key-guardrail`` the per-run
        key is revoked last, on every path."""
        if self.closed:
            return
        self.closed = True
        try:
            try:
                if not self.finished:
                    self.finish()
                if not self.reconciled:
                    self.reconcile_run()
            except KeyboardInterrupt:
                self.backfill.close(retry=False)
                _log("interrupted; key-usage settle skipped (rows already written are kept)")
        finally:
            try:
                self._revoke()
            finally:
                self._run_cleanups()          # a Ctrl-C during the revoke must not orphan a K8s Secret

    def _run_cleanups(self) -> None:
        for cleanup in self.cleanups:
            try:
                cleanup()
            except Exception as exc:                        # noqa: BLE001 — every cleanup gets its turn
                _log(f"cleanup failed: {exc}")
        self.cleanups = []

    def _revoke(self) -> None:
        pk = getattr(self.plan, "provisioned", None)
        if pk is None or pk.revoked_at:
            return
        if not revoke_plan_key(self.plan, run_dir=self.run_dir):
            warning = (f"per-run key {pk.hash} could not be revoked ({pk.revoke_error}); retry with "
                       f"`python3 -m agent_eval.providers.openrouter.keys revoke {self.run_dir}`")
            try:
                self.reconcile_run(warnings=[warning])
            except Exception as exc:                        # noqa: BLE001 — the ERROR line is already out
                _log(f"could not record the revoke failure in run_result.json: {exc}")

