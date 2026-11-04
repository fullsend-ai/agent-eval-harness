# 014 — First-class OpenRouter provider (agent + judge)

Status: proposed. The agent-under-test talks to OpenRouter **directly** — there is **no
proxy of any kind** in scope — routing pins are enforced by **post-hoc audit** (default)
or by a **server-side per-run key guardrail** (opt-in); the local claude-code runner and
Harbor podman are MVP scope, Kubernetes follows in PR-7.
Base: stacked on spec 013 / PR #216 (`<provider>:/<model>` judge URIs), harness 1.49.1

All `file:line` references are against `/tmp/aeh-main` (release 1.49.1) unless
prefixed with `rfe-creator/` or `harbor/` (the Harbor package, 0.13.1). Evidence labels
used throughout:

- **VERIFIED** — a live no-secret probe, a local run, or the OpenAPI-derived request/
  response schema pages (field lists, status codes).
- **DOCUMENTED** — stated in OpenRouter docs prose only (e.g. keep-alive cadence on slow
  streams, `/endpoints` `status` enum semantics, `Retry-After` handling); re-verified by the
  checklist before the PR it gates. (Streaming event placement and the `X-Generation-Id`
  header are VERIFIED — see Architecture.)
- **UNVERIFIED** — an assumption; every load-bearing one is listed in "Verification
  checklist before implementation" with the PR it blocks.

## Problem

The harness can point a judge at any OpenAI-compatible endpoint (spec 013), but it
has **no notion of a provider for the agent-under-test** and **no cost provenance**:

- `EvalRunner.execute()` receives a bare `model` string (agent_eval/agent/base.py:56-67)
  that `ClaudeCodeRunner` passes to `claude --print --model <model>`
  (agent_eval/agent/claude_code.py:286). Routing Claude Code to a non-Anthropic
  endpoint is done entirely by hand in `execution.env`
  (rfe-creator/eval-openrouter.yaml:21-41): blank the user-forced Vertex vars, set
  `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`, blank `ANTHROPIC_API_KEY`, alias
  `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL` to the slug, and route a per-run
  header through `ANTHROPIC_CUSTOM_HEADERS: $EVAL_RUN_HEADER` (a shim the harness
  already carries, skills/eval-run/scripts/workspace.py:81-85).
- `execution.env` has **three writers with three semantics**: workspace.py
  `_inject_env` resolves `$VAR` and `str()`s everything including `None` → `"None"`
  (workspace.py:644-659); `agent_eval/tools/interception.py:171-176` bakes it into
  Harbor task packages **without** `$VAR` resolution (so `$EVAL_RUN_HEADER` and a
  loopback proxy URL ship literally into containers); `agent_eval/harbor/run.py:196-210`,
  `codex.py:54-56`, `cli_runner.py:228-241` treat it as process env. A fourth writer,
  `runner.settings.env`, is deep-merged last into the workspace settings.json
  (workspace.py:721-757) and is the documented way to add Claude Code env (runner.md:125-142).
- Claude Code cannot set OpenRouter request-body fields, so today the provider pins
  that made open-model runs comparable (`provider.order`, `allow_fallbacks: false`,
  `quantizations`, `require_parameters`) live only in an untracked proxy config (today
  rfe-creator runs the agent through a local translating proxy — LiteLLM — plus a
  cost-reconcile hook; the proxy config in rfe-creator, `eval/litellm/config.yaml:44-58`), together with router
  retries, first-token timeouts and cooldowns (config.yaml:216-229). That proxy is itself
  a problem: a second process to run, a config to keep in sync, a monkeypatch to carry
  across proxy versions, and nothing that works inside a Harbor podman container or a K8s
  pod without more infrastructure. The harness has **no
  way to say what routing a run *should* have had and no way to check what it *did*
  have** — the `/generation` endpoint returns `provider_name` per request and the public
  `/endpoints` catalog says which providers serve which quantization, but nothing reads
  them.
- `cost_usd` is Claude Code's **Anthropic-priced estimate** (`total_cost_usd` /
  `modelUsage.costUSD`, agent_eval/agent/stream_capture.py:123,147; `_billed_cost`
  claude_code.py:736-750). For OpenRouter models it is 2x-55x inflated across 13
  local runs (e.g. $87.81 estimated vs $1.67 real). rfe-creator repairs it
  out-of-band: the proxy captures inline `usage.cost`/`provider` only via a
  version-fragile `chunk_parser` monkeypatch (the proxy's `custom_callbacks.py:187-217`),
  and `reconcile_cost.py:73-112` rewrites `run_result.json` from two hooks, with an
  all-null guard (:171-175) that silently keeps the inflated number. Harbor cannot
  be reconciled at all (no lifecycle hooks; `run.py:458` calls `generate_report`
  directly). Every reader — `compute_run_metrics` (score.py:3219-3255), report.py
  Cost rows (:943-982, :1248-1257), eval-compare (compare.py:126-139), eval-anova
  (analyze.py:330-335), MLflow (log_results.py:281-303) — treats `cost_usd` as billed.
- Judge spend is invisible on **every** backend: the scorer contract is a bare
  `(value, rationale)` tuple (score.py:1574-1580, 1974-1994) and neither
  `_call_structured_judge_openai` (:1353-1400) nor `_call_pairwise_openai`
  (:2979-3012) reads `response.usage`. `openrouter:/` is rejected by
  `resolve_judge_backend` (agent_eval/prompt_backends.py:86-91); the only route
  is `openai:/<slug>` + process-global `OPENAI_BASE_URL`/`OPENAI_API_KEY`
  (score.py:1279-1306), which cannot coexist with a real OpenAI judge, sends no
  routing body, and is forwarded to codex agents and podman containers.
- `--max-budget-usd` (claude_code.py:288) is enforced by the CLI on the inflated
  estimate, so OpenRouter configs set `max_budget_usd: 100.0` to avoid being killed early.
  The field is documented as a **per-invocation** cap (execution.md:14,29; config.py:436;
  `steps[].max_budget_usd` overrides, config.py:1155-1171) and resolved once per case/step
  (execute.py:493-495, 1289-1290) — any replacement must keep that scope.
- a per-provider copy of the base config drifts from it (the copy in use today already
  differs by 9 judge names, the timeout, `traces.events` and permissions); the harness has no
  overlay/include mechanism, and five production readers load eval.yaml raw
  (harbor/tasks.py:99, harbor/run.py:452, evalhub/runner.py:206,
  skills/eval-analyze/scripts/validate_eval.py:447, config.py `discover_configs` :1798,
  plus report.py:2978 `_load_yaml`, anova/matrix.py:42, reorganize.py:45), so an overlay
  resolved only in `from_yaml` would be invisible to them.

Goal: **declare OpenRouter once (`openrouter:/<author>/<slug>` on any model role
plus a `models.providers.openrouter` block); have the agent-under-test talk to OpenRouter
directly (no proxy of any kind); get *audited* routing
(preflight resolves the declared pins against the public catalogs, the per-request
`/generation` backfill checks every served provider against them, and an opt-in per-run
key guardrail enforces them server-side), true cost with provenance from the same
backfill, and judge cost — on local claude-code, Harbor podman and Kubernetes alike —
without a monkeypatch or a second config file, while keeping every existing config,
budget contract and Harbor/K8s deployment working unchanged.** The fix is
*direct transport + audit*, not a proxy; what a proxy would add (per-request body pins,
an in-flight real-cost gate) is listed under "Out of scope (future)".

## Design

### Architecture at a glance

**One transport** for the agent-under-test (direct to OpenRouter, on every runner), **two
enforcement levels** for the declared routing pins, one contract for everything downstream:

| Role | Talks to | How | Routing pins | Cost truth | Who holds which key |
| --- | --- | --- | --- | --- | --- |
| agent (`skill`/`subagent`/`hook`), local claude-code | `https://openrouter.ai/api` (`/v1/messages`) | env template written into the per-run overlay (`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, blank `ANTHROPIC_API_KEY`, blank Vertex vars, `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU,FABLE}_MODEL` + `CLAUDE_CODE_SUBAGENT_MODEL` aliases; Direct transport contract) | model-suffix variant in the id (`:exacto` recommended for tool-calling agents — accepted, VERIFIED; `:nitro`/`:floor`) + the enforcement level below; **no request-body pins** (Claude Code cannot send them) | **per request**: `GET /api/v1/generation?id=<gen-…>` backfill keyed on the `assistant.message.id`s in Claude Code's stream-json (`cost_source: openrouter:generation`; probe #12 VERIFIED 2026-09-16) + `GET /api/v1/key` usage delta as cross-check / < 0.8-coverage fallback | agent holds the run's inference key |
| agent, Harbor podman | same | same env block, passed via `--agent-env` (Harbor merges it last, `harbor 0.13.1 agents/base.py:288-291`, VERIFIED; value-free argv via carriers, harbor/run.py:216-233); host Vertex vars **not** forwarded while a plan is active | same | same, gen ids parsed from the trial's captured stream-json (harbor/results.py:80-160 extended; probe #25 UNVERIFIED) + key-usage fallback | same |
| agent, Harbor Kubernetes / EvalHub | same | credentials Secret carries `OPENROUTER_API_KEY`; the harness maps it to `ANTHROPIC_AUTH_TOKEN` via `secretKeyRef` next to the existing `envFrom` (kubernetes.py:244-246); non-secret plan env in the pod spec; Vertex vars already excluded (kubernetes.py:42-50) | same | same (transcripts come back through the job dir) | same |
| judges | `https://openrouter.ai/api/v1/chat/completions` | OpenAI SDK, dedicated client (never process-global `OPENAI_*`), `extra_body` routing, `usage.cost` inline | judge pins opt-in (`judge.inherit_pins`), `tool_choice` ladder (Decision 25) — unchanged | `source: judge` ledger records, `judge_cost_usd` in `summary.yaml` | judge client holds `OPENROUTER_API_KEY` in the harness process only |

| `models.providers.openrouter.routing.enforcement` | What is enforced, where | Budget | Key isolation | Requires |
| --- | --- | --- | --- | --- |
| `audit` (**default**) | **preflight** resolves the declared pins (`order`/`quantizations` intent per routing key) against the public `/models/{slug}/endpoints` + `/providers` catalogs and writes `routing_snapshot.json`; **post-run audit** joins every backfilled `provider_name` against the pinned set → `routing.violations`, `routing_enforcement: audit`; compare/anova refuse to pool runs whose audits differ (`policy: strict` → run degraded/failed per config; `warn` → flagged) | CLI cap on the inflated estimate during the run; **real cost is post hoc** (`--strict-cost` fails the run when the backfilled Σ exceeds `budget.run_usd`) | none — the agent holds the operator key (`provider.key_exposed_to_agent: true`) | `OPENROUTER_API_KEY` |
| `key-guardrail` (opt-in) | everything `audit` does **plus** a per-run inference key provisioned through the management API with a guardrail allow-list = pinned providers and `limit_usd` = run budget, used for the run and revoked in a `finally` → **server-side** enforcement of pins and budget | server-side `limit_usd` (real cost, in flight) + the CLI cap | per-run key; the agent never sees the operator key | `OPENROUTER_API_KEY` + `OPENROUTER_MANAGEMENT_KEY`; guardrail field semantics DOCUMENTED/UNVERIFIED (probe #26) |

There is no proxy process and no second transport mode. An operator who already fronts
Claude Code with an operator-run Anthropic-compatible endpoint keeps doing
so through plain `execution.env` exactly as today — that path is untouched and
**unsupported by this feature** (no ledger; `cost_source: runner:estimate`, a label the
runner itself writes — reconcile is a no-op with no plan). Judges behind such an endpoint
keep the unchanged PR #216 paths (`anthropic:/` + `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_BASE_URL`,
or `openai:/` + `OPENAI_BASE_URL`).

Load-bearing facts, by evidence class:

- **VERIFIED (live / schema):** OpenRouter's `POST /api/v1/messages` speaks the Anthropic
  Messages format with streaming, tools, extended thinking, images and `cache_control`;
  its request body ACCEPTS `provider{order,allow_fallbacks,require_parameters,quantizations,sort,only,ignore,…}`,
  `models`/`fallbacks`, `session_id`, `user`, `plugins`, `trace` and NOT `usage`/`transforms`/
  `reasoning`/`preset`/`response_format`; `usage.cost` (+`cost_details`, `is_byok`) and a
  top-level `provider` are ALWAYS returned (`usage.include` is a deprecated no-op);
  `POST /api/v1/messages/count_tokens` does NOT exist (404); Bearer auth is the documented
  path and a non-empty `ANTHROPIC_API_KEY` is sent as `x-api-key` **and a valid key sent
  that way works (probe #13, 2026-09-16)**; `/api/v1/providers`,
  `/api/v1/models`, `/api/v1/models/{author}/{slug}/endpoints` are public; the
  account paid-model-training opt-out filters endpoints (the 2026-09-10 404 body).
- **VERIFIED (live probes 2026-09-16, `probes/probe_report_2026-09-16_run{1,2}.json`;
  checklist rows 3–8, 12, 13, 16-KEY, 17–20):** on the `/messages` stream
  `message_start.message` carries `id` (`gen-…`), `model` (bare slug echo), `provider`
  (display name) and `usage`; `usage.cost` arrives in `message_delta.usage`;
  `X-OpenRouter-Metadata: enabled` is honoured and `openrouter_metadata` arrives in
  `message_stop` with `endpoints.available[]` where the served endpoint is the entry with
  `selected: true` and `model` is the dated permaslug (there is no `endpoints.selected`
  field); `X-Generation-Id` is a response header; `usage.cost == /generation total_cost`
  (credits are USD 1:1) but `/generation` lags `message_stop` by 7.6–12.7 s; explicit
  `order` + `allow_fallbacks: false` overrides Auto Exacto under `tool_choice: auto`, while a
  forced `{type: tool, name}` under pins can 404 `not_found` ("No endpoints found") on a
  model that routes fine unpinned; provider ids match by slug or display name; bare,
  `:variant` and `[1m]` ids all echo the bare slug; keep-alive comments are absent on fast
  streams; Claude Code's direct-mode stream-json `message.id`s are `gen-…` ids and its
  `modelUsage` keys echo the bare slug.
- **DOCUMENTED (docs prose, not contradicted by the probes):** keep-alive comments
  `: OPENROUTER PROCESSING` are emitted on slow streams; errors carry a canonical
  `error_type`; commit semantics (200 committed once provider headers arrive; no failover
  after the first token); the endpoint `status` enum beyond the observed `{0, -2}`.
- **VERIFIED (routing, probe #5, 2026-09-16 — load-bearing for the enforcement design):**
  an explicit `provider.order` + `allow_fallbacks: false` **overrides Auto Exacto** under
  `tool_choice: auto` (20/20 requests served by the pinned provider), so a pinned set is a
  meaningful audit target even for a `:exacto` id; a **forced/named `tool_choice` under the
  same pins 404s** `not_found` "No endpoints found" on a model that routes fine unpinned —
  routability of a pinned judge is therefore a **preflight** property
  (`supports_tool_choice` per pinned endpoint), and the agent path never forces
  `tool_choice`. `:exacto` on the model id is accepted by `/messages` (probe #13).
- **DOCUMENTED / UNVERIFIED:** management-API key
  guardrail semantics — allowed-provider list, `limit_usd`, training-opt-in interplay
  (probe #26, blocks `key-guardrail` in PR-6); Harbor trial artefacts contain the
  `assistant.message.id`s (probe #25, blocks per-request cost on podman — fallback: key-usage
  delta).
- **VERIFIED (no-key Claude Code CLI probes 2026-09-16, Claude Code 2.1.273+ against a local
  fake Anthropic endpoint; `probes/probe_cli_report_2026-09-16.json`, produced by
  `specs/014-openrouter-provider/probes/probe_claude_cli.py`; checklist rows 1, 9, 10, 22, 23, 27):** a `--settings` env
  block beats a user-level `~/.claude/settings.json` that forces `CLAUDE_CODE_USE_VERTEX=1` and
  routes **every** request of a 3-turn run — root turns and the spawned subagent's request —
  to `ANTHROPIC_BASE_URL`; a PreToolUse hook subprocess inherits the settings-env values
  (`ANTHROPIC_BASE_URL`, blanked Vertex vars, `ANTHROPIC_AUTH_TOKEN`); a single-header and a
  three-line `ANTHROPIC_CUSTOM_HEADERS` value both arrive as separate headers on root and
  subagent requests; the CLI made **zero** `count_tokens` calls in a run with a tool call and
  a subagent; `--max-budget-usd 0` is rejected by the CLI before any request ("must be a
  positive number greater than 0", exit 1). Claude Code posts to `/v1/messages?beta=true` —
  any path match must strip the query string.
- **UNVERIFIED (remaining no-key rows):** #11 (container egress to `openrouter.ai`),
  #15, #16 (echo-server half), #24, #25.

Without a proxy on the HTTP path, every Claude-Code-side behaviour above **is** load-bearing
for the default mode. PR-5 (MVP) ships with `enforcement: audit`, whose two inputs are
already VERIFIED: stream-json gen ids (probe #12) and `/generation` `provider_name` +
`total_cost` (probe #6); the open rows #25 and #26 gate only per-request cost on podman and
`key-guardrail` respectively.

### Contracts

#### Model URIs

Same grammar as spec 013 (`split_model_uri`, prompt_backends.py:25-36):
`openrouter:/<author>/<slug>[:variant[:variant…]]` is accepted on `models.skill`,
`models.subagent`, `models.judge`, `models.hook`, per-judge `model:`, and the CLI
`--model`/`--subagent-model`/`--judge-model`. `openrouter:/` is the only URI scheme this
spec adds; this spec reserves no other scheme (Decision 1).
Variants (`:exacto`, `:nitro`, `:floor`; combinable, last one sets the sort — VERIFIED)
travel in the URI tail and are passed verbatim to Claude Code (`--model z-ai/glm-5.2:exacto`)
and to the alias env vars; on the agent path they are the **only** routing control that
reaches OpenRouter in the request (Direct transport contract). Bare ids keep today's semantics (a bare `z-ai/glm-5.2` still
means "whatever the ambient endpoint serves"; nothing is inferred from a slash;
`is_anthropic_model("openrouter:/anthropic/claude-…")` is `False` because the explicit
provider wins, prompt_backends.py:49-51). The routing key of a model is its **routing
key**: provider prefix, `:variant` suffixes, `[1m]` markers and an `openrouter/` prefix
stripped (`routing_key()` in `agent_eval/providers/base.py`). The helper is deliberately
not called `canonical_slug`: OpenRouter's `canonical_slug` field is the dated permaslug
(`z-ai/glm-5.2-20260616`), a different identifier.

Within one run the agent roles (`skill`, `subagent`, `hook`) must share a provider kind
(one plan per run); mixing `openrouter:/` and a bare id across them is a load error
(Config validation) — one overlay env block cannot serve two endpoints.

#### Provider registry (`models.providers`)

```
models:
  skill: openrouter:/<author>/<slug>   # any role URI may name a declared provider
  providers:
    <name>:                 # the only name in this spec: openrouter
      kind: <name>          # optional; must equal <name> for now (reserved for future
                            #   user-chosen names and kind: openai-compatible)
```

The registry is a sub-key of `models:`, not a top-level key (Decision 17): it exists only
to resolve the `<provider>:/<model>` URIs that already live under `models.*` (skill /
subagent / judge / hook — the cross-cutting scope of spec 013 / PR #216), so nesting keeps
model configuration cohesive, adds no top-level namespace, and lets an `extends:` overlay
override `models.providers.openrouter.routing` alongside `models.skill` in one block.
Builtin implicit providers `anthropic`, `openai`, `runner` are URI schemes with today's
env-driven behaviour and cannot be declared under `models.providers`. **One declared kind**:
`openrouter` (full design below). No second provider kind is introduced: an operator who
runs an Anthropic-compatible endpoint of their own keeps reaching it through plain
`execution.env` as today, outside this feature. `kind: openai-compatible` is
reserved in the schema and rejected with "not implemented" (Out of scope). A provider
entry is a **declaration**: it is inert until an effective role URI names it (see
`execute.py` activation rule). Validation rejects an explicit URI provider only if it
is neither builtin nor declared. Sub-keys of `models.providers.openrouter` (the `OpenRouterConfig` dataclass, config.py): `kind`,
`api_key_env`, `base_url`, `management_key_env`, `attribution`, `background_model`,
`preflight`, `cli_budget_inflation`, `budget`, `routing` (`defaults`, `models`, `policy`,
`enforcement`, `guardrail`), `judge`. There is no transport-mode key of any kind; an
unknown sub-key fails validation by name with a pointer to Decision 1.

#### Config overlay (`extends:` — a NEW, optional top-level key, PR-3a)

`extends:` is **not an existing harness feature**: it is a new top-level eval-config key
proposed by this spec and delivered in PR-3a. Semantics: `extends: <path relative to the
file>` loads the base config and deep-merges the overlay on top (dicts merge, scalars
override, lists per the merge policy in the `agent_eval/config.py` section — scalar lists
extend with dedupe, `judges`/`steps` merge by key — with a `!replace` tag as the escape
hatch). It is resolved in the **single raw loader** (`load_raw`), so every reader —
execute/score/report, Harbor task bundling, EvalHub, validate, discovery — sees the merged
config, and the resolved chain is recorded in `eval_params.config_chain`. The key is
**independent of the OpenRouter feature and droppable**: its motivation is the
`eval.yaml` / `eval-openrouter.yaml` drift in rfe-creator, not the transport. Relation to
rfe-creator: rfe-creator now *generates* `eval.yaml` from a skeleton plus per-type fragments
(`scripts/generate_eval_config.py`); the generated `eval.yaml` stays the base and an
OpenRouter profile `extends:` it, so the generator is untouched. (Alternatively the
generator could emit per-provider configs directly, in which case PR-3a would be skipped.)

#### `RoutingSpec` (one object, two serialisations)

```
RoutingSpec:
  order: list[str] | None            # provider slugs, tried in order
  allow_fallbacks: bool | None       # False = only `order`
  require_parameters: bool | None
  quantizations: list[str] | None    # int4|int8|fp4|mxfp4|nvfp4|fp6|fp8|mxfp8|fp16|bf16|fp32|unknown
  sort: str | dict | None            # price|throughput|latency or {by, partition}
  only: list[str] | None
  ignore: list[str] | None
  data_collection: "allow"|"deny"|None
  zdr: bool | None
  max_price: dict | None
  fallbacks: list[str] | None        # OpenRouter `models` (≤3, VERIFIED)
```

**Two consumers, two serialisations.** The same merged `RoutingSpec` per routing key is
read by (a) the **agent path**, where it is a *declaration of intent* that never reaches
the request body — Claude Code cannot send `provider`/`models` (VERIFIED: no request-body
control from the CLI) — and is instead (1) checked by preflight against the catalogs,
(2) audited post hoc against `/generation provider_name`, and (3) under
`enforcement: key-guardrail` turned into the per-run key's allowed-provider list; only
`order`, `only`, `ignore`, `quantizations` and `fallbacks` are meaningful there
(`allow_fallbacks: false` = "any served provider outside `order` is a violation";
`require_parameters`, `sort`, `data_collection`, `zdr`, `max_price` are **ignored on the
agent path with a validation WARNING** — `sort` is expressed through the `:nitro`/`:floor`
variant, data policy through the account settings); and (b) the **judge path**, unchanged:
`to_chat_extra_body()` below sends the spec in the request. Merge order (later wins,
dict-deep, **lists replace** — this is the one place list
replacement is correct: `order` and `quantizations` are complete statements, unlike the
`extends:` overlay policy below):
`models.providers.openrouter.routing.defaults` ← `models.providers.openrouter.routing.models.<routing key>`
← role override (`judges[].provider_options.routing`). Provider identifiers are
normalised to lowercase slugs via the `/api/v1/providers` catalog before sending
(`Novita`→`novita`, `Z.AI`→`z-ai`, `StreamLake`→`streamlake`). This is a **cosmetic /
consistency step, not a correctness requirement**: OpenRouter accepts both the slug and the
display name, case-insensitively (probe #4 VERIFIED 2026-09-16 — `["z-ai"]` and `["Z.AI"]`
both served by Z.AI), so normalisation only keeps `routing_sha`, the ledger `provider` field
and the snapshot stable across spellings. Display names are accepted **with a WARNING**
(normalised to the slug, e.g. `"Z.AI" → z-ai`); unknown names (matching neither slug nor
display name in the catalog) still fail `preflight: strict`.
`pinned_set(catalog)` yields the agent-path audit target for a routing key: the set of
`(provider slug, permaslug, quantization)` endpoint tuples that satisfy `order`/`only`/
`ignore`/`quantizations` per the `/endpoints` catalog, or `None` when the key is unpinned
(no `order`/`only` and `allow_fallbacks` not `false`) — an unpinned key is never audited.
`to_chat_extra_body(slug, role="judge")` yields the judge `extra_body` — the full
`{"provider": {...}, "models": [...]}` dict **only when the judge inherits pins** (`models.providers.openrouter.judge.inherit_pins: true`
or a per-judge `provider_options.routing`, Decision 25); otherwise the judge dict carries
only the non-binding keys (`sort`, `data_collection`, `zdr`, `max_price`, `fallbacks`) and
no `order`/`only`/`quantizations`. `require_parameters` is sent for judges **only when the
judge routing carries pins** (`order`/`only`); an unpinned judge never sends it. It does not
make a forced `tool_choice` routable — an endpoint that lacks
`supports_tool_choice.function` 404s the model as "No endpoints found" whether or not
`require_parameters` is set (probe #5, 2026-09-16), so routability is a **preflight**
property (`supports_tool_choice` per pinned endpoint) with the judge fallback policy under
score.py and Decision 25. The SHA-256 of
the canonical JSON of the merged spec per routing key is `routing_sha`, recorded in
`eval_params.provider.routing_sha`, `routing_snapshot.json` and every ledger record. On
the agent path nothing is ever *sent*, so there is no "effective vs configured" split:
`routing_sha` is the SHA of the declared spec, and compare/anova pool only runs whose
`routing_sha` **and** audit outcome match (Reconcile `routing`). On the judge path a record's
`routing_sha` is the SHA of the `extra_body` actually sent after the Decision 25 ladder.

#### Ledger (`<run_dir>/provider/ledger.jsonl`)

One JSONL file per run, `O_APPEND`, one record per **generation** the harness learns
about: agent/hook generations from the `/generation` backfill of the stream-json ids,
judge calls from the judge client's response, plus at most one run-level `key-usage`
record. The harness is **not** on the request path, so it never sees a request that
OpenRouter did not answer with a gen id (a 4xx/5xx before `message_start` leaves no id in
stream-json) — failed attempts are visible only through Claude Code's own error output,
never as ledger rows; `requests_missing_cost` therefore counts ids whose backfill failed,
not failed requests. Never bodies, headers or keys. The path is provider-neutral so a
future provider kind writes the same file.

```json
{
  "ts": "2026-09-16T12:00:00.123Z",
  "run_id": "2026-09-16-glm", "case_id": "case-01", "step_id": null, "judge": null,
  "provider_kind": "openrouter",       // openrouter | anthropic | openai (anthropic/openai only on judge records)
  "role": "agent",                     // agent | hook | judge | key-usage (run-level delta record, one per run)
  "source": "generation",              // generation (GET /api/v1/generation backfill) | key-usage (GET /api/v1/key delta) | judge (judge-client response, usage.cost inline)
  "gen_id": "gen-01J…",                // assistant.message.id from stream-json (agent/hook) or response id (judge); null on key-usage
  "message_index": 17,                 // ordinal of the assistant message in the case's stream-json; ties the row back to the transcript without storing content
  "model_requested": "z-ai/glm-5.2:exacto",      // the id the harness put in --model / the alias env (agent) or the judge request
  "model": "z-ai/glm-5.2",                       // routing_key(model_requested)
  "model_echo": "z-ai/glm-5.2",                  // assistant.message.model in stream-json — echoes the BARE slug (VERIFIED 2026-09-16, probes #12/#19)
  "model_served": "z-ai/glm-5.2-20260616",       // dated permaslug from /generation `model` (agent) or the chat response (judge)
  "provider": "novita", "provider_name": "Novita", "endpoint_tag": "novita/fp8", "quantization": "fp8",
                                       // provider_name = display name from /generation provider_name (or the judge response `provider`); provider = catalog slug; quantization/endpoint_tag from the /endpoints catalog joined on (provider, permaslug) — /generation carries no quantization
  "audit": "compliant",                // agent/hook rows under a pinned key: compliant | violation | unattributed; null for unpinned keys, judge and key-usage rows
  "status": "ok",                      // ok | backfill_failed (gen id known, /generation never answered within the backoff + run-end retry; cost_usd null) | partial (key-usage row when coverage < 1)
  "stop_reason": "tool_use", "native_finish_reason": "tool_calls",   // /generation finish_reason / native_finish_reason
  "tool_choice_mode": null,            // judge records only: function | required | auto — the forcing mode actually sent after the Decision 25 fallback ladder; null on non-judge records
  "error_type": null, "error_class": null, "error_message": null,   // backfill/judge-client errors only; message ≤200 chars
  "cost_usd": 0.001234,
  "cost_details": {"upstream_inference_cost": null}, "is_byok": false,
  "tokens": {"input": 1200, "output": 340, "cache_read": 900, "cache_create": 0, "reasoning": 120},   // /generation native token counts (tokens_prompt/tokens_completion/…); judge: response.usage
  "streamed": true, "latency_ms": 20431, "generation_time_ms": 19600,   // /generation latency / generation_time; null on judge rows
  "backfill_lag_s": 9.4,               // wall time from the id's first sighting to the successful /generation answer (VERIFIED 8–13 s typical)
  "routing_sha": "9f3a…"               // SHA of the declared spec for this routing key (agent/hook) or of the extra_body sent (judge)
}
```

**Row semantics.** OpenRouter commits and bills a
generation once it answers 200; the harness learns of it from the `gen-…` id that
`stream_capture.py` already reads from every `assistant.message.id` (stream_capture.py:106,
203, 244; probe #12 VERIFIED) and, for a stream Claude Code abandoned mid-way (case
timeout, killed process), from the same id — the generation is billed either way and
`/generation` still answers for it, so **there is no "truncated" class**: a committed
generation is one row, `status: ok`, authoritative `cost_usd`, whether or not the client
read it to the end. The three row states are:

- `status: ok` — `/generation` answered; `cost_usd = total_cost` (== the stream's
  `usage.cost`, probe #6), `provider_name`, `model` (permaslug), token counts filled.
- `status: backfill_failed` — an id was seen but `/generation` never answered 200 within
  the per-id backoff (5 s → 2 s steps → 60 s cap) plus the run-end retry; `cost_usd: null`,
  counted in `cost_coverage.requests_missing_cost` and `routing.unattributed`. The id stays
  in the row so a later `agent-eval provider backfill <run_dir>` can complete it (readers
  re-reconcile).
- `role: key-usage`, `status: ok | partial` — one run-level row: `cost_usd` = `GET /api/v1/key`
  `usage` after − before (taken ≥ 20 s after the last agent process exits, VERIFIED settle);
  `partial` when generation coverage < 1.0 says the delta is the only number covering the
  missing ids. It is a **cross-check** when coverage ≥ 0.8 (`cost_warnings` when Σ ledger
  deviates > 5 %) and the **cost source** below 0.8 (Reconcile). It is exact only on a key
  nothing else uses — `enforcement: key-guardrail` guarantees that by construction (the
  per-run key), `audit` relies on the operator's `budget.dedicated_key: true` assertion.

Ids are collected **as the run proceeds** (stream_capture's per-message hook in local mode;
the captured transcript in Harbor), the backfill runs on a background thread from first
sighting, and reconcile at each `run_result.json` write site uses whatever has landed —
so the live per-case cost line converges within ~15 s of a case ending rather than at
run end. Judge rows (`source: judge`) are written synchronously from the judge client's
response (`usage.cost` inline, no backfill).

`error_class` ∈ `infra | config | agent` (enum in `agent_eval/providers/base.py`, mapping
for OpenRouter's `error_type` in `agent_eval/providers/openrouter/errors.py`); it classifies
**backfill and judge-client** failures (`/generation` 404 after
the retry window → `infra`, 401/403 → `config`, judge 404 "No endpoints found" → `config`
with `error_message` prefixed `routing:`), and is also applied by `stream_capture.py` to
the `error` events Claude Code itself prints so that a case that failed on a provider 5xx
is classified `infra` in `run_result.json` even though no ledger row exists for it. Raw
error JSON is kept only in an opt-in debug file under `AGENT_EVAL_DEBUG`.

#### Reconcile (pure function, called once at every `run_result.json` write)

`agent_eval/providers/reconcile.py:reconcile(run_result: dict, ledger: Iterable[dict], plan: ProviderPlan|None, *, key_usage: KeyUsageDelta|None, catalog: ModelCatalog|None = None) -> dict`

Adds/normalises these fields (all optional; old files stay readable):

| Field | Meaning |
| --- | --- |
| `cost_usd` | agent spend. Provider active: when generation coverage ≥ 0.8, Σ ledger `cost_usd` over `role: agent` rows with `status: ok` (6 dp); when coverage < 0.8, the `key-usage` row's delta (the only number that covers the missing ids); `null` when neither landed (no ok rows and no key delta). Provider inactive: unchanged. Never a mix of the two sources and never the estimate. |
| `cost_usd_estimate` | the runner's own number (Claude Code's Anthropic-priced estimate, 2–60× inflated on OpenRouter); set once, never overwritten (idempotent). |
| `cost_source` | `<origin>:<method>`: `openrouter:generation` (Σ backfilled rows, coverage ≥ 0.8), `openrouter:key-usage` (key delta, coverage < 0.8), `runner:estimate` (provider active but no truth source landed **and** the operator passed `--allow-estimate`, otherwise `unavailable`; also written **by the Claude Code runner itself, never by reconcile**, when its own env carries an `ANTHROPIC_BASE_URL` whose host is not `api.anthropic.com` — an operator-run Anthropic-compatible endpoint through plain `execution.env`, no plan: the CLI's Anthropic-priced number is an estimate there, and reconcile leaves such a file untouched), `runner:reported` (Claude Code on Anthropic-direct/Vertex, opaque CLI `metrics.json`; legacy default when the field is missing), `harness:estimate` (the codex runner's own price-table estimate), `unavailable`. Legacy literals are recognised by every reader: `openrouter-reconciled` (files patched by `reconcile_cost.py`) reads as real OpenRouter cost, `runner-reported`/`harness-estimate` read as their colon forms. |
| `cost_confidence` | **by coverage** (`requests_priced / requests`, over the stream-json `message_ids` of the run — the denominator is what the transcript says happened, not what the ledger holds): `high` — coverage ≥ 0.95 (the expected outcome now that probe #12 is VERIFIED) **and** the key-usage cross-check, when available, is within 5 %; `medium` — coverage in [0.8, 0.95), or key-usage-only on a key asserted dedicated (`key-guardrail`'s per-run key, or `budget.dedicated_key: true` under `audit`); `low` — coverage < 0.8 on a non-dedicated key, or coverage ≥ 0.8 with a key-usage deviation > 5 % (something else spent on the key, or ids are missing from the transcript). |
| `cost_coverage` | `{requests, requests_priced, requests_missing_cost, requests_unattributed, coverage, key_usage_delta_usd, key_usage_settle_s}` — `requests` = distinct `gen-…` ids seen in stream-json (agent + hook), `requests_priced` = `status: ok` rows, `requests_missing_cost` = `backfill_failed`, `requests_unattributed` = rows without `provider_name` (expected 0; `/generation` always carries it). |
| `cost_warnings` | list of strings, e.g. `"ledger sum $1.61 differs from key-usage delta $1.67 by 3.6%"`, `"3 of 1341 requests lack provider attribution; routing audit incomplete"`, `"per-model cost: no ledger rows for modelUsage key 'z-ai/glm-5.2-20260616'"`. |
| `hook_cost_usd` | Σ ledger `cost_usd` for `role: hook`; excluded from `cost_usd` and from `compute_run_metrics`. |
| `providers` | `{"<provider slug>": {"requests": N, "cost_usd": X}}`; unattributed records under `"unknown"`. |
| `per_model_usage[m].cost_usd` | Σ ledger cost joined per the **per-model join rule** below; `per_model_usage[m].cost_usd_estimate` preserved; `per_model_usage[m].providers` added. No proportional split is ever performed. |
| `routing` | `{"enforcement": "audit"\|"key-guardrail"\|"none", "policy": "strict"\|"warn", "sha": "…", "snapshot": "provider/routing_snapshot.json", "audited": N, "compliant": N, "violations": [{gen_id, case_id, provider, quantization, expected: [..]}], "unattributed": N, "degraded": bool, "degraded_reason": null\|"violations"\|"unattributed"\|"preflight", "served": {"novita/fp8": 1341, …}, "audit_complete": bool}`. `enforcement: none` is written only for unpinned routing keys (nothing to audit). `audit_complete` is `false` while any id is still `backfill_failed`. `degraded: true` when `policy: strict` and (`violations` > 0 or `unattributed` > 0 or the preflight downgraded); under `warn` the same conditions set `degraded_reason` but not `degraded`, and the report shows the flag. compare/anova pool two runs only when `sha` matches **and** both have `violations == []` and `audit_complete: true` (or the operator passes `--allow-unaudited`). |
| `provider` | `{"name": "openrouter", "kind": "openrouter", "transport": "direct", "runner": "claude-code"\|"harbor-podman"\|"harbor-k8s"\|"evalhub", "base_url": "https://openrouter.ai/api", "key_exposed_to_agent": bool, "key_scope": "operator"\|"per-run", "key_hash": "sha256:…8", "background_model": …}`. `key_exposed_to_agent` is `true` at `audit` (the agent process holds the operator key) and `true` at `key-guardrail` too (it holds the per-run key) — what changes is `key_scope`. `key_hash` is the first 8 hex of the SHA-256 of the key actually used, so two runs can be shown to share/not share a key without recording it. |
| `budget` | `{"invocation_usd": X, "run_usd": Y\|null, "cli_cap_usd": Z, "enforcement": "key-guardrail"\|"cli-estimate", "exceeded": null\|"invocation"\|"run", "exceeded_reason": null\|"cli-cap"\|"limit_usd"\|"post-hoc", "overshoot_usd": …}`. `cli-estimate` (the `audit` level) means the only in-flight cap was the CLI's on the inflated estimate; `"run"` + `"post-hoc"` is set by reconcile when the backfilled Σ exceeds `budget.run_usd` (and `--strict-cost` then fails the run). `key-guardrail`: `"run"` + `"limit_usd"` when OpenRouter refused with 402 `insufficient_credits`/key-limit on the per-run key (detected from Claude Code's error output, `stream_capture.py`). |
| `judge_usage` | never written here — judge spend lives in `summary.yaml` (below). |

**Per-model join rule.** A `per_model_usage` key `m` (Claude Code's `modelUsage` key,
which comes from the response-echoed `message.model`, stream_capture.py:106-107,124-148 —
not from `--model`) matches a ledger record when `routing_key(m)` equals `routing_key()` of
ANY of `{model_requested, model, model_echo, model_served}` of that record, or when the
`/api/v1/models` catalog maps `canonical_slug(permaslug) → id` between them. **Echo rule
(probes #12/#19, VERIFIED 2026-09-16):** OpenRouter echoes the **bare** slug in
`message.model` for a bare, a `:variant` and a `[1m]` request alike (`z-ai/glm-5.3-flash:exacto`
→ echo `z-ai/glm-5.3-flash`), and Claude Code's `modelUsage` keys / `assistant.message.model`
carry that bare echo; the dated permaslug (`z-ai/glm-5.3-flash-20260826`) appears **only** in
`openrouter_metadata.endpoints.available[].model` and `/generation`. The join therefore
strips variants/`[1m]` on the request side (`routing_key`) and expects a bare-slug echo; the
catalog permaslug map covers the `model_served` side and any future echo change. The
join-rule fixture is cut from the probe #12 capture (bare, `:exacto`, `[1m]` all → bare echo)
and extended in PR-6 with a `/generation` answer for a `fallbacks`-served judge request
(the agent path cannot send `fallbacks`; the direct half of #19 is VERIFIED). When
`fallbacks` are configured for a judge, attribution is by the response side (`model_echo`),
never the requested slug. Deterministic single-model fallback: if the ledger holds exactly one
routing key and `per_model_usage` has exactly one real key, they join regardless of string
equality. A key with no match keeps `cost_usd: null` (estimate preserved) and appends a
`cost_warnings` item naming the key and the candidates; ledger cost with no
`per_model_usage` key appends a warning too, so Σ per-model == `cost_usd` is asserted.

**Audit and attribution.** Provider attribution and cost come from the same
`/generation` answer, so a row is either fully attributed (`provider_name`, permaslug,
`total_cost`) or `backfill_failed`. For every `status: ok` agent/hook row under a pinned
routing key, reconcile joins (`provider` slug via the `/providers` catalog, permaslug) against
the `/endpoints` catalog to recover `quantization`/`endpoint_tag` (`/generation` carries
neither — quantization is pinned **indirectly**: the operator chooses providers whose
endpoint is the wanted quantization, and the audit confirms via the join; a provider that
serves the same permaslug at two quantizations is ambiguous, `quantization: null`,
`cost_warnings` names it, and the snapshot recorded at preflight is the tiebreak), then
marks `audit: compliant` when the tuple is in `pinned_set(catalog)` and `violation`
otherwise. `backfill_failed` rows are `unattributed`: counted in
`cost_coverage.requests_unattributed` and `routing.unattributed`, listed under
`providers["unknown"]`, **excluded** from `routing.violations` (never inferred as compliant
or violating). If `routing.violations` or `routing.unattributed` is non-empty: append a
`cost_warnings` entry and, under `policy: strict`, set `routing.degraded: true`
(eval-compare parity excludes the run by default). `cost_confidence` and the audit are
**independent**: a run can have exact cost and a failed audit (a served provider outside
`order`) or a clean audit with `low` confidence (backfill gaps on a shared key).

Loudness: `cost_source: unavailable` prints a stderr WARNING at write time, renders a
banner in the report, and `--strict-cost` makes execute.py exit 2. `cost_usd: null`
is already a first-class value for every reader (`_cost_label` prints `cost n/a`,
execute.py:748-751; `_sum_reported_costs` preserves `None`, :754-761; report `_fmt`
renders `—`, report.py:971-972; compare `get_metric` defaults, compare.py:126-128;
anova `_run_cost` returns `None` for non-numbers, analyze.py:330-335; MLflow logs only
truthy `cost_usd`, log_results.py:281) because the Cursor runner already emits it
(cursor_agent.py:203).

**Null-cost arithmetic.** Every cost reader follows one rule — *no reader re-inflates a `null` from the estimate and no reader crashes on
it*:

| Reader | Rule |
| --- | --- |
| `summary.total_cost_usd` (score.py `score_cases`) | `cost_usd + judge_cost_usd` **only when both addends are numeric**; otherwise `null`. `judge_cost_usd` and `hook_cost_usd` are always exposed separately, and `total_cost_source: "complete" \| "judge-only" \| "agent-only" \| "none"` says which addends were numeric. Never `cost_usd_estimate + judge_cost_usd`. |
| `agent_eval/judges/efficiency/cost_budget.py:9-11` | provenance-aware: reads `cost_source` from the case result; `unavailable`/missing cost → `(None, "cost unavailable (cost_source: unavailable) — abstained, not failed")`; score.py's builtin tuple normalisation (`_make_builtin_scorer`, :921-924) must pass `None` through as `value: None` **without** an `error`, which the aggregation (:2019-2024) already counts as skipped — neither FAIL nor `errored_cases`; `runner:estimate`/`harness:estimate` → judged, rationale suffixed `(estimate; not real spend)`; `openrouter:*` → judged as today. |
| `log_results.py:165-193 _harbor_step_run_result` | per-step cost is the **reconciled** per-step `cost_usd` produced by results.py's per-case ledger join (`step_id` in the ledger), labelled with `cost_source`; when the join yields `null` the step logs no cost and tags `cost_source`; the transcript `total_cost_usd` is logged only as `cost_usd_estimate`. |
| `trace_builder.py:612, :1040-1048` | distributes `per_model_usage[m].cost_usd` (reconciled) across spans; a `null` per-model cost sets no `mlflow.llm.cost` on that model's spans (never falls back to `cost_usd_estimate`), and the root span carries the `cost_source` tag. |
| `trace_from_stdout.py:157,167` | prints `cost n/a (est. $Y)` when the reconciled value is `null`, `$X (<cost_source>)` otherwise. |
| execute.py case aggregate (:1716-1770) and batch `_sum_reported_costs` (:754-761) | Σ over cases skips `null` and reports `cost_usd: null` when **any** case is `null` (a partial sum is not spend); `cost_coverage` says how many cases were priced. |

#### Direct transport contract

There is exactly one transport: the agent-under-test's Claude Code process sends its
Anthropic-format requests **straight to `https://openrouter.ai/api/v1/messages`**. The
harness owns nothing on the HTTP path; it owns (1) the env the agent starts with, (2) a
preflight against OpenRouter's public catalogs, (3) a post-hoc audit and cost backfill
over the gen ids the agent's own transcript exposes, and (4) optionally a per-run key. All
four are pure clients of `openrouter.ai`; none listens on a port.

**Env template (`ProviderPlan.agent_env()`, `agent_eval/providers/openrouter/plan.py`).**
Derived once per run from the effective role URIs, identical on every runner:

```
ANTHROPIC_BASE_URL=https://openrouter.ai/api        # Claude Code appends /v1/messages
ANTHROPIC_AUTH_TOKEN=<inference key>                # Bearer — operator key (audit) or per-run key (key-guardrail)
ANTHROPIC_API_KEY=                                  # blank: hygiene only — a non-empty value is sent as x-api-key and ALSO works (probe #13 VERIFIED)
CLAUDE_CODE_USE_VERTEX=  ANTHROPIC_VERTEX_PROJECT_ID=  CLOUD_ML_REGION=  GOOGLE_CLOUD_PROJECT=  CLAUDE_CODE_USE_BEDROCK=  AWS_REGION=  AWS_BEARER_TOKEN_BEDROCK=
                                                    # blanked (empty string, not unset) so a user-level settings.json cannot re-route the run
ANTHROPIC_MODEL=<skill id>                          # e.g. z-ai/glm-5.2:exacto — the :variant is the only in-request routing control
ANTHROPIC_DEFAULT_OPUS_MODEL=<skill id>  ANTHROPIC_DEFAULT_SONNET_MODEL=<skill id>  ANTHROPIC_DEFAULT_FABLE_MODEL=<skill id>
ANTHROPIC_DEFAULT_HAIKU_MODEL=<background_model or skill id>
CLAUDE_CODE_SUBAGENT_MODEL=<subagent id>            # models.subagent, defaults to the skill id
ANTHROPIC_CUSTOM_HEADERS=HTTP-Referer: <attribution.referer>\nX-OpenRouter-Title: <attribution.title>[\nx-eval-run-id: <run_id>]
                                                    # multi-header form VERIFIED (checklist row 27, CLI 2.1.273+): each line arrives as its own header on root and subagent requests → PR-5 sends Referer + Title (+ the optional run tag)
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1          # no telemetry/update calls from inside a metered container
```

The `--model` argv value stays the raw id (claude_code.py:286) and the aliases guarantee
every slot Claude Code picks by *name* (`opus`/`sonnet`/`haiku`/`fable`, subagents,
background/Haiku tasks) resolves to an OpenRouter id — otherwise a `claude-haiku-*` slug would
be sent to OpenRouter and 404. `count_tokens`: OpenRouter has no
`/v1/messages/count_tokens` (404, VERIFIED) and Claude Code 2.1.273+ **never calls it** — zero
calls in a 3-turn direct run with a tool call and a subagent (probe #22, RESOLVED for this CLI
version, `probes/probe_cli_report_2026-09-16.json`). The design does not plan around it; the
only watch item: re-run `specs/014-openrouter-provider/probes/probe_claude_cli.py` on CLI upgrades,
and if `count_tokens` appears, OpenRouter 404s it. The request path the CLI actually uses is
`/v1/messages?beta=true` — matchers strip the query string. **Beta headers (row 20):** Claude
Code 2.1.273+ sends `anthropic-beta: claude-code-20250219, interleaved-thinking-2025-05-14,
thinking-token-count-2026-05-13, context-management-2025-06-27,
prompt-caching-scope-2026-01-05, mid-conversation-system-2026-04-07,
mid-conversation-tool-changes-2026-07-01, effort-2025-11-2…` (list truncated in the evidence file); OpenRouter accepted this set
on a live non-Anthropic request (probe #20, run 1) — VERIFIED-tolerated, not something the
harness strips or rewrites.

Per runner, the template lands as follows:

- **Local `claude-code`** — merged into the per-run **settings overlay** `env`
  (`<ws>/.claude/.eval-overlay.json`, written 0600 next to the workspace `settings.json` by
  `_write_settings_overlay` and passed via `--settings`, removed in `finally`; PR-5 — a
  runtime file, unrelated to the `extends:` config overlay of PR-3a), which is applied last,
  so it wins over `execution.env` and `runner.settings.env`. Subagents and hook children inherit the
  settings env (probe #1 VERIFIED incl. children, CLI 2.1.273+: the subagent's request and the
  PreToolUse hook subprocess both carried the overlay's `ANTHROPIC_BASE_URL`/blanked Vertex
  vars; PR-5 e2e additionally asserts that a run with `--subagent-model` produces gen ids
  under the subagent id).
- **Harbor podman** — `harbor/run.py:216-233 _harbor_agent_env_args` already turns a
  resolved env dict into value-free `--agent-env KEY=${CARRIER}` argv with the value in
  the child process env; the plan's block is appended to `_resolve_harbor_agent_env`'s
  dict (run.py:196-210) **after** `execution.env`/`runner.env`, so it wins. Harbor merges
  `--agent-env` last into the agent's environment (`harbor 0.13.1 agents/base.py:288-291`,
  VERIFIED 0.13.1). The key is resolved on the host from `OPENROUTER_API_KEY` — the same
  exposure class as today's `ANTHROPIC_AUTH_TOKEN` forwarding (podman.py:36-49,
  `_FORWARD_ENV`; podman is documented as "no security boundary", podman.py:34-36).
  **While a plan is active the host Vertex/Bedrock vars must not be forwarded**: podman.py
  forwards `CLAUDE_CODE_USE_VERTEX`, `ANTHROPIC_VERTEX_PROJECT_ID`, `CLOUD_ML_REGION`
  (podman.py:36-49, applied at :258); the plan blanks them in `--agent-env`, which wins
  (base.py merge order), and `harbor/run.py` additionally drops them from the forwarded set
  when a plan is active so the container never sees a Vertex project id at all.
  `openrouter.ai` is reachable from the container as from the host — there is no
  host-reachability or `host.containers.internal` concern.
- **Harbor Kubernetes** — the credentials Secret named by `AGENT_EVAL_K8S_CREDENTIALS_SECRET`
  carries `OPENROUTER_API_KEY`; today it is attached whole via `envFrom.secretRef`
  (kubernetes.py:244-246). The harness adds one explicit `env[]` entry
  `ANTHROPIC_AUTH_TOKEN` ← `valueFrom.secretKeyRef {name: <secret>, key: OPENROUTER_API_KEY}`
  (explicit `env[]` wins over `envFrom` in the pod spec, so a stale `ANTHROPIC_AUTH_TOKEN` in
  the same Secret cannot leak through) and the plan's **non-secret** lines as plain
  `env[]` values. Host Vertex/Bedrock vars are already excluded on K8s (kubernetes.py:42-50);
  the plan's blank lines are still emitted so an in-image default cannot re-route. At
  `key-guardrail` the harness creates a per-run Secret (`agent-eval-<run_id>-openrouter`)
  holding the per-run key and deletes it with the key.
- **EvalHub** — same as K8s through the adapter's env pass-through (evalhub/runner.py:206
  loads eval.yaml raw and so sees the resolved `extends:` overlay, PR-3a).
- **Per-trial tagging** is not needed for cost (gen ids are per request and the transcript
  is per trial); the optional `x-eval-run-id` custom header exists only for the operator's
  OpenRouter activity page.

**Enforcement levels (`models.providers.openrouter.routing.enforcement`).**

- `audit` (default). Three steps, all outside the request path:
  1. **Preflight** (`agent_eval/providers/openrouter/preflight.py`, `preflight: strict | warn | off`):
     for every routing key with a `pinned_set`, fetch `GET /api/v1/models/{author}/{slug}/endpoints`
     and `GET /api/v1/providers` (public, no key needed) and check that each pinned provider
     (a) serves the model, (b) at a declared quantization when `quantizations` is set,
     (c) has `supports_tools` (agents) and, for judges carrying pins, `supports_tool_choice.function`
     (the probe #5 consequence: a forced `tool_choice` under pins 404s otherwise), (d) is not
     `status < 0` (deranked/offline). `variant` sanity: `:exacto` requires ≥ 1 pinned endpoint
     in the model's Exacto set (DOCUMENTED; warn only). Failures: `strict` aborts before any
     case (`ConfigError`, exit 2); `warn` continues with `routing.degraded_reason: preflight`.
     Output `<run_dir>/provider/routing_snapshot.json`, **PR-5 minimum form** `{ts, routing_sha, keys: {<routing key>:
     {pinned_set: [...], variant, catalog: {providers: [...], endpoints: [...]}}}}` (PR-6 adds
     `enforcement`, `key_scope`, `providers_map_sha`, per-key `eligible`/`excluded` and `pricing`
     — the full form is in the preflight.py section) — the frozen
     catalog view the audit joins against (so a catalog change *during* the run cannot
     retroactively change a verdict).
  2. **Request** carries only the model-suffix variant configured on the id. Nothing else is
     injected; `require_parameters` is dropped from the agent path (unsendable).
  3. **Audit** at reconcile: every `status: ok` agent/hook row is joined as under "Audit and
     attribution" → `routing.violations`, `routing_enforcement: audit`. Violations are
     **reported, never repaired** — the generation is billed and the case's output stands;
     `policy: strict` marks the run `degraded` (and `--strict-routing` fails it, exit 2),
     `warn` flags it. compare/anova refuse to pool runs whose `routing_sha` or audit outcome
     differ (Reconcile `routing`).
- `key-guardrail` (opt-in; requires `OPENROUTER_MANAGEMENT_KEY` in the harness process env).
  Everything in `audit`, plus at plan build (before preflight):
  1. `POST /api/v1/keys` with the management key → a per-run inference key named
     `agent-eval <run_id>`, `limit: <budget.run_usd>` (USD; required at this level —
     validation error when `budget.run_usd` is null), and the guardrail fields restricting
     allowed providers to the union of every routing key's pinned providers (**DOCUMENTED /
     UNVERIFIED — probe #26**: the exact field names, whether the allow-list is per key or per
     guardrail object, whether `limit` is a hard 402 or a soft flag, and how an account-level
     paid-training opt-in interacts with a key-level allow-list; PR-6 lands only after the probe
     answers, and the probe's JSON is committed under `probes/`).
  2. The per-run key is what `ANTHROPIC_AUTH_TOKEN` carries; the operator key never reaches
     the agent (`provider.key_scope: per-run`). Backfill (`/generation`) and `/key` are
     queried **with the per-run key** so the key-usage delta is exact by construction.
  3. **Revocation** `DELETE /api/v1/keys/{hash}` runs in the plan's `finally` on every exit
     path — normal end, `KeyboardInterrupt`, `SystemExit`, unhandled exception, and the
     `atexit` fallback — after the run-end backfill retry and the ≥ 20 s key-usage settle.
     A failed revoke is a stderr ERROR + `cost_warnings` entry naming the key hash and is
     retried by `agent-eval provider revoke <run_dir>`; the key's `limit` bounds the blast
     radius meanwhile. Server-side enforcement means a pin violation is *impossible* rather
     than *reported*: an unroutable request 404s at OpenRouter (the audit then shows zero
     violations and Claude Code's error output shows the 404), and a budget breach 402s.

**Account-level settings (apply to every key on the account; documented, not managed).**
The privacy/data-policy toggles (paid-model-training opt-in, ZDR-only, "ignored
providers") filter the endpoint set **server-side for the whole account**. This is what
made `deepseek/deepseek-v4.1-flash` unroutable on 2026-09-10 (404 body: the only endpoint
requires the paid-training opt-in). Preflight surfaces it: when a pinned endpoint is present
in the public catalog but the account probe (`GET /api/v1/models/{slug}/endpoints` with the
key — the filtered view) omits it, the error names the toggle. The harness never changes
account settings; the doc lists the three toggles and their effect on pins.

**Backfill and catalog client (`agent_eval/providers/openrouter/generation.py`, `catalog.py`).**
Stdlib **`urllib.request`** over the default SSL context (already `truststore`-injected by
`agent_eval/_bootstrap.py:147-148`): a client that issues a few hundred small
GETs with no streaming, no connection pooling requirement and no timeouts finer than a
per-call `timeout=` gains nothing from httpx, and this removes the `openrouter` extra /
`ensure_deps` question entirely (the judge path already brings the `openai` SDK; it is not
reused for these GETs to keep the judge client dedicated). Endpoints: `GET /api/v1/generation?id=`
(auth: the run's inference key), `GET /api/v1/key` (same key), `GET /api/v1/models/{a}/{s}/endpoints`,
`GET /api/v1/providers`, `GET /api/v1/models` (public), and at `key-guardrail`
`POST/DELETE /api/v1/keys` (management key). **Backoff per gen id** (VERIFIED lag 8–13 s):
first attempt 5 s after the id is sighted, then every 2 s while `/generation` answers 404
(not yet materialised), giving up at 60 s after first sighting → `backfill_failed`; 429
honours `Retry-After`, 5xx retries with the same cadence; 401/403 abort the whole backfill
with `error_class: config`. **Run-end retry**: one more pass over every `backfill_failed`
id after the last agent process exits, then the key-usage read at ≥ 20 s settle. At most
`min(8, execution.parallelism × 2)` concurrent backfill GETs, one process-wide worker.
Everything the client writes is a ledger row; it never touches `run_result.json` directly
(reconcile does).

**Budget mapping.** `execution.max_budget_usd` keeps its **per-invocation** contract
(execution.md:14,29; resolved per case/step at execute.py:493-495, 1289-1290): the CLI
receives `--max-budget-usd = cap × models.providers.openrouter.cli_budget_inflation` (default 50)
because Claude Code enforces it on its Anthropic-priced estimate (2–60× inflated); the
resolved value is recorded as `eval_params.budget.cli_cap_usd`. A resolved cap `≤ 0` or
`None` means **omit the flag** (no CLI cap; `cli_cap_usd: null`) — the CLI rejects
`--max-budget-usd 0` outright ("must be a positive number greater than 0", exit 1, no
request; probe #23 VERIFIED on 2.1.273+), so execute.py must never pass `0` through.
`models.providers.openrouter.budget.run_usd` is the
whole-run real-dollar pool: at `audit` it is enforced **post hoc only** (reconcile sets
`budget.exceeded: run`, `exceeded_reason: post-hoc`; `--strict-cost` makes execute.py
exit 2 when the backfilled Σ exceeds it — Known limitations); at `key-guardrail` it is the
per-run key's `limit` and OpenRouter refuses further requests server-side, in flight.
`budget.dedicated_key: bool` (default false) is the operator's assertion that nothing else
spends on `OPENROUTER_API_KEY` during the run (raises key-usage-only confidence to `medium`;
implied true at `key-guardrail`). No in-flight harness-side gate exists at `audit`.

**Secrets.** `OPENROUTER_API_KEY` and `OPENROUTER_MANAGEMENT_KEY` are read from the harness
process env only; a literal or `$VAR` reference to either in `execution.env`, `runner.env`
or `runner.settings.env` is a validation error (they would be baked into Harbor task
packages by interception.py:171-176 and into workspace settings). The inference key is
written only to the 0600 overlay (local) / the `--agent-env` carrier env (podman) / the
Secret (K8s), never to `run_result.json`, `eval_params`, the ledger, events or logs (`key_hash`
only). At `audit` the agent-under-test can read it from its own environment
(claude_code.py:712-733) — recorded as `provider.key_exposed_to_agent: true` and printed as
a one-line stderr notice at run start; `key-guardrail` bounds that exposure to a per-run
key with a `limit`.

**Resilience.** The harness owns no retries on the agent's request path: Claude Code's own
retry/backoff on 429/5xx (whether it honours `Retry-After` — UNVERIFIED, probe #24) and
OpenRouter's server-side fallbacks (whatever the variant and the account settings allow)
are the only in-flight resilience. The harness owns backoff for its own clients (above)
and classifies the agent's error output per `error_class` so infra vs config vs agent
failures stay distinguishable in `run_result.json`. There is no cooldown, widening or
first-byte watchdog: a slow pinned provider is bounded by the case/step timeout only.

### `agent_eval/prompt_backends.py`

- `resolve_judge_backend(model)` (:61-100) keeps its 2-tuple contract and its three
  transport values `anthropic | openai | runner`: add, before the generic
  unsupported-provider branch (:86-91), `if provider == "openrouter": if not bare: raise ValueError("openrouter judge model needs '<author>/<slug>', e.g. 'openrouter:/z-ai/glm-5.2'"); return ("openai", bare)`.
  Unknown provider prefixes are rejected exactly as today — no special case: they keep
  falling into the generic unsupported-provider branch, whose ValueError text names
  `openrouter:/…` as the supported non-builtin form. Update the docstring (:61-75) and the
  ValueError text (:86-91) accordingly. Every existing return value is unchanged; bare
  `vendor/model` ids still route to `openai` (spec 013 contract, tests/test_prompt_backends.py:160-162).
- New sibling `resolve_judge_client(model, providers) -> JudgeClientConfig | None`:
  `None` for today's paths; for `openrouter:/…` a frozen
  `JudgeClientConfig(name="openrouter", base_url, api_key_env, default_headers, extra_body: dict, token_param="max_tokens", max_retries, timeout_s, concurrency)`
  built from `models.providers.openrouter` (`extra_body` is the static operator dict copied from
  `JudgeClientOptions.extra_body`; there is no `extra_body_fn` — the per-model routing part
  is computed at the call site by `routing.to_chat_extra_body(...)` and merged as
  `routing | cfg.extra_body`, see score.py). The provider name is recoverable from
  `split_model_uri`; the four score.py dispatch sites stay three-way and pass the client
  config through. Config-load validation (config.py:1742) already has `config` in scope.
- `run_prompt_via_runner` (:187-237) is unchanged: it builds its runner from `config`
  without a provider plan, so a `runner:/` judge runs Claude Code with the ambient env
  (Anthropic/Vertex), never with the OpenRouter overlay. See config validation for `agent:` judges.

### `agent_eval/config.py`

- `ModelsConfig` (:659-672) gains one field, `providers: ProvidersConfig`, parsed from the
  `models.providers` mapping (Decision 17); new dataclasses next to it:

  ```
  ModelsConfig(skill, subagent, judge, hook,                # existing role URIs (:659-672)
               providers=ProvidersConfig(openrouter=None))  # NEW: the provider registry lives under models
  ProvidersConfig(openrouter: OpenRouterConfig | None)      # one declared kind (Decision 1)
  OpenRouterConfig(
      kind="openrouter",
      api_key_env="OPENROUTER_API_KEY", base_url="https://openrouter.ai/api",   # $VAR allowed; the ONLY base-URL knob; no /v1 — Claude Code appends /v1/messages
      management_key_env="OPENROUTER_MANAGEMENT_KEY",   # read only at routing.enforcement: key-guardrail; env-only like api_key_env
      attribution=Attribution(referer=None, title="agent-eval-harness", run_id_header=False),   # run_id_header adds x-eval-run-id (optional; activity-page tagging only)
      background_model=None,                  # haiku-slot model; None = model under test
      preflight="strict",                     # strict | warn | off  (catalog *fetch* failure degrades to warn with reason; key/slug/eligibility checks stay strict)
      cli_budget_inflation=50,                # multiplier applied to execution.max_budget_usd before it becomes --max-budget-usd (per-invocation contract kept; Budget mapping)
      budget=BudgetOptions(run_usd=None,      # whole-run real-dollar pool: post hoc at audit (--strict-cost), limit_usd of the per-run key at key-guardrail (required there)
                           dedicated_key=False),   # operator assertion that nothing else spends on OPENROUTER_API_KEY during the run (key-usage confidence; implied at key-guardrail)
      routing=RoutingConfig(defaults=RoutingSpec(allow_fallbacks=True),   # require_parameters is unset by default: unsendable on the agent path, judge-only per Decision 25
                            models={}, policy="strict",                    # strict | warn — what a failed audit does to the run (degraded vs flagged)
                            enforcement="audit",                           # audit (default) | key-guardrail (opt-in; needs management_key_env)
                            guardrail=GuardrailOptions(key_name="agent-eval {run_id}",
                                                       providers="pinned",       # pinned (union of every routing key's pinned providers) | explicit list
                                                       revoke_on_exit=True,      # always true in this release; field reserved for a keep-for-debug mode
                                                       settle_s=20)),            # ≥ 20 s key-usage settle before the run-end read and the revoke (VERIFIED settle)
      judge=JudgeClientOptions(concurrency=4, max_retries=3, timeout_s=300, extra_body={},
                               inherit_pins=False))   # Decision 25: judge pins are opt-in; False → judges send no order/only/quantizations and no require_parameters
  ```

  Deliberately absent (a config carrying any of them fails validation with a pointer to
  Decision 1, like any other unknown sub-key): transport mode/options keys — there is one
  transport; a `generation_backfill` toggle — the backfill is always on, it *is* the cost
  source; a key-exposure acknowledgement — the operator key reaching the agent at `audit`
  is the documented default, printed at run start, and `key-guardrail` is the opt-in that
  removes it; `budget.max_unpriced`/`max_unpriced_ratio` — there is no in-flight gate to
  trip, unpriced requests are reported through `cost_coverage`; and a second provider-kind
  dataclass.

  `ModelsConfig.providers` is parsed in `from_yaml` inside the models block (:1195-1203); a
  top-level `providers:` key is rejected with "moved: declare providers under
  `models.providers` (spec 014 Decision 17)". Every reader addresses it as
  `config.models.providers` (never `config.providers`).
  `JudgeConfig` (:770-832) gains `provider_options: dict`, kept opaque in `JudgeConfig` and
  validated by the judge's provider kind (`validate_judge_options(dict)` — for `openrouter`:
  `routing`, `fallbacks`, `max_tokens`; precedence `provider_options.max_tokens` > call-site
  default) so the schema lives with the provider — the URI stays the contract, as
  spec 013 required.
- **`extends:` overlay via one raw loader.** `extends:` is a **new**, optional
  top-level eval-config key introduced by this spec (PR-3a), not an existing harness
  feature; it is independent of the OpenRouter feature and can be dropped without touching
  the transport (its motivation is the `eval.yaml` / `eval-openrouter.yaml` drift; see the
  Contracts subsection "Config overlay"). `agent_eval.config.load_raw(path) -> tuple[dict, list[str]]`
  is the *only* place that resolves `extends: <relative path>` (against the file's own
  directory, recursively, cycle detection, depth ≤ 8); it returns the merged mapping with
  the `extends` key removed plus the resolved chain. `from_yaml` (:1108-1115) becomes a thin
  wrapper that parses the merged mapping and sets `config.config_chain: list[str]`
  (surfaced in `eval_params.config_chain`). Every other raw reader calls `load_raw`:
  harbor/tasks.py `_bundle_eval_config` (:99), harbor/run.py `_write_report` (:452),
  evalhub/runner.py (:206), skills/eval-run/scripts/report.py `_load_yaml` (:143-147, used at
  :2978 — the "standalone" comment is moot since :2974 already imports `EvalConfig`),
  skills/eval-analyze/scripts/validate_eval.py (:447 keeps its syntax-only `safe_load` for the
  friendly YAML error, then runs the structural checks :483-491 on the merged dict and prints
  the chain), `discover_configs` (:1798), anova/matrix.py (:42), reorganize.py (:45).
  The invariant is "every config reader goes through the merged loader", pinned by
  `tests/test_config_raw_readers.py` (below). `python3 -m agent_eval.config --print <path>`
  dumps the merged YAML with per-list provenance comments (`# from: eval.yaml` /
  `# from: eval-profiles/x.yaml`). No CLI flag exists to forget at score/report time.
- **Merge policy** — one implementation, shared with `runner.settings`:
  workspace.py `_deep_merge` (:709-718) moves to `agent_eval.config.deep_merge(dst, src, *, dedupe)`
  and workspace.py imports it (`dedupe=False` there, so `runner.settings` behaviour is
  byte-identical to today). For `extends:` (`dedupe=True`): dicts merge; scalars override;
  lists of scalars **extend with dedupe**, base first, order preserved (`permissions.allow/deny`,
  `traces.events`, `plugin_dirs`); lists of mappings whose entries all carry `name` (`judges`)
  or `id` (`execution.steps`) **merge by key** — a same-keyed entry deep-merges over the base,
  new keys append; other lists of mappings (`hooks.*` entries have no name, config.py:389-397)
  extend with dedupe by equality. A `!replace` YAML tag (small SafeLoader constructor) forces
  whole-value replacement for the rare case (removing a judge or an allow rule requires it);
  `--print` shows it. Cross-reference runner.md:131 so readers see the two surfaces agree.
  This contrasts deliberately with `RoutingSpec` merge (lists replace) — documented at both
  places.
- **Path resolution under `extends:`.** Only `extends:` itself resolves against the profile
  file's directory. `config_path`/`config_dir` are taken from the **root of the chain** (the
  base eval.yaml), so `dataset.path` (config.py:1030-1043, execute.py:1053, score.py:220,
  report.py:2979-2983) resolves exactly as today; `prompt_file`/`plugin_dirs` resolve against
  `project_root` = CWD (config.py `project_root`) and are unchanged. `eval_name()` therefore
  derives from the base, not the profile stem. Tested with `eval-profiles/x.yaml` + base
  `dataset.path: eval/dataset/cases`.
- `discover_configs` (:1776-1830) gains `include_profiles: bool = False`: a file whose raw
  mapping has `extends:` is a profile, not a standalone eval — skipped by default (it must not
  register as an eval named after its stem), returned with `DiscoveryResult.profile_of=<root>`
  when requested. The scan set gains `eval-profiles/*.yaml` and `eval/profiles/*.yaml`.
- Import-cycle note (:1740-1742) is honoured: `agent_eval/providers/` imports neither
  `agent_eval.agent` nor `config`; config.py imports `agent_eval.providers.base` eagerly
  (pure data) and `prompt_backends` lazily as today.

### `agent_eval/providers/` (new; provider-neutral core + one subpackage per kind)

Layout and the one-way dependency rule: `providers/` top level imports **nothing** from
a kind subpackage; kind subpackages import upward only; `agent/`, `config`, `harbor` and the
eval-run scripts import providers. There is no proxy package (Decision 1) and nothing in
`providers/` opens a listening socket: every module below is a pure client
of `openrouter.ai` or a pure function over files in `<run_dir>/provider/`.

- `base.py`: `AgentModel(provider, slug, variants, key)`, `parse_agent_model(uri) -> AgentModel`
  (reuses `split_model_uri`), `routing_key(id)`, `ProviderKind` enum, `ErrorClass` enum,
  `RoutingTableProtocol`, `ProviderPlan` (frozen: kind, `transport="direct"`, base URL,
  `key_scope: "operator" | "per-run"`, the inference key (never repr'd — `__repr__` prints
  `key_hash`), aliases, opaque `routing: RoutingTableProtocol`, `enforcement`, run id,
  attribution, `cli_budget_inflation`, `budget_run_usd`) with `agent_env()` (the Direct
  transport contract template) and `close()` (the `finally` hook: run-end backfill retry →
  key-usage settle/read → per-run key revoke at `key-guardrail`).
- `env.py`: the **single** env template function
  `settings_env_block(plan, *, secrets: "ref"|"literal"|"omit", target: "overlay"|"harbor_carrier"|"k8s_pod" = "overlay") -> dict[str, str|None]`
  consumed by every writer (claude_code overlay, harbor `--agent-env` carriers, the K8s pod
  manifest, tests), and the static
  `MANAGED_ENV_KEYS = frozenset({ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_API_KEY, ANTHROPIC_MODEL, ANTHROPIC_VERTEX_PROJECT_ID, CLOUD_ML_REGION, GOOGLE_CLOUD_PROJECT, CLAUDE_CODE_USE_VERTEX, CLAUDE_CODE_USE_BEDROCK, AWS_REGION, AWS_BEARER_TOKEN_BEDROCK, ANTHROPIC_DEFAULT_OPUS_MODEL, ANTHROPIC_DEFAULT_SONNET_MODEL, ANTHROPIC_DEFAULT_HAIKU_MODEL, ANTHROPIC_DEFAULT_FABLE_MODEL, CLAUDE_CODE_SUBAGENT_MODEL, ANTHROPIC_CUSTOM_HEADERS, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC})`
  — the union of every key the template can emit, defined once and reused by
  `claude_code._build_env`, `_write_settings_overlay`, harbor `run.py` (forwarded-set scrub
  and carrier ordering), `podman.py`/`kubernetes.py` (forwarding exclusions under a plan),
  `interception.py` (skip list) and Config validation, so the writers cannot drift.
  It is the Direct transport contract's env template, one target at a time:

  | key | value |
  | --- | --- |
  | `ANTHROPIC_BASE_URL` | `models.providers.openrouter.base_url` (default `https://openrouter.ai/api`, no `/v1` — VERIFIED) |
  | `ANTHROPIC_AUTH_TOKEN` | the run's inference key: `secrets="ref"` → `$OPENROUTER_API_KEY` (resolved by the writer's existing `$VAR` logic, never a literal in config/argv); `"literal"` → the value (0600 overlay only); `"omit"` → key absent (K8s pod: supplied via `secretKeyRef`). At `key-guardrail` the per-run key replaces the operator key on every target (`literal` in the overlay/carrier env, per-run Secret on K8s). |
  | `ANTHROPIC_API_KEY` | `""` (explicitly empty as **hygiene**: a non-empty value is sent as `x-api-key`, and a valid key sent that way WORKS on `/messages` — probe #13 VERIFIED 2026-09-16 — so the blank exists to keep a stale host Anthropic key or cached-OAuth state from reaching OpenRouter, not for correctness; Config validation warns, never errors, on a non-empty value) |
  | `CLAUDE_CODE_USE_VERTEX`, `ANTHROPIC_VERTEX_PROJECT_ID`, `CLOUD_ML_REGION`, `GOOGLE_CLOUD_PROJECT`, `CLAUDE_CODE_USE_BEDROCK`, `AWS_REGION`, `AWS_BEARER_TOKEN_BEDROCK` | `""` (blank, not unset — an empty string beats a user-level settings.json and a podman-forwarded host value alike) |
  | `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_FABLE_MODEL` | skill `slug:variants` (the `:variant` is the only in-request routing control) |
  | `ANTHROPIC_DEFAULT_HAIKU_MODEL` | `background_model` if set, else skill `slug:variants` |
  | `CLAUDE_CODE_SUBAGENT_MODEL` | subagent `slug:variants` (same value `_build_env` forces, claude_code.py:727-728) |
  | `ANTHROPIC_CUSTOM_HEADERS` | `HTTP-Referer: <attribution.referer>\nX-OpenRouter-Title: <attribution.title>` when attribution is set, plus `\nx-eval-run-id: <run_id>` when `run_id_header: true` — one `Header: value` per line; the CLI sends each line as its own header on root and subagent requests (checklist row 27 VERIFIED, PR-5); absent when nothing is set |
  | `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | `"1"` |

  `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY` is not set. Cost truth is per request on
  every target: the `gen-…` ids in Claude Code's stream-json (`RunResult.message_ids`,
  probe #12 VERIFIED 2026-09-16; the trial transcript on Harbor) are backfilled from
  `/generation` by `generation.py`, and the `/key` delta is the cross-check (Decision 1).
  **Targets:** `overlay` = the full block with literal secrets; `harbor_carrier` = the
  same block with `secrets="ref"` (the value travels in the harbor child env under a carrier
  name, harbor/run.py:216-233); `k8s_pod` = the same block with `secrets="omit"` (the key
  arrives via `secretKeyRef`). **Nothing is baked into a Harbor task package**: the plan's
  keys are host/run-specific (which key, which model, whether a per-run key is in play), so
  `interception.py` skips every `MANAGED_ENV_KEYS` entry and task packages stay reusable
  across runs and keys. Invariant, tested per target: the three targets emit the same key
  set except `ANTHROPIC_AUTH_TOKEN`, which the `k8s_pod` target omits, so the design never
  depends on Claude Code's settings-env vs process-env precedence inside a container (Harbor
  merges `--agent-env` last, `harbor 0.13.1 agents/base.py:288-291`, VERIFIED; the K8s pod
  `env[]` wins over `envFrom`). The **alias rule** settles the haiku-slot
  drift in rfe-creator's current setup (eval-openrouter.yaml:30 vs its proxy README): the haiku slot
  defaults to the model under test because subagents declared `model: haiku` and utility
  calls are part of what is being evaluated, and a second model would silently appear in
  `per_model_usage`; a cheap `background_model` is opt-in and is recorded in
  `eval_params.provider.background_model`.
- `ledger.py`: writer/reader for the schema above (thread-safe append; `read(run_id, case_id=None, step_id=None, role=None)`).
- `reconcile.py`: the pure function above plus `routing_audit(ledger, snapshot)`; imports
  neither `agent_eval.agent` nor `config` and is invoked on every `run_result.json` write
  regardless of provider.
- `http.py`: the one thin HTTP helper every OpenRouter client module uses —
  `get_json(url, *, key=None, timeout=15)`, `post_json(...)`, `delete(...)` over stdlib
  **`urllib.request`** (see "Backfill and catalog client": no new dependency, no
  `ensure_deps` rule, no extra; the default SSL context is already `truststore`-injected by
  `agent_eval/_bootstrap.py:147-148`). Raises `OpenRouterHTTPError(status, error_type,
  retry_after)` and never logs the request headers.
- `openrouter/` (OpenRouter API knowledge):
  - `plan.py`: `build_plan(config, roles, *, runner: "claude-code"|"harbor-podman"|"harbor-k8s"|"evalhub", run_id) -> ProviderPlan`.
    There is **one transport on every runner** (Decision 1), so the runner argument selects
    only the env *target* (`overlay` / `harbor_carrier` / `k8s_pod`) and the `provider.runner`
    label; nothing about routing, cost or budget differs per runner. At `enforcement:
    key-guardrail` `build_plan` calls `keys.provision()` first (below) and the plan carries
    the per-run key (`key_scope: per-run`); at `audit` it carries `$OPENROUTER_API_KEY`
    (`key_scope: operator`). `plan.close()` is the single `finally` every host wraps around
    the run (execute.py, harbor/run.py).
  - `catalog.py`: cached public GETs (`/api/v1/providers` display↔slug map;
    `/api/v1/models` incl. `canonical_slug`; `/api/v1/models/{author}/{slug}/endpoints` —
    `provider_name`, `tag`, `quantization`, `status`, `max_completion_tokens`,
    `supported_parameters`, `supports_tool_choice`, `pricing`, `uptime_last_30m`; all VERIFIED
    public); with an inference key also `/api/v1/models/user` (account privacy/guardrail
    filtering). Exposes `ModelCatalog` for the reconcile join and `pricing_for(slug, provider)`
    for the budget estimator.
  - `routing.py`: `RoutingSpec`, `RoutingTable.for_model(key, role, overrides)` (implements
    `RoutingTableProtocol`), slug normalisation, `routing_sha`.
  - `preflight.py`: runs before any spend (execute.py after config load; harbor/run.py before
    task generation; `python3 -m agent_eval.providers.openrouter.preflight --config eval.yaml`).
    Applies to every `openrouter:/` slug in the effective roles — there is no other
    provider kind. It is the **only place the declared pins are checked before spend** on
    the agent path (nothing checks them in flight; Decision 1), so it is in the PR-5 MVP
    in its minimum form (slug exists, pinned providers serve the model, key valid (`GET /key`), `/models/user` eligibility (paid-training case → strict FAIL / warn), catalog-failure degrade, `routing_snapshot.json` written) and completed in PR-6. Per slug
    (agent, subagent, background, every openrouter judge): slug exists; each pinned provider
    has an endpoint; `quantizations` intersect; **`tool_choice` routability (probe #5,
    2026-09-16)**: for every pinned endpoint (`order`/`only`) of an agent slug,
    `supported_parameters ⊇ {tools, tool_choice}` and `supports_tool_choice.auto` (Claude
    Code sends `auto`) else FAIL under `strict`; for every pinned endpoint of a **judge**
    slug additionally `supports_tool_choice.function` (the structured-judge call forces a
    named function) else the judge falls back per score.py's policy (`required` → `auto` +
    strict parse) and preflight WARNs naming the endpoint — a pinned set with **no**
    endpoint supporting the judge's forcing mode is a FAIL, because OpenRouter answers it
    with 404 "No endpoints found" regardless of `require_parameters`; the public
    `/endpoints` API exposes `supports_tool_choice{none, auto, required, function}` per
    endpoint; `max_completion_tokens ≥ 32000` (Claude Code requests 32k) else WARN;
    `status < 0` = **degraded** (observed values `{0, -2}`, probe #18): excluded from the
    eligible set under `strict` (FAIL if nothing eligible remains), WARN under `warn`;
    key valid (`GET /api/v1/key`, value never logged); `/models/user` excludes the slug ⇒
    FAIL with OpenRouter's own `ineligibility_reasons` (this is what would have caught the
    2026-09-10 `paid-model-training-violation-by-account` 404 before spending — VERIFIED body;
    the account-level toggles are named in the message, "Account-level settings"); at
    `key-guardrail`, the per-run key is what `/key` and `/models/user` are queried with, so
    the eligibility view is the one the agent will actually get. A catalog *fetch* failure
    (network) degrades the run to `warn` with the reason rather than failing it; key
    validity, slug existence and eligibility remain strict under `strict`. Writes
    `<run_dir>/provider/routing_snapshot.json`, **full PR-6 form** (PR-5 writes only the
    `{ts, routing_sha, keys: {variant, pinned_set, catalog}}` subset, Direct transport
    contract) = `{ts, routing_sha, enforcement, key_scope,
    providers_map_sha, keys: {<routing key>: {variant, pinned_set: [(provider, permaslug,
    quantization)], eligible: [...], excluded: [{tag, reasons}], catalog: {providers: [...],
    endpoints: [...]}}}, pricing}` — the frozen catalog view `audit.py` joins against.
    `strict` fails the run; `warn` prints and continues; `off` skips (no snapshot → the audit
    joins against a catalog fetched at reconcile time and marks `routing.audit_complete`
    with `snapshot: null`).
  - `errors.py`: canonical `error_type` → `ErrorClass`: `infra` (`provider_unavailable`,
    `provider_overloaded`, `rate_limit_exceeded`, `timeout`, `server`, `unmapped`),
    `config` (`authentication`, `permission_denied`, `payment_required` — incl. the
    per-run key's `limit_usd` 402 at `key-guardrail`, mapped to `budget.exceeded_reason:
    limit_usd` — `not_found` incl. the **routing 404** "No endpoints found for <slug>"
    produced by pins + an unsupported request shape such as forced `tool_choice` (probe #5,
    2026-09-16), tagged `error_message: "routing: …"`, and the guardrail/privacy 404s,
    `precondition_failed`), `agent` (`context_length_exceeded`, `max_tokens_exceeded`,
    `invalid_request`, `content_policy_violation`, `refusal`, image errors). Applied to two
    inputs: the harness's own client errors (backfill, catalog, keys, judge client) and the
    `error` events Claude Code prints in stream-json (`stream_capture.py`), which is the
    only place the harness sees an agent-path request fail. Feeds `infra_errors`
    (harbor/run.py:656-657 already carries the field) and eval-anova exclusion.
  - `generation.py` (**the** cost and attribution source on the agent path):
    `Backfill(plan, ledger)` is one process-wide worker (daemon thread, bounded to
    `min(8, execution.parallelism × 2)` concurrent GETs) fed with `(gen_id, case_id,
    step_id, role, message_index, model_requested, model_echo)` tuples **as ids are
    sighted** — by `stream_capture.py`'s per-message hook locally, by `results.py`'s
    transcript parse on Harbor — and drained by `plan.close()` in the host's `finally`.
    Each id → one ledger row via `GET /api/v1/generation?id=` (`total_cost`, `provider_name`
    — a display name, mapped to the slug via the catalog — the dated permaslug `model`,
    native tokens, `finish_reason`/`native_finish_reason`, `latency`/`generation_time` —
    VERIFIED). **Backoff (probe #6, 2026-09-16: the first `/generation` 200 arrives
    7.6–12.7 s after `message_stop`, never sooner):** per id, first poll at `first_poll_s`
    (default **5 s** after sighting), then every `poll_s` (default **2 s**) on 404,
    honouring `Retry-After` on 429, 5xx on the same cadence, up to `give_up_s` (default
    **60 s**) → `status: backfill_failed` (id kept in the row); 401/403 abort the worker with
    `error_class: config`. **Run-end retry**: `close()` makes one more pass over every
    `backfill_failed` id after the last agent process exits, so a slow generation is not
    lost but never blocks a case; `agent-eval provider backfill <run_dir>` repeats it
    offline (readers re-reconcile). Authenticated with the run's inference key (the per-run
    key at `key-guardrail`, so the row set and the key's usage describe the same spend).
    `key_usage_delta(before, after)` from `GET /api/v1/key` (`usage` USD; read once at plan
    build and once at `close()` after a ≥ 20 s settle, polled until stable for two polls,
    **settle max 60 s** — probe #7 observed the update 20.3 s after a request). A delta that
    is zero or smaller than ledgered requests × the minimum endpoint price is inconsistent →
    `cost_warnings` rather than a small positive number. The generation backfill over the
    stream-json `message_ids` is the primary source (`cost_source: openrouter:generation`,
    probe #12) and the key delta is compared against Σ generation cost (> 5 % → warning);
    only when generation coverage < 0.8 does the key delta become the source
    (`openrouter:key-usage`, `cost_confidence` per Reconcile), and only when both fail is
    `cost_source: unavailable` written.
  - `keys.py` (management API; `enforcement: key-guardrail` only, PR-6):
    `provision(management_key, *, name, limit_usd, allowed_providers) -> ProvisionedKey(key, hash, created_at)`
    via `POST /api/v1/keys`, `revoke(management_key, hash)` via `DELETE /api/v1/keys/{hash}`,
    `verify_guardrail(management_key, hash) -> dict` via `GET /api/v1/keys/{hash}` (the
    read-back that the preflight compares with what was requested). **Field semantics are
    DOCUMENTED / UNVERIFIED (probe #26):** the exact guardrail field names, whether the
    allow-list is per key or a referenced guardrail object, whether `limit` produces a hard
    402 or a soft flag, and how an account-level paid-training opt-in interacts with a
    key-level allow-list; the module's request builder is a single function so the probe's
    answer changes one place. The management key is read from `management_key_env` at plan
    build, used for exactly these three calls, and never placed in any env target, the
    ledger or the snapshot; `ProvisionedKey.__repr__` prints the hash only. Revocation runs
    in `plan.close()` on every exit path (normal, `KeyboardInterrupt`, `SystemExit`,
    unhandled exception, `atexit` fallback) after the run-end backfill retry and the
    key-usage settle; a failed revoke is a stderr ERROR + `cost_warnings` entry naming the
    hash, retried by `agent-eval provider revoke <run_dir>` (the hash is persisted in
    `<run_dir>/provider/key.json` — hash, name, `limit_usd`, `created_at`, `revoked_at`;
    never the key).
  - `audit.py`: `routing_audit(ledger_rows, snapshot, catalog) -> RoutingAudit` — the pure
    join under "Audit and attribution" (`(provider slug, permaslug) → (quantization,
    endpoint_tag)` via the snapshot's frozen `/endpoints` view, membership in `pinned_set`
    → `compliant | violation`, `backfill_failed` → `unattributed`), returning the Reconcile
    `routing` dict. Called by `reconcile()`; also runnable offline (`agent-eval provider
    audit <run_dir>`) and by eval-compare's snapshot diff. It never repairs anything: a
    violation is a billed, kept generation whose provider was outside the declared set.

### `agent_eval/agent/base.py`

`RunResult` (:15-30) gains, all defaulted to `None`: `cost_source`, `cost_usd_estimate`,
`providers`, `message_ids: list[str]` (Claude assistant message ids, used for turn
accounting) and `generation_ids: list[str]` (top-level OpenRouter `requestId` values,
the **cost-truth key set**),
`error_class` (from the runner's own error output, `errors.py`), `budget` (dict as in
Reconcile). `execute()`'s signature
(:56-67) is unchanged: the plan is a constructor argument, not a per-call one.

### `agent_eval/agent/stream_capture.py`

`extract_usage` (:65-160) already collects `seen_msg_ids` (:105-112); expose them on
`RunResult.message_ids` (root stream) merged with ids from `count_subagent_turns`
(:165) transcripts. Current OpenRouter transcripts carry the generation id in the
assistant event's top-level `requestId` (`gen-…`) and Claude's `message.id` separately;
collect both, keep the latter for turn counting, and hand every `requestId` to
the `generation.py` worker **as the stream is read** (a per-message callback the
runner installs when a plan is active; `extract_usage` itself stays a pure post-hoc
function for callers without a plan) — not only at run end — and each id yields one
ledger record `source: generation`, `role: agent` (or `hook` for the ids the same collector drains from the
case's `$AGENT_EVAL_HOOK_IDS` JSONL after the root stream ends — the hook's own response
ids, tools.py), with `message_index`, `model_echo` = `assistant.message.model` (bare
slug) and `model_served` = `/generation` permaslug. Ids that are not `gen-…`
(Anthropic-direct, Vertex, an operator-run Anthropic-compatible endpoint through plain
`execution.env`) are kept for the estimate path only. The same pass classifies Claude Code's `error` events with
`errors.py` (`RunResult.error_class`) and detects the `key-guardrail` 402 (`budget.exceeded_reason:
limit_usd`) — the only visibility the harness has into a failed agent-path request, since it
is not on the HTTP path. Tag nothing else — the estimate stays what it is (and is known to
be far off for non-Anthropic models: probe #12 measured Claude Code's `total_cost_usd` at
~60× the real `/generation` cost). Harbor's `results.py` reuses `extract_usage` on the
trial transcript, so the id collection is one implementation.

### `agent_eval/agent/claude_code.py`

- `from_config` (:199-218) / `__init__` (:220-255): accept `provider_plan=` and `run_id=`
  overrides (execute.py passes both; `run_prompt_via_runner` passes neither).
- Settings overlay: generalise the `.eval-permissions.json` block (:337-395) into
  `_write_settings_overlay(workspace, settings_path, deny, allow)` that fires when a
  plan is active **or** path-based permissions exist; it loads the workspace
  `settings.json` (:350-355) and sets
  `settings["env"] = {**existing_env, **settings_env_block(plan, secrets="literal")}` —
  **the plan wins for managed keys** (`None` entries are dropped, non-managed
  keys from `existing_env` untouched), writes `<ws>/.claude/.eval-overlay.json` (0600) and
  passes it via `--settings` (:394-395). Ownership therefore holds by construction for every
  writer the validator does not see (a pre-existing repo-mode `.claude/settings.json` env,
  `runner.settings.env` merged by workspace.py:757); the load-time check is a diagnostic.
  The overlay is the one layer that beats a user `~/.claude/settings.json` with
  `CLAUDE_CODE_USE_VERTEX=1` (asserted by rfe-creator's incident; probe #1).
  The overlay is written with the plan's **literal** inference key (`secrets="literal"`),
  which is why it is 0600 and why its removal moves into a `finally:` around the Popen
  block (today's unlinks sit only on the timeout/normal paths, :518-523, :622-627; the
  `finally:` also covers `KeyboardInterrupt` and post-processing exceptions). There is no
  per-case token and nothing to register or revoke per case: the key is per run (operator
  key at `audit`, per-run key at `key-guardrail`) and its lifecycle belongs to `plan.close()`.
- `cmd` (:283-297): `--model` receives the bare `slug:variants`; `--max-budget-usd` is
  `max_budget_usd × plan.cli_budget_inflation` (the CLI enforces it on its
  Anthropic-priced estimate, so the multiplier keeps the per-invocation cap from killing an
  OpenRouter run early) — no sentinel; a resolved cap `≤ 0`/`None` **omits the flag** (the
  CLI rejects `--max-budget-usd 0`, probe #23); the effective CLI cap is recorded in
  `eval_params.budget.cli_cap_usd` (`null` when omitted). It is the **only in-flight cap at `audit`** (Known
  limitations). `--effort` is passed through untouched (:296-297).
- `_build_env` (:712-733): when a plan is active, **remove every `MANAGED_ENV_KEYS` entry
  from the ambient `_SAFE_ENV_KEYS` copy** (:714) before applying `self._env`/`extra_env`
  — routing does not depend on a settings-env empty string overriding a non-empty
  process value, and the host's real `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` can never
  reach the CLI, its subagents or hook children (probe #1 becomes a defence-in-depth check,
  not a secrecy gate). `GOOGLE_*`/`CLOUDSDK_*` stay forwarded (only a Vertex hook would use
  them, and a Vertex hook is rejected at load when a plan is active — justified by probe
  #10: the hook subprocess demonstrably inherits the overlay env). Without a plan, forwarding is
  unchanged (`_SAFE_ENV_KEYS` itself is untouched; tests/test_extract_progress.py:397-404 passes).
  Hook children and subagents get the same direct env as the
  CLI by inheritance (the overlay `env` is process env for everything Claude Code spawns —
  probe #1 VERIFIED incl. subagent and hook children on 2.1.273+, kept green by
  `test_claude_cli_direct`). `OPENROUTER_API_KEY` and
  `OPENROUTER_MANAGEMENT_KEY` are never added to `_SAFE_ENV_KEYS`: the agent sees the
  inference key only as `ANTHROPIC_AUTH_TOKEN`, and never sees the management key. When a
  plan is active `_build_env` also exports `AGENT_EVAL_HOOK_IDS=<run_dir>/provider/hook-ids-<case>.jsonl`,
  the per-case file a Claude Code hook appends its response `gen-…` ids to (tools.py).
- Result construction (:527-538 timeout path, :633-645): populate `message_ids`,
  `cost_usd_estimate = cost_usd` and `cost_source = "runner:reported"` — or
  `"runner:estimate"` when the runner's effective env has an `ANTHROPIC_BASE_URL` whose host
  is not `api.anthropic.com` (an operator-run Anthropic-compatible endpoint via plain
  `execution.env`, no plan: the CLI prices at Anthropic rates, so it is an estimate, not a report; the runner labels it because
  reconcile is a no-op without a plan). Reconcile rewrites both when a plan is active. Live progress (:895-897) prints
  `Done (N turns, est. $X)` when a plan is active so nobody reads the estimate as spend.

### `agent_eval/tools/interception.py`

`generate_interception` (:171-176): the baked `env` block skips `None` (already),
**skips `$VAR` references** instead of baking them literally (today `$EVAL_RUN_HEADER` ships
into containers as a literal string), and skips every `MANAGED_ENV_KEYS` entry — provider
env reaches Harbor agents through `--agent-env` (podman) or the pod spec (K8s), never
through the task package, because the plan's values are run-specific (which key, per-run
key or operator key, model aliases) and a task package must stay reusable across runs
(`_validate_task_package_reuse`, run.py:327-369). Nothing provider-related is baked at all;
the skip list is single-sourced from `env.py`, not hard-coded here. The package keeps
carrying the merged `tests/eval.yaml` (tasks.py) so the in-container verifier can run
`openrouter:/` judges.

### `agent_eval/hooks.py`

`build_hook_env` (:204-232) starts from `dict(os.environ)` (:216); when a plan is active it
adds `AGENT_EVAL_COST_LEDGER=<run_dir>/provider/ledger.jsonl`, `AGENT_EVAL_PROVIDER=<name>`
and `AGENT_EVAL_ROUTING_ENFORCEMENT=<level>` (not `AGENT_EVAL_HOOK_IDS`, which is per case
and set by `claude_code._build_env` for Claude Code hooks only), and **drops `cfg.api_key_env` and
`cfg.management_key_env`** from the copied environment — lifecycle hooks
(`before_all`/`after_all`/…) do not talk to OpenRouter, and the management key must never
reach a subprocess. Nothing else is scrubbed.

### `agent_eval/harbor/run.py`, `podman.py`, `kubernetes.py`, `k8s_resources.py`, `results.py`, `tasks.py`, `reward.py`

- `run.py` — **podman is MVP (PR-5), K8s is PR-7; both use the same plan and the same env
  block (Decision 23).** `run_harbor()` builds the plan right after config load
  (`build_plan(config, roles, runner="harbor-podman" | "harbor-k8s", run_id=output_dir.name)`),
  runs preflight (before task generation, so a failed `strict` preflight costs nothing), and
  wraps everything from task generation through `_write_report` (:606-663) in
  `try/finally: plan.close()` (run-end backfill retry, key-usage settle/read, per-run key
  revoke; also on `KeyboardInterrupt`, which `_forward_signal` (:609-614) already relays to
  the child). `_resolve_harbor_agent_env` (:196-210) merges `execution.env`, then
  `runner.env`, then **the plan's block last** (`settings_env_block(plan, secrets="ref",
  target="harbor_carrier")`), so the plan wins (managed keys cannot collide anyway — see
  validation); `_harbor_agent_env_args` (:216-234) keeps the carriers value-free
  (`--agent-env KEY=${AGENT_EVAL_HARBOR_AGENT_ENV_n}`, the value in `child_env`;
  `_display_command` redaction :237-244 unchanged), and Harbor merges `--agent-env` **last**
  into the agent's environment (`harbor 0.13.1 agents/base.py:288-291`, VERIFIED), so the
  container's Claude Code sees exactly the plan's `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`/
  aliases/blanks. The key is resolved on the host from `OPENROUTER_API_KEY` (or is the per-run
  key at `key-guardrail`) — the same exposure class as today's `ANTHROPIC_AUTH_TOKEN`
  forwarding (podman.py:36-49; "no security boundary", podman.py:34-36).
  **Host Vertex/Bedrock vars are not forwarded while a plan is active:** the harbor
  child env (:606-608, `child_env = os.environ.copy()`) is scrubbed of every
  `MANAGED_ENV_KEYS` entry plus `cfg.management_key_env` and `cfg.api_key_env` — the latter
  **retained only when an `openrouter:/` judge is configured**, because podman.py's
  `_start_container` runs inside this child and forwards `OPENROUTER_API_KEY` to the
  in-container verifier from it (podman.py section; the agent never reads that variable,
  its key is `ANTHROPIC_AUTH_TOKEN`) — before `Popen`,
  so podman's own forwarding (`_FORWARD_ENV`, podman.py:36-49, applied at :258) has nothing
  to pick up — `CLAUDE_CODE_USE_VERTEX`, `ANTHROPIC_VERTEX_PROJECT_ID`, `CLOUD_ML_REGION`,
  `GOOGLE_CLOUD_PROJECT`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`
  never enter the container from the host — and Harbor's stock agent (which copies host
  `ANTHROPIC_AUTH_TOKEN` into `ANTHROPIC_API_KEY`, harbor `installed/claude_code.py:1233-1236`)
  cannot re-route it either; the plan's blank lines in `--agent-env` are the second line of
  defence (they win by merge order). `-m` (:579) gets the bare `slug:variants`. No
  reachability probe, no `host.containers.internal`, no port: `openrouter.ai` is reachable
  from the container exactly as from the host, and proxy/CA vars (`HTTPS_PROXY`, `NO_PROXY`,
  `SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS`) are forwarded when set so a corporate egress works
  in the container too. Before `run_meta` is written (:641-663) call `write_run_result()`
  (execute.py's helper — the ninth write site) with the host-side ledger, so `cost_usd`,
  `cost_source`, `cost_usd_estimate`, `providers`, `routing`, `provider`, `budget` land in
  `run_result.json` exactly as in local mode; `n_infra_errors`/`infra_errors` (:656-657)
  gain the `error_class` view from the transcripts. `_validate_task_package_reuse`
  (:327-369) also compares `metadata.config_chain` and refuses stale packages; since no
  provider key is baked (interception.py) there is no `provider_sha` — a package built for
  one key/enforcement level is valid for another. The `--no-llm-judges` pre-check (:517)
  gets the merged bundle for free.
- `podman.py` `_FORWARD_ENV` (podman.py:36-49) is unchanged as a tuple; `_start_container` (:258)
  takes an additional `exclude: frozenset[str]` that run.py sets to `MANAGED_ENV_KEYS ∪
  {api_key_env, management_key_env}` while a plan is active — belt and braces with the
  child-env scrub above, and the unit test that pins it (`test_podman_forward_excludes_under_plan`)
  builds the forwarded dict from a fake host env containing `CLAUDE_CODE_USE_VERTEX=1` and
  asserts it is absent. `OPENROUTER_API_KEY` is forwarded **only** when an `openrouter:/`
  judge is configured (in-container verifier, reward.py:220-221 sees container-level env
  only); it is the operator key at every enforcement level — judges never use the per-run
  key (score.py), so the agent's `ANTHROPIC_AUTH_TOKEN` and the verifier's
  `OPENROUTER_API_KEY` are different values at `key-guardrail`. `NODE_EXTRA_CA_CERTS`,
  `SSL_CERT_FILE`, `HTTPS_PROXY`, `NO_PROXY` are forwarded when set. The host-side
  `AGENT_EVAL_PODMAN_GCP_CREDENTIALS_FILE` mount (:260-263) is skipped under a plan (a Vertex
  credential inside an OpenRouter run is noise).
- `kubernetes.py` (PR-7): the agent talks to OpenRouter directly from the pod, the same as
  podman. `_FORWARD_ENV` (:42-50) already excludes the Vertex/Bedrock vars — it forwards only
  `ANTHROPIC_MODEL`/`ANTHROPIC_BASE_URL`, and both are managed keys, so under a plan the
  forwarded set is empty and the plan's values come from the pod spec instead. `_pod_manifest`
  (:193-268) adds, when an OpenRouter plan is active, the plan's **non-secret** block as
  plain `env[]` entries (`settings_env_block(plan, secrets="omit", target="k8s_pod")`,
  including the blank Vertex lines so an in-image default cannot re-route) plus
  `{"name": "ANTHROPIC_AUTH_TOKEN", "valueFrom": {"secretKeyRef": {"name": <secret>, "key": "OPENROUTER_API_KEY"}}}`
  where `<secret>` is `$AGENT_EVAL_K8S_CREDENTIALS_SECRET` at `audit` and the per-run
  Secret `agent-eval-<run_id>-openrouter` at `key-guardrail` (container `env[]` wins over
  the existing `envFrom.secretRef` :244-246, so a stale `ANTHROPIC_AUTH_TOKEN`/
  `ANTHROPIC_BASE_URL` in the credentials Secret cannot leak through — documented). Existing
  K8s deployments whose credentials Secret points `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`
  at an in-cluster Anthropic-compatible endpoint keep working **unchanged** when no
  `openrouter:/` role is configured (no plan, no new requirement, `cost_source: runner:estimate`). The
  `export KEY=value;` exec prefix is built in **kubernetes.py `exec()` (:541-548, `:547`)**,
  not run.py: run.py drops the `ANTHROPIC_AUTH_TOKEN` carrier from `--agent-env` on the
  K8s path (the pod already has it), so the key value is never inlined into an exec command
  or a log. Proxy/CA vars pass through like podman. `k8s_resources.create_openrouter_secret(namespace, name, key)`
  (next to `create_env_secret`, :202-211) writes the per-run Secret holding only
  `OPENROUTER_API_KEY` and `delete_openrouter_secret(namespace, name)` removes it from
  `plan.close()` together with the key revoke; at `audit` the operator manages the
  credentials Secret as today and the harness creates nothing.
- `results.py` — **gen-id extraction from trial artefacts.** `_extract_transcript_metrics`
  (:80-160; reached through `_agent_transcript_metrics(trial_dir / "agent")`, :161-168,
  :335) already walks the trial's captured stream-json line by line (:87-100) and reads
  `total_cost_usd` (:114); it now also collects every `assistant` `message.id` (the same
  rule as stream_capture.py:105-112, shared code) and returns `message_ids` +
  `model_echo` per id. Because the trial transcript is per trial and the ids are per
  request, attribution is **per trial, exact** — no `ts`-window split, no shared-`case_id`
  ambiguity (replications of one case get their own ids). Ids
  are fed to the host-side `generation.py` worker when `run.py` parses the job dir after
  `harbor run` exits (there is no job-dir poller today and none is added in PR-5: by then
  the `/generation` lag has long passed, so the first poll succeeds and the whole backfill
  takes seconds; a poller is a PR-7 nicety for live per-trial cost lines); per-trial records
  (:300-349) get `cost_usd` = Σ their rows,
  `cost_source: openrouter:generation`, `cost_usd_estimate` = the transcript
  `total_cost_usd`, and per-step records the same by `step_id`; job totals (:583-633) carry
  `cost_source`/`cost_coverage`/`providers`/`routing`/`budget` from one run-level
  `reconcile()`. **Probe #25 (UNVERIFIED, blocks per-request cost on podman in PR-5):**
  Harbor's captured agent transcript contains the `assistant.message.id`s (it is Claude
  Code's own `--output-format stream-json`, so this is expected, but Harbor 0.13.1's
  `installed/claude_code.py` post-processing has not been checked for id stripping).
  **Fallback if it does not:** the run-level key-usage delta (`GET /api/v1/key` before
  `harbor run` and ≥ 20 s after it exits) becomes the run's `cost_usd`
  (`cost_source: openrouter:key-usage`, `cost_confidence: medium` on a dedicated key, `low`
  otherwise), per-trial `cost_usd` stays `null` with the estimate preserved, and the routing
  audit is `audit_complete: false` with `routing.unattributed = requests` — the report says
  so in the banner. Either way `results.py` never invents a per-trial number.
- `tasks.py` `_bundle_eval_config` (:96-103): loads via `load_raw` and bundles the **merged
  mapping** (no `extends:` key) so `tests/eval.yaml` is self-contained, the in-container
  `reward.py:481 from_yaml` never resolves a path relative to the container, and both the
  `bundled_cfg.get("judges")` verifier selection (:511) and the reward bridge see the full
  config; keeps `models.providers.openrouter` (judge routing, `judge.inherit_pins`) and
  `judges[].provider_options`; records `config_chain` in `task.toml metadata`. The
  bundle carries no key, no enforcement level and no plan-derived env (interception.py).
- `reward.py` `score_case` (:213-225): judge usage is written per judge into the
  `verifier/judges.json` sidecar; results.py sums it into `judge_usage`.

### `skills/eval-run/scripts/execute.py`

- `--model`/`--subagent-model` (:354-372) accept URIs; resolution (:406-411) becomes
  `skill = parse_agent_model(args.model or config.models.skill)`. **Activation rule:**
  the agent plan is built iff the *effective* agent model (CLI > config) has provider
  `openrouter`; judges consult `models.providers.openrouter` iff their own URI is `openrouter:/`;
  a `models.providers.*` block with no matching role is inert. After CLI resolution (not in
  `from_yaml`) print one WARNING when `models.providers.openrouter` is declared but no effective
  role or CLI model uses `openrouter:/` ("routing table inactive").
- Startup order: config → `build_plan(config, roles, runner="claude-code", run_id)` (at
  `key-guardrail` `build_plan` first checks that `management_key_env` is set in the harness
  process env — the Config-validation error, value never echoed — then provisions the
  per-run key, so it precedes preflight, which then validates *that* key) → preflight (catalog/key/eligibility checks, `routing_snapshot.json`)
  → key-usage "before" read → one stderr line naming the enforcement level and, at `audit`,
  `key exposed to agent: operator key (enforcement: audit)` → start the backfill worker →
  `runner_cls.from_config(..., provider_plan=plan, run_id=args.run_id)` (:476-484; per-step
  reconstruction :1257-1285 receives the same) → run. Batch and case mode share one plan
  and one worker. The whole sequence after `build_plan` sits inside `try/finally:
  plan.close()` — that `finally` is where the run-end backfill retry, the ≥ 20 s key-usage
  settle/read, the last reconcile and (at `key-guardrail`) the key revoke happen, after the
  `after_all` hooks (:584-588, :1695-1699) so a hook's own transcript ids are included. Each
  `execute()` keeps computing its per-invocation cap (`max_budget` :493-495 /
  `step_budget` :1289-1290) and maps it to the CLI flag as `cap > 0 → --max-budget-usd
  cap × cli_budget_inflation`, `cap ≤ 0 / None → no flag` (the CLI rejects `0`, probe #23;
  the key-guardrail `limit_usd` is unaffected by this mapping); nothing is registered per case.
- **Reconcile-at-write contract.** One helper
  `write_run_result(path, payload, *, plan, ledger, key_usage=None) -> dict` is the ONLY code
  in execute.py that opens `run_result.json`; it calls `reconcile()` (no-op with no plan and
  no ledger file) and then dumps. All **eight** current write sites go through it —
  enumerated so no subset is patched: (1) `:982` case-mode per-case result (repo mode,
  :967-985); (2) `:1117` case-mode `failed_result` on runner exception (crash path);
  (3) `:1168` case-mode per-case result (:1155-1170); (4) `:1392` multi-step failed result
  with partial `steps` (step-error path); (5) `:1426` multi-step final write with aggregated
  step metrics; (6) `:1679` batch per-case exception result (parallel crash path);
  (7) `:1768` case aggregate (:1716-1770 — sums reconciled per-case values, never re-sums
  estimates); (8) `:1840` batch `_save_result` (:1791-1857). Crash and step paths are where
  the runner estimate is most misleading (killed run, partial ledger), so `cost_source` and
  `cost_usd_estimate` are written on every path, not only the happy ones. harbor/run.py:663
  is the ninth writer outside execute.py and calls the same helper. `after_all` hooks
  (:584-588, :1695-1699) still run after; `plan.close()` runs after them in the same
  `finally` and performs the **final** reconcile (the one that sees the run-end backfill
  retry and the key-usage delta) through the same helper, so the last `run_result.json` on
  disk is the most complete one. Guarded by the grep-based write-site test (Tests).
- `_build_eval_params` (:672-696) adds `provider {name, kind, transport: "direct", runner, base_url, routing_sha (the declared spec's SHA — nothing is sent, so there is no effective/configured split), routing_enforcement, background_model, key_exposed_to_agent, key_scope, key_hash}`,
  `config_chain`, `budget {cli_cap_usd, invocation_usd, run_usd, enforcement: "cli-estimate" | "key-guardrail"}`
  (the report renders `enforcement` next to the budget and the startup line names it).
  `_cost_label` (:748-751) prints `$X (openrouter:generation)` / `$X (openrouter:key-usage)` /
  `cost n/a (est. $Y, unreconciled)`.
- Live markers: the per-case progress line (:985-990) is written from
  the reconciled per-case payload, so it can append `[routing: N violations]` when the
  case's backfilled rows already show a served provider outside the pinned set,
  `[cost pending: N ids]` while ids are still inside the backfill window (the line converges
  within ~15 s of the case ending), and `[budget: limit_usd]` when the case's error output
  carried the per-run key's 402. There is no in-flight event stream to tail — everything is
  derived from the ledger at write time. In Harbor the same markers are printed per trial
  when the job dir is parsed.
- New flags: `--strict-cost` (exit 2 when a plan is active and `cost_source` is
  `unavailable`, **or** when `budget.run_usd` is set and the final backfilled Σ exceeds it —
  the post hoc form of the run budget at `audit`; at `key-guardrail` the server enforces it
  in flight and this flag only adds the exit code), `--strict-routing` (exit 2 when
  `routing.violations` is non-empty or `audit_complete` is false at run end), and
  `--allow-estimate` (under a plan: lets reconcile write `cost_source: runner:estimate` instead
  of `unavailable` when neither truth source landed — for offline development against a
  recorded transcript; never the default).

### `skills/eval-run/scripts/workspace.py`

- `_inject_env` (:644-659): skip `None` instead of writing `"None"` (parity with
  interception.py:173-174, claude_code.py:716-717, harbor/run.py:201-202). Otherwise
  unchanged — provider env is emitted by the runner overlay, not here, so the three
  `_inject_env` call sites (:500-501, :791-792, :845-846) keep their tests.
- `_deep_merge` (:709-718) becomes `from agent_eval.config import deep_merge` (called with
  `dedupe=False`); `_apply_runner_settings` (:721-757) last-wins is unchanged **for
  non-managed keys**; its docstring and runner.md gain the ownership rule: while a plan is
  active, managed keys are owned by the plan (load error if authored; overwritten by the
  overlay regardless).
- The `EVAL_RUN_HEADER` shim (:81-85) stays for configs that reference it.

### `skills/eval-run/scripts/score.py`

- `_client_for(judge_client_cfg)` next to `_get_openai_client` (:1279-1306): lazy `from openai import OpenAI` with the same
  actionable ImportError text; `api_key = os.environ.get(cfg.api_key_env)` →
  `RuntimeError("Set OPENROUTER_API_KEY")` if absent (no placeholder — OpenRouter always
  authenticates); `base_url = cfg.base_url + "/v1"` (the one knob, `$VAR` resolved at config
  load; no `OPENROUTER_BASE_URL`); `default_headers = {"HTTP-Referer": …, "X-OpenRouter-Title": …}`;
  `max_retries=cfg.max_retries`, `timeout=cfg.timeout_s`; memoised per `(base_url, headers, api_key_env)`.
  `_get_openai_client()` is the `judge_client_cfg is None` case. Error text never
  contains a value (test-enforced, as for `_get_openai_client` :1289-1298). The judge path
  is independent of the agent transport (score.py runs in its own process): the client
  talks to `https://openrouter.ai/api/v1/chat/completions` with the
  operator key from `OPENROUTER_API_KEY` at every enforcement level (the per-run key is the
  *agent's*; judge rows are always spent on the operator key (ledger rows carry no scope field;
  `provider.key_scope` describes the agent's key), and judge spend is therefore outside the
  per-run key's `limit_usd` — `budget.run_usd` bounds agent spend only, stated in Known
  limitations).
- Shared call: `_call_structured_judge_openai(prompt, model, feedback_type, images=None, max_tokens=4096, bounds=None, *, client=None, extra_body=None, token_param="auto")`
  (:1353-1400) and `_call_pairwise_openai(client, system_prompt, user_message, model, max_tokens=16384, *, extra_body=None, token_param="auto")`
  (:2979-3012). The `openai` transport arm passes `client=_client_for(cfg)`,
  `extra_body = routing.to_chat_extra_body(slug, role="judge", overrides=jc.provider_options) | cfg.extra_body`
  and `token_param="max_tokens"` when a client config is present (OpenRouter's universal
  parameter; note `openai/gpt-5.2` does not match `_OPENAI_REASONING_PREFIXES` at :1313 so
  `max_tokens` is already what would be sent — the explicit policy pins it). Hardening on
  the shared path: tolerate `function.arguments` arriving as a dict (:1386-1388 →
  `data = args if isinstance(args, dict) else json.loads(args)`); raise
  `JudgeProviderError(error_type)` when `choices[0].finish_reason == "error"` or the response
  carries a top-level `error` (OpenRouter's committed-200 failure — VERIFIED) instead of
  feeding `""` to the text parser; text fallback last. No `reasoning` field is ever sent
  unless the operator puts one in `judge.extra_body`.
- **Forced `tool_choice` vs pins (probe #5, 2026-09-16; Decision 25).** The structured-judge
  call forces a named function (`tool_choice: {type: "function", function: {name}}`). Under
  pinned providers that do not support named-function forcing OpenRouter answers 404
  `not_found` "No endpoints found for <slug>" — identical with and without
  `require_parameters: true` — while the same request unpinned succeeds. Policy:
  1. **Judge pins are opt-in.** `routing.defaults`/`routing.models` `order`/`only`/
     `quantizations` apply to the *agent* roles; `to_chat_extra_body(slug, role="judge")`
     emits them for a judge only when `models.providers.openrouter.judge.inherit_pins: true` or the
     judge sets `provider_options.routing`. Unpinned judges keep `sort`, `data_collection`,
     `zdr`, `max_price` and `fallbacks` only; `require_parameters` is sent for a judge **only
     when its routing carries pins** (`order`/`only`) — an unpinned judge never sends it
     (they never bind to one endpoint). `to_chat_extra_body()` therefore equals
     `to_messages_body()` only for a judge that inherits pins (RoutingSpec).
  2. **Preflight** (per pinned judge slug): every pinned endpoint is checked for
     `supports_tool_choice.function`; endpoints without it are WARNed and the call uses the
     fallback below; a pinned set with none is a FAIL under `strict`.
  3. **Fallback ladder** when a pinned endpoint lacks `function` forcing (or on a live 404
     `not_found` with the "No endpoints found" message on the first attempt): retry once
     with `tool_choice: "required"` if `supports_tool_choice.required`, else
     `tool_choice: "auto"`, then **strict-parse the first tool call** in
     `choices[0].message.tool_calls` (name must equal the requested function, arguments must
     parse as the schema; anything else is a `JudgeProviderError`, never the text fallback,
     so a degraded forcing mode cannot silently turn a structured verdict into prose). The
     ledger judge record carries `tool_choice_mode: function|required|auto`, and `summary.yaml`
     `judge_usage` counts `tool_choice_fallbacks`.
  4. The routing 404 is `error_class: config` (errors.py), not retried by
     `_with_judge_retries` (a config error does not heal), and is
     reported as a config problem naming the pinned endpoints and their `supports_tool_choice`
     — not as a judge failure and not as `infra_errors`.
- Dispatch sites stay three-way (`anthropic | openai | runner`): `_make_builtin_scorer`
  (:915-946), `_load_llm_judge` (:2612-2638), `compare_runs` (:2696-2712, client selection),
  `_call_judge` (:2942-2947) obtain `judge_client_cfg = resolve_judge_client(model, config.models.providers)`
  and pass it into the `openai` arm. Pairwise `--model` (:3407-3410) accepts the URI.
- Usage side channel (all backends): scorers may return `JudgeOutcome(value, rationale, usage)`;
  `_normalize_result` (:1574-1580) returns `(value, rationale, usage|None)`; `_score_case`
  (:1974-1994) stores `usage` on the per-case judge record and sums it across `samples`
  (including failed attempts, which still consumed tokens). Usage shape:
  `{"model", "provider", "id", "prompt_tokens", "completion_tokens", "reasoning_tokens", "cost_usd", "cost_source": "provider-inline"|"none"}`.
  Extraction uses `getattr(response.usage, "cost", None)` / `getattr(response, "provider", None)`
  with a `model_dump().get(...)` fallback rather than `model_extra` internals
  (`extra="allow"` verified on openai 2.46.0; probe #14 remains the floor check);
  Anthropic/OpenAI judges: tokens only, `cost_usd: null`. Every judge call appends a
  `role: judge` ledger record with `provider_kind`. `score_cases` aggregates
  `judge_usage: {judge_cost_usd, by_judge, by_model, requests, requests_missing_cost, tool_choice_fallbacks}`
  (`tool_choice_fallbacks` = judge calls that ran in `tool_choice_mode` `required`/`auto`
  after the Decision 25 ladder) into `summary.yaml` next to `run_metrics` (:3352-3359) plus `total_cost_usd`, defined
  per the **Null-cost arithmetic** table: `cost_usd + judge_cost_usd` only when both
  are numeric, else `null`, with `total_cost_source` naming the numeric addends and
  `judge_cost_usd`/`hook_cost_usd` always exposed separately. The builtin
  `cost_budget` judge (`agent_eval/judges/efficiency/cost_budget.py:9-11`) becomes
  provenance-aware as specified there (abstain on `unavailable`, label estimates);
  `compute_run_metrics` (:3219-3255) stays agent-only so `cost_per_turn_usd`/`cost_per_mtok_usd`
  remain comparable with old runs.
- Resilience: `_with_judge_retries(fn, cfg)` around the SDK call for 429 (`Retry-After`),
  502/503 and committed-200 `provider_unavailable`/`provider_overloaded`, jittered, up to
  `max_retries`; a `BoundedSemaphore(cfg.concurrency)` caps concurrent OpenRouter
  judge calls independently of the `min(len(cases), cpu_count)` pools (:1922, :2745).

### `skills/eval-run/scripts/report.py`

`_render_run_config` (:943-982) gains two panels. **Cost provenance:** rows `Cost source`
(`openrouter:generation` / `openrouter:key-usage` / …), `Cost confidence` + coverage
(`requests_priced / requests`, `requests_missing_cost`), `Cost (runner estimate)` when it
differs (with the inflation factor, e.g. `×41`), `Key-usage cross-check` (delta and
deviation %), `Hook cost` (when > 0), `Judge cost`, `Total cost` (+ `total_cost_source`),
`Budget` (invocation cap, `cli_cap_usd`, `run_usd`, `enforcement: cli-estimate |
key-guardrail`, `exceeded`/`exceeded_reason`), `Key` (`key_scope`, `key_hash`,
`key_exposed_to_agent`). **Routing audit:** rows `Enforcement` (`audit` / `key-guardrail`
/ `none`), `Declared pins` per routing key (order / allow_fallbacks / quantizations /
variant / policy), `Providers served` (`endpoint_tag: N`, incl. `unknown: N`), `Audit`
(`compliant / audited`, `violations` listed with `case_id`, `gen_id`, served vs expected,
`unattributed`, `audit_complete`), `Snapshot` (link to `provider/routing_snapshot.json`,
`routing_sha`). A red banner when `cost_source: unavailable`, `routing.degraded`,
`audit_complete: false` at run end, or `budget.exceeded`; an amber one under `policy: warn`
with violations. `_render_model_usage` (:1115-1290):
`Provider(s)` row from `per_model_usage[*].providers`; Cost/turn and Cost/Mtok cells carry
a source mark (`≈` for estimate, `?` for an unmatched per-model key). Analysis subtitle
(:1620-1622) uses `cost_usd`, falls back to `≈$Y est.` styling. `_load_yaml` (:143-147)
delegates to `load_raw`.

### `skills/eval-run/scripts/tools.py`

The AskUserQuestion hook model client (:243, `anthropic.Anthropic(timeout=30.0)`) needs no
change of construction: the hook subprocess inherits Claude Code's env, which under a plan
is the overlay's direct block, so the SDK's own `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`
resolution points it at OpenRouter with the bare hook slug (`models.hook`, defaulting to the
skill model). Its spend is real: the response `id`
is a `gen-…` id, so the hook writes it to `$AGENT_EVAL_HOOK_IDS` (a per-case JSONL under
`<run_dir>/provider/`, path exported by `claude_code._build_env` when a plan is active) and
`stream_capture`'s collector feeds those ids to the backfill as `role: hook` (reported as
`hook_cost_usd`, excluded from `cost_usd`). With a plan active `models.hook` must share
the plan's provider kind (validated at load) — an Anthropic hook model alongside an
OpenRouter agent is a known limitation: probe #10 (VERIFIED, CLI 2.1.273+) shows the hook
subprocess inherits the settings-env `ANTHROPIC_BASE_URL`/token, so its client **would** be
redirected to OpenRouter and 404 on the Anthropic slug — the load-time rejection rests on
evidence, not a conditional.

### `skills/eval-compare/scripts/compare.py`, `skills/eval-anova/scripts/analyze.py`, `skills/eval-mlflow/scripts/log_results.py`, `agent_eval/mlflow/trace_builder.py`

`get_metric` (compare.py:126-139) and `_run_cost` (analyze.py:330-335) read `cost_source`
(legacy literals normalised) and refuse to pool `runner:*`/`harness:*`/`unavailable` rows
with `openrouter:*` costs in one column (greyed cell + footnote; `--allow-mixed-cost-sources`
overrides); eval-compare prints a `routing_snapshot.json` diff between runs. `log_results.py`
(:281-303) logs `judge_cost_usd`, `total_cost_usd`, `hook_cost_usd`, per-provider metrics and
tags `cost_source`/`routing_sha`/`budget.enforcement`; `trace_builder.py` (:612-615, :1018-1073)
keeps distributing agent cost only — and only the **reconciled** `per_model_usage[m].cost_usd`,
per the Null-cost arithmetic table: `_harbor_step_run_result` (log_results.py:165-193)
reads the per-step reconciled cost from results.py's ledger join instead of the transcript
`total_cost_usd`, and a `null` per-model cost leaves `mlflow.llm.cost` unset on those spans
rather than re-deriving it from the estimate. **Pooling rule (audit-aware):** eval-compare
and eval-anova pool two runs only when their `routing.sha` match **and** both audits are
clean and complete (`violations == []`, `audit_complete: true`); a run with violations, an
incomplete audit or a different `routing.enforcement` (`audit` vs `key-guardrail` vs
`none`) is a **different factor level** — compare greys the pair with a footnote naming the
served providers that differ, anova adds `enforcement` and `audit_clean` to the blocking
factors and excludes degraded runs (`routing.degraded: true`) by default;
`--allow-unaudited` / `--allow-mixed-enforcement` override. The snapshot diff shows the
declared pins and the *served* endpoint histogram side by side, which is how two runs of
"the same model" that were actually served by different providers become visible.
`log_results.py` tags `routing_enforcement`, `audit_violations`, `cost_confidence`
and `key_scope` on the MLflow run.

### `scripts/ensure_deps.py`

`_needs_openai_backend` (:123-137) already returns `True` for `openrouter:/…`; align the
bare-id branch with `resolve_judge_backend` (any non-Claude bare id → `True`, add `o5`).
Discovery (:241-242 uses `discover_configs`; :252 fallback) calls
`discover_configs(cwd, include_profiles=True)`; `_deps_for_config` (:70-80) follows the
`extends:` chain — via `load_raw` when `agent_eval` imports, else a stdlib-only follow of
the `extends` key (depth ≤ 8) merging only the `judges`, `models` and `mlflow` keys it
inspects — so an overlay that *adds* an `openrouter:/` judge installs `openai` even when its
base does not. Documented limitation: deps are the union over discovered files; a profile
outside the scan set (`eval-profiles/`, `eval/profiles/`, `eval/*.yaml`) contributes
nothing until it is run with `--config`, at which point the session-start scan is repeated.
**HTTP client rule (stated once here, consistent with "Backfill and catalog client").** The OpenRouter client modules (`providers/http.py` →
`generation.py`, `catalog.py`, `keys.py`, preflight) use **stdlib `urllib.request`**, so an
OpenRouter *agent* configuration adds **no dependency** and `ensure_deps` gains **no new
rule**: there is no `httpx` spec, no `openrouter` extra in pyproject.toml, no
`require_httpx()`. Rationale: the client issues a few hundred small, non-streaming GETs
plus at most two management calls per run; per-call `timeout=` and `Retry-After` handling
are all it needs, the default SSL context is already `truststore`-injected
(`agent_eval/_bootstrap.py:147-148`), and there is no server, no streaming relay and no
connection-pool requirement to justify httpx. The only
OpenRouter-related dependency remains the existing **judge** rule: `openai` for
`openrouter:/` judges (`_needs_openai_backend`), which the `extends:` follow above already
covers. `test_ensure_deps` asserts that an OpenRouter config with no LLM judges yields an
empty extra-deps list.

### `skills/eval-dataset/scripts/generate_synthetic.py`

Reject `openrouter:/` explicitly (:66-70 pattern) with guidance ("use a Claude model or
`runner:/…`"); today it would silently become a runner call with the bare id (:323).

### `deploy/Containerfile`

`:30` installs `openai` so `openrouter:/` (and `openai:/`) judges can run in the
in-container verifier. Nothing else: the agent's transport is Claude Code's own HTTP stack
pointed at OpenRouter by env, and the harness-side backfill/preflight/audit run on the
host (podman) or in the harness pod (K8s/EvalHub) over stdlib `urllib` — the
agent image needs no OpenRouter client of its own. `NODE_EXTRA_CA_CERTS`/`SSL_CERT_FILE`
are honoured if the operator's egress requires them (forwarded by podman.py / set in the
pod spec).

### Config validation

Fail fast in `EvalConfig.from_yaml` (next to :1737-1761), one consolidated error per
category listing every offender:

- **Enforcement / guardrail (Decisions 3, 8 and 10):** `models.providers.openrouter.routing.enforcement`
  ∉ {`audit`, `key-guardrail`} → error. `key-guardrail` requires (a) `budget.run_usd` set and
  `> 0` (it becomes the per-run key's `limit_usd`; there is no unlimited per-run key), (b)
  at least one routing key with a `pinned_set` **or** `guardrail.providers` as an explicit
  non-empty list (a guardrail with no provider restriction is a validation error — use
  `audit` if only the budget is wanted; that combination is `Out of scope (future)` until
  probe #26 says `limit` alone is meaningful), (c) `management_key_env` naming a variable
  that is **set in the harness process env at plan build** (checked in execute.py/
  harbor/run.py, not in `from_yaml`, so a config can be validated without the key; the
  message never echoes the value). Until PR-6 lands, PR-3b validation accepts
  `key-guardrail` but PR-5's plan build raises `ConfigError: routing.enforcement:
  key-guardrail lands in PR-6`. `guardrail.providers` entries are normalised like
  `RoutingSpec.order` (catalog slug, display name with a WARNING, unknown → error under
  `preflight: strict`). `guardrail.settle_s < 20` is a warning naming the VERIFIED settle.
  `routing.policy` ∉ {`strict`, `warn`} → error. **Reserved keys** — transport
  mode/options keys, `direct`, `budget.max_unpriced`, `budget.max_unpriced_ratio` — are
  rejected by name with "not supported: no proxy in scope; see spec 014 Decision 1" (one
  error listing every offender), never silently ignored; any other unknown sub-key is an
  ordinary unknown-key error.
- **Agent-path routing intent (RoutingSpec):** a `routing.defaults`/`routing.models` entry
  carrying `require_parameters`, `sort`, `data_collection`, `zdr` or `max_price` under a
  routing key that is used by an agent role emits a WARNING per key ("not sendable from
  Claude Code; audited keys use order/only/ignore/quantizations; sort → `:nitro`/`:floor`
  variant; data policy → account settings") and the keys are ignored on that path; the same
  spec is sent in full to judges that inherit pins. `quantizations` with no `order`/`only`
  is a WARNING ("quantization is pinned indirectly through providers; nothing to audit").
- `openrouter:/` with an empty model on any role; unknown explicit provider on
  `models.skill`/`subagent`/`hook` (same label style as judges; any prefix other than the
  builtins and `openrouter:/` is simply unknown); agent roles (`skill`, `subagent`, `hook`)
  on different provider kinds.
- **Bare-id footgun:**
  `models.providers.openrouter` present AND a role model is a bare id whose `routing_key` matches a
  `routing.models` key (or is a bare non-Anthropic id) → error "bare model id next to
  models.providers.openrouter — use openrouter:/<id> or drop the pins". An unused `models.providers.*`
  block is otherwise inert (a warning is printed by execute.py/score.py after CLI resolution).
- An `openrouter:/` agent model with `runner.type: cursor` → error (no base-URL knob);
  `codex`/`cli`/`responses-api` with such an agent model → error ("direct OpenRouter
  transport is implemented for `claude-code` — local, Harbor podman, Harbor K8s"; Out of scope).
- `agent:` judges whose model is `openrouter:/…` → error (they would become
  `claude --model <bare>` via score.py:2489-2491; the exemption at :1745-1750 is narrowed).
- **Managed-key ownership:** while a plan is active, across every surface that
  can reach settings.json or the process env — `execution.env`, `runner.env`,
  `runner.settings.env`, `execution.steps[].env`, `execution.steps[].runner.env`,
  `execution.steps[].runner.settings.env` — a `MANAGED_ENV_KEYS` entry whose plan value is
  dynamic or secret (`ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_CUSTOM_HEADERS`)
  is rejected on **presence**; entries with a static plan value (the Vertex/Bedrock blank set,
  the alias vars) are tolerated when identical and rejected when different, so old
  Vertex-blanking configs keep loading. **`ANTHROPIC_API_KEY` is the one static key with
  warning severity (probe #13, 2026-09-16):** a non-empty value under an active plan is a
  **warning** — "`<surface>.ANTHROPIC_API_KEY` is non-empty; it would be sent to OpenRouter
  as `x-api-key` (which works) — the plan blanks it so a stale Anthropic key or cached OAuth
  state never reaches the wire; remove it" — not an error, because the overlay's `""` wins by
  merge order anyway and correctness does not depend on the blank (hygiene only).
  **Secrets are env-only:** `OPENROUTER_API_KEY`/`$OPENROUTER_API_KEY` and
  `OPENROUTER_MANAGEMENT_KEY`/`$OPENROUTER_MANAGEMENT_KEY` (or whatever `api_key_env`/
  `management_key_env` name) on any of those surfaces — `execution.env`, `runner.env`,
  `runner.settings.env`, the `steps[]` variants — are **always** rejected, plan or no plan
  (they would be baked into task packages by interception.py:171-176 and into workspace
  settings). Message format: `remove <surface>.<KEY>; owned by models.providers.openrouter
  (enforcement=<level>)`, one error listing every offending pair. (Ownership is enforced by
  overlay/`--agent-env`/pod-spec merge order regardless; this check exists so a migrating
  user gets one actionable message, not N retries.)
- `routing` schema: known keys only, quantization enum, `fallbacks ≤ 3`, `sort` shape;
  `models.providers.openrouter.judge.inherit_pins` must be a bool (default `false`; Decision 25) and
  is rejected when `models.providers.openrouter.routing` is absent (nothing to inherit);
  `api_key_env`/`management_key_env` must name a variable (never a value — a value that
  looks like `sk-or-…` is rejected with the variable name only in the message);
  `models.providers.<name>.kind` must equal `<name>`; `kind: openai-compatible` → "not implemented
  in this release"; any other `models.providers.*` name is an error; a top-level
  `providers:` key is an error pointing at `models.providers` (Decision 17).
  `cli_budget_inflation` must be a number ≥ 1 (a value of 1 is allowed for operators who
  want the CLI's estimate cap to bite; a warning notes the estimate is inflated).
- `provider_options` validated by the judge's provider kind; `extends:` cycles/depth errors;
  `!replace` only on list values.

### Docs

`website/reference/config/models.md:20-27,110-124` (URIs on every role),
`judges.md:169-195` (table row, `provider_options`), `runner.md:125-142,246-268` (managed-key
ownership next to `runner.settings`, precedence) and `execution.md:14,29,198-215,252-253`
(managed keys, precedence, "per-invocation semantics unchanged under OpenRouter",
`models.providers.openrouter.budget.run_usd`), `environment-variables.md:13-70,104-141`
(`OPENROUTER_API_KEY`, `OPENROUTER_MANAGEMENT_KEY` — env-only, never in config; proxy/CA
pass-through; corrected Harbor forwarding table stating which host vars are **dropped**
under a plan; no `OPENROUTER_BASE_URL`), `guides/harbor.md:146-157,202-232` (podman:
direct env via `--agent-env`, key resolved on the host, Vertex vars not forwarded; K8s:
Secret key name `OPENROUTER_API_KEY` → `ANTHROPIC_AUTH_TOKEN` mapping, per-run Secret at
`key-guardrail`; existing in-cluster Anthropic-compatible-endpoint deployments unchanged
when no `openrouter:/` role is configured), new `reference/config/providers.md` (the `models.providers` registry
— nested under `models` per Decision 17, cross-linked from `models.md` — the single `openrouter` kind,
every knob above, `extends:` merge policy with `!replace`, path resolution) and
`guides/openrouter.md` (direct transport and why there is no proxy — Decision 1 in one
paragraph; enforcement levels `audit` vs `key-guardrail` with what each does and does not
guarantee; account-level settings — the three toggles and the deepseek-v4.1-flash case;
quantization pinned indirectly; routing lifecycle preflight → snapshot → run → backfill →
audit; cost provenance and confidence; budget scope — per-invocation CLI cap ×
`cli_budget_inflation`, `run_usd` post hoc at `audit` / server-side at `key-guardrail`,
judge spend outside it; secrets and `key_exposed_to_agent`; offline
`agent-eval provider backfill|audit|revoke`). Spec 013 "Out of scope" (:169-173) gets a
cross-link.

## Behavior changes / migration

- No behaviour changes unless a model URI uses `openrouter:/` (the only provider kind and
  the only URI scheme this spec adds, Decision 1). Bare ids, `openai:/` + `OPENAI_BASE_URL`,
  `runner:/`, `_SAFE_ENV_KEYS` pass-through (tests/test_extract_progress.py:397-404;
  unchanged when no plan is active), `runner.env` `$VAR` semantics,
  `execution.env → settings.json`, `runner.settings.env` last-wins (**for non-managed
  keys**; managed keys are owned by the plan while one is active — load error if authored,
  overwritten by the overlay regardless), the forced `CLAUDE_CODE_SUBAGENT_MODEL`, Harbor
  `--agent-env` carriers (tests/test_harbor_run.py:336-366), podman host-env forwarding
  (`_FORWARD_ENV`, podman.py:36-49 — unchanged **without** a plan), existing K8s
  in-cluster credentials Secrets that point at an Anthropic-compatible endpoint, and
  existing configs that set the Anthropic env vars by hand keep working unchanged. An
  operator who fronts Claude Code with their own Anthropic-compatible endpoint via plain
  `execution.env` keeps that path as-is:
  no plan, no ledger, `cost_source: runner:estimate` (unsupported by this feature, not
  broken by it).
- **`models.providers` is a new, optional sub-key of `models`** (Decision 17) — the only
  config surface this feature adds besides `judges[].provider_options` and the
  `openrouter:/` URIs; the OpenRouter feature itself introduces no top-level key (the
  independent, optional `extends:` key is PR-3a, below), and an existing `models:` block
  without it parses exactly as today.
- **No process is added to the run.** Nothing listens on a port, no bind address, no
  token file, no handshake file; `openrouter.ai` is the only endpoint the agent talks to,
  from the host, from the podman container and from the K8s pod alike. What the harness
  adds is out-of-band only: preflight GETs before spend, `/generation` + `/key` GETs after
  each request, and (at `key-guardrail`) three management-API calls per run.
- **Budget semantics are unchanged in scope** — `execution.max_budget_usd` and
  `steps[].max_budget_usd` remain per-invocation caps on every backend (execution.md:252-253),
  enforced by the CLI on its Anthropic-priced estimate; under OpenRouter the CLI receives
  `cap × cli_budget_inflation` so the inflated estimate does not kill a run early. The
  cap stops being a real-dollar bound: at `enforcement: audit` the real cost is known
  post hoc (`--strict-cost` against `budget.run_usd`), at `key-guardrail` the per-run key's
  `limit_usd` is the in-flight real-dollar bound. `models.providers.openrouter.budget.run_usd` is
  new and additive.
- **Host Vertex/Bedrock vars stop reaching containers while a plan is active.** Today
  podman forwards `CLAUDE_CODE_USE_VERTEX`, `ANTHROPIC_VERTEX_PROJECT_ID`, `CLOUD_ML_REGION`
  (podman.py:36-49); under an OpenRouter plan they are dropped from the child env and
  blanked again by `--agent-env`. A Harbor user who relied on host Vertex forwarding for an
  Anthropic run is unaffected (no plan → forwarding unchanged); one who mixes an
  `openrouter:/` agent with a Vertex `models.hook` gets a load error (Config validation).
- `_inject_env` no longer writes the literal string `"None"` for YAML `null` (it omits the
  key) — a bug fix that could change behaviour only for a config relying on `"None"`.
- `.eval-permissions.json` is renamed `.eval-overlay.json` (same lifecycle; docs updated).
- `run_result.json` gains optional fields; a missing `cost_source` is read as
  `runner:reported`; legacy literals (`runner-reported`, `harness-estimate`,
  `openrouter-reconciled`) are recognised by every reader. With a provider active and no
  truth source, `cost_usd` is `null` (not the inflated estimate) unless `--allow-estimate`
  was passed; `cost_usd_estimate` keeps the runner number. Real cost lands `~15 s` after a
  case ends (the `/generation` lag plus one poll), so a `run_result.json` read *during* the
  run can show `[cost pending: N ids]`; the final write in `plan.close()` is complete.
- `routing` and `provider` are new `run_result.json` blocks; eval-compare/eval-anova
  **refuse to pool** two runs whose `routing.sha` differ or whose audit is not clean
  (`violations != []` or `audit_complete: false`) unless `--allow-unaudited` is passed —
  a run that today pools silently with a different proxy-side `order` is flagged.
- The Harbor podman child env and container are scrubbed of `MANAGED_ENV_KEYS` and the
  two OpenRouter key names while a plan is active; `OPENROUTER_API_KEY` is forwarded into
  the container **only** when an `openrouter:/` judge runs in-container (verifier). Task
  packages carry no provider env, so packages built with and without a plan are
  interchangeable; packages generated before this change lack `metadata.config_chain` and
  are refused for reuse only when the current config was loaded through `extends:`.
- `_normalize_result` returns a 3-tuple; third-party scorers returning 2-tuples/`Feedback`
  keep working (`usage=None`).
- `resolve_judge_backend` returns unchanged values for every existing input; only
  `openrouter:/…` stops raising (it resolves to the `openai` transport). `ensure_deps` may now
  install `openai` for a few bare non-Claude judge ids that previously failed at score time.
- `extends:` is a **new, optional** top-level key introduced by PR-3a (no existing config
  uses it; a config without it parses exactly as today). `extends: <path relative to the
  file>` deep-merges the overlay over the base in the single raw loader, so every reader
  sees the merged config and `eval_params.config_chain` records the chain. A file containing
  it is a profile (skipped by discovery, run via `--config`). List merge follows the
  documented `runner.settings` policy (extend) plus dedupe and key-merge for
  `judges`/`steps`; `!replace` opts out. The key is independent of the OpenRouter feature
  and droppable; rfe-creator's generated `eval.yaml` (`scripts/generate_eval_config.py`)
  stays the base an OpenRouter profile extends, so the generator is untouched.
- **rfe-creator migration** (PR-8, its own PR after PR-5 for local + podman and PR-7 for
  K8s; nothing in the harness depends on it). What goes away and what replaces it:

  | Today (proxy-based setup) | After |
  | --- | --- |
  | the proxy config router pins (`config.yaml:34-207`: `order`, `allow_fallbacks: false`, `quantizations`, `require_parameters`) | `models.providers.openrouter.routing.models` in `eval.yaml`, ported **once** with slugs normalised and `require_parameters` dropped from agent keys (unsendable — a WARNING if kept); inert for Anthropic runs. Enforcement: `audit` by default; `key-guardrail` when a management key is available. |
  | the proxy config router retries / first-token timeouts / cooldowns (`config.yaml:216-229`) | none (no proxy) — Claude Code's retries + OpenRouter fallbacks; the case timeout is the bound (Known limitations). |
  | the proxy's `custom_callbacks.py` `chunk_parser` monkeypatch (inline `usage.cost`/`provider`) | `/generation` backfill of the stream-json gen ids (`cost_source: openrouter:generation`) — no code in rfe-creator. |
  | `reconcile_cost.py` from `after_all`/`before_report` + `real_cost.json` | reconcile at every `run_result.json` write inside the harness; `real_cost.json` files stay as history only. |
  | the per-provider config's env block (Vertex blanks, `ANTHROPIC_BASE_URL` → the proxy, `ANTHROPIC_CUSTOM_HEADERS: $EVAL_RUN_HEADER`, aliases) and `max_budget_usd: 100.0` | `eval-profiles/openrouter-glm-5.2.yaml` with `extends: ../eval.yaml` that flips only `models.skill/subagent`, `execution.timeout`, `execution.env: {JIRA_USER: "", JIRA_TOKEN: ""}`, the two *additional* project allows and the MLflow experiment; `max_budget_usd` returns to a real per-case number (the harness applies `cli_budget_inflation`). |
  | the proxy process + its env in `.env` | `OPENROUTER_API_KEY` (and optionally `OPENROUTER_MANAGEMENT_KEY`) exported in the shell; add `.env` to `.gitignore` (currently only `.envrc`, `.gitignore:7`). |

  Delete the proxy directory and the per-provider config copy; move the proxy README's successor
  section into the repo README and retire the proxy-era MEMORY notes. Acceptance: re-run the glm-5.2
  v5 config through the profile; `cost_usd` matches the last proxy-reconciled
  `real_cost.json` within 5 % with `cost_source: openrouter:generation`,
  `cost_confidence: high`, `routing.violations == []` for the ported pins, and the merged
  profile still contains `Skill` and `Agent` in `permissions.allow`.

- **What changes for Harbor users.** Podman: nothing to install or expose — the plan's env
  block travels in `--agent-env` exactly like today's `ANTHROPIC_AUTH_TOKEN` forwarding, the
  key is resolved on the host, and per-trial cost/attribution comes from the trial's captured
  stream-json (probe #25; key-usage fallback otherwise). Host Vertex vars are no longer
  forwarded under a plan (above). K8s: the credentials Secret gains one key,
  `OPENROUTER_API_KEY`; the harness maps it to `ANTHROPIC_AUTH_TOKEN` via `secretKeyRef` and
  writes the non-secret plan env into the pod spec — a Secret that today points
  `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` at an in-cluster Anthropic-compatible endpoint
  keeps working unchanged when no `openrouter:/` role is configured. At `key-guardrail` on K8s the harness creates
  and deletes a per-run Secret (`agent-eval-<run_id>-openrouter`), which needs `create`/
  `delete` on `secrets` in the namespace — a new RBAC requirement documented in
  `guides/harbor.md`. Task packages (`--tasks-dir`) never contain provider env or keys.

## Config examples

Every key below is one of the `OpenRouterConfig` fields in the `agent_eval/config.py`
section; nothing else is accepted (transport keys, `direct` and `budget.max_unpriced*` are
rejected by name; anything else is an unknown key — Config validation).

Minimal — direct, local claude-code, agent and judge on OpenRouter, all defaults
(`enforcement: audit`, `preflight: strict`, `cli_budget_inflation: 50`, no pins):

```yaml
models:
  skill:    openrouter:/z-ai/glm-5.2:exacto     # :exacto = OpenRouter's tool-calling routing variant (VERIFIED accepted); the only in-request routing control
  subagent: openrouter:/z-ai/glm-5.2:exacto
  judge:    openrouter:/openai/gpt-5.2
  providers:
    openrouter: {}          # OPENROUTER_API_KEY must be exported in the shell running /eval-run; the agent talks to https://openrouter.ai/api directly
```

Run start prints `enforcement: audit — key exposed to agent: operator key`; the run ends
with `cost_source: openrouter:generation` and `routing.enforcement: none` (nothing pinned,
nothing to audit).

`audit` vs `key-guardrail` — the same pins, two enforcement levels. The pinned set is
declared once; what differs is who enforces it and which key the agent holds:

```yaml
models:
  skill:    openrouter:/z-ai/glm-5.2
  providers:
    openrouter:
      routing:
        enforcement: audit                         # DEFAULT — preflight checks the pins against the public catalogs; the /generation backfill audits every served provider after the fact
        policy: strict                             # a violation marks the run degraded (--strict-routing exits 2); warn = flagged only
        models:
          z-ai/glm-5.2: { order: [novita, streamlake], allow_fallbacks: false, quantizations: [fp8] }
      budget:
        run_usd: 25.0                              # post hoc at audit: --strict-cost exits 2 when the backfilled Σ exceeds it; no in-flight real-dollar gate
```

```yaml
models:
  skill:    openrouter:/z-ai/glm-5.2
  providers:
    openrouter:
      management_key_env: OPENROUTER_MANAGEMENT_KEY   # NAME of the env var; read once at plan build, never placed in any env target
      routing:
        enforcement: key-guardrail                 # OPT-IN — per-run inference key with allowed providers = pinned set and limit_usd = budget.run_usd; revoked in finally
        policy: strict
        models:
          z-ai/glm-5.2: { order: [novita, streamlake], allow_fallbacks: false, quantizations: [fp8] }
        guardrail:
          key_name: "agent-eval {run_id}"
          providers: pinned                        # union of every routing key's pinned providers (or an explicit list)
          settle_s: 20                             # key-usage settle before the run-end read and the revoke (VERIFIED ~20 s)
      budget:
        run_usd: 25.0                              # REQUIRED here: becomes the per-run key's limit_usd, enforced server-side in flight
```

At `key-guardrail` the agent never holds the operator key (`provider.key_scope: per-run`),
a pin violation 404s at OpenRouter instead of being reported, and a budget breach 402s
(`budget.exceeded_reason: limit_usd`). Guardrail field semantics are DOCUMENTED/UNVERIFIED
(probe #26) until PR-6 lands.

Full block with pins (the pins rfe-creator relied on, ported 1:1 minus
`require_parameters` on agent keys). This block is inert when the roles are Anthropic —
the base `eval.yaml` can hold it for every profile:

```yaml
models:
  skill:    openrouter:/z-ai/glm-5.2
  subagent: openrouter:/z-ai/glm-5.2
  judge:    anthropic:/claude-opus-4-8          # ambient Vertex/Anthropic in the score.py process — untouched
  # hook: openrouter:/z-ai/glm-5.2               # default = skill model when a plan is active
  providers:
    openrouter:
      api_key_env: OPENROUTER_API_KEY              # NAME of the env var; never a value
      management_key_env: OPENROUTER_MANAGEMENT_KEY   # read only at routing.enforcement: key-guardrail
      base_url: https://openrouter.ai/api          # the one base-URL knob ($VAR allowed); no /v1 — Claude Code appends /v1/messages
      attribution:
        referer: https://github.com/opendatahub-io/rfe-creator
        title: rfe-creator eval
        run_id_header: false                       # true adds x-eval-run-id (activity-page tagging only); Referer + Title (+ run id) travel as one multi-line ANTHROPIC_CUSTOM_HEADERS (row 27 VERIFIED)
      background_model: null                       # haiku slot = model under test (default)
      preflight: strict                            # strict | warn | off — the only pre-spend check of the pins on the agent path
      cli_budget_inflation: 50                     # CLI --max-budget-usd = execution.max_budget_usd × 50 (the CLI caps its own inflated estimate)
      budget:
        run_usd: null                              # optional whole-run real-dollar pool: post hoc at audit (--strict-cost), limit_usd of the per-run key at key-guardrail
        dedicated_key: false                       # operator assertion that nothing else spends on OPENROUTER_API_KEY during the run (key-usage confidence)
      routing:
        enforcement: audit                         # audit (default) | key-guardrail
        policy: strict                             # what a failed audit does: strict = run degraded (--strict-routing exits 2); warn = flagged
        defaults: { allow_fallbacks: true }        # require_parameters is NOT here: unsendable from Claude Code (warning if set on an agent key); judges add it themselves when they inherit pins
        models:
          z-ai/glm-5.2:
            order: [novita, streamlake]            # slugs (display names accepted with a warning)
            allow_fallbacks: false
            quantizations: [fp8]
          z-ai/glm-5.3-flash:
            order: [z-ai, novita]
            allow_fallbacks: false
          deepseek/deepseek-v4.1-flash:
            order: [deepseek]
            allow_fallbacks: false                 # preflight fails if the account privacy toggle excludes the endpoint
          moonshotai/kimi-k3:
            sort: throughput                       # judge-only knob (unsendable from Claude Code → WARNING on an agent key; use the :nitro variant on the id instead)
            fallbacks: [z-ai/glm-5.3-flash]        # judge-only: OpenRouter `models` (≤3); the agent path cannot send it
        guardrail:                                 # used only at enforcement: key-guardrail
          key_name: "agent-eval {run_id}"
          providers: pinned
          revoke_on_exit: true
          settle_s: 20
      judge:
        concurrency: 4
        max_retries: 3
        timeout_s: 300
        extra_body: {}
        inherit_pins: false                        # Decision 25: judges ignore routing order/only/quantizations (and send no require_parameters) unless true or the judge sets provider_options.routing

execution:
  max_budget_usd: 5.0                            # per case/step (unchanged contract) — the CLI receives 250.0 (× cli_budget_inflation) and enforces it on its Anthropic-priced estimate; real dollars are bounded by budget.run_usd (post hoc at audit, in flight at key-guardrail)
```

Judge pins opt-in (Decision 25). By default judges send **no** `order`/`only`/
`quantizations` and no `require_parameters` even when the table above pins their slug; the
two ways to opt in:

```yaml
models:
  judge:    openrouter:/z-ai/glm-5.2
  providers:
    openrouter:
      judge:
        inherit_pins: true                         # every openrouter:/ judge whose slug has a routing.models entry sends that entry (+ require_parameters: true) in extra_body.provider
# — or per judge, leaving inherit_pins false:
judges:
  - name: rfe_quality
    model: openrouter:/z-ai/glm-5.2
    provider_options:
      routing: { order: [novita], allow_fallbacks: false, quantizations: [fp8] }   # this judge only; preflight checks supports_tool_choice.function on novita's endpoint, else the required→auto ladder
```

Mixed judges with per-judge routing:

```yaml
judges:
  - name: rfe_quality
    prompt_file: eval/judges/rfe_quality.md
    model: openrouter:/z-ai/glm-5.2
    provider_options:
      routing: { order: [novita], allow_fallbacks: false, quantizations: [fp8] }
  - name: reasoning_quality
    prompt_file: eval/judges/reasoning_quality.md
    model: openai:/gpt-5.2                       # real OpenAI via OPENAI_API_KEY — unchanged PR #216 path
  - name: pairwise_vs_baseline
    pairwise: true
    model: openrouter:/openai/gpt-5.2
```

Overlay profile (kills the eval.yaml / eval-openrouter.yaml fork) using the **new, optional**
`extends:` key of PR-3a. The routing table lives once in `eval.yaml` (above — in rfe-creator
the file generated by `scripts/generate_eval_config.py`, left untouched); the profile flips
roles and adds project glue only:

```yaml
# eval-profiles/openrouter-glm-5.2.yaml
extends: ../eval.yaml            # dicts merge, scalars override, scalar lists EXTEND (dedupe), judges merge by name; !replace to override a list
models:
  skill:    openrouter:/z-ai/glm-5.2
  subagent: openrouter:/z-ai/glm-5.2
execution:
  timeout: 36000
  env: { JIRA_USER: "", JIRA_TOKEN: "" }         # project safety, explicit and small
permissions:
  allow: ["Bash(python3 *)", "Bash(bash *)", "Write(/tmp/rfe-assess/**)"]   # ADDED to eval.yaml's Skill / Agent / Edit(tmp/rfe-assess/**); /private/tmp variants are derived by workspace.py
mlflow:
  experiment: rfe-speedrun-openrouter
```

Run: `/eval-run --config eval-profiles/openrouter-glm-5.2.yaml`; `eval_params.config_chain`
records `["eval.yaml", "eval-profiles/openrouter-glm-5.2.yaml"]`. With the table in the
base, a zero-profile run also works: `/eval-run --model openrouter:/z-ai/glm-5.2` on
`eval.yaml` builds the plan with the pinned `routing_sha`.

Harbor podman — **the same profile, no extra config**. The `models.providers` block has no
runner-specific key; `run_harbor()` picks the `harbor_carrier` env target by itself:

```bash
export OPENROUTER_API_KEY=…                      # resolved on the host, travels in the harbor child env under a carrier name, never in argv
python -m agent_eval.harbor.run --config eval-profiles/openrouter-glm-5.2.yaml \
  --model openrouter:/z-ai/glm-5.2:exacto --env podman \
  --tasks-dir eval/harbor-tasks --jobs-dir eval/harbor-jobs --output eval/runs/glm-podman
# → harbor run … -m z-ai/glm-5.2:exacto --agent-env ANTHROPIC_BASE_URL=${AGENT_EVAL_HARBOR_AGENT_ENV_1} --agent-env ANTHROPIC_AUTH_TOKEN=${AGENT_EVAL_HARBOR_AGENT_ENV_2} --agent-env CLAUDE_CODE_USE_VERTEX=${…} …
#   (run.py:216-233 carriers; Harbor merges --agent-env last, harbor 0.13.1 agents/base.py:288-291; the host's CLAUDE_CODE_USE_VERTEX / ANTHROPIC_VERTEX_PROJECT_ID / CLOUD_ML_REGION are NOT forwarded while the plan is active)
```

`run_result.json` ends with `provider.runner: harbor-podman`, per-trial `cost_usd` from
the trial transcript's gen ids (`cost_source: openrouter:generation`; probe #25 — if Harbor
strips the ids, the run-level key-usage delta is written instead, `openrouter:key-usage`,
per-trial cost `null`). Add `models.judge: openrouter:/…` and the verifier container gets
`OPENROUTER_API_KEY` forwarded (the operator key, at every enforcement level).

Kubernetes with a Secret (zero infra; PR-7). The credentials Secret named by
`AGENT_EVAL_K8S_CREDENTIALS_SECRET` gains one key; the harness does the mapping:

```yaml
# kubectl create secret generic agent-eval-credentials --from-literal=OPENROUTER_API_KEY=…   (plus whatever it holds today)
models:
  skill:    openrouter:/z-ai/glm-5.2:exacto
  subagent: openrouter:/z-ai/glm-5.2:exacto
  providers:
    openrouter:
      routing: { enforcement: audit }              # key-guardrail on K8s additionally creates/deletes the per-run Secret agent-eval-<run_id>-openrouter (RBAC: secrets create/delete)
      budget: { dedicated_key: true }              # a cluster-only key nothing else spends on → key-usage fallback is `medium`, not `low`
```

Pod spec produced (`kubernetes.py _pod_manifest`, :193-268): the plan's non-secret block as
`env[]` entries — `ANTHROPIC_BASE_URL`, blank Vertex/Bedrock lines, the alias vars,
`ANTHROPIC_API_KEY: ""` — plus
`{name: ANTHROPIC_AUTH_TOKEN, valueFrom: {secretKeyRef: {name: agent-eval-credentials, key: OPENROUTER_API_KEY}}}`;
`env[]` wins over the existing `envFrom.secretRef` (:244-246), so a stale
`ANTHROPIC_BASE_URL` in the same Secret cannot re-route. A deployment whose Secret points
Claude Code at an in-cluster Anthropic-compatible endpoint keeps working unchanged **as long
as no role is `openrouter:/`** — that setup is outside this feature (no ledger, `runner:estimate`), not
broken by it.

Environment (harness process): `OPENROUTER_API_KEY` (required only when some role uses
`openrouter:/`; on K8s it lives in the credentials Secret instead), `OPENROUTER_MANAGEMENT_KEY`
(only at `enforcement: key-guardrail`; never forwarded anywhere),
`AGENT_EVAL_OPENROUTER_PREFLIGHT=strict|warn|off` (machine-level override of
`preflight`; config wins when both are set so a run is reproducible from its eval.yaml),
`AGENT_EVAL_K8S_CREDENTIALS_SECRET` (existing). There is no bind address, port or token
env var. CLI: `--model openrouter:/…`, `--judge-model openrouter:/…`, `--strict-cost`,
`--strict-routing`, `--allow-estimate`, `--allow-unaudited` / `--allow-mixed-enforcement` /
`--allow-mixed-cost-sources` (compare/anova),
`python3 -m agent_eval.providers.openrouter.preflight --config eval.yaml`,
`python3 -m agent_eval.config --print <path>`, and the offline
`agent-eval provider backfill|audit|revoke <run_dir>` (module-main form).

## Tests

- `tests/test_prompt_backends.py`: `openrouter:/z-ai/glm-5.2` → `("openai","z-ai/glm-5.2")`;
  `resolve_judge_client` returns a `JudgeClientConfig` for it and `None` for every existing
  row; variants preserved; `openrouter:/` raises; any other non-builtin prefix (e.g.
  `other:/x`) is an **unknown prefix** on judges and agents alike (same error as any unknown
  scheme — no special casing);
  `is_anthropic_model("openrouter:/anthropic/claude-opus-4-8") is False`; every pre-existing
  row unchanged.
- `tests/test_providers.py` (new): `parse_agent_model`/`routing_key` (variants, `[1m]`,
  `openrouter/` prefix); **direct env-template conformance** — `settings_env_block(plan)`
  exact keys/values for the three targets `overlay` / `harbor_carrier` / `k8s_pod` at both
  enforcement levels: `ANTHROPIC_BASE_URL == "https://openrouter.ai/api"` (no `/v1`),
  `ANTHROPIC_API_KEY == ""`, every Vertex/Bedrock key `== ""` (blank, present), the four
  `ANTHROPIC_DEFAULT_*` aliases + `ANTHROPIC_MODEL` = skill `slug:variants` (`:exacto`
  preserved), `CLAUDE_CODE_SUBAGENT_MODEL` = subagent, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC == "1"`,
  `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY` absent; `secrets="ref"` emits
  `$OPENROUTER_API_KEY` only, `"literal"` the plan key, `"omit"` no `ANTHROPIC_AUTH_TOKEN`;
  at `key-guardrail` the token is the per-run key on every target; alias rule (haiku =
  skill by default, = `background_model` when set); display-name→slug warning;
  `MANAGED_ENV_KEYS ==` the union of every emitted key; **the three targets emit the same
  key set except `ANTHROPIC_AUTH_TOKEN` on `k8s_pod`**; `ProviderPlan.__repr__` and
  `ProvisionedKey.__repr__` contain the hash, never the key.
- `tests/test_env_writers_conformance.py` (new; PR-5 for the overlay/`--agent-env`/interception
  legs, PR-7 adds the K8s `_pod_manifest` leg): the **cross-writer conformance test** —
  one config run through the claude-code overlay (`_write_settings_overlay`), harbor's
  `_resolve_harbor_agent_env` + `_harbor_agent_env_args` (**`--agent-env`**), kubernetes
  `_pod_manifest` `env[]`, and `interception.generate_interception` yields: identical
  non-secret env across overlay/carrier/pod, `$VAR` never baked, `None` never written,
  **every `MANAGED_ENV_KEYS` entry absent from the task package** (interception), the
  carrier argv value-free (`KEY=${AGENT_EVAL_HARBOR_AGENT_ENV_n}`), the pod `env[]` carrying
  `secretKeyRef` for `ANTHROPIC_AUTH_TOKEN` and no literal token; a fixture whose workspace
  settings.json (`runner.settings.env` and a repo-mode pre-existing env) carries conflicting
  `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_DEFAULT_OPUS_MODEL` still yields
  overlay managed keys `== settings_env_block(plan)`; the Harbor child-env scrub removes
  exactly `MANAGED_ENV_KEYS ∪ {api_key_env, management_key_env}` (incl. `ANTHROPIC_BASE_URL`),
  `api_key_env` being retained only when an `openrouter:/` judge is configured (verifier carrier).
- `tests/test_openrouter_routing.py` (new): merge order (defaults ← model ← role), lists
  replace (RoutingSpec only), slug normalisation via a saved `/providers` fixture,
  `routing_sha` stability; `to_chat_extra_body(role="judge")` equals `to_messages_body()`
  only when `judge.inherit_pins: true` or the judge sets `provider_options.routing`, and
  otherwise drops `order`/`only`/`quantizations`; `require_parameters` present for judges
  only when the judge routing carries pins and absent for an unpinned judge; display-name
  → slug normalisation emits a warning; `to_messages_body()` is **never called on the agent
  path** (a test asserts the plan/env layer has no reference to it — nothing is sent);
  agent-key `require_parameters`/`sort`/`fallbacks` produce the "not sendable from Claude
  Code" WARNING and are dropped from `pinned_set`; `routing_sha` is stable across the
  dropped keys.
- `tests/test_openrouter_audit.py` (new): `routing_audit(rows, snapshot, catalog)` over
  saved `/generation` + `/endpoints` + `/providers` fixtures — a row whose `provider_name`
  (display name) maps to a pinned slug at the pinned quantization → `compliant`; outside
  the pinned set → **`violations`** entry `{gen_id, case_id, provider, quantization,
  expected}`; pinned provider at a non-declared quantization → violation; a provider
  serving the same permaslug at two quantizations → `quantization: null` + warning, snapshot
  tiebreak; `backfill_failed` rows → `unattributed`, **excluded** from `violations`;
  `policy: strict` + any violation/unattributed → `degraded: true`, `warn` → `degraded_reason`
  only; `audit_complete: false` while any id is `backfill_failed`; unpinned routing key →
  `enforcement: none`; `preflight: off` → join against a reconcile-time catalog with
  `snapshot: null`; compare/anova pooling refusal when `sha` differ or the audit is unclean
  and acceptance with `--allow-unaudited`.
- `tests/test_openrouter_preflight.py` (new): saved `/endpoints` fixture for `z-ai/glm-5.2`
  → fp8 filter; **per-role `supports_tool_choice`** — agent slugs need `auto` (a pinned
  endpoint lacking it → strict FAIL naming the endpoint), judge slugs need `function` (a
  pinned endpoint lacking it → WARN + the Decision 25 ladder; a pinned set where **none**
  supports it → strict FAIL / warn continues); `supports_tools` required for every pinned
  agent endpoint; `max_completion_tokens < 32000` warn; `status < 0` excluded under strict,
  warn otherwise; `/models/user` exclusion → strict FAIL quoting `ineligibility_reasons`
  (the deepseek-v4.1-flash paid-training fixture) / warn continues; catalog fetch failure
  → degrade to warn with reason while key/slug/eligibility stay strict; `:exacto`
  with no pinned endpoint in the Exacto set → warn only; at `key-guardrail` the `/key` and
  `/models/user` calls use the per-run key (asserted on the fake server's `Authorization`);
  the PR-5 minimum (slug exists, pinned providers serve the model, key valid (`GET /key`), `/models/user` eligibility (paid-training case → strict FAIL / warn), catalog-failure degrade, `routing_snapshot.json` written) is a separate parametrised subset so the MVP can ship it alone; snapshot content
  `{ts, routing_sha, enforcement, key_scope, keys{…pinned_set, eligible, excluded, catalog}, pricing}`
  (full PR-6 form; the PR-5 subset asserts only `{ts, routing_sha, keys{variant, pinned_set, catalog}}`).
- `tests/test_openrouter_keys.py` (new, PR-6; **fake management API** on loopback, plain
  `http.server`, no OpenRouter key): `provision()` sends `POST /api/v1/keys` with the
  management key as Bearer, `name == "agent-eval <run_id>"`, `limit == budget.run_usd` and
  the allowed-provider list == the union of every routing key's pinned providers (or the
  explicit `guardrail.providers` list) — the request-builder function is the **only** place
  the probe-#26 field names live, and the test pins them so the probe's answer is a
  one-function change; `verify_guardrail()` read-back mismatch (server echoes a different
  provider list or limit) → `ConfigError` before any spend; `revoke()` sends
  `DELETE /api/v1/keys/{hash}`; `key.json` holds hash/name/limit/timestamps and never the key
  (grep of the run dir for the fake key value → 0 hits); **revocation on every exit path**:
  normal end, `KeyboardInterrupt` and an unhandled exception injected mid-run, `SystemExit`,
  and an `atexit`-only path (plan never `close()`d) each produce exactly one `DELETE`;
  a failed `DELETE` (503) → stderr ERROR + `cost_warnings` naming the hash, `revoked_at: null`,
  and `agent-eval provider revoke <run_dir>` retries it; the management key is absent from
  every env target, the overlay, the hook env, the harbor child env and the pod manifest;
  the per-run key (not the operator key) is what `settings_env_block(plan)` carries and what
  the backfill/`/key` client authenticates with; a stream-json `error` carrying the key's
  402 → `budget.exceeded: run`, `exceeded_reason: limit_usd`; validation: `key-guardrail`
  without `budget.run_usd`, without pins/explicit providers, or with `management_key_env`
  unset at plan build → the documented errors, value never echoed.
- `tests/test_providers_ledger.py` / `test_providers_reconcile.py` (new, parameterised over
  `enforcement: audit` × `key-guardrail` × runner label `claude-code` / `harbor-podman` /
  `harbor-k8s`, plus the two no-ledger cases — provider inactive, and a plain
  `execution.env` pointing `ANTHROPIC_BASE_URL` at a non-Anthropic host — whose only
  reconcile assertion is `untouched` in both; the latter's `cost_source: runner:estimate` is
  the runner's own label, asserted in test_claude_cli_direct, not here):
  schema round-trip incl. `gen_id`/`message_index`/`model_echo`/`model_served`/`provider_kind`/
  `source ∈ {generation, key-usage, judge}`; `status: ok | backfill_failed`; `error_message`
  truncation and `metadata` stripping; reconcile rules per source — **coverage ≥ 0.8 → Σ
  generation rows (`openrouter:generation`), < 0.8 → key-usage delta (`openrouter:key-usage`),
  neither → `unavailable` (or `runner:estimate` only with `--allow-estimate`), never a mix,
  never the estimate**; `cost_confidence` by coverage (≥ 0.95 + cross-check within 5 % →
  `high`; [0.8, 0.95) → `medium`; < 0.8 non-dedicated → `low`; ≥ 0.8 with > 5 % deviation →
  `low`); estimate preserved and idempotent; **per-model join**: bare-slug echo (probe #12
  fixture), `:variant`/`[1m]` request → bare echo, permaslug via catalog, single-model
  fallback, unmatched key → `null` + warning, ledger cost without key → warning;
  `providers` incl. `unknown`; `backfill_failed` → `routing.unattributed`, `degraded` under
  strict, `cost_confidence` unchanged by the audit (independence); key-usage cross-check
  warning; inconsistent delta (zero or below rows × min price) → warning, never a small
  positive; `dedicated_key` gating of `medium`; `hook_cost_usd` excluded from `cost_usd`;
  legacy `openrouter-reconciled`/`runner-reported` recognised; `budget` per case and run
  incl. `enforcement: cli-estimate | key-guardrail`, post-hoc `exceeded: run` when Σ >
  `run_usd`, `limit_usd` from a 402 in the error output; `provider` block
  (`transport: direct`, `key_scope`, `key_hash` = 8 hex, `key_exposed_to_agent: true` at
  both levels); **null-cost arithmetic**: `total_cost_usd` is `null` with
  `total_cost_source: judge-only` when `cost_usd` is `null` and never equals
  `cost_usd_estimate + judge_cost_usd`; case aggregate with one `null` case → `null`.
  `tests/test_judges_builtin.py` (extend): `cost_budget` returns `(None, …)` on
  `cost_source: unavailable` (aggregated as skipped, not failed) and labels estimates.
  `tests/test_log_results.py` / `test_trace_builder.py` (extend): Harbor step cost comes
  from the reconciled join, `null` per-model cost sets no span cost, estimate never leaks
  into `mlflow.llm.cost`.
- `tests/test_openrouter_generation.py` (new in PR-4 against a stub `/generation`; the
  worker wiring is exercised from PR-5): **backfill backoff** with the probe-#6 defaults —
  first poll 5 s after sighting, 2 s steps on 404, give up at 60 s → `backfill_failed`
  (id kept) + run-end retry; a stub that answers 200 only after 9 s (the VERIFIED lag) is
  backfilled **without stalling the case** (the runner's `execute()` returns before the row
  lands; the progress line shows `[cost pending: N ids]` then converges); 429 honours
  `Retry-After`; 5xx same cadence; 401/403 abort the worker with `error_class: config` and
  no further GETs; concurrency bounded to `min(8, parallelism × 2)`; ids are fed **as
  sighted** by the stream-capture callback (a slow stream with 20 messages produces 20 GETs
  before the process exits) and by `results.py` on Harbor; **coverage math** over
  `RunResult.message_ids` (denominator = transcript ids, not ledger rows); non-`gen-`
  ids never queried; `/generation provider_name` display-name → slug via the catalog;
  `key_usage_delta` settle logic (first read at plan build, run-end read after ≥ 20 s,
  polled until stable for two polls, 60 s max); source selection per Reconcile (coverage
  ≥ 0.95 → `openrouter:generation` + `high`; < 0.8 → `openrouter:key-usage`); the offline
  `agent-eval provider backfill <run_dir>` re-runs the failed ids and readers re-reconcile;
  the request never carries the management key and never logs headers.
- `tests/test_score_builtin.py` (extend :1340-1445 pattern): openrouter dispatch goes through
  the `openai` transport with `client=_client_for(cfg)`, captured kwargs contain
  `extra_body.provider.require_parameters` and `provider.order/quantizations` from config
  only when the fixture sets `judge.inherit_pins: true` (or `provider_options.routing`), and
  none of the three for the default unpinned judge,
  `max_tokens` (not `max_completion_tokens`) for `openai/gpt-5.2`; dict-valued
  `function.arguments`; `finish_reason == "error"` raises `JudgeProviderError`; Decision 25
  ladder: a pinned judge whose endpoint lacks `function` forcing (fixture) or a first-attempt
  404 `not_found` "No endpoints found" retries once with `tool_choice: "required"` (when the
  endpoint supports it) else `"auto"`, strict-parses the first `tool_calls` entry (name must
  match, arguments must validate) and records `tool_choice_mode: required|auto` on the judge
  usage/ledger record with `judge_usage.tool_choice_fallbacks` incremented; a mismatched or
  unparsable tool call and a routing 404 that persists after the ladder raise
  `JudgeProviderError` (never the text fallback); the 404 is `error_class: config`, not
  retried by `_with_judge_retries` and not counted in `infra_errors`; an unpinned judge
  sends no `require_parameters`; usage captured
  via `getattr` from a plain-object fake response (no pydantic internals); fake `openai`
  module test for `_client_for` (base URL `cfg.base_url + "/v1"`, headers, `max_retries`/`timeout`,
  `RuntimeError` without the key via `monkeypatch.delenv`, message has no value);
  `provider_options` validated by kind, `max_tokens` precedence.
- `tests/test_pairwise_providers.py`: `compare_runs("openrouter:/x")` uses `_client_for`;
  anthropic getter and bare `_get_openai_client` raise if called; `extra_body` passed.
- `tests/test_llm_rubric_scoring.py`, `test_score_range_enforcement.py`: three-way dispatch
  with an OpenRouter client config; 3-tuple `_normalize_result` with legacy 2-tuple/`Feedback`;
  per-case usage summed over samples incl. failed attempts; `summary.judge_usage`/`total_cost_usd`;
  `run_result.cost_usd` untouched by judge cost.
- `tests/test_config.py`: `OpenRouterConfig` defaults (`enforcement: audit`, `policy: strict`,
  `preflight: strict`, `cli_budget_inflation: 50`, `judge.inherit_pins: false`, guardrail
  defaults); `providers` parsed from `models.providers` into `ModelsConfig.providers`, a
  top-level `providers:` key rejected with the Decision 17 pointer, a `models:` block without
  `providers` unchanged; `kind` must equal name; the **single kind** — `kind:
  openai-compatible` and any other `models.providers.*` name rejected; **reserved keys**
  (transport mode/options keys, `direct`, `budget.max_unpriced`, `budget.max_unpriced_ratio`)
  rejected by name with the Decision 1 pointer, one error
  listing every offender; `enforcement`
  enum; `key-guardrail` requires `budget.run_usd > 0` and pins or an explicit
  `guardrail.providers` list; `guardrail.settle_s < 20` warns; agent-key
  `require_parameters`/`sort`/`fallbacks` warn; `api_key_env`/`management_key_env` values
  that look like `sk-or-…` rejected without echoing; `extends` (relative path, extend-with-dedupe, judge
  merge-by-name, steps merge-by-id, `!replace`, cycle detection, `config_chain`,
  `config_dir` from root, `eval_name` from base); every rejection listed under Config
  validation with its label, including `runner.settings.env.ANTHROPIC_BASE_URL` with a plan
  (error) and an identical Vertex-blank value (loads), presence-rejection of
  `ANTHROPIC_AUTH_TOKEN`, per-step surfaces, the bare-id footgun, and the consolidated message
  naming every `<surface>.<KEY>`; base eval.yaml with Anthropic roles + routing table loads;
  judge-only openrouter config + block builds no agent plan.
- `tests/test_config_raw_readers.py` (new): greps `agent_eval/`, `scripts/` and
  `skills/*/scripts/` for `yaml.safe_load(` applied to an eval-config path outside `load_raw`
  (explicit allow-list for non-config YAML: `tools.py:57` tool_handlers.yaml, dataset
  `input.yaml`, `summary.yaml`, manifests, frontmatter, validate_eval's syntax-only first pass)
  — fails on any new raw reader; `discover_configs` skips profiles by default and returns them
  with `profile_of` when asked; `ensure_deps` scan with `eval/openrouter-x.yaml` overlay
  adding `models.judge: openrouter:/…` triggers the `openai` install (both the `load_raw`
  path and the stdlib fallback).
- `tests/test_execute_result_fields.py`, `test_extract_progress.py`: URI split for
  `--model`; `--max-budget-usd` = cap × `cli_budget_inflation` at both enforcement levels,
  a cap `≤ 0`/`None` **omits the flag** (the CLI rejects `--max-budget-usd 0`, probe #23;
  `cli_cap_usd: null`), and `eval_params.budget.{cli_cap_usd,
  invocation_usd, run_usd, enforcement}`; activation rule (plan iff the effective agent URI
  is `openrouter:/`; `--model openrouter:/…` on a base with the routing table builds the
  plan with the pinned `routing_sha`; unused block → warning, not error); startup order
  `build_plan → preflight → key-usage before-read → stderr enforcement line → worker`
  and the `finally: plan.close()` running **after** `after_all` hooks; run_result provenance
  fields; `est. $` progress label and the `[cost pending: N ids]` / `[routing: N
  violations]` / `[budget: limit_usd]` markers derived from the ledger at write time; legacy
  fields untouched for Anthropic runs; `.eval-overlay.json` env precedence
  `{**existing, **provider}`, mode 0600, removed in `finally` (normal, timeout,
  `KeyboardInterrupt`); `_build_env` strips `MANAGED_ENV_KEYS` only when a plan is active
  and never forwards `OPENROUTER_API_KEY`/`OPENROUTER_MANAGEMENT_KEY`; `--strict-cost`
  exits 2 on `unavailable` and on post-hoc Σ > `run_usd`; `--strict-routing` exits 2 on
  violations / incomplete audit; `--allow-estimate` writes `runner:estimate`;
  **crash/step-path cases**: a runner that raises in case mode (`:1117`), a
  multi-step run whose step 2 fails (`:1392`) and a batch case that raises in the pool
  (`:1679`) each leave a `run_result.json` carrying `cost_source` and `cost_usd_estimate`.
- `tests/test_execute_write_sites.py` (new, **write-site guard**, grep-based): reads
  `skills/eval-run/scripts/execute.py` and asserts (a) every line matching
  `run_result\.json` that is not inside `write_run_result` (or a comment/log string) is
  zero — i.e. `open(`/`write_text(`/`json.dump(` never target that filename elsewhere;
  (b) the count of `write_run_result(` call sites is ≥ 8 and each of the eight enumerated
  paths (execute.py section) is covered by a named marker comment `# run_result write-site N`
  next to the call, so a renumbering of line anchors fails the test loudly rather than
  silently; (c) `write_run_result` calls `reconcile(` exactly once; (d) the same filename
  grep over `agent_eval/harbor/run.py` finds only the helper call. Runs without a key.
- `tests/test_stream_capture.py`: `message_ids` returned for root + subagent transcripts
  (probe #12 fixture: 2 ids per turn incl. the `generate_session_title` call); the
  per-message callback fires once per id **while the stream is read** (order preserved,
  before `extract_usage` returns) and is a no-op when no plan is installed; non-`gen-` ids
  (Anthropic/Vertex fixture) are collected but never handed to the worker; `error` events
  classified via `errors.py` into `RunResult.error_class`, the key-limit 402 into
  `budget.exceeded_reason: limit_usd`; `extract_usage` stays pure for callers without a plan.
- `tests/test_harbor_run.py` (**podman env forwarding under a plan**, PR-5): the plan's
  block is merged **last** in `_resolve_harbor_agent_env` (over `execution.env` and
  `runner.env`) and reaches `--agent-env` as value-free carriers
  (`KEY=${AGENT_EVAL_HARBOR_AGENT_ENV_n}`, values in `child_env`, `_display_command`
  redacted); the carrier set equals `settings_env_block(plan, secrets="ref",
  target="harbor_carrier")` with `$OPENROUTER_API_KEY` resolved from the host; `-m` gets the
  bare `slug:variants`; **host Vertex vars are not forwarded**: with
  `CLAUDE_CODE_USE_VERTEX=1`, `ANTHROPIC_VERTEX_PROJECT_ID`, `CLOUD_ML_REGION`,
  `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` set on the fake host
  env, the `child_env` handed to `Popen` contains none of them and no
  `OPENROUTER_MANAGEMENT_KEY`, and no `OPENROUTER_API_KEY` unless an `openrouter:/` judge is
  configured (then present, as the verifier's carrier only), while the same keys appear blank in
  `--agent-env`; without a plan the child env is `os.environ.copy()` as today (tests
  :336-366 unchanged); no listener, no port, no reachability probe (a test asserts
  `run_harbor` opens no socket); preflight runs before task generation and a strict
  failure exits before `harbor run`; `plan.close()` runs in `finally` around task
  generation → `_write_report`, also on a simulated `KeyboardInterrupt` during `harbor run`;
  `write_run_result()` is called before `run_meta` (the ninth write site) and `run_meta`
  has `cost_source`/`routing`/`provider`; K8s path (PR-7) drops the `ANTHROPIC_AUTH_TOKEN`
  carrier; `config_chain` reuse refusal; a package built under a plan equals one built
  without (no `provider_sha`); overlay config passes both the reuse check and the
  `--no-llm-judges` pre-check.
- `tests/test_harbor_task_generation.py`: task settings.json contains **no** `MANAGED_ENV_KEYS`
  entry, no `$VAR` literals, no `None`, no `ANTHROPIC_CUSTOM_HEADERS` (nothing per-case is
  baked; attribution is per trial via gen ids); the skip list is imported from `env.py`,
  not duplicated; bundled `tests/eval.yaml` of an `extends:` overlay without `judges:`
  contains the base judges, dataset, thresholds, `models.providers.openrouter` (judge routing,
  `inherit_pins`) and no `extends` key; `task.toml` metadata carries `config_chain`.
- `tests/test_harbor_podman.py`: `test_podman_forward_excludes_under_plan` — `_start_container`
  with `exclude = MANAGED_ENV_KEYS ∪ {api_key_env, management_key_env}` (minus `api_key_env`
  when an `openrouter:/` judge is configured) builds the forwarded
  dict from a fake host env containing `CLAUDE_CODE_USE_VERTEX=1`,
  `ANTHROPIC_VERTEX_PROJECT_ID`, `CLOUD_ML_REGION` and asserts all three are absent, while
  `_FORWARD_ENV` itself is unchanged (no plan → forwarded as today); `OPENROUTER_API_KEY`
  forwarded **only** when an `openrouter:/` judge is configured, never the management key;
  `NODE_EXTRA_CA_CERTS`/`SSL_CERT_FILE`/`HTTPS_PROXY`/`NO_PROXY` pass through when set; the
  `AGENT_EVAL_PODMAN_GCP_CREDENTIALS_FILE` mount is skipped under a plan.
- `tests/test_harbor_k8s_resources.py`, `test_harbor_kubernetes.py` (PR-7): `_pod_manifest`
  under a plan carries the `k8s_pod` block as `env[]` (blank Vertex lines included) plus
  `ANTHROPIC_AUTH_TOKEN` via `secretKeyRef {name: $AGENT_EVAL_K8S_CREDENTIALS_SECRET, key:
  OPENROUTER_API_KEY}` at `audit` and `{name: agent-eval-<run_id>-openrouter}` at
  `key-guardrail`; no literal token anywhere in the manifest; `_FORWARD_ENV` (:42-50) yields
  an empty forwarded set under a plan; no `export ANTHROPIC_AUTH_TOKEN=` in the `exec()`
  prefix (:541-548); `create_openrouter_secret`/`delete_openrouter_secret` shape and the
  delete being invoked from `plan.close()`; a config with no `openrouter:/` role produces
  today's manifest byte-for-byte.
- `tests/test_harbor_results.py`: `_extract_transcript_metrics` returns `message_ids` +
  `model_echo` from a captured Harbor stream-json fixture (the shared id rule with
  stream_capture.py); per-trial ledger join is exact by gen id (two trials of one case never
  share a row; no time-window split); per-step `cost_usd` by `step_id`; job totals carry
  `cost_source`/`cost_coverage`/`providers`/`routing`/`budget` from one run-level reconcile;
  **probe #25 fallback**: a fixture transcript with the ids stripped yields per-trial
  `cost_usd: null` + estimate preserved, run-level `openrouter:key-usage`, `audit_complete:
  false`, `routing.unattributed == requests`, and the banner text.
- `tests/test_report_markdown.py`, `test_report_attribution.py`, `test_compare.py`,
  `test_anova.py`, `test_log_results.py`: new rows/banner incl. `Cost source`, `Cost
  confidence`, `Budget` enforcement (`cli-estimate` vs `key-guardrail`), `Hook cost`,
  `Routing` (enforcement level, served providers, violations count) and the
  `key_exposed_to_agent` notice; mixed-source refusal with legacy literals; **audit-based
  pooling refusal** in compare/anova (different `routing.sha`, or violations / incomplete
  audit on either side) with the `--allow-unaudited` override and the reason printed;
  snapshot diff between two runs' `routing_snapshot.json`; overlay config renders the base
  judge table.
- `tests/test_secrets_hygiene.py` (new; **audit-mode hygiene** — the level at which the
  agent legitimately holds the operator key): export fake `OPENROUTER_API_KEY`, fake
  `OPENROUTER_MANAGEMENT_KEY`, fake `ANTHROPIC_API_KEY`, fake `ANTHROPIC_AUTH_TOKEN` and
  `CLAUDE_CODE_USE_VERTEX=1` on the host; run an `audit` eval with a stub `claude` that
  dumps its env and a local echo server as `base_url`; assert (a) the child env contains
  none of the fake Anthropic values, no `OPENROUTER_API_KEY` under that name and no
  management key — the OpenRouter value appears **only** as `ANTHROPIC_AUTH_TOKEN`, (b) the
  echo server sees it only in `Authorization: Bearer` and `x-api-key` is empty/absent,
  (c) the value exists on disk only in `<ws>/.claude/.eval-overlay.json` (0600) for the
  lifetime of the CLI process — absent from the run dir (`run_result.json`, `eval_params`,
  `ledger.jsonl`, `routing_snapshot.json`, `key.json`), stderr/stdout, the report and the
  hook env (`build_hook_env` drops both key names) — and `key_hash` is the only trace;
  (d) the overlay is gone after a normal run AND after an injected `KeyboardInterrupt`
  (the `finally:`); (e) `provider.key_exposed_to_agent: true`, `key_scope: operator` are
  recorded and the startup stderr line names it. A `key-guardrail` variant (fake management
  API) asserts the child env holds the **per-run** key, never the operator key, and the
  management key reaches no subprocess.
- `tests/test_ensure_deps.py`, `test_synthetic_generation.py`: alignment (`openai` for
  `openrouter:/` judges; **no `httpx`/`openrouter` extra** — the backfill/catalog/keys client
  is stdlib `urllib.request`, asserted by importing `agent_eval.providers` with `httpx`
  blocked in `sys.modules`), profile follow, and rejection of `openrouter:/` for synthetic
  generation.
- `tests/test_claude_cli_direct.py` (new; **Claude CLI in the loop against a local
  echo server** — marker `claude_cli` registered in pyproject `markers`, **not** in the
  default `-m 'not e2e'` deselect: it runs whenever `shutil.which("claude")` succeeds, i.e.
  on every developer machine and in the Containerfile CI image that pins
  `@anthropic-ai/claude-code`; `pytest.skip` otherwise). No key, no network: a loopback
  `http.server` that replays the recorded `/v1/messages` SSE fixtures and records every
  request (path, headers, body) stands in for `openrouter.ai` via `base_url`;
  `CLAUDE_CONFIG_DIR=<tmp>` so user settings cannot leak, `ANTHROPIC_API_KEY` unset,
  `DISABLE_TELEMETRY=1`, 60 s per case. The test is the CI form of the real
  `specs/014-openrouter-provider/probes/probe_claude_cli.py` (PR-0, a spec-local verification
  artefact that the harness suite does not import): the test carries its own copy of the
  fake-endpoint server and request recorder, and the assertions below are the facts the script established on Claude
  Code 2.1.273+ (`probes/probe_cli_report_2026-09-16.json`) — note the CLI posts to
  `/v1/messages?beta=true`, so route matching strips the query string. Automates probes 1, 9,
  10, 16 (echo-server half), 22, 23, 24 and checklist row 27 (multi-header
  `ANTHROPIC_CUSTOM_HEADERS`) against the
  real client through the **real** `_write_settings_overlay` + `_build_env` path: (a) the
  overlay env block beats a `CLAUDE_CODE_USE_VERTEX=1` user settings file in the tmp config
  dir — the echo server receives the request with `Authorization: Bearer <dummy>` and an
  empty/absent `x-api-key` (probe 1, CLI half); (b) a prompt that spawns one `Task` →
  the subagent request also reaches the echo server with the same auth and
  `CLAUDE_CODE_SUBAGENT_MODEL` as its `model` (probe 1, subagent half — VERIFIED on
  2.1.273+, kept green here); (c)
  fixture `basic` replayed → exit 0, text on stdout, `RunResult.message_ids == ["gen-REDACTED-1", …]`
  (the fixture ids) and the backfill worker issued one `GET /api/v1/generation?id=` per id
  against the same echo server (which answers 404 once, then 200 — the backoff path with
  the real worker); (d) the three-line `ANTHROPIC_CUSTOM_HEADERS` value (`HTTP-Referer`,
  `X-OpenRouter-Title`, `x-eval-run-id`) arrives as three separate headers on root **and**
  subagent requests, and the single-header form likewise (rows 27 and 9, both VERIFIED on
  2.1.273+ — asserted, PR-5); (e) `count_tokens` is answered 404 and the test **asserts zero
  calls** to the route across a run with a tool call and a subagent (probe 22, RESOLVED for
  2.1.273+ — a CLI upgrade that starts calling it fails here, which is the watch item);
  (f) `--max-budget-usd 0` is rejected by the CLI (exit 1, no request, probe 23) and the
  runner's mapping omits the flag for a cap `≤ 0`/`None` — asserted on the recorded argv;
  (g) 503 + `Retry-After: 1` → attempt count and spacing recorded (probe 24 — Claude
  Code's only retry layer); (h) a 402 with OpenRouter's key-limit body →
  `RunResult.error_class: config`, `budget.exceeded_reason: limit_usd` (the `key-guardrail`
  detection path, fixture-driven); (i) a hook child (`AskUserQuestion` through tools.py)
  inherits the overlay env and reaches the echo server with the same base URL (probe 10);
  (j) **no plan**, plain `execution.env` pointing `ANTHROPIC_BASE_URL` at the echo server →
  the runner's own result carries `cost_source: runner:estimate` (host ≠ `api.anthropic.com`)
  and reconcile leaves the file untouched; the same env with `ANTHROPIC_BASE_URL` unset →
  `runner:reported`.
  Failures point at a Claude Code behaviour change and block the PR the probe gates.
- Recorded SSE golden fixtures: `tests/data/openrouter/messages_stream_<shape>.sse`
  + `.headers.json` sidecar for shapes `basic`, `tool_use`, `thinking`, `keepalive_heavy`,
  `error_mid_stream`, `fallbacks_served`, plus `messages_nonstream.json` and
  `chat_completions_stream.sse`. Produced only by `specs/014-openrouter-provider/probes/probe_openrouter.py --record-fixtures`
  (PR-0, KEY) from live `/v1/messages` streams; redaction keeps event order, event names,
  every `usage`/`cost`/`provider`/`openrouter_metadata`/`endpoints` field and comment lines
  verbatim, rewrites `id`s to `gen-REDACTED-<n>` and text/thinking deltas to same-length
  filler, strips `x-request-id`. Each file starts with `: aeh-fixture v1 recorded=<date>
  probe=3 sha=<12 hex of the redacted body>`; `tests/test_fixtures_lint.py` fails on a
  missing header or a sha mismatch (hand edits). Consumers: the echo server of `test_claude_cli_direct.py` (replays them to the real CLI), `test_stream_capture`
  (gen-id extraction from the stream-json the CLI produces over them) and the judge tests
  (`chat_completions_stream.sse`). A second family, `tests/data/openrouter/generation_<shape>.json`,
  `key.json`, `endpoints_<slug>.json`, `providers.json`, `models_user_<case>.json`, is recorded
  by the same script from the REST endpoints (redacted ids/labels, exact numeric fields) and
  feeds the generation, preflight, audit and reconcile tests — so the join rule, the
  display-name→slug mapping and the `ineligibility_reasons` text come from captures, not
  from the tests' author.
- Raw-reader and bundle lints: `tests/test_config_raw_readers.py`
  (grep guard for `yaml.safe_load` outside `load_raw`), `test_harbor_task_generation`
  (overlay `extends` bundles the merged mapping, no `extends` key) and
  `tests/test_execute_write_sites.py` (every `run_result.json` writer goes through
  `write_run_result` → `reconcile`) form the "no new bypass" trio; a PR touching a config
  reader, the bundler or a result writer must keep all three green.
- `tests/e2e/` (opt-in, skipped without `OPENROUTER_API_KEY`; the Harbor one additionally
  without `podman`): (1) **local direct**: one `claude --print` tool-calling case on a pinned
  slug at `enforcement: audit` → `cost_source: openrouter:generation`, `cost_confidence:
  high`, every ledger row `status: ok` with `provider`, `model_served` permaslug and
  `cost_usd`, `routing.violations == []`, `audit_complete: true`, key-usage delta within 5 %;
  (2) **direct e2e via Harbor podman with tools** (checklist row #2): the same case
  through `agent_eval.harbor.run --env podman` → the trial transcript yields gen ids (probe
  #25 evidence is *this* test's artefact), per-trial `cost_usd` non-null, no host Vertex var
  inside the container (`env` dumped by the task), `provider.runner: harbor-podman`;
  (3) one openrouter judge call verifying `usage.cost` and, on a pinned judge slug lacking
  `function` forcing, the `required`/`auto` ladder; (4) with `OPENROUTER_MANAGEMENT_KEY`
  also exported: a `key-guardrail` run that provisions, uses and revokes a per-run key
  (`GET /api/v1/keys/{hash}` → 404 afterwards) and whose `/key` delta equals Σ rows exactly.

## Known limitations

- OpenRouter's own cookbook states Claude Code via OpenRouter "is only guaranteed to
  work with the Anthropic first-party provider"; non-Anthropic models are best-effort.
  Tool-call/thinking translation regressions surface as agent failures classified from
  Claude Code's own error output (`RunResult.error_class`, `errors.py`) — never as ledger
  rows, since the harness is not on the HTTP path — not as harness bugs. Probe #12 showed
  one such wrinkle already (a harmless `unrecognized_model` stderr line for the
  `generate_session_title` call).
- **No per-request body pins on the agent path.** Claude Code cannot send
  `provider.order/only/ignore`, `quantizations`, `require_parameters` or `models`; the only
  in-request routing control is the model-suffix variant (`:exacto`, `:nitro`, `:floor`).
  What the declared pins buy at `enforcement: audit` is a **preflight** (are the pinned
  providers able to serve this at all) and a **post-hoc audit** (did they); a request served
  by a provider outside the set is billed, kept and reported as a `routing.violations`
  entry, never prevented. Only `key-guardrail` turns pins into a server-side refusal — and
  its field semantics are DOCUMENTED/UNVERIFIED until probe #26 (PR-6). Per-request body
  pins are listed under "Out of scope (future)".
- **Real-cost budget is post hoc at `audit`.** The only in-flight cap is the CLI's
  `--max-budget-usd` on its Anthropic-priced estimate (× `cli_budget_inflation`, i.e.
  deliberately loose); `budget.run_usd` is checked against the backfilled Σ at reconcile
  (`budget.exceeded: run`, `exceeded_reason: post-hoc`, `--strict-cost` exits 2) — after the
  money is spent. A runaway case is bounded by the case/step timeout and the loose CLI cap,
  not by dollars. `key-guardrail`'s `limit_usd` is the supported in-flight real-dollar bound;
  it covers agent spend only (judges use the operator key) and its hard-402 behaviour is
  part of probe #26.
- **The operator key is exposed to the agent at `audit`.** `ANTHROPIC_AUTH_TOKEN` in the
  agent's environment *is* `OPENROUTER_API_KEY` (0600 overlay locally; `--agent-env` in the
  podman container, where podman is "no security boundary", podman.py:34-36; Secret on
  K8s). Recorded as `provider.key_exposed_to_agent: true`, `key_scope: operator` and
  printed at run start. An agent-under-test that exfiltrates its own env exfiltrates the
  key; `key-guardrail` bounds that to a per-run key with `limit_usd`, but `key_exposed_to_agent`
  stays `true` there too (the per-run key is still in the agent's env).
- **Quantization is pinned indirectly.** Nothing in the request names a quantization; the
  operator picks providers whose endpoint for the slug is the wanted quantization
  (preflight checks this against `/endpoints`), and the audit confirms it through the
  `provider → permaslug → /endpoints` join. A provider serving the same permaslug at two
  quantizations is ambiguous (`quantization: null`, warned, snapshot tiebreak); a provider
  that changes its endpoint's quantization mid-run is caught by the audit against the
  **frozen** snapshot, not prevented.
- Mid-run provider re-routing after the first byte is impossible (an OpenRouter commit
  semantic, independent of the client);
  a slow pinned provider can burn a case's timeout. There is no harness-side first-byte/
  idle watchdog, retry, cooldown or widening (Resilience) — Claude Code's own retries and
  the case/step timeout are the bounds. Mitigation: ≥ 2 providers in `order`, `status < 0`
  and uptime exclusion at preflight.
- Auto Exacto reorders providers on every tool-calling request; an explicit `order` +
  `allow_fallbacks: false` overrides it under `tool_choice: auto` (20/20, probe #5 VERIFIED
  2026-09-16) — but the agent path **cannot send `order`**, so on a `:exacto` id the pinned
  set is an audit target, not a constraint: expect churn inside Exacto's set and read
  `routing.served` before comparing runs; the audit records the served provider per request
  so churn is visible.
- `session_id` sticky routing cannot be requested from Claude Code (body field), so no run
  gets prompt-cache affinity from stickiness; with `order` set it would be bypassed anyway
  (VERIFIED). Prompt-cache hit rates on OpenRouter are therefore whatever the served
  provider gives per request.
- **`/generation` lag.** Cost and attribution arrive 8–13 s after `message_stop` (VERIFIED,
  probe #6): a case's cost is `[cost pending]` for ~15 s after it ends, a run killed before
  the run-end retry can leave `backfill_failed` ids (`cost_confidence: low`,
  `audit_complete: false`), and a generation that never materialises within 60 s is
  recovered only by the offline `agent-eval provider backfill <run_dir>`. Nothing here is
  visible in-stream to the harness: it is not on the HTTP path, so failed requests (4xx/5xx
  before `message_start`) leave no ledger row at all and are known only from Claude Code's
  own error output (`error_class`).
- Key-usage fallback (< 0.8 generation coverage) is exact only on a key nothing else uses
  (`cost_confidence: medium` with `dedicated_key: true` or at `key-guardrail`, else `low`);
  `GET /api/v1/key` settles ~20 s after a request (VERIFIED) and the harness waits up to
  60 s — a run whose last request lands inside the settle window can under-read.
- Pinned providers + a **forced/named `tool_choice`** can make a model unroutable (404
  `not_found` "No endpoints found", probe #5): preflight catches it from
  `supports_tool_choice`, judges fall back (`required` → `auto` + strict parse), and Claude
  Code sends `auto`; an agent-under-test that itself forces a named tool while a
  `key-guardrail` allow-list excludes every `function`-capable endpoint gets OpenRouter's
  404 in its own error output, by design.
- **Harbor podman**: the agent container holds the key (above); in-container `openrouter:/`
  judges additionally need `OPENROUTER_API_KEY` at container level (the operator key at
  every enforcement level — judges never use the per-run key), where the agent can also
  read it. Per-trial cost depends on Harbor's captured transcript keeping the assistant
  `message.id`s (probe #25 UNVERIFIED, PR-5 gate); if it strips them, per-trial cost is
  `null`, the run has key-usage cost only, and the routing audit is `audit_complete: false`
  with every request unattributed — the fallback is honest, not silent.
- **Kubernetes** (PR-7): `key-guardrail` needs `create`/`get`/`delete` on `secrets` in the
  namespace for the per-run Secret (create-only: a name clash fails the run; the delete
  checks an owner label and the observed UID), which is deleted in the same `finally` as the key
  revoke whether or not the revoke succeeds; a Secret is left behind only when its own
  delete fails or the harness dies before cleanup (`kubectl delete secret
  agent-eval-<run_id>-openrouter`; the key itself is bounded by `limit_usd`). The
  credentials Secret stays attached whole for what else it holds; the provider key names
  are blanked in the pod (explicit empty `env[]` entries), the operator key staying visible
  only for an in-container `openrouter:/` judge — the podman rule. EvalHub inherits the
  K8s behaviour through the env pass-through and nothing more.
- Subagent/hook children inheriting the overlay env (`CLAUDE_CODE_SUBAGENT_MODEL`, the
  blank Vertex lines) is VERIFIED on Claude Code 2.1.273+ (probe #1 incl. children,
  `probes/probe_cli_report_2026-09-16.json`) and kept green by `test_claude_cli_direct.py`;
  a Claude Code release that stops inheriting settings-env into `Task` children would route
  subagents to Vertex/Anthropic and show up as non-`gen-` ids in the transcript
  (`cost_coverage` drops, `cost_warnings` names it).
- Records without provider attribution (`backfill_failed`) are never inferred as compliant
  or violating; a strict-policy run with any of them is `routing.degraded` and compare/anova
  refuse to pool it without `--allow-unaudited`.
- **`count_tokens` (probe #22, RESOLVED for Claude Code 2.1.273+):** OpenRouter has no
  `POST /api/v1/messages/count_tokens` (404, VERIFIED) and the CLI made zero calls to it in a
  3-turn run with a tool call and a subagent, so nothing answers it and nothing needs to.
  Watch item: re-run `specs/014-openrouter-provider/probes/probe_claude_cli.py` on CLI upgrades; if `count_tokens`
  appears, OpenRouter 404s it and `test_claude_cli_direct.py` (e) fails first.
- Cost units: `usage.cost` is documented as "credits", `/generation` and `/key` as USD;
  the 1:1 USD mapping is VERIFIED (probe #6, 2026-09-16: `usage.cost == /generation
  total_cost` for 10/10 requests), so no unit conversion is applied anywhere.
- Verified-facts caveats: every VERIFIED row was measured against `z-ai/glm-5.3-flash` on
  2026-09-16 with one account's settings (probes/probe_report_2026-09-16_run{1,2}.json) —
  the gen-id echo, the bare-slug `modelUsage` keys, the `/generation` lag, the `/key` settle
  and the Exacto override are OpenRouter/Claude Code behaviours that can change without
  notice; `test_claude_cli_direct.py` and the fixtures lint exist to turn such a change into
  a CI failure rather than a silent cost/attribution regression. Account-level toggles
  (paid-training opt-in, ZDR, ignored providers) are read by preflight but never changed by
  the harness, and a toggle flipped mid-run is caught by the audit, not prevented.
- `ensure_deps` sees only discovered config files; a profile outside the scan set adds its
  deps at first `--config` run.

## Out of scope (future)

- **Harness-owned shaping proxy.** An in-process Anthropic-in / Anthropic-out proxy on the
  agent's request path was evaluated as the transport (Decision 1) and not pursued. What it
  would add over direct + audit: per-request body pins (`provider.order/only/ignore`,
  `quantizations`, `require_parameters`, `models` fallbacks — hard enforcement per request
  without a management key); an in-flight real-cost budget gate (402 on a ledger Σ at
  invocation and run scope); key isolation without the management API (the agent holds a
  random token, never an OpenRouter key); a per-request ledger with no backfill lag
  (`usage.cost` from `message_delta`, provider from `message_start`, permaslug from
  `message_stop` metadata — all VERIFIED 2026-09-16); a local `count_tokens` estimate; and
  per-case attribution on Harbor via a baked `x-eval-case-id`. It costs one more process to
  manage, bind and secure (a LAN-reachable holder of the operator key on podman), and Harbor
  parity comes for free without it. If ever built it would slot in as a third `enforcement`
  level reusing the ledger, reconcile, catalog and snapshot of this spec unchanged; this
  spec reserves no name, URI scheme or ledger `source` value for it.
- **Other provider kinds.** Other provider kinds (an OpenAI-compatible endpoint, an MLflow
  AI Gateway or OpenShift AI gateway, an operator-run translating proxy, ...) are future work; this
  spec reserves no URI scheme, provider name or config key for them. Today an operator-run
  Anthropic-compatible endpoint keeps working through plain `execution.env` /
  `runner.settings.env` exactly as before, outside this feature (no pins, no ledger,
  `cost_source: runner:estimate`). rfe-creator's previous proxy-based setup is retired by PR-8, not
  re-declared. A future kind would be a `models.providers.<name>` entry with its own `kind`,
  reusing `settings_env_block` with a different base URL — not a transport the harness runs.
- Any harness-owned in-flight watchdog on the agent's request path (`first_byte_s`,
  `idle_s`, a content-level `first_token_s`), retry, cooldown or provider widening — all
  presuppose a harness process on the request path (the shaping proxy above); today the case/step timeout and Claude Code's own
  retries are the only bounds (Known limitations).
- A local `count_tokens` answer (a chars/4 estimate served by a harness process on the request path) — moot on Claude Code
  2.1.273+ (zero `count_tokens` calls, probe #22); revived only if a later CLI starts calling
  the route and cannot live with OpenRouter's 404.
- Generic `models.providers.<name>: {kind: openai-compatible, base_url, api_key_env}` judge
  providers and user-chosen provider names — they will reuse the `JudgeClientConfig` seam and
  the `kind` key introduced here rather than a new backend string; `models.providers.openrouter: {}`
  stays the shorthand and `openrouter` stays the single kind of this release.
- Any harness-side in-flight real-cost gate at `enforcement: audit` (reservation, 402 on a
  running Σ) — needs a harness process on the request path (the shaping proxy above); `key-guardrail`'s server-side `limit` is
  the supported in-flight cap.
- Codex (`OPENAI_BASE_URL` + `OPENAI_API_KEY` mapping to OpenRouter's `/v1/chat/completions`
  with the same backfill), opaque `cli` runner placeholders (`{base_url}`, `{auth_token}`),
  Responses API runner, EvalHub beyond the env pass-through (`adapter.py:213` ignores
  `model.url`).
- `agent:` judges on OpenRouter (score.py would need to build a plan and run the backfill
  per judge call).
- OpenAI-native/OpenRouter synthetic generation (spec 013 already excludes it).
- Management-key features **beyond** the per-run key create/guardrail/`limit`/delete used by
  `enforcement: key-guardrail` (key rotation, per-case keys, `/analytics/query`, account
  settings changes), presets (`@preset/slug`) — unverified or higher-privilege; not needed.
- A budget-only `key-guardrail` (a per-run key with `limit` but no provider allow-list) —
  config validation rejects it; `audit` plus `budget.run_usd` covers a budget-only need.
  Offered only if probe #26 shows a per-run `limit` alone is meaningful.
- Absolute-workspace variants of relative permission rules (workspace.py:617-641) — the
  denial→text-only-turn failure that forced `Bash(python3 *)` is independent of OpenRouter.
- A price table to cost Anthropic/OpenAI judge tokens (today `cost_usd: null` for them).

## Decision log

1. **Agent transport: a direct connection to OpenRouter — no proxy of any kind.** Options:
   (a) a harness-owned Anthropic-native shaping proxy (in-process, Anthropic-in /
   Anthropic-out) as the default transport with direct as a peer mode; (b) a translating
   proxy subprocess with a generated config (the previous project-side setup); (c) direct
   only — Claude Code's env template (`ANTHROPIC_BASE_URL=https://openrouter.ai/api`,
   `ANTHROPIC_AUTH_TOKEN=<key>`, blank `ANTHROPIC_API_KEY`, Vertex vars blanked,
   `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU,FABLE}_MODEL` + `CLAUDE_CODE_SUBAGENT_MODEL`
   aliases). Choice: (c). Why: the probes make direct *viable*, not merely possible — #12
   (stream-json `message.id` is the OpenRouter `gen-…` id and `GET /generation` answers for
   it → per-request cost truth without an in-stream tap), #5 (explicit `order` +
   `allow_fallbacks: false` beats Auto Exacto 20/20 under `tool_choice: auto` → pins are
   enforceable *and* auditable from `/generation.provider_name`), #6 (`usage.cost ==
   total_cost`, 8–13 s lag → the backfill is exact, only late), #3/#13/#20 (`/messages`
   accepts Claude Code's headers, `x-api-key` and `anthropic-beta` → no request rewriting
   needed). Against those facts (a) buys only per-request body pins (`quantizations`,
   `require_parameters`), an in-flight real-cost gate, per-case tokens and an in-stream
   ledger — at the price of a listener, framing, timeouts, cooldown, a `count_tokens`
   synthesiser, an httpx dependency, a second (pass-through) provider kind, ~1000 lines and a
   host-reachability problem under podman; (b) carries a version-fragile cost monkeypatch,
   loopback-only reach and a second process to manage. Operational simplicity wins: no
   process on the HTTP path, Harbor podman/K8s parity for free (the env block is the whole
   integration), one transport on every runner. Costs, stated honestly: no per-request body
   pins on the agent path (quantization is pinned indirectly through provider choice;
   `require_parameters` is judge-only), no in-flight real-cost gate at the default level
   (post hoc via `--strict-cost`), the agent holds a key, provider attribution is 8–13 s
   late. Mitigations: `enforcement: audit` (preflight against `/endpoints` + `/providers`,
   `routing_snapshot.json`, post-run audit → `routing.violations`, compare/anova refuse to
   pool differing audits) by default; `enforcement: key-guardrail` (per-run key, server-side
   provider allow-list + `limit_usd`, revoked in a `finally`; probe #26) opt-in. Cost truth on
   this path: per-request `/generation` backfill (`cost_source: openrouter:generation`,
   `cost_confidence: high` at ≥ 0.95 coverage), the key-usage delta as the cross-check, and
   the CLI's own `total_cost_usd` — ~60× the real cost for `z-ai/glm-5.3-flash` — kept only
   as `cost_usd_estimate`, never reported as spend. `models.providers` has one kind; no second
   provider kind or URI scheme is introduced. The shaping proxy is described under "Out of
   scope (future)"; if ever built it would slot in as a third `enforcement` level.
2. **Judge backend: OpenAI SDK on `/api/v1/chat/completions` with a dedicated client.**
   Options: Anthropic SDK against `/api/v1/messages`; OpenAI SDK; routing judges through the
   agent's transport. Choice: OpenAI SDK — reuses the PR #216 structured-judge path verbatim,
   `extra_body` carries the same `RoutingSpec`, `usage.cost`/`provider` are reachable via
   `getattr` on the response objects without SDK changes, and score.py must work as a
   separate process (re-scoring) with nothing else alive.
3. **One `RoutingSpec` per routing key; body injection for judges, preflight + audit or a
   key guardrail for the agent; no presets.** Options: inject the spec into every request
   body (impossible on the agent path — Claude Code cannot carry `provider.order` /
   `quantizations` / `require_parameters`); presets (`@preset/slug`); guardrails alone; the
   split adopted here. Choice: the spec is injected into the request body only for judges
   (`extra_body`, **opt-in** via `judge.inherit_pins` or a per-judge
   `provider_options.routing`, because a forced named `tool_choice` under pins can make a
   judge unroutable — Decision 25; so `to_chat_extra_body()` equals `to_messages_body()`
   only for a judge that inherits pins). On the agent path the same spec is enforced by
   **preflight + post-hoc audit** (`enforcement: audit`, default) or by a **server-side
   per-run key guardrail** (`enforcement: key-guardrail`, opt-in), and the request itself
   carries only the model-suffix variant (`:exacto` recommended for tool-calling agents,
   VERIFIED accepted; `:nitro`/`:floor`). `require_parameters` is judge-only. Why not the
   alternatives: presets are unverified on `/messages` and always resolve to latest
   (reproducibility hazard); guardrails cannot express `order`/`quantizations` (quantization
   is pinned indirectly through provider choice and confirmed by the audit's `/endpoints`
   join) and their field semantics remain DOCUMENTED/UNVERIFIED (probe #26), so a management
   key — higher privilege than the key it protects — is used only at `key-guardrail`,
   env-only, never on an authoring surface.
4. **Overlay mechanism: `extends:` file overlay resolved in one raw loader, not in-file
   `profiles:` + `--profile`.** Options: in-file `profiles:` selected by `--profile`; one
   full config per provider; `extends:`. Choice: `extends:` — `--profile` would have to be
   re-supplied to score/report/compare and forgetting it recreates the drift the feature is
   meant to kill. `extends:` is resolved by `load_raw`, shared by `from_yaml`, Harbor
   bundling/reports, EvalHub reports, local report, validate_eval, anova/reorganize and
   discovery (five production readers load the file raw, so resolving it only in
   `from_yaml` would not do); it records `config_chain` in `eval_params`, and `--print`
   makes the merge visible with provenance. `extends:` is a **new**, optional top-level key
   (PR-3a): `extends: <path relative to the file>` deep-merges the overlay over the base
   (dicts merge, lists per Decision 19, `!replace` escape). It is independent of the
   OpenRouter feature and droppable: its motivation is the `eval.yaml` /
   `eval-openrouter.yaml` drift. rfe-creator generates `eval.yaml` from a skeleton +
   per-type fragments (`scripts/generate_eval_config.py`); that generated file stays the
   base and an OpenRouter profile `extends:` it, so the generator is untouched —
   alternatively the generator could emit per-provider configs and PR-3a would be skipped.
5. **Cost provenance: `cost_usd` is `null` when a provider is active and no truth source
   succeeded; the estimate is always preserved as `cost_usd_estimate`; `cost_source` is
   always written.** Options: keep the runner's estimate in `cost_usd` when the backfill
   fails; write `null`. Choice: `null` — never show an inflated number as spend, and every
   reader already handles `None` because the Cursor runner emits it (cursor_agent.py:203;
   execute.py:748-761; report.py:971-972; compare.py:126-128; analyze.py:330-335;
   log_results.py:281), so no reader needs teaching. `--strict-cost` adds an exit code for
   CI. The one case where the estimate is the only number — Claude Code behind an
   operator-run Anthropic-compatible endpoint through plain `execution.env`, no plan — is
   labelled `runner:estimate` by the runner itself at result construction
   (claude_code.py), never by reconcile, which stays a no-op without a plan; no pass-through
   provider kind is introduced for it.
6. **Reconcile once, at write time, as a pure function — no hooks.** Options:
   `after_all` + `before_report` hooks; reconcile at every `run_result.json` write. Choice:
   write time — derived metrics, MLflow, compare and anova read `run_result.json` at their
   own time (digest), and Harbor runs no hooks and bypasses `before_report` (run.py:458).
   This retires the double run and the silent all-null guard.
7. **Env emission: single `settings_env_block()` + one `MANAGED_ENV_KEYS` set; the
   claude-code runner owns local emission via the generalised settings overlay; managed keys
   are owned by the plan by merge order, with a load-time diagnostic across all authoring
   surfaces.** Options: three new workspace.py hooks; a runner-owned overlay; "provider <
   execution.env" precedence. Choice: the overlay — one emission point that already exists
   (claude_code.py:337-395) and covers per-step runners. The overlay writes
   `{**existing_env, **plan}`, so `runner.settings.env` (documented, merged last by
   workspace.py:757) and any pre-existing settings.json env cannot re-route the agent
   silently, and the validator names all six surfaces. `runner.env`/`steps[].env` are
   process-env and already subordinate to the settings overlay; the settings.json path is
   the one that needed the merge-order guarantee. Identical static values are tolerated so
   old Vertex-blanking configs still load; dynamic/secret keys are rejected on presence.
   With a plan active `_build_env` also strips the managed keys from the ambient copy, so
   the host's real Anthropic credentials never reach the CLI regardless of Claude Code's
   settings-vs-process precedence. A cross-writer conformance test pins the writers to one
   output. The blank `ANTHROPIC_API_KEY` in the template is hygiene, not correctness — a
   valid key sent as `x-api-key` works on `/messages` (probe #13) — so a non-empty static
   value on an authoring surface is a validation **warning** ("stale Anthropic key would
   reach OpenRouter as x-api-key"), not an error; the overlay still writes `""` because the
   strip in `_build_env` and the overlay together keep the host's real key off the wire.
8. **Secrets: at `audit` the agent process holds the operator key; at `key-guardrail` it
   holds a per-run key; both OpenRouter keys are env-only and never touch an authoring
   surface or a run artefact.** Options: a per-case random token with the key held by a
   harness process on the HTTP path (needs a proxy); the operator key in the agent process;
   a per-run key provisioned through the management API. Choice: the last two, as the two
   `enforcement` levels. At `audit` the agent process (local CLI, podman container, K8s pod)
   holds the operator key as `ANTHROPIC_AUTH_TOKEN` (`provider.key_exposed_to_agent: true`,
   `key_scope: operator`, one stderr notice at run start); the agent's Bash environment
   (claude_code.py:712-733) can therefore spend on that key — an accepted, stated property
   (Known limitations). At `key-guardrail` it holds a per-run key with a provider allow-list
   and `limit_usd`, provisioned with `OPENROUTER_MANAGEMENT_KEY` and revoked in a `finally`
   (`key_scope: per-run`; the agent never sees the operator or the management key), which
   bounds that spend. Invariants: `OPENROUTER_API_KEY`/`OPENROUTER_MANAGEMENT_KEY` are
   env-only and rejected on every authoring surface (`execution.env`, `runner.env`,
   `runner.settings.env`, `steps[]` variants); the key reaches the CLI only through the 0600
   settings overlay (unlinked in a `finally`) or Harbor's `--agent-env` / the K8s Secret;
   never in argv, ledger, snapshot, `_SAFE_ENV_KEYS`, logs or run artefacts (`key_hash` is
   the only trace); lifecycle hooks do not receive it. In-container `openrouter:/` judges
   reach OpenRouter directly with the same key. The hygiene test greps run dir, stderr and
   child env — including the host's fake Anthropic credentials — and covers the podman
   `--agent-env` and K8s `secretKeyRef` paths.
9. **Alias derivation: OPUS/SONNET/FABLE = skill model; HAIKU = skill model unless
   `background_model`; SUBAGENT = subagent model.** Options: a cheap background model by
   default; one model everywhere. Choice: one model under test by default so
   `per_model_usage`/cost stay single-model; a cheap background model is an explicit,
   recorded choice. Settles README.md:62 vs eval-openrouter.yaml:30.
10. **Budget: `execution.max_budget_usd` keeps its documented per-invocation scope; an
    optional `budget.run_usd` caps the run — post hoc at `audit`, server-side and in flight
    at `key-guardrail`; the CLI cap is an inflated backstop, never a sentinel.** Options:
    sum the whole run against the per-case number (a 20-case run at ~$0.54/case would die
    at `5.0` after ~9 cases); remove the client ceiling with a sentinel such as `1000000`; a
    harness-side per-token gate (needs a process on the HTTP path); the shape adopted.
    Choice: `execution.max_budget_usd` maps to the CLI's `--max-budget-usd` ×
    `cli_budget_inflation` (default 50) because the CLI prices a non-Anthropic model 2–60×
    high (probe #12); the estimate is never reported as spend. At `enforcement: audit` the
    real-dollar cap is **post hoc** — reconcile sets `budget.exceeded: "run"`,
    `exceeded_reason: "post-hoc"` when the backfilled Σ exceeds `budget.run_usd`, and
    `--strict-cost` then fails the run (exit 2) — stated in Known limitations. At
    `enforcement: key-guardrail` the per-run key's `limit_usd = budget.run_usd` is the
    **in-flight, server-side** real-cost gate (OpenRouter's 402 on the per-run key →
    `exceeded_reason: "limit_usd"`). There is no harness-side reservation, pricing-based
    charging of unpriced records or miss-count trip: there is no harness gate to fail
    closed. Claude Code 2.1.273+ rejects `--max-budget-usd 0` ("must be a positive number
    greater than 0", exit 1, no request; probe #23), so a resolved per-invocation cap of 0
    or None means **omit the flag** (no CLI cap) — never pass 0 and never a sentinel;
    `key-guardrail` `limit_usd` is unaffected. Budget mapping, claude_code.py and
    execute.py state the same rule.
11. **Resilience: the harness owns no retries, timeouts, cooldown or pin widening on the
    agent path; it keeps resilience only where it makes requests itself.** Options:
    harness-side pre-first-byte retries, first-byte/idle watchdogs and a per-routing-key
    cooldown (all presuppose a process on the HTTP path); rely on Claude Code + OpenRouter.
    Choice: the latter — resilience on the agent path is Claude Code's own retry/backoff
    (probe #24 documents it) plus OpenRouter's server-side behaviour under the configured
    variant (`:exacto`/`:nitro`/`:floor`; with `key-guardrail` the allow-list bounds the
    providers it may fall to). The harness keeps resilience where it still makes requests:
    preflight catalog GETs (degrade to warn on fetch failure), the `/generation` backfill
    (first poll ~5 s, then 2 s, give up at 60 s, run-end retry, `Retry-After` on 429) and
    the `/key` settle read (`guardrail.settle_s`, 20 s by default, 60 s ceiling). The case/step timeout is the only wall clock; the project
    rule — never cap reasoning for performance — holds, so there is no default per-request
    wall cap. Keep-alives are not a steady heartbeat on fast streams (probe #16) —
    informational, since the harness owns no idle timer. `count_tokens` is a non-issue on
    Claude Code 2.1.273+ (zero calls observed, probe #22 RESOLVED; watch item on CLI upgrades
    via `specs/014-openrouter-provider/probes/probe_claude_cli.py`). The only in-flight
    refusal the harness sees is the `key-guardrail` 402.
12. **Harbor: podman and K8s are consumers of the one direct transport — podman via
    `--agent-env`, K8s via `secretKeyRef`; no exec-inlining; no second provider kind.**
    Options: a host-side proxy thread with a reachability probe and a managed→direct
    fallback; a separate pass-through provider kind for operator-run Anthropic-compatible
    endpoints; direct everywhere. Choice: direct everywhere. Podman receives the plan's env
    block via `--agent-env` (Harbor merges it last, `harbor 0.13.1 agents/base.py:288-291`,
    VERIFIED) with the key resolved on the host — the same exposure class as today's
    `ANTHROPIC_AUTH_TOKEN` forwarding (podman.py:36-49) — and the host's
    `CLAUDE_CODE_USE_VERTEX`/`ANTHROPIC_VERTEX_PROJECT_ID`/`CLOUD_ML_REGION` are **not**
    forwarded while a plan is active (the plan blanks them in `--agent-env`). K8s gets
    `OPENROUTER_API_KEY` from the credentials Secret (`secretKeyRef` →
    `ANTHROPIC_AUTH_TOKEN` mapped by the harness) plus the plan's non-secret env, with
    Vertex vars already excluded (kubernetes.py:42-50); the exec-prefix suppression lives
    where the prefix is built (kubernetes.py:547). There is no fallback, so nothing is
    recorded as a separate factor level; `key_exposed_to_agent: true` is recorded at
    `audit`. An operator who fronts an Anthropic-compatible endpoint keeps using plain
    `execution.env` as today — outside this feature, no config change — rather than a
    nested `external` block that would force `openrouter:/` slugs, `OPENROUTER_API_KEY`
    and preflight onto an endpoint that may front Bedrock or Anthropic.
13. **One ledger file and schema for agent, hook and judge records with `role`, `source`
    and `provider_kind` fields; per-model attribution by the join rule, never a
    proportional split.** Options: one file per role; a proportional split of run cost by
    model; one schema with a join. Choice: one schema — it retires
    `reconcile_cost.py:102-109`. Hook-model spend is separated (`hook_cost_usd`) so
    `cost_usd` stays comparable with the runner-reported figure. Sources: agent and hook
    records are written by the `/generation` backfill worker (`source: generation`, keyed
    by the `gen-…` ids parsed from stream-json locally — `stream_capture.py` — or from the
    Harbor trial transcript — `results.py`, probe #25) and by the key-usage delta as a
    run-level cross-check record (`source: key-usage`); judge records are written from the
    OpenAI-SDK response (`source: judge`). The join rule (`model_requested`/`model_echo`/
    `model_served` + permaslug map) is the only attribution mechanism. Lifecycle: the plan
    starts its backfill worker after preflight and `plan.close()` runs in a `finally` in
    both execute.py and harbor/run.py (backfill drain, key-usage settle read, per-run key
    revoke) — both hosts are long-lived Python processes that already own the run
    lifecycle, so no separate process is needed (the offline `agent-eval provider backfill|audit|revoke` commands are recovery tools, not a daemon).
14. **Interception bakes nothing provider-related; managed keys are excluded from baked
    env.** Options: bake a per-case header or provider values into reusable packages; keep
    them out. Choice: host-specific values (key, run id) must not be frozen into reusable
    packages, and with no process reading a case header there is nothing to bake; only the
    `MANAGED_ENV_KEYS` exclusion applies, so baked keys and `--agent-env` keys are disjoint
    by construction.
15. **Judge cost lives in `summary.yaml.judge_usage`, never in `run_result.cost_usd`.**
    Options: fold judge spend into `cost_usd`; keep it separate. Choice: separate — keeps
    `cost_per_turn_usd`/`cost_per_mtok_usd` agent-only and old-run comparable, and prevents
    double counting with agent reconciliation.
16. **Provider seam shape: a `models.providers` mapping with a reserved `kind`, a single
    fixed name (`openrouter`) in this spec, and a `JudgeClientConfig` seam that keeps the
    judge transport set at three.** Options: encode OpenRouter as a fourth backend string
    with a name-specific client getter and new dispatch arms; build a generic `kind` now;
    the narrow seam. Choice: implement the narrow seam now (transport + client config,
    `_client_for`), reserve the registry shape (`kind`) so user-chosen names and
    `openai-compatible` land without breaking the schema, and do not build the generic kind
    in this spec. `resolve_judge_backend` keeps its 2-tuple contract (existing tests and
    the spec 013 rows unchanged); the sibling `resolve_judge_client` carries the provider
    context. No second provider kind or URI scheme is introduced.
17. **The provider registry is `models.providers`, not a top-level `providers:` key.**
    Options: (a) a new top-level `providers:` mapping; (b) `models.providers`. Choice: (b).
    Why: the registry exists only to resolve the `<provider>:/<model>` URIs that already
    live under `models.*` (skill / subagent / judge / hook — the same cross-cutting scope
    spec 013 / PR #216 gave model URIs), so nesting it keeps model configuration cohesive
    in one block, avoids reserving a new top-level namespace in the eval config, and lets an
    `extends:` overlay override `models.providers.openrouter.routing` alongside
    `models.skill` in a single `models:` stanza. Consequences: `ProvidersConfig` is a field
    of `ModelsConfig` (`config.models.providers`, never `config.providers`); every path in
    prose, validation messages and docs reads `models.providers.openrouter.<key>`; a
    top-level `providers:` key is rejected with a pointer here; the activation rule reads
    "a `models.providers.*` block is inert until an effective role URI names it". Reserved
    names and kinds are unchanged (single kind `openrouter`, Decision 1); nothing about the
    ledger, `eval_params` or the env templates embeds the config path, so no field is
    renamed there.
18. **Package layout: one package — `agent_eval/providers/` (neutral core: base, env,
    ledger, reconcile; plus `openrouter/` for OpenRouter API knowledge) — with a one-way
    import direction and provider-neutral names (`<run_dir>/provider/…`,
    `cost_source: <origin>:<method>`).** Options: a `providers/` ↔ `openrouter/` split in
    which `providers/openrouter.py` imports `openrouter/routing.py` while
    `openrouter/reconcile.py` needs `ProviderPlan` (a cycle from day one, with
    provider-neutral concerns under an OpenRouter-named path); a separate runtime package;
    the layout adopted. Choice: `ProviderPlan` holds an opaque `RoutingTableProtocol`, which
    removes the cycle rather than tolerating it; the `cost_source` enum is
    `<origin>:<method>` (legacy literals kept readable) rather than `<provider>-<method>`,
    a form that invites drift between spellings such as `runner-estimate` and
    `runner-reported`. There is no runtime package because nothing sits on the HTTP path.
19. **`extends:` merge policy: reuse the harness's existing `_deep_merge` (lifted, with
    dedupe) — scalar lists extend, `judges`/`steps` merge by key, `!replace` opts out; paths
    resolve from the root of the chain.** Options: lists replace; lists extend. Choice:
    extend — a lists-replace policy would contradict the documented `runner.settings`
    policy (runner.md:131), and an overlay that dropped `Skill`/`Agent` from a permission
    list would end a headless run at the first `/rfe.create` call. One merge implementation
    means one list semantics per YAML file; `RoutingSpec` is the documented exception
    because its lists are complete statements.
20. **Attribution degraded path: never infer provider or quantization; count, warn, and
    mark strict runs degraded; do not touch `cost_confidence`; signal degradation at
    reconcile and on stderr.** Options: lower `cost_confidence` on attribution gaps; infer
    the provider from the pins; the adopted rule. Choice: cost comes from
    `/generation.total_cost` independently of the provider field, so attribution gaps
    degrade the *routing audit*, not the cost; the `/generation` backfill is the only
    attribution path and the never-infer rule applies to it. Degradation is signalled at
    reconcile (`routing.violations` / `degraded_reason`) plus stderr one-liners and a
    marker in the live progress line, so the report banner is not the first signal; there
    is no in-flight widening, so a run carries one `routing_sha` — the declared spec's.
21. **Activation is URI-driven; a `models.providers.*` block is an inert declaration.**
    Options: error when a block is present but no `openrouter:/` role names it; inert.
    Choice: inert — an error would prevent the base `eval.yaml` from holding the shared
    routing table and force every profile to duplicate the pins whose drift caused the
    2026-07-22 provider-roulette incident. The real footgun stays a narrow error (bare id
    next to matching pins) and the "unused block" signal is a post-CLI-resolution warning.
22. **Rollout ordering: the cost substrate (ledger, reconcile, catalog) lands in PR-4,
    ahead of the PR-5 MVP; each PR depends only on earlier PRs.** Options: ship the
    transport first and add cost truth afterwards; substrate first. Choice: substrate
    first — a first milestone that shipped routing without cost truth would regress
    rfe-creator's current proxy-reconciled cost figures, and `pricing_for` and the catalog
    must precede the preflight that needs them.
23. **Harbor podman in the MVP (PR-5); Kubernetes/EvalHub in PR-7.** Options: (a) local
    claude-code first, Harbor in PR-7; (b) podman in the MVP, K8s next; (c) everything in
    one PR. Choice: (b) — **PR-5 = local claude-code + Harbor podman**, PR-7 = K8s/EvalHub.
    Why: Harbor podman is a hard requirement, and on the direct transport (Decision 1) it
    costs almost nothing — the plan's env block is passed via `--agent-env` (merged last by
    `harbor 0.13.1 agents/base.py:288-291`, VERIFIED), the key is resolved on the host
    exactly as `ANTHROPIC_AUTH_TOKEN` is forwarded today (podman.py:36-49), `openrouter.ai`
    is reachable from the container so no `host.containers.internal` concern exists, and
    cost truth reuses the same gen-id backfill over the trial's captured stream-json
    (harbor/results.py:80-168; probe #25 UNVERIFIED, with the key-usage delta as the honest
    fallback if Harbor strips ids). The only podman-specific change is a conditional: the
    host's Vertex vars (podman.py:36-49 forwards `CLAUDE_CODE_USE_VERTEX` /
    `ANTHROPIC_VERTEX_PROJECT_ID` / `CLOUD_ML_REGION`) are not forwarded while a plan is
    active, because the plan blanks them. K8s is one PR later only because it adds a Secret
    mapping (`OPENROUTER_API_KEY` `secretKeyRef` → `ANTHROPIC_AUTH_TOKEN`; Vertex vars
    already excluded, kubernetes.py:42-50), a per-run Secret at `key-guardrail` and EvalHub
    reporting — not because the transport differs. Per-trial run tagging is not needed for
    cost (gen ids are per request); an `x-eval-run-id`-style custom header stays optional.
    Costs: PR-5 grows by the `--agent-env` builder, the forwarding conditional and
    `results.py` id extraction; the MVP acceptance gains a podman run.
24. **HTTP client: stdlib `urllib.request` for the harness's own OpenRouter calls; no httpx
    extra; the judge path keeps the `openai` SDK.** Options: httpx as a declared
    `openrouter` extra; stdlib. Choice: stdlib — the harness's client (`/generation`,
    `/key`, `/models/{slug}/endpoints`, `/providers`, `/models/user` reads and the
    management-API `POST/DELETE /keys` at `key-guardrail`) is small, non-streaming JSON and
    runs over the default SSL context, already `truststore`-injected by
    `agent_eval._bootstrap` (probe #17): no extra, no `require_httpx()`, no Containerfile
    change; "declare it, don't assume it" is honoured by having nothing to declare.
25. **Forced `tool_choice` under pins is a routing hazard: preflight it, never introduce
    it, fall back for judges, and classify the resulting 404 as config (probe #5,
    `probes/probe_report_2026-09-16_run{1,2}.json`).** Facts: under `order: [z-ai, novita]`
    + `allow_fallbacks: false`, 20/20 tool-calling requests with `tool_choice: {type: auto}`
    were served by z-ai (pins beat Auto Exacto), but the same pins with a forced
    `{type: "tool", name: …}` returned 404 `not_found` "No endpoints found for
    z-ai/glm-5.3-flash" — with and without `require_parameters: true` — while the forced
    request without pins was served (by NextBit). Named-function forcing is an
    endpoint-level capability (`/endpoints`
    `supports_tool_choice{none,auto,required,function}`), and `require_parameters` only
    filters, it cannot create an endpoint. Consequences: (a) preflight checks
    `supports_tool_choice` for every pinned endpoint — `auto` for agent slugs (Claude Code
    sends `auto`), `function` for judge slugs — and FAILs a pinned set that supports none of
    the mode the role needs; (b) the agent path never forces `tool_choice` because Claude
    Code sends `auto`, and nothing in the harness adds or rewrites it; (c) judge pins are
    **opt-in** (`judge.inherit_pins` / `provider_options.routing`) because a judge does not
    need endpoint determinism the way the agent does, and a pinned judge whose endpoint
    lacks `function` forcing falls back `required` → `auto` + strict parse of the first
    tool call (score.py), recorded as `tool_choice_mode`; (d) the routing 404 can only
    occur on the judge path (chat/completions), where score.py maps it to
    `JudgeProviderError` (`error_class: config`, `error_type: not_found`,
    `error_message: "routing: …"`) and fails fast under `strict` (`routing.degraded`,
    `degraded_reason: unroutable`). Rejected alternative: auto-widening pins on this 404 —
    it would silently route a strict run to an unpinned endpoint, the exact failure mode
    strict pins exist to prevent.

## Verification checklist before implementation

Probes marked **KEY** need `OPENROUTER_API_KEY` and must be run by the user; the harness
never embeds or prints the value. The two probe scripts are **spec-local verification
artefacts** (PR-0): they live under `specs/014-openrouter-provider/probes/` next to the
evidence they produce and are not harness tooling (nothing under `agent_eval/`, `skills/` or
`scripts/` imports or ships them). `specs/014-openrouter-provider/probes/probe_openrouter.py` runs the no-key
API probes and, when the key is exported, the KEY probes, writing `probe_report.json` with
results only (and, with `--record-fixtures`, the redacted SSE golden fixtures);
`specs/014-openrouter-provider/probes/probe_claude_cli.py` runs the no-key **Claude Code CLI** probes (rows 1, 9,
10, 22, 23, 27) against a local fake Anthropic endpoint and writes
`probes/probe_cli_report_2026-09-16.json`. Blocking probes gate the PR named in the last
column. The no-key Claude Code probes 1, 9, 10, 16 (echo-server half), 22, 23, 24 and row 27
(multi-header) are additionally kept green by `tests/test_claude_cli_direct.py` (Tests; the
CI form of `probe_claude_cli.py` — a local echo server standing in for `openrouter.ai`), so a
Claude Code upgrade that changes client behaviour fails CI rather than a future run. Rows
25 and 26 cover the two prerequisites specific to the direct transport: Harbor trial
artefacts keep the assistant `message.id`s, and the management-API key-guardrail semantics.

**Evidence summary (2026-09-16; file-by-file index in `probes/README.md`).** API probes ran
with a key against `z-ai/glm-5.3-flash`, `order: [z-ai, novita]`
(`probes/probe_report_2026-09-16_run{1,2}.json`; `_run2.json` is authoritative for probes
3, 5 and 12); CLI probes ran Claude Code 2.1.273+ against a local fake Anthropic endpoint ("2.1.273+" = identical outcomes on 2.1.273, 2.1.274 and 2.1.278 — the CLI auto-updates between regenerations; the committed report's `claude_version` field records the exact version, 2.1.278 at the time of writing)
with the user's `~/.claude/settings.json` forcing Vertex
(`probes/probe_cli_report_2026-09-16.json`). Every report under `probes/` carries a
`producer` stamp (script, schema version, git sha) from schema 2 onwards, and
`probes/README.md` maps each file to the script revision that produced it. Load-bearing
facts: `provider` sits in `message_start.message`, `usage.cost` in `message_delta.usage`,
`openrouter_metadata` in `message_stop` with `endpoints.available[]` + `selected: true`
(no `endpoints.selected` field), `X-Generation-Id` present — this fixes the redacted SSE
fixtures (`tests/data/openrouter/*.sse`); `x-api-key` with a valid key works, so the blank
`ANTHROPIC_API_KEY` is hygiene (warning severity); `usage.cost == /generation total_cost`
10/10 and `/generation` lags `message_stop` by 7.6–12.7 s → backfill backoff and `/key`
settle (20.3 s observed → 60 s ceiling); direct-mode `claude --print` yields `gen-…` message
ids and a bare-slug echo → gen-id backfill is the per-request cost truth
(`cost_source: openrouter:generation`) with the key-usage delta as cross-check, and the
CLI's own `total_cost_usd` is ~60× inflated for this model; explicit `order` +
`allow_fallbacks: false` beats Auto Exacto 20/20 under `tool_choice: auto`, but a forced
`{type: tool, name}` under the same pins 404s while it succeeds unpinned → preflight
`supports_tool_choice` check, judge fallback ladder and error classification (Decision 25);
slug and display name both accepted as provider ids; bare/`:variant`/`[1m]` all echo the
bare slug, permaslug only in metadata/`/generation`; a 5 s stream carried zero
`: OPENROUTER PROCESSING` comments; `/endpoints` status values seen `{0, -2}` →
`status < 0` = degraded. CLI side: the `--settings` env block wins over user settings for
root turns, the subagent request and the hook subprocess env (rows 1, 10 → the
Vertex/Anthropic-hook load-time rejection is evidence-based); single- and multi-line
`ANTHROPIC_CUSTOM_HEADERS` arrive as separate headers on root and subagent requests (rows
9, 27 → the whole attribution header set ships in PR-5); zero `count_tokens` calls in a
3-turn run with a tool call and a subagent (row 22; watch item on CLI upgrades);
`--max-budget-usd 0` is rejected before any request (row 23 → cap ≤ 0/None omits the flag);
the CLI posts to `/v1/messages?beta=true`, so route matchers strip the query string. Row 14
was verified by introspection on the installed `openai` client (2.30.0 / 2.46.0); row 11
(container egress) is DEFERRED until the podman machine is up. Unverified rows still gate
their PRs.

| # | Assumption | Probe | Blocks |
| --- | --- | --- | --- |
| 1 | **VERIFIED incl. subagent/hook children (2026-09-16, Claude Code 2.1.273+, `probes/probe_cli_report_2026-09-16.json` via `specs/014-openrouter-provider/probes/probe_claude_cli.py`; CLI half first seen live in `probes/probe_report_2026-09-16_run2.json`, probe #12):** with the user `~/.claude/settings.json` forcing `CLAUDE_CODE_USE_VERTEX=1` and every `ANTHROPIC_*`/Vertex var removed from the process env, a `--settings` env block (Vertex vars `""`, `ANTHROPIC_BASE_URL` = local fake endpoint, `ANTHROPIC_AUTH_TOKEN` dummy, `ANTHROPIC_API_KEY` `""`) routed **all 5 requests** of a 3-turn run to the local endpoint — root turns and the spawned subagent request (Agent tool; `subagent_stats spawned=1 completed=1`); nothing reached Vertex; the PreToolUse hook subprocess inherited the settings-env values (`ANTHROPIC_BASE_URL` = local endpoint, `CLAUDE_CODE_USE_VERTEX` = `""`, `ANTHROPIC_VERTEX_PROJECT_ID` = `""`, `ANTHROPIC_AUTH_TOKEN` present). The CLI posts to `/v1/messages?beta=true` — path matching must strip the query string. Since `_build_env` strips managed keys from the process env when a plan is active, this is a functional check of the settings-vs-settings layer pair, not a secrecy gate. | No key: `specs/014-openrouter-provider/probes/probe_claude_cli.py` (a local fake Anthropic endpoint; `claude --print --output-format stream-json --settings <env block>` with Vertex forced in user settings and the managed keys absent from the process env; a prompt that spawns a subagent and fires a PreToolUse hook). Kept green by `tests/test_claude_cli_direct.py` (a)/(b)/(i). | PR-5 |
| 2 | **UNVERIFIED.** Claude Code works end-to-end **directly** against `https://openrouter.ai/api` from a **Harbor podman trial** with a non-Anthropic model and tool calls (streaming, thinking blocks, `cache_control` accepted; no parameter rejections; the plan's env block delivered via `--agent-env` and the host Vertex vars not forwarded), and the trial's captured transcript yields the `gen-…` ids the backfill needs (joint with #25). The local-CLI half of the same statement is already VERIFIED by #12. | **KEY**: PR-5 build; one `harbor run` under podman with a one-tool prompt on `z-ai/glm-5.3-flash`, `enforcement: audit`, `:exacto`; assert exit 0, tool call executed, `run_result.json` per-trial `cost_usd` non-null with `cost_source: openrouter:generation`, `routing.audited == requests`, `violations == []`, and `podman inspect`-level absence of `CLAUDE_CODE_USE_VERTEX` from the container env. | PR-5 |
| 3 | **VERIFIED (2026-09-16, `probes/probe_report_2026-09-16_run1.json`):** `message_start.message` carries `id` (`gen-…`), `model` (bare slug echo), `provider` (display name, e.g. `"Z.AI"`) and `usage`; `usage.cost` arrives in `message_delta.usage`; `X-OpenRouter-Metadata: enabled` IS honoured on `/messages` and `openrouter_metadata` arrives in `message_stop` as `{requested, strategy: "direct", region, summary, attempt, is_byok, endpoints: {total, available: [{provider, model: <dated permaslug>, selected: bool}, …]}}` — there is **no** `endpoints.selected` field, the selected endpoint is the `available[]` entry with `selected: true`; `X-Generation-Id` header present; event order `message_start`, `content_block_*`, `message_delta`, `message_stop`, then one trailing `data` frame. Placement of top-level `provider` and `openrouter_metadata` in the `/messages` SSE stream. | **KEY**: one streaming `POST /api/v1/messages` with the metadata header; record which events carry which fields. Result: the evidence fixes the SSE golden fixtures and establishes that quantization is **not** in the stream (it is joined from `/endpoints`), which the audit relies on. | — (evidence for the SSE fixtures and the audit join) |
| 4 | **VERIFIED (2026-09-16, run1):** `order: ["z-ai"]` and `order: ["Z.AI"]` are both accepted and both served by Z.AI — matching accepts slug and display name, case-insensitively. Provider identifier matching in `provider.order`/`only` on `/messages`. | **KEY**: two requests with slug vs display name, `allow_fallbacks: false`, metadata header; compare the `selected: true` endpoint. Result: slug normalisation stays as a cosmetic/consistency step, not a correctness requirement; "unknown names fail `preflight: strict`" stays. | PR-6 |
| 5 | **VERIFIED with a critical caveat (2026-09-16, run1+run2):** `order: [z-ai, novita]` + `allow_fallbacks: false` + `tool_choice: {type: auto}` on 20 tool-calling requests → 20/20 served by z-ai, 0 outside the list, `stop_reason: tool_use` — explicit `order` + `allow_fallbacks: false` DOES override Auto Exacto. **BUT** a forced `tool_choice: {type: "tool", name: …}` under the same pins → HTTP 404 `error_type: not_found` "No endpoints found for z-ai/glm-5.3-flash" (identical with and without `require_parameters: true`), while the same forced request **without** pins → 200 served by NextBit. Forced/named `tool_choice` is only supported by some endpoints; pins + forced `tool_choice` can make a model unroutable. | **KEY**: 20 tool-calling requests under a 2-provider `order`; tally the `selected: true` endpoint; then one forced-tool request pinned vs unpinned. Result: preflight `supports_tool_choice` check for pinned endpoints, judge fallback policy, 404 classified `error_class: config` (judge path; never retried) — Decision 25. | PR-6 |
| 6 | **VERIFIED (2026-09-16, run1):** `usage.cost == GET /generation total_cost` for 10/10 requests (USD 1:1); the cost event is `message_delta`; the first `/generation` 200 arrived **7.6–12.7 s after `message_stop`** (never "within seconds"); `/generation` `provider_name` is the display name. `usage.cost` == `total_cost`, cost event placement, `/generation` availability. | **KEY**: compare for 10 requests; record the event carrying `usage.cost`; time first 200 on `/generation` after `message_stop`. Result: `generation.py` backoff = first poll ~5 s, then every 2 s, give up at 60 s (record `cost_confidence: low` + retry at run end). | PR-4 |
| 7 | **VERIFIED (2026-09-16, run1):** `GET /api/v1/key` `usage` changed 20.3 s after a request. Settle window for the key-usage cross-check. | **KEY**: poll every 5 s after one request; record when it changes/stops changing. Result: `guardrail.settle_s` default 20 s (minimum wait before the run-end read), poll ceiling 60 s. | PR-6 |
| 8 | **VERIFIED (2026-09-16, run1):** `openai/gpt-5.2` on chat/completions with forced function `tool_choice` + `max_tokens` → 200, `function.arguments` is a JSON string, `usage.cost` present (0.000854). OpenRouter accepts `max_tokens` with forced `tool_choice` for `openai/gpt-5*` and returns `function.arguments` as a JSON string. | **KEY**: one judge-shaped call per slug; assert 200 and the arguments type. Result: PR-1 OpenAI-slug handling confirmed (`openai/o3*` not separately probed). | PR-1 |
| 9 | **VERIFIED (2026-09-16, CLI 2.1.273+, `probes/probe_cli_report_2026-09-16.json`):** a single-header `ANTHROPIC_CUSTOM_HEADERS` (`x-eval-run-id: …`) from the settings env was present on the root requests **and** on the subagent request. Together with row 27 (multi-header form) this puts the whole attribution set (`HTTP-Referer` + `X-OpenRouter-Title` [+ optional `x-eval-run-id`]) in PR-5; the run tag stays optional (`run_id_header`, activity-page tagging only — cost and per-case attribution come from gen ids, Decision 23). | No key: `specs/014-openrouter-provider/probes/probe_claude_cli.py`; inspect the recorded headers across root and subagent requests. Kept green by `test_claude_cli_direct` (d). | PR-5 |
| 10 | **VERIFIED (2026-09-16, CLI 2.1.273+, `probes/probe_cli_report_2026-09-16.json`):** the PreToolUse hook subprocess inherits the settings-env values Claude Code applied (`ANTHROPIC_BASE_URL`, blanked Vertex vars, `ANTHROPIC_AUTH_TOKEN`), so an Anthropic `models.hook` alongside an OpenRouter agent **would** be redirected to OpenRouter and 404 on its slug — the load-time rejection of a Vertex/Anthropic hook under a plan (Config validation, tools.py) rests on evidence rather than a conditional. The hook's own Anthropic client therefore reaches OpenRouter **directly** with the overlay env; its `gen-…` ids ledger as `role: hook` (`hook_cost_usd`). | No key: `specs/014-openrouter-provider/probes/probe_claude_cli.py` records the hook subprocess env. `test_claude_cli_direct` (i) asserts the hook's request is distinguishable (its own `message.id`) from the agent's. | PR-5 |
| 11 | **DEFERRED — not executed:** the only podman requirement is **container egress to `openrouter.ai`** (no host-side process, no `host.containers.internal` reachability needed). The podman machine on the dev box was not running, so the check did not run. Public TLS, CA/proxy vars forwarded as today (podman.py:36-49). | No key, when the machine is up: `podman run --rm docker.io/library/python:3.12-alpine python3 -c "import urllib.request;print(urllib.request.urlopen('https://openrouter.ai/api/v1/providers',timeout=20).status)"` → expect `200`. | PR-5 (podman acceptance only; #2 is the full e2e) |
| 12 | **VERIFIED (2026-09-16, `probes/probe_report_2026-09-16_run2.json`):** direct-mode `claude --print --output-format stream-json` exits 0; assistant `message.id` values ARE `gen-…` ids (2 per turn incl. a `generate_session_title` background call); `assistant.message.model` and `result.modelUsage` keys echo the **bare** slug; `GET /generation` for the CLI's gen id → 200 after 9.4 s with `total_cost 0.00165633` vs Claude Code's `total_cost_usd` estimate 0.1005 (~60× inflated); stderr shows a harmless `[claude-code:unrecognized_model] {"model":"z-ai/glm-5.3-flash","query_source":"generate_session_title"}`. The run went to `openrouter.ai` despite `CLAUDE_CODE_USE_VERTEX=1` in user settings (probe #1, CLI half) and Claude Code's `anthropic-beta` headers were tolerated (probe #20). Claude Code's stream-json `message.id` is OpenRouter's `gen-…` id; `modelUsage` keys echo the bare slug. | **KEY**: direct env template, `claude --print --output-format stream-json --verbose`; grep `"id":"gen-`; record `modelUsage` keys and `message.model`; `GET /generation?id=`. Result: gen-id backfill is the per-request cost truth (`cost_source: openrouter:generation`); the key-usage delta is the cross-check; join-rule fixture = bare-slug echo (see #19). | PR-6, PR-4 |
| 13 | **VERIFIED (2026-09-16, run1):** a VALID key sent as `x-api-key` (i.e. a non-empty `ANTHROPIC_API_KEY`) **works** on `/messages` (200). A valid key sent as `x-api-key` works or fails cleanly. | **KEY**: one request with `x-api-key` only. Result: the blank `ANTHROPIC_API_KEY` is hygiene (do not leak a stale Anthropic key; avoid cached-OAuth confusion), not correctness — validation severity = **warning** (Config validation; Decision 7). | PR-5 |
| 14 | **VERIFIED on the installed versions (2026-09-16, openai 2.30.0 and 2.46.0):** `OpenAI(default_headers=, max_retries=, timeout=, base_url=)` and `chat.completions.create(extra_body=, extra_headers=, tool_choice=, max_tokens=, max_completion_tokens=)` exist and the response models expose pydantic `model_extra` (→ `usage.cost`/`provider`). These have been part of the v1 client since openai 1.0, so the `>=1.70` floor (pyproject.toml:35) is safe; **PR-1 CI pins/verifies the 1.70 floor** in a scratch venv. | No key: introspection on the installed client (done); PR-1 CI: `pip install openai==1.70` in a scratch venv + the fake-server unit tests; raise the floor (Containerfile + ensure_deps) only if that job fails. | PR-1 (floor re-pinned in CI) |
| 15 | **Measurement, not an assertion of a winner:** which layer Claude Code applies inside the Harbor container when a baked project settings.json env key and a `--agent-env` process env key overlap (HOME=/workspace, CLAUDE_CONFIG_DIR=/logs/agent/sessions). Probe #1 covers settings-vs-settings; this is the sole settings-vs-process probe. Interception bakes **no** provider env key (there is no per-case header to bake), so the design carries everything via `--agent-env` and this row is a guard against a future baked key, not an MVP gate. | No key: one podman trial against an echo endpoint with a sentinel `ANTHROPIC_CUSTOM_HEADERS` baked and a different one via `--agent-env`; record which arrives, plus `Authorization` (dummy token). | PR-7 (guard; low) |
| 16 | **KEY half VERIFIED (2026-09-16, run1): a 5 s / 702-event stream carried ZERO `: OPENROUTER PROCESSING` comments — keep-alives are not a steady heartbeat on fast streams (informational: the harness owns no idle timer, Decision 11). Echo-server half UNVERIFIED:** how Claude Code surfaces an upstream mid-stream SSE `error` event and an HTTP 402 (OpenRouter's `insufficient_credits` / per-run key `limit_usd` refusal) — ends the turn with an API error, no retry storm, and the error text reaches stream-json/stderr in a form `stream_capture.py` can classify as `budget.exceeded_reason: limit_usd` (`key-guardrail`) or `error_class: infra`. | No key: local echo server (`tests/test_claude_cli_direct.py`) replaying the `error_mid_stream` fixture and answering 402 with an OpenRouter-shaped body; run `claude --print --output-format stream-json` against it; record exit code, the `result` event's `is_error`/subtype and stderr. | PR-5 (classification incl. `limit_usd` detection); PR-6 only adds the per-run keys whose refusals it classifies |
| 17 | **VERIFIED (2026-09-16, run1, no key): TLS via the default trust store reaches `openrouter.ai`.** The injected truststore reaches `openrouter.ai` on a machine with a corporate CA (RH-IT-Root-CA.pem present in rfe-creator). The probe ran with httpx; the design uses stdlib `urllib.request` over the same default SSL context (Decision 24), which `agent_eval._bootstrap`'s `truststore.inject_into_ssl()` also covers — re-checked by a one-line `urllib` variant in PR-4's `test_openrouter_generation`. | No key: `.eval-venv/bin/python -c "import agent_eval._bootstrap, urllib.request; print(urllib.request.urlopen('https://openrouter.ai/api/v1/models').status)"`. | PR-4 (urllib re-check) |
| 18 | **VERIFIED as far as observable (2026-09-16, run1, no key):** `/endpoints` `status` values observed `{0, -2}` (Crusoe `-2` at 94.8 % uptime); the exact enum semantics remain undocumented. Endpoint `status` enum semantics and `uptime_last_30m` thresholds usable for preflight filtering. | No key: sample `/models/{slug}/endpoints` for the models in scope. Result: preflight treats `status < 0` as **degraded** (excluded from the eligible set under `strict`, WARN under `warn`). | PR-6 |
| 19 | **API half VERIFIED (2026-09-16, run1): bare, `:exacto` and `[1m]` all 200; `response.model` echoes the BARE slug in all three cases; metadata carries the dated permaslug. A `fallbacks`-served request cannot happen on the agent path (Claude Code sends no `models` list), and the CLI echo for bare/`:variant`/`[1m]` is already covered by #12.** The `[1m]` marker and `:variant` suffixes are handled by OpenRouter, and what `message_start.message.model` / `result.modelUsage` keys echo for a bare slug, a `:variant` slug and a `[1m]` slug. | **KEY** (done for the API; the CLI form of `:exacto` is a one-line extension of #12's direct run — record `modelUsage` keys and `GET /generation` `model` for it). Result: join rule = strip variants/`[1m]` on the request side AND expect a bare-slug echo; permaslug only in `/generation` (`model_served`); PR-4's join-rule fixture is cut from probe #12's direct captures. | PR-4 (`:exacto` CLI echo: low) |
| 20 | **VERIFIED (2026-09-16, run1/run2, via probe #12): Claude Code's `anthropic-beta` headers were tolerated by OpenRouter on a direct `claude --print` run against a non-Anthropic model** — the set 2.1.273+ sends is recorded once in the Direct transport contract (`claude-code-20250219, interleaved-thinking-2025-05-14, thinking-token-count-2026-05-13, context-management-2025-06-27, prompt-caching-scope-2026-01-05, mid-conversation-system-2026-04-07, mid-conversation-tool-changes-2026-07-01, effort-2025-11-2…`) as VERIFIED-tolerated.** Claude Code's `anthropic-beta` headers (1M context, interleaved thinking) are accepted or ignored by OpenRouter for non-Anthropic models. | **KEY**: direct `claude --print` run (done); the 1M-context alias (`[1m]`) on a direct run is covered by #2's podman e2e `RunResult.error_class`. | PR-5 (low) |
| 21 | **VERIFIED (by reading `harbor 0.13.1 agents/base.py:288-291`):** Harbor merges `--agent-env` last over its stock claude-code agent env. | Source read (no run needed); re-check on Harbor upgrades. | — |
| 22 | **RESOLVED for Claude Code 2.1.273+ (2026-09-16, `probes/probe_cli_report_2026-09-16.json`):** the CLI made **zero** calls to `POST /v1/messages/count_tokens` in a 3-turn direct run with a tool call and a subagent; OpenRouter's 404 on that route is moot for this version. **Watch item:** re-run `specs/014-openrouter-provider/probes/probe_claude_cli.py` on CLI upgrades; if `count_tokens` appears, OpenRouter 404s it. | No key: `specs/014-openrouter-provider/probes/probe_claude_cli.py` records every route the CLI hits; `test_claude_cli_direct` (e) asserts zero `count_tokens` calls so an upgrade that starts calling it fails CI. | (watch item) |
| 23 | **VERIFIED (2026-09-16, CLI 2.1.273+, `probes/probe_cli_report_2026-09-16.json`):** `claude --print --max-budget-usd 0` is **rejected** by the CLI ("--max-budget-usd must be a positive number greater than 0", exit 1, no request). Consequence: execute.py/claude_code.py must **not** pass `0` — a resolved cap `≤ 0`/`None` **omits the flag** (no CLI cap, `cli_cap_usd: null`); the key-guardrail `limit_usd` is unaffected (Budget mapping; Decision 10). | No key: `specs/014-openrouter-provider/probes/probe_claude_cli.py` (done). `test_claude_cli_direct` (f) asserts the flag is omitted for cap ≤ 0 on the recorded argv. | PR-5 |
| 24 | Claude Code's retry behaviour on a 503/529 with `Retry-After` (honoured? max attempts? backoff ceiling?). Informational — there is no harness cooldown to tune (Decision 11); the answer documents how long a case can stall on an OpenRouter-side provider outage before the case timeout is the only backstop, and feeds the `case timeout < PIPELINE_WAVE_STALL_SECS` guidance. | No key: echo server answers 503 + `Retry-After: 5` to `claude --print "say hi"`; time the attempts. | — (docs; low) |
| 25 | **UNVERIFIED:** Harbor's captured trial artefacts (`<trial>/agent/claude-code.txt`, read by harbor/results.py:163-168 → `_extract_transcript_metrics` :80-160) contain the assistant `message.id`s (`gen-…`) — it is Claude Code's own `--output-format stream-json`, so this is expected, but harbor 0.13.1's `installed/claude_code.py` post-processing has not been checked for id stripping. Determines whether per-trial cost on podman/K8s is per-request (`openrouter:generation`) or key-usage-only. | No key: one podman trial against the local echo server replaying the `basic` fixture (ids `gen-REDACTED-<n>`); grep the trial dir for `"id":"gen-`; also check `result.modelUsage` keys survive. Fallback if stripped: per-trial `cost_usd: null`, run-level `openrouter:key-usage`, `audit_complete: false` (Known limitations › Harbor podman). | PR-5 |
| 26 | **DOCUMENTED / UNVERIFIED:** management-API key guardrail semantics — `POST /api/v1/keys` field names for the allowed-provider list and `limit` (USD), whether the allow-list is per key or account-wide, how it interacts with the account's paid-training / ZDR data policy (the filter that made `deepseek-v4.1-flash` unroutable), whether a request outside the allow-list fails with a routing 404 or is silently re-routed, `limit` semantics (hard 402 vs soft), and that `DELETE /api/v1/keys/{hash}` revokes immediately. Needed for `enforcement: key-guardrail` to be **server-side** enforcement rather than a label. | **KEY (management)**: with `OPENROUTER_MANAGEMENT_KEY` exported, `specs/014-openrouter-provider/probes/probe_openrouter.py --management`: create a key with allow-list `[z-ai]` and `limit: 0.01`; one pinned-compatible request → 200 served by z-ai; one request on a slug z-ai does not serve → record status/body; spend past the limit → record the 402 body; `DELETE` → a follow-up request 401s; never prints key values. | PR-6 (`key-guardrail`) |
| 27 | **VERIFIED (2026-09-16, CLI 2.1.273+, `probes/probe_cli_report_2026-09-16.json`):** the multi-line value `HTTP-Referer: …\nX-OpenRouter-Title: …\nx-eval-run-id: …` was sent as **three separate headers** (all three observed) on root and subagent requests. Consequence: PR-5 sends Referer + Title (+ the optional run id) together; row 9 is the single-header case. | No key: `specs/014-openrouter-provider/probes/probe_claude_cli.py` (done); `test_claude_cli_direct` (d) asserts each line arrives as its own header on root and subagent requests. | PR-5 |

## Rollout plan

Each PR is independently shippable with green tests and no behaviour change for
configs that do not opt in. **Ordering rule (Decision 22):** a PR may depend only on code
shipped in the same or an earlier PR, so the cost substrate (ledger, reconcile, catalog,
generation client, readers) lands *before* the transport that feeds it. The first PR that
replaces rfe-creator's proxy-based setup is **PR-5, the MVP**: the direct transport for the
local claude-code runner **and** Harbor podman, with cost truth (gen-id `/generation`
backfill from PR-4) and `enforcement: audit` at its preflight minimum, so the first
proxy-free run already reports real `cost_usd` (`cost_source: openrouter:generation`) —
direct transport without cost truth would regress today's cost-reconcile setup and is not a
shippable milestone on its own. PR-6 completes preflight/audit/snapshot and adds
`key-guardrail`; PR-7 adds Kubernetes/EvalHub and docs.

- **PR-0 — Spec + probes (no behaviour change).** `specs/014-openrouter-provider/spec.md`
  (this document) and two **spec-local verification artefacts** under
  `specs/014-openrouter-provider/probes/` — probe scripts that live next to the evidence
  they produce, are not harness tooling (not under `scripts/`, not imported by `agent_eval/`
  or `skills/`, not part of the harness test suite) and are run by hand when the checklist
  needs refreshing. `specs/014-openrouter-provider/probes/probe_openrouter.py`
  implementing the checklist (no-key probes runnable in CI, KEY probes when the variable is
  exported, `--management` probes when `OPENROUTER_MANAGEMENT_KEY` is exported; never prints
  values); `probe_report.json` schema; the evidence directory
  `specs/014-openrouter-provider/probes/` holding the committed reports
  (`probe_report_2026-09-16_run1.json`, `_run2.json`: results, timings and redacted shapes
  only — no key material, no bodies) that the Verification checklist cites;
  `--record-fixtures` writes the redacted `tests/data/openrouter/*.sse` golden fixtures
  that the echo server replays. `specs/014-openrouter-provider/probes/probe_claude_cli.py` runs the no-key Claude Code CLI probes (rows 1, 9, 10, 22, 23,
  27) against a local fake Anthropic endpoint (records path incl. the `?beta=true` query,
  headers, the subagent request and the hook subprocess env; never prints secret values) and
  writes `probes/probe_cli_report_2026-09-16.json` (Claude Code 2.1.273+), the second
  committed evidence file; `tests/test_claude_cli_direct.py` (PR-5) is its CI form. Tests:
  none for the probe scripts themselves (spec-local artefacts outside the harness suite);
  `tests/test_fixtures_lint.py` lints the fixtures they record. Status: the KEY probes
  3–8, 12, 13, 16 (keep-alive half), 19 (API half), 20 and the no-key 17/18 ran on 2026-09-16
  (`probe_openrouter.py`); the no-key CLI rows 1 (incl. subagent/hook children), 9, 10, 23,
  27 are VERIFIED and 22 RESOLVED on 2.1.273+ (`probe_claude_cli.py`); 14 VERIFIED on the
  installed `openai` 2.30.0/2.46.0. **Open:** #2 (direct podman e2e),
  #11 (container egress to `openrouter.ai`, podman machine was down), #16
  (echo-server half: SSE `error` / 402 surfacing), #25 (Harbor trial ids) gate **PR-5**; #26
  (key-guardrail semantics, management KEY) gates **PR-6**; #15 is a PR-7 guard; #24 is
  informational; #14 is pinned to the 1.70 floor by PR-1 CI.
- **PR-1 — `openrouter:/` judge backend.** prompt_backends branch (→ `openai` transport) +
  `resolve_judge_client`/`JudgeClientConfig`; `_client_for`; `client`/`extra_body`/`token_param`
  on the shared OpenAI-shaped calls; dict-arguments tolerance; committed-200 detection; retry
  wrapper + semaphore; the Decision 25 judge `tool_choice` ladder (`function` → `required` →
  `auto` + strict parse of the first tool call, `JudgeProviderError` never text fallback, the
  routing 404 as `error_class: config`) with `tool_choice_mode` on the judge usage/ledger
  record and `judge_usage.tool_choice_fallbacks`; `judge.inherit_pins` (default `false`) and
  the per-role `to_chat_extra_body()` rule; `JudgeConfig.provider_options` (kind-validated); a minimal `models.providers.openrouter` parse
  (`base_url`, `attribution`, `routing`, `judge` options only — the full `ProvidersConfig`, plan
  build and cross-surface validation land in PR-3b) so `resolve_judge_client(model,
  config.models.providers)` has its input under the ordering rule; config-load acceptance;
  ensure_deps alignment; generate_synthetic rejection; Containerfile `openai`; docs rows.
  Immediately usable with `OPENROUTER_API_KEY` exported. Tests: test_prompt_backends,
  test_score_builtin, test_pairwise_providers, test_llm_rubric_scoring, test_config,
  test_ensure_deps, test_synthetic_generation.
- **PR-2 — Judge usage side channel (all backends).** `JudgeOutcome`, 3-tuple
  `_normalize_result`, per-case usage, `summary.judge_usage`/`total_cost_usd`, report
  `Judge cost` row, MLflow metrics. No `run_result.json` change. Tests:
  test_llm_rubric_scoring, test_score_range_enforcement, test_report_markdown, test_log_results.
- **PR-3a — Config overlay (generic).** `load_raw` + `extends:` + `deep_merge`
  (lifted from workspace.py, `!replace`) + `config_chain` + `--print` with provenance; every
  raw reader routed through `load_raw` (tasks.py, harbor/run.py, evalhub/runner.py, report.py,
  validate_eval.py, discover_configs with `include_profiles`, anova/matrix.py, reorganize.py);
  ensure_deps profile follow; `task.toml metadata.config_chain`. Tests: test_config,
  test_config_raw_readers, test_harbor_task_generation (overlay bundle), test_report
  (overlay renders base judges), test_ensure_deps. `extends:` is a **new, optional**
  top-level eval-config key (not an existing harness feature): `extends: <path relative to
  the file>` deep-merges the overlay over the base in the single raw loader, so every reader
  sees the merged config and `eval_params.config_chain` records the chain. This PR is
  **independent of the OpenRouter feature and droppable** — its motivation is the
  `eval.yaml` / `eval-openrouter.yaml` drift. rfe-creator now generates `eval.yaml` from a
  skeleton + per-type fragments (`scripts/generate_eval_config.py`); the generated file
  stays the base and an OpenRouter profile `extends:` it, so the generator is untouched and
  rfe-creator can convert `eval-openrouter.yaml` to an overlay immediately after this PR.
  (Alternatively the generator could emit per-provider configs directly and PR-3a would be
  skipped.)
- **PR-3b — `models.providers` block (single kind, Decision 1; nested under `models`, Decision 17).** `agent_eval/providers/` (base,
  env with `settings_env_block`/`MANAGED_ENV_KEYS` and the alias derivation of Decision 9,
  `openrouter/routing.py` — `RoutingSpec` with `to_chat_extra_body()` for judges and the
  pinned-set view the audit and the key guardrail consume, `openrouter/plan.py` —
  `ProviderPlan` build with `runner ∈ {claude-code, harbor-podman, harbor-k8s, evalhub}`
  selecting only *how the env block is delivered*, `routing.enforcement ∈ {audit,
  key-guardrail}` parsed and validated (`key-guardrail` requires `management_key_env` present
  in the process env and `budget.run_usd` set), `plan.close()` as a no-op hook for now),
  `ModelsConfig.providers: ProvidersConfig(openrouter: OpenRouterConfig | None)` with the reserved `kind` and the
  **rejection of reserved keys** (transport mode/options keys and
  `direct.*` → `ConfigError` pointing at Decision 1; any other unknown provider name or URI
  prefix is an ordinary unknown-name/unknown-prefix error), all
  load-time validation (managed-key ownership across six surfaces, `ANTHROPIC_API_KEY`
  non-empty = warning, `OPENROUTER_API_KEY`/`OPENROUTER_MANAGEMENT_KEY` env-only, bare-id
  footgun, WARNING on `require_parameters`/`sort`/`data_collection`/`zdr`/`max_price` under a
  routing key an agent role uses — not sendable from Claude Code, ignored on that path),
  `_inject_env` `None` fix, `settings_env_block(plan)` unit tests. The env writers and the
  cross-writer conformance test (`test_env_writers_conformance.py`) land **with their
  writers** — overlay, `--agent-env` and interception legs in PR-5, the K8s `_pod_manifest`
  leg in PR-7 — per the ordering rule. Nothing consumes the plan yet. Tests: test_config,
  test_providers, test_openrouter_routing.
- **PR-4 — Cost substrate: ledger + reconcile + catalog + generation client + readers
  (no producer wired yet).** `providers/ledger.py` (thread-safe append, `read(...)`,
  `source ∈ {generation, key-usage, judge}`, `role`, `provider_kind`),
  `providers/reconcile.py` (join rule, unattributed path, `cost_confidence` by coverage,
  budget fields incl. the post-hoc `run_usd` check, hook cost, null-cost arithmetic),
  `openrouter/http.py` (stdlib `urllib.request` JSON client over the truststore-injected
  default context: GET/POST/DELETE, `Retry-After`, timeouts, no key in exceptions — Decisions
  8 and 24), `openrouter/catalog.py` (cached public GETs: `/models`, `/providers`,
  `/models/{slug}/endpoints`; `ModelCatalog` for the permaslug join and the
  provider→quantization map the audit needs; `pricing_for(slug, provider)` for the report's
  price context), `openrouter/generation.py` (`backfill(ids)` worker: first poll ~5 s, then
  every 2 s, give up at 60 s, run-end retry, 429 `Retry-After`, coverage math; writes
  `source: generation` records with `provider_name`/`total_cost`/`model` — probe #6/#12
  defaults; plus the key-usage delta reader with the 60 s settle, probe #7), `RunResult`
  fields (`message_ids`, `cost_source`, `cost_coverage`, `routing`, `provider`, `budget`),
  reconcile at **all** execute.py `run_result.json` write sites and harbor/run.py:663,
  `--strict-cost`, report rows/banner, compare/anova mixed-source refusal with legacy
  literals, MLflow tags. Nothing writes a ledger yet: with no plan and no ledger file
  reconcile leaves `run_result.json` untouched (the `provider inactive → untouched` test), so
  this PR has no behaviour change. Acceptance: reconcile over a ledger fixture converted from
  a past glm-5.2 run's proxy log reproduces its `real_cost.json` within 5 %, and `backfill()`
  against a stub `/generation` reproduces probe #12's `0.00165633` from its recorded gen id
  (the live re-run acceptance is PR-5's). Gated by probes 6, 12, 17 (urllib re-check), 19.
  Tests: test_providers_ledger, test_providers_reconcile, test_openrouter_generation
  (stubbed 404-then-200 backoff, 429 `Retry-After`, coverage math, key-delta settle,
  `urllib` truststore one-liner), test_report_*, test_compare, test_anova, plus the write-site
  guard `tests/test_execute_write_sites.py` (all eight execute.py `run_result.json` writers —
  :982, :1117, :1168, :1392, :1426, :1679, :1768, :1840 — and harbor/run.py:663 go through
  `write_run_result` → `reconcile`).
- **PR-5 — MVP: direct transport for the local claude-code runner AND Harbor podman,
  with cost truth (backfill) + `enforcement: audit` at its preflight minimum (replaces
  rfe-creator's proxy-based setup; Decisions 1/23).** *Local runner:* `settings_env_block()`
  emitted through the generalised settings overlay (`.eval-overlay.json`, 0600, plan-wins
  merge `{**existing_env, **plan}`, unlinked in a `finally`), managed-key strip in
  `_build_env`, CLI backstop `cap × cli_budget_inflation` (no sentinel, cap ≤ 0 = no
  CLI cap), `stream_capture.py` collecting every assistant `message.id` (root, subagent,
  background `generate_session_title`, hook children) into `RunResult.message_ids`,
  `openrouter/errors.py` (`error_type` → `ErrorClass`, the `infra | config | agent` taxonomy)
  classifying the runner's error output into `RunResult.error_class`, and
  `stream_capture.py`/`results.py` detecting OpenRouter's key-limit 402 → `budget.exceeded:
  "run"`, `exceeded_reason: "limit_usd"` (classification lands here because PR-5's tests
  assert it; the per-run keys that produce such 402s arrive with PR-6),
  execute.py startup order (config → plan → preflight minimum → run → `plan.close()`:
  backfill via PR-4 `generation.py`, key-usage settle/read, last reconcile) and the
  activation rule (URI-driven, Decision 21), `--effort` passthrough, tools.py hook client
  inheriting the overlay env (`role: hook` by id), stderr notice `key exposed to agent:
  operator key (enforcement: audit)`, `eval_params.provider`/`budget`/`routing`. *Harbor
  podman:* harbor/run.py builds the same plan (`runner="harbor-podman"`), runs the preflight
  minimum before task generation, passes the env block via **`--agent-env`** (value-free
  argv via carriers, run.py:216-233; Harbor merges it last, `harbor 0.13.1 agents/base.py:288-291`),
  podman.py forwards **no** `CLAUDE_CODE_USE_VERTEX`/`ANTHROPIC_VERTEX_PROJECT_ID`/
  `CLOUD_ML_REGION` while a plan is active (podman.py:36-49 conditional; CA/proxy vars still
  forwarded), `MANAGED_ENV_KEYS` child-env scrub, the existing `config_chain` reuse check on
  `--tasks-dir` (no `provider_sha` — nothing provider-related is baked; run.py section), results.py extracts the trial transcript's `gen-…` ids (:80-168) and the
  host-side backfill + reconcile run before `run_meta`; `try/finally: plan.close()` around
  :606-663. *Preflight minimum* (`openrouter/preflight.py`, ≤ 120 lines): slug exists, pinned providers serve the model, key valid (`GET /key`), `/models/user` eligibility (paid-training case → strict FAIL / warn), catalog-failure degrade, `routing_snapshot.json` written (the deepseek-v4.1-flash case is the fixture); the full audit fields are
  PR-6. Configuring `routing.enforcement: key-guardrail` before PR-6 lands: PR-3b validation
  accepts the key, but PR-5's plan build raises `ConfigError: routing.enforcement:
  key-guardrail lands in PR-6`. *Audit minimum* (`openrouter/audit.py`): join backfilled `provider_name` against the
  pinned set → `routing.violations`/`audited`/`compliant`/`unattributed`,
  `routing_enforcement: audit`, `audit_complete`; compare/anova refuse to pool differing
  audits (`--allow-unaudited`). No listener, no framing, no timeouts, no cooldown, no
  `count_tokens` synthesiser, no tokens, no httpx. After this PR rfe-creator runs without
  a proxy on the local runner **and** under podman, and `run_result.json` carries real
  `cost_usd` with `cost_source: openrouter:generation`; `cost_usd_estimate` is kept
  alongside. Size guard: env ≤ 120, plan ≤ 150, stream_capture delta ≤ 60,
  preflight (minimum) ≤ 120, audit ≤ 120, results.py delta ≤ 60 lines excluding tests;
  anything that does not fit moves to PR-6. Acceptance: (1) re-run a past glm-5.2 case set
  locally and match its proxy-era `real_cost.json` within 5 % with `cost_coverage ≥ 0.95`;
  (2) one podman `harbor run` with `--config eval-profiles/<overlay>.yaml`, with and without
  `--no-llm-judges`, plus a `--tasks-dir` reuse of packages generated from that overlay,
  ending with per-trial `cost_source: openrouter:generation` and `routing.violations == []`
  (probe #2); (3) the same runs with `models.providers` removed are byte-identical to today.
  PR-5 ships the **full attribution header set** (`HTTP-Referer` + `X-OpenRouter-Title`
  [+ `x-eval-run-id`]) as one multi-line `ANTHROPIC_CUSTOM_HEADERS` (row 27 VERIFIED) and
  the budget-flag mapping `cap ≤ 0`/`None` → no `--max-budget-usd` (row 23 VERIFIED: the CLI
  rejects `0`). Gated by probes 2, 16 (echo-server half), 25 (blocking) and 11 (container
  egress to `openrouter.ai`, podman acceptance only); probes 1 (incl. subagent/hook
  children), 9, 10, 23, 27 are VERIFIED and 22 RESOLVED on Claude Code 2.1.273+
  (`probes/probe_cli_report_2026-09-16.json`), and probes 3, 12, 13, 17 and 20 are VERIFIED
  (`probes/probe_report_2026-09-16_run{1,2}.json`), so they do not gate. Tests:
  test_env_writers_conformance (overlay/`--agent-env`/interception legs),
  test_claude_cli_direct (local echo server, no key; the CI form of
  `specs/014-openrouter-provider/probes/probe_claude_cli.py`; automates probes 1, 9, 10, 16 (echo-server half), 22, 23,
  24 and checklist row 27 (multi-header `ANTHROPIC_CUSTOM_HEADERS`) — overlay beats user
  settings, subagent auth, fixture replay → `message_ids` + one `GET /generation` per id,
  zero `count_tokens` calls, 402/`error` surfacing, `--max-budget-usd` omitted for cap ≤ 0),
  test_stream_capture (id collection incl. background
  and hook ids; `errors.py` classification into `RunResult.error_class`, the key-limit 402
  into `budget.exceeded_reason: limit_usd`), test_openrouter_preflight (minimum subset), test_openrouter_audit
  (violations/unattributed/`audit_complete`, pooling refusal), test_harbor_run (plan built
  before task generation, `--agent-env` carries the block, `plan.close()` on
  `KeyboardInterrupt`), test_harbor_podman (`test_podman_forward_excludes_under_plan`),
  test_harbor_results (gen-id extraction, per-trial `cost_usd` from rows, fallback when
  ids are absent), test_execute_result_fields, test_extract_progress, test_secrets_hygiene
  (run dir + stderr + child env + `--agent-env` argv grep with fake host Anthropic and
  OpenRouter credentials; post-run/post-interrupt overlay removal).
- **PR-6 — Full preflight + audit + snapshot, and `enforcement: key-guardrail`.**
  `preflight.py` grows from the PR-5 minimum to the full per-slug audit on top of PR-4
  `catalog.py`: pinned endpoint per provider **at the declared quantization** (indirect pin,
  Known limitations), `supports_tool_choice` per role (`auto` for agent slugs, `function`
  for judge slugs; a pinned set supporting none of the role's mode → FAIL under `strict`,
  Decision 25), `max_completion_tokens`, `status < 0` = degraded (probe #18), `:exacto`
  membership warning, provider-id normalisation (probe #4), `/models/user` eligibility
  reasons; `routing_snapshot.json` full content; the snapshot's `infra_errors` view over
  PR-5's `errors.py` classes; `python3 -m agent_eval.providers.openrouter.preflight` CLI; `audit.py`
  gains the `/endpoints` join for quantization (`served: {"novita/fp8": N}`), `policy:
  strict|warn` → `routing.degraded`/`degraded_reason`, and the eval-compare snapshot diff.
  **`key-guardrail`** (`openrouter/keys.py`): `provision()` → `POST /api/v1/keys` with
  `OPENROUTER_MANAGEMENT_KEY` as Bearer, name `agent-eval <run_id>`, `limit ==
  budget.run_usd`, allowed providers == the union of every routing key's pinned set (or the
  explicit `guardrail.providers`); the per-run key replaces the operator key on every env
  target (`key_scope: per-run`, `key_hash`); `/key` and `/models/user` queried with it;
  `revoke()` (`DELETE /api/v1/keys/{hash}`) in `plan.close()` on **all** exit paths incl.
  `KeyboardInterrupt`, with `agent-eval provider revoke <run_dir>` for a failed revoke;
  the per-run key's `limit_usd` 402 is already classified by PR-5's
  `stream_capture.py`/`results.py` (`budget.exceeded: "run"`, `exceeded_reason: "limit_usd"`)
  — PR-6 only makes such refusals occur; `budget.enforcement: key-guardrail` in
  `eval_params` and the report. Podman inherits it through `--agent-env`
  with no further change. Gated by probe #26 (management KEY; the PR lands only after it
  runs — field names are DOCUMENTED/UNVERIFIED until then); the attribution headers are
  PR-5's (row 27 VERIFIED) and the
  `limit_usd` 402 classification it relies on is PR-5's (probe #16, echo-server half). Tests: test_openrouter_preflight (full audit cases incl. the
  deepseek-v4.1-flash fixture, quantization mismatch, `supports_tool_choice` per role,
  degrade rules), test_openrouter_audit (quantization join, strict vs warn, snapshot diff),
  test_openrouter_keys (**fake management API** on loopback, plain `http.server`, no
  OpenRouter key: request builder is the only place the allow-list is computed; revoke on
  success/failure/interrupt; per-run key never equals the operator key in any env target;
  `key_hash` written, value never), test_openrouter_routing (audit view),
  test_secrets_hygiene (management key never in argv/env of the CLI or container),
  test_compare (`enforcement` as a factor level, `--allow-unaudited`).
- **PR-7 — Kubernetes + EvalHub + docs.** kubernetes.py: credentials Secret carries
  `OPENROUTER_API_KEY`; the harness maps it to `ANTHROPIC_AUTH_TOKEN` via `secretKeyRef` and
  adds the plan's non-secret env to the container `env[]` (wins over `envFrom`); Vertex vars
  already excluded (:42-50, unchanged); exec-prefix suppression at :547; at
  `key-guardrail` a per-run Secret `agent-eval-<run_id>-openrouter` created before the Job
  and deleted in `plan.close()` (`create`/`delete` on `secrets` documented, Known
  limitations); `create_openrouter_secret` helper; results.py id extraction reused
  unchanged (same trial layout); EvalHub inherits through the env pass-through and reports
  `cost_source`/`routing` from the reconciled `run_result.json` (evalhub/runner.py:206 via
  `load_raw`); optional `x-eval-run-id` custom header (probe #9) if wanted for OpenRouter
  activity filtering; a job-dir poller (live per-trial cost lines) if cheap. Docs: `reference/config/providers.md` (the `openrouter` kind, every knob, `extends:`
  policy) and `guides/openrouter.md` (direct transport, levels, secrets, limitations), execution.md rows,
  runner.md ownership rule, README quick-start (`--model openrouter:/…`), migration notes.
  Acceptance: one K8s run (`audit`) ending with `cost_source: openrouter:generation` and
  `cost_confidence: high`; one K8s run at `key-guardrail` whose per-run Secret is gone after
  the run; one K8s run on an existing Secret-based Anthropic-compatible-endpoint config with no
  `models.providers` block, unchanged. Gated by probes 9 and 15 (guards, low). Tests: test_harbor_kubernetes
  (`secretKeyRef` mapping, `env[]` precedence, exec-prefix, no Vertex vars),
  test_harbor_k8s_resources (per-run Secret create/delete, name, labels),
  test_env_writers_conformance (K8s `_pod_manifest` leg added), test_harbor_task_generation, test_harbor_results, test_report_markdown (docs rows).
- **PR-8 — rfe-creator migration** (rfe-creator repo). Routing table into `eval.yaml`,
  `eval-profiles/openrouter-*.yaml` with `extends: ../eval.yaml`, retire `eval-openrouter.yaml`,
  the proxy directory (config + `custom_callbacks.py` monkeypatch) and
  `reconcile_cost.py` with **no replacement proxy**, `.gitignore` `.env`, README/MEMORY updates. Acceptance:
  glm-5.2 v5 re-run under podman compared against the last proxy-reconciled baseline
  (cost within 5 %, `routing.violations == []`); merged profile keeps `Skill`/`Agent`.
- **PR-9 (future) — other runners, other provider kinds.** Codex `OPENAI_BASE_URL`
  mapping, cli runner placeholders, `kind: openai-compatible` judge providers and other
  kinds (an MLflow AI Gateway or OpenShift AI gateway provider, ...; no name or scheme
  reserved here). See "Out of scope (future)".
