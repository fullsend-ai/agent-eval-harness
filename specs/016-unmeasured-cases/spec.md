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
| `provision`, `before_each`, `agent`, `after_each`, `collect` | `run_result.json`, under `per_case.<id>.unmeasured` | `execute.py` / `collect.py` |
| `score` | `summary.yaml`, under a **top-level** `unmeasured: {case_id: [...]}` | `score.py` |

Splitting the WRITERS is forced, not chosen: `score.py` never writes
`run_result.json`, and the execution stages are finished before scoring starts.
Splitting the READERS is not. `score.py` already reads `run_result.json` and
produces `summary.yaml`, so it folds the execution-phase entries in as it goes and
`summary.yaml` is the single canonical list. Downstream consumers — report,
thresholds, reward, eval-anova — read one place; `unmeasured_cases` counts a case
once however many entries it has.

**`summary.yaml` is canonical for things that read a summary — the Harbor
verifier is not one.** It composes the reward **in-container**, where no
`summary.yaml` exists: `harbor/reward.py::_score_case_counted` keeps only
`result["per_case"][case]` from `score_cases` and discards the rest, and the
host writes `summary.yaml` in `harbor/run.py::build_summary` *after* the
rewards are already written. So `score_cases` is the single canonical
**producer**, and there are two readers of it:

| path | how `compose_reward` gets the entries |
| --- | --- |
| local | `score.py` folds them into `summary.yaml`; consumers read that |
| Harbor verifier | `score_cases` returns them beside `per_case`; `_score_case_counted` passes them to `compose_reward` explicitly |

Migration steps 4 and 5 are incomplete without that: step 5 cannot drop a
`harness`-attributed sample in the container if nothing carries the attribution
there. Returning the entries from `score_cases` is the smaller change — the
verifier already destructures that return value — — the host side is covered by step 4's transport table, which is longer than
`build_summary` alone.

**A sibling mapping, NOT a key inside `per_case.<case>`.** That is a pure
judge-name → record map today, and every consumer iterates its members
expecting a record: `compose_reward` does `rec.get("value")` on each
(`harbor/reward.py:241,317`), so a list would raise `AttributeError`;
eval-anova's `_judge_columns` would emit a spurious `judge:unmeasured`
observation column; and the key would collide with a judge a project actually
named `unmeasured`. A top-level sibling needs no migration of any judge-map
consumer and cannot collide with a judge name. `run_result.json`'s `per_case`
is not a judge map, so the nested key is safe there.

```json
// run_result.json per_case.<id>.unmeasured — same entry shape as
// summary.yaml's top-level unmeasured[case_id]
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
| `attribution` | `harness` \| `agent` — decides BOTH the reward treatment and whether the case blocks a score threshold, below |
| `detail` | one line for a human |
| `log` | optional pointer, relative to the run directory |

### `attribution` drives both consumers, in opposite directions

| | reward | score thresholds |
| --- | --- | --- |
| `agent` — the graded skill withheld or corrupted its own evidence | `0.0` | **blocks**, on top of being counted |
| `harness` — a positively identified, agent-independent infrastructure fault | **drop the sample** | no extra block (coverage counts it, like every unmeasured case) |

`max_error_rate` has to be widened for that to mean anything. Today its
numerator is the judge aggregate's `errored_cases`, which increments only when
a judge returns no value (`score.py:3680-3685`). A hook or collect failure
where the judges still returned values never reaches it — so a
`harness`-attributed case would be exempt from the score gate AND absent from
the coverage gate, i.e. gated by nothing, even with `max_error_rate: 0`.

The coverage gate therefore counts **cases that produced no valid
measurement**: the union of `errored_cases` and **every** `unmeasured` case,
counted once per case, over a denominator that includes them. The same
accounting is required locally, in Harbor and in EvalHub — otherwise this
repeats the split #277 already complains about.

**Attribution-independent, deliberately.** An earlier draft counted only
`harness`-attributed cases here, which left the `agent`-attributed ones in no
gate at all: excluded from the aggregates, present in the denominator, absent
from the numerator, and reached by the blocking rule only when a score
threshold or baseline happens to be configured. Nine valid cases plus one
agent-induced hook failure, with `max_error_rate: 0` as the only threshold,
passed. Coverage asks *how many cases failed to produce a measurement*, which
is a question about the measurement and not about whose fault it was.
Attribution governs the two things that are about fault — the reward
disposition, and the **additional** automatic block on score thresholds and
baselines — and nothing else.

The two consumers are asking different questions, and the same entry answers
them differently. Reward asks *should this episode train the policy* — a
provider 500 should not. Thresholds ask *did the suite meet the author's bar* —
and the author already has `max_error_rate` to price judge flakiness, so
auto-blocking on it would make one flaky provider fail every run.

This is what keeps the rule compatible with what #260 and #276 already shipped.
A case artifact that cannot be read is `agent`-attributed — the case directory
is the graded skill's own output, which is the whole argument of those PRs — so
it still blocks.

A judge that errored is **usually** `harness`-attributed, so it usually does not —
which is today's behaviour. Not blanket, though, and an earlier draft said it was:
`score_cases` catches arbitrary scorer exceptions, so an inline `check:` judge that
raises on malformed agent output is an agent-induced failure wearing a judge error's
clothes. Dropping that sample is the reward escape again. The judge-error contributor
classifies the cause — provider 5xx, timeout or transport fault is `harness`; a scorer
raising while reading agent content is `agent`; anything it cannot tell apart is
`agent`, per the default in Open Question 2. Acceptance case: malformed agent output
makes a scorer raise, and the sample scores `0.0` rather than being dropped.

**Mixed attribution resolves to `agent`.** A case can carry entries of both
kinds — the example above does. The rule is order-independent and deliberately
asymmetric: if ANY entry is attributed to the agent, the case scores `0.0`
rather than being dropped, and blocks. Dropping is the outcome that removes a
case from training, so it is the one an agent could profit from; a harness
fault that happens to coincide with the agent suppressing evidence must not
launder the suppression into a dropped sample. The spec's own example therefore
scores `0.0` and blocks.

### Case-level, not judge-level

`incomplete_cases` is per judge today, and that is already strained: in #276, when the
case record could not be built, the fix had to stamp **every** judge, because the
condition is not a property of any judge — it is a property of the case. At case level
the stamping loop disappears and per-judge counts derive by counting.

### What changes for each consumer

The table above is the authoritative list of *who must see the entries and what carries
them there* — check new rules against that one. This is the narrower question of what
each consumer does differently once it has them.

| Consumer | Today | With `unmeasured` |
| --- | --- | --- |
| judges | `outputs["hook_failures"]`, a marker file, `staging_skips` | `outputs["unmeasured"]` |
| thresholds | the `incomplete_staging` special case in `detect_regressions` | **every** unmeasured case enters the widened `max_error_rate` numerator; an **`agent`-attributed** one additionally blocks score thresholds and baselines |
| `summary.yaml` | `incomplete_cases` stamped by each layer | `unmeasured_cases`, derived by counting |
| **aggregates** | every non-None judge value joins `mean`/`pass_rate` | an unmeasured case's observations are **excluded** |
| reward | errored ⇒ `0.0` | `attribution: harness` ⇒ drop the sample; `agent` ⇒ `0.0` |
| Harbor / EvalHub | counters dropped | one field to carry |

**An unmeasured case's judge values are not measurements either.** A case can be
unmeasured and still carry verdicts — a hook failed, or collection lost an artifact,
while the judges ran and returned something. `score.py` appends every non-None value
to the sample, so without this rule:

- nine passes plus one harness-failed case that returned `False` gives `pass_rate 0.9`,
  failing `min_pass_rate: 0.95` even though `max_error_rate: 0.1` was set to tolerate
  exactly that one case; and
- the same case returning `True` **inflates** the score with a verdict reached over
  material the harness knows was incomplete.

So: exclude an unmeasured case's observations from `mean`, `pass_rate` and the baseline
comparison, whatever the attribution — the verdict is not evidence about the agent
either way. Keep the case in the coverage denominator (it was attempted) and keep its
raw verdict in `per_case` for diagnostics. Counting a case as unscored while still
scoring it is the inconsistency this whole document exists to remove.

The threshold rule **replaces** `incomplete_staging` rather than sitting beside it.
`max_error_rate` still never auto-fails, for the reason #260 gave: that gate is the
author pricing how much of the dataset may go unscored, and auto-failing it would make
a tolerant error budget impossible. What changes is only what it counts — see the
coverage-gate note above.

### Who reads it, and what carries it there

Five of the six review rounds on this spec found the same thing: a rule stated for one
consumer, with another consumer or another transport hop left without it. So the
inventory goes in the document, and the rules are written against it rather than from
memory.

**Readers.** Every one of these must see the same list for a case:

| Reader | Needs | Local | Harbor | EvalHub |
| --- | --- | --- | --- | --- |
| judges (`outputs["unmeasured"]`) | the case's entries | `score_cases` record | same, in-container | same, in-pod |
| aggregates (`mean`, `pass_rate`) | exclude unmeasured observations | `score_cases` | `build_summary` | `_metrics_to_summary` |
| thresholds + coverage (`detect_regressions`) | per-case count | `summary.yaml` | `summary.yaml` (host) | `runner.py` gate |
| reward (`compose_reward`) | `attribution` | n/a — local does not compose | in-container | in-pod |
| report, eval-anova | per-case detail | `summary.yaml` | `summary.yaml` | n/a |

**Transports.** `score_cases` is the only producer. Each hop below has to carry the
entries, and today none of the containerised ones do:

| Path | Hops | State |
| --- | --- | --- |
| local | `score_cases` → `summary.yaml` | one process; nothing to add |
| Harbor | `score_cases` → `judges.json` → `parse_trial` → `build_summary` → `summary.yaml` | `build_summary` copies only per-judge record fields (`value`, `rationale`, `judge_type`, `usage`, `tool_choice_mode`, `verdict_transport`) — no case-level channel exists |
| Harbor reward | `score_cases` → `_score_case_counted` → `compose_reward` | keeps only `result["per_case"][case]`; in-container, before any `summary.yaml` |
| EvalHub | `score_cases` → `adapter` → `results_mapper` → `JobResults` | `_metrics_to_summary` returns `per_case: {}` literally |

`judges.json` is the Harbor channel to extend: the verifier already writes it, the host
already reads it back, and it already carries per-case judge detail. `reward.json` is
the wrong place — it is a flat metric map Harbor itself consumes. EvalHub needs the
entries through `results_mapper` before the summary is rebuilt.

A disposition scalar is not enough on any of these hops: `phase`, `kind` and mixed
attribution are all needed downstream, so the entries travel whole.

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
   regression metric name so CI output does not churn. Widen the `max_error_rate`
   numerator to "cases that produced no valid measurement" — `errored_cases` union
   **every** `unmeasured` case, counted once per case, whatever its attribution — so
   every lost case lands in a gate and not only the ones with a score threshold.
4. Carry the entries along every hop in the transport table above, not just into the
   two summary builders — `build_summary` and `_metrics_to_summary` are destinations
   with no source reaching them today. For Harbor that means the verifier writing them
   into `judges.json` and `parse_trial` reading them back; for EvalHub, through
   `results_mapper` before the summary is rebuilt. Acceptance cases on both backends
   for a `harness`-only case and for a mixed-attribution one.
5. Teach `compose_reward` to drop a `harness`-attributed sample, and give the Harbor
   verifier a source for the entries (see "canonical producer" above — it has no
   `summary.yaml` at reward time). `compose_reward`
   currently returns a `float` that is written to the Harbor reward file and
   formatted `:.4f`, and `skills/eval-anova/scripts/analyze.py` calls
   `float(composite)` on it — so `None` is not available as a sentinel. Proposal:
   keep the return type a float and carry the disposition beside it, in the
   metrics dict `compose_reward` already returns as its second element
   (`{"reward_disposition": "scored" | "dropped"}`), with the float left at `0.0`
   when dropped. Reports keep showing the case; the training consumer filters on
   the disposition.

   **It has to reach every channel, not just the one that carries metrics.**
   `harbor/reward.py::write_reward` writes three, and only one of them would
   see it as drafted:

   | channel | contents today | needs |
   | --- | --- | --- |
   | `reward.json` | `{reward, **metrics}` | nothing — the disposition rides in `metrics` |
   | `reward.txt` | the bare scalar, Harbor's fallback | a sibling marker, or Harbor must be required not to use the fallback for dropped samples |
   | `judges.json` | `{reward, per_judge}`, no metrics | add the disposition to the sidecar |

   A consumer reading `reward.txt` sees `0.0` and cannot tell a dropped sample
   from a real zero, which is the failure this whole section exists to prevent
   — silently training on a harness fault. So: name the supported training
   consumers, carry the disposition through all three channels, and require a
   consumer that cannot honour a drop to **fail loudly** rather than treat it
   as a zero. A disposition that is merely available is not a contract.
6. Deprecate `incomplete_cases` one minor release later.

### Release impact, step by step

Step 1 is behaviour-preserving: it moves where the same facts are stored and read, and
`incomplete_cases` is derived so existing gates and docs keep working.

**Step 3 is not**, and an earlier draft said it was. It widens the `max_error_rate`
numerator and excludes unmeasured observations from the aggregates, either of which can
newly fail a local run. The release note belongs here, not only on step 4.

**Step 2 is not**, and an earlier draft wrongly said it was. Adding the judge-error
contributor would, under a naive "any `unmeasured` entry blocks", turn a run that
passes today — nine scored cases and one provider 500, inside a declared
`max_error_rate` — into a failing one. That is why the gate keys on
`attribution`, not on the mere presence of an entry: a judge error is
`harness`-attributed, so it drops the reward sample and leaves the score
thresholds alone. It is not quite "as today", because **step 3** widens the
`max_error_rate` numerator to count unmeasured cases whose judges still returned
values — without that, such a case is exempt from every gate. For a
judge error the two coincide (`errored_cases` already counts it), so the nine-plus-one
run above is unaffected; a hook failure that previously counted toward nothing now
counts toward the author's declared error budget. Release-note that, and set it out
with step 2 rather than attributing all new failures to step 4.

Step 4 makes containerised runs *start* failing where they silently passed, which is
the point, and wants a release note. Step 5 changes RL training data and should land
on its own.

## Open questions

1. **The name, and a collision.** `unmeasured` is the author's pick. `void`, `invalid`,
   `unjudgeable` were considered; `infra_failures` was rejected because the
   agent-suppressed-evidence case is not infrastructure failing, and the unifying
   property is that the measurement is void rather than whose fault it was.

   But the word is **already spent on a different referent** in PR #200
   (measurement-validity, open since 2026-08-21): `_VALIDITY_LAYER_NAMES` /
   `unmeasured_layers` / `"status": "unmeasured"` describe validity **layers**, not
   cases — and both write into `summary.yaml`. If both land as drafted,
   `validity.v_total.unmeasured_layers` and a case-level `unmeasured` list sit one key
   apart meaning different things. One of the two should move. Since #200 is 204
   commits behind and will be re-landed in slices, whichever lands second should
   rename; this spec is the cheaper one to change while nothing writes it.
2. **Is `attribution` binary?** A judge-provider 500 is `harness`; an agent hiding an
   artifact is `agent`. A hook that fails *because* the agent left the repo dirty is
   neither cleanly. Proposal: keep it binary, let `kind` carry the nuance, and make
   the rules about who may set what:

   - **`harness` is the narrow case, not the default.** It may be set only by harness
     code that has positive evidence the fault was **independent of the graded
     agent** — a provider 5xx, a control-plane failure, the harness's own crash.
     Anything else, including a missing or unrecognised value, resolves to `agent`.
   - **Agent-inducible faults are `agent`, however they surface.** An OOM kill, a
     resource-limit termination, a disk-quota failure and an ambiguous container
     exit are all reachable by the graded agent, so "a container failure" is NOT
     by itself evidence of agent-independent causation. Classify them `agent`
     unless independent causation is established. Without this the escape the
     Problem section forbids is open at one remove: an agent that induces an OOM
     produces a single `harness` entry, mixed-attribution precedence never
     applies because there is no `agent` entry to mix, and the sample is dropped
     instead of scoring `0.0`.
   - **Acceptance criterion.** The implementation ships with a case that induces
     each of those failures and asserts the sample is scored `0.0`, not dropped.
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
