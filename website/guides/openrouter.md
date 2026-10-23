# Running on OpenRouter

OpenRouter is a first-class provider for both the **agent under test** and the **LLM
judges**: write `openrouter:/<author>/<slug>[:variant]` on a
[role](../reference/config/models.md#the-four-roles) and the harness does the rest. This
guide is the narrative; the knobs are in [`models.providers`](../reference/config/providers.md);
the provider-neutral mental model — which roles accept which URI, how the agent path differs
from the judge path, where credentials come from on each backend — is
[Model providers](../concepts/providers.md).

```yaml
models:
  skill:    openrouter:/z-ai/glm-5.2:exacto     # the agent under test
  judge:    openrouter:/z-ai/glm-5.2            # the judges (optional; see the judge note below)
  providers:
    openrouter:
      routing:
        models:
          z-ai/glm-5.2: { order: [novita], allow_fallbacks: false }
```

```bash
export OPENROUTER_API_KEY=…
/eval-run --config eval-profiles/openrouter-glm-5.2.yaml
```

!!! note "The judge role is independent — except on Harbor"
    A Claude or `openai:/` judge next to an OpenRouter agent works on Local and EvalHub,
    where the judge engine runs in the harness process or job pod with its own
    environment. On Harbor under a plan the trial container carries **no Anthropic
    credentials** (Podman strips them; on Kubernetes the plan's `ANTHROPIC_API_KEY=""`
    and blanked Vertex entries are explicit pod env and win over the credentials Secret),
    so an in-container LLM judge there must be `openrouter:/` or `openai:/` (or
    deterministic). See [Model providers → Agent path and judge path](../concepts/providers.md#agent-path-and-judge-path).

## Direct transport, no proxy

The agent's Claude Code process talks to `https://openrouter.ai/api/v1/messages`
**directly**. The harness derives one env block from the role URIs — `ANTHROPIC_BASE_URL`,
`ANTHROPIC_AUTH_TOKEN` (the inference key), a blank `ANTHROPIC_API_KEY` and blank
Vertex/Bedrock variables, the `ANTHROPIC_DEFAULT_*_MODEL` / `CLAUDE_CODE_SUBAGENT_MODEL`
aliases so every slot Claude Code picks by name resolves to an OpenRouter id, the
attribution headers — and hands it to the runner: a 0600 settings overlay locally,
value-free `--agent-env` carriers on Harbor podman, the pod spec plus a Secret on
Kubernetes. `claude --model` receives the bare `slug:variants`; the `openrouter:/` scheme
is the harness's and never reaches the CLI. There is no listener, no framing, no request rewriting and no second
transport mode. The harness deliberately does not ship a proxy: one would put it on the
HTTP path for nothing the design needs, since cost, attribution and routing can all be read
back from OpenRouter itself. An operator who already fronts Claude Code with an
Anthropic-compatible endpoint keeps doing so through plain `execution.env`; that path is
untouched and outside this feature (`cost_source: runner:estimate`).

Claude Code cannot send OpenRouter's `provider` routing object, so the only in-request
routing control is the **`:variant`** on the model id (`:exacto` is the recommended one
for tool-calling agents; `:nitro` / `:floor` sort by throughput / price). Everything else
about pins is enforced around the request, by the two levels below.

## Enforcement levels

### `audit` (default)

1. **Preflight** before any spend (see below); writes `provider/routing_snapshot.json`.
2. The request carries only the variant. Nothing is injected.
3. **Audit** at reconcile: every billed generation's served provider is joined against
   the pinned set → `routing.violations`. Violations are **reported, never repaired** —
   the generation is billed and the case's output stands. `policy: strict` marks the run
   `degraded` (`--strict-routing` exits 2), `warn` flags it. Downstream, `/eval-compare`
   pools such runs and footnotes them while `/eval-anova` skips them — see
   [eval-compare → Mixed providers and cost sources](eval-compare.md#mixed-providers-and-cost-sources)
   and [eval-anova → Rules at a glance](eval-anova.md#rules-at-a-glance).

The agent holds the **operator key** (`provider.key_exposed_to_agent: true`,
`key_scope: operator`; the startup line says so). `budget.run_usd` is enforced after the run.

### `key-guardrail` (opt-in)

Everything `audit` does, plus a **per-run key** provisioned through the management API
(`POST /api/v1/keys`) before any spend: `limit` = `budget.run_usd`, allow-list = the
pinned providers (or `guardrail.providers`). The key is read back and any mismatch — a different limit, no
echoed allow-list — revokes it and refuses to start, so a server that does not enforce
what was asked never runs the eval. The per-run key is what the agent, the backfill and
the key-usage reads use; judges keep the operator key. It is revoked
(`DELETE /api/v1/keys/{hash}`) at run end, after the backfill drain and the key-usage
settle, on every exit path (Ctrl-C included, with an `atexit` fallback should the
session never close); `provider/key.json` records hash and state (never the key), and
`python3 -m agent_eval.providers.openrouter.keys revoke <run_dir>` retries a failed
revoke (reported as a stderr `ERROR` and a `cost_warnings` entry). Server-side enforcement means a pin violation cannot happen (an unroutable request
404s) and a budget breach is a 402 (`budget.exceeded_reason: limit_usd`).

!!! warning "Verified per run, not established"
    The management API's guardrail field names have not been exercised against a live
    management key (spec 014 probe #26). They live in one function
    (`agent_eval.providers.openrouter.keys.key_request`) and the read-back fails closed; until the probe runs, treat `key-guardrail` as bounded exposure whose
    provider restriction is verified by that read-back at every start.

## Account-level settings

OpenRouter's privacy toggles — the paid-model-training opt-in, ZDR-only, ignored
providers — filter the endpoint set **server-side for the whole account**, every key
included. That is what made `deepseek/deepseek-v4.1-flash` unroutable one day: its only
endpoint required the paid-training opt-in, and every request answered 404 "No endpoints
found". The preflight's eligibility check (`GET /models/user`) catches it before spending
and names the setting; the harness never changes account settings.

## Quantization is pinned indirectly

There is no per-request quantization control. `quantizations: [fp8]` on a routing key
narrows the **pinned providers** to those serving the model at that quantization
(preflight excludes the others, the audit checks what was served); with no `order`/`only`
there is nothing to audit and the key warns.

## The routing lifecycle

1. **Preflight** (`preflight: strict | warn | off`): every routing key an agent role uses
   and every OpenRouter judge — slug exists; each pinned provider serves it, at a declared
   quantization, with tools and `tool_choice: auto` for agents; for pinned judges
   `structured_outputs` (the default verdict transport; a WARN when absent, the judge
   then forces a tool) and tool-forcing support (`tool_choice: function`, else
   `required`, else `auto` — warned about, not required: a WARN naming the rung the
   judge lands on, a failure only when no eligible endpoint supports any of them); degraded
   endpoints (`status < 0`) excluded, and a pinned set with no eligible endpoint left
   fails; `max_completion_tokens < 32000` warns; key valid; account eligible. A catalog fetch failure degrades to `warn`; key, slug and eligibility
   stay strict. `python3 -m agent_eval.providers.openrouter.preflight --config eval.yaml`
   runs it without a run.
2. **Snapshot**: the frozen catalog view the audit joins against, so a catalog change
   during the run cannot change a verdict.
3. **Run**: ids are sighted from Claude Code's own stream-json as it is read.
4. **Backfill**: `GET /generation` per id (first poll 5 s after sighting, then every 2 s,
   giving up at 60 s with a run-end retry); `python3 -m
   agent_eval.providers.openrouter.backfill <run_dir>` repeats it offline.
5. **Audit**: served providers against the pins, at every `run_result.json` write.

## Cost provenance and confidence

`cost_usd` is a **truth source or null**: the sum of priced generation rows when coverage
is at least 80 %, else the key-usage delta (`GET /key` before the run and after a ≥ 20 s
settle), else `null` (`--allow-estimate` writes the runner's estimate as
`runner:estimate` for offline replays). Claude Code's own number is kept as
`cost_usd_estimate` — it prices a non-Anthropic model at Anthropic rates, 2 to 60 times
too high. `cost_confidence` is `high` (coverage ≥ 95 % and the key delta within 5 % of
the ledger, agent plus hook spend), `medium` or `low`; a per-case file written inside the
`/generation` lag reads `unavailable` for a few seconds and converges at run end. The
fields are listed in [runs directory → cost provenance](../reference/runs-directory.md#cost-provenance).

## Budget scope

`execution.max_budget_usd` keeps its **per-invocation** meaning; the CLI receives
`× cli_budget_inflation` (default 50) because it enforces the cap on its inflated
estimate, and a cap of 0 omits the flag. `models.providers.openrouter.budget.run_usd` is
the **whole-run** pool: after the run at `audit` (`budget.exceeded: run`, `--strict-cost`),
server-side and in flight at `key-guardrail`. Judge spend is outside both — it lands in
`summary.yaml` (`judge_usage`, `total_cost_usd`), never in `run_result.cost_usd`.

## Secrets

`OPENROUTER_API_KEY` and `OPENROUTER_MANAGEMENT_KEY` are read from the harness process
only and may never be authored in a config or an `env:` surface. The inference key reaches
the agent as `ANTHROPIC_AUTH_TOKEN` through the overlay / the carrier env / the Secret —
never argv, the ledger, the snapshot, logs or `run_result.json` (`key_hash` is the only
trace); lifecycle hooks and the harbor child env are scrubbed of both variables, and the
management key reaches no subprocess. Because the agent under test can read its own
environment, `provider.key_exposed_to_agent` is `true` at both levels; `key-guardrail`
bounds what that exposure is worth. The local runner does not forward the Google
credential-location variables (`GOOGLE_APPLICATION_CREDENTIALS`, `CLOUDSDK_CONFIG`,
`CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE`) while a plan is active; it still shares the
operator's `HOME`, so application-default credentials on disk stay reachable — use the
podman or Kubernetes runner for isolation from them (podman skips the credentials mount,
the pod gets no credentials volume).

<a id="runners"></a>

## Backends

How the plan reaches the agent on each execution backend (the `--runner` / `--env`
choice — see [Execution backends](../concepts/backends.md)). The cross-provider version of
this list is the matrix in
[Model providers → Credentials by backend](../concepts/providers.md#credentials-by-backend).

- **Local claude-code**: the overlay beats `execution.env`, `runner.settings.env` and a
  user-level `settings.json` that forces Vertex; managed keys are stripped from the CLI's
  process env.
- **Harbor podman** (`--runner harbor --env podman`): value-free `--agent-env` carriers;
  while the plan is active the Anthropic credentials, the Vertex variables and the
  Bedrock switches (`CLAUDE_CODE_USE_BEDROCK`, `AWS_REGION`, `AWS_BEARER_TOKEN_BEDROCK`)
  are not forwarded and the GCP credentials file is not mounted — `AWS_ACCESS_KEY_ID` /
  `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` and the `OPENAI_*` variables still are
  (unset them on the host for isolation).
- **Harbor Kubernetes / OpenShift** (`--env kubernetes|openshift`): the credentials
  Secret named by `AGENT_EVAL_K8S_CREDENTIALS_SECRET` must hold `OPENROUTER_API_KEY`; the
  harness maps it to `ANTHROPIC_AUTH_TOKEN` via `secretKeyRef` and writes the plan's
  non-secret env into the pod spec (winning over `envFrom`, so a stale gateway URL in the
  Secret cannot re-route). The token is never a `--agent-env` carrier there (the K8s exec
  prefix would inline it into every command). At `key-guardrail` the harness creates the
  per-run Secret `agent-eval-<run_id>-openrouter` before `harbor run` and deletes it with
  the key — this needs `create`, `get` and `delete` on `secrets` in the namespace (a
  name clash is an error, never a replacement). The
  credentials Secret stays attached for whatever else it holds, but the provider key
  names are blanked in the pod (explicit empty `env[]` entries win over `envFrom`), so at
  `key-guardrail` the agent sees only the per-run key; the operator key stays visible
  under its configured name only when an in-container `openrouter:/` judge needs it. On
  the host, `audit` needs `OPENROUTER_API_KEY` exported (preflight, backfill and key-usage
  reads use the operator key); `key-guardrail` needs `OPENROUTER_MANAGEMENT_KEY` instead,
  plus `OPENROUTER_API_KEY` only for `openrouter:/` judges. A deployment whose Secret
  points Claude Code at an in-cluster Anthropic-compatible endpoint keeps working
  unchanged as long as no role is `openrouter:/`.
- **EvalHub**: the plan is built inside the job pod from the JobSpec model and the pod's
  own environment (`OPENROUTER_API_KEY`, plus `OPENROUTER_MANAGEMENT_KEY` at
  `key-guardrail`, injected by the cluster), the run is reconciled in the pod and the
  provenance (`cost_source`, `routing`, `provider`, `budget`) travels back with the job
  results into the client's `run_result.json`.

## Offline commands

```bash
python3 -m agent_eval.providers.openrouter.preflight --config eval.yaml   # checks without a run
python3 -m agent_eval.providers.openrouter.backfill <run_dir>             # re-query unpriced ids, re-reconcile
python3 -m agent_eval.providers.openrouter.keys revoke <run_dir>          # retry a failed per-run key revoke
```

## Limitations

- No in-flight harness-side gate at `audit`: the CLI's inflated cap is the only live cap;
  `run_usd` is checked after the fact. `key-guardrail` is the in-flight bound.
- Claude Code's own retries and OpenRouter's fallbacks are the only in-flight resilience;
  there is no cooldown or provider widening.
- A `models.hook` on a different provider kind is rejected: the hook subprocess inherits
  the agent's env and would 404 on an Anthropic slug.
- Synthetic dataset generation rejects `openrouter:/` models — see
  [Model providers → Limitations](../concepts/providers.md#limitations).
- `/eval-setup` does not recognise `OPENROUTER_API_KEY` (the run itself is unaffected) —
  see [Model providers → Limitations](../concepts/providers.md#limitations).
- `anthropic:/` on an agent role is normalised only while a plan is active — see
  [Model providers → Limitations](../concepts/providers.md#limitations).
- Kubernetes `key-guardrail`: the per-run Secret is deleted in the same `finally` as the
  key revoke, whether or not the revoke succeeds. It is left behind only when its own
  delete fails (RBAC, API outage — logged as `cleanup failed`) or when the harness
  process dies before it can clean up; remove it with `kubectl delete secret
  agent-eval-<run_id>-openrouter -n <ns>` (or `k8s_resources.cleanup(<ns>,
  name_prefix="agent-eval-")`). The offline `keys revoke` retries the key revoke only.
