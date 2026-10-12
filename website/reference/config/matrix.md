# matrix

`matrix` declares a **factorial experiment** for [`/eval-anova`](../../guides/eval-anova.md):
the knobs to vary (factors), their values (levels), how often to repeat each cell, and
how the resulting p-values are corrected. It is the one block a comparison adds on
top of a normal `eval.yaml` — everything else (dataset, judges, thresholds) is reused
as-is, and `/eval-run` ignores the block entirely.

```yaml
matrix:
  factors:
    model:
      - claude-opus-4-8
      - claude-sonnet-4-6
    effort:
      - low
      - high
  replications: 3         # optional, default 1
  analysis:               # optional
    correction: holm      # holm (default) | bh (alias fdr_bh) | none
    per_judge: false      # opt-in per-judge screening
```

The block is parsed by `MatrixBuilder.from_yaml` through the same raw loader as the
rest of the config, so an [`extends`](extends.md) profile can add or override factors.

## `factors` (required)

A mapping of factor name to a **non-empty list** of levels. Every combination of
levels — the Cartesian product — becomes one **condition**, and each condition gets a
stable `condition_id` (the first 12 hex digits of a SHA-256 over its sorted levels).
Factor names are sorted before expansion, so the order you write them in does not
change the ids.

How a level reaches each run is decided by the factor's **name**:

| Factor | How it reaches the run |
| --- | --- |
| `model` | `--model <level>` on the runner. A condition without a `model` level falls back to `models.skill`; with neither, the orchestrator exits before running anything. |
| `effort` | `--effort <level>` on the runner. |
| `subagent` / `subagent_model` | `--subagent-model <level>` (`subagent` wins when both are present). |
| anything else | `--input-override <name>=<level>`, merged into the case's `input.yaml`. It changes behaviour only if something consumes it — `{name}` in a `cli` runner command, or `{{ input.name }}` in `execution.arguments` / `execution.prompt`. A factor nothing consumes still defines a distinct condition; it just won't alter the run. |

!!! warning "Levels must be a YAML list"
    ```yaml
    factors:
      model: claude-opus-4-8      # ✗ rejected — a scalar, not a list
      model: [claude-opus-4-8]    # ✓ a one-level list
    ```

    A scalar would otherwise be iterated character by character into a garbage
    design, and an empty list would yield zero conditions — both fail at load.

## `replications` (optional, default `1`)

How many times to repeat every condition × case combination. Replications are
averaged into one observation per (condition, case) before the ANOVA and shrink the
per-cell noise; the cost grows linearly.

```text
total runs = conditions × cases × replications
```

| Replications | Use |
| --- | --- |
| `1` | Quick screening, high noise |
| `3` | A good default for most comparisons |
| `5+` | High-confidence results, expensive |

With more than one replication each run id carries an `-r<n>` suffix
(`2026-07-30-opus-effort-high-r2`).

## `analysis.correction` (optional, default `holm`)

The multiple-comparison correction applied across the ANOVA's **family of term
tests** — every main effect plus every interaction from the one fitted model:

| Value | Controls | When |
| --- | --- | --- |
| `holm` | family-wise error rate (Holm step-down) | the default; a confirmatory question |
| `bh` (alias `fdr_bh`) | false discovery rate (Benjamini–Hochberg) | less conservative; screening many factors |
| `none` | nothing — significance on raw p-values | |

Spelling is case-insensitive; `anova.json` always records the canonical short name
(`fdr_bh` is written as `bh`). Both raw and adjusted p are reported;
`significant` is computed on the adjusted value (on raw under `none`). Degenerate
terms (no finite p) are listed under `excluded_terms` and never counted in the family.

The same method also corrects the **post-hoc level contrasts** written under
`anova.json`'s `contrasts` key, where the family is the pairs *within one factor* —
never pooled across factors.

**Precedence:** `--correction` on `orchestrate.py` › `matrix.analysis.correction` ›
`holm`.

## `analysis.per_judge` (optional, default `false`)

Opt-in **per-judge screening**: the same single/multi-factor ANOVA the composite gets is
run once per judge over that judge's own per-case values (numeric as-is, booleans as
0/1; pairwise verdicts and error samples contribute nothing). All resulting
(judge, term) raw p-values are corrected as **one Benjamini–Hochberg family**,
regardless of `analysis.correction` — the composite ANOVA keeps its own family and
stays the headline result.

`anova.json` gains a `per_judge` block; a judge with a degenerate design is listed
under `per_judge.excluded` with a reason and adds no test to the family. Enable it
with this key **or** `--per-judge` — either turns it on, and the flag cannot turn a
configured `true` off. Off by default because it multiplies model fits and report
rows.

## Validation at load time

`MatrixBuilder.from_yaml` validates the block before any run executes, so a typo
fails at config parse rather than after the matrix has spent its budget:

- `matrix`, `matrix.factors`, and `matrix.analysis` must each be a mapping;
- every factor's levels must be a **non-empty list** (a scalar or an empty list is
  rejected, with a hint to write `name: [a, b]`);
- `replications` must be an integer `>= 1` — a boolean is rejected even though
  Python treats it as an int;
- `analysis.correction` must be a **string** among `holm`, `bh`, `fdr_bh`, `none`
  (any case) — a non-string is rejected rather than stringified, so an accidental
  YAML `null` cannot turn into `"none"` and silently disable correction;
- `analysis.per_judge` must be a **boolean** — `"false"` as a string, `0`, or `1` are
  rejected.

`/eval-anova` additionally requires at least one factor (`strict` mode). The
`--analyze-only` path reads the block non-strictly so it can also analyse run
directories a CI fan-out produced without a matrix.

## Full example

```yaml title="eval.yaml"
name: context-ab

execution:
  mode: case
  prompt: "{{ input.prompt }}"      # non-model factors arrive as input.<name>

models:
  skill: claude-sonnet-4-6          # fallback when a condition has no `model` level
  judge: claude-opus-4-6

dataset:
  path: eval/dataset/cases
  schema: "Each case has an input.yaml with a 'prompt' field."

judges:
  - name: tests_pass
    check: |
      return outputs.get("exit_code") == 0, "exit code"
  - name: solution_quality
    prompt: "Score 1-5 the quality of the solution.\n\n{{ outputs }}"
    score_range: [1, 5]

matrix:
  factors:
    model: [claude-opus-4-8, claude-sonnet-4-6]    # → --model
    context: [none, cognee]                        # → --input-override context=<level>
  replications: 2
  analysis:
    correction: bh          # screening many terms — control the FDR
    per_judge: true         # which judge moves, not just the composite
```

This is a `2 × 2 = 4`-condition design: 4 conditions × N cases × 2 replications
runs, a mixed-effects ANOVA with terms `model`, `context`, and `model:context`, and
a per-judge screen over `tests_pass` and `solution_quality`. Preview the grid and the
cost with `orchestrate.py --config eval.yaml --dry-run` before committing.

## Related

<div class="grid cards" markdown>

- [**Analysis of variance**](../../concepts/anova.md) — factors, conditions, the composite, corrections, contrasts
- [**/eval-anova guide**](../../guides/eval-anova.md) — run a matrix end to end, every flag
- [**Cookbook: comparing runs with ANOVA**](../../cookbook/anova.md) — a worked context A/B and a model × context grid
- [**Experiment-level artifacts**](../runs-directory.md#experiment-level-artifacts) — `condition.json`, `anova.json`, `comparison-report/`
- [**reward**](reward.md) — how judges collapse into the composite the ANOVA runs on

</div>
