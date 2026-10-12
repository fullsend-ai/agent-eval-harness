# Comparing runs with ANOVA

This recipe runs a Design-of-Experiments eval over four **real bugfix PRs** and
measures whether giving the agent a knowledge-graph MCP server
(`context=cognee`) actually beats a bare agent (`context=none`) at repo-editing
quality. It uses the generic [`cli` runner](../concepts/runners.md) — no Harbor,
no committed credentials — and the [`/eval-anova`](../guides/eval-anova.md)
orchestrator to fan the eval out, run the ANOVA, and render the comparison.

!!! info "Concept vs. cookbook"
    This page is task-oriented. For the statistics behind it — factorial design,
    repeated-measures vs mixed-effects ANOVA, the Pareto frontier — see
    [Analysis of variance](../concepts/anova.md).

The complete, runnable example lives at
[`eval/anova-example/`](https://github.com/opendatahub-io/agent-eval-harness/tree/main/eval/anova-example)
(README, `eval.yaml`, `solve.sh`, MCP configs, dataset, and committed
`sample-runs/`).

## Recipe 1 — a context A/B on one model

The [`matrix:`](../reference/config/matrix.md) block varies one non-model factor (`context`) on a single model:

```yaml title="eval.yaml (excerpt)"
matrix:
  factors:
    model:
      - claude-sonnet-4-6
    context:
      - cognee
      - none
  replications: 1
```

`model` maps to the runner's `--model`. `context` is a **non-model factor**: the
orchestrator injects it with `execute.py --input-override context=<level>`,
merging it into each case's `input.yaml`, so `{context}` resolves in the `cli`
command and selects the right MCP config file.

```yaml title="eval.yaml (excerpt)"
execution:
  mode: case
  arguments: "{prompt}"        # unused by solve.sh (it reads the prompt from input.yaml)
  timeout: 1800

runner:
  type: cli
  command: >-
    bash {config_dir}/solve.sh {workspace} {output_dir} {model} {config_dir}/mcp-{context}.json

models:
  judge: claude-sonnet-4-6

dataset:
  path: dataset

outputs:
  - path: output
    schema: "solution.diff — the agent's changes captured by solve.sh"
```

The `{config_dir}` placeholder points at the eval directory, so `solve.sh` and
the MCP configs resolve no matter where you run from. The two `context` levels
are just two MCP files:

```json title="mcp-cognee.json"
{ "mcpServers": { "cognee": { "type": "streamable-http",
    "url": "${COGNEE_MCP_URL:-http://localhost:8321/mcp}" } } }
```

```json title="mcp-none.json"
{ "mcpServers": {} }
```

Two judges score each run — an objective gate plus a quality rubric (the
LLM judge's prompt is lightly tidied from the committed file: a numeric judge
commits its verdict through the harness's `submit_score` tool, so the prompt
needs no "respond with JSON" instructions, and iterating `outputs.files` keeps
each collected file fenced and labelled instead of dumping a dict):

```yaml title="eval.yaml (judges, adapted)"
judges:
  - name: tests_pass           # objective boolean gate — reads the collected tests.json
    check: |
      import json
      files = outputs.get("files", {})
      for path, content in files.items():
          if path.endswith("tests.json") and isinstance(content, str):
              d = json.loads(content)
              if d.get("passed") is True:
                  return True, "module tests passed"
              if d.get("passed") is False:
                  return False, "module tests failed"
              return False, d.get("skipped", "tests not run")
      return False, "no tests.json in outputs"

  - name: solution_quality     # LLM rubric, 1-5, vs the merged-PR oracle
    feedback_type: int
    score_range: [1, 5]
    prompt: |
      Score the agent's attempt at a models-as-a-service coding task.

      ## Task instruction
      {{ outputs.annotation_instruction_content }}
      ## Agent's changes (including output/solution.diff)
      {% for path, content in outputs.files.items() %}
      ### {{ path }}
      {{ content }}
      {% endfor %}
      ## Reference oracle patch (the merged PR)
      {{ outputs.annotation_oracle_content }}

      Assess whether it addresses the core problem, whether the changes are
      sound, and whether they would plausibly match the oracle / pass the tests.
      Score 1-5 (1=no meaningful attempt, 3=addresses it with gaps,
      5=comparable to the oracle).

thresholds:
  tests_pass:
    min_pass_rate: 0.5
  solution_quality:
    min_mean: 3.0
```

The composite gates on `tests_pass` (a failing fix scores `0`), and otherwise
uses the normalized `solution_quality` — exactly the metric ANOVA runs on.

Run it locally (the generic path — needs an API key and the Go toolchain for the
`tests_pass` gate):

```bash
pip install -e ".[anova,anthropic]"
export ANTHROPIC_API_KEY=sk-...
export COGNEE_MCP_URL=http://<your-cognee-mcp>/mcp   # only for context=cognee

# See the design + cost before committing:
python3 skills/eval-anova/scripts/orchestrate.py --config eval/anova-example/eval.yaml --dry-run

# Fan out, analyze, and render:
python3 skills/eval-anova/scripts/orchestrate.py --config eval/anova-example/eval.yaml
```

`solve.sh` does a fresh `git` checkout of `opendatahub-io/models-as-a-service` at
a fixed base commit, runs the agent, captures the diff to `output/solution.diff`,
and runs `go test ./...` per touched module into `output/tests.json`. Override
`MAAS_BASE_COMMIT`, `AGENT_CMD`, or `TEST_CMD` to adapt it.

## Recipe 2 — scale to a model × context grid

To ask "does cognee help *every* model, or only some?", add levels under
`matrix.factors.model` — nothing else changes. The design becomes full-factorial
and the ANOVA gains a second factor (and their interaction):

```yaml title="eval.yaml (excerpt)"
matrix:
  factors:
    model:
      - claude-opus-4-8
      - claude-sonnet-4-6
    context:
      - cognee
      - none
  replications: 3
```

That's `2 × 2 = 4` conditions; with 3 replications over 4 cases → `4 × 4 × 3 =
48` runs. Use `--dry-run` first to see the cost.

With multiple factors, `anova.json` reports one joint Wald test per term —
`model`, `context`, and the `model:context` interaction (the row that answers
"only some models") — with raw and adjusted p (Holm by default) side by
side (switch methods with `--correction bh` or `none`; see
[the statistics](../concepts/anova.md#per-term-wald-tests-and-multiplicity-correction)).
There is no overall F in this design: the mixed model answers per term.

**Following up a significant `model:context` interaction.** A significant
interaction means "does cognee help?" has a different answer per model, so
the marginal `context` row is no longer the story. Read it this way:

1. `anova.p_adjusted["model:context"]` is the verdict; if it is significant,
   do not quote the `context` main effect on its own.
2. The `contrasts.context` block is then flagged `contrast_type:
   reference-cell` (its `note` says so): the cognee-vs-none estimate is the
   difference *at the reference model* (the first `model` level in sorted
   order — `claude-opus-4-8` here), not an average across models. Likewise
   `contrasts.model` is the model gap at `context=cognee`.
3. For the direction per model, read the four cell means in
   `condition_summaries` (each entry carries its `levels`), or the
   `per_case` matrix in the report.
4. For a *tested* per-model answer, re-analyze one model's runs on their own:
   copy (not symlink — the discovery walk does not follow symlinks) that
   model's run directories into a scratch `<runs>/anova-example/` and run
   `--analyze-only` with `AGENT_EVAL_RUNS_DIR=<runs>`. With a single `model`
   level left, the analysis drops the factor and reports a one-way `context`
   comparison with a `paired` contrast — a real cognee-vs-none estimate for
   that model, on the composite scale.
5. Add `--per-judge` to see *which* judge moves — whether cognee is lifting
   the `tests_pass` rate, the `solution_quality` rubric, or both. The
   per-judge rows are BH-screened as one family and are leads, not
   headlines; the composite keeps its own Holm family.

```bash
# Re-analysis is free — no cell is re-executed:
python3 skills/eval-anova/scripts/orchestrate.py --config eval/anova-example/eval.yaml \
    --analyze-only --per-judge
```

## Reproduce the analysis offline

The example ships committed `sample-runs/`, so you can exercise the analysis and
report path with **no API key and no checkout** — point `AGENT_EVAL_RUNS_DIR` at
them and run `--analyze-only`:

```bash
AGENT_EVAL_RUNS_DIR=eval/anova-example/sample-runs \
  python3 skills/eval-anova/scripts/orchestrate.py \
  --config eval/anova-example/eval.yaml --analyze-only

open eval/anova-example/sample-runs/anova-example/comparison-report/index.html
```

This recomputes `anova.json` from the recorded `summary.yaml` files and
regenerates the `/eval-compare` report — including the Statistical Significance
section — entirely offline. It writes `anova.json` and `comparison-report/`
*inside* `sample-runs/anova-example/`; copy `sample-runs/` somewhere scratch
first if you want to keep the checkout clean (nothing below is meant to be
committed).

## Reading the offline example

What follows is the real output of that command — re-run with `--per-judge`
and `--no-report` to keep it to the artifact — read in the order
[When is a difference real?](../concepts/anova.md#when-is-a-difference-real)
recommends. The design is four cases, one replication, and one model, so the
`model` factor has a single level and drops out: this is a one-way `context`
comparison.

```bash
AGENT_EVAL_RUNS_DIR=eval/anova-example/sample-runs \
  python3 skills/eval-anova/scripts/orchestrate.py \
  --config eval/anova-example/eval.yaml --analyze-only --per-judge --no-report
```

```text
Wrote stats artifact: eval/anova-example/sample-runs/anova-example/anova.json
ANOVA: Repeated-measures ANOVA (pingouin rm_anova) — not significant
Per-judge ANOVA: 1 judge(s) analysed (BH family of 1 test(s), 1 excluded)
```

### 1. The omnibus: `jq .anova`

```json
{
  "f_statistic": 3.947368421052632,
  "p_value": 0.14112193971403447,
  "p_uncorrected": 0.14112193971403447,
  "p_adjusted": 0.14112193971403447,
  "significant": false,
  "correction": "holm",
  "family_size": 1,
  "method": "Repeated-measures ANOVA (pingouin rm_anova)",
  "alpha": 0.05,
  "factor": "context",
  "details": [
    {
      "Source": "context",
      "ddof1": 1,
      "ddof2": 3,
      "F": 3.947368421052632,
      "p_unc": 0.14112193971403447,
      "ng2": 0.16556291390728478,
      "eps": 1.0
    }
  ]
}
```

One effective factor, so the result is scalar: `F(1, 3) = 3.95`, `p = 0.141`.
`p_adjusted` equals `p_value` because a single factor is a family of one
(`family_size: 1`) — Holm has nothing to correct. The verdict is **not
significant** at α = 0.05. Notice the tension with the effect size: η²
(`ng2`) is 0.166, "large" by the usual buckets. A large effect that fails to
reach significance is the signature of low power — four cases and one
replication give `ddof2 = 3` residual degrees of freedom. This is "not enough
data", not "no difference".

### 2. The contrast: `jq .contrasts`

```json
{
  "context": {
    "correction": "holm",
    "family": "pairwise level contrasts within factor 'context'",
    "family_size": 1,
    "contrast_type": "paired",
    "omnibus_p_adjusted": 0.14112193971403447,
    "pairs": [
      {
        "a": "cognee",
        "b": "none",
        "estimate": 0.3125,
        "se": 0.15728821740147395,
        "p_raw": 0.14112193971403447,
        "p_adjusted": 0.14112193971403447,
        "significant": false
      }
    ],
    "note": "Estimates are observed paired mean differences (a − b) across cases."
  }
}
```

Two levels make one pair. `estimate: 0.3125` is `cognee − none` on the
composite scale — cognee scored 0.31 higher out of 1 (condition means 0.625
vs 0.3125), with a standard error of 0.157. `contrast_type: paired` says this
is pingouin's paired test across the four cases, so the estimate is an
observed mean difference, not a model coefficient. With two levels the paired
test and the omnibus are the same question, hence the identical p. The
direction is suggestive; it is not established.

### 3. Per-judge screening: `jq .per_judge`

```json
{
  "correction": "bh",
  "family_size": 1,
  "alpha": 0.05,
  "judges": {
    "tests_pass": {
      "method": "Repeated-measures ANOVA (pingouin rm_anova)",
      "terms": {
        "context": {
          "p_raw": 0.3910022189557705,
          "p_adjusted": 0.3910022189557705,
          "significant": false
        }
      },
      "n_cases": 4,
      "n_conditions": 2
    }
  },
  "excluded": [
    {
      "judge": "solution_quality",
      "reason": "Degenerate design — near-zero within-subject variance produced a non-finite F."
    }
  ],
  "note": "Benjamini-Hochberg (FDR) across the one family of 1 (judge, term) test(s); screening only — the composite ANOVA keeps its own separate correction family."
}
```

`tests_pass` is a boolean judge analysed as a 0/1 rate (3 of 4 passing with
cognee, 2 of 4 without): `p = 0.391`, nowhere near significant. The
interesting row is the *excluded* one. `solution_quality` scored exactly one
point higher with cognee on every case (5/4/4/3 vs 4/3/3/2), so the four
paired differences are all `+1` — zero within-subject variance, and the
F-ratio's denominator vanishes. A perfectly consistent shift is the one thing
a repeated-measures F cannot quantify, and the harness says so with a reason
rather than printing a fabricated `p = 0.0`. The family therefore holds one
real test, and BH leaves it unchanged. (The composite was analysable because
`tests_pass` gating two cases to `0.0` put variance back into the paired
differences.)

### 4. The rendered tables

`python3 skills/eval-anova/scripts/report.py eval/anova-example/sample-runs/anova-example`
writes `anova-report.md` and `anova-report.html` next to `anova.json`. The
HTML badge reads **not significant · adj. p=0.141** — labelled *adj.* because a
correction ran, even though it changed nothing here — and the two sections
that matter render as:

```markdown
## Pairwise contrasts (post-hoc)

### context

- Holm-corrected across the 1 contrast(s) within this factor; significance on adjusted p. Omnibus p-adj: 0.1411.
- Estimates are observed paired mean differences (a − b) across cases.

| A | B | Estimate | SE | p (raw) | p (adj) | Result |
|---|---|---|---|---|---|---|
| cognee | none | 0.312 | 0.157 | 0.1411 | 0.1411 | not significant |

## Per-judge effects (screening)

| Judge | Term | p (raw) | p (adj) | Result | n cases |
|---|---|---|---|---|---|
| tests_pass | context | 0.3910 | 0.3910 | not significant | 4 |

*Benjamini-Hochberg (FDR)-corrected across one family of 1 (judge, term) test(s); screening only — the composite ANOVA keeps its own correction family.*

- Excluded from the family: solution_quality — Degenerate design — near-zero within-subject variance produced a non-finite F.
```

The `/eval-compare` report shows the same numbers under **Statistical
Significance (ANOVA)** — titled **Pairwise level contrasts (post-hoc)** and
**Per-judge effects (screening)** there — followed by the Pareto table. Both
conditions cost `$0.14`, so the frontier holds `context=cognee` alone (higher
mean at equal cost); the frontier is descriptive and knows nothing about
significance. (In the deep report's ranked means table both rows read
`claude-sonnet-4-6`, because that column is the model; the per-case matrix
underneath carries the full `context=…, model=…` levels.)

### The verdict

Step 1 of the reading order already says *not established*: a +0.31
composite difference with `adj. p = 0.141` on four cases. The honest summary
is "cognee looks better on every case, including a uniform +1 on the rubric,
but this sample cannot rule out noise". The next move is not a stronger
adjective — it is `replications: 3` (or more cases), after which the same
`--analyze-only` reads the new runs for free.

!!! tip "Containerize it with Harbor"
    To run the trials in containers instead, point `runner.command` at
    `harbor run` and pass cloud/MCP settings via env or `--input-override`
    (never commit credentials). Install the Harbor extra with
    `pip install -e ".[anova,harbor]"` (Python ≥ 3.12). See
    [Running on Harbor](../guides/harbor.md).

## Related

<div class="grid cards" markdown>

- [**Analysis of variance**](../concepts/anova.md) — the statistics behind this recipe
- [**/eval-anova guide**](../guides/eval-anova.md) — every flag and the fan-out flow
- [**/eval-compare**](../guides/eval-compare.md) — the report that surfaces the stats
- [**Writing custom judges**](custom-judges.md) — the `check` and rubric judges used here

</div>
