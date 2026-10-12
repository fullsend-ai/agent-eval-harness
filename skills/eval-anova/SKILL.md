---
name: eval-anova
description: Run Design-of-Experiments (DoE) evaluations with ANOVA over a matrix of agent configurations — comparing models, thinking-effort levels, prompts, or other factors across shared test cases, with repeated-measures / mixed-effects statistics that account for case difficulty plus a cost/quality Pareto view. Use whenever the user wants to compare models or configurations on an eval, decide which model or config is best, sweep or grid factors, run replications, or check whether a difference in eval scores is statistically significant (F, p, effect size) — even if they don't say "ANOVA" or "DoE". Also use when an eval.yaml has a matrix block or the user asks to fan an eval out across configurations.
---

# eval-anova

Run a full-factorial experiment comparing agent configurations (models, effort levels, prompts) across shared test cases, then analyze results with repeated-measures ANOVA.

## Usage

```
python3 ${CLAUDE_SKILL_DIR}/scripts/orchestrate.py --config eval.yaml               # run → analyze → report
python3 ${CLAUDE_SKILL_DIR}/scripts/orchestrate.py --config eval.yaml --dry-run     # design + cost estimate, no execution
python3 ${CLAUDE_SKILL_DIR}/scripts/orchestrate.py --config eval.yaml --analyze-only  # re-analyze existing runs + re-render
```

New to this skill? See [QUICKSTART.md](QUICKSTART.md) for from-scratch setup and run steps, and [`eval/anova-example/`](../../eval/anova-example/) for a self-contained worked example (with committed sample runs you can analyze offline).

## How it works

eval-anova is **not** its own executor — it wraps `/eval-run` in a matrix loop:

- **eval-run** stays the single-condition primitive (one model/config → one run with a
  `summary.yaml`). eval-anova runs it **once per matrix cell** (condition × replication), so every
  cell is a standard run under `$AGENT_EVAL_RUNS_DIR/<eval-name>/`, tagged with a `condition.json`
  recording its factor levels.
- **Statistics** are computed over those runs' `summary.yaml` files (`analyze.py` →
  `anova.json`): each case's composite uses the harness's canonical reward composition (the
  eval.yaml `reward:` section, else boolean-gate + normalised-numeric average), then
  repeated-measures / mixed-effects ANOVA + a cost/quality Pareto frontier.
- **The report** is `/eval-compare`, which eval-anova invokes over the runs. eval-compare surfaces
  the ANOVA/Pareto stats automatically when it finds `anova.json`, and stays a standalone
  descriptive comparison when it does not.

Because the stats read standard `summary.yaml` runs, you can also analyze runs produced elsewhere
(e.g. a CI fan-out that runs `/eval-run` per model) — just point `--analyze-only` at their
directory.

## Prerequisites

Install ANOVA dependencies:

```bash
pip install -e ".[anova]"
```

Results archival is optional — set `RHAI_RESULTS_REPO=/path/to/results` to archive experiments to a
git repo (a per-user temp dir is used as a fallback when unset).

## Workflow

1. **Design**: Define factors and levels in your eval YAML's `matrix:` section (`--dry-run` prints
   the grid + a cost estimate).
2. **Execute**: For each condition × replication, drive `/eval-run` (workspace → execute → collect
   → score) → one standard run + `summary.yaml`, tagged with `condition.json`.
3. **Analyze**: Repeated-measures / mixed-effects ANOVA + cost/quality Pareto over the runs →
   `anova.json` (`--analyze-only` runs just this over existing runs). Multi-factor designs get
   one joint Wald test per model term — main effects *and* interactions — and the whole term
   family is then corrected for multiplicity: Holm by default, Benjamini-Hochberg via
   `--correction bh` (alias `fdr_bh`; or `matrix.analysis.correction`), or `none` to skip. Raw and adjusted
   p-values are both reported; `significant` is judged on the adjusted value. Degenerate terms
   (no finite p) are excluded from the family and listed, never given a fabricated p-value.
   Optionally fan the same ANOVA out **per judge** (`--per-judge` or
   `matrix.analysis.per_judge: true`): each judge's per-case values (booleans as 0/1; pairwise
   and errored samples skipped) get the same analysis, and every (judge, term) p-value is
   Benjamini-Hochberg-corrected as *one* family — a screen for which judge moves, while the
   composite keeps its own separate correction family and stays the headline. Judges with
   degenerate designs (constant values, too few crossed cases) are excluded with a reason.

   `anova.json` also carries post-hoc **pairwise level contrasts** per factor (`contrasts` key):
   every level pair gets an estimate (composite-scale difference), SE, raw p, and a p adjusted
   within that factor's family — so a significant omnibus becomes actionable ("opus vs sonnet:
   +0.06, adjusted p = .02"). Computed regardless of the omnibus result — coefficient contrasts on
   the already-fitted mixed model (no refitting) for multi-factor designs, paired tests across
   cases for single-factor ones; with interactions they are reference-cell contrasts, labelled as such.
4. **Report**: `/eval-compare` renders the cross-condition comparison, including the statistics
   section, from the runs + `anova.json`.

## Reports

The orchestrator invokes `/eval-compare` automatically. To (re-)render from existing artifacts:

```bash
# Comparison report (leaderboard, heatmap, + the ANOVA/Pareto stats section if anova.json exists):
python3 ${CLAUDE_PLUGIN_ROOT}/skills/eval-compare/scripts/compare.py generate $AGENT_EVAL_RUNS_DIR/<eval-name>

# Statistics-forward deep view for one experiment (condition means, F / p / η², per-case matrix):
python3 ${CLAUDE_SKILL_DIR}/scripts/report.py $AGENT_EVAL_RUNS_DIR/<eval-name>   # reads anova.json
```

Both read only on-disk artifacts (`summary.yaml` / `anova.json`) and never re-run the experiment.

## Matrix Configuration

Add a `matrix:` key to your eval YAML:

```yaml
matrix:
  factors:
    model:
      - claude-opus-4-8
      - claude-sonnet-4-6
    effort:
      - low
      - high
  replications: 3
```

See `references/matrix-schema.md` for the full schema.

## Statistical Methods

- **Repeated-measures ANOVA** (default): Accounts for case difficulty as a blocking factor. Correct when the same cases are evaluated under all conditions.
- **Mixed-effects model**: For multi-factor designs with crossed random effects. Reports one joint Wald test per term (main effects and interactions), Holm/BH-corrected across the term family.
- **Pairwise level contrasts (post-hoc)**: For every factor with ≥2 levels, all level pairs with estimate/SE/raw p/adjusted p, corrected within the factor by the configured method (`--correction` / `matrix.analysis.correction`: Holm by default, `bh`, or `none`). From the fitted model (mixedlm coefficient contrasts — reference-cell when interactions are present) or paired pingouin tests (single factor).
- **One-way ANOVA**: available in the stats library for independent samples (cases NOT reused), but rarely appropriate — the orchestrator does not auto-select it.

See `prompts/interpret-anova.md` for guidance on interpreting results.
