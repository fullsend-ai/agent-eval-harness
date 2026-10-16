"""Test doubles for the direct OpenRouter transport (spec 014): a loopback
OpenRouter (catalog, key, eligibility, /generation) and a stand-in ``claude``
binary that records what it was launched with and prints a stream-json run.
No key, no network beyond 127.0.0.1."""

from __future__ import annotations

import json
import os
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from agent_eval.providers.base import ProviderPlan, parse_agent_model
from agent_eval.providers.openrouter.routing import RoutingTable

FAKE_KEY = "sk-or-fake-0123456789"
FAKE_MGMT_KEY = "sk-or-mgmt-fake-0123456789"
GEN_COST = 0.00165633           # probe #12's recorded /generation total_cost
PROVIDERS = [{"slug": "novita", "name": "Novita"}, {"slug": "z-ai", "name": "Z.AI"},
             {"slug": "deepinfra", "name": "DeepInfra"}]
MODELS = [
    {"id": "z-ai/glm-5.2", "canonical_slug": "z-ai/glm-5.2-20260616", "name": "GLM 5.2"},
    {"id": "qwen/qwen3-8b", "canonical_slug": "qwen/qwen3-8b", "name": "Qwen3 8B"},
    {"id": "deepseek/deepseek-v4.1-flash", "canonical_slug": "deepseek/deepseek-v4.1-flash-20260701",
     "name": "DeepSeek V4.1 Flash"},
]
_TC = {"none": True, "auto": True, "required": True, "function": True}
ENDPOINTS = {
    "z-ai/glm-5.2": [
        {"provider_name": "Novita", "tag": "novita/fp8", "quantization": "fp8", "status": 0,
         "supported_parameters": ["tools", "tool_choice", "response_format", "structured_outputs"],
         "supports_tool_choice": dict(_TC),
         "max_completion_tokens": 131072, "pricing": {"prompt": "0.0000002", "completion": "0.0000008"}},
        {"provider_name": "Z.AI", "tag": "z-ai", "quantization": "fp8", "status": 0,
         "supported_parameters": ["tools", "tool_choice", "response_format"],
         "supports_tool_choice": dict(_TC),
         "max_completion_tokens": 128000, "pricing": {"prompt": "0.0000003", "completion": "0.0000009"}},
        # a second Novita endpoint at another quantization, listed last on purpose
        {"provider_name": "Novita", "tag": "novita/bf16", "quantization": "bf16", "status": 0,
         "supported_parameters": ["tools", "tool_choice", "response_format", "structured_outputs"],
         "supports_tool_choice": dict(_TC),
         "max_completion_tokens": 131072},
    ],
    "qwen/qwen3-8b": [
        # a degraded endpoint and one that cannot force a tool call
        {"provider_name": "DeepInfra", "tag": "deepinfra", "quantization": "bf16", "status": -2,
         "supported_parameters": ["tools", "tool_choice"], "supports_tool_choice": dict(_TC)},
        {"provider_name": "Novita", "tag": "novita/qwen", "quantization": "bf16", "status": 0,
         "supported_parameters": ["tools", "tool_choice"],
         "supports_tool_choice": {"none": True, "auto": True, "required": False, "function": False},
         "max_completion_tokens": 16384},
    ],
    "deepseek/deepseek-v4.1-flash": [{"provider_name": "Novita", "tag": "novita", "quantization": "fp8", "status": 0}],
}


class FakeOpenRouter:
    """``start()`` returns the plan's ``base_url`` (``http://127.0.0.1:<port>/api``)."""

    def __init__(self, *, key: str = FAKE_KEY, cost: float = GEN_COST, provider: str = "Novita",
                 served_model: str = "z-ai/glm-5.2-20260616", lag_calls: int = 0,
                 ineligible=("deepseek/deepseek-v4.1-flash",), key_usage_start: float = 0.5,
                 catalog_down: bool = False, management_key: str = FAKE_MGMT_KEY,
                 echo_allowlist: bool = True, revoke_status: int = 200, readback_status: int = 200):
        self.key, self.cost, self.provider, self.served_model = key, cost, provider, served_model
        self.lag_calls, self.ineligible, self.catalog_down = lag_calls, set(ineligible), catalog_down
        self.key_usage_start = key_usage_start
        self.management_key, self.echo_allowlist, self.revoke_status = management_key, echo_allowlist, revoke_status
        self.readback_status = readback_status
        self.requests: list = []
        self.generation_calls: dict = {}
        self.priced: list = []            # gen ids that returned 200, in order
        self.issued: dict = {}            # hash -> {"key", "record"} (management API)
        self.deletes: list = []           # hashes DELETEd, in order
        self.key_requests: list = []      # POST /keys bodies
        self._server = None

    # -- lifecycle ---------------------------------------------------------------
    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):        # quiet
                pass

            def _answer(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._answer(*fake._route(self.path, self.headers))

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode() or "{}")
                except ValueError:
                    body = {}
                self._answer(*fake._manage("POST", self.path, self.headers, body))

            def do_DELETE(self):
                self._answer(*fake._manage("DELETE", self.path, self.headers, None))

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self._server.server_address[1]}/api"

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # -- routes ------------------------------------------------------------------
    def _route(self, raw_path, headers):
        parts = urlsplit(raw_path)
        path = parts.path
        auth = headers.get("Authorization", "")
        self.requests.append((path, bool(auth)))
        if path in ("/api/v1/providers", "/api/v1/models") or path.endswith("/endpoints"):
            if self.catalog_down:
                return 503, {"error": {"message": "catalog down", "code": 503}}
            if path == "/api/v1/providers":
                return 200, {"data": PROVIDERS}
            if path == "/api/v1/models":
                return 200, {"data": MODELS}
            slug = path[len("/api/v1/models/"):-len("/endpoints")]
            eps = ENDPOINTS.get(slug)
            if eps is None:
                return 404, {"error": {"message": "not found", "code": 404}}
            return 200, {"data": {"id": slug, "endpoints": eps}}
        if path.startswith("/api/v1/keys"):
            return self._manage("GET", raw_path, headers, None)
        live = {self.key} | {v["key"] for v in self.issued.values() if not v["record"].get("revoked_at")}
        if auth not in {f"Bearer {k}" for k in live}:
            return 401, {"error": {"message": "No auth credentials found", "code": 401}}
        if path == "/api/v1/key":
            usage = round(self.key_usage_start + self.cost * len(self.priced), 8)
            return 200, {"data": {"label": "fake", "usage": usage, "limit": None}}
        if path == "/api/v1/models/user":
            return 200, {"data": [m for m in MODELS if m["id"] not in self.ineligible]}
        if path == "/api/v1/generation":
            gen_id = (parse_qs(parts.query).get("id") or [""])[0]
            if not gen_id.startswith("gen-fake-"):
                return 404, {"error": {"message": "Generation not found", "code": 404}}
            n = self.generation_calls[gen_id] = self.generation_calls.get(gen_id, 0) + 1
            if n <= self.lag_calls:
                return 404, {"error": {"message": "Generation not found", "code": 404}}
            if gen_id not in self.priced:
                self.priced.append(gen_id)
            return 200, {"data": {
                "id": gen_id, "total_cost": self.cost, "provider_name": self.provider,
                "model": self.served_model, "tokens_prompt": 120, "tokens_completion": 34,
                "native_tokens_prompt": 125, "native_tokens_completion": 34,
                "finish_reason": "stop", "native_finish_reason": "stop", "latency": 2043,
                "generation_time": 1960, "streamed": True, "is_byok": False}}
        return 404, {"error": {"message": f"no route {path}", "code": 404}}

    # -- management API (key-guardrail) --------------------------------------------
    def _manage(self, method, raw_path, headers, body):
        path = urlsplit(raw_path).path
        self.requests.append((f"{method} {path}", bool(headers.get("Authorization"))))
        if headers.get("Authorization") != f"Bearer {self.management_key}":
            return 401, {"error": {"message": "management key required", "code": 401}}
        if method == "POST" and path == "/api/v1/keys":
            n = len(self.issued) + 1
            key_hash, key = f"hash-{n:04d}", f"sk-or-run-{n:04d}-{'x' * 16}"
            record = {"hash": key_hash, "name": body.get("name"), "limit": body.get("limit"),
                      "usage": 0, "created_at": "2026-09-25T00:00:00Z"}
            if self.echo_allowlist and "allowed_providers" in body:
                record["allowed_providers"] = list(body["allowed_providers"])
            self.issued[key_hash] = {"key": key, "record": record}
            self.key_requests.append(dict(body))
            return 201, {"data": dict(record), "key": key}
        if path.startswith("/api/v1/keys/"):
            key_hash = path[len("/api/v1/keys/"):]
            entry = self.issued.get(key_hash)
            if entry is None:
                return 404, {"error": {"message": "key not found", "code": 404}}
            if method == "GET":
                if self.readback_status != 200:
                    return self.readback_status, {"error": {"message": "try later", "code": self.readback_status}}
                return 200, {"data": dict(entry["record"])}
            if method == "DELETE":
                self.deletes.append(key_hash)
                if self.revoke_status != 200:
                    return self.revoke_status, {"error": {"message": "try later", "code": self.revoke_status}}
                entry["record"]["revoked_at"] = "2026-09-25T00:10:00Z"
                return 200, {"data": {"deleted": True}}
        return 404, {"error": {"message": f"no route {method} {path}", "code": 404}}


def make_plan(base_url: str = "https://openrouter.ai/api", *, key=FAKE_KEY, pins=True,
              runner="claude-code", run_id="run-1", **over) -> ProviderPlan:
    table = RoutingTable.from_dict(
        {"models": {"z-ai/glm-5.2": {"order": ["novita"], "allow_fallbacks": False}}} if pins else None)
    base = dict(
        kind="openrouter", base_url=base_url, key_scope="operator", key=key,
        key_env="OPENROUTER_API_KEY", skill=parse_agent_model("openrouter:/z-ai/glm-5.2:exacto"),
        subagent=parse_agent_model("openrouter:/z-ai/glm-5.2"), hook=None, background_model=None,
        routing=table, enforcement="audit", run_id=run_id, runner=runner,
        attribution=SimpleNamespace(referer="https://example.test", title="agent-eval-harness",
                                    run_id_header=True),
        cli_budget_inflation=50, budget_run_usd=None, management_key_env="OPENROUTER_MANAGEMENT_KEY")
    base.update(over)
    return ProviderPlan(**base)


FAKE_CLAUDE = r'''#!/usr/bin/env python3
"""Stand-in for the Claude Code CLI (tests): records argv, env and the
--settings overlay, then prints a stream-json run with gen-fake-* ids."""
import hashlib, json, os, sys
argv = sys.argv[1:]
if "--version" in argv:
    print("9.9.9 (Claude Code fake)")
    sys.exit(0)
prompt = sys.stdin.read()
def flag(name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default
settings, mode = None, None
if flag("--settings"):
    p = flag("--settings")
    try:
        settings = json.load(open(p))
        mode = oct(os.stat(p).st_mode & 0o777)
    except Exception as e:                              # noqa: BLE001
        settings = {"error": str(e)}
env_block = (settings or {}).get("env") or {}
token = env_block.get("ANTHROPIC_AUTH_TOKEN")
if flag("--max-budget-usd") is not None and float(flag("--max-budget-usd")) <= 0:
    print("--max-budget-usd must be a positive number greater than 0", file=sys.stderr)
    sys.exit(1)
tag = hashlib.sha1(os.getcwd().encode()).hexdigest()[:6]
model = flag("--model", "?")
bare = model.split(":")[0]
ids = [f"gen-fake-{tag}-{i}" for i in range(1, 3)]
hook_path = os.environ.get("AGENT_EVAL_HOOK_IDS")
if hook_path and "HOOKCALL" in prompt:
    with open(hook_path, "a") as f:
        f.write(json.dumps({"id": f"gen-fake-{tag}-hook", "model": bare}) + "\n")
rec_dir = os.path.join(os.getcwd(), ".fake-claude"); os.makedirs(rec_dir, exist_ok=True)
with open(os.path.join(rec_dir, "records.jsonl"), "a") as f:
    f.write(json.dumps({
        "argv": argv, "cwd": os.getcwd(), "prompt": prompt, "env_keys": sorted(os.environ),
        "env": {k: v for k, v in os.environ.items() if k.startswith(
            ("ANTHROPIC", "CLAUDE_CODE", "AGENT_EVAL", "OPENROUTER", "CLOUD_ML", "GOOGLE_CLOUD", "AWS"))},
        "settings": settings, "settings_mode": mode, "ids": ids,
        "token_sha": hashlib.sha256(token.encode()).hexdigest()[:8] if token else None}) + "\n")
events = [{"type": "system", "subtype": "init", "model": model, "claude_code_version": "9.9.9"}]
for i, gid in enumerate(ids, 1):
    events.append({"type": "assistant", "message": {"id": gid, "model": bare, "role": "assistant",
                   "content": [{"type": "text", "text": f"step {i}"}],
                   "usage": {"input_tokens": 10, "output_tokens": 5}}})
result = {"type": "result", "subtype": "success", "is_error": False, "num_turns": len(ids),
          "total_cost_usd": 0.5, "result": "done", "api_error_status": None,
          "modelUsage": {bare: {"inputTokens": 20, "outputTokens": 10, "cacheReadInputTokens": 0,
                                "cacheCreationInputTokens": 0, "costUSD": 0.5}}}
if "FAIL402" in prompt:
    result.update({"subtype": "error_during_execution", "is_error": True, "api_error_status": 402,
                   "result": 'API Error: 402 {"error":{"message":"Key limit exceeded: this key '
                             'reached its spending limit","code":402}}'})
if flag("--output-format", "json") == "stream-json":
    for e in events + [result]:
        print(json.dumps(e))
else:
    print(json.dumps(result))
'''


def quiet_atexit(monkeypatch) -> list:
    """Keep the plan's ``atexit`` revoke fallback out of the interpreter's
    exit handlers during a test (the fake server is gone by then) while
    letting every other registration through. Returns the captured
    ``(fn, args)`` list so a test can invoke the fallback itself."""
    import atexit

    from agent_eval.providers.openrouter.keys import revoke_plan_key

    captured: list = []
    real_register = atexit.register

    def register(fn, *args, **kwargs):
        if fn is revoke_plan_key:
            captured.append((fn, args))
            return fn
        return real_register(fn, *args, **kwargs)

    monkeypatch.setattr(atexit, "register", register)
    return captured


def install_fake_claude(tmp_path: Path, monkeypatch) -> Path:
    """Put a fake ``claude`` first on PATH; returns its directory."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    exe = bin_dir / "claude"
    exe.write_text(FAKE_CLAUDE)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bin_dir


def fake_claude_records(workspace: Path) -> list:
    path = Path(workspace) / ".fake-claude" / "records.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
