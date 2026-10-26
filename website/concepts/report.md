# The HTML report

Every `/eval-run` writes a single self-contained `report.html` to the run
directory. It has no external assets — CSS, JavaScript, and every image are
inlined (images as base64 data URIs) — so you can email it, drop it in a PR,
or open it offline and everything still works.

!!! abstract "Where it lives"
    `eval/runs/<eval-name>/<run-id>/report.html`. See the
    [runs directory](../reference/runs-directory.md) for the full layout.

This page covers the report's internals. For a first-time walkthrough of *what
to look at*, see [reading the report](../get-started/reading-the-report.md).

## How it's generated

The report is rendered by `skills/eval-run/scripts/report.py` from artifacts
`/eval-run` already produced — `summary.yaml`, `run_result.json`, the collected
`cases/` tree, and optionally `analysis.md` and `review.yaml`. You rarely call
it directly, but you can regenerate a report (e.g. to add a baseline) without
re-running the eval:

```bash
python3 skills/eval-run/scripts/report.py \
  --run-id <id> \
  --config eval.yaml \
  --baseline <prior-run-id> \   # optional A/B comparison
  --open                        # optional: open in the browser
```

| Flag | Effect |
| --- | --- |
| `--run-id` | Run to render (required). A single path segment — no `/`. |
| `--config` | Path to `eval.yaml` (required). |
| `--baseline` | Prior run ID; adds deltas, a pairwise row, and per-case diffs. |
| `--open` | Open the finished report in your default browser. |

## Structure

Sections render top-to-bottom in this order; empty sections are omitted.

```mermaid
flowchart TD
    H[Header: skill / run / baseline / date] --> RC[Run Configuration + Cost Provenance + Model Usage]
    RC --> AN[Analysis — from analysis.md, if present]
    AN --> SS[Scoring Summary + stability bars]
    SS --> RG[Regressions — only if a threshold failed]
    RG --> SO[Shared Outputs — batch_pattern '*']
    SO --> RO["Per-Case Reward Overview — only with a reward config"]
    RO --> PC[Per-Case Details: rationale, artifacts, diffs]
```

| Section | Source | Notes |
| --- | --- | --- |
| Run Configuration | `run_result.json` + `summary.yaml` | Model, effort, agent, runner provenance rows for the fullsend runner (fullsend/OpenShell versions, sandbox image, harness commit; shown only when recorded), duration, cost, turns, exit code, then **Judge Cost** / **Total Cost** rows from `summary.yaml` (`judge_usage`, `total_cost_source`), plus a **Model Usage** table (per-model tokens, cache hit rate, cost/turn, cost/Mtok). |
| Cost Provenance | `run_result.json` | Between the configuration grid and Model Usage; rendered only when the run-level `run_result.json` carries a `cost_source` — a batch-mode `claude-code` run or any provider-plan run; absent in case mode without a plan. One **Cost source** row for a batch-mode Anthropic run; for a provider-routed run, the full provenance and routing-audit record with red/amber banners. See [below](#cost-provenance-and-routing-audit). |
| Analysis | `analysis.md` | The agent's recommendation, rendered as markdown in a highlighted callout. Optional YAML frontmatter (`agent`, `model`, `date`) drives the subtitle. |
| Scoring Summary | `summary.yaml` + `thresholds` | One row per judge: type, metric (`pass_rate` or `mean`), value, threshold, PASS/FAIL/SKIP/ERROR. Pairwise gets its own row. |
| Regressions | thresholds | Only appears when a judge is below its [threshold](../concepts/thresholds.md). |
| Per-Case Reward Overview | `summary.yaml` + `reward` | Compact matrix of the [reward](../concepts/reward-api.md) and every judge score per case. **Rendered only when a non-empty `reward:` block is configured.** |
| Per-Case Details | `cases/` tree | Judge rationales, inputs, rendered output artifacts, and baseline diffs — one collapsible card per case. |

## Cost provenance and routing audit

A **Cost Provenance** panel is drawn from the run-level `run_result.json` whenever
it carries a `cost_source`. Two kinds of run do: a **batch-mode** run of the
`claude-code` runner, whose runner label (`runner:reported` on the Anthropic API or
Vertex, `runner:estimate` behind an `ANTHROPIC_BASE_URL` gateway) is written to that
file, and every run under a **provider plan** on any backend — Local, Harbor or
EvalHub — where reconciliation writes the label. In the default case mode without a
plan the runner's label lives only in `cases/<id>/run_result.json`, which the report
does not read, so there is no panel; nor is there one on Harbor or EvalHub without a
plan, or for the other runners, which write no `cost_source`. Which rows appear
depends on what was written:

| Row | Present when | Source field |
| --- | --- | --- |
| Cost source | always (the panel's own condition) | `cost_source` |
| Cost confidence — `priced / total` requests, unpriced count | reconciled under a plan | `cost_confidence`, `cost_coverage` |
| Cost (runner estimate), with its `×` factor over real spend | `cost_usd_estimate` is set and differs from `cost_usd` | `cost_usd_estimate` |
| Key-usage cross-check | a key-usage delta was recorded | `cost_coverage.key_usage_delta_usd` |
| Hook cost | hook spend > 0 | `hook_cost_usd` |
| Budget — invocation cap, CLI cap, run pool, enforcement, `EXCEEDED` | a plan | `budget` |
| Key — scope, hash, "exposed to the agent" | a plan | `provider` |
| Enforcement, Declared pins, Providers served, Audit (`compliant / audited`, violations, unattributed, incomplete), Snapshot | a plan | `routing` |

Banners render above the grid for the loud states: red for `cost_source:
unavailable`, a degraded routing audit, an incomplete audit (unattributed
generations) and a budget exceeded; amber for routing violations under
`policy: warn`. `cost_warnings` are listed under the grid. The panel describes
the current run only; with `--baseline`, the configuration grid above it — the
**Judge Cost** / **Total Cost** rows included — shows the baseline value next to
the current one wherever they differ.

Those two rows come from `summary.yaml`, not `run_result.json`. **Judge Cost**
appears when `judge_usage` was recorded (any LLM-backed judge call). Its figure,
`judge_cost_usd`, is the sum over the priced calls — Anthropic and OpenAI SDK
judges report tokens only, OpenRouter judges carry `usage.cost` — and reads `n/a`
only when no call was priced. With judges on several providers it is therefore a
partial figure: the `N unpriced` count next to it (`requests_missing_cost`) says how
many calls it misses. **Total Cost** appears whenever `total_cost_source` is not
`none` — the agent or the judge spend is numeric — and shows a figure only for
`complete`, meaning both addends were numeric (not that every judge call was
priced); otherwise `n/a (agent-only | judge-only)`. Field semantics are in the
[runs directory](../reference/runs-directory.md#cost-provenance) and
[`summary.yaml`](../reference/runs-directory.md#summaryyaml); the provider
background is on [Model providers](../concepts/providers.md#cost-provenance-across-providers).

## Scoring summary and per-case rationale

The **Scoring Summary** aggregates each judge across all cases (boolean judges →
`pass_rate`, numeric judges → `mean`) and marks status against its threshold.

**Per-Case Details** expands each case into a judge table with the full
`rationale`. Rationales are rendered as [markdown](#markdown-rendering), so
lists, tables, `code`, and **emphasis** from the judge come through formatted.

!!! tip "Tabbed rationales for sampled judges"
    When a judge ran with `--samples N > 1`, each sample's rationale is shown in
    its own tab (`#1`, `#2`, …), labelled with that sample's score. This lets
    you see *why* a wobbly judge disagreed with itself, not just that it did.
    See [pairwise and sampling](../concepts/pairwise-and-sampling.md).

## Visual artifact rendering

Files collected under your `outputs` paths are rendered inline by type, not just
dumped as text. Visual artifacts sort to the top of each case's **Output files**.

| Artifact | Rendered as | How |
| --- | --- | --- |
| `.png` `.jpg` `.jpeg` `.gif` `.webp` `.svg` | Inline image | base64 data URI |
| `.d2` | SVG | `d2 --bundle --layout elk` |
| `.drawio` | SVG | draw.io CLI (`-x -f svg`); `draw.io.app` on macOS, `drawio` elsewhere |
| graph JSON (`outputs[].types: graph`) | SVG | converted to D2, then rendered via ELK |
| metrics JSON (`outputs[].types: metrics`) | Two-column table | parsed key/value |
| `.html` | Sandboxed `<iframe>` | inlined via `srcdoc`, auto-sized |
| anything else | `<pre>` text (truncated to 200 lines) | — |

Diagrams (D2 and drawio) are laid out with the **ELK** engine for stable,
deterministic positioning, and the rendered diagram is followed by a collapsible
**Source** block with the original text.

!!! warning "Diagram rendering needs external CLIs"
    D2 rendering requires the `d2` binary; drawio rendering requires the draw.io
    desktop app / `drawio` CLI. If the tool is missing (or a render fails), the
    report **falls back to showing the source file as plain text** — no error,
    just no picture. If a pre-rendered sibling exists (e.g. `diagram.drawio.png`),
    that image is shown and the source render is skipped to avoid duplication.

## Image comparison modes

When a case has a **gold standard** to compare against, generated images and
diagrams render inside a tabbed comparison widget instead of standalone. The
gold standard comes from the dataset case's `annotations.yaml` via a
`gold_diagram` key — the report either loads a pre-rendered image or renders the
gold source (D2/drawio) to SVG. With `--baseline`, the same widget compares the
current run against the baseline run in the **Baseline diff** section.

=== "Side by side"

    Two panels next to each other (the default tab). Best for spotting
    structural differences at a glance.

=== "Swipe"

    Both images stacked with a draggable divider (a `clip-path` slider) that
    wipes between them — good for pixel-level alignment.

=== "Onion"

    The generated image overlaid on the reference with an opacity slider
    (0–100%) — good for detecting subtle drift.

The two sides are labelled by context: **Generated** vs **Gold Standard** for a
dataset reference, or **Current** vs **Baseline** in an A/B run.

## The reward table

**Per-Case Reward Overview** is a matrix: one row per case, a **Reward** column,
then every judge grouped into three colour-coded bands:

| Band | Judges | Cell values |
| --- | --- | --- |
| **Gate Judges** | inline `check` judges | binary `PASS` / `FAIL` |
| **LLM Judges** | `prompt`/`prompt_file`, `agent`, and LLM builtins | `PASS` / `FAIL` for a boolean judge; a numeric one is coloured by position in `score_range` when it declares one |
| **Other** | Python builtins, external `module` judges | `PASS` / `FAIL` for a boolean judge; a numeric one is uncoloured unless it declares a `score_range`, banded on that scale when it does |

The **Reward** value is computed with the same `compose_reward` logic the
harness trains on, so the number shown matches your configured
[`reward:`](../concepts/reward-api.md) section (single-judge / weighted /
formula). Numeric cells are green/amber/red by where the score falls in the
judge's declared `score_range` — a value *off* that scale bands red rather than
scoring past the top of it — and a bottom **Average** row summarises scored
cases. For every judge it normalizes, the Reward column reads those same
declared ranges, so a cell near the top of its band and its contribution to the
reward agree — judges the reward clamps as-is (`reward.raw`, or a single
`reward.judge` without `normalize`) are the exception.

!!! note "Only rendered with a reward config"
    The Per-Case Reward Overview is **omitted entirely** unless the config sets a
    **non-empty** `reward:` block (an empty `reward: {}` doesn't enable it) —
    judge-only evals don't get this section (the Scoring Summary and Per-Case
    Details already cover every judge score). Define a `reward:` to enable it and
    reflect real training behaviour.

## The sampling-stability view

When judges or the pairwise comparison were sampled multiple times, the report
visualises how much the verdict wobbled.

- **Scoring Summary / pairwise row** — a small proportion bar showing
  `stable / total` cases and the sample count (e.g. `4/5 · 3×`). Green = cases
  that agreed across all samples, amber = cases that flipped.
- **Per-Case Details** — a monospace ASCII histogram of the sampled values on
  the judge's scale (its declared `score_range` — widened to show any off-scale
  sample, or the observed span when no range is declared — `F…P` for boolean,
  `A…B` for pairwise), with the median/winning bucket highlighted. A perfectly
  stable judge shows a single bar. Hover to see the raw samples.

## Markdown rendering

`analysis.md` and every judge rationale pass through a built-in markdown
renderer supporting headers, ordered/unordered lists, tables, fenced code,
bold/italic/inline code, and links. A few behaviours worth knowing:

- **Links are XSS-hardened** — only `http`, `https`, `mailto`, and relative
  targets survive; anything else (e.g. `javascript:`) is stripped to plain text.
  All content is HTML-escaped first.
- **Literal escapes are normalised** — judges that emit `\n`/`\t` as literal
  characters in JSON rationales get them converted to real line breaks.
- **Status keywords become pills** — inside markdown tables, `PASS`, `FAIL`,
  `SKIP`, `FIXED`, `REGRESSION` are auto-styled as coloured badges.

## Dark mode

The report ships light and dark themes driven by a `data-theme` attribute on
`<html>`. On load it reads a saved preference from `localStorage`
(`eval-report-theme`), falling back to the OS `prefers-color-scheme`. A toggle
button (top-right) flips and persists the choice. Printing forces a light,
shadow-free layout so hard copies stay legible.

## See also

<div class="grid cards" markdown>

- [**Reading the report**](../get-started/reading-the-report.md) — a guided tour for first-timers
- [**Judges**](../concepts/judges.md) — where the scores and rationales come from
- [**Pairwise & sampling**](../concepts/pairwise-and-sampling.md) — A/B comparison and stability
- [**Reward API**](../concepts/reward-api.md) — how the reward column is composed
- [**Thresholds**](../concepts/thresholds.md) — what drives PASS/FAIL and the Regressions section
- [**Tracing**](../concepts/tracing.md) — the execution data behind Model Usage
- [**Model providers**](../concepts/providers.md) — what the Cost Provenance panel is accounting for

</div>
