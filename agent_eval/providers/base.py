"""Provider-neutral building blocks: model ids, error classes, the plan.

This package never imports ``agent_eval.agent`` or ``agent_eval.config``
(one-way dependency rule): ``config`` and the runners import providers, not
the other way round.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol

# How a failure is attributed in run/judge records. ``config`` = the eval
# author has to change something (unroutable pins, unknown slug); ``infra`` =
# the harness/network side; ``provider`` = the model host failed a request that
# was well-formed; ``agent`` = the model under test misbehaved.
ERROR_CLASSES = ("config", "infra", "provider", "agent")


class ErrorClass(str, Enum):
    CONFIG = "config"
    INFRA = "infra"
    PROVIDER = "provider"
    AGENT = "agent"


class ProviderKind(str, Enum):
    """Declared provider kinds. One in this release (spec 014 Decision 1)."""

    OPENROUTER = "openrouter"


class ConfigError(ValueError):
    """An eval-config problem surfaced after load (plan build, preflight):
    the run stops before any spend (exit 2); never a judge or agent failure."""


# ``<author>/<slug>`` [``[suffix]``] [``:variant`` ...] — the bracket suffix
# (e.g. ``[1m]``) and the colon variants (``:exacto``, ``:nitro``) both ride
# on the id sent to the provider; neither is part of the routing key.
_MODEL_ID_RE = re.compile(
    r"^(?P<slug>[^:\[\]\s]+)(?P<suffix>\[[^\]]+\])?(?P<variants>(?::[A-Za-z0-9_.-]+)*)$")


def routing_key(model: Optional[str]) -> str:
    """Routing-table key for a provider model id.

    The bare slug without its ``[suffix]`` and ``:variant`` parts, lowercased
    — ``z-ai/glm-5.2:exacto``, ``Z-AI/glm-5.2`` and ``z-ai/glm-5.2[1m]`` all
    key ``z-ai/glm-5.2`` — so one ``routing.models`` entry covers every
    variant of a model.
    """
    value = (model or "").strip()
    slug, _, _variant = value.partition(":")
    slug = slug.split("[", 1)[0]
    return slug.strip().lower()


@dataclass(frozen=True)
class AgentModel:
    """A role model id as written on ``models.*`` / ``--model``.

    ``id`` is exactly what goes on the wire (``z-ai/glm-5.2:exacto``,
    ``anthropic/claude-opus-4-8[1m]``); ``slug`` drops suffix and variants;
    ``key`` is the routing-table key. ``provider`` is the URI scheme
    (``openrouter``, ``anthropic``) or ``None`` for a bare id.
    """

    provider: Optional[str]
    id: str
    slug: str
    variants: tuple = ()
    suffix: str = ""

    @property
    def key(self) -> str:
        return routing_key(self.slug)

    @property
    def uri(self) -> str:
        return f"{self.provider}:/{self.id}" if self.provider else self.id


def parse_agent_model(uri: Optional[str]) -> AgentModel:
    """Parse ``[<provider>:/]<id>`` into an :class:`AgentModel`.

    Same URI grammar as ``split_model_uri`` (provider lowercased, leading
    slashes on the id stripped). An ``openrouter:/`` id must be
    ``<author>/<slug>``; an empty id is an error on any provider.
    """
    value = (uri or "").strip()
    provider = None
    if ":/" in value:
        head, _, rest = value.partition(":/")
        provider = head.strip().lower() or None
        value = rest.lstrip("/").strip()
    if not value:
        raise ValueError(
            f"model id missing in {uri!r}"
            + (" — openrouter models are written 'openrouter:/<author>/<slug>'"
               if provider == "openrouter" else ""))
    match = _MODEL_ID_RE.match(value)
    if not match:
        raise ValueError(f"unparseable model id {uri!r}")
    slug = match.group("slug").strip()
    if provider == "openrouter":
        author, sep, name = slug.partition("/")
        if not (sep and author and name and "/" not in name):
            raise ValueError(
                f"openrouter model needs '<author>/<slug>', e.g. "
                f"'openrouter:/z-ai/glm-5.2' (got {uri!r})")
    variants = tuple(v for v in match.group("variants").split(":") if v)
    return AgentModel(provider=provider, id=value, slug=slug, variants=variants,
                      suffix=match.group("suffix") or "")


class RoutingTableProtocol(Protocol):
    """What a plan needs from a provider's routing table."""

    def for_model(self, model: str): ...

    def has_entry(self, model: str) -> bool: ...


# Runner → env target: the only thing the runner changes about a plan is HOW
# the env block is delivered (spec 014 Decision 1).
PLAN_RUNNERS = {
    "claude-code": "overlay",
    "harbor-podman": "harbor_carrier",
    "harbor-k8s": "k8s_pod",
    "evalhub": "k8s_pod",
}


@dataclass(frozen=True, repr=False)
class ProviderPlan:
    """The agent-side plan for one run: which provider, which key scope, which
    role ids, how the env block is delivered. Built by
    ``agent_eval.providers.openrouter.plan.build_plan``.

    The inference ``key`` is never repr'd: ``__repr__`` prints ``key_hash``.
    ``agent_env()`` renders the Direct transport env template for this plan's
    target; ``close()`` is the run-end hook (a no-op until the cost substrate
    and per-run keys land).
    """

    kind: str
    base_url: str
    key_scope: str                      # "operator" | "per-run"
    key: Optional[str]
    key_env: str                        # variable the key is read from
    skill: AgentModel
    subagent: AgentModel
    hook: Optional[AgentModel]
    background_model: Optional[str]
    routing: Any                        # RoutingTableProtocol
    enforcement: str                    # "audit" | "key-guardrail"
    run_id: Optional[str]
    runner: str
    attribution: Any                    # .referer / .title / .run_id_header
    cli_budget_inflation: float
    budget_run_usd: Optional[float]
    transport: str = "direct"
    management_key_env: Optional[str] = None   # never read by the plan; scrubbed from child envs
    session: Any = field(default=None, compare=False, repr=False)
    provisioned: Any = field(default=None, compare=False, repr=False)   # ProvisionedKey at key-guardrail

    @property
    def key_hash(self) -> Optional[str]:
        if not self.key:
            return None
        return hashlib.sha256(self.key.encode("utf-8")).hexdigest()[:8]

    @property
    def target(self) -> str:
        return PLAN_RUNNERS[self.runner]

    def agent_env(self, *, secrets: Optional[str] = None) -> dict:
        """The Direct transport env block for this plan's runner target."""
        from agent_eval.providers.env import settings_env_block

        return settings_env_block(self, secrets=secrets, target=self.target)

    def attach(self, session) -> "ProviderPlan":
        """Bind the run-scoped session (backfill worker, key-usage reads) whose
        ``close()`` this plan's ``close()`` delegates to."""
        object.__setattr__(self, "session", session)
        return self

    def close(self) -> None:
        """The single ``finally`` every host wraps around the run: run-end
        backfill retry, key-usage settle/read, the last reconcile and, at
        ``key-guardrail``, the per-run key revoke. Without a session (the run
        never started) only the revoke remains."""
        if self.session is not None:
            self.session.close()
        elif self.provisioned is not None:
            from agent_eval.providers.openrouter.keys import revoke_plan_key

            revoke_plan_key(self)

    def __repr__(self) -> str:
        return (f"ProviderPlan(kind={self.kind!r}, transport={self.transport!r}, "
                f"runner={self.runner!r}, key_scope={self.key_scope!r}, "
                f"key_hash={self.key_hash!r}, skill={self.skill.id!r}, "
                f"subagent={self.subagent.id!r}, enforcement={self.enforcement!r})")


class JudgeProviderError(RuntimeError):
    """A provider-side failure of an LLM judge request.

    Raised instead of feeding a degraded response to the text parser: a
    committed-200 upstream error (OpenRouter reports provider failures inside a
    200 body), a degraded ``tool_choice`` mode that did not yield the requested
    tool call (Decision 25 strict parse), or a routing 404 / ``tool_choice``
    400 that survived the ``tool_choice`` ladder. ``error_class`` follows
    ``ERROR_CLASSES``;
    ``retryable`` tells ``_with_judge_retries`` whether another attempt can heal
    it (``provider_unavailable``/``provider_overloaded`` can, a config error
    cannot).
    """

    def __init__(self, message: str, *, error_type: str = "provider_error",
                 error_class: str = "provider", retryable: bool = False,
                 provider: Optional[str] = None,
                 tool_choice_mode: Optional[str] = None):
        if error_class not in ERROR_CLASSES:
            raise ValueError(f"unknown error_class {error_class!r}")
        super().__init__(message)
        self.error_type = error_type
        self.error_class = error_class
        self.retryable = retryable
        self.provider = provider
        self.tool_choice_mode = tool_choice_mode
