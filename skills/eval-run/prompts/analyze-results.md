You are analyzing the results of a skill evaluation run. Your job is to identify patterns in failures, suggest root causes, and recommend next steps.

## Input

You will be given:
1. The scoring summary (pass/fail per judge, aggregate metrics)
2. Per-case results (which cases passed/failed which judges)
3. The skill being evaluated (what it does)
4. Run metrics (`summary.yaml.run_metrics`): workload-agnostic cost figures — `cost_per_turn_usd`, `output_tokens_per_turn`, `cache_hit_rate`, `cost_per_mtok_usd`. Stable across runs of the same model + effort.
5. Run result (`run_result.json`): headline cost, duration, turns, token usage, model, effort, per-case `permission_denials` (structured denied tool calls).
6. Collection summary (`collection.json`): per-case artifact counts — the actual output volumes the skill produced.
7. Eval config (`eval.yaml`): in particular the `outputs` block, which describes what artifacts the skill is expected to produce.
8. Optionally: a baseline comparison (what changed vs previous run)

## Analysis Framework

### 1. Aggregate Assessment
- Overall pass rate across all judges
- Which judges have the lowest pass rates?
- Is the pass rate consistent across cases, or are there outliers?

### 2. Failure Pattern Analysis
- **Clustered failures**: Do certain cases fail multiple judges? (suggests the case itself is problematic or the skill fails on certain input types)
- **Judge-specific failures**: Does one judge fail across many cases? (suggests a systematic skill issue matching that judge's criteria)
- **Sporadic failures**: Random failures across cases/judges? (suggests non-determinism or edge cases)

### 3. Root Cause Hypotheses
For each failure pattern, hypothesize WHY:
- Is the skill not producing expected artifacts? (execution issue)
- Is the skill producing artifacts but with quality issues? (quality issue)
- Is the skill partially completing? (timeout, budget, permission issue —
  confirm via each case's `permission_denials` in `run_result.json`, whose
  entries name the denied tool and input — or
  background tasks killed at the bg-wait ceiling -- exit_code=1 with an
  `ERROR: ... bg-wait ceiling` note at the end of stderr)
- Does the failure correlate with input complexity? (scaling issue)

### 4. Regression Analysis (if baseline provided)
- What got worse? (new failures that weren't in baseline)
- What got better? (failures in baseline that now pass)
- Is the regression concentrated in specific cases or judges?

### 4b. Cost Attribution

The headline cost (`cost_usd` in `run_result.json`) mixes three independent effects:
- **Model/runner cost** — how much each turn or token costs. Surfaced in `run_metrics`: `cost_per_turn_usd`, `cost_per_mtok_usd`, `cache_hit_rate`. These stay flat across runs of the same model + effort, so any drift here is a model, runner-version, or effort change — **not** the skill doing different work.
- **Workload cost** — how much work the skill did. Stochastic per run: more revisions, splits, retries, or fan-out subagents → higher cost without any model/runner change.
- **Billed-vs-conversation gap** -- `cost_usd` is billed cost: when background
  agents are killed at the bg-wait ceiling or still running at an evaluator
  timeout, their tokens are billed but produce no conversation turns, inflating
  `cost_per_turn_usd` with no model/runner change. Check the end of stderr.log
  for the bg-kill ERROR note and compare the `per_model_usage` sum against the
  headline. Baselines from harness versions predating the billed-cost rule
  under-report cost -- treat cross-boundary cost jumps as a measurement
  correction, not a regression.

Attribute the gap by deriving a **skill-specific cost-per-unit-of-work** metric on the fly:

1. Read `eval.yaml.outputs` to identify what units the skill produces (each entry has a `path` and a `schema` describing the artifact). Pick the 1–2 most meaningful units — typically the primary artifact the skill is designed to create, and, if the skill produces variable side-effects, a denominator that captures fan-out (subagent transcripts under `subagents/`, items inside per-run reports the skill writes, etc.).
2. Pull actual production counts from `collection.json` (per-case artifact counts), from per-run report files the skill emits, or by listing the artifact directories. Sum across cases.
3. Compute `cost_usd / unit_count` for each candidate normalizer, both for the current run and the baseline.
4. Compare the deltas:
   - `run_metrics` flat AND `cost_per_unit` flat → no real cost change; headline gap is workload variance only.
   - `run_metrics` flat AND `cost_per_unit` shifted → cost change is real and skill-attributable (efficiency drifted).
   - `run_metrics` shifted → model/runner/effort changed; subtract that effect before judging the skill.

State the attribution explicitly. Template:
> "Cost rose X% headline, but `cost_per_<unit>` only rose Y% and `cost_per_turn` is flat — the gap is mostly N extra `<units>` from <stochastic cause>, not skill or model regression."

### 5. Recommendations
Prioritize by impact:
- **CRITICAL**: Fixes that would improve pass rate significantly
- **HIGH**: Fixes for systematic patterns
- **MEDIUM**: Edge case improvements
- **LOW**: Nice-to-have quality improvements

For each recommendation, be specific:
- What to change (in the skill, in the test cases, or in the judges)
- Why you think it will help
- What risk it carries (could it cause regressions elsewhere?)

## Output Format

Lead with `## Recommendation` so readers see the call-to-action before the supporting evidence. Then provide the analysis details. The full structure:

```markdown
## Recommendation

**[One-line headline verdict — e.g. "Promote opus-4-7 as the primary skill model" or "Investigate revision_quality regression on long-input cases".]**

[2–4 sentences explaining the verdict and why it matters. Reference the specific evidence — judge name, case IDs, score deltas — that drives the conclusion.]

**Top actions:**
- **[CRITICAL/HIGH/MEDIUM/LOW]** — [Most important action]
- **[CRITICAL/HIGH/MEDIUM/LOW]** — [Second action]
- **[CRITICAL/HIGH/MEDIUM/LOW]** — [Third action, if material]

## Summary

[Aggregate scores table, pass rates, run metrics (duration/cost/turns), headline numbers — the at-a-glance dashboard.]

## Failure Patterns

[Clustered failures, judge-specific failures, sporadic patterns — only if there are failures or notable variance.]

## Root Causes

[Hypotheses for each failure pattern, tied to specific evidence — judge names, case IDs, input characteristics.]

## Regressions

[If `--baseline` was provided: what got worse, what got better, where the change is concentrated. Omit if no baseline.]

## Cost Attribution

[Show the workload-agnostic deltas (`cost_per_turn_usd`, `cost_per_mtok_usd`, `cache_hit_rate`) alongside one or two skill-specific cost-per-unit metrics derived from `eval.yaml.outputs` and `collection.json`. State whether the headline cost is model/runner/effort-driven, workload-driven, or skill-efficiency-driven — or whether all three are flat (which is itself worth confirming). When comparing different models, this is the section that makes pricing tradeoffs legible.]
```

Use tables for per-case data. Be decisive — don't hedge with "might" or "could be". State your assessment and the evidence supporting it. The Recommendation section is the only thing many readers will see, so make it self-contained — it should land even without the supporting sections below.

## Saving the analysis

Write analysis to `<run-dir>/analysis.md` with YAML
frontmatter so the HTML report can attribute it:

```markdown
---
agent: Claude Code
model: <your own model id — the model YOU are running as>
date: <UTC ISO 8601>
---

## Recommendation
...
## Summary
...
```

The frontmatter is consumed by the report renderer (not displayed verbatim).
The file is rendered as a prominent callout near the top of the HTML report.

`model` is **your own** model — the one you're running as (from your own
context, any provider), not the model under test (`--model` / the eval subject),
which the report already shows in its Model row. Omit it only if you can't
determine your own.
