---
name: eval-compare
description: "Compare evaluation results across multiple models or runs. Takes a directory of eval run artifacts (summary.yaml, run_result.json, HTML reports) and produces a tabbed HTML comparison report with model cards, quality/cost tables, per-case breakdowns, and embedded original reports. Use when the user wants to compare models, compare runs, produce a model comparison report, or analyze eval results side-by-side."
user-invocable: true
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, AskUserQuestion
---

You are an eval comparison report generator. You take a directory of eval run results and produce a self-contained HTML comparison report with LLM-generated analysis. You do not run evaluations or modify source data.

**IMPORTANT: Follow the steps below sequentially. Do not explore the filesystem, run `ls`, `find`, or otherwise investigate the input directory. The scripts handle all discovery. Just run the commands as written.**

## Step 0: Parse Arguments

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `<input-dir>` | yes | — | Directory to scan recursively for eval runs (any subdirectory containing `summary.yaml`; cap-evolve rollouts are skipped unless this is their `.optimize` root) |
| `--output <path>` | no | `<input-dir>/comparison-report` | Output directory for the HTML report |
| `--title <text>` | no | `Model Comparison` | Report title |
| `--overview <text>` | no | none (section omitted) | Context paragraph shown at the top of the report |

## Step 1: Discover Runs

Run the discovery script to find all valid eval runs:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/compare.py discover <input-dir>
```

This recursively scans for directories containing `summary.yaml` and prints a JSON manifest of discovered runs with model names, costs, and judge scores. Just pass the input directory — do not search for files yourself. If the input dir also contains an `anova.json` (written by `/eval-anova`), the manifest reports `"has_stats": true` and the generated report gains an ANOVA/Pareto **Statistical Significance** section automatically — no extra step needed. eval-compare works with or without it.

If no valid runs are found, report the error and stop.

## Step 2: Generate Report

Run the report generator:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/compare.py generate <input-dir> --output <output-dir> --title "<title>"
```

If `--overview` was provided, also pass `--overview "<text>"`.

This produces:
- `<output-dir>/index.html` — the comparison report
- Copies of any `report.html` files into per-run subdirectories (named by a unique run slug) for iframe embedding

## Step 3: Write Analysis Sections

Using the discovery JSON from Step 1, read each run's `summary.yaml` (paths are in the `"dir"` field of each discovered run) to understand the full picture — per-case scores, judge breakdowns, cost data, variance across runs. Then edit the generated `<output-dir>/index.html` to replace placeholder content and add badges.

### Model Card Badges

Add a badge `<div>` to model cards based on your analysis. Not every model needs a badge — only add one when it clearly applies. Available badge styles:

- **Best Value** (green): The best model considering both quality and cost. A model with slightly higher quality but much higher cost is NOT the best value — the cheaper model wins unless the quality difference is substantial. Must also be consistent across runs.
  `<div class="badge" style="background: var(--green); color: #000;">Best Value</div>`
- **Highly Variable** (yellow): A model with multiple runs whose scores diverge significantly across runs (e.g., same case scoring 1 in one run and 4 in another). A highly variable model should NEVER also be Best Value — inconsistency disqualifies it.
  `<div class="badge" style="background: var(--yellow); color: #000;">Highly Variable</div>`
- **Not Viable** (red): A model that fundamentally fails the task — very low scores, missing outputs, can't invoke the skill reliably.
  `<div class="badge" style="background: var(--red); color: #000;">Not Viable</div>`

Insert the badge `<div>` right after the opening `<div class="card"...>` tag. Also add `style="border-color: var(--green);"` (or `--yellow`/`--red`) to the card's outer div.

### Bottom Line (`<div class="verdict">`)

Replace the placeholder with a concise summary — 3 sentences maximum. State which model is the best choice and why, note any models that aren't viable, and call out the most important tradeoff. Do not list every model individually. Write for someone who hasn't seen the raw data. Use `<p>` tags.

### Where Each Model Shined (`<div id="model-strengths">`)

Replace the placeholder `<p>` with per-model subsections. For each model, write a `<h3>` with the model name and 2-3 sentences highlighting its unique strengths. What cases did it handle best? What patterns show it excelling? What is it uniquely good at compared to the others?

### Shared Weaknesses (`<div id="shared-weaknesses">`)

Replace the placeholder `<p>` with an HTML table (Issue | Impact | Affected Cases) listing weaknesses that appear across ALL or most models. Look at per-case scores to find cases where every model scored poorly or failed. These are skill-level issues, not model-level.

### Recommendations (`<div id="recommendations">`)

Replace the placeholder `<p>` with 3-5 actionable bullet points (`<ul><li>`). Cover: which model to use for production, when alternatives make sense, and what to improve in the skill itself based on the shared weaknesses.

## Step 4: Present Summary

Show the user:
- Number of runs discovered, grouped by model
- Best value model and why
- Where the report was saved

Suggest opening the report:
```bash
open <output-dir>/index.html
```

## Rules

- **Read-only on source data.** Never modify input files. Only write to the output directory.
- **Graceful degradation.** If a run is missing `run_result.json`, still include it with available data from `summary.yaml`. If an HTML report is missing, the run's tab shows a "No HTML report available" message instead of an iframe.
- **Aggregate repeated models.** When multiple runs use the same model, show averages with min/max ranges. Each run still gets its own iframe tab.
- **Highlight best/worst.** In comparison tables, mark the best value green and worst value red for each metric.

$ARGUMENTS
