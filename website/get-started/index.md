# Get Started

The harness ships as a **Claude Code plugin**. Once it's installed, you drive it
entirely through slash commands (`/eval-*`). This section takes you from zero to a
scored HTML report.

## The shortest path

<div class="steps" markdown>

1. **Analyze** — point the harness at a skill and let it write `eval.yaml`.
   `/eval-analyze --skill my-skill`
2. **Build a dataset** — generate test cases that match the config.
   `/eval-dataset`
3. **Run** — execute, score with judges, and build the HTML report.
   `/eval-run --model opus`

</div>

That's it — `/eval-setup` and `/eval-mlflow` are optional (dependencies auto-install,
and MLflow logging is opt-in), and so are `/eval-compare` and `/eval-anova`, which
only come into play once you have more than one run to put side by side.

## How the pieces fit

``` mermaid
graph TD
    subgraph required ["Required for a first run"]
        A["/eval-analyze<br/>writes eval.yaml"] --> D["/eval-dataset<br/>writes test cases"]
        D --> R["/eval-run<br/>execute + score + report"]
    end
    subgraph optional ["Optional"]
        S["/eval-setup<br/>env + MLflow"] -.-> A
        R -.-> V["/eval-review<br/>human feedback"]
        R -.-> O["/eval-optimize<br/>auto-refine"]
        R -.-> M["/eval-mlflow<br/>log + trace"]
        R -.-> C["/eval-compare<br/>runs side by side"]
        N["/eval-anova<br/>matrix sweep + ANOVA"] -.-> R
        N -.-> C
    end
```

!!! tip "Once you have more than one run"
    [`/eval-compare`](../guides/eval-compare.md) renders models or runs side by side
    in one report. [`/eval-anova`](../guides/eval-anova.md) goes further: it sweeps a
    `matrix:` of models/configs (fanning out `/eval-run` per cell) and tells you —
    with Holm-corrected p-values and post-hoc level contrasts — whether a difference
    is statistically real before you act on it.

## In this section

<div class="grid cards" markdown>

-   :material-download: **[Installation & setup](installation.md)**

    ---

    Add the plugin, install dependencies, and configure API keys and MLflow.

-   :material-play: **[Your first eval (skill mode)](first-eval.md)**

    ---

    Analyze a skill, generate a dataset, run it, and read the report.

-   :material-file-document-multiple: **[Your first agentic-docs eval](agentic-docs.md)**

    ---

    Test whether an agent can navigate and correctly use your documentation.

-   :material-chart-box: **[Reading the report](reading-the-report.md)**

    ---

    Understand scores, per-case detail, diffs, and cost.

</div>

!!! tip "New to the terminology?"
    Two words are worth pinning down before you start: a **runner** is the *agent
    runtime* (`claude-code`, `cursor`, `codex`, `fullsend`, `cli`, `responses-api`), while an **execution backend**
    is *where* it runs (Local, Harbor, EvalHub). See the
    [Glossary](../reference/glossary.md).
