# CI integration & regression gating

Wire an eval into continuous integration so a pull request that degrades your skill
fails the build. The mechanism is deliberately small: you declare per-judge
[`thresholds`](../reference/config/thresholds.md) in `eval.yaml`, and the scorer
**exits non-zero** when a run falls below them — which is all a CI job needs to gate on.

!!! note "This is an evolving area"
    The harness ships the primitives for CI gating (thresholds + a non-zero exit), and
    the GitHub Actions job below works today. Turnkey CI recipes, reusable actions, and
    caching patterns are still being fleshed out — see
    [remaining work](https://github.com/opendatahub-io/agent-eval-harness/blob/main/CLAUDE.md).
    Treat the example as a starting point to adapt, not a drop-in.

## How gating works

Scoring aggregates each judge by value type — **boolean** judges become a `pass_rate`,
**numeric** judges become a `mean`, and a `--baseline` [pairwise](../concepts/pairwise-and-sampling.md)
comparison produces a `win_rate`. Regression detection compares those aggregates against
the `thresholds` block and, on any breach, returns exit code `1`.

```mermaid
flowchart TD
    A[score.py judges] --> B{thresholds<br/>configured?}
    B -- no --> P[exit 0]
    B -- yes --> C[detect_regressions<br/>aggregates vs thresholds]
    C --> D{any breach?}
    D -- no --> E["print REGRESSIONS: 0<br/>exit 0"]
    D -- yes --> F["print REGRESSIONS: N detected<br/>exit 1 → CI fails"]
```

## Thresholds

`thresholds` is a top-level map of **judge name → gate**. Each gate sets one or more of
four keys; a run regresses if the matching aggregate misses its bound.

| Key | Applies to | Aggregate compared | Passes when |
| --- | --- | --- | --- |
| `min_pass_rate` | boolean judges | `pass_rate` (fraction of cases that returned `True`) | `pass_rate >= min_pass_rate` |
| `min_mean` | numeric judges (e.g. 1–5 LLM scores) | `mean` (average value across cases) | `mean >= min_mean` |
| `min_win_rate` | a pairwise judge (needs `--baseline`) | `win_rate` | `win_rate >= min_win_rate` |
| `max_error_rate` | any judge (opt-in) | fraction of cases where the judge errored | `error_rate <= max_error_rate` |

```yaml title="eval.yaml (excerpt)"
judges:
  - name: has_content          # boolean judge → pass_rate
    check: |
      content = outputs.get("main_content", "")
      return (len(content.strip()) >= 100,
              f"{len(content.strip())} chars")
  - name: completeness       # numeric judge (1–5) → mean
    feedback_type: int
    score_range: [1, 5]        # declare the scale — omitting it warns at config load
    prompt: "Score 1-5 how completely the output covers the request (1 = most requirements missing, 3 = basics with gaps, 5 = complete).\n\nRequest:\n{{ inputs }}\n\nOutput:\n{{ outputs }}"

thresholds:
  has_content:    { min_pass_rate: 1.0 }   # every case must pass
  completeness: { min_mean: 3.5 }        # average score must stay >= 3.5
```

!!! warning "An unavailable metric counts as a regression"
    If a threshold is set but its aggregate is `None`, that is reported as a regression,
    not silently skipped. This is almost always a config mistake — the judge was skipped
    for every case (its `if:` condition, or it errored), or the key targets the wrong
    judge type (e.g. `min_pass_rate` on a numeric judge, whose `pass_rate` is always
    `None`). Match the key to the judge's value type.

## Exit-code behavior

Two entry points enforce thresholds; **both `sys.exit(1)` on regression** so a CI runner
fails the step automatically.

=== "Inline (default)"

    `score.py judges` runs the judges and, if `thresholds` is set, checks them at the end
    of scoring — so a normal `/eval-run` already gates. It prints `REGRESSIONS: 0` or
    `REGRESSIONS: N detected` (one line per breach) before exiting.

    ```bash
    python3 skills/eval-run/scripts/score.py judges \
      --run-id "$RUN_ID" --config eval.yaml
    ```

=== "Standalone gate"

    `score.py regression` re-checks the thresholds against an already-scored run's
    `summary.yaml` (run the judges first). Useful as an explicit, self-documenting CI
    step separate from scoring.

    ```bash
    python3 skills/eval-run/scripts/score.py regression \
      --run-id "$RUN_ID" --config eval.yaml
    ```

Runs live under `$AGENT_EVAL_RUNS_DIR/<eval-name>/<run-id>/` (default `eval/runs/`), so
pass a stable `--run-id` (e.g. the commit SHA) to find the same directory later in the job.

## Comparing against a baseline

Beyond absolute floors, you can gate a run **relative to a previous one** with
`--baseline <run-id>`. The baseline must be a prior run under the same eval-name.

- **`/eval-run --baseline <run-id>`** adds a position-swapped pairwise judge on top of
  the regular judges. Each case is judged both A/B and B/A, and only a *consistent*
  preference counts as a win; `summary.yaml` gains a `pairwise` section (`wins_a`,
  `wins_b`, `ties`). Gate it with a `min_win_rate` threshold.
- **`score.py regression --baseline <run-id>`** also does a direct aggregate comparison:
  for `mean` and `pass_rate`, a current value more than `0.5` below the baseline is
  flagged as `Degraded vs baseline` — catching drift even where no absolute floor was
  crossed. A comparable baseline also arms the
  [incomplete evidence](../reference/runs-directory.md#incomplete-evidence) gate for
  that judge, so a run missing evidence cannot clear the baseline on its survivors.

```bash
# Score this run with a pairwise comparison against last week's baseline,
# then gate both the absolute thresholds and the vs-baseline deltas.
python3 skills/eval-run/scripts/score.py pairwise \
  --run-id "$RUN_ID" --baseline 2026-07-09-opus --config eval.yaml
python3 skills/eval-run/scripts/score.py regression \
  --run-id "$RUN_ID" --baseline 2026-07-09-opus --config eval.yaml
```

!!! tip "Absolute vs relative gates"
    Use `min_mean` / `min_pass_rate` for a hard quality floor that must always hold, and
    `--baseline` for catch-any-drift protection between a known-good run and the PR under
    test. They compose — a run can pass its absolute floors yet still fail because it
    dropped sharply versus the baseline.

## Gating a model or config comparison

`thresholds` and `--baseline` judge **one run**. When a job fans `/eval-run` out across
several models or configs and analyzes them with
[`/eval-anova`](eval-anova.md#re-analyze-existing-runs) (`--analyze-only` over the runs
directory), the question becomes *"is the candidate significantly worse than the
baseline?"* — and the orchestrator will not answer it with its exit code. After a
completed analysis `orchestrate.py` **exits `0` whether or not anything was
significant** (it exits non-zero only when it cannot analyze at all: no scored runs, or
the `anova` extra missing). The gate has to be a step of your own over `anova.json`.

The relevant part of the artifact is the level-contrast block for the factor you are
comparing on — `contrasts.<factor>.pairs[]`, each with `a`, `b`, `estimate` (`a − b` on
the composite scale, so its sign depends on which level sorted first), `p_adjusted`, and
`significant` (judged on the adjusted p under the block's `correction`):

```bash title="gate-anova.sh"
#!/usr/bin/env bash
# usage: gate-anova.sh anova.json FACTOR CANDIDATE BASELINE
set -euo pipefail
verdict=$(jq -r --arg f "$2" --arg cand "$3" --arg base "$4" '
  (.contrasts[$f].pairs // [])
  | map(select(([.a, .b] | sort) == ([$cand, $base] | sort)))
  | if length == 0 then "missing: no \($f) contrast for \($cand) vs \($base)"
    elif .[0].p_adjusted == null then "no-evidence: \(.[0].reason // "no finite p")"
    else .[0]
      | (if .a == $cand then .estimate else -(.estimate) end) as $delta   # candidate − baseline
      | if .significant and $delta < 0 then
          "regression: \($cand) - \($base) = \($delta) (adj. p \(.p_adjusted))"
        elif .significant then
          "improvement: \($cand) - \($base) = \($delta) (adj. p \(.p_adjusted))"
        else "no-difference: adj. p \(.p_adjusted)" end
    end' "$1")
echo "$verdict"
case "$verdict" in regression:*|missing:*) exit 1 ;; esac
```

```bash
bash gate-anova.sh "$AGENT_EVAL_RUNS_DIR/<eval-name>/anova.json" model "$CANDIDATE_MODEL" "$BASELINE_MODEL"
```

Three semantics are baked in deliberately:

- **Fail only on a *significant* loss.** The step exits `1` when the pair is
  `significant: true` *and* the candidate-minus-baseline estimate is negative (the sign
  is flipped when the candidate happens to be `b`). A significant gain and a
  non-significant gap both exit `0`.
- **A `null` p is "no evidence", not a pass.** A degenerate pair (zero-variance paired
  differences, for instance) carries `p_adjusted: null`, `significant: false`, and a
  `reason`. The step prints `no-evidence:` and exits `0` so the build is not blocked on a
  statistic that does not exist — but nothing has been shown either way, which is why the
  next point exists. If your policy is "the comparison must be testable", add
  `no-evidence:*` to the failing patterns.
- **Non-significance ≠ equivalence.** `no-difference` means *this sample could not tell
  them apart*, not that the candidate is as good. Keep `thresholds` as the hard floor —
  run `score.py regression` on the candidate's own run before this step — so a candidate
  can never ship on a sweep too small to detect its regression.

Two refinements, both read from the same artifact:

- **Omnibus first, if you want it.** The factor's adjusted omnibus p is
  `.anova.p_adjusted` — a scalar for a single-factor analysis, a dict keyed by term for a
  grid: `jq -r --arg f model '.anova.p_adjusted | if type == "object" then .[$f] else . end'`.
  The contrast gate above does not require it; the contrasts are computed regardless.
- **Mind the contrast type on a grid.** With interactions in the model (two or more
  factors), `contrasts.model` is flagged `contrast_type: reference-cell` — the model gap
  *at the other factors' reference levels*, not a marginal mean. Gate on a single-factor
  comparison (only `model` varies) when you want the marginal reading, or re-analyze the
  subset of runs for one level of the other factor.

Pin the correction on the command line in CI (`--analyze-only --correction holm`) so an
edit to `matrix.analysis.correction` cannot silently change what "significant" means to
the gate; the `--correction` flag takes precedence over the config.

## A GitHub Actions job

!!! warning "Illustrative skeleton — not drop-in runnable"
    The `score.py regression` step below is the real, copy-pasteable **gate**
    (exit `1` fails the job). The run step is **pseudocode**: `/eval-run` is a
    Claude Code slash command, not a binary on `PATH`. Wire
    [Claude Code headless](headless.md) or [Harbor](harbor.md) before using this
    in a real repo.

```yaml title=".github/workflows/eval.yml (pseudocode)"
name: skill-eval
on: [pull_request]

jobs:
  eval:
    runs-on: ubuntu-latest
    env:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      AGENT_EVAL_RUNS_DIR: eval/runs
      RUN_ID: ci-${{ github.sha }}
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install the harness
        run: pip install -e .

      # PSEUDOCODE — replace with a real driver (not a bare /eval-run shell call).
      # Options: Claude Code headless (guides/headless.md) or Harbor
      # (`/eval-run --runner harbor …`). Must produce:
      #   eval/runs/<eval-name>/$RUN_ID/summary.yaml
      - name: Run the eval
        run: |
          your-headless-or-harbor-driver \
            --run-id "$RUN_ID" \
            --model sonnet

      # Gate: exits 1 on any threshold breach, failing the job.
      - name: Check for regressions
        run: |
          python3 skills/eval-run/scripts/score.py regression \
            --run-id "$RUN_ID" --config eval.yaml

      - name: Upload report
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: eval-report
          path: eval/runs/**/report.html
```

!!! note "Driving the run in CI"
    Replace `your-headless-or-harbor-driver` with [Claude Code headless](headless.md)
    (or an equivalent driver that prepares workspaces, executes cases, and scores).
    For heavier or containerized CI, use the same `eval.yaml` on [Harbor](harbor.md)
    with `--runner harbor` — the config is unchanged; only the substrate flag
    differs.

## OpenRouter in CI

A run whose skill model is `openrouter:/…` ([Running on OpenRouter](openrouter.md)) needs
three more things from the job:

- **The key as a secret.** Export `OPENROUTER_API_KEY` from the CI secret store, and
  `OPENROUTER_MANAGEMENT_KEY` as well at `enforcement: key-guardrail`. Neither may be
  authored in `eval.yaml` or on an `env:` surface — the loader rejects it. Anthropic
  judges still need their own credentials; the agent under a plan does not.
- **Two extra gates.** `execute.py` takes `--strict-cost` and `--strict-routing`. Both
  turn a run that would otherwise exit `0` into exit `2` (a run that already failed keeps
  its own code), and both are no-ops without a provider plan. `--strict-cost` fails on
  `cost_source: unavailable` or `budget.exceeded: run`; `--strict-routing` fails on
  routing violations or `audit_complete: false`. The reason is printed as `STRICT: …` on
  stderr and persisted in `run_result.json` as `strict_failures`, with `exit_code`
  rewritten to `2`. `--allow-estimate` is the opposite knob — it records the runner
  estimate as `runner:estimate` instead of `unavailable` — and belongs to offline
  replays, not to CI.
- **A null cost is unknown, not zero.** Without `--strict-cost` an unreconciled run still
  exits `0`: `execute.py` prints `COST: unavailable`, the reconcile step warns
  `cost_source unavailable — no /generation row and no key-usage delta landed`, and the
  report's **Cost Provenance** panel opens with a red banner ("Cost unavailable: no
  /generation row and no key-usage delta landed — cost_usd is null (the runner estimate is
  not spend)"). A job that reads `cost_usd` for a spend budget must treat `null` as
  unknown.

```yaml title=".github/workflows/eval.yml (excerpt)"
    env:
      OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}     # the inference key
      # OPENROUTER_MANAGEMENT_KEY: ${{ secrets.OPENROUTER_MANAGEMENT_KEY }}  # key-guardrail only
```

The threshold gate exits `1` and the strict flags exit `2`; a CI step fails on either.
`--strict-cost` / `--strict-routing` are flags of `execute.py`, listed with the other
`/eval-run` flags in [eval-run → Flags](eval-run.md#flags).

## Where to go next

<div class="grid cards" markdown>

-   :material-gate: **Threshold semantics**

    ---

    The full reference for `min_mean`, `min_pass_rate`, `min_win_rate`, and `max_error_rate`.

    [:octicons-arrow-right-24: thresholds reference](../reference/config/thresholds.md)

-   :material-scale-balance: **Pairwise & sampling**

    ---

    How `--baseline` comparisons and repeated-sample stability work.

    [:octicons-arrow-right-24: pairwise & sampling](../concepts/pairwise-and-sampling.md)

-   :material-console: **Run headless**

    ---

    Auto-answer questions and gate external services for unattended runs.

    [:octicons-arrow-right-24: running headless](headless.md)

</div>
