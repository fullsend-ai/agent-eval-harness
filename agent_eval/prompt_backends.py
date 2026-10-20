"""Helpers for model-agnostic prompt execution.

Anthropic-backed direct calls remain the fast path for Claude-family models, but
some eval flows (synthetic dataset generation, plain prompt judges) need to work
with runner-managed model ids such as Cursor's ``gpt-5.4-medium``.  This module
provides a small runner-backed fallback for those cases.
"""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from agent_eval.agent import RUNNERS
from agent_eval.events import extract_conversation_text, parse_stream_events


_ANTHROPIC_ALIAS_PREFIXES = ("opus", "sonnet", "haiku")


def split_model_uri(model: Optional[str]) -> tuple[Optional[str], str]:
    """Split a ``"<provider>:/<model>"`` id into ``(provider, bare_model)``.

    A bare id (no ``":/"``) returns ``(None, id)``.  The provider is lowercased;
    leading slashes on the model part are stripped so ``"openai:/gpt-4o"`` and
    ``"openai://gpt-4o"`` both yield ``("openai", "gpt-4o")``.
    """
    value = (model or "").strip()
    if ":/" in value:
        provider, _, rest = value.partition(":/")
        return (provider.strip().lower() or None), rest.lstrip("/").strip()
    return None, value


def is_anthropic_model(model: Optional[str]) -> bool:
    """Best-effort classifier for Anthropic/Claude model ids.

    The direct Anthropic client can only serve Claude-family models.  Other model
    ids (for example Cursor's ``gpt-5.4-medium``) need a different backend.

    URI-aware (``anthropic:/…`` → True) and prefix-aware so versioned aliases such
    as ``sonnet-4-5`` and bracketed ids such as ``opus[1m]`` classify correctly.
    """
    provider, bare = split_model_uri(model)
    if provider is not None:
        return provider == "anthropic"
    value = bare.lower()
    if not value:
        return False
    if value.startswith("anthropic/"):  # LiteLLM single-slash form
        return True
    if "claude" in value:
        return True
    return value.startswith(_ANTHROPIC_ALIAS_PREFIXES)


def resolve_judge_backend(model: Optional[str]) -> tuple[str, str]:
    """Route a judge model id to a backend, independent of the eval runner.

    Returns ``(backend, model_arg)`` where ``backend`` is one of:

    - ``"anthropic"`` — direct Anthropic SDK; ``model_arg`` is the bare id.
    - ``"openai"`` — OpenAI SDK (honors ``OPENAI_BASE_URL`` for OpenAI-compatible
      gateways); ``model_arg`` is the bare id. ``openrouter:/<author>/<slug>``
      resolves here too: same transport, but score.py binds it to a dedicated
      client built from ``models.providers.openrouter`` (see
      ``resolve_judge_client``), never to the process-global ``OPENAI_*``.
    - ``"runner"`` — the configured eval runner CLI (explicit opt-in for
      runner-managed ids such as Cursor's ``gpt-5.4-medium``); ``model_arg`` is
      the bare id.

    Raises ``ValueError`` only for an unsupported *explicit* provider (or an
    empty ``runner:/``); a bare id never fails to route.
    """
    provider, bare = split_model_uri(model)
    if provider == "runner":
        if not bare:
            raise ValueError(
                "runner-backed judge model needs a name, e.g. 'runner:/gpt-5.4-medium'")
        return ("runner", bare)
    if provider == "anthropic":
        return ("anthropic", bare)
    if provider == "openai":
        return ("openai", bare)
    if provider == "openrouter":
        # OpenRouter ids are always ``<author>/<slug>[:variant]``.
        if not bare or "/" not in bare.partition(":")[0]:
            raise ValueError(
                "openrouter judge model needs '<author>/<slug>', e.g. "
                "'openrouter:/z-ai/glm-5.2'")
        return ("openai", bare)
    if provider is not None:
        raise ValueError(
            f"Unsupported judge model provider {provider!r} in {model!r}. Use "
            f"'anthropic:/…', 'openai:/…' (point OPENAI_BASE_URL at an "
            f"OpenAI-compatible gateway), 'openrouter:/<author>/<slug>' (any "
            f"model OpenRouter serves), or 'runner:/{bare}' to grade through "
            f"the configured runner.")
    # Bare id (no provider prefix).
    if is_anthropic_model(bare):
        return ("anthropic", bare)
    # Any other bare id — a GPT/o-series id or a custom/gateway model name —
    # grades via the OpenAI SDK, which also serves OpenAI-compatible gateways
    # through OPENAI_BASE_URL. A runner-managed id (e.g. Cursor's
    # ``gpt-5.4-medium``) must be written ``runner:/<model>`` to grade through
    # the configured runner instead.
    return ("openai", bare)


@dataclass(frozen=True)
class JudgeClientConfig:
    """How score.py builds the OpenAI-SDK client of a provider-backed judge.

    Resolved by ``resolve_judge_client`` from ``models.providers.<name>`` for a
    ``<name>:/`` judge model (``None`` on the Anthropic/OpenAI/runner paths).
    ``extra_body`` is the operator's static dict (``judge.extra_body``); the
    per-model routing part is computed per call by ``judge_extra_body`` and the
    static dict wins on a key clash.
    """

    name: str
    base_url: str
    api_key_env: str
    default_headers: dict = field(default_factory=dict)
    extra_body: dict = field(default_factory=dict)
    token_param: str = "max_tokens"
    max_retries: int = 3
    timeout_s: float = 300.0
    concurrency: int = 4
    routing: Any = None  # RoutingTable (agent_eval.providers.openrouter)
    inherit_pins: bool = False

    def client_key(self) -> tuple:
        """Memoisation key for the SDK client. Names the key variable, never a value."""
        return (self.name, self.base_url,
                tuple(sorted(self.default_headers.items())),
                self.api_key_env, self.max_retries, self.timeout_s)

    def judge_extra_body(self, model: str,
                         provider_options: Optional[dict] = None) -> dict:
        """``extra_body`` of one judge request: the Decision 25 routing part for
        ``model`` merged with the operator's static ``extra_body`` (later wins)."""
        from agent_eval.providers.openrouter.routing import judge_extra_body

        body = judge_extra_body(self.routing, model, inherit_pins=self.inherit_pins,
                                provider_options=provider_options)
        return {**body, **dict(self.extra_body or {})}


def resolve_judge_client(model: Optional[str], providers=None) -> Optional[JudgeClientConfig]:
    """Client config for a provider-backed judge model, or ``None``.

    ``providers`` is ``config.models.providers``. Absent ``openrouter`` block →
    the defaults apply, so an ``openrouter:/…`` judge works with nothing but
    ``OPENROUTER_API_KEY`` exported. Every pre-existing routing (bare ids,
    ``anthropic:/``, ``openai:/``, ``runner:/``) returns ``None``.
    """
    provider, _bare = split_model_uri(model)
    if provider != "openrouter":
        return None
    from agent_eval.config import OpenRouterConfig  # local: keep this module light

    cfg = getattr(providers, "openrouter", None) or OpenRouterConfig()
    headers = {}
    if cfg.attribution.referer:
        headers["HTTP-Referer"] = cfg.attribution.referer
    if cfg.attribution.title:
        headers["X-OpenRouter-Title"] = cfg.attribution.title
    return JudgeClientConfig(
        name="openrouter",
        base_url=cfg.base_url.rstrip("/"),
        api_key_env=cfg.api_key_env,
        default_headers=headers,
        extra_body=dict(cfg.judge.extra_body or {}),
        token_param="max_tokens",
        max_retries=cfg.judge.max_retries,
        timeout_s=cfg.judge.timeout_s,
        concurrency=cfg.judge.concurrency,
        routing=cfg.routing,
        inherit_pins=cfg.judge.inherit_pins,
    )


def extract_runner_text(result) -> str:
    """Extract visible assistant text from a runner ``RunResult``.

    Prefers normalized event data when present, then falls back to parsing the
    runner stdout stream, and finally returns raw stdout as a last resort.
    """
    raw = getattr(result, "raw_output", None)
    if isinstance(raw, dict):
        events = raw.get("events")
        if isinstance(events, list):
            text = extract_conversation_text(events)
            if text:
                return text.strip()

    stdout = getattr(result, "stdout", "") or ""
    try:
        events = parse_stream_events(stdout)
    except Exception:  # pragma: no cover - defensive fallback
        events = []
    if events:
        text = extract_conversation_text(events)
        if text:
            return text.strip()

    # Cursor emits JSONL with assistant text nested under message.content.
    parts = []
    terminal_result = None
    for line in stdout.splitlines():
        try:
            obj = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "result" and isinstance(obj.get("result"), str):
            terminal_result = obj["result"]
        if obj.get("type") != "assistant":
            continue
        message = obj.get("message", {}) or {}
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            if content.strip():
                parts.append(content.strip())
            continue
        if isinstance(content, list):
            text_bits = [
                str(block.get("text", "")).strip()
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
                and str(block.get("text", "")).strip()
            ]
            if text_bits:
                parts.append("\n".join(text_bits))
                continue
        text_value = message.get("text")
        if isinstance(text_value, str) and text_value.strip():
            parts.append(text_value.strip())

    if terminal_result and terminal_result.strip():
        return terminal_result.strip()
    if parts:
        return "\n\n".join(parts).strip()
    return stdout.strip()


def run_prompt_via_runner(
    config,
    prompt: str,
    model: str,
    *,
    timeout_s: int = 600,
    max_budget_usd: float = 5.0,
    permissions: Optional[dict] = None,
    permission_mode: Optional[str] = None,
    permission_prompts: Optional[str] = None,
    system_prompt: Optional[str] = None,
    workspace: Optional[Path] = None,
    staged_files: Optional[dict[str, bytes]] = None,
):
    """Execute a single prompt through the configured runner.

    Returns ``(RunResult, extracted_text, workspace_path)``.  When the workspace
    is created internally, the caller is responsible for cleaning it up.
    """
    if config.runner.type not in RUNNERS:
        raise RuntimeError(
            f"Unknown runner '{config.runner.type}'. Available: {sorted(RUNNERS)}")

    created_workspace = workspace is None
    workspace_path = Path(workspace) if workspace else Path(
        tempfile.mkdtemp(prefix="prompt-backend-"))
    workspace_path.mkdir(parents=True, exist_ok=True)

    # Keep prompt-only backends isolated even when the evaluated skill runs in
    # repo mode; the prompt already carries the information they need.
    cfg = copy.copy(config)
    cfg.runner = copy.copy(config.runner)
    cfg.runner.workspace_mode = None
    cfg.runner.system_prompt = ""
    # Prompt-only backends (LLM judges, synthetic generation) must not inherit
    # the skill agent's permission_mode. Cursor maps ``plan`` to ``--mode plan``,
    # which makes judges explore the workspace instead of emitting a verdict.
    # A caller may still ask for a specific mode (an agent judge asks for
    # deny-by-default); that is an explicit argument, never an inheritance.
    #
    # Only Claude Code accepts the deny-by-default modes (`manual`); the Cursor
    # and Codex constructors reject them with ValueError, so a runner-backed
    # judge on those runners would fail before running. Keep None there.
    cfg.runner.permission_mode = (
        permission_mode if cfg.runner.type == "claude-code" else None)
    cfg.runner.settings = dict(cfg.runner.settings or {})
    # Strip `add_dirs`: a prompt-only judge/generation call grades untrusted,
    # model-generated content in an isolated workspace and must not inherit
    # host-directory grants from the skill's runner settings (CWE-200/829). The
    # agent-judge path scrubs this too.
    cfg.runner.settings.pop("add_dirs", None)
    # These are independent judge/generation invocations, not the evaluated
    # skill. Do not make Cursor reject a top-level interception configuration
    # that does not apply to this prompt-only call.
    if hasattr(config, "inputs"):
        cfg.inputs = copy.copy(config.inputs)
        cfg.inputs.tools = []
    # `Read(**)`, not a bare `Read`: naming the bare tool REMOVES the CLI's
    # working-directory confinement, so the obvious spelling would widen a
    # prompt-only call to host-wide reads (see `_judge_tool_policy`).
    cfg.permissions = (permissions if permissions is not None
                       else {"allow": ["Read(**)"]})

    try:
        _stage_prompt_files(workspace_path, staged_files)
        extra = {}
        if permission_prompts and cfg.runner.type == "claude-code":
            # Only Claude Code has the flag, and only it needs it: this is
            # what makes `cfg.permissions["allow"]` authoritative instead of
            # merely pre-approving (#239).
            extra["permission_prompts"] = permission_prompts
        runner = RUNNERS[cfg.runner.type].from_config(
            cfg,
            log_prefix=None,
            permissions=cfg.permissions,
            effort=cfg.runner.effort,
            **extra,
        )
        result = runner.execute(
            target=None,
            args=prompt,
            workspace=workspace_path,
            model=model,
            system_prompt=system_prompt,
            max_budget_usd=max_budget_usd,
            timeout_s=timeout_s,
        )
        return result, extract_runner_text(result), workspace_path
    except Exception:
        if created_workspace:
            shutil.rmtree(workspace_path, ignore_errors=True)
        raise


def _stage_prompt_files(workspace: Path, staged_files: Optional[dict[str, bytes]]) -> None:
    """Write caller-provided prompt evidence under the isolated workspace."""
    if not staged_files:
        return
    root = workspace.resolve()
    for relative, content in staged_files.items():
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Prompt evidence path must be relative: {relative!r}")
        destination = (workspace / path).resolve()
        if destination != root and root not in destination.parents:
            raise ValueError(f"Prompt evidence path escapes workspace: {relative!r}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            destination.write_bytes(content)
        else:
            destination.write_text(str(content))
