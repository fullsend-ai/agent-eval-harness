# CLI & entry points

The harness has three kinds of interface: **slash commands** you run inside Claude
Code (the day-to-day authoring and run pipeline), a single installed **console
script** (`claude-trace`), and a few **`python -m` module entry points** used by the
containerized and platform backends. This page is the map of all of them.

!!! warning "`agent-eval run` is not a real command"
    The deployment docs and some diagrams show `agent-eval run --config eval.yaml` as
    a local entry point. **No such console script is registered.** The
    `[project.scripts]` entries in
    [`pyproject.toml`](https://github.com/opendatahub-io/agent-eval-harness/blob/main/pyproject.toml)
    are `claude-trace` and `agent-eval-capevolve` (below). Run evals locally with the
    **`/eval-run`** slash command; use the `python -m` modules below for the Harbor
    and EvalHub backends.

## The surface at a glance

```mermaid
flowchart TD
    subgraph SC["Slash commands (inside Claude Code)"]
        A["/eval-setup → /eval-analyze → /eval-dataset →<br/>/eval-run → /eval-review → /eval-optimize → /eval-mlflow"]
        C["/eval-check"]
        C2["/eval-compare"]
        C3["/eval-anova → /eval-run per cell → /eval-compare"]
    end
    subgraph CS["Console scripts (pip-installed)"]
        T["claude-trace"]
        CV["agent-eval-capevolve"]
    end
    subgraph PM["python -m entry points"]
        H["agent_eval.harbor.run"]
        E["agent_eval.evalhub.runner"]
    end
    ST["state.py utility<br/>(context-safe key/value store)"]
    SC --> ST
    T --> ST
```

## Slash commands

Ten skills ship as slash commands. Each one is a stage in the pipeline; run them in
order for a first eval, or invoke individually.

| Command | Purpose | Guide |
| --- | --- | --- |
| `/eval-setup` | Preflight environment: dependencies, MLflow, API keys, run dirs | [Installation](../get-started/installation.md) |
| `/eval-analyze` | Analyze a skill or docs, generate `eval.yaml` + `eval.md` | [eval-analyze](../guides/eval-analyze.md) |
| `/eval-dataset` | Generate or expand test cases (and Harbor task packages / S3 export via `export_s3.py`) | [eval-dataset](../guides/eval-dataset.md) |
| `/eval-run` | Execute the suite, collect artifacts, score, report | [eval-run](../guides/eval-run.md) |
| `/eval-compare` | Compare results across models/runs into one HTML report | [eval-compare](../guides/eval-compare.md) |
| `/eval-anova` | Fan a `matrix:` of configurations out over `/eval-run`, then ANOVA + Pareto into `anova.json` and the comparison report | [eval-anova](../guides/eval-anova.md) |
| `/eval-review` | Interactive human review of a run; propose config changes | [eval-review](../guides/eval-review.md) |
| `/eval-optimize` | Automated refinement loop (composes with `/eval-run`); `--engine capevolve` for statistically validated optimization (train/val/test, gated accept, sealed test) | [eval-optimize](../guides/eval-optimize.md) / [optimize-capevolve](../guides/optimize-capevolve.md) |
| `/eval-mlflow` | Dataset sync, result logging, trace feedback | [eval-mlflow](../guides/eval-mlflow.md) |
| `/eval-check` | Full-harness config health check across all skills | [eval-check](../guides/eval-check.md) |

!!! note "`/eval-run` provider flags"
    Besides `--model` / `--subagent-model`, the `execute.py` step behind `/eval-run` takes
    three flags for runs whose skill model is `openrouter:/…`: `--strict-cost` (exit `2`
    on `cost_source: unavailable` or `budget.exceeded: run`), `--strict-routing` (exit `2`
    on routing violations or an incomplete audit) and `--allow-estimate` (record
    `runner:estimate` instead of `unavailable` on an offline replay). All three are no-ops
    without a provider plan and only ever turn a successful run into exit `2`. There is no
    `--judge-model` on `execute.py`; the judge model comes from a per-judge `model:`,
    `models.judge` or the `EVAL_JUDGE_MODEL` variable. See
    [eval-run → Flags](../guides/eval-run.md#flags) and
    [Model providers](../concepts/providers.md).

!!! tip "See the whole flow"
    The [pipeline guide](../guides/pipeline.md) shows how these stages hand off to each
    other, and the [get-started walkthrough](../get-started/first-eval.md) runs the
    core four end to end.

### `/eval-anova` scripts — `orchestrate.py` and `report.py`

`/eval-anova` is an orchestrator over `/eval-run`: `orchestrate.py` reads the
[`matrix:`](config/matrix.md) block, drives the eval-run pipeline once per
condition × replication (each cell a standard run stamped with a `condition.json`),
then writes `anova.json` and renders the `/eval-compare` report. Runs land under
`$AGENT_EVAL_RUNS_DIR/<eval-name>/`.

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/orchestrate.py --config eval.yaml                 # run → analyze → report
python3 ${CLAUDE_SKILL_DIR}/scripts/orchestrate.py --config eval.yaml --dry-run       # design + cost estimate only
python3 ${CLAUDE_SKILL_DIR}/scripts/orchestrate.py --config eval.yaml --analyze-only  # re-analyze existing runs
```

| Flag | Default | Effect |
| --- | --- | --- |
| `--config <path>` | — (required) | The `eval.yaml` carrying the `matrix:` block |
| `--dry-run` | off | Validate the matrix, print the design and a cost estimate, run nothing |
| `--analyze-only` | off | Recompute `anova.json` over the existing runs and re-render; no execution. Reads the matrix non-strictly, so it also works over CI-produced run dirs that never had one |
| `--cases <id…>` | all cases | Restrict execution to these case ids |
| `--avg-cost-per-run <float>` | unset | Per-run USD cost for the `--dry-run` estimate (else `execution.max_budget_usd` as an upper bound, else "unknown") |
| `--output <path>` | `<runs>/comparison-report` | Output dir for the `/eval-compare` report |
| `--no-report` | off | Write `anova.json` but skip the report render |
| `--correction <method>` | unset | `holm`, `bh` (alias `fdr_bh`), or `none` across the ANOVA term family; overrides `matrix.analysis.correction` (then `holm`) |
| `--per-judge` | off | Also run the ANOVA once per judge, one Benjamini–Hochberg family across judges × terms; enables `matrix.analysis.per_judge` |

The process exits non-zero when the config has no `matrix:` (outside
`--analyze-only`), when the dataset has no cases, or when no cell completed; a single
failing cell is logged and skipped so a partial matrix still yields an analysis.

`report.py` is the statistics-forward companion view — condition means, F / p / η²,
the per-case matrix, contrasts and the per-judge table — rendered purely from
`anova.json`:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/report.py [PATH]   # an anova.json, a dir holding one, or a runs dir
```

`PATH` defaults to `$AGENT_EVAL_RUNS_DIR` (else `eval/runs`); the script writes
`anova-report.html` and `anova-report.md` next to the `anova.json` it found. The
pooled cross-model comparison (leaderboard, heatmap) is `/eval-compare`'s
`compare.py generate <runs-dir> [--output DIR] [--title T] [--overview TEXT]`, which
folds the statistics section in whenever it finds an `anova.json`. See
[experiment-level artifacts](runs-directory.md#experiment-level-artifacts).

## `claude-trace` (console script)

`claude-trace` is the one command installed on your `PATH` by `pip install`. It's a
drop-in replacement for `claude --print` that captures the stream-json output and
builds a hierarchical MLflow trace (tool calls, subagent spans, execution metrics).
Use it to trace a skill run **standalone**, outside the eval pipeline.

=== "Pipe a prompt via stdin"

    ```bash
    echo "/rfe.speedrun --input batch.yaml --headless" | \
      claude-trace --model opus
    ```

=== "Prompt as an argument"

    ```bash
    claude-trace --model opus -p "/rfe.create 'GPU autoscaling'"
    ```

=== "Capture only, no MLflow push"

    ```bash
    claude-trace --model opus --no-mlflow --trace-dir /tmp/my-run
    ```

It forces `--output-format stream-json`, `--print`, and `--verbose`; every other flag
passes straight through to `claude`.

| Flag | Effect |
| --- | --- |
| `-p <prompt>` | Prompt to run (otherwise read from stdin) |
| `--model <name>` | Model passed through to `claude` |
| `--experiment <name>` | MLflow experiment (else `MLFLOW_EXPERIMENT_NAME`, else `Default`) |
| `--trace-dir <path>` | Where to write artifacts (default `tmp/trace-runs/<timestamp>`) |
| `--no-mlflow` | Capture artifacts only; skip the trace push |
| `--help` | Print usage and exit |

Each run writes `stdout.log`, `stderr.log`, and `run_result.json` into the trace dir.

!!! note "Environment"
    `MLFLOW_TRACKING_URI` sets the MLflow server (default `http://127.0.0.1:5000`);
    `--experiment` overrides `MLFLOW_EXPERIMENT_NAME`. If `mlflow` isn't installed the
    push is skipped with a warning — artifacts are still saved. See
    [tracing](../concepts/tracing.md) and
    [environment variables](environment-variables.md).

## `agent-eval-capevolve` (console script)

The other registered console script — `/eval-optimize`'s cap-evolve path
(`skills/eval-optimize/scripts/capevolve.py`) is a thin shim around it. Run directly
from any benchmark checkout for scripting or CI; see
[Optimize with cap-evolve](../guides/optimize-capevolve.md) for the full walkthrough.

```bash
agent-eval-capevolve --dry-run
agent-eval-capevolve --max-usd 25
agent-eval-capevolve --status
agent-eval-capevolve --stop
agent-eval-capevolve --apply
```

## `python -m` module entry points

The containerized and platform backends aren't console scripts — they're modules you
invoke with `python -m`. `/eval-run --runner harbor` and `--runner evalhub` call these
for you; run them directly for CI or debugging.

### `agent_eval.harbor.run` — the Harbor backend

Generates (or reuses) Harbor task packages, runs one `harbor run` job over them, then
maps the per-case verifier output back into the harness `run_result.json` +
`summary.yaml` + `report.html` shape.

```bash
python -m agent_eval.harbor.run \
  --config eval.yaml \
  --model opus \
  --output eval/runs/my-run \
  --tasks-dir eval/harbor/tasks \
  --jobs-dir eval/harbor/jobs \
  --env kubernetes
```

| Flag | Purpose |
| --- | --- |
| `--config` | Path to `eval.yaml` (required) |
| `--model` | Model for the agent under test (required); a bare id or an `openrouter:/<author>/<slug>` URI |
| `--output` | Harness run dir to write (required) |
| `--tasks-dir` | Where task packages live / are generated (required) |
| `--jobs-dir` | Where Harbor writes its job output (required) |
| `--image` | Task image — required **only** when generating tasks |
| `--agent` | Harbor agent name (default: derived from `runner.type`) |
| `--env` | `podman`, `kubernetes`, `k8s`, or `openshift` (default `kubernetes`) |
| `--environment-import-path` | Custom Harbor environment path (overrides `--env`) |
| `--n-concurrent` | Concurrent trials (default `1`) |
| `--cases` | Restrict to specific case IDs |
| `--mount SOURCE:TARGET` (optional `:ro` or `:rw` suffix) | Repeatable Podman bind mount; defaults to read-only |
| `--cpus` / `--memory-mb` | Hard per-environment CPU and memory limits |
| `--no-llm-judges` | Keep deterministic judges and omit model-calling judges |
| `--judge-model` | Override the in-container judge model — Harbor **task generation only** (errors on pre-generated tasks unless `--regenerate`); accepts `provider:/` URIs such as `openrouter:/<author>/<slug>`. The local `/eval-run` has no such flag. |
| `--arguments` / `--skill` | Override generated task instructions |
| `--regenerate` | Rebuild task packages even if `--tasks-dir` already has them |

!!! note "Tasks are reused if present"
    If `--tasks-dir` already holds packages (e.g. emitted by `/eval-dataset`), they're
    used as-is and `--image` isn't needed. Generation-time options (`--arguments`,
    `--skill`, `--cases`, `--judge-model`, `--no-llm-judges`) cannot be applied to
    pre-generated tasks — the command errors unless `--regenerate` is passed. The
    process exits non-zero if regression [thresholds](config/thresholds.md) are
    violated, or if the run produces no numeric scores. Mounts are supported only by
    the bundled Podman environment; the command fails instead of silently ignoring
    them on Kubernetes/OpenShift. Requires the `harbor` extra
    (`pip install 'agent-eval-harness[harbor]'`,
    Python ≥ 3.12). See the [Harbor guide](../guides/harbor.md).

### `agent_eval.evalhub.runner` — the EvalHub backend

Submits a job to an EvalHub server, polls to completion, and maps the platform's
`BenchmarkResult.metrics` back into the same `summary.yaml` + `report.html`.

```bash
python -m agent_eval.evalhub.runner \
  --config eval.yaml \
  --model opus \
  --output eval/runs/my-run \
  --evalhub-url https://evalhub.example.com
```

| Flag | Purpose |
| --- | --- |
| `--config` | Path to `eval.yaml` (required) |
| `--model` | Model for the agent under test (required) |
| `--output` | Harness run dir to write (required) |
| `--evalhub-url` / `--evalhub-token` | Server URL and auth token |
| `--namespace` | Kubernetes namespace for the Job |
| `--provider-id` | EvalHub provider id (default `agent-eval`) |
| `--benchmark-id` | Benchmark id to submit under |
| `--project-dir` | Project directory to package into ConfigMaps |
| `--timeout` / `--poll-interval` | Job wait timeout and poll cadence (default `10.0s`) |

!!! note "In-process on the platform"
    Requires the `evalhub` extra (`pip install 'agent-eval-harness[evalhub]'`). The
    adapter runs **in-process** inside the EvalHub-created Job pod — no sub-pods, no
    Harbor. See the [EvalHub guide](../guides/evalhub.md) and
    [backends](../concepts/backends.md).

### `agent_eval.config` — inspect a merged config

```bash
python3 -m agent_eval.config --print eval-profiles/openrouter-glm-5.2.yaml
```

Prints the config as the harness loads it: the [`extends`](config/extends.md)
chain resolved (root first), every list item annotated with `# from: <file>`,
and `# !replace from: <file>` on a list a profile replaced. Exit `1` with the
loader's message on a broken chain (missing base, cycle, absolute path).

### `agent_eval.providers.openrouter.*` — provider maintenance

OpenRouter runs have three maintenance entry points; none of them spends on the agent
path, and every key is read from the environment, never from a flag:

```bash
# the run's preflight without a run: catalog, pins, key, eligibility; writes routing_snapshot.json
python3 -m agent_eval.providers.openrouter.preflight --config eval.yaml [--model openrouter:/…] [--run-dir DIR]

# re-query the run's unpriced generations and re-reconcile every run_result.json
python3 -m agent_eval.providers.openrouter.backfill <run_dir> [--config eval.yaml] [--base-url URL]

# retry the revocation of a key-guardrail per-run key recorded in <run_dir>/provider/key.json
python3 -m agent_eval.providers.openrouter.keys revoke <run_dir>
```

See [`models.providers`](config/providers.md) and the
[OpenRouter guide → Offline commands](../guides/openrouter.md#offline-commands).

## `state.py` — the context-safe state store

`agent_eval/state.py` is a small YAML/JSON key-value utility the skills use to persist
pipeline state (run ids, config paths, flags) so it survives context compression
between skill steps. It isn't installed on your `PATH`; the skills call it by absolute
path, e.g. `python3 ${CLAUDE_SKILL_DIR}/scripts/state.py`.

| Subcommand | Effect |
| --- | --- |
| `init <path> key=value ...` | Create a state file with the given keys |
| `set <path> key=value ...` | Merge keys into an existing file |
| `read <path>` | Print the state (JSON pretty-printed for `.json`) |
| `write-ids <path> ID ...` | Write a de-duplicated id list, one per line |
| `read-ids <path>` | Print the id list space-separated |
| `clean` | Remove the `tmp/` directory |
| `timestamp` | Print a UTC ISO-8601 timestamp |

Values `true`/`false`/`null`/`none` are parsed to their typed equivalents; everything
else stays a string.

## Related

<div class="grid cards" markdown>

- [**Runs directory**](runs-directory.md) — what `/eval-run` and the backends write
- [**Environment variables**](environment-variables.md) — every var the CLIs read
- [**Container images**](container-images.md) — images the Harbor / EvalHub paths use
- [**Python API**](python-api.md) — importable modules behind these entry points

</div>
