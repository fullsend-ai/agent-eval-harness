# 015 — Judge verdict transport: structured outputs first, forced tool second

Status: implemented on branch `feat/judge-structured-verdicts` (verification checklist items 1, 2 and 4 open — they need a key). Stacked on PR #250 (`fix(judge)`: the `tool_choice` ladder steps down on
a 400), harness 1.56.1. `file:line` references are against that branch head (ce26d20);
`S` is `skills/eval-run/scripts/score.py`, `PB` `agent_eval/prompt_backends.py`, `C`
`agent_eval/config.py`.

Evidence labels: **DOCUMENTED** — stated in the provider's current reference
(platform.claude.com, developers.openai.com, openrouter.ai/docs, read 2026-10-14);
**VERIFIED** — the public keyless `/endpoints` catalog or the installed SDKs
(`anthropic` 0.89.0, `openai` 2.30.0); **UNVERIFIED** — an assumption, listed in the
checklist with the step it gates.

## Problem

Every SDK-backed LLM judge obtains its verdict by **forcing a tool call**: the Anthropic
path (`_call_structured_judge` S:1282-1289 and the pairwise `_call_judge` S:3733-3741)
sends `tool_choice: {"type": "tool", "name": …}`, and the OpenAI-shaped path that serves
`openai:/` and `openrouter:/` (`_openai_judge_request` S:1848-1909) sends
`tool_choice: {"type": "function", …}` and walks the Decision 25 ladder
`function → required → auto` when the provider refuses the forcing mode. The verdict tool
(`submit_evaluation` S:1212-1227, `submit_score` S:1129-1163, `submit_comparison`
S:3694-3713) does nothing: it exists so that `rationale`, `score`/`passed`/`preferred`
come back in known fields. The system prompts hard-code the contract ("Call the
submit_score tool once…", S:1068, S:1123, S:3717-3720).

That contract is now broken by the model vendor the harness is built around:

- **Forced tool use is rejected by the current Claude generation.** DOCUMENTED: Claude
  Opus 5.5, Sonnet 5.5, Fable 5.1 and Mythos 5.1 return a 400 `invalid_request_error`,
  `tool_choice: type "tool" and "any" are not supported for this model.`, regardless of
  thinking settings; `auto` and `none` are accepted. Anthropic's stated replacement is
  **structured outputs** (`output_config.format`, GA, no beta header) when "you need the
  response itself in a fixed JSON shape", or `auto` with **strict tool use**
  (`strict: true`) to keep tool inputs schema-valid.
- **The Anthropic-direct path has no fallback.** `judge: claude-opus-5-5` through the
  Anthropic SDK or Vertex (`_get_anthropic_client` S:3644-3680) fails every judge call and
  every pairwise comparison with that 400; the retry policy (S:1694-1709) does not apply
  to this path at all.
- **Through OpenRouter the fix is a degradation.** After #250 the ladder lands on `auto`
  on the third request of every call: two rejected requests per verdict (billed as
  requests under the account's rate cap, not as tokens), no guarantee that the verdict
  tool is called, every call counted under `judge_usage.tool_choice_fallbacks`, and no
  memory of the rung that worked (`_TOOL_CHOICE_LADDER` S:1358 restarts per call).
- **Forcing a tool skipped thinking.** DOCUMENTED: "Forcing a tool also skips thinking on
  every model that accepts forced tool use: the response starts with the `tool_use` block
  and has no `thinking` block." The rationale-first ordering the harness relies on
  (S:1140-1150) was therefore a verdict written without the model's reasoning pass;
  structured outputs let the model think first — "Grammar state resets between sections,
  allowing Claude to think freely while still producing structured output in the final
  response."
- **No schema is strict anywhere.** `strict` has zero hits under `agent_eval/` and
  `skills/` (outside `json.loads(strict=False)`); `minimum`/`maximum` on `score` are
  advisory (S:1131-1133) and the range is enforced after the fact (`_enforce_bounds`).
  On the Anthropic path a content-only reply from a bool judge goes to
  `_parse_bool_response` S:2166-2178, which returns `(False, "Could not parse…")` instead
  of raising — a judge that answers in an unexpected shape fails quietly.

## Provider landscape (what each backend can do today)

| Backend | Structured output of the response | Forced tool call | Strict tool schema | Notes |
| --- | --- | --- | --- | --- |
| Anthropic API, Vertex AI (`anthropic` / bare Claude id) | `output_config.format: {type: json_schema, schema}` — GA; Claude API and Vertex AI both GA (DOCUMENTED). Models: Opus 4.5+, Sonnet 4.5+, Haiku 4.5+, every 5.x, Fable, Mythos (DOCUMENTED list). SDK: `output_config` since `anthropic` 0.77.0 (2026-01-29); the harness floor is `>=0.40` (`pyproject.toml:23,44`). | Rejected with a 400 on Opus 5.5 / Sonnet 5.5 / Fable 5.1 / Mythos 5.1; accepted (thinking skipped) on Opus 5, Haiku 5.5, 4.x. | `strict: true` on the tool, same grammar pipeline and schema subset as structured outputs. | Schema subset: `additionalProperties: false` required on every object; **no `minimum`/`maximum`**, no `minLength`/`maxLength`, no recursion; `enum`/`const`/`$defs` fine; ≤24 optional properties. Unsupported keyword → 400. Response: JSON in the text block; `stop_reason: refusal` (200, schema not honoured) and `max_tokens` (incomplete) must be handled. Prefill is not supported on 4.6+. Bedrock: structured outputs only on the legacy integration (≤ 4.6); the harness has no Bedrock client. |
| OpenAI (`openai:/`, bare non-Claude id) | `response_format: {type: json_schema, json_schema: {name, strict: true, schema}}` on Chat Completions (`text.format` on Responses). gpt-4o-2024-08-06+ and later families (DOCUMENTED; the page names no model that lacks it). Reasoning models included. | Still supported (`required`, named function); no model documented as rejecting it. | `strict: true` on the function ("We recommend always enabling strict mode"). | Strict subset: all properties `required`, `additionalProperties: false`, root must be an object; `minimum`/`maximum` **are** supported (not for fine-tuned models); `allOf`/`not`/`if` unsupported. Refusal → `message.refusal`; truncation → `finish_reason: length`. First request with a new schema pays schema-processing latency, then cached. |
| OpenRouter (`openrouter:/`, OpenAI-shaped) | `response_format` json_schema **per endpoint**: VERIFIED from the keyless catalog, every Anthropic-hosted Claude endpoint (Anthropic, Google, Azure, Claude Platform on AWS) advertises `structured_outputs`; the Bedrock-hosted ones only `response_format`. Open-weights models vary by provider: for `z-ai/glm-5.2` Novita, Z.AI, AtlasCloud lack `structured_outputs` and SiliconFlow lacks `response_format`; for `deepseek/deepseek-v4-pro` GMICloud, DigitalOcean, SiliconFlow, Novita, Azure lack it. DOCUMENTED: an endpoint without support **fails the request with an error** (no silent fallback); `provider.require_parameters: true` restricts routing to endpoints that support every parameter sent; some providers "translate your schema into their own structured-output format" or treat it "as a strong hint, so exact compliance is not guaranteed on every endpoint". | Relayed from the upstream: the Anthropic 400 above (now stepped down by #250), the routing 404 under pins (Decision 25). | Upstream-dependent. | Whether OpenRouter maps `response_format` onto Anthropic's `output_config.format` for Claude endpoints is UNVERIFIED (checklist item 1); the harness parses the reply the same way either way. |
| `runner:/` and `agent:` judges | n/a — prompt-driven JSON (`_RUNNER_LLM_JUDGE_CONTRACT` S:2959-2980, `score.json` S:3022-3037) | not used | not used | Unaffected by this spec. |

The agent path is unaffected throughout: Claude Code sends `tool_choice: auto`
(`preflight.py:8`), and spec 014's agent-role checks stay as they are.

## Design

### The verdict transport

A judge call carries its verdict over one of two **transports**:

| Transport | Request | Reply | Where |
| --- | --- | --- | --- |
| `json_schema` | Structured output of the response: Anthropic `output_config.format`, OpenAI-shaped `response_format` json_schema (`strict: true`). **No `tools`, no `tool_choice`.** | JSON object in the text content, validated in-harness against the verdict schema and the judge's bounds. | New. |
| `tool` | Today's forced tool call (`tool_choice` named tool / function), with the Decision 25 ladder on the OpenAI-shaped path and a new `auto` rung on the Anthropic path. | The tool call's input, strict-parsed on every degraded rung as today. | Existing (S:1282-1289, S:1848-1909). |

The transport is **chosen per judge call by a policy**, recorded on every verdict, and
learnt once per model within a run so a fallback is paid once, not on every call.

### Policy

`models.judge_verdict: auto | json_schema | tool` (default `auto`; validated at load) sets
the run-wide policy; a judge may override it with `verdict:` on its own block (and
`provider_options` keeps carrying OpenRouter-only knobs as before). `auto` resolves per
backend:

| Backend | `auto` first rung | Fallback rungs, in order |
| --- | --- | --- |
| anthropic | `json_schema` when the model id belongs to a family that supports structured outputs (`claude-{opus,sonnet,haiku}-4-5*`, `-4-6`…, every `-5*`, `fable`, `mythos`; a bare or unknown Claude id is assumed capable); `tool` for anything older | `json_schema` → (400 naming `output_config` / structured outputs) → `tool` forced → (400 naming `tool_choice`) → `tool` with `tool_choice: auto` and the strict first-call parse of Decision 25 |
| openai | `json_schema` | (400 naming `response_format` / `json_schema` / structured) → `tool` ladder `function → required → auto` (unchanged) |
| openrouter | `json_schema`, with `provider.require_parameters: true` added to the judge body so OpenRouter routes only to endpoints that honour `response_format` (an amendment to Decision 25 rule 1: an unpinned judge still never sends pins, but it does send `require_parameters` together with `response_format`) | (400 naming `response_format`/structured, or the routing 404 "No endpoints found") → `tool` ladder exactly as #250 left it, without `require_parameters` unless the judge is pinned |
| runner / agent | n/a | n/a |

A refusal of the request itself — any other 400, auth, 429/5xx — never changes the rung:
it propagates through the existing retry policy (`_with_judge_retries` S:1712-1726) as
today. Rung changes are not backed off (a 400 and a 404 do not heal).

**Sticky rung.** The first rung that returns a verdict for a `(backend, model, routing)`
(`routing` = the `routing_sha` of the request's provider body, empty when there is none, so
a pinned and an unpinned judge on one slug learn separately) becomes
the starting rung for every later call of that key in the process (a small locked dict,
reset with the other judge-side counters in `score_cases` S:2610). With `auto` on Opus
5.5 through OpenRouter this means one request per verdict instead of three.

**Explicit policy.** `json_schema` or `tool` pins the transport: no cross-transport
fallback (the Decision 25 ladder inside `tool` still runs), so an operator holding a
baseline on the forced-tool contract can keep it (`models.judge_verdict: tool`). The `tool`
pin reaches models that reject forcing (Claude Opus 5.5, Sonnet 5.5) through the strict
`tool_choice: auto` rung; `verdict_unsupported` (a config error) applies to the
`json_schema` pin only, when the model refuses structured outputs.

### The verdict schema, strict

One function builds the schema both transports send from the existing tool definitions
(`_BOOL_JUDGE_TOOL`, `_score_judge_tool`, `_PAIRWISE_TOOL`), so the field names, their
rationale-first order and their descriptions stay the single source of truth:

- `additionalProperties: false` on every object; every property listed in `required`
  (already true for all three).
- `minimum`/`maximum` are **removed** and folded into the `score` description
  (`"… Integer from 1 to 10."`) — Anthropic's grammar rejects numeric constraints with a
  400, and OpenRouter may relay the schema to an Anthropic endpoint. The range is still
  enforced in-harness by `_enforce_bounds`, exactly as today; nothing is lost because the
  constraint was advisory on the non-strict tool schema anyway (S:1131-1133).
- Property order is preserved. DOCUMENTED on both vendors: OpenAI emits keys in schema
  order; Anthropic emits required properties first, in schema order — with every property
  required the `rationale`-before-verdict order survives on the wire.

The `tool` transport keeps sending the tool definitions **unchanged** (no `strict: true`):
it is the fallback rung, and widening it with a flag that older endpoints may reject
would add a failure mode to the path that exists to absorb failures.

### Prompts

The system prompts are parametrised by transport. `json_schema`: "Respond with exactly one
JSON object and nothing else — no prose before or after it, no code fence. Write the
`rationale` first …, then `score` (an integer from 1 to 10)." The `tool` wording is
unchanged. `_UNTRUSTED_DATA_GUARD` (S:1040-1047) is appended in both cases. The pairwise
system prompt gets the same two variants.

### Parsing the `json_schema` reply

- Anthropic: the concatenated `text` blocks (thinking blocks skipped); `stop_reason ==
  "refusal"` → `JudgeProviderError(error_type="refusal", retryable=False)`; `max_tokens`
  → `JudgeProviderError(error_type="truncated")` (the pairwise caller keeps its doubling
  retry, S:3755-3757, keyed on this error instead of on a failed parse).
- OpenAI-shaped: `message.refusal` → `refusal`; `finish_reason == "length"` →
  `truncated`; otherwise `message.content`.
- The text goes through a **strict parse** (`_loads_strict_json_object`): the whole
  reply is one JSON object, optionally inside a single ```json fence that wraps the entire
  reply. It never scans for an embedded fragment or slices between braces, so a fenced
  snippet quoted in the rationale cannot displace the verdict, and prose around the object
  is `bad_verdict`. The tolerant extraction (`_loads_json_object`, `_extract_judge_json`)
  stays for the forced-tool text fallback only. Then **strict validation**: the object must carry the verdict key with the right type and
  the rationale key; anything else is `JudgeProviderError(error_type="bad_verdict")`.
  There is no prose fallback on this transport: a constrained reply that does not parse
  is a truncation, a refusal or a provider that ignored the schema, and each of those
  must be visible, not scored as `False` or guessed from a number in the prose.

### What is recorded

- Per judge call: `verdict_transport: json_schema | tool`, next to the existing
  `tool_choice_mode` (which keeps its meaning — the forcing mode of a `tool` call — and is
  absent on `json_schema`). Surfaced on the per-case record (S:2710-2714),
  `JudgeProviderError` (`agent_eval/providers/base.py`), the Harbor summary copy
  (`agent_eval/harbor/run.py:435`).
- `summary.yaml` `judge_usage.verdict_transports: {json_schema: N, tool: N}` and
  `judge_usage.verdict_fallbacks: N` (cross-transport fallbacks only: calls that ran on a
  different transport than the policy's first rung; the Decision 25 `tool_choice` steps
  inside the `tool` rung are counted under `tool_choice_fallbacks`, not here). MLflow logs `judge/verdict_fallbacks` next to `judge/tool_choice_fallbacks`
  (`skills/eval-mlflow/scripts/log_results.py:275`).

### Preflight (OpenRouter, pinned judges)

`_check_key` (`agent_eval/providers/openrouter/preflight.py:138-170`) gains the transport:
under `auto`/`json_schema` every eligible pinned endpoint is checked for
`structured_outputs` in `supported_parameters`; a pinned set with none is a WARN under
`auto` (the run will take the `tool` rung) and a FAIL under `json_schema`; the existing
`supports_tool_choice.function` check applies to the `tool` rung as before. Unpinned
judges are not preflighted today and stay that way: `require_parameters` does the routing.

### SDK floor

`anthropic[vertex]>=0.77` (first release carrying `output_config`) in `pyproject.toml`
and `scripts/ensure_deps.py`. An older SDK raises `TypeError` on the keyword; the
Anthropic path treats that as "rung unavailable" and steps to `tool` with a one-line
warning naming the floor, so a stale environment degrades the way an old model does.

## Behavior changes / migration

- **Default transport moves to structured outputs** for Anthropic 4.5+ and OpenAI
  judges, and for OpenRouter judges whose endpoint supports it. Verdict fields, order and
  bounds are identical; what changes is that the model may think before it writes the
  verdict (it could not under a forced tool) and that the schema is enforced by the
  provider. Treat it as a **scores move** boundary in `whats-new.md`: do not compare a
  run judged under 015 with a baseline judged under the forced tool without re-judging.
  `models.judge_verdict: tool` restores the previous contract for a baseline — until the
  judge model is a Claude that rejects forcing.
- `tool_choice_mode` / `tool_choice_fallbacks` keep their semantics; they are simply
  absent/zero on runs where every call went over `json_schema`.
- An OpenRouter judge on Opus 5.5 goes from "fails" (1.56.1) and "three requests per
  verdict, no guarantee" (#250) to one request per verdict with a provider-enforced
  schema.
- A judge on a pre-4.5 Claude (`claude-3-*`) is unaffected: `auto` picks `tool`.
- Judge cost on Anthropic may rise: thinking tokens before the verdict are billed. The
  judge usage side channel already reports `reasoning_tokens` per call.

## Config examples

```yaml
models:
  judge: openrouter:/anthropic/claude-opus-5.5
  judge_verdict: auto            # default — structured outputs first, forced tool second
  providers:
    openrouter:
      judge: {concurrency: 2, max_retries: 8}

judges:
  - name: rubric_strict_baseline
    prompt_file: judges/rubric.md
    verdict: tool                # this judge only: keep the forced-tool contract
```

```yaml
# Pin the whole run to the old contract (a forced-tool baseline on Opus 4.6)
models:
  judge: claude-opus-4-6
  judge_verdict: tool
```

## Verification checklist before merge

1. **UNVERIFIED — OpenRouter → Anthropic schema relay.** One keyed request to
   `openrouter:/anthropic/claude-opus-5.5` with the `submit_score` strict schema; expect a
   JSON text reply, `provider: Anthropic`, no `tool_calls`. Gates: nothing in code (the
   fallback covers a failure), but the docs claim "one request per verdict" only if it
   passes.
2. **UNVERIFIED — `require_parameters: true` on an unpinned judge.** Expect routing to
   skip the Novita/Z.AI-style endpoints for an open-weights judge and no 404 for Claude.
   Gates the Decision 25 amendment; if it 404s, the amendment is dropped and the
   `json_schema` rung relies on the fallback alone.
3. **VERIFIED locally** — `anthropic` 0.89.0 `messages.create` accepts `output_config`,
   exposes `transform_schema`, `StopReason` includes `refusal`; `openai` 2.30.0
   `ChatCompletionMessage.refusal`, `finish_reason` includes `length` and
   `content_filter`.
4. A live Anthropic-direct run on `claude-opus-5-5` (Vertex and API key) for the single
   and pairwise paths before the release notes claim the Anthropic path is fixed.

## Tests

- `_strict_verdict_schema`: `additionalProperties`, required, numeric constraints
  removed and folded into the description, order preserved, idempotent, pairwise enum
  kept.
- Anthropic path (fake client, `TestStructuredJudge`): `json_schema` request shape (no
  tools, `output_config.format.schema`), verdict from text, thinking blocks ignored,
  `refusal`/`max_tokens` errors, 400 naming `output_config` → `tool` rung, forced-tool
  400 → `auto` rung with strict parse, pre-4.5 id starts on `tool`, `TypeError` on
  `output_config` → `tool` + warning, sticky rung across calls, explicit `tool`/`json_schema`
  pins without cross-fallback.
- OpenAI-shaped path (`_or_client`, `TestOpenAIStructuredJudge`, `TestOpenRouterJudge`):
  `response_format` shape with `strict: true` and no `tools`; OpenRouter body carries
  `require_parameters: true` only on this rung; 400 naming `response_format` → tool
  ladder; routing 404 → tool ladder; `refusal`, `length`, fenced JSON tolerated,
  non-verdict JSON → `bad_verdict`; per-case `verdict_transport` and summary counters;
  images still sent on both transports.
- Pairwise: both backends on `json_schema`, truncation doubling keyed on `truncated`.
- Config: `models.judge_verdict` values, per-judge `verdict`, invalid values rejected,
  `provider_options` unchanged.
- Preflight: `structured_outputs` WARN/FAIL matrix for pinned judges.
- Ledger/Harbor/MLflow: the new fields round-trip.
- Docs build `--strict`.
