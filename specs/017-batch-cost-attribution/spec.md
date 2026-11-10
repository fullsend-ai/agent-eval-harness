# 017 — Distinguish batch costs from per-case costs

Status: proposed. Design only, no code. This spec settles the cost policy for
`execution.mode: batch` that #306 asks for before any implementation work.
Base: harness 1.63.1 (`8999ce6`). Related: #306 (this), #154 (positional prefix
grouping), #292 (unify collect loops), spec 014 (cost provenance), spec 016
(`unmeasured`, proposed).

All `file:line` references are against `8999ce6`. `S` is
`skills/eval-run/scripts/score.py`, `E` `skills/eval-run/scripts/execute.py`, `C`
`agent_eval/config.py`, `CB` `agent_eval/judges/efficiency/cost_budget.py`.

## Scope

In scope, per #306:

- Keep the invocation total available as a batch-wide cost.
- Mark per-case cost unavailable unless it can be measured.
- Never silently treat an unavailable required cost as a pass.
- A one-case batch keeps its invocation cost as the case cost.

Out of scope: multi-strategy input and output mapping for collection-oriented skills
such as Epic Creator (per-case `workspace.files`, hooks, identity-based output mapping).
That is separate work, tracked in #154 and #292. This spec does not add a new execution
mode or option.

## Problem

`execution.mode: batch` makes one agent invocation for every case, and the invocation
reports one cost (E:2284-2307). A batch `run_result.json` has no `per_case` block.
`load_case_record` reads execution metrics with a run-level fallback (S:500-522):

```python
per_case = meta.get("per_case", {}).get(case_id, {})
record["cost_usd"] = per_case.get("cost_usd", meta.get("cost_usd"))
```

**Every case record therefore receives the whole-batch `cost_usd`** (and `cost_source`).
This is substitution, not allocation: the judge is handed a number for a different unit
of work, with nothing marking it as such.

The `cost_budget` builtin (CB:21-32) then compares the batch total against a per-case
cap. Take a 10-case batch that costs $60, judged at a $40 per-case cap. Every case
fails, even if no single case cost more than $6. With a $100 cap, a run where one case
consumed $55 passes every case. Neither outcome measures what the judge description says
it measures.

The fallback is correct in exactly one situation: a batch with one case. There the
invocation *is* the case, and the run-level figure is a measurement.

### Per-case cost cannot be measured in a shared invocation

- Claude Code reports one cumulative `total_cost_usd` per invocation
  (`agent_eval/agent/stream_capture.py:124`, `agent_eval/agent/claude_code.py:620`).
  Subagent transcripts carry per-message token usage but no billed USD, so a per-case
  figure from them would be a token-priced estimate.
- The coordinator's turns serve every case at once. Any per-case figure that includes
  them depends on an allocation policy (equal split, token share, wall time), which #306
  rules out. Any figure that excludes them is partial and not comparable to case mode's
  `cost_usd`.

So for a multi-case shared batch, per-case cost is unavailable. A measured per-case cost
exists only where each case is its own invocation, which is case mode.

### Unavailable cost shrinks the gate instead of failing it

`cost_budget` abstains when `cost_usd` is absent or `cost_source` is `unavailable`
(CB:24-26). It returns `None` with no error. An abstaining case is added to neither
`values` nor `errored_cases` (S:3859-3865):

- **Every case abstains:** `pass_rate` is `None`, and `min_pass_rate` reports an `n/a`
  regression (S:5743-5749). The gate fails loudly, which is correct.
- **Some cases abstain:** `pass_rate` is computed over the cases that remain, so a
  `min_pass_rate` gate passes on the measured cases only.
- **`max_error_rate` is the only threshold:** abstentions are not errors, so the gate
  passes even when every case abstains.

A design that marks per-case cost unavailable must therefore produce the all-or-none
outcome and make the `max_error_rate`-only configuration visible. The partial-abstention
gap already exists in case mode (for example, an unpriced case under an OpenRouter plan)
and is not specific to batch mode. See Open Question 2.

## Decisions

### D1. A multi-case shared batch never substitutes the batch figure for a per-case one

When `run_result.json` has `execution_mode: batch` and the run had more than one case,
`load_case_record` stops falling back to run-level cost:

- `cost_usd: null`, `cost_source: "unavailable"`, and a new
  `cost_unavailable_reason: "shared-invocation"` so the report can say *why*.
  `cost_budget` already abstains on `unavailable` (CB:24-26), so no judge change is
  needed.
- The run-level cost is exposed on every case record under explicit names:
  `batch_cost_usd`, `batch_cost_source` and `batch_size` (case count). A judge that
  really wants to cap the whole batch can read those, with a cap sized for a batch.
- **One-case batch:** the run-level cost is the case's measurement, so the per-case
  fields keep it. It also appears under the `batch_*` names.
- `report.html` renders per-case cost as "unavailable (shared batch invocation)" and
  shows the batch total at run level.
- The key is `execution_mode`, not "`per_case` is absent", so older or non-batch
  `run_result.json` files without a `per_case` block keep their current reading.
- Only cost changes. Tokens, turns, duration and `exit_code` keep today's fallback. See
  Open Question 1.

This produces the all-or-none outcome required above. In a multi-case shared batch,
every case abstains, `pass_rate` is `None`, and a `min_pass_rate` threshold on a cost
judge reports `n/a` (S:5743-5749). The run does not pass on a cost it never measured,
and it does not fail every case on a total it was never meant to compare.

**This changes existing batch-mode behavior, and that is intended.** #306 allows changes
that are made explicitly. Batch evals with a cost judge stop judging the batch total
against a per-case cap. Their score thresholds report `n/a` until the author rewrites the
judge against `batch_cost_usd`, or moves the eval to `mode: case`, which measures cost
per case. The change ships with a CHANGELOG entry and the config-load warning in D2.

### D2. Warn at config load when a cost judge cannot be evaluated

When `execution.mode: batch` is set and a `builtin: cost_budget` judge is configured,
`EvalConfig` warns: per-case cost is unavailable in a shared batch invocation, the judge
will abstain on every case of a multi-case run, and a score threshold on it
(`min_pass_rate`) will report `n/a`. `max_error_rate` is not a score threshold:
abstentions are not errors, so a config whose only threshold on the cost judge is
`max_error_rate` still passes when every case abstains, and the warning is the only
signal in that case. This follows the warning pattern of C:2983-2999. Custom `check:`
judges that read `cost_usd` cannot be detected statically. They get `null` and the
`cost_unavailable_reason` field, and D1's report text covers them.

## Behavior preservation

| Configuration | After this spec |
| --- | --- |
| `mode: case` | Unchanged |
| `mode: batch`, one case | Unchanged per-case values, plus the `batch_*` fields |
| `mode: batch`, many cases, no cost judge | Unchanged scores. The report shows per-case cost as unavailable and the batch total at run level |
| `mode: batch`, many cases, cost judge | **Changed (explicit):** the judge abstains on every case and score thresholds on it (`min_pass_rate`) report `n/a`, instead of judging the batch total per case. `max_error_rate` alone still passes, since abstentions are not errors |
| `run_result.json` without `execution_mode: batch` | Unchanged fallback |

## Acceptance criteria (for the implementing change)

1. A multi-case batch `run_result.json` with `cost_usd: 60.0`: every case record has
   `cost_usd: null`, `cost_source: "unavailable"`, `cost_unavailable_reason:
   "shared-invocation"` and `batch_cost_usd: 60.0`. `cost_budget` abstains on every
   case. A `min_pass_rate` threshold on it yields an `n/a` regression, never a pass. A
   config whose only threshold on it is `max_error_rate` still passes, because
   abstentions are not errors.
2. A one-case batch `run_result.json` with `cost_usd: 6.0`: the case record has
   `cost_usd: 6.0`, and `cost_budget` judges it.
3. A `run_result.json` with no `execution_mode` and no `per_case` keeps today's
   fallback.
4. `mode: batch` + `builtin: cost_budget` emits the D2 warning at config load.
   `mode: case` does not.

## Open questions for maintainers

1. **Scope of D1.** Tokens, turns and duration have the same substitution problem as
   cost. D1 leaves them on the fallback because #306 is about cost and fewer judges gate
   on them. Should they be nulled and exposed under `batch_*` names as well?
2. **Partial abstention.** The "some cases abstain, the gate passes on the rest" gap
   (S:3859-3865) exists today in case mode. It looks like spec 016's `unmeasured` field,
   but 016 charges an unscoped entry to every judge, and an unknown cost should charge
   only cost judges. Should 016 gain a judge-scoped `cost_unavailable` entry, or should
   abstentions get their own per-judge counter that `max_error_rate` reads?
3. **Is a shared batch's run-level cost worth a builtin?** For example `cost_budget`
   with `scope: batch` reading `batch_cost_usd`. Or should authors write a `check:`
   judge when they need it?
