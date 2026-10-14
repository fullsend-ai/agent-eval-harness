# `models.providers` — the provider registry

This page is the block reference. For the provider-neutral model — which roles accept which
URI, the three provider families, credentials per backend — read
[Model providers](../../concepts/providers.md).

`models.providers` declares how a `<provider>:/<model>` URI on any role (`models.skill`,
`subagent`, `hook`, `judge`, a per-judge `model:`, the `--model` / `--subagent-model`
flags, and the Harbor runner's `--judge-model`, which applies to task generation only) is
served. It lives under `models` because it exists only to resolve those URIs. One provider
kind exists, **`openrouter`** (the block, the direct agent transport, the routing snapshot
and the audit since v1.50.0; the full per-slug preflight, audit-aware pooling and a working
`enforcement: key-guardrail` since v1.51.0; Kubernetes and EvalHub delivery since
v1.53.0), and the block is optional: an `openrouter:/…` judge
works with the defaults and `OPENROUTER_API_KEY` exported, and an `openrouter:/…` skill
model activates the direct agent transport with the defaults too. A declared block is **inert until a role names it** — a base `eval.yaml` can
hold the shared routing table while its roles stay on Anthropic, and a
[profile](extends.md) flips `models.skill` to `openrouter:/…` to activate it.

For the narrative (what the transport does, what each enforcement level guarantees, cost
provenance, secrets) read the [OpenRouter guide](../../guides/openrouter.md); the
[models](models.md) page covers the roles and their precedence.

## The `openrouter` block

```yaml
models:
  skill:    openrouter:/z-ai/glm-5.2:exacto
  providers:
    openrouter:
      kind: openrouter                          # optional; must equal the block name
      api_key_env: OPENROUTER_API_KEY           # variable holding the inference key (never the value)
      management_key_env: OPENROUTER_MANAGEMENT_KEY  # variable holding the management key (key-guardrail only)
      base_url: https://openrouter.ai/api       # no /v1; https unless loopback
      attribution: { referer: https://…, title: agent-eval-harness, run_id_header: false }
      background_model: null                    # the haiku slot + the default hook model; null = the model under test
      preflight: strict                         # strict | warn | off
      cli_budget_inflation: 50                  # execution.max_budget_usd × this → --max-budget-usd
      budget: { run_usd: null, dedicated_key: false }
      routing:
        defaults: { allow_fallbacks: true }
        models:
          z-ai/glm-5.2: { order: [novita], allow_fallbacks: false, quantizations: [fp8] }
        policy: strict                          # strict | warn
        enforcement: audit                      # audit | key-guardrail
        guardrail: { key_name: "agent-eval {run_id}", providers: pinned, revoke_on_exit: true, settle_s: 20 }
      judge:
        concurrency: 4
        max_retries: 3
        timeout_s: 300
        extra_body: {}
        inherit_pins: false
```

| Key | Default | Meaning |
| --- | --- | --- |
| `kind` | `openrouter` | Must equal the block name. `openai-compatible` is rejected with a pointer to `openai:/…` plus `OPENAI_BASE_URL` (the loader's message reads "not implemented in this release"); any other block name is rejected as an unknown provider. |
| `api_key_env` | `OPENROUTER_API_KEY` | Name of the environment variable holding the inference key. The key itself may never be authored in a config or on any `env:` surface; a value that looks like a key is rejected. On Kubernetes the same key also lives in the credentials Secret under this name. |
| `management_key_env` | `OPENROUTER_MANAGEMENT_KEY` | Name of the variable holding the management key, read only at `enforcement: key-guardrail` to provision the per-run key. Never forwarded anywhere. |
| `base_url` | `https://openrouter.ai/api` | Origin the agent (`ANTHROPIC_BASE_URL`) and the harness clients talk to. No `/v1` suffix (the harness appends it). Plain `http` is allowed for a loopback host only. |
| `attribution.referer` / `.title` | `null` / `agent-eval-harness` | `HTTP-Referer` / `X-OpenRouter-Title` sent by the judge client and, as `ANTHROPIC_CUSTOM_HEADERS`, by the agent. Single line each. |
| `attribution.run_id_header` | `false` | Adds `x-eval-run-id: <run id>` to the agent's headers (OpenRouter activity-page tagging only; cost attribution comes from generation ids). |
| `background_model` | `null` | The haiku slot (`ANTHROPIC_DEFAULT_HAIKU_MODEL`) and the default hook model. `null` = the model under test, so a second model never appears silently in `per_model_usage`. |
| `preflight` | `strict` | Catalog, key and eligibility checks before any spend (see the [guide](../../guides/openrouter.md#the-routing-lifecycle)). `strict` fails the run; `warn` continues degraded; `off` skips and writes no snapshot. |
| `cli_budget_inflation` | `50` | `execution.max_budget_usd × cli_budget_inflation` is what `--max-budget-usd` receives — the CLI enforces that cap on its Anthropic-priced estimate, 2 to 60 times the real OpenRouter cost. Must be ≥ 1; a cap of 0 omits the flag. |
| `budget.run_usd` | `null` | The whole-run real-dollar pool: enforced after the run at `audit` (`budget.exceeded: run`, `--strict-cost` exits 2) and server-side, in flight, at `key-guardrail` (it becomes the per-run key's limit; required and > 0 there). |
| `budget.dedicated_key` | `false` | The operator's assertion that nothing else spends on the key during the run; raises a key-usage-only `cost_confidence` from `low` to `medium`. Implied at `key-guardrail`. |
| `routing.defaults` / `routing.models` | `{}` | OpenRouter provider-routing declarations (`order`, `only`, `ignore`, `allow_fallbacks`, `quantizations`, `require_parameters`, `sort`, `data_collection`, `zdr`, `max_price`, plus `fallbacks` for the `models` array). `models` is keyed by the bare slug; one entry covers every `:variant`. |
| `routing.policy` | `strict` | What a failed routing audit (checked after the run) does to the run: `strict` marks it `degraded` (`--strict-routing` exits 2), `warn` flags it. |
| `routing.enforcement` | `audit` | `audit`: preflight plus an audit after the run; the agent holds the operator key. `key-guardrail`: a per-run key with a provider allow-list and a real-dollar limit, provisioned through the management API and revoked at run end. |
| `routing.guardrail.key_name` | `agent-eval {run_id}` | Name of the per-run key (`{run_id}` substituted). |
| `routing.guardrail.providers` | `pinned` | The per-run key's allow-list: `pinned` = the union of the pinned sets of the routing keys the agent roles use, or an explicit list of provider slugs. A guardrail with no provider restriction is a validation error. |
| `routing.guardrail.revoke_on_exit` | `true` | Always `true`; `false` is rejected at load. |
| `routing.guardrail.settle_s` | `20` | Key-usage settle before the run-end read; a shorter value warns (the counter settles in about 20 s). |
| `judge.concurrency` | `4` | Concurrent OpenRouter judge requests. |
| `judge.max_retries` | `3` | Retries on 429 (`Retry-After`), 502/503 and provider-unavailable replies. |
| `judge.timeout_s` | `300` | Per judge request. |
| `judge.extra_body` | `{}` | Static request additions, merged last. |
| `judge.inherit_pins` | `false` | Judges send `order`/`only`/`quantizations` only when true (or when a judge carries its own `provider_options.routing`). Rejected when `routing` is absent. |

### Agent path vs judge path

Claude Code cannot put a `provider` object in its requests, so on the **agent path** only
`order`, `only`, `ignore`, `quantizations` and `fallbacks` mean anything: pins are checked
by the preflight and audited after the run, and the `:variant` on the model id
(`:exacto`, `:nitro`, `:floor`) is the only in-request routing control.
`require_parameters`, `sort`, `data_collection`, `zdr` and `max_price` on a routing key an
agent role uses raise a load-time warning and are ignored there. Judges that inherit pins
receive the full declaration. See [judges → OpenRouter judges](judges.md#openrouter-judges)
for what a judge sends: the verdict transport (structured output with
`require_parameters`, then the `tool_choice` ladder).

### Validation

Fail fast at load, one consolidated error per category: unknown keys; reserved keys
(`transport`, `direct`, `proxy`, `gateway`, `generation_backfill`, `key_exposure_ack`,
`budget.max_unpriced*`) named as unsupported; `key-guardrail` without `budget.run_usd`
or without pins / an explicit allow-list; `revoke_on_exit: false`; `judge.inherit_pins`
without a routing table; with the block declared but no plan active, a bare agent id
that has a routing entry or is not an Anthropic id ("bare-id footgun" — write
`openrouter:/<id>` or drop the pins); `subagent` / `hook` on a
different provider kind than the skill; `runner.type` other than `claude-code` for an
`openrouter:/` skill model (also per step); an `agent:` judge whose model is
`openrouter:/…`. The **managed-key ownership** rule applies while a plan is active:
`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_CUSTOM_HEADERS` are rejected
on presence on every env surface (`execution.env`, `runner.env`, `runner.settings.env`,
the per-step variants), static managed keys must equal the plan's values, a non-empty
`ANTHROPIC_API_KEY` warns, and `OPENROUTER_API_KEY` / `OPENROUTER_MANAGEMENT_KEY` may
never be authored anywhere, plan or no plan.

## Profiles: `extends:` and this block

The usual pattern keeps the routing table in the base `eval.yaml` (roles on Anthropic,
block inert) and activates it from a profile:

```yaml
# eval-profiles/openrouter-glm-5.2.yaml
extends: ../eval.yaml
models:
  skill: openrouter:/z-ai/glm-5.2:exacto
  providers:
    openrouter:
      routing:
        models:
          z-ai/glm-5.2: { order: [novita], allow_fallbacks: false }
```

The overlay is deep-merged over the base by the single raw loader: mappings merge key by
key, so a profile can add or change one knob without restating the block; lists (an
`order`, `quantizations`, a `fallbacks` array) **replace** the base list — use the
`!replace` tag on a mapping to swap it wholesale instead of merging. Paths in the overlay
(`extends:` itself, `dataset.path`, `prompt_file`, …) resolve relative to the file they
appear in; `eval_params.config_chain` records the chain, and
`python3 -m agent_eval.config --print <profile>` shows the merged result with provenance.
See [extends](extends.md).

<a id="where-the-key-goes-per-runner"></a>

## Where the key goes, per backend

| Backend | How the agent gets `ANTHROPIC_AUTH_TOKEN` | Host variables needed |
| --- | --- | --- |
| local `claude-code` | the per-run settings overlay (`.eval-overlay.json`, 0600, removed at exit) | `OPENROUTER_API_KEY` (+ `OPENROUTER_MANAGEMENT_KEY` at `key-guardrail`) |
| Harbor podman | value-free `--agent-env` carriers (the value in the harbor child env) | same |
| Harbor Kubernetes / OpenShift | `valueFrom.secretKeyRef` from the credentials Secret (`AGENT_EVAL_K8S_CREDENTIALS_SECRET`, key `OPENROUTER_API_KEY`) at `audit`; from the per-run Secret `agent-eval-<run_id>-openrouter` at `key-guardrail` | `audit`: `OPENROUTER_API_KEY` on the host (preflight, backfill, key usage) **and** in the credentials Secret; `key-guardrail`: `OPENROUTER_MANAGEMENT_KEY` on the host, `OPENROUTER_API_KEY` only for `openrouter:/` judges (then also in the credentials Secret) |
| EvalHub | the pod's own environment (credentials injected by the cluster), through the same overlay in the pod | `OPENROUTER_API_KEY` in the job pod (+ `OPENROUTER_MANAGEMENT_KEY` at `key-guardrail`) |

The management key is never forwarded to any of them. The cross-provider version of this
table — Anthropic direct, Vertex, Bedrock, OpenRouter and OpenAI on every backend — is
[Model providers → Credentials by backend](../../concepts/providers.md#credentials-by-backend).
