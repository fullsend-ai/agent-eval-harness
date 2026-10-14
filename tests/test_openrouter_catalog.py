"""The public catalog view, the HTTP helper and the error taxonomy (spec 014)."""

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_eval.providers.base import ErrorClass  # noqa: E402
from agent_eval.providers.openrouter import http as http_mod  # noqa: E402
from agent_eval.providers.openrouter.catalog import ModelCatalog  # noqa: E402
from agent_eval.providers.openrouter.errors import (  # noqa: E402
    classify, describe, is_routing_404, is_tool_choice_400,
)
from agent_eval.providers.openrouter.http import OpenRouterHTTPError, get_json  # noqa: E402

SNAPSHOT = {
    "providers": [{"slug": "novita", "name": "Novita"}, {"slug": "z-ai", "name": "Z.AI"},
                  {"slug": "deepinfra", "name": "DeepInfra"}],
    "models": [{"id": "z-ai/glm-5.2", "canonical_slug": "z-ai/glm-5.2-20260616"}],
    "endpoints": {"z-ai/glm-5.2": [
        {"provider_name": "Novita", "tag": "novita/fp8", "quantization": "fp8",
         "pricing": {"prompt": "0.0000004", "completion": "0.0000016"}},
        {"provider_name": "Z.AI", "tag": "z-ai", "quantization": "fp8",
         "pricing": {"prompt": "0.0000006", "completion": "0.0000022"}},
        {"provider_name": "DeepInfra", "tag": "deepinfra/bf16", "quantization": "bf16",
         "pricing": {"prompt": "0.0000005", "completion": "0.000002"}},
        {"provider_name": "DeepInfra", "tag": "deepinfra/fp8", "quantization": "fp8",
         "pricing": {"prompt": "0.0000003", "completion": "0.000001"}}]}}


def test_catalog_fetches_lazily_and_memoises():
    calls = []

    def fetch(url):
        calls.append(url)
        if url.endswith("/v1/providers"):
            return {"data": SNAPSHOT["providers"]}
        if url.endswith("/v1/models"):
            return {"data": SNAPSHOT["models"]}
        return {"data": {"endpoints": SNAPSHOT["endpoints"]["z-ai/glm-5.2"]}}

    cat = ModelCatalog(fetch=fetch)
    assert cat.provider_slug("Z.AI") == "z-ai" and cat.provider_slug("novita") == "novita"
    assert cat.provider_slug("Unknown Host") == "unknown-host"          # normalised, not listed
    assert cat.canonical_to_id("z-ai/glm-5.2-20260616") == "z-ai/glm-5.2"
    assert cat.canonical_to_id("other") is None
    assert cat.quantization_for("z-ai/glm-5.2:exacto", "Novita") == ("fp8", "novita/fp8")
    assert cat.quantization_for("z-ai/glm-5.2", "DeepInfra") == (None, None)   # two quantizations
    assert cat.quantization_for("z-ai/glm-5.2", "nobody") == (None, None)
    assert cat.pricing_for("z-ai/glm-5.2", "z-ai")["prompt"] == "0.0000006"
    assert cat.min_prompt_price("z-ai/glm-5.2") == pytest.approx(3e-7)
    assert len([c for c in calls if c.endswith("/endpoints")]) == 1      # memoised


def test_snapshot_round_trip_and_offline_unknown_slug():
    cat = ModelCatalog.from_snapshot(SNAPSHOT)
    assert cat.endpoints("z-ai/glm-5.2")[0]["tag"] == "novita/fp8"
    assert cat.endpoints("unknown/model") == []                # no network offline
    snap = cat.to_snapshot(["z-ai/glm-5.2"])
    assert snap["endpoints"]["z-ai/glm-5.2"] == SNAPSHOT["endpoints"]["z-ai/glm-5.2"]
    assert snap["providers"] == SNAPSHOT["providers"]


# --- errors -----------------------------------------------------------------------------

@pytest.mark.parametrize("error_type, status, expected", [
    ("provider_overloaded", None, ErrorClass.INFRA),
    ("rate_limit_exceeded", 429, ErrorClass.INFRA),
    (None, 503, ErrorClass.INFRA),
    ("authentication", 401, ErrorClass.CONFIG),
    ("payment_required", 402, ErrorClass.CONFIG),
    ("not_found", 404, ErrorClass.CONFIG),
    (None, 404, ErrorClass.CONFIG),
    ("context_length_exceeded", 400, ErrorClass.AGENT),
    (None, 400, ErrorClass.AGENT),
    (None, None, ErrorClass.INFRA),
])
def test_classify(error_type, status, expected):
    assert classify(error_type, status, "") is expected


def test_routing_404_is_recognised_and_prefixed():
    assert is_routing_404(404, "No endpoints found for z-ai/glm-5.2 that support tool use.")
    assert not is_routing_404(404, "model not found") and not is_routing_404(500, "No endpoints found")
    assert describe(404, "No endpoints found for x").startswith("routing: ")
    assert len(describe(500, "x" * 500)) == 200


def test_tool_choice_400_is_recognised_by_status_and_text():
    assert is_tool_choice_400(400, 'tool_choice: type "tool" is not supported for this model')
    assert is_tool_choice_400(400, "Thinking may not be enabled when tool_choice forces tool use.")
    assert is_tool_choice_400(400, 'Provider returned error {"raw": "invalid tool choice"}')
    assert not is_tool_choice_400(400, "messages: at least one message is required")
    assert not is_tool_choice_400(404, "tool_choice is not supported")
    assert not is_tool_choice_400(None, "tool_choice")


# --- http ---------------------------------------------------------------------------------

class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_get_json_sends_bearer_and_parses(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["timeout"] = timeout
        return _Resp(json.dumps({"data": {"usage": 1.5}}).encode())

    monkeypatch.setattr(http_mod.urllib.request, "urlopen", fake_urlopen)
    out = get_json("https://openrouter.ai/api/v1/key", key="sk-or-x", timeout=9, params={"id": "gen-1"})
    assert out == {"data": {"usage": 1.5}}
    assert seen["url"] == "https://openrouter.ai/api/v1/key?id=gen-1"
    assert seen["auth"] == "Bearer sk-or-x" and seen["timeout"] == 9


def test_http_error_body_is_parsed_without_leaking_headers(monkeypatch):
    body = json.dumps({"error": {"code": 429, "message": "Rate limited sk-or-secret",
                                 "metadata": {"type": "rate_limit_exceeded"}}}).encode()

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {"Retry-After": "7"},
                                     io.BytesIO(body))

    monkeypatch.setattr(http_mod.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(OpenRouterHTTPError) as exc:
        get_json("https://openrouter.ai/api/v1/generation", key="sk-or-secret")
    err = exc.value
    assert err.status == 429 and err.retry_after == 7.0
    assert err.error_type == "rate_limit_exceeded" and err.error_class is ErrorClass.INFRA
    assert "Authorization" not in str(err) and "Bearer" not in str(err)


@pytest.mark.parametrize("error, expected_type", [
    ({"code": 404, "message": "m", "type": "not_found", "metadata": "provider text"}, "not_found"),
    ({"code": 502, "message": "m", "metadata": {"type": "provider_unavailable", "raw": "raw text"}},
     "provider_unavailable"),
    ({"code": 400, "message": "m", "metadata": {"raw": {"type": "invalid_request"}}}, "invalid_request"),
    ({"code": "insufficient_credits", "message": "m"}, "insufficient_credits"),
])
def test_http_error_type_is_read_from_every_documented_place(monkeypatch, error, expected_type):
    body = json.dumps({"error": error}).encode()

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, int(error["code"]) if isinstance(error["code"], int) else 402,
                                     "err", {}, io.BytesIO(body))

    monkeypatch.setattr(http_mod.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(OpenRouterHTTPError) as exc:
        get_json("https://openrouter.ai/api/v1/generation")
    assert exc.value.error_type == expected_type
    assert exc.value.message.endswith("m") or exc.value.message == "m"


def test_transport_errors_become_key_free_exceptions(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("name resolution failed")

    monkeypatch.setattr(http_mod.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(OpenRouterHTTPError) as exc:
        get_json("https://openrouter.ai/api/v1/providers")
    assert exc.value.status is None and "name resolution" in exc.value.message
