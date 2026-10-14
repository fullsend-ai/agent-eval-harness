"""The per-run provider ledger: ``<run_dir>/provider/ledger.jsonl`` (spec 014).

One JSONL record per generation the harness learns about — agent/hook rows from
the ``/generation`` backfill, judge rows from the judge client's response, and
at most one run-level ``key-usage`` delta row. Never bodies, headers or keys.
The path and the schema are provider-neutral so a future provider kind writes
the same file. Appends are ``O_APPEND`` under a process lock; readers filter
by run, case, step, role and status.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

LEDGER_RELPATH = Path("provider") / "ledger.jsonl"

ROLES = ("agent", "hook", "judge", "key-usage")
SOURCES = ("generation", "key-usage", "judge")
STATUSES = ("ok", "backfill_failed", "partial")
AUDITS = (None, "compliant", "violation", "unattributed")
ERROR_MESSAGE_MAX = 200

# The record schema, in the documented order. Unknown keys are dropped (an
# upstream ``metadata`` blob never lands in the ledger).
SCHEMA_KEYS = (
    "ts", "run_id", "case_id", "step_id", "judge", "provider_kind", "role", "source",
    "gen_id", "message_index", "model_requested", "model", "model_echo", "model_served",
    "provider", "provider_name", "endpoint_tag", "quantization", "audit", "status",
    "stop_reason", "native_finish_reason", "tool_choice_mode", "verdict_transport",
    "error_type", "error_class", "error_message",
    "cost_usd", "cost_details", "is_byok", "tokens", "streamed", "latency_ms",
    "generation_time_ms", "backfill_lag_s", "routing_sha",
)


def ledger_path(run_dir) -> Path:
    return Path(run_dir) / LEDGER_RELPATH


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def make_record(*, role: str, source: str, status: str = "ok", provider_kind: str = "openrouter",
                **fields) -> dict:
    """Build a validated ledger record. Enum fields are checked, the error
    message is truncated, unknown keys are dropped, missing keys are ``None``."""
    if role not in ROLES:
        raise ValueError(f"ledger role must be one of {ROLES}, got {role!r}")
    if source not in SOURCES:
        raise ValueError(f"ledger source must be one of {SOURCES}, got {source!r}")
    if status not in STATUSES:
        raise ValueError(f"ledger status must be one of {STATUSES}, got {status!r}")
    audit = fields.get("audit")
    if audit not in AUDITS:
        raise ValueError(f"ledger audit must be one of {AUDITS}, got {audit!r}")
    record = {key: None for key in SCHEMA_KEYS}
    record.update({k: v for k, v in fields.items() if k in SCHEMA_KEYS})
    record.update({"role": role, "source": source, "status": status,
                   "provider_kind": provider_kind})
    if not record.get("ts"):
        record["ts"] = utc_now()
    if record.get("model") is None and record.get("model_requested"):
        from agent_eval.providers.base import routing_key

        record["model"] = routing_key(record["model_requested"])
    message = record.get("error_message")
    if message is not None:
        record["error_message"] = str(message)[:ERROR_MESSAGE_MAX]
    record["cost_usd"] = _num(record.get("cost_usd"))
    return record


class Ledger:
    """Append-only JSONL writer/reader for one run."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    @classmethod
    def for_run(cls, run_dir) -> "Ledger":
        return cls(ledger_path(run_dir))

    def exists(self) -> bool:
        return self.path.is_file()

    def append(self, record: dict) -> dict:
        """Append one record (validated through ``make_record`` when it does not
        already carry every schema key). Thread-safe within the process; the
        file is opened ``O_APPEND`` so concurrent processes interleave whole
        lines."""
        if set(SCHEMA_KEYS) - set(record):
            record = make_record(**{k: v for k, v in record.items()
                                    if k in SCHEMA_KEYS or k in ("role", "source", "status",
                                                                 "provider_kind")})
        line = json.dumps(record, separators=(",", ":"), sort_keys=False) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
        return record

    def iter_records(self) -> Iterator[dict]:
        if not self.path.is_file():
            return iter(())
        return self._iter()

    def _iter(self) -> Iterator[dict]:
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj

    def read(self, run_id: Optional[str] = None, case_id: Optional[str] = None,
             step_id: Optional[str] = None, role: Optional[str] = None,
             status: Optional[str] = None) -> list:
        """Records matching every given filter (``None`` = any)."""
        out = []
        for rec in self.iter_records():
            if run_id is not None and rec.get("run_id") != run_id:
                continue
            if case_id is not None and rec.get("case_id") != case_id:
                continue
            if step_id is not None and rec.get("step_id") != step_id:
                continue
            if role is not None and rec.get("role") != role:
                continue
            if status is not None and rec.get("status") != status:
                continue
            out.append(rec)
        return out


def read_ledger(run_dir, **filters) -> list:
    """Records of ``<run_dir>/provider/ledger.jsonl`` (empty when absent)."""
    return Ledger.for_run(run_dir).read(**filters)


def rows_for_case(rows: Iterable[dict], case_id: Optional[str]) -> list:
    """Case-scoped view of ledger rows. Run-level rows (``key-usage``) belong
    to no case, so a case scope always excludes them and a ``None`` case id
    selects nothing."""
    if case_id is None:
        return []
    return [r for r in rows if r.get("case_id") == case_id]
