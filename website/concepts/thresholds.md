# Regression thresholds

Thresholds turn judge scores into a **pass/fail gate**. After scoring, the harness
compares each judge's aggregate against the bounds you declare in `eval.yaml` and
**exits non-zero** if any is missed — so a regression fails a CI job instead of quietly
landing in a report.

```yaml title="eval.yaml"
thresholds:
  has_content:    { min_pass_rate: 1.0 }   # boolean judge
  completeness: { min_mean: 3.5 }        # numeric (1–5) judge
  pairwise:       { min_win_rate: 0.6 }     # pairwise comparison
```

Each key under `thresholds` is a **judge name**; each value is a dict of one or more
threshold checks.

## The four threshold keys

Three of the keys are *minimums* — the run passes when the metric is `>=` the value.
`max_error_rate` is the exception: it is a *maximum*, and passes when the error rate is
`<=` the value. Either way a metric exactly equal to its threshold passes. Which key you
use must match the **value type the judge produces**.

| Key | Judge type | Metric compared | Value range |
| --- | --- | --- | --- |
| `min_pass_rate` | Boolean (`return True/False`, `feedback_type: bool`) | Fraction of cases that passed | `0.0`–`1.0` |
| `min_mean` | Numeric (a score on the judge's `score_range`) | Mean score across cases | matches the score scale |
| `min_win_rate` | [Pairwise](pairwise-and-sampling.md) | Win rate vs. a baseline run | `0.0`–`1.0` |
| `max_error_rate` | Any judge | Fraction of cases where the judge errored | `0.0`–`1.0` |

!!! note "How aggregates are derived"
    The harness aggregates each judge across all cases before checking thresholds:

    - **Boolean judges** get a `pass_rate` (fraction of `True`), and `mean` is set to the
      same value. So `min_mean` *also* works on a boolean judge — `0.9` means "90% passed".
    - **Numeric judges** get a `mean`; their `pass_rate` is always `None`.
    - `win_rate` is populated only for a pairwise judge, and only when a baseline
      comparison actually ran.

## Match the key to the judge's value type

This is the most common misconfiguration. A `min_pass_rate` on a numeric judge, or a
`min_mean` on a judge that was skipped for every case, has **no metric to compare** — and
that is *not* silently ignored.

!!! warning "A missing metric is reported AS a regression"
    When a configured threshold's metric is `None` (the judge was skipped for all cases,
    it **errored** on all of them, or the key targets the wrong judge type), the harness
    records it as a regression with an `n/a` value rather than skipping it. The rationale:
    a threshold you asked for but that can never evaluate is a mistake worth surfacing,
    not hiding.

    ```text
    REGRESSIONS: 1 detected
      [completeness] pass_rate: >= 0.9 -> n/a
    ```

    The detail names the cause: *"pass_rate unavailable — judge errored on 12 cases; see
    the per-case rationales"* when every value was rejected (by its `score_range`, say),
    or *"…judge skipped for all cases or not a boolean judge"* when the judge genuinely
    never ran. Fix the first by looking at the rationales, the second by switching to
    `min_mean` for a numeric judge or by loosening an `if:` that skipped every case.

!!! warning "Partial errors shrink the sample — gate them with `max_error_rate`"
    A judge that errors on *some* cases still produces a `mean` — over the survivors only.
    Two off-scale cases out of ten leave `min_mean` gating the remaining eight, and one
    good score with nine errors passes a `min_mean` outright. The report shows `ERROR` for
    the judge only when *no* case produced a value.

    One class of error is exempt: a case where the judge was denied material it
    should have graded fails the run on its own, with no `max_error_rate` set.
    See [incomplete evidence](../reference/runs-directory.md#incomplete-evidence).

    ```yaml
    thresholds:
      completeness:
        min_mean: 3.5
        max_error_rate: 0.2    # ...over at least 80% of the dataset
    ```

    `max_error_rate` is off unless declared, so this is opt-in: one flaky judge run
    should not fail a suite by default.

!!! tip "Unknown keys and unknown judges are silently ignored"
    Thresholds are stored as-is at config load — there is no key-name validation. A typo
    like `min_pass` (instead of `min_pass_rate`) simply does nothing, and a threshold
    naming a judge that doesn't exist in the results is skipped. Only the four keys above
    are honored.

## How detection works

```mermaid
flowchart TD
    A[thresholds: judge -> checks] --> B{judge in results?}
    B -- no --> S[skip]
    B -- yes --> C{check present}
    C --> D[min_pass_rate -> pass_rate]
    C --> E[min_mean -> mean]
    C --> F[min_win_rate -> win_rate]
    C --> EC[max_error_rate -> error_rate]
    D & E & F --> G{metric is None?}
    G -- yes --> R1[regression: n/a]
    G -- no --> H{metric < threshold?}
    H -- yes --> R2[regression]
    H -- no --> P[pass]
    EC --> HE{error_rate > threshold?}
    HE -- yes --> R2
    HE -- no --> P
    R1 & R2 --> X[exit code 1]
```

You can define several checks for one judge; each is evaluated independently and any
failure counts.

## Exit-code gating

Thresholds are enforced in two places, both of which `exit(1)` on any regression:

=== "During a run"

    `/eval-run` (which calls `score.py judges`) checks thresholds at the end of scoring:

    ```text
      REGRESSIONS: 1 detected
        [completeness] mean: >= 3.5 -> 3.1
    ```

    A non-zero exit fails the surrounding CI step. See the [CI guide](../guides/ci.md).

=== "As a standalone check"

    Re-check a completed run's saved `summary.yaml` without re-scoring:

    ```bash
    python3 skills/eval-run/scripts/score.py regression \
      --run-id <id> --config eval.yaml
    ```

    Prints `REGRESSIONS: 0` and exits `0` when clean, or lists each regression and exits
    `1` otherwise.

## Optional baseline comparison

Pass a prior run to also flag **relative degradation**, independent of the absolute
bounds above:

```bash
python3 skills/eval-run/scripts/score.py regression \
  --run-id <id> --baseline <prior-id> --config eval.yaml
```

For each judge present in both runs, the current `mean` and `pass_rate` are compared to
the baseline's. A drop of **more than 0.5** (absolute) is reported as a
`<metric>_vs_baseline` regression. Because that comparison gates on a score, it also
arms the [incomplete evidence](../reference/runs-directory.md#incomplete-evidence) gate
for those judges — a run that withheld evidence cannot clear a baseline on its
survivors either:

```text
  [completeness] mean_vs_baseline: 4.2 -> 3.4   Degraded vs baseline
```

!!! note "0.5 is a fixed absolute delta"
    The baseline tolerance is hard-coded, not configurable per judge. It catches a real
    slide (a full half-point on a 1–5 mean, or 50 percentage points on a rate) while
    absorbing normal judge noise. Use `min_mean` / `min_pass_rate` for the absolute floor
    and the baseline for drift detection.

## See also

<div class="grid cards" markdown>

- [**thresholds reference**](../reference/config/thresholds.md) — every field and its schema
- [**Judges**](judges.md) — the value types thresholds gate on
- [**Pairwise & sampling**](pairwise-and-sampling.md) — where `win_rate` comes from
- [**CI integration**](../guides/ci.md) — turning exit codes into build gates
- [**Reward API**](reward-api.md) — collapsing judges into a single RL scalar instead

</div>
