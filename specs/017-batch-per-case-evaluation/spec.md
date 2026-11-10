# 017 — Per-case evaluation of batch-oriented skills

Status: proposed. Design only, no code. This spec settles the cost-measurement policy
that has to be decided before any batch-mode implementation work for #306.
Base: harness 1.63.1 (`8999ce6`). Related: #306 (this), #111 (closed: `workspace.files`
ignored in batch mode), #151 (cross-case reads), #154 (positional prefix grouping),
#292 (unify collect loops), spec 014 (cost provenance), spec 016 (`unmeasured`,
proposed).

All `file:line` references are against `8999ce6`. `S` is
`skills/eval-run/scripts/score.py`, `E` `skills/eval-run/scripts/execute.py`, `W`
`skills/eval-run/scripts/workspace.py`, `Co` `skills/eval-run/scripts/collect.py`, `C`
`agent_eval/config.py`, `CB` `agent_eval/judges/efficiency/cost_budget.py`.

## Problem

`execution.mode: batch` makes one agent invocation for every case. It was built for
skills whose entry point takes a collection (`/rfe.speedrun --input batch.yaml`). It
evaluates the run as a whole well enough. It does not evaluate individual cases on the
same basis as case mode:

| Concern | Case mode | Batch mode today |
| --- | --- | --- |
| Inputs | Each case gets its own workspace with `input.yaml`, `answers.yaml` and per-case `dataset.workspace.files` | One shared workspace. `workspace.files` is ignored with a warning (W:157-163, the outcome of #111). `batch.yaml` entries hold input *content* with no case identity (W:176-194). |
| Outputs | Collected from each case workspace | Mapped to cases by `{n}` position through `batch_pattern` (Co:606-643) or by prefix grouping (Co:646-664). Extra prefixes are dropped or misassigned (#154). |
| Traces | Per-case `stdout.log` / `events.json` | One root stream. Subagent transcripts are routed to cases by a `<prefix><n>` marker (Co:262-283, Co:424-489). Coordinator turns and unmarked transcripts stay on the shared trace, which judges fall back to (S:528-535). |
| Per-case hooks | `before_each` / `after_each` run | Ignored with a warning (C:2983-2999) |
| Cost, tokens, turns, duration | Measured per session, written to `run_result.json` `per_case` | One invocation-level figure (E:2284-2307). There is no `per_case` block. |
| Isolation | Separate workspace per case (sibling reads still possible, #151) | None. Every case sees every other case's inputs and outputs. |

### The batch-cost fallback

`load_case_record` reads execution metrics with a run-level fallback (S:500-522):

```python
per_case = meta.get("per_case", {}).get(case_id, {})
record["cost_usd"] = per_case.get("cost_usd", meta.get("cost_usd"))
```

A batch `run_result.json` has no `per_case` block, so **every case record receives the
whole-batch `cost_usd`**. The same applies to `cost_source`, `token_usage`,
`duration_s`, `num_turns` and `exit_code`. This is substitution, not allocation: the
judge is handed a number for a different unit of work, with nothing marking it as such.

The `cost_budget` builtin (CB:21-32) then compares the batch total against a per-case
cap. Take a 10-case batch that costs $60 and is judged at Epic Creator's $40
per-strategy cap. Every case fails, even if no single case cost more than $6. With a
$100 cap, a run where one case consumed $55 passes every case. Neither outcome measures
what the judge description says it measures.

The fallback is correct in exactly one situation: a batch with one case. There the
invocation *is* the case, and the run-level figure is a measurement.

### Unavailable cost shrinks the gate instead of failing it

`cost_budget` abstains when `cost_usd` is absent or `cost_source` is `unavailable`
(CB:24-26). It returns `None` with no error. An abstaining case is added to neither
`values` nor `errored_cases` (S:3859-3865):

- **Every case abstains:** `pass_rate` is `None`, and `min_pass_rate` reports an `n/a`
  regression (S:5743-5749). The gate fails loudly, which is correct.
- **Some cases abstain:** `pass_rate` is computed over the cases that remain, and
  `max_error_rate` does not count the abstentions either. With Epic Creator's
  `budget_check: {min_pass_rate: 1.0}`, a run where 9 of 10 cases have no known cost
  passes on the 1 measured case.

A design that makes per-case cost "unavailable" must therefore produce the all-or-none
outcome. Otherwise "clearly reports when they are unavailable" turns into a gate that
silently gets smaller. This partial-abstention gap already exists in case mode (an
unpriced case under an OpenRouter plan) and is not specific to batch mode. See Open
Question 3.

### Why this matters now

Epic Creator (`fullsend-ai/epic-creator`, `eval/epic-decompose.yaml` at `f46e923`)
evaluates a collection-oriented skill. Its config comment says it chose
`mode: case` because batch mode "delivers cases via batch.yaml (which this skill does
not read) and maps outputs to cases by {n}-index prefixes (artifacts here are keyed by
strat_id)". It depends on three per-case properties that batch mode cannot provide:

- Per-case companion inputs: `artifacts/strat-tasks/RHAISTRAT-<n>.md`, plus a
  `.context/` tree that **differs by tier**. Synthetic cases ship a 13-name
  `rhai-components.txt` and `rhoai-3.0` architecture stubs. Real cases ship the 93-name
  production list and `rhoai-3.6-ea.1` docs.
- A `before_each` hook (`stage-case.sh`) that replaces symlinks with real copies.
- `budget_check` (`builtin: cost_budget`, `max_cost_usd: 40`) with
  `thresholds.budget_check.min_pass_rate: 1.0`, documented as a per-strategy runaway
  guard that gates the reward.

## The two models

### A. Shared invocation (today's `mode: batch`)

One agent session processes all cases. A coordinator (the root conversation) plans,
schedules subagent waves, allocates IDs, deduplicates and summarizes on behalf of every
case.

**Per-case cost (issue open question 1).** It cannot be measured on the same basis as
case mode:

- Claude Code reports one cumulative `total_cost_usd` per invocation
  (`agent_eval/agent/stream_capture.py:124`, `agent_eval/agent/claude_code.py:620`).
  Subagent transcripts carry per-message token usage but no billed USD, so a per-case
  USD figure from them would be a token-priced *estimate*.
- Under an OpenRouter plan the ledger prices each generation (spec 014). The generation
  ids in each case's routed subagent transcripts could be summed into a *measured*
  per-case subagent spend.
- The coordinator's turns serve every case at once. Any per-case figure that includes
  them depends on an allocation policy (equal split, token share, wall time), and #306
  rules that out. Any figure that excludes them is partial. It is not comparable to case
  mode's `cost_usd`, which includes the coordinator of a one-case session.

At best, model A can produce a coverage-labelled *case-attributed* spend plus an
unattributed remainder. That figure must never be written to `cost_usd` or judged
against a cap that was calibrated on case mode.

**Per-case inputs.** A shared root can hold only one version of each path. Epic
Creator's `strat-tasks` files are disjoint by name, but `.context/rhai-components.txt`
and the architecture release directories conflict between tiers. Merging with
fail-on-conflict would reject the dataset. Namespacing each case under its own
directory only works if the skill reads a per-entry path, which changes the skill under
test. #111's conclusion still holds.

**Identity, isolation and hooks.** `batch.yaml` carries no `case_id`, so mapping
outputs and traces back to cases relies on order and prefixes (#154). Per-case hooks
have no per-case point at which to run. Isolation is impossible by construction.

**What only A measures.** Cross-case behavior: shared ID allocation (the #154 failure
was exactly this), deduplication, wave scheduling, and the amortized coordinator cost of
running N cases together. These are properties of the *run*, and A measures them at the
run level.

### B. Independently metered sessions

One agent session per case, each in its own workspace with its own inputs, run
concurrently.

**How it differs from case mode with `parallelism` (issue open question 2).** It does
not differ in mechanism. It *is* case mode. Per-case `workspace.files`,
`before_each`/`after_each`, per-case `stdout.log`/`events.json`, `cost_usd` and
`cost_source` on the case-mode basis, and directory-based identity all come for free.

The only real difference is the *interface* the skill is invoked through. A skill whose
entry point is `--input batch.yaml` cannot run in case mode without the author rewriting
`execution.arguments` around a per-case field (Epic Creator switched to
`"{strat_id} --headless"`). That works only when the skill also accepts single-item
arguments.

Harbor already does B for batch configs. `agent_eval/harbor/tasks.py:549-555` writes a
one-entry `batch.yaml` into each per-case task, so a `mode: batch` `eval.yaml` runs as
independently metered sessions under Harbor and as one shared invocation locally. The
same config therefore measures different things depending on the execution path, which
contradicts the "same `eval.yaml` across local, Harbor and EvalHub" principle.

**What B gives up.** Anything the skill does *across* cases. Each session is a batch of
one, so cross-case ID allocation, deduplication and amortization are not exercised. If
that behavior is the thing under evaluation, B does not evaluate it.

### Comparison

| | A. Shared invocation | B. Independent sessions |
| --- | --- | --- |
| Per-case inputs (`workspace.files`) | No (one shared root) | Yes |
| Per-case hooks | No | Yes |
| Output → case identity | Positional / prefix (#154) | Workspace directory |
| Per-case traces | Partial: routed subagents only, coordinator shared | Yes |
| Per-case cost, case-mode basis | **No**. Partial at best, never including the coordinator | **Yes** |
| Existing per-case cost gate | Cannot be evaluated honestly | Works unchanged |
| Isolation between cases | None | Per-case workspace (#151 still applies) |
| Cross-case skill behavior | Exercised | Not exercised |
| Run-level cost of the whole batch | Measured | Sum of cases (no amortization) |
| Matches Harbor's execution of the same config | No | Yes |

## Decisions

### D1. Per-case evaluation of a batch-interface skill uses model B, as a case-mode option

Add an opt-in case-mode flag. Working name: `execution.batch_input: true`; the final
name is Open Question 1. With the flag set, the harness writes a one-entry `batch.yaml`
into each case workspace. The entry is the case's input, or that case's own entries when
the input is a list, the same flattening W:186-193 uses. Everything else is ordinary
case mode:

```yaml
execution:
  mode: case
  batch_input: true          # write a one-entry batch.yaml into each case workspace
  skill: rfe.speedrun
  arguments: "--input batch.yaml --headless"
  parallelism: 4
```

- This is not a third `execution.mode`. Invocation count is still "one per case", which
  is what `mode` already expresses.
- Local runs then match `harbor/tasks.py:549-555` for the same intent. The flag should
  set Harbor's behavior explicitly rather than Harbor inferring it from `mode: batch`.
- Every per-case property in #306's desired outcome comes from case mode: inputs,
  identity, traces and measured cost. The `budget_check` gate keeps its meaning with no
  allocation.
- It is opt-in, so existing case-mode and batch-mode configs are unchanged.

### D2. Shared-invocation batch never substitutes the batch figure for a per-case one

When `run_result.json` has `execution_mode: batch` and the run had more than one case,
`load_case_record` stops falling back to run-level metrics:

- `cost_usd: null`, `cost_source: "unavailable"`, and a new
  `cost_unavailable_reason: "shared-invocation"` so the report can say *why*.
  `cost_budget` already abstains on `unavailable` (CB:24-26), so no judge change is
  needed.
- `token_usage`, `num_turns` and `duration_s` become `null` per case for the same
  reason. Each is a whole-batch figure under a per-case name.
- `exit_code` keeps the fallback. A failed invocation genuinely applies to every case
  it covered.
- The run-level figures are exposed on every case record under explicit names:
  `batch_cost_usd`, `batch_cost_source`, `batch_token_usage`, `batch_duration_s`,
  `batch_num_turns` and `batch_size` (case count). A judge that really wants to cap the
  whole batch can read those, with a cap sized for a batch.
- **One-case batch:** the run-level figures are the case's measurement, so the per-case
  fields keep them. They also appear under the `batch_*` names.
- `report.html` renders per-case cost as "unavailable (shared batch invocation)" and
  shows the batch total at run level.

This produces the all-or-none outcome required above. In a multi-case shared batch,
every case abstains, `pass_rate` is `None`, and a `min_pass_rate` threshold on a cost
judge reports `n/a` (S:5743-5749). The run does not pass on a cost it never measured,
and it does not fail every case on a total it was never meant to compare.

**This changes existing batch-mode behavior, and that is intended.** #306 allows changes
that are made explicitly. Batch evals with cost or token judges stop judging the batch
total against a per-case cap. Their thresholds report `n/a` until the author either
moves to D1 or rewrites the judge against `batch_cost_usd`. The change ships with a
CHANGELOG entry and the config-load warning in D3. The key is `execution_mode`, not
"`per_case` is absent", so older or non-batch `run_result.json` files without a
`per_case` block keep their current reading.

### D3. Warn at config load when a cost judge cannot be evaluated

When `execution.mode: batch` is set and a `builtin: cost_budget` judge is configured,
`EvalConfig` warns: per-case cost is unavailable in a shared batch invocation, the judge
will abstain on every case of a multi-case run, any threshold on it will report `n/a`,
and `mode: case` with `batch_input: true` measures per-case cost. This follows the
warning pattern of C:2983-2999. Custom `check:` judges that read `cost_usd` cannot be
detected statically. They get `null` and the `cost_unavailable_reason` field, and D2's
report text covers them.

### D4. Case-attributed spend is deferred and never becomes `cost_usd`

Under an OpenRouter plan, model A could report per case
`attributed_cost_usd` + `attributed_coverage` (from routed subagent generation ids) plus
a run-level `unattributed_cost_usd` for coordinator and unrouted turns. It is useful for
diagnosis ("which strategy blew the budget"). It is not a case-mode-comparable cost, so
it would be written only under those names and never fed to `cost_usd`, `cost_budget`
or `run_metrics`. It is not part of the first deliverable. It depends on the subagent
routing that #154 and #292 are reworking.

### D5. Per-case inputs and identity mapping in shared invocation stay out of scope

This spec does not reopen #111. A shared root cannot hold conflicting per-case inputs,
and D1 is the supported path for cases that need them. Identity-based output mapping in
shared batches (for example, a `batch_pattern` keyed by an input field rather than
`{n}`) belongs with #154 and the single collection routine in #292. It should be
designed there rather than duplicated here.

## Behavior preservation

| Configuration | After this spec |
| --- | --- |
| `mode: case` (no `batch_input`) | Unchanged |
| `mode: case`, `batch_input: true` | New. Ordinary case mode plus a one-entry `batch.yaml` per case |
| `mode: batch`, one case | Unchanged per-case values, plus the `batch_*` fields |
| `mode: batch`, many cases, no cost/token/duration judges | Unchanged scores. The report shows per-case cost as unavailable and the batch total at run level |
| `mode: batch`, many cases, cost judge | **Changed (explicit):** the judge abstains on every case and thresholds on it report `n/a`, instead of judging the batch total per case |
| Harbor, `mode: batch` config | Unchanged (already one task per case). Harbor additionally honours `batch_input` under `mode: case` |
| `run_result.json` without `execution_mode: batch` | Unchanged fallback |

## Acceptance criteria (for the implementing change)

1. `mode: case` + `batch_input: true`: each case workspace contains a `batch.yaml` with
   exactly that case's entries. Per-case `workspace.files` and `before_each` run as in
   case mode. `run_result.json` has a `per_case` entry with that case's own `cost_usd`.
2. The same config generates Harbor task packages whose `batch.yaml` is byte-identical
   to the local per-case one.
3. A multi-case batch `run_result.json` with `cost_usd: 60.0`: every case record has
   `cost_usd: null`, `cost_source: "unavailable"`, `cost_unavailable_reason:
   "shared-invocation"` and `batch_cost_usd: 60.0`. `cost_budget` abstains on every
   case. A `min_pass_rate` threshold on it yields an `n/a` regression, never a pass.
4. A one-case batch `run_result.json` with `cost_usd: 6.0`: the case record has
   `cost_usd: 6.0`, and `cost_budget` judges it.
5. A `run_result.json` with no `execution_mode` and no `per_case` keeps today's
   fallback.
6. `mode: batch` + `builtin: cost_budget` emits the D3 warning at config load.
   `mode: case` does not.

## Open questions for maintainers

1. **Flag name and placement.** `execution.batch_input: true`, or a value on
   `dataset.workspace` (for example `batch_file: batch.yaml`) that also lets the author
   choose the file name the skill expects?
2. **Scope of D2.** Null only the cost fields and leave tokens, turns and duration on the
   fallback for compatibility, or null all four as proposed? Tokens and duration have the
   same substitution problem, but fewer judges gate on them.
3. **Partial abstention in case mode.** The "some cases abstain, the gate passes on the
   rest" gap (S:3859-3865) exists today in case mode, for example with one unpriced case
   under an OpenRouter plan. D1 inherits it. It looks like spec 016's `unmeasured`
   field, but 016 charges an unscoped entry to every judge, and an unknown cost should
   charge only cost judges. Should 016 gain a judge-scoped `cost_unavailable` entry, or
   should abstentions get their own per-judge counter that `max_error_rate` reads?
4. **Is a shared batch's run-level cost worth a builtin?** For example `cost_budget`
   with `scope: batch` reading `batch_cost_usd`. Or should authors write a `check:`
   judge when they need it?

## Answers to #306's open questions

- **Can a shared invocation provide per-case costs, including coordinator work, without
  an allocation policy?** No. The coordinator's turns serve all cases, and the only
  per-case figures available without allocation exclude it (D4). They are not on case
  mode's basis.
- **Could a batch of independently metered sessions satisfy the use case? How does it
  differ from case mode with `parallelism`?** Yes, and it is case mode. The gap is the
  invocation interface, which D1 closes with a one-entry `batch.yaml` per case, the same
  way Harbor already runs batch configs. What it gives up is cross-case skill behavior,
  which only a shared invocation exercises and only at run level.
- **What isolation and identity-based mapping are needed?** For per-case evaluation:
  the per-case workspace and directory identity of case mode, with #151 as the
  remaining isolation gap. For shared invocation: identity-based mapping is #154/#292's
  work (D5). #111's decision stands, because a shared root cannot carry conflicting
  per-case inputs.
