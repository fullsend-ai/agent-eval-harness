"""OpenRouter ``error_type`` / HTTP status → :class:`ErrorClass` (spec 014).

Applied to the harness's own client errors (backfill, catalog, keys, judge
client) and, later, to the ``error`` events Claude Code prints in stream-json
— the only place the harness sees an agent-path request fail.
"""

from __future__ import annotations

from typing import Optional

from agent_eval.providers.base import ErrorClass

INFRA_TYPES = frozenset({
    "provider_unavailable", "provider_overloaded", "rate_limit_exceeded", "timeout",
    "server", "server_error", "unmapped", "overloaded",
})
CONFIG_TYPES = frozenset({
    "authentication", "authentication_error", "permission_denied", "payment_required",
    "insufficient_credits", "not_found", "precondition_failed", "forbidden",
})
AGENT_TYPES = frozenset({
    "context_length_exceeded", "max_tokens_exceeded", "invalid_request",
    "invalid_request_error", "content_policy_violation", "refusal", "image_error",
    "invalid_image", "unsupported_image",
})
ROUTING_404_MARKER = "no endpoints found"
TOOL_CHOICE_400_MARKERS = ("tool_choice", "tool choice")
STRUCTURED_OUTPUT_400_MARKERS = ("response_format", "json_schema", "structured output",
                                 "structured_output", "output_config", "output_format")


def is_routing_404(status: Optional[int], message: Optional[str]) -> bool:
    """OpenRouter's "no endpoint can serve this request" answer: a 404 whose
    message reads "No endpoints found …" (pins + an unsupported request shape,
    e.g. a forced ``tool_choice``)."""
    return status == 404 and ROUTING_404_MARKER in (message or "").lower()


def is_tool_choice_400(status: Optional[int], message: Optional[str]) -> bool:
    """A provider refusing the request's ``tool_choice`` itself: a 400 whose
    text names ``tool_choice`` (Anthropic, relayed by OpenRouter, rejects the
    forcing modes ``tool``/``any`` for Claude Opus 5.5). The model is served —
    only the forcing mode is wrong — so the judge ladder steps down instead of
    failing. ``message`` should include the relayed upstream text
    (``error.metadata.raw``) when the top-level message is only
    "Provider returned error"."""
    text = (message or "").lower()
    return status == 400 and any(marker in text for marker in TOOL_CHOICE_400_MARKERS)


def is_structured_output_400(status: Optional[int], message: Optional[str]) -> bool:
    """A provider refusing the request's structured-output parameter itself
    (spec 015): a 400 whose text names ``response_format`` / ``json_schema``
    (OpenAI-shaped) or ``output_config`` (Anthropic), or an endpoint that
    does not support structured outputs. The judge steps from the
    ``json_schema`` transport to the forced tool call instead of failing."""
    text = (message or "").lower()
    return status == 400 and any(marker in text for marker in STRUCTURED_OUTPUT_400_MARKERS)


def classify(error_type: Optional[str] = None, status: Optional[int] = None,
             message: Optional[str] = None) -> ErrorClass:
    """The error class for an OpenRouter failure.

    ``infra``: the provider/network side failed a well-formed request (and
    can heal); ``config``: the eval author has to change something (bad key,
    unroutable pins, unknown slug, exhausted per-run key); ``agent``: the
    request itself was wrong (context length, policy). Unknown inputs fall to
    ``infra`` for 5xx/429 and ``agent`` for other 4xx.
    """
    kind = (error_type or "").strip().lower()
    if kind in INFRA_TYPES:
        return ErrorClass.INFRA
    if kind in CONFIG_TYPES:
        return ErrorClass.CONFIG
    if kind in AGENT_TYPES:
        return ErrorClass.AGENT
    if status is not None:
        if status == 429 or status >= 500:
            return ErrorClass.INFRA
        if status in (401, 402, 403, 404, 412):
            return ErrorClass.CONFIG
        if 400 <= status < 500:
            return ErrorClass.AGENT
    return ErrorClass.INFRA


def describe(status: Optional[int], message: Optional[str]) -> str:
    """Short, key-free error message for a ledger row (``routing:`` prefixed
    for the routing 404)."""
    text = (message or "").strip().replace("\n", " ")
    if is_routing_404(status, text):
        text = f"routing: {text}"
    return text[:200]
