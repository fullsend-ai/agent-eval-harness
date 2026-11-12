# Installation & setup

The harness is a **Claude Code plugin**. Installing it makes all ten skills
available: `/eval-setup`, `/eval-analyze`, `/eval-dataset`, `/eval-run`,
`/eval-review`, `/eval-mlflow`, `/eval-optimize`, `/eval-compare`, `/eval-anova`,
and `/eval-check`.

## Requirements

- **Python 3.11+** (3.12+ if you plan to use the [Harbor](../guides/harbor.md) backend)
- **Claude Code**
- Model credentials: an **Anthropic API key**, **Google Vertex** credentials, or an
  **OpenRouter** key (see [step 2](#2-provide-model-credentials))

## 1. Install the plugin

=== "From the registry"

    ```bash
    claude plugin install agent-eval-harness@opendatahub-skills
    ```

=== "As a local plugin"

    ```bash
    git clone https://github.com/opendatahub-io/agent-eval-harness
    pip install -e ./agent-eval-harness
    claude --plugin-dir ./agent-eval-harness
    ```

!!! info "Dependencies auto-install"
    The plugin's `SessionStart` hook installs the Python dependencies into an
    isolated virtual environment the first time you open a session — you don't need
    to `pip install` anything for local runs. The optional extras below are for
    specific backends, runners and skills.

Optional extras (installed into the same environment):

| Extra | Enables | Command |
| --- | --- | --- |
| `mlflow` | Experiment tracking, datasets, traces | `pip install -e '.[mlflow]'` |
| `harbor` | Containerized execution (Podman/Kubernetes) | `pip install -e '.[harbor]'` |
| `evalhub` | The EvalHub platform adapter | `pip install -e '.[evalhub]'` |
| `openai` | The OpenAI SDK: the Responses API runner, and `openai:/` / `openrouter:/` judges (the session hook adds it to `.eval-venv` by itself when a judge model needs it) | `pip install -e '.[openai]'` |
| `anova` | The statistics behind [`/eval-anova`](../guides/eval-anova.md) (scipy, statsmodels, pandas, pingouin) | `pip install -e '.[anova]'` |
| `optimize` | The cap-evolve engine behind [`/eval-optimize --engine cap-evolve`](../guides/optimize-capevolve.md): `cap-evolve-core`, the dashboard and the telemetry backend, pinned to one commit from git | `pip install -e '.[optimize]'` (or `/eval-setup --optimize`) |
| `all` | Everything above **except `optimize`** — it is pinned by git SHA, and a VCS requirement in `all` would make the ordinary install need git and reachable GitHub; name it alongside: `.[all,optimize]` | `pip install -e '.[all]'` |

## 2. Provide model credentials

The harness reads credentials from the environment. Pick the path for the models
you name in `models:` — the agent under test and the LLM judges each follow their
own model id (see [Model providers](../concepts/providers.md#credentials-by-backend)):

=== "Anthropic API"

    ```bash
    export ANTHROPIC_API_KEY=sk-ant-...
    ```

=== "Google Vertex"

    ```bash
    export CLAUDE_CODE_USE_VERTEX=1
    export ANTHROPIC_VERTEX_PROJECT_ID=my-gcp-project
    export CLOUD_ML_REGION=us-east5
    ```

    This is the one Vertex recipe: the Claude Code CLI (the agent) reads all three,
    and the Anthropic judge client reads `ANTHROPIC_VERTEX_PROJECT_ID` and
    `CLOUD_ML_REGION` (default `us-east5`). Only `/eval-dataset`'s synthetic
    generation reads `ANTHROPIC_VERTEX_REGION` instead of `CLOUD_ML_REGION` — export
    it too if you generate synthetic cases on Vertex.

=== "OpenRouter"

    ```bash
    export OPENROUTER_API_KEY=sk-or-...
    ```

    Then write the model as `openrouter:/<author>/<slug>` — `--model` or
    `models.skill` for the agent (requires `runner.type: claude-code`), `models.judge`
    or a per-judge `model:` for judges. Two things still want an Anthropic path:
    [`/eval-setup`'s credential check](#3-optional-run-eval-setup) and
    [synthetic dataset generation](../guides/eval-dataset.md#synthetic-generation-in-detail).
    See [Running on OpenRouter](../guides/openrouter.md).

See the [environment variables reference](../reference/environment-variables.md) for
the full list.

## 3. (Optional) Run `/eval-setup`

`/eval-setup` is a convenience command that verifies dependencies, checks your model
credentials, configures MLflow, and sets the runs directory. **You can skip it** — but
it's the easiest way to stand up MLflow and confirm your environment is healthy.

```bash
/eval-setup
```

!!! warning "The credential check is not provider-aware"
    The setup preflight (`check_env.py`) recognises `ANTHROPIC_API_KEY` and
    `ANTHROPIC_VERTEX_PROJECT_ID` only. An OpenRouter-only or OpenAI-only setup — or a
    `cursor` / `codex` runner authenticating through `CURSOR_API_KEY` / `OPENAI_API_KEY`
    — is reported as `api_keys … none set` and the check exits non-zero, although runs
    work. Export whatever your model ids need per
    [Model providers → Credentials by backend](../concepts/providers.md#credentials-by-backend);
    the gap is listed under [Limitations](../concepts/providers.md#limitations).

Useful flags:

| Flag | Purpose |
| --- | --- |
| `--tracking-uri <uri>` | Point MLflow at a specific server or store |
| `--skip-mlflow` | Configure everything except MLflow |
| `--runs-dir <path>` | Set where run artifacts are written |
| `--harbor` | Also install Harbor + the Kubernetes client |

### MLflow tracking (optional)

MLflow logging is **opt-in** — it only happens when your `eval.yaml` has an
[`mlflow:` block](../reference/config/mlflow.md). Pick a store:

=== "Local server"

    ```bash
    mlflow server --port 5000
    export MLFLOW_TRACKING_URI=http://127.0.0.1:5000
    ```

=== "SQLite (self-contained)"

    ```yaml
    # in eval.yaml
    mlflow:
      experiment: my-skill-eval
      tracking_uri: sqlite:///mlflow.db
    ```

=== "Remote"

    ```bash
    export MLFLOW_TRACKING_URI=https://mlflow.example.com
    ```

!!! note "Where runs are stored"
    Run artifacts land under `$AGENT_EVAL_RUNS_DIR` (default: `eval/runs`),
    independently of MLflow. See
    [Runs directory & artifacts](../reference/runs-directory.md).

## Next step

You're ready to evaluate something.

[Run your first eval :material-arrow-right:](first-eval.md){ .md-button .md-button--primary }
