# Statistically validated optimization (cap-evolve)

Pass `--engine cap-evolve` to `/eval-optimize` to use
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

Everything on this page — every command, every number, every screenshot — comes
from one real campaign run (`run_20261106_174921`) against a small toy skill while
writing this guide. Nothing here is simulated.

## What actually happens, in plain words

Think of it like this: you have a skill (a prompt, a SKILL.md, a small tool) and
some test cases with a pass/fail judge. The interactive loop is a human watching an
intern edit and re-test, one case at a time, trusting their gut on whether it got
better. cap-evolve is the same intern, except:

1. It never looks at your **test** split until the very end.
2. It only keeps an edit if the improvement on the cases it *can* see (the **val**
   split) is bigger than what you'd expect from noise alone.
3. At the end, it scores the winner on the cases it never saw, once, and tells you
   honestly whether that held up.

That's the whole idea. The rest of this page is the mechanics and a real example.

## Install

```bash
/eval-setup --optimize
```

This installs `cap-evolve-core` (plus `capevolve-dashboard`, for the live dashboard
every campaign starts by default, and `capevolve-telemetry`, the MLflow observer
backend the generated spec names whenever your eval config has an `mlflow:`
experiment) into the harness's own venv and fetches cap-evolve's skills tree
(phases, algorithms, templates) at a pinned commit — nothing to check out
yourself. MLflow itself comes from the harness's own `[mlflow]` extra. Needs Python 3.11+ in the harness venv;
`/eval-setup --optimize` rebuilds it with a newer interpreter if needed.

## A real end-to-end example

Here's a deliberately tiny, real benchmark: a `greet` skill that writes one
greeting sentence in a requested tone (formal or casual), and a judge that checks
whether the output actually reads as that tone. Six test cases, a 3/2/1
train/val/test split. Nothing about this example is staged — the seed skill was
genuinely imperfect, cap-evolve genuinely found and fixed the gap, and the numbers
below are copy-pasted from the real run.

The eval config:

```yaml
name: greet-eval
execution:
  mode: case
  skill: greet
  arguments: "{prompt}"
runner:
  type: claude-code
models:
  skill: claude-haiku-4-5
  judge: claude-haiku-4-5
dataset:
  path: eval/dataset/cases
judges:
  - name: tone_match
    description: Does the greeting match the requested tone (formal vs casual)?
    check: |
      ann = outputs.get("annotations", {})
      tone, name = ann["tone"], ann["name"]
      text = outputs.get("stdout", "").strip().lower()
      # ... checks the name appears and the right tone-marker words are used
reward:
  judge: tone_match
optimize:
  candidate:
    - {dest: SKILL.md, source: .claude/skills/greet/SKILL.md}
  apply_to: .claude/skills/greet
```

### Step 1 — `--dry-run`: see the plan before spending anything

```bash
/eval-optimize --engine cap-evolve --max-usd 5 --dry-run
```

(Outside Claude Code, activate the harness venv and use
`agent-eval-capevolve --max-usd 5 --dry-run` directly.)

![dry-run output](../assets/images/optimize-capevolve/01-dry-run.png)

This mines the benchmark — the skill, the dataset, the judges — and tells you
exactly what a real campaign would do: a 3/2/1 split, one candidate component
(`SKILL.md`), the models each rollout will use, and a cost estimate. No spend yet,
and your `eval.yaml` is never touched (missing `optimize:`/`reward:` blocks get
proposed into a separate `.capevolve/eval.optimize.yaml` overlay instead). The
`check.notes` line `tasks('val') -> 6 task(s)` isn't a second, bigger val split —
the adapter's `tasks()` always returns the full dataset by design (cap-evolve does
its own splitting on top of it); `estimate.spec_summary.val_tasks` (`2` here) is
the number that actually matters.

### Step 2 — launch a real campaign

```bash
/eval-optimize --engine cap-evolve --max-usd 5 --max-iterations 1 --num-trials 1 \
  --pilot-case-ids case-1 case-3 case-5 --run
```

(`--pilot-case-ids` pins one case per split for a fast, cheap sanity run — the
normal path is just `--max-usd 25` with no pilot flags, over the real split sizes.
The pilot run above is what produced every campaign screenshot below.)

The campaign runs **detached** — it survives you closing the terminal — and
launches the real pipeline: scaffold the project, probe the seed skill on train and
val, hand the failing case to a real coding agent (`claude-code`, by default) as
the optimizer, score its candidate, and — only if it clears the significance gate —
accept it and move to the sealed test.

In our run, the seed skill passed the train case but **failed the val case**: asked
to greet "Dr. Chen" formally, it wrote *"I look forward to our conversation, Dr.
Chen."* — plausible-sounding, but it has no formal greeting opener and reads as
casual small talk. The judge caught it.

### Step 3 — the optimizer reads the failure and proposes a fix

This is the part that's easy to under-explain: a real `claude-code` subprocess
reads the failing case's full feedback (reward, judge outcomes, the harness's
explanation of what's being optimized) and edits `SKILL.md` directly. Here is the
**exact diff it produced**, unedited:

![the optimizer's real diff](../assets/images/optimize-capevolve/04-skill-diff.png)

It added a decision table — must-include/never-include words per tone, with a
worked example for each — covering five tones, not just the two train/val cases it
had seen. That generality is the point: a model deciding its own "must
include"/"never include" criteria per tone is a real description of how to write
in that register. It also pins that one failing case's exact sentence as a
negative example — so the edit is both: a general rule, and a memorised answer to
the prompt it saw. This candidate then re-ran
on the val split and scored a perfect 1.0 — though with a single val case the gate
is in its strict fallback (accept any positive delta), so this is a plumbing
demonstration, not a significance result.

!!! warning "A weaker judge would have bought a worse edit here"
    `tone_match`'s real check is a plain substring match against a short,
    fixed word list — good enough to catch the seed's
    specific failure, but exploitable: an edit that just repeated the judge's own
    marker words verbatim back at it would also have scored 1.0, without any real
    improvement in how the skill reasons about tone. That is reward hacking — the
    optimizer finding the judge's blind spot instead of the task — and a weak judge
    is exactly what makes it possible. The campaign's sealed test score is only
    as trustworthy as the judge measuring it; see "When not to trust a campaign"
    below before you act on a result from a judge you haven't scrutinized this
    closely.

### Step 4 — `--status`: what happened, honestly

```bash
/eval-optimize --engine cap-evolve --status
```

![status output](../assets/images/optimize-capevolve/02-status.png)

`best_id: cand_0001`, one accepted iteration, `best_val: 1.0` versus the seed's
`0.0` on val. The sealed test case scored 1.0 for **both** the winner and the seed
baseline in this particular pilot run — with only one test case, that's expected
variance, not a contradiction. The `warnings` field spells out exactly why: fewer
than 2 validation cases means the significance gate had no way to tell noise from
signal, so what you're looking at is a plumbing check, not a measurement. A full
campaign with real split sizes resolves this.

### Step 5 — `--apply`: land the winner on a branch

```bash
/eval-optimize --engine cap-evolve --apply
```

![apply output](../assets/images/optimize-capevolve/03-apply.png)

This copies the winning `SKILL.md` onto a new `capevolve/run_<id>` git branch and
returns you to your original branch — it never commits to your current branch
directly. The `caveat` field is cap-evolve's own honesty check: it tells you how
many of your total cases were actually seen during the search (train + val), since
a full `/eval-run` over the whole dataset will look *better* than the sealed test
delta for exactly that reason.

### The dashboard — on by default

A launched campaign starts a **live dashboard automatically** (`--dashboard auto`,
the default) at `http://127.0.0.1:7878` and prints the URL. You don't have to ask
for it:

```bash
/eval-optimize --engine cap-evolve --max-usd 25                    # dashboard starts with it
/eval-optimize --engine cap-evolve --max-usd 25 --dashboard off    # explicitly skip it
/eval-optimize --engine cap-evolve --max-usd 25 --dashboard-port 9001
```

`--dry-run` reports whether it can actually launch — `dashboard_available` is
`false` until the optional `capevolve-dashboard` package is installed
(`/eval-setup --optimize` installs it alongside `cap-evolve-core`; a missing
install is a no-op, never a failed campaign — you just get no live view until
then).

**What "the dashboard" gets you depends on what's installed.** The pip package
(`capevolve-dashboard`) is the backend only — a REST API plus the static,
self-contained `dashboard.html` every run writes to its run directory (open it
directly, no server needed: this is the artifact the screenshots below under
"self-contained, no backend" came from). The full live React UI you see in the
other screenshots below is a separate frontend, built with `npm`, that the pip
package doesn't bundle — most users will only ever see the static export, and
that's genuinely enough day to day. If you want the live UI too, build it once
from a cap-evolve checkout at the pinned commit:

```bash
git clone https://github.com/skillberry-ai/cap-evolve.git
cd cap-evolve && git checkout <the commit pyproject.toml pins>
cd dashboard/frontend && npm install && npm run build
export CAPEVOLVE_STATIC_DIR=$(pwd)/dist     # before launching a campaign
```

#### Self-contained, no backend

Every run directory (`.capevolve/run_<timestamp>/dashboard.html`) gets a static
export of the same data — open it straight in a browser, nothing to install:

![the static dashboard.html export](../assets/images/optimize-capevolve/05-dashboard.png)

#### The live UI (with the frontend built)

Here's the real live dashboard — same campaign, with the frontend built as shown
above. The overview tab:

![live dashboard — overview](../assets/images/optimize-capevolve/06-dashboard-overview.png)

The **Candidates** tab — every proposed edit, its verdict, and the exact val delta
that got it accepted or rejected:

![live dashboard — candidates](../assets/images/optimize-capevolve/07-dashboard-candidates.png)

The **Diffs** tab — the optimizer's literal edit, syntax-highlighted, with a
dropdown to compare any candidate against its parent or the seed:

![live dashboard — diffs](../assets/images/optimize-capevolve/08-dashboard-diffs.png)

Reading the overview tab left to right, top to bottom: baseline vs. best val vs.
the delta vs. the sealed test score; verdicts (accept/reject/indecisive), spend,
wall-clock, tokens; a lineage graph (seed → accepted candidates — on a longer
campaign this is where you'd see rejected branches dead-end); a val-score-per-
candidate chart; and one card per pipeline phase (intake → implement & check →
baseline → optimize → finalize → report), each with a one-line summary of what
it did.

## What this is actually for

Reach for `--engine cap-evolve` instead of the interactive loop when:

- You're about to **act on the result** — ship a prompt change, hand a skill to
  other people, report a number to someone — and being wrong costs more than the
  campaign does.
- Your dataset is big enough to split three ways and still have a meaningful val
  size (`--status` and `--apply` both warn you when it isn't: with `val < 2` the
  gate degrades to "any positive delta is accepted", which is a coin flip, not a
  measurement).
- You want a **paper-trail**: every accepted/rejected candidate, its diff, and why,
  recorded in the run directory — useful when someone asks "why did the skill
  change" six months later.

Stick with the interactive `/eval-optimize` loop when you're iterating quickly on a
handful of cases and just want a fast, good-enough fix — the statistical machinery
is overhead you don't need yet.

!!! warning "When not to trust a campaign"
    A campaign's sealed test delta is only as good as its coverage. Watch for:

    - **Too few val cases** (`--status` warns): with `val < 2`, the significance
      gate accepts any positive delta — noise and signal look the same.
    - **High error rate** (rollouts that failed before scoring): missing data
      shrinks the sample the gate sees, making it easier to clear by chance.
    - **A weak judge**: if the judge measures a proxy (substring matching, keyword
      counting) rather than the capability, the optimizer will find and exploit the
      proxy — a perfect val score on a bad judge is reward hacking, not improvement.

    The `--status` output reports measurement coverage and val-size warnings;
    read them before trusting `--apply`.

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
commit real spend — in the example above, real calibration data from the pilot run
immediately sharpened the estimate for the next `--dry-run`.

Budget caps spend, not time: a campaign runs roughly
`|val| × num-trials × (iterations + 1)` rollouts, and by default they are
**strictly serial** — a real campaign is hours of wall-clock regardless of
budget. Pass `--workers N` to let cap-evolve run rollouts N-wide
(`--dry-run` reports the effective setting); wall-clock scales roughly 1/N,
bounded by your provider's rate limits. On the Harbor substrate each worker
runs a container, so `--workers N` means N concurrent containers against one
podman socket — start small there.

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
  directories under `$AGENT_EVAL_RUNS_DIR/.optimize/<eval-name>/` for the underlying
  error; `--apply` is refused until a campaign actually measures something. (This happened once while
  writing this guide — the demo judge's code read a variable, `case_dir`, that
  the harness's inline `check:` scorer never provides; the fix was to read
  `outputs["case_dir"]` or, better, the pre-parsed `outputs["annotations"]` the
  harness already loads from each case's `annotations.yaml`. Worth knowing before
  you write your first judge.)
- **`--apply` refuses with "not clean"** — commit or stash real changes first. If
  you suspect it's the campaign's own byproduct rather than your changes, check that
  your runs dir (`$AGENT_EVAL_RUNS_DIR`, default `eval/runs`) is gitignored.
- **The dashboard URL loads, but looks bare or shows a status that disagrees with
  `--status`'s JSON** — the backend's REST API (`.../api/runs`) is correct either
  way; a blank or stale-looking page means the live React frontend isn't built
  (`CAPEVOLVE_STATIC_DIR` unset, so there's no UI to serve, only the API), or — if
  you did build one yourself — it's from a different commit than the harness's
  pinned one. Rebuild from the exact pinned commit (see "The dashboard" above);
  the self-contained `dashboard.html` in the run directory always matches, since
  cap-evolve generates it itself at the pinned commit's own code.

## Relationship to the rest of the pipeline

A cap-evolve campaign produces a standard harness run directory per rollout
(`run_result.json`, `summary.yaml`, `report.html`, transcripts, a `condition.json`
naming the candidate and case) in a **parallel runs root**:
`$AGENT_EVAL_RUNS_DIR/.optimize/<eval-name>/<run-id>`. A campaign is
`|val| × num_trials × (iterations + 1)` of them, so they are kept out of the
ordinary runs dir — and [`/eval-compare`](eval-compare.md) and
[`/eval-anova`](eval-anova.md) skip them when sweeping a runs dir (including the
flat `opt-*` rollouts that releases up to 1.62.x wrote next to your other runs).

To browse every rollout's own `report.html` (and include them in a comparison),
point `/eval-compare` at the rollout root: `/eval-compare eval/runs/.optimize/<eval-name>`
— it groups by model, so the search *curve* itself is cap-evolve's: `--status`,
the run's `history.jsonl`/`report.md`, or the dashboard. To run a `--run-id` script
on one rollout, name its directory with `--run-dir` instead — `score.py judges`,
`report.py`, `log_results.py` and `attach_feedback.py` all take it (the basename is
the run id; everything else still resolves under your ordinary runs dir):

```bash
python3 skills/eval-mlflow/scripts/log_results.py \
  --run-dir eval/runs/.optimize/<eval-name>/opt-case-3-cand_2-0-1a2b3c4d --config eval.yaml
```

Don't re-point `AGENT_EVAL_RUNS_DIR` at `.optimize` for a session instead: your next
`/eval-run` would land in there (and be skipped as a rollout), and your next campaign
would nest a second `.optimize` inside it. `--status` reports the directory as
`rollouts_dir`. Rollouts a v1.62.x campaign left flat under `eval/runs/<eval-name>/opt-*`
are skipped too; to inspect those, move them once:
`mkdir -p eval/runs/.optimize/<eval-name> && mv eval/runs/<eval-name>/opt-* eval/runs/.optimize/<eval-name>/`.

`/eval-anova` doesn't understand a campaign's rollouts as a matrix (their
`condition.json` records provenance, not factor levels) — run it over ordinary
`/eval-run` runs instead.
