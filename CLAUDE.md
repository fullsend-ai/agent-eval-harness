# Agent Eval Harness

Generic evaluation framework for Claude Code skills and agent capabilities. Uses MLflow as the backbone for tracing, evaluation, datasets, and reporting.

## Project Status

Phase 1 (core framework), Phase 2 (scoring integration), and Phase 3 (prompt-based evaluation) are implemented. See `eval/plans/agent-eval-harness-design.md` in the rfe-creator project for the full design doc.

## Execution Model

The harness separates **how many invocations** (`execution.mode`) from **what to execute** (`execution.skill` or `execution.prompt`):

### Execution Mode (case vs batch)
- **case**: One invocation per test case (default). The harness loops over cases.
- **batch**: One invocation for all cases via batch.yaml. The skill/agent loops internally.

### What to Execute (skill vs prompt)
- **Skill mode** (`execution.skill`): Test predefined skill implementations (`/my-skill --args`). Evaluates skill correctness, quality, and cost efficiency.
- **Prompt mode** (`execution.prompt`): Test agent capabilities directly by sending prompts without a skill wrapper.

### Common Patterns

**Skill evaluation (case mode)**:
```yaml
execution:
  mode: case
  skill: rfe.create
  arguments: '--priority {{ input.priority }} "{{ input.prompt }}"'
```

**Skill evaluation (batch mode)**:
```yaml
execution:
  mode: batch
  skill: rfe.speedrun
  arguments: '--input batch.yaml --headless'
```

**Agentic documentation testing (prompt mode)** ✨:
```yaml
execution:
  mode: case
  prompt: "{{ input.prompt }}"
```

**Implemented flavor - Agentic Documentation Testing** (see `examples/openshift-agentic-docs.md`):
- Documentation effectiveness: Can agents navigate and use your docs?
- Pattern understanding: Can agents identify and apply code patterns?
- Constraint compliance: Do agents respect documented rules?
- API usage: Can agents call APIs with the right fields and structure from documentation alone?

Includes builtin documentation generation prompts (navigation, anti-pattern, authoring, component-usage, architecture) for structured evaluation. See `agent_eval/prompts/docs/`.

**Extensible to other scenarios**: Code generation, API pattern validation, reasoning quality, custom agent benchmarks.

Prompt mode provides direct agent invocation with LLM rubric judges and flexible evaluation criteria.

## Architecture

```
agent_eval/              # Python package (config, runner, state)
  config.py              # EvalConfig from eval.yaml (+ resolve_workspace_source)
  state.py               # Shared state persistence (key-value store)
  workspace_provisioning.py # Materialize shared {dest,source} workspace.files (local + Harbor + S3 export)
  agent/
    base.py              # EvalRunner ABC + RunResult
    claude_code.py       # Claude Code CLI runner (claude --print)
    codex.py             # OpenAI Codex CLI runner (codex exec --json)
    cli_runner.py        # Opaque CLI runner (arbitrary command templates)
    fullsend.py          # fullsend runner (fullsend run <agent>; run dir → RunResult)
    stream_capture.py    # Stream-json processing (events, timestamps, usage, hooks)
  harbor/                # Harbor integration (containerized execution)
    tasks.py             # Generate self-contained Harbor task packages from eval.yaml
    reward.py            # Judge → Harbor reward.json bridge (in-container verifier)
    run.py               # /eval-run --runner harbor orchestration
    results.py           # Parse Harbor job dirs into per-case results
    podman.py            # Podman BaseEnvironment (local containers)
    kubernetes.py        # Kubernetes BaseEnvironment (OpenShift, Python client)
    templates/           # task.toml, instruction.md, test.sh templates
  tools/
    interception.py      # Shared tool interception generation (workspace + Harbor)
  judges/                # Builtin judges (auto-discovered by category)
  prompts/               # Builtin generation prompts (synthetic generation)
    __init__.py          # BuiltinPromptRegistry + resolve_seed_prompt
    docs/                # navigation, anti-pattern, authoring, component-usage, architecture
  mlflow/
    experiment.py        # MLflow experiment setup, server check, feedback logging
    datasets.py          # Dataset create/sync utilities
    traces.py            # Trace search and input extraction
    trace_builder.py     # Hierarchical trace builder (stream-json → MLflow trace)
  cli/
    trace_run.py         # claude-trace CLI (standalone skill tracing)
  anova/                 # eval-anova: DoE matrix testing + ANOVA statistics
    matrix.py            # Factorial (DoE) experiment design + cost estimation
    composite.py         # Composite scoring helpers (bool gates + numeric; aggregate)
    archive.py           # Git-backed results archival (ResultsArchiver)
    stats/
      anova.py           # Repeated-measures, mixed-effects, one-way ANOVA
      pareto.py          # Cost/quality Pareto frontier

skills/eval-setup/       # Skill: environment setup
  SKILL.md               # Dependencies, MLflow, API keys, directories
  scripts/
    check_env.py         # Preflight environment checks

skills/eval-analyze/     # Skill: bootstrap eval config
  SKILL.md               # Analyze skill or docs, generate eval.yaml + eval.md
  scripts/
    find_skills.py       # Skill discovery (reads plugin.json for paths)
    validate_eval.py     # Config and memory validation
    resolve_prompt.py    # Resolve prompt file paths
    assess_skills.py     # --assess: extract per-skill facts (LLM judges eval-worthiness)
  prompts/
    analyze-skill.md     # Skill analysis prompt (skill mode)
    generate-eval-md.md  # eval.md generation prompt
    assess-skills.md     # --assess classification (RECOMMENDED/OPTIONAL/SKIP/EXISTS)
  references/
    eval-yaml-template.md # Full eval.yaml template for generation

skills/eval-dataset/     # Skill: generate test cases
  SKILL.md               # Bootstrap, expand, or extract cases from traces (skill mode)
                         # OR synthetic generation from seeds (prompt mode)
  scripts/
    generate_synthetic.py # Synthetic test case generation (prompt mode)
    list_prompts.py      # List builtin generation prompts (from agent_eval/prompts/)
    harbor.py            # CLI: generate Harbor task packages (thin wrapper → harbor.tasks)
    export_s3.py         # CLI: materialize shared workspace.files into per-case dirs for S3/EvalHub (→ evalhub.export)
  # Builtin generation prompts live in agent_eval/prompts/ (like builtin judges)

skills/eval-run/         # Skill: execute eval suite
  SKILL.md               # Prepare, execute, collect, score, report
  scripts/
    workspace.py         # Workspace creation, batch.yaml, symlinks
    execute.py           # Skill execution via agent runner
    collect.py           # Artifact collection + case mapping
    score.py             # Scoring: inline checks, LLM judges, pairwise, regression
    report.py            # HTML report generation (scoring summary, per-case details, diffs)
    tools.py             # PreToolUse hook for tool interception
  prompts/
    analyze-results.md   # Results interpretation prompt
    comparison-judge.md  # Pairwise comparison judge prompt
  references/
    data-pipeline.md     # Dataset → workspace → execution → scoring flow
    tool-interception.md # Tool interception format and field reference

skills/eval-review/      # Skill: interactive human review
  SKILL.md               # Present results, collect feedback, propose changes
  prompts/
    review-results.md    # Analysis framework for feedback patterns

skills/eval-mlflow/      # Skill: MLflow integration
  SKILL.md               # Dataset sync, result logging, trace feedback
  scripts/
    sync_dataset.py      # Push cases to MLflow dataset registry
    log_results.py       # Log run params, metrics, artifacts to MLflow
    attach_feedback.py   # Push/pull feedback between harness and traces
    from_traces.py       # Extract inputs from production traces

skills/eval-optimize/    # Skill: automated refinement loop
  SKILL.md               # Composes with /eval-run via Skill tool

skills/eval-compare/     # Skill: cross-model/cross-run comparison report
  SKILL.md               # Discover runs, generate tabbed HTML, LLM analysis sections
  scripts/
    compare.py           # Discover runs + comparison HTML; renders anova.json stats if present

skills/eval-anova/       # Skill: DoE/ANOVA matrix experiments (orchestrator over eval-run)
  SKILL.md               # matrix: → fan out /eval-run per cell → anova.json → /eval-compare
  QUICKSTART.md          # From-scratch setup and run steps
  scripts/
    orchestrate.py       # Runnable entrypoint: fan-out loop, --dry-run, --analyze-only
    analyze.py           # analyze_runs: ANOVA + Pareto over runs' summary.yaml → anova.json
    design.py            # Factorial design + cost estimation helpers
    report.py            # Deep ANOVA detail report from anova.json
  references/
    matrix-schema.md     # Full matrix: config schema reference

skills/eval-check/ # Skill: full-harness configuration health check
  SKILL.md               # Scans all skills, commands, CLAUDE.md, hooks for overlap and issues
  scripts/
    harness_inventory.py # Project artifact discovery, word counting, eval config discovery
    reference_checker.py # Cross-component reference validation (broken refs, missing scripts, orphans)
```

### Adding a skill (or a script to an existing one)

Skill scripts run as `python3 ${CLAUDE_SKILL_DIR}/scripts/foo.py`, so `sys.path[0]`
is the script's own directory — not the plugin root. Two things are therefore
required, and both have been forgotten before:

1. **Symlink** `scripts/agent_eval -> ../../../agent_eval` in the new
   `skills/<skill>/scripts/` dir. Without it `import agent_eval` raises
   ModuleNotFoundError.
2. **`import agent_eval._bootstrap  # noqa: F401 — auto-activate venv` as the first
   import** in any script that uses a third-party package (`yaml`, `mlflow`,
   `anthropic`, `jinja2`, `pandas`, …) or imports `agent_eval`. It puts
   `.eval-venv`'s site-packages on `sys.path`, or re-execs into the venv
   interpreter on an ABI mismatch. It must precede the third-party imports —
   including ones deferred inside functions, which still need the venv when they run.

Stdlib-only scripts need neither. `scripts/ensure_deps.py` is exempt: it *creates*
the venv. Modules under `agent_eval/` that SKILL.md invokes directly by path count
as entry points too (see `agent_eval/state.py` for the `__package__` guard that case
needs). Spawning a python child? Strip `_AGENT_EVAL_BOOTSTRAP_DONE` from its env —
the sentinel is for surviving `os.execv` within one process, and a child has to
activate the venv itself (`orchestrate.py::_child_env`).

`tests/test_venv_activation.py` enforces all of this, so CI catches a miss.

## How It Works

Projects create an `eval.yaml` config file with:
- `execution` — `mode` (`case` or `batch`), `skill` or `prompt` (mutually exclusive; skill mode vs prompt mode), `arguments` template with `{field}` placeholders, optional `timeout`/`max_budget_usd`/`parallelism` (concurrent case execution), `env` for injecting environment variables into workspaces (`$VAR` syntax resolves from caller's env), and `project_hooks` (default `true`) to carry the project's `.claude/settings.json` hooks into each workspace after the harness's own, and `symlinks` (default `[]`) listing extra top-level project entries linked into each local workspace after the defaults (single names; names the harness writes into a workspace are rejected at load; a missing or out-of-bounds entry is skipped with a warning; an existing destination fails staging; Harbor/EvalHub ignore it — use `dataset.workspace.files` `{dest, source}` there). A top-level `skill:` is still accepted (auto-normalized to `execution.skill`) but deprecated.
- `runner` — `type` discriminator (`claude-code`, `cursor`, `codex`, `fullsend`, etc.; an unknown type is rejected at config load) plus runner-specific `effort`/`settings`/`plugin_dirs`/`env`/`system_prompt`. `fullsend` also takes `agent` (required), `fullsend_dir`, `binary`, `extra_args` and `term_grace_s`. `cursor` and `fullsend` are **local execution only** (not available in the Harbor or EvalHub backends); `cursor` and `fullsend` reject `inputs.tools`.
- `models` — defaults for `skill`/`subagent`/`judge`/`hook` roles (CLI flags override). `hook` is the model for LLM-based AskUserQuestion answering. `judge_verdict` (`auto` | `json_schema` | `tool`, default `auto`) picks how SDK-backed LLM judges carry their verdict — the provider's structured output first, the forced tool call as the fallback (spec 015); a judge's own `verdict:` overrides it. The `judge` backend is chosen by the judge model's provider, independent of `runner.type` (so a Claude judge works with a Cursor/Codex runner and vice-versa): `anthropic:/…` or a bare Claude id → Anthropic SDK; `openai:/…` or any other bare id → OpenAI SDK (set `OPENAI_BASE_URL` for an OpenAI-compatible gateway); `runner:/…` → grade through the configured runner. An explicit unsupported provider (`gemini:/…`) is rejected at config load for a statically-set judge model (an env-only `EVAL_JUDGE_MODEL` is checked at judge build; `agent:` judges route through the runner). Resolution: `agent_eval/prompt_backends.resolve_judge_backend`.
- `mlflow` — `experiment`, optional `tracking_uri`/`tags`
- `permissions` — `allow`/`deny` tool patterns for headless execution
- `dataset` — `path` to test cases directory, `schema` describing case structure in natural language. `workspace.files` whitelists companion files copied into each case workspace: either a per-case string path, or a shared `{dest, source}` mapping (a project/plugin resource resolved against the project root or `runner.plugin_dirs`, symlinks followed, materialized as a real file — never a symlink — so it ports across local, Harbor, and S3/EvalHub via `agent_eval.workspace_provisioning.materialize_shared_files`; the S3 path uses the `export_s3.py` step). Prefer `{dest, source}` over committing a symlink into a case (symlinks are skipped locally, ignored by Harbor, and don't round-trip through object storage).
- `generation` — optional. `strategy` selects case provenance: `skill` (default — agent authors from skill analysis; needs no block), `synthetic` (LLM generates from seeds), or `from-traces` (extracted from MLflow traces). For `synthetic`: `context` holds repository knowledge injected into every prompt, and `seeds` is a list where each has a `category`, a `count`, and exactly one prompt discriminator (mirroring judges): `builtin` (from `agent_eval/prompts/`, e.g. `docs/navigation`), `prompt_file` (project path), or inline `prompt`. Each seed's `category` is stamped onto cases as `annotations.category` (derived, never declared). `seeds`/`context` apply only to `synthetic`. Whether `/eval-dataset` creates a fresh set or augments is derived from the existing dataset — there is no `--strategy` flag.
- `inputs.tools` — tool interception: `match` describes what to intercept, `prompt` how to handle it. AskUserQuestion uses 3-tier answering: exact `case_overrides` → LLM call (`models.hook`) with case context (`input.yaml` + `answers.yaml`) → fallback
- `outputs` — list of artifact dirs (`path`) and/or tool calls (`tool`) with natural language schemas. Optional `batch_pattern` maps output files to cases in batch mode using `{n}` as a 1-based index
- `traces` — execution data to capture: stdout/stderr, events, metrics (exit code, tokens, cost)
- `judges` — `builtin` reusable judges (from `agent_eval/judges/`), inline `check` scripts, LLM `prompt`/`prompt_file` (Jinja2 rendered), `llm_rubric`, external `module`/`function`. Optional `arguments` dict for parameterization. Optional `if` condition to skip judges per case based on annotations. Optional `verdict` (`auto` | `json_schema` | `tool`) overrides `models.judge_verdict` for one judge. Judges receive `outputs["annotations"]` from dataset `annotations.yaml`. `feedback_type: bool` selects a pass/fail verdict, anything else a numeric `score` on `score_range` — a declared range is stated in the LLM judge's prompt and tool schema and enforced on the returned value of every judge type (off-scale → error sample, dropped from the mean, never clamped), and omitting it on a numeric LLM/agent judge warns at config load. Rejected at load: `bool` with a `score_range`, `int` with fractional bounds, a non-`bool` `feedback_type` or any `score_range` on a **builtin LLM** judge, unknown `builtin:` names. Optional `examples` (`{source: reviews, count, mix}`) on LLM/agent judges injects few-shot exemplars harvested from prior runs' `review.yaml` human labels (`agent_eval/examples.py`; the case being judged is never its own anchor) — rejected on check/builtin/code judges and on the reserved `pairwise` judge.
- `thresholds` — per-judge regression detection. Valid keys: `min_mean`, `min_pass_rate`, `min_win_rate`, `max_error_rate` (opt-in coverage gate — the other three are computed over the cases that produced a value, so errors shrink the sample rather than lowering the metric; a case in `incomplete_cases` — evidence withheld from the judge — blocks these and `--baseline`).
- `reward` — optional. Collapses per-judge results into a single scalar in `[0, 1]` for RL training (GRPO). Two mutually exclusive modes: `judge` names a single judge whose value is the reward (clamped to `[0, 1]` as-is, or set `normalize: true` to map it from `score_range`; `gate` defaults to false here), validated against the defined judges at config load. Otherwise `formula` composes from multiple judges: `weighted` (weighted sum of `weights`) or a Python `<expression>` over judge names (allowed calls: `min`/`max`/`abs`/`round`/`sum`/`len`/`mean`, AST-validated at config load). `gate: true` zeros the reward on any false boolean judge — it gates on *every* boolean judge, so expressions using booleans as their own gate want `gate: false`. Resolution order: `reward:` section → default (boolean gates + averaged normalized numerics). On both paths a numeric judge is normalized over its own declared `score_range` — except `raw` judges and a single `judge` without `normalize`, which are clamped instead. The deprecated `reward.score_range` only covers composed judges that declare no range of their own (`[1, 5]` if it too is absent), and warns at load once one of them declares a different range.

Runs are stored in `$AGENT_EVAL_RUNS_DIR` (default `eval/runs`), configured during `/eval-setup`.

The `schema` descriptions are documentation for the LLM agents and judges. Scripts operate on file paths from eval.yaml directly — no extraction spec, no hardcoded field names.

## Usage

### Skill Mode Workflow
```
/eval-setup                            # Setup: dependencies, MLflow, API keys
/eval-analyze --skill my-skill         # Analyze: understand skill, generate eval.yaml
/eval-dataset                          # Dataset: generate test cases
/eval-run --model opus                 # Run: execute eval suite
/eval-review --run-id <id>             # Review: interactive human feedback + changes
/eval-mlflow --run-id <id>             # MLflow: sync dataset, log results
/eval-optimize --model opus            # Optimize: automated refinement loop
/eval-compare <input-dir>              # Compare: tabbed HTML report across models/runs
/eval-anova --config eval.yaml         # DoE: matrix of configs → runs → ANOVA + comparison
```

### eval-anova / eval-compare relationship

eval-run runs one condition. `/eval-anova` reads a `matrix:` block and fans out `/eval-run`
per cell (condition × replication) → standard runs, then computes ANOVA/Pareto over their
`summary.yaml` into `anova.json`. `/eval-compare` renders the cross-condition comparison and
surfaces the statistics section when `anova.json` is present — but stays standalone (no stats
dependency) for manual/CI-produced run sets.

### Prompt Mode Workflow (Agentic Documentation Testing)
```text
/eval-setup                            # Setup: dependencies, MLflow, API keys
/eval-analyze --prompt examples/openshift-agentic-docs.md # Analyze: analyze docs, generate synthetic-generation eval.yaml
/eval-dataset                          # Dataset: generate test cases from templates
/eval-run --model sonnet               # Run: test agent against documentation
/eval-review --run-id <id>             # Review: analyze documentation effectiveness
/eval-mlflow --run-id <id>             # MLflow: sync dataset, log results
```

## Tests

```bash
python3 -m pytest tests/ -v                # Unit tests only (e2e skipped by default)
python3 -m pytest tests/e2e/ -v -s -m e2e  # E2E tests (requires ANTHROPIC_API_KEY, ~$0.50)
python3 -m pytest tests/ -v -s -m ""       # Everything
```

E2E tests invoke real Claude API calls against a fake Jira skill fixture to verify the eval-analyze/eval-dataset pipeline. Use `-s` to see real-time progress from the runner.

## Key Design Decisions

1. **Schema-driven** — dataset and output structures described in natural language in eval.yaml; agents and judges interpret them, scripts just move files
2. **Agent-agnostic runner** — `EvalRunner` ABC with `--agent` flag on execute.py; Claude Code included, extensible to OpenCode/Agent SDK
3. **Four judge types** — `builtin` reusable judges, inline `check` scripts, LLM `prompt`/`prompt_file`, external `module`/`function`
4. **MLflow as separate skill** — `/eval-mlflow` handles dataset sync, result logging, trace feedback; eval-run works without it

## Brainstorms

The `brainstorm/` directory contains exploratory ideas and design thinking. These are just ideas, not reflected in the code. Do not treat brainstorm content as implemented features or current architecture. Implemented brainstorms are moved to `brainstorm/attic/`.

## Remaining Work

- CI integration patterns and examples

## Execution Paths

The same `eval.yaml` works unchanged across three parallel execution paths.
The execution substrate is a CLI flag, never in the eval config.

### Local (`/eval-run` or `agent-eval run`)
Process-level execution — the harness invokes the agent CLI directly, collects
artifacts, scores with judges, generates the report. No containers.

### Harbor (`harbor run` or `/eval-run --runner harbor`)
Containerized execution via [Harbor](https://github.com/laude-institute/harbor).
Self-contained task packages (from `/eval-dataset`) carry instruction, inputs,
tool interception, and the verifier (judge engine as `reward.json`). Any Harbor
agent (claude-code, opencode, codex, etc.) runs them directly — no custom agent
wrapper. Environments: Podman (local) and Kubernetes (OpenShift). See
`deploy/harbor/README.md`.

### EvalHub (platform-triggered)
The `agent_eval.evalhub` adapter runs the eval **in-process** inside the Job pod
created by EvalHub's server — matching EvalHub's architecture where adapter pods
are execution-only (no sub-pod creation). Uses `ClaudeCodeRunner` directly, not
Harbor. In-process parallelism handles concurrent cases within the pod.

- Implements `FrameworkAdapter` from `eval-hub-sdk`
- Downloads test cases from S3 via `s3_dataset.py`
- Maps `RunResult` + judge scores to `JobResults` via `results_mapper.py`
- Ships as a UBI9 container image (`deploy/evalhub/Containerfile`)

## Container Images

| Image | Containerfile | Used by |
|---|---|---|
| `agent-eval-harness` | `deploy/Containerfile` | Trial pods (Harbor), EvalHub Job pods (base) |
| `agent-eval-hub` | `deploy/evalhub/Containerfile` | EvalHub provider (FROM base + eval-hub-sdk) |

No project-specific images needed. Project resources via ConfigMap (K8s),
bind-mount (Podman), or `FROM agent-eval-harness` in the project's own repo.

<!-- SPECKIT START -->
For additional context about the current feature work, read `specs/005-eval-directory-layout/plan.md`
<!-- SPECKIT END -->
