# Reference

Look-up material for every config key, built-in catalog, CLI command, and API symbol.
For the concepts behind these knobs, see [Concepts](../concepts/index.md); for
task-oriented walkthroughs, see [Guides](../guides/index.md).

!!! tip "The one file to know"
    Almost everything is driven by a single `eval.yaml`. Start with the
    [eval.yaml schema](../reference/eval-yaml.md) — it links out to a per-key page for
    every block below.

## Config

<div class="grid cards" markdown>

- [**eval.yaml schema**](../reference/eval-yaml.md) — Every top-level key, with two minimal configs and a fully annotated example
- [**extends**](../reference/config/extends.md) — Layer a profile over a base config: merge policy, `!replace`, `config_chain`
- [**execution**](../reference/config/execution.md) — `mode` (case/batch), `skill`/`prompt`, `arguments`, `timeout`, `max_budget_usd`, `parallelism`, `env`, `symlinks`
- [**runner**](../reference/config/runner.md) — `type`, `effort`, `settings`, `plugin_dirs`, `env`, `system_prompt`, `workspace_mode`
- [**models**](../reference/config/models.md) — `skill`, `subagent`, `judge`, `hook` roles and CLI precedence
- [**models.providers**](../reference/config/providers.md) — The `openrouter` block behind `provider:/` ids: key variables, routing table, enforcement, judge client
- [**mlflow**](../reference/config/mlflow.md) — `experiment`, `tracking_uri`, `tags`
- [**dataset**](../reference/config/dataset.md) — `path`, `schema`, `workspace.files`
- [**generation**](../reference/config/generation.md) — `strategy` (skill/synthetic/from-traces), `context`, `seeds`
- [**inputs.tools**](../reference/config/inputs-tools.md) — Tool interception `match` / `prompt` handlers
- [**outputs**](../reference/config/outputs.md) — `path` vs `tool` artifacts, `schema`, `batch_pattern`
- [**traces**](../reference/config/traces.md) — `stdout`, `stderr`, `events`, `metrics`
- [**permissions**](../reference/config/permissions.md) — `allow` / `deny` tool patterns
- [**hooks**](../reference/config/hooks.md) — Lifecycle shell hooks
- [**judges**](../reference/config/judges.md) — The five judge types and every field
- [**thresholds**](../reference/config/thresholds.md) — `min_mean`, `min_pass_rate`, `min_win_rate`, `max_error_rate`
- [**reward**](../reference/config/reward.md) — Collapse judges into an RL reward scalar
- [**matrix**](../reference/config/matrix.md) — `factors`, `replications`, `analysis.correction`, `analysis.per_judge` for `/eval-anova`

</div>

## Catalogs & tooling

<div class="grid cards" markdown>

-   :material-gavel: **Built-in judges**

    ---

    Reusable judges auto-discovered by category (e.g. `consulted_docs`), with their arguments.

    [:octicons-arrow-right-24: Built-in judges](../reference/builtin-judges.md)

-   :material-text-box-multiple: **Built-in generation prompts**

    ---

    Shipped seed prompts for synthetic datasets (`docs/navigation`, `docs/anti-pattern`, and more).

    [:octicons-arrow-right-24: Built-in prompts](../reference/builtin-prompts.md)

-   :material-console: **CLI & entry points**

    ---

    Slash commands (`/eval-*`), `claude-trace`, and `python -m` entry points.

    [:octicons-arrow-right-24: CLI reference](../reference/cli.md)

-   :material-code-braces: **Python API**

    ---

    The `agent_eval` package: `EvalConfig`, `EvalRunner`, and the MLflow/Harbor modules.

    [:octicons-arrow-right-24: Python API](../reference/python-api.md)

</div>

## Environment & artifacts

<div class="grid cards" markdown>

-   :material-folder-open: **Runs directory**

    ---

    The per-run and per-case layout under `$AGENT_EVAL_RUNS_DIR` (default `eval/runs`), plus `review.yaml`, `anova.json`, and the other experiment-level artifacts.

    [:octicons-arrow-right-24: Runs directory](../reference/runs-directory.md)

-   :material-docker: **Container images**

    ---

    The `agent-eval-harness` base image and the `agent-eval-hub` provider image.

    [:octicons-arrow-right-24: Container images](../reference/container-images.md)

-   :material-variable: **Environment variables**

    ---

    Every `AGENT_EVAL_*`, `ANTHROPIC_*`, `OPENROUTER_*`, `OPENAI_*`, and `MLFLOW_*` variable the harness reads.

    [:octicons-arrow-right-24: Environment variables](../reference/environment-variables.md)

-   :material-book-alphabet: **Glossary**

    ---

    Definitions of core terms used throughout the docs.

    [:octicons-arrow-right-24: Glossary](../reference/glossary.md)

</div>
