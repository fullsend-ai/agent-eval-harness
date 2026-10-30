# Statistically validated optimization (cap-evolve)

Pass `--engine capevolve` to `/eval-optimize` to use
[cap-evolve](https://github.com/skillberry-ai/cap-evolve) instead of the default
interactive loop: a train/val/test split, a significance gate on val, and a sealed
test score computed exactly once. This is the harness's recommended optimization
path for anything you'd act on — prefer it over the default loop (documented at
[Optimize a skill](eval-optimize.md)) once it's worth the one-time setup.

!!! abstract "Why this instead of the interactive loop"
    The interactive `/eval-optimize` loop edits a skill, re-runs, and reports whether
    judges pass — on however many cases you give it, with no protection against
    noise or overfitting. cap-evolve splits your dataset, evaluates a candidate edit
    on a held-out validation split, and accepts it only when the improvement clears
    `k_se` standard errors — a change too small to trust comes back **indecisive**,
    not "improved" or "regressed". The sealed test split is scored once, at the end,
    so the number you report was never visible during the search.

## Install

```bash
/eval-setup --optimize
```

This installs `cap-evolve-core` into the harness's own venv and fetches its skills
tree (phases, algorithms, templates) at a pinned commit — nothing to check out
yourself. Needs Python 3.11+ in the harness venv; `/eval-setup --optimize` rebuilds
it with a newer interpreter if needed.

## Quickstart

```bash
# See what would happen — no spend, no launch
/eval-optimize --engine capevolve --dry-run

# Launch a campaign, $25 hard cap, up to 10 improvement iterations
/eval-optimize --engine capevolve --max-usd 25

# From any later session (the campaign runs detached from the one that launched it)
/eval-optimize --engine capevolve --status

# Stop it (signals the whole process group — rollouts included, not just the launcher)
/eval-optimize --engine capevolve --stop

# Once finished and a candidate was accepted: apply the winner onto a new git branch
/eval-optimize --engine capevolve --apply
```

`--dry-run` mines your benchmark (the eval config's `execution.skill`/`execution.prompt`,
dataset, judges) for everything cap-evolve needs — it never edits your eval config.
If your `eval.yaml` has no `optimize:`/`reward:` block, intake proposes sensible
defaults (the skill chain as the candidate, every judge weighted equally, no
pass/fail gate) into a separate `.capevolve/eval.optimize.yaml` overlay, and tells
you what it chose. Edit the overlay if you want something different; `--reintake`
regenerates it.

## Watching a campaign: the dashboard

A launched campaign writes its own run directory under `.capevolve/run_<timestamp>/`.
To watch it live rather than polling `--status`:

```bash
# One-time: the dashboard backend needs a source install
pip install -e <cap-evolve checkout>/dashboard/backend

cap-evolve dashboard --base .capevolve
```

This opens a local server reading every `run_*` directory under `.capevolve/` and
streams live progress (iteration-by-iteration accept/reject/indecisive, cost,
timing, a tasks × iterations pass/fail heatmap, candidate diffs and lineage). If you
don't want to install the dashboard backend, a static, shareable snapshot is still
available:

```bash
cap-evolve dashboard --export .capevolve/run_<timestamp>
```

### Reading what the dashboard shows you

- **Accepted / rejected / indecisive** — a candidate is accepted only when it clears
  the significance gate on the validation split; *indecisive* means the gate could
  not distinguish the change from noise at your current sample size, not that the
  change was bad.
- **Coverage** — the fraction of cases that actually produced a measurement. A
  campaign with low coverage (many infra failures or errored judges) needs
  investigation before its accept/reject calls can be trusted — missing data is
  never silently scored as 0.
- **Sealed test** — scored exactly once, after everything else is decided. The
  number you'll want to report. `--apply`'s `caveat` field reminds you how many of
  your total cases were actually seen during the search (train + val), since a full
  `/eval-run` over the whole dataset will look optimistically better than the sealed
  test delta for that reason.
- **Cost and timing** — per-rollout and cumulative, against your `--max-usd` cap.

## Substrates

| `--runner` | What runs | When to use it |
| --- | --- | --- |
| `local` (default) | Each rollout's workspace is staged and executed the same way `/eval-run` does, locally | Most benchmarks |
| `harbor` | Each rollout becomes a private, self-contained Harbor task package (`--env podman\|kubernetes`, `--image <img>`) | Containerized isolation, or an existing Harbor setup |

`--runner evalhub` is refused — EvalHub's architecture doesn't support cap-evolve's
per-rollout fan-out.

## Cost control

`--max-usd` is the campaign's hard total (optimizer + runner spend combined).
`--runner-budget-usd` additionally caps what a *single* rollout may spend — useful if
one case is prone to runaway tool-call loops. `--dry-run` gives you a cost-per-rollout
estimate, calibrated from this eval's own past runs when it has any, before you
commit real spend.

## Stopping and re-applying

`--stop` is the only safe way to end a launched campaign — it signals the whole
process group, so every in-flight rollout stops too. Don't `kill` the printed `pid`
yourself: that only reaches the session leader and leaves rollouts (and any `claude`
CLI children) running and spending in the background.

`--apply` copies the campaign's winning candidate onto a new `capevolve/<run_id>`
branch and returns you to your original branch — it never commits to your current
branch. It requires a clean working tree (campaign-owned directories, `.capevolve/`
and your configured runs dir, don't count as dirty) and a finished campaign with an
accepted candidate (`best_id` other than `seed`). Re-running `--apply` for the same
campaign after a failed attempt is safe — a failed apply cleans up its own branch.

## Troubleshooting

- **`{"error": "setup_required", ...}`** — `cap-evolve-core` isn't installed, or the
  venv's Python is older than 3.11. Run `/eval-setup --optimize`.
- **A campaign shows `measurements.n_scored: 0`** — every rollout failed before
  producing a measurement. Check the campaign's `log_file` and the `opt-*` run
  directories under your configured runs dir for the underlying error; `--apply` is
  refused until a campaign actually measures something.
- **`--apply` refuses with "not clean"** — commit or stash real changes first. If
  you suspect it's the campaign's own byproduct rather than your changes, check that
  your runs dir (`$AGENT_EVAL_RUNS_DIR`, default `eval/runs`) is gitignored.

## Relationship to the rest of the pipeline

A cap-evolve campaign produces a standard harness run directory per rollout
(`run_result.json`, `summary.yaml`, `report.html`, transcripts) — readable by
[`/eval-compare`](eval-compare.md) and [`/eval-mlflow`](eval-mlflow.md) like any other
run. [`/eval-anova`](eval-anova.md) doesn't yet understand a campaign's rollouts as a
matrix (different `condition.json` schema) — run it over ordinary `/eval-run` runs
instead.
