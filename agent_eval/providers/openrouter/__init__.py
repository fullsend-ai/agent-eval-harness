"""OpenRouter provider (spec 014)."""

from agent_eval.providers.openrouter.audit import routing_audit
from agent_eval.providers.openrouter.catalog import ModelCatalog
from agent_eval.providers.openrouter.errors import (
    classify,
    is_routing_404,
    is_structured_output_400,
    is_tool_choice_400,
)
from agent_eval.providers.openrouter.generation import (
    Backfill,
    KeyUsageDelta,
    coverage,
    parse_generation,
    read_key_usage,
    read_key_usage_settled,
)
from agent_eval.providers.openrouter.http import OpenRouterHTTPError, get_json
from agent_eval.providers.openrouter.routing import (
    RoutingSpec,
    RoutingTable,
    judge_extra_body,
    normalize_provider,
    routing_sha,
)

__all__ = ["Backfill", "KeyUsageDelta", "ModelCatalog", "OpenRouterHTTPError", "RoutingSpec",
           "RoutingTable", "classify", "coverage", "get_json", "is_routing_404",
           "is_structured_output_400", "is_tool_choice_400", "judge_extra_body",
           "normalize_provider", "parse_generation",
           "read_key_usage", "read_key_usage_settled", "routing_audit", "routing_sha"]
