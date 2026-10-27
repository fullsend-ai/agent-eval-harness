# 016 — One field for "this case produced no measurement"

Status: proposed. No code in this PR — it is the schema decision that #264 and #276
both need and that neither should make alone.
Base: harness 1.59.0 (`ac46692`). Related: #259, #264 (open), #260 (merged), #276 (open),
#277 (the tracking issue).

All `file:line` references are against `ac46692` unless stated otherwise.

## Problem

The harness has no way to say *this case is not a measurement of the agent*. Every
layer that discovers it invents its own spelling, and each one is a separate thing for
judges, thresholds, reports and reward composition to know about.

| Spelling | Where | Added by | Says |
| --- | --- | --- | --- |
| `incomplete_cases` | `summary.yaml`, per judge | #260, #276 | evidence was withheld from the judge |
| `hooks` + `hook_failures` | `run_result.json`, per case | #264 | an `after_each` hook crashed or timed out |
| `output/infrastructure-failure.json` | an eval's own output tree | fullsend-ai/agents#1622 | the capture hook could not run |

The third is the telling one. It is not harness code at all: an eval wrote a marker
file, its runner script counted it, and four judges checked for it before scoring. Six
review rounds each found one more exit path where the marker was not written. None of
that logic was specific to the eval — it was the harness's own knowledge, written down
by hand because the harness does not write it down.

And the count is still rising. #277 lists two more sites that lose a case silently
(`collect.py:201`'s `rglob`, which skips an unlistable directory without raising) and
two execution paths that drop the counters entirely (`harbor/run.py::build_summary` and
`evalhub/runner.py::_metrics_to_summary` rebuild the aggregate as `{mean, pass_rate}`,
so the same `eval.yaml` fails locally and passes in a container).

### Why it keeps happening

Two different questions are being answered by one set of fields:

- **Did the agent do well?** — a measurement. Low score, failed gate, wrong answer.
- **Did we obtain a measurement at all?** — a precondition. Setup hook crashed, artifact
  unreadable, judge API 500, container OOM.

The harness records the *phase* in which something went wrong, in a field shaped for
that phase. It has no field for the second question, so every consumer re-derives it,
differently, from whatever phase-specific fields it happens to know about.

### Where it does damage, not just mess

`compose_reward` (`agent_eval/harbor/reward.py:362`). A case where every scoring judge
errored composes to **reward 0.0**. The comment there reasons carefully about why `0.0`
and not `1.0`, and is right on its own terms — but the option it does not have is *drop
the sample*. For GRPO a `0.0` teaches the policy that what it did was bad, when the
truth is that the harness broke. That is poisoned training data, not a reporting wart.

The same gap in the other direction: #276's case is an agent making its own artifact
unreadable. If that merely dropped the sample, suppressing evidence would be a reliable
way to escape a bad reward. So the two causes genuinely need opposite treatment, and
nothing in the current data model can tell them apart.

## Proposal

One case-level list. Presence is the signal; cause is detail inside it.

Entries are written by the stage that discovers them, into that stage's own
artifact, and read as the union of the two:

| Phase | Artifact | Written by |
| --- | --- | --- |
| `provision`, `before_each`, `agent`, `after_each`, `collect` | `run_result.json`, under `per_case.<id>` | `execute.py` / `collect.py` |
| `score` | `summary.yaml`, under `per_case.<case>` | `score.py` |

Splitting them is forced, not chosen: `score.py` never writes `run_result.json`,
and the execution stages are finished before scoring starts. Consumers read both
and concatenate; `unmeasured_cases` counts a case once however many entries it has.

```json
// run_result.json per_case.<id> — and the same shape in summary.yaml per_case
"unmeasured": [
  {"phase": "after_each", "kind": "hook_timeout",  "attribution": "harness",
   "detail": "after_each[1]: Save the PR diff", "log": "hooks/after_each.c0.1.log"},
  {"phase": "score",      "kind": "evidence_lost", "attribution": "agent",
   "detail": "content not staged: out/bad.txt"}
]
```

Absent or empty ⇒ the case is a valid measurement. A list, not a scalar, because more
than one thing can go wrong in a case and #276 already had to record two at once
(a hardlinked file *and* unreadable content).

| Field | Meaning |
| --- | --- |
| `phase` | `provision` \| `before_each` \| `agent` \| `after_each` \| `collect` \| `score` |
| `kind` | stable slug for the cause; the set grows, consumers must tolerate unknown values |
| `attribution` | `harness` \| `agent` — decides reward treatment, see below |

**Mixed attribution resolves to `agent`.** A case can carry entries of both
kinds — the example above does. The rule is order-independent and deliberately
asymmetric: if ANY entry is attributed to the agent, the case scores `0.0`
rather than being dropped. Dropping is the outcome that removes a case from
training, so it is the one an agent could profit from; a harness fault that
happens to coincide with the agent suppressing evidence must not launder the
suppression into a dropped sample. The spec's own example therefore scores
`0.0`, not dropped.
| `detail` | one line for a human |
| `log` | optional pointer, relative to the run directory |

### Case-level, not judge-level

`incomplete_cases` is per judge today, and that is already strained: in #276, when the
case record could not be built, the fix had to stamp **every** judge, because the
condition is not a property of any judge — it is a property of the case. At case level
the stamping loop disappears and per-judge counts derive by counting.

### Consumers

| Consumer | Today | With `unmeasured` |
| --- | --- | --- |
| judges | `outputs["hook_failures"]`, a marker file, `staging_skips` | `outputs["unmeasured"]` |
| thresholds | the `incomplete_staging` special case in `detect_regressions` | one rule: any unmeasured case blocks score thresholds and baselines |
| `summary.yaml` | `incomplete_cases` stamped by each layer | `unmeasured_cases`, derived by counting |
| reward | errored ⇒ `0.0` | `attribution: harness` ⇒ drop the sample; `agent` ⇒ `0.0` |
| Harbor / EvalHub | counters dropped | one field to carry |

The threshold rule **replaces** `incomplete_staging` rather than sitting beside it.
`max_error_rate` stays exempt, for the reason #260 gave: that gate is the author pricing
how much of the dataset may go unscored, and auto-failing it would make a tolerant error
budget impossible.

### What stays

`hooks` (#264) stays exactly as it is. It is per-hook diagnostic detail — exit code,
timeout, duration, log path — and is useful whatever sits above it. `errored_cases`
stays too: "the judge itself failed" is a different fact from "the case was never
measurable", and conflating them is how we got here.

What goes is the *derived verdict* each layer publishes next to its detail:
`hook_failures` (never shipped — #264 drops it on this spec's account) and
`incomplete_cases` (shipped in 1.58; needs a deprecation window).

## Migration

1. Write `unmeasured` from the staging and record-build paths, alongside
   `incomplete_cases`. Derive `incomplete_cases` from it so the existing threshold rule
   and docs keep working unchanged.
2. Add the hook contributor (#264, merged) and the `collect.py` contributor (#277).
   Add the **judge-error** contributor at the same time: a judge that fails
   terminally after its retries emits a `phase: score` entry. Without it the
   example this spec opens with — every scoring judge errored, reward `0.0` —
   stays unfixed, because the case's `unmeasured` list would be empty and the
   spec defines that as a valid measurement. `errored_cases` still records
   "the judge itself failed"; the two are not redundant, because a judge can
   error on one case out of ten and the run is still a measurement of the
   agent on the other nine. The `unmeasured` entry is for the case, the
   `errored_cases` count is for the judge.
3. Switch `detect_regressions` to read `unmeasured`; keep `incomplete_staging` as the
   regression metric name so CI output does not churn.
4. Carry the field in `harbor/run.py::build_summary` and
   `evalhub/runner.py::_metrics_to_summary`, so the gate stops being local-only.
5. Teach `compose_reward` to drop a `harness`-attributed sample. `compose_reward`
   currently returns a `float` that is written to the Harbor reward file and
   formatted `:.4f`, and `skills/eval-anova/scripts/analyze.py` calls
   `float(composite)` on it — so `None` is not available as a sentinel. Proposal:
   keep the return type a float and carry the disposition beside it, in the
   metrics dict `compose_reward` already returns as its second element
   (`{"reward_disposition": "scored" | "dropped"}`), with the float left at `0.0`
   when dropped. Reports keep showing the case; the training consumer filters on
   the disposition. The spec's job here is to name the contract — which writer
   emits it, and which consumer is required to honour it — not to assume a
   `None` nobody can accept.
6. Deprecate `incomplete_cases` one minor release later.

Steps 1–3 are behaviour-preserving. Step 4 makes containerised runs *start* failing
where they silently passed, which is the point, and wants a release note. Step 5 changes
RL training data and should land on its own.

## Open questions

1. **The name.** `unmeasured` is the author's pick. `void`, `invalid`, `unjudgeable`
   were considered; `infra_failures` was rejected because the agent-suppressed-evidence
   case is not infrastructure failing, and the unifying property is that the measurement
   is void rather than whose fault it was. Weakly held — this is the part most worth
   arguing about, and it is cheap to change before anything writes it.
2. **Is `attribution` binary?** A judge-provider 500 is `harness`; an agent hiding an
   artifact is `agent`. A hook that fails *because* the agent left the repo dirty is
   neither cleanly. Proposal: keep it binary, let `kind` carry the nuance, and make
   the rules about who may set what:

   - **`harness` is the narrow case, not the default.** It may be set only by harness
     code that has positively identified an infrastructure fault — a provider error, a
     container failure, the harness's own crash. Anything else, including a missing or
     unrecognised value, resolves to `agent`.
   - **Attribution is never read out of workspace content.** Only harness code sets it;
     no file an agent can write contributes to it.

   This inverts the draft's original "default `harness`". Defaulting to the
   sample-dropping side meant every ambiguous or agent-influenced failure landed in the
   reward-escaping direction, which is the thing the Problem section says must not be
   possible. The cost of the safer default is that a genuine harness fault we failed to
   classify scores `0.0` instead of being dropped — unfair to one sample, where the other
   way round is exploitable.
3. **Does a `kind` need to be registered?** Argues for a module-level tuple and a test
   that every writer uses a known one; argues against for the same reason
   `_CASE_OUTPUT_SKIP_REASONS` is a prefix match — new causes appear faster than the
   enumeration is updated. Proposal: no registry, consumers tolerate unknown `kind`.
4. **Should an unmeasured case still be reported in `num_cases`?** It executed. It just
   did not produce a measurement. Proposal: yes, with `unmeasured_cases` beside it,
   because the denominator shrinking silently is the whole bug.

## Why now

Not speculation about future contributors: there are four concrete ones already
(`hooks`, staging/record, `collect.py`, judge errors) and two consumers that already get
it wrong (reward composition, the containerised summary builders). Generalising after
the third instance is the right moment — the first would have been guessing, the fifth
is a deprecation.
