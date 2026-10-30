<p align="center">
  <img src="website/assets/images/aeh-logo.png" alt="Agent Eval Harness" width="96"/>
</p>

<p align="center">
  <span style="display:inline-flex;align-items:center;gap:0.85rem;color:#888;font-size:12px;letter-spacing:0.1em;text-transform:uppercase;font-family:system-ui,sans-serif;">
    Made at&nbsp;<img src="website/assets/images/redhat-logo.svg" alt="Red Hat" height="22"/>
  </span>
</p>

<h1 align="center">Agent Eval Harness</h1>

<p align="center"><em>Make agent performance measurable — and improvable</em></p>

<p align="center">
  <a href="https://fullsend-ai.github.io/agent-eval-harness/"><img src="https://img.shields.io/badge/docs-live-7c5cff" alt="docs"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="python">
  <img src="https://img.shields.io/badge/license-Apache--2.0-informational" alt="license">
  <img src="https://img.shields.io/badge/claude-plugin-7c5cff" alt="claude plugin">
  <img src="https://img.shields.io/badge/mlflow-traces-orange" alt="mlflow">
  <img src="https://img.shields.io/badge/harbor-containers-success" alt="harbor">
</p>

**Agent Eval Harness** evaluates skills and agent capabilities with one declarative
`eval.yaml`: analyze → generate cases → run → judge → trace in MLflow → optimize.
Same config on your laptop, Harbor containers, or EvalHub.

<p align="center">
  <a href="https://fullsend-ai.github.io/agent-eval-harness/">Docs</a> ·
  <a href="https://fullsend-ai.github.io/agent-eval-harness/get-started/">Get started</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#execution-model">Execution model</a>
</p>

## Why Agent Eval Harness

- **One config everywhere.** `eval.yaml` drives local runs, Harbor, and EvalHub.
- **Skill or prompt mode.** Test packaged skills, or agent capabilities directly
  (including agentic documentation checks).
- **Robust scoring.** LLM + code judges, pairwise A/B, thresholds, and HTML reports.
- **MLflow-native traces.** Opt-in experiments, datasets, and hierarchical GenAI traces.
- **Close the loop.** `/eval-optimize` proposes skill fixes from failures and re-runs,
  or, with `--engine capevolve` (`/eval-setup --optimize`), runs a statistically
  validated search instead: train/val/test splits, a significance gate, a sealed
  test score.

## Execution Model

The harness separates **how many invocations** (`execution.mode`) from **what to execute** (`execution.skill` or `execution.prompt`):

### Execution Mode
- **case**: One invocation per test case (default). The harness loops over cases.
- **batch**: One invocation for all cases via batch.yaml. The skill/agent loops internally.

### What to Execute
- **Skill mode** (`execution.skill`): Test predefined skill implementations (`/my-skill --args`). Evaluates skill correctness, quality, and cost efficiency.
- **Prompt mode** (`execution.prompt`) ✨ NEW: Test agent capabilities directly by sending prompts without a skill wrapper. Extensible to any agent evaluation scenario.

**Implemented flavor - Agentic Documentation Testing** (see `examples/openshift-agentic-docs.md`):
- **Documentation effectiveness**: Can agents navigate and use your docs?
- **Pattern understanding**: Can agents identify and apply code patterns?
- **Constraint compliance**: Do agents respect documented rules?
- **API usage**: Can agents correctly use APIs from documentation alone?

**Extensible to other scenarios**:
- Code generation from specifications
- API usage pattern validation  
- Reasoning trace quality assessment
- Custom agent capability benchmarks

Useful for testing documentation quality (CLAUDE.md, AGENTS.md, ai-docs/), onboarding effectiveness, and establishing agent baseline capabilities.

## Quick Start

### 1. Add to your project

Install from the [skills registry](https://github.com/opendatahub-io/skills-registry):

```bash
claude plugin install agent-eval-harness@opendatahub-skills
```

Or clone and load as a local plugin:

```bash
git clone https://github.com/fullsend-ai/agent-eval-harness
pip install -e ./agent-eval-harness
claude --plugin-dir ./agent-eval-harness
```

This makes all eval skills available: `/eval-setup`, `/eval-analyze`, `/eval-dataset`, `/eval-run`, `/eval-review`, `/eval-mlflow`, `/eval-optimize`, `/eval-compare`, `/eval-anova`, and `/eval-check`.

### 2. Set up environment

```
/eval-setup
```

This checks dependencies, configures MLflow, verifies API keys, and creates directories.

### 3a. Analyze your skill (skill mode)

```bash
/eval-analyze --skill my-skill
```

This examines the skill's SKILL.md, discovers test cases, and generates `eval.yaml` with:
- `execution.mode: case` or `batch`
- Natural language `schema` descriptions of your dataset and outputs
- Suggested judges (inline checks + LLM quality assessment)
- Regression thresholds

### 3b. Analyze for prompt mode evaluation

```bash
/eval-analyze --prompt examples/openshift-agentic-docs.md
```

This analyzes your repository's documentation (CLAUDE.md, AGENTS.md, ai-docs/) and generates `eval.yaml` with:
- `execution.prompt: "{{ input.prompt }}"` (prompt mode)
- A `generation:` block with builtin documentation prompts (`docs/navigation`, `docs/anti-pattern`, etc.)
- LLM rubric judges for semantic evaluation
- Documentation tracking to verify agents use docs correctly

**Note**: Prompt mode is extensible. The OpenShift analysis prompt is a domain-specific example. You can create custom analysis prompts for other domains or agent capability testing scenarios.

### 4. Generate test cases (if needed)

```
/eval-dataset
```

Creates 5 starter test cases based on the skill analysis. Skip this if you already have cases.

### 5. Run evaluation

```
/eval-run --model opus
```

This prepares a workspace, runs the skill (headless or interactive), collects artifacts, scores with judges, and reports results. To run the same eval against an open model through OpenRouter, export `OPENROUTER_API_KEY` and pass `--model openrouter:/<author>/<slug>` (see the [OpenRouter guide](website/guides/openrouter.md)).

## eval.yaml

The harness uses natural language to describe evaluation datasets and skills input/output and spawns LLM sub-agents to interpret them.

```yaml
name: my-skill-eval
description: Evaluate the main skill pipeline

# Execution — what to run and how (runner-agnostic)
execution:
  mode: case              # case (per-case invocation) or batch (single invocation)
  skill: my-skill-name    # skill mode; use `prompt:` instead for prompt mode
                          # (direct agent invocation, no skill wrapper)
  arguments: "{prompt}"   # resolved per case from input.yaml fields
  # timeout: 3600            # Wall-clock timeout in seconds per invocation
  # max_budget_usd: 5.0      # Cost cap in USD per invocation
  # parallelism: 3            # Run up to N cases concurrently (case mode only)
  # env:                     # Inject env vars into workspace settings
  #   JIRA_SERVER: http://localhost:8080   # Literal value
  #   JIRA_TOKEN: $JIRA_TOKEN              # $VAR resolved from caller's env
  # project_hooks: true      # Carry the project's .claude/settings.json hooks
                             # into each workspace (default); false = harness hooks only
  # symlinks: [types]        # Extra top-level project entries to link into each workspace

# Runner — agent harness + runner-specific knobs
runner:
  type: claude-code          # claude-code | cursor | codex | fullsend | cli | responses-api
  # effort: high              # Claude: low..max; Codex: minimal..xhigh
  # settings: {}              # Arbitrary Claude Code settings merged into workspace
  # plugin_dirs: []           # Directories to load plugins from
  # env:                       # Extra env vars for subprocess ($VAR resolves from caller)
  #   CUSTOM_AUTH_TOKEN: "$CUSTOM_AUTH_TOKEN"
  # system_prompt: |          # Appended to Claude CLI system prompt
  #   Custom instructions for the skill run.

# Models — defaults for each role (CLI flags override)
models:
  skill: claude-opus-4-6
  judge: claude-opus-4-6
  # hook: claude-sonnet-4-6  # Model for LLM-based AskUserQuestion answering

# MLflow logging target (optional)
mlflow:
  experiment: my-skill-eval

# Permissions — tool access during headless execution
permissions:
  allow: []            # Tool patterns to allow (empty = all)
  deny:
    - "mcp__*"         # Block MCP tools during eval

# Dataset — where test cases live and what they look like
dataset:
  path: eval/dataset/cases
  schema: |
    Each case directory contains:
    - input.yaml: YAML file. The 'prompt' field is the main input to
      the skill. Optionally 'context' with additional context.
    - reference.md: Gold standard output for comparison scoring.

# Inputs — tool interception for headless/interactive execution
# AskUserQuestion uses 3-tier answering: exact case_overrides →
# LLM call (models.hook) with input.yaml + answers.yaml context → fallback
inputs:
  tools: []
  # - match: Questions asked to the user via AskUserQuestion.
  #   prompt: |
  #     Answer based on test case context in input.yaml and answers.yaml.
  #     Default to "yes" for confirmations.
  # - match: |
  #     Any interaction with Jira — MCP tools or scripts.
  #   prompt: |
  #     Block production Jira. Only allow test instances.

# Outputs — what the skill produces (files on disk or tool calls)
outputs:
  # File artifacts on disk
  - path: artifacts
    # batch_pattern: "RFE-{n:03d}"  # Map output files to cases in batch mode
    schema: |
      One markdown file per case, named NNN-slug.md where NNN is the
      case number (001, 002, ...).

  # Tool call outputs (for side effects like API calls)
  # - tool: mcp__atlassian__create_issue
  #   schema: |
  #     Creates a Jira issue with title, description, priority.

# Traces — execution data to capture for judges
traces:
  stdout: true     # Capture stdout.log
  stderr: true     # Capture stderr.log
  events: true     # Execution events: tool calls, reasoning, results (default: true)
  metrics: true    # Capture exit code, tokens, cost, duration

# Judges — evaluate output quality
judges:
  # Inline code check
  - name: has_content
    description: |
      Check that the generated output is non-empty and has at least
      100 characters of content.
    check: |
      content = outputs["main_content"]
      if len(content.strip()) < 100:
          return False, f"Output too short ({len(content.strip())} chars)"
      return True, f"Output has {len(content.strip())} chars"

  # LLM judge with inline prompt (conditional — skipped when condition is false)
  - name: completeness
    if: "not annotations.get('skip_completeness', False)"  # Skip based on annotations
    feedback_type: int
    score_range: [1, 5]      # declare the scale — omitting it warns at config load
    description: |
      How completely the output covers the reference — one criterion;
      clarity and accuracy belong to other judges.
    prompt: |
      How completely does the generated output cover the reference?

      {{ outputs }}

      Score 1-5: 1 = most requirements missing, 3 = covers the basics
      with gaps, 5 = every requirement addressed.

  # LLM judge with prompt file and supplementary context
  # - name: detailed_quality
  #   description: Detailed quality assessment with rubric
  #   prompt_file: eval/prompts/quality-judge.md
  #   context:
  #     - eval/prompts/scoring-rubric.md
  #     - eval/prompts/domain-guidelines.md

  # External code judge (for complex validation)
  # - name: schema_valid
  #   description: Validate output schema
  #   module: eval.judges.schema_checks
  #   function: check_schema

  # Execution efficiency check (uses trace metrics)
  # - name: cost_reasonable
  #   description: Verify cost stays under $0.50 per case
  #   check: |
  #     cost = outputs.get("cost_usd", 0)
  #     if cost and cost > 0.50:
  #         return False, f"Cost ${cost:.2f} exceeds limit"
  #     return True, f"Cost ${cost:.2f}"

  # Tool call check (uses tool outputs)
  # - name: jira_created
  #   description: Verify the skill created a Jira issue
  #   check: |
  #     calls = outputs.get("tool_calls", [])
  #     jira = [c for c in calls if "create_issue" in c.get("name","")]
  #     if not jira:
  #         return False, "No Jira issue created"
  #     return True, "Created issue"

  # Pairwise comparison judge
  # - name: pairwise
  #   description: Compare two runs and pick the better output
  #   prompt_file: eval/prompts/comparison-judge.md
  #   # model: <model-id>   # Optional override; default is models.judge

# Thresholds for regression detection
thresholds:
  completeness:
    min_mean: 3.5            # Minimum average score
  # has_content:
  #   min_pass_rate: 1.0     # Minimum fraction of cases passing (0.0–1.0)
  # pairwise:
  #   min_win_rate: 0.6      # Minimum pairwise win rate
```

### Key concepts

- **`execution`** — `mode` determines how evaluation runs:
  - **`case`** (default, skill mode): Skill invoked once per test case with `{field}` placeholders resolved from each case's input.yaml
  - **`batch`** (skill mode): All cases bundled into batch.yaml for a single skill invocation
  - **`prompt`** (prompt mode): Agent receives prompts directly without a skill wrapper, useful for testing agent capabilities like documentation navigation, pattern understanding, constraint compliance, etc.
  
  Additional fields: `arguments` template, optional `timeout` (wall-clock seconds per invocation), `max_budget_usd` (cost cap per invocation), `parallelism` (run up to N cases concurrently in case/prompt modes), `env` for injecting environment variables into workspaces (`$VAR` syntax resolves from caller's environment), and `project_hooks` (default `true`): the project's `.claude/settings.json` `hooks` are appended to each workspace's settings after the harness's own `SubagentStop`/`PreToolUse` hooks, so a skill keeps the hooks it has in production (a `SessionStart`/`compact` recovery banner, a `Stop` guard). Hook commands run with the workspace as cwd, so a relative command must name a file the harness links into the workspace (`scripts/`, `.claude/`, …); set `project_hooks: false` to run with the harness hooks only. Local workspaces only — a Harbor task package carries `eval.yaml` permissions, not the project's settings file. `symlinks` (default `[]`) lists extra top-level project entries linked into each local workspace after the defaults (`scripts`, `.claude`, `CLAUDE.md`, `.context`, `skills`) — single names, not names the harness writes into a workspace (rejected at load); a missing entry or one resolving outside the project root and `runner.plugin_dirs` is skipped with a warning, and one whose workspace path already exists fails staging. Use `dataset.workspace.files` `{dest, source}` instead for staging that must also work under Harbor or EvalHub.
- **`schema`** — natural language description of structure. Used on `dataset` and each `outputs` entry. Agents and judges read these to understand the data.
- **`dataset.workspace.files`** — whitelist of companion files copied into each case workspace (case mode). Each entry is either a per-case path (a string relative to the case dir) or a shared `{dest, source}` mapping — a project/plugin resource (`source` resolved against the project root or `runner.plugin_dirs`) materialized as a real file, never a symlink, into every case at `dest`. The shared form ports unchanged across local, Harbor, and S3/EvalHub (materialize before S3 upload with `skills/eval-dataset/scripts/export_s3.py`); prefer it over committing a symlink into a case.
- **`generation`** — optional top-level block selecting case **provenance** via `strategy`: `skill` (default — agent authors from skill analysis; needs no block), `synthetic` (LLM generates from seeds), or `from-traces` (extracted from MLflow production traces). For `synthetic`: `context` holds repository-specific knowledge (`documentation_structure`, `constraints`, `apis`, `components`, etc.) injected into every generation prompt, and `seeds` is a list where each seed has a `category`, a `count`, and exactly one **generation prompt** discriminator (mirroring judges): `builtin` (from `agent_eval/prompts/`, e.g. `docs/navigation` — discover with `list_prompts.py`), `prompt_file` (a project path), or an inline `prompt`. Each seed's `category` is stamped onto generated cases as `annotations.category`. `seeds`/`context` apply only to `synthetic`.
- **`inputs.tools`** — tool interception for headless and interactive execution. Each entry has a `match` (what to intercept) and a `prompt` (how to handle it). AskUserQuestion uses 3-tier answering: exact `case_overrides` → LLM call (`models.hook`) with case context (`input.yaml` + `answers.yaml`) → fallback to first option.
- **`outputs`** — two types: `path` for file artifacts on disk, `tool` for tool call side effects (Jira, APIs). Both have `schema` descriptions. Optional `batch_pattern` maps output files to cases in batch mode using `{n}` as a 1-based index (e.g. `"RFE-{n:03d}"` → `RFE-001`, `RFE-002`).
- **`traces`** — execution data to capture: stdout/stderr logs, events (tool calls, reasoning text, results), metrics (exit code, tokens, cost, duration). Available to judges via the `outputs` dict.
- **`check`** — inline Python snippet for deterministic validation. Receives an `outputs` dict with file contents, execution metadata, tool calls, logs, and `annotations` (from dataset `annotations.yaml`). Returns `(bool, str)`.
- **`if`** — optional condition on a judge. Python expression evaluated against `annotations` and `outputs`. When false, the judge is skipped for that case (not counted in pass_rate or mean).
- **`prompt`** / **`prompt_file`** / **`llm_rubric`** — LLM judge evaluation instructions. All three compile to the same internal prompt before Jinja2 rendering. Priority order: `llm_rubric` > `prompt` > `prompt_file`.
  - **`llm_rubric`**: Syntactic sugar for simple criteria. Auto-appends `{{ conversation }}` template if missing. Best for synthetic-generation configs. Example: `llm_rubric: "Agent cited documentation sources"`
  - **`prompt`**: Full Jinja2 template with manual control. Use for complex logic or multiple placeholders like `{{ outputs }}`, `{{ conversation }}`, `{{ tool_trace }}`, `{{ inputs }}`, `{{ evidence }}`.
  - **`prompt_file`**: External file path (absolute or relative to project root). Use for sharing prompts across judges. File can contain rubric-style or full template content.
- **`context`** — list of file paths loaded and appended to the LLM judge prompt as supplementary material (rubrics, guidelines, examples).
- **`module`** / **`function`** — external Python code judge for complex validation.
- **`feedback_type`** / **`score_range`** — the judge's verdict shape and numeric scale. `feedback_type: bool` gives a pass/fail verdict; anything else gives a `score` on `score_range`. A **declared** scale reaches the model — it is stated in the judge's system prompt and in the verdict schema (a structured output or the `submit_score` tool) — and the returned value is enforced against it: a value outside the scale is recorded as an error sample rather than clamped, because clamping a 4 from a 0-2 judge into a 2 invents a perfect score. Omit the range and the judge is only *told* `[1, 5]`, with nothing checking the answer — so a numeric LLM or agent judge without one warns at config load. Incoherent combinations (`bool` with a `score_range`, `int` with fractional bounds, a non-`bool` `feedback_type` or any `score_range` on a builtin LLM judge) and unknown `builtin:` names fail at config load.

  > **Upgrading:** LLM judges with a non-default `score_range` were previously asked for a `[1, 5]` score regardless of what they declared, and nothing checked the answer; agent judges *were* told their declared range, but an off-scale verdict was silently clamped into it. Both are now asked for, and held to, the scale they declare. How much a mean moves depends on the rubric: a prompt that already stated "0-2" was often answered on 0-2 anyway, since the rubric text competes with the system prompt and usually wins — in one 1,449-sample corpus every value was already on the declared scale. A rubric that left the scale to the config will move more, and in either direction. Two further changes affect existing runs: an off-scale value is now an **error sample** rather than a number, so a judge whose model ignores its scale can end up with no mean at all; and *every* reward composition — the default one and an explicit `reward:` block alike — now normalizes a numeric judge over its own `score_range` instead of a flat `[1, 5]` (judges listed in `reward.raw`, and a single-judge reward without `normalize: true`, are clamped as before), which shifts `reward.json` and `anova.json`. A `[0, 2]` judge scoring 0/1/2 used to compose to `0.0`/`0.0`/`0.25` and now composes to `0.0`/`0.5`/`1.0`. `reward.score_range` is deprecated by the same change: it is now only a fallback for composed judges that declare no range of their own, and writing it warns at config load once one of them declares a different range. Two smaller ones: in the default path a case whose every scoring judge errored now composes to `0.0` rather than `1.0`, and an LLM response with no parseable score is now an error sample rather than the old silent `3`. Re-baseline any `thresholds.min_mean` tuned against the old numbers, and do not compare pre- and post-upgrade runs. A live GRPO loop needs the same treatment: rewards are on a different scale after the upgrade, so re-baseline it rather than continuing against rollouts collected before.

  > **Upgrading — scores move:** LLM judges on Anthropic, OpenAI and OpenRouter now ask the provider for the verdict as a structured output (`output_config.format` / `response_format`) instead of a forced tool call, which is kept as the fallback. The judge can now think before it writes its rationale and verdict, so a mean can shift in either direction. Pin `models.judge_verdict: tool` (or a judge's own `verdict: tool`) to keep the previous contract on a judge model that still accepts forced tool use, and re-judge a baseline before comparing across the boundary; do not compare runs judged under the two transports without re-baselining `thresholds`. The `anthropic[vertex]` floor also rises to `>=0.77`.
- **`agent`** — turns a judge into a tool-using **agent judge**: instead of a single stateless model call, the judge runs as an agent *through the runner abstraction*, with read-only file tools and a staged, isolated workspace, so it can Read/Grep/Glob the material and any reference docs to **ground its verdict** (e.g. verify architecture claims against the real docs) instead of guessing from prompt text. An agent judge still takes its instructions from `prompt`/`prompt_file`/`llm_rubric`, and reuses `model`, `feedback_type`, `score_range`, `samples`, `if`, and `thresholds` like any other judge. The presence of an `agent:` block is what upgrades an otherwise-LLM judge. Sub-keys (all optional):
  - **`runner`** — a per-judge runner block, parsed exactly like the top-level `runner:` (`type`, `effort`, `command`, `env`, …). Defaults to `{type: claude-code}`. Lets the judge use a different runner/model stack than the skill-under-test.
  - **`allowed_tools`** — tool allowlist for the judge. Default `[Read(**), Edit(output/**)]`: read anywhere in the staged workspace, write nothing but the verdict file. On the Claude Code runner the judge runs **deny-by-default** (`--permission-mode manual --permission-prompts none`), so a call the policy does not cover is refused rather than silently executed. That gate is about *scope*, not about enumeration: the read-only tools still work inside the working directory whether or not the list names them, while anything reaching outside it — or any write, shell or network call — is refused. Two rule-syntax traps, both measured: a **bare** `Read`/`Grep`/`Glob` *widens* the judge (the CLI confines reads to the working directory until you name the bare tool, which removes that), and **`Write(...)` matches nothing** — the Write tool is governed by `Edit(<path>)` rules. Add `Bash` only under a sandboxed runner. `[]` is treated as unset.
  - **`denied_tools`** — tools the judge may never use. Defence in depth behind the deny-by-default mode, and the only control on runners that have no equivalent of it. Default `[Bash, WebFetch, WebSearch, Agent, Edit, MultiEdit, NotebookEdit]`, minus anything `allowed_tools` names *and the rest of that tool's capability group* (`Write`/`Edit`/`MultiEdit`/`NotebookEdit`, and `Agent`/`Task`) — a deny beats an allow, so leaving `Bash` denied would take away the tool an `allowed_tools: [Bash(pytest:*)]` asks for, and leaving `Edit` denied is a `Write(**)` deny on Cursor that takes the verdict file away. With the default allow list the effective deny is therefore `[Bash, WebFetch, WebSearch, Agent]`. A list you write yourself replaces the default outright, with **no** subtraction applied: the rule you wrote is the one that wins. `[]` disables the deny list entirely. Keep `Bash` denied: merely leaving it unlisted is weaker, because a built-in classifier still auto-approves `cat`, `grep`, `echo` and friends, whereas a deny de-registers the tool.
  - **`context`** — dirs/files copied into the judge workspace under `./.context/<name>` for the agent to consult; a copy, so nothing the judge writes reaches the originals (distinct from the top-level `context:`, which is appended to the prompt text).
  - **`inputs`** — which collected output dirs (by `outputs[].path` name) to stage as files; default: all declared output dirs and `_modified/`. Use `[.]` to stage everything.
  - **`timeout`** — seconds (default: `execution.timeout` or harness default). **`max_budget_usd`** — per-judge-run cap (default `2.0`).

  The judge writes its verdict to `./output/score.json` — `{"score": <number>, "rationale": "…"}` (numeric) or `{"passed": <bool>, "rationale": "…"}` (bool); `feedback_type` selects which, and `score_range` states the scale in the prompt, bands the score in the report, and records an off-scale verdict as an error sample rather than counting it. The harness appends this output contract (plus an untrusted-data guard) to the prompt automatically, so rubric authors write only the criteria. If `score.json` is absent, the harness falls back to parsing the last `{"score"|"passed", …}` JSON object from the run's stdout; if neither yields a value it records an error sample rather than silently passing. Example:

  ```yaml
  judges:
    - name: architecture_score
      prompt_file: eval/prompts/architecture-agent-judge.md
      model: claude-opus-4-8
      feedback_type: int
      score_range: [0, 2]
      samples: 3
      agent:
        runner: {type: claude-code, effort: high}   # optional; defaults to claude-code
        allowed_tools: [Read(**), Edit(output/**)]  # the default; see below
        context: [.context/architecture-context]    # copied under ./.context/
        inputs: [strat-tasks]                        # which output dirs to stage (default: all)
        timeout: 420
        max_budget_usd: 2.0
  ```
- **`permissions`** — tool access patterns (`allow`/`deny`) for headless execution. Claude Code enforces these exactly. Codex maps `runner.permission_mode` to its filesystem sandbox and warns because it cannot translate Claude's tool-level patterns.
- **`runner`** — `type` selects `claude-code`, `cursor`, `codex`, `fullsend`, `cli`, or `responses-api` (an unknown type is rejected at config load); remaining fields are runner-specific. `fullsend` runs the skill through `fullsend run <agent>` (local backend only) and needs `runner.agent`. Cursor appends `runner.effort` to base model IDs, preserves IDs that already encode an effort variant, supports isolated and `workspace_mode: repo` execution, and enforces a *subset* of file permissions through its project-local CLI config — rules Cursor cannot express (some tools, path-scoped allows) are dropped with a warning and are **not** security boundaries, so do not rely on them to protect answer keys in `workspace_mode: repo`; it still rejects `inputs.tools`. Codex accepts `minimal`, `low`, `medium`, `high`, and `xhigh`; its CLI does not enforce `max_budget_usd`. Local Codex defaults to `workspace-write`; `permission_mode: plan` maps to `read-only`, while `bypassPermissions` maps to Codex's unrestricted bypass and should be used only inside a container or VM.
- **`models`** — `skill`/`subagent`/`judge`/`hook` defaults, overridable per-judge or via CLI flags. `hook` is the model used for LLM-based AskUserQuestion answering. `judge_verdict` (`auto` | `json_schema` | `tool`, default `auto`) selects how SDK-backed LLM judges carry their verdict: the provider's structured output first, the forced tool call as the fallback; a judge's own `verdict:` overrides it.
- **`mlflow`** — `experiment` (and optional `tracking_uri`/`tags`) for result logging.
- **`thresholds`** — per-judge regression detection. Valid keys: `min_mean` (minimum average score), `min_pass_rate` (minimum fraction of cases passing, 0.0–1.0), `min_win_rate` (minimum pairwise win rate), `max_error_rate` (**maximum** fraction of cases the judge may error on, 0.0–1.0 — an opt-in coverage gate; the other three are computed over the cases that produced a value). A case where the judge was denied material it should have graded is the exception: it counts in `incomplete_cases` and blocks every score threshold and baseline comparison on its own.

## Example: eval.yaml for RFE Creator

```yaml
name: rfe-creator
execution:
  mode: batch
  skill: rfe.speedrun
  arguments: "--input batch.yaml --headless --dry-run"
runner:
  type: claude-code
models:
  skill: claude-opus-4-6
  judge: claude-opus-4-6
mlflow:
  experiment: rfe-eval
permissions:
  deny: ["mcp__atlassian__*"]  # Block Jira writes during eval

dataset:
  path: eval/dataset/cases
  schema: |
    Each case directory contains:
    - input.yaml: YAML file. The 'prompt' field is the problem statement
      to send to the skill. 'clarifying_context' has additional context.
    - reference-rfe.md: Gold standard RFE (markdown with YAML frontmatter:
      rfe_id, title, priority, size, status).
    - reference-review.md: Gold standard review (markdown with YAML
      frontmatter: score 0-10, pass bool, recommendation, feasibility,
      per-criterion scores: what, why, open_to_how, not_a_task,
      right_sized each 0-2).
    - annotations.yaml: Expected scores and test metadata.

inputs:
  tools:
    - match: Questions asked to the user via AskUserQuestion.
      prompt: |
        Answer based on the test case. If asked about priority,
        say "Normal". If asked to confirm, say "yes".
    - match: |
        Any interaction with Jira — via MCP tools (mcp__atlassian__*)
        or scripts that import jira-python or call the Jira REST API.
      prompt: |
        Block production Jira. Only allow if JIRA_SERVER points to
        a test instance or jira-emulator.

outputs:
  - path: artifacts/rfe-tasks
    schema: |
      One markdown file per case, named RFE-NNN-slug.md where NNN is
      the case number (001, 002, ...). Contains YAML frontmatter with
      rfe_id, title, priority, size, status.
      Skip files ending in -comments.md or -removed-context.md.
  - path: artifacts/rfe-reviews
    schema: |
      One review file per case, named RFE-NNN-slug-review.md. Contains
      YAML frontmatter with score, pass, recommendation, feasibility,
      and per-criterion scores.

traces:
  metrics: true

judges:
  - name: frontmatter_valid
    description: |
      Validate that each generated RFE has valid YAML frontmatter with
      required fields: rfe_id, title, priority, status.
    check: |
      import yaml
      task = outputs["rfe-tasks_content"]
      if not task.startswith("---"):
          return False, "No YAML frontmatter"
      fm = yaml.safe_load(task.split("---", 2)[1])
      required = ["rfe_id", "title", "priority", "status"]
      missing = [f for f in required if f not in fm]
      if missing:
          return False, f"Missing: {', '.join(missing)}"
      return True, "All required fields present"

  - name: quality
    description: |
      Evaluate quality of the generated RFE compared to the reference.
    feedback_type: int
    score_range: [1, 5]
    prompt_file: eval/prompts/quality-judge.md
    context:
      - eval/prompts/rfe-scoring-rubric.md

  - name: cost_efficient
    description: Verify the pipeline doesn't exceed $1 per case.
    check: |
      cost = outputs.get("cost_usd", 0)
      if cost and cost > 1.0:
          return False, f"Cost ${cost:.2f} exceeds $1.00"
      return True, f"Cost ${cost:.2f}"

thresholds:
  frontmatter_valid: {min_pass_rate: 1.0}
  quality: {min_mean: 3.5}
```

## Example: eval.yaml for Architecture Context

```yaml
name: architecture-context
execution:
  skill: repo-to-architecture-summary
runner:
  type: claude-code

dataset:
  path: eval/dataset/cases
  schema: |
    Each case directory contains:
    - input.yaml: YAML file. 'repo_path' is the local path to the
      repository to analyze. 'distribution' (rhoai or odh) and
      'version' identify the platform.
    - reference-architecture.md: Gold standard architecture document
      with sections: Architecture Components, APIs, Dependencies,
      Network Architecture, Security. Claims have source references
      in file:line format.

inputs:
  tools:
    - match: Questions asked to the user via AskUserQuestion.
      prompt: |
        If asked which distribution, answer "rhoai".
        If asked which version, answer the latest.

outputs:
  - path: output
    schema: |
      A single GENERATED_ARCHITECTURE.md file per case with markdown
      sections matching the reference structure.

traces:
  metrics: true
  events: true   # Capture tool calls for source reference analysis

judges:
  - name: required_sections
    description: |
      Check that the generated architecture document contains all
      required sections.
    check: |
      content = outputs["output_content"]
      required = ["Architecture Components", "APIs", "Dependencies",
                  "Network Architecture", "Security"]
      missing = [s for s in required if s.lower() not in content.lower()]
      if missing:
          return False, f"Missing sections: {', '.join(missing)}"
      return True, f"All {len(required)} sections present"

  - name: accuracy
    description: |
      Compare the generated architecture summary against the reference.
    feedback_type: int
    score_range: [1, 5]
    prompt: |
      Compare the generated architecture summary against the reference.
      Are the same components identified? Are APIs correct?
      Are dependencies and security details accurate? Score 1-5.

thresholds:
  required_sections: {min_pass_rate: 1.0}
  accuracy: {min_mean: 3.5}
```

## Example: eval.yaml for Prompt-Based Documentation Testing

```yaml
name: docs-navigation-eval
description: Test if agents can navigate and use repository documentation
# Prompt mode — sends prompts directly to the agent (no skill wrapper)

execution:
  mode: case
  prompt: "{{ input.prompt }}"  # Resolved from input.yaml per case

runner:
  type: claude-code

models:
  skill: claude-sonnet-4-6
  judge: claude-opus-4-6

dataset:
  path: eval/dataset/cases
  schema: "input.yaml with 'prompt' (question) and 'expected_files' (docs to consult)"

# Synthetic generation: a top-level block, peer of execution/dataset/judges
generation:
  strategy: synthetic
  # Repository knowledge injected into every generation prompt
  context:
    documentation_structure:
      entry_point: CLAUDE.md
      areas:
        - path: ai-docs/
          topics: [component-docs, workflows]
    constraints:
      - rule: "All new APIs must start with v1alpha1"
        documentation: ai-docs/practices/api-evolution.md
      - rule: "Never modify files in vendor/"
        documentation: CLAUDE.md
  # Each seed picks a generation prompt via builtin / prompt_file / prompt
  seeds:
    - category: navigation
      builtin: docs/navigation          # from agent_eval/prompts/ (see list_prompts.py)
      count: 10
      description: Finding specific documentation
    - category: anti-pattern
      builtin: docs/anti-pattern
      count: 5
      description: Rejecting constraint violations

outputs:
  - path: outputs
    schema: "agent responses (markdown files)"

traces:
  stdout: true
  events: true
  metrics: true

judges:
  # Check if agent read the expected documentation
  - name: consulted_docs
    builtin: consulted_docs
    if: "annotations.get('category') == 'navigation'"
    arguments:
      min_coverage: 0.8
      match: suffix
  
  # Semantic quality assessment
  - name: answer_quality
    feedback_type: int
    score_range: [1, 5]
    llm_rubric: |
      Evaluate the agent's answer against the expected behavior.
      Score 1-5 where 5 is excellent.

thresholds:
  consulted_docs: {min_pass_rate: 0.8}
  answer_quality: {min_mean: 3.5}
```

## Skills

### /eval-setup

Set up the evaluation environment: verify dependencies, configure MLflow tracking and tracing, check API keys, create directory structure.

### /eval-analyze

Analyze a target and generate `eval.yaml`. Two modes:

**Skill mode** (`--skill`): Examines the skill's SKILL.md, discovers test cases, and produces configuration with:
- `execution.mode: case` or `batch`
- Dataset schema, output descriptions
- Suggested judges (inline checks + LLM prompts)

**Prompt mode** (`--prompt`): Uses an analysis prompt to generate evaluation config. Domain-specific analysis prompts (see `examples/`) analyze repository documentation (CLAUDE.md, AGENTS.md, ai-docs/) and produce:
- `execution.prompt: "{{ input.prompt }}"` (direct agent invocation)
- A `generation:` block with seeds (navigation, anti-pattern, authoring, component-usage, architecture)
- LLM rubric judges for semantic evaluation
- `generation.context` for test generation

```bash
/eval-analyze --skill my-skill                           # Skill mode: analyze skill implementation
/eval-analyze --skill my-skill --update                  # Update existing skill mode eval.yaml
/eval-analyze --prompt examples/openshift-agentic-docs.md # Prompt mode: analyze agentic documentation
/eval-analyze --prompt custom.md                         # Prompt mode: use custom analysis prompt
/eval-analyze --assess                                   # Batch: assess which skills would benefit from evals
```

Prompt mode is extensible. Create custom analysis prompts for other agent capability testing scenarios (code generation, API usage patterns, reasoning quality, etc.).

**Batch assessment** (`--assess`): profiles every skill in the project and classifies each as RECOMMENDED / OPTIONAL / SKIP / EXISTS, so you can decide where evals are worth building before analyzing any single skill. It ignores `--skill` and writes no config.

### /eval-dataset

Generate evaluation test cases. Case **provenance** is set in the config via `generation.strategy`:

- **`skill`** (default — no `generation` block needed): the agent authors realistic inputs from the skill analysis.
- **`synthetic`**: an LLM generates cases from `generation.seeds` + `generation.context` (from `/eval-analyze --prompt`).
- **`from-traces`**: cases are extracted from MLflow production traces.

Whether a run **creates a fresh set or augments an existing one** is derived from the current dataset (empty → fresh; populated → gap-fill) — there is no `--strategy` flag.

```bash
/eval-dataset                              # generate per the config's generation.strategy
/eval-dataset --count 20                   # target 20 cases (skill/from-traces; synthetic uses per-seed count)
/eval-dataset --run-id <id>                # augment, targeting failures from a prior eval run
```

**Synthetic generation** uses builtin generation prompts (`docs/navigation`, `docs/anti-pattern`, `docs/authoring`, `docs/component-usage`, `docs/architecture` — from `agent_eval/prompts/`) combined with repository-specific `generation.context` to create targeted test cases. Extensible with project-specific prompts via `prompt_file:` or inline `prompt:`.

### /eval-run

Execute the evaluation suite: prepare workspace, run the skill headlessly, collect artifacts, score with judges, detect regressions, and report results.

```
/eval-run --model opus                          # Run all cases
/eval-run --model opus --parallelism 3          # Run 3 cases concurrently
/eval-run --model opus --cases case-001         # Run specific case
/eval-run --model opus --baseline prev-run-id   # Compare against baseline
/eval-run --model opus --no-llm-judges          # Skip LLM judges
```

### /eval-compare

Compare evaluation results across multiple models or runs. Scans a directory of eval run artifacts (`summary.yaml`, `run_result.json`, `report.html`) and produces a self-contained tabbed HTML comparison report with model cards, quality/cost tables, per-case breakdowns, embedded per-run reports, and LLM-written analysis (Bottom Line, Where Each Model Shined, Shared Weaknesses, Recommendations).

```
/eval-compare <input-dir>                          # Discover runs and generate the report
/eval-compare <input-dir> --output <dir>           # Custom output directory
/eval-compare <input-dir> --title "Opus vs Sonnet" # Custom report title
/eval-compare <input-dir> --overview "<context>"   # Add a context paragraph
```

When an `/eval-anova` `anova.json` is present in the input directory, the report also gains an ANOVA/Pareto **Statistical Significance** section. eval-compare works standalone without it (it never imports the stats libraries).

### /eval-anova

Design-of-Experiments over a `matrix:` of agent configurations. eval-run runs one condition; `/eval-anova` reads the matrix and fans out `/eval-run` per cell (condition × replication) into standard runs, then computes repeated-measures / mixed-effects ANOVA + a cost/quality Pareto frontier over their `summary.yaml` files (`anova.json`) and renders the comparison via `/eval-compare`. Because the statistics read standard runs, it can also analyze runs produced by a CI fan-out. Requires the `anova` extra.

```
/eval-anova --config eval.yaml                # run every cell → analyze → report
/eval-anova --config eval.yaml --dry-run      # design + cost estimate, no execution
/eval-anova --config eval.yaml --analyze-only # re-analyze existing runs + re-render
```

See [`eval/anova-example/`](eval/anova-example/) for a self-contained worked example (committed sample runs let you reproduce the analysis + report offline).

### /eval-review

Interactive human review of eval results. Presents judge scores and outputs, collects qualitative feedback, analyzes patterns, and proposes SKILL.md changes.

```
/eval-review --run-id 2026-04-04-opus      # Review a completed run
/eval-review --run-id <id> --cases case-003 # Review specific cases
```

### /eval-mlflow

MLflow integration: sync datasets, log run results, attach judge feedback to traces. The agent reads the `schema` descriptions to understand case structure — no hardcoded field mappings.

```
/eval-mlflow --action sync-dataset              # Push cases to MLflow dataset
/eval-mlflow --run-id <id> --action log-results # Log scoring results
/eval-mlflow --run-id <id> --action push-feedback # Push judge+human feedback to traces
/eval-mlflow --run-id <id> --action pull-feedback # Pull MLflow UI annotations
/eval-mlflow --run-id <id>                      # Do everything
```

### /eval-optimize

Automated refinement loop: run eval, identify failures, read traces + judge rationale, edit the skill to fix issues, re-run to verify, check for regressions.

```
/eval-optimize --model opus --max-iterations 3
```

With `--engine capevolve` (one-time setup: `/eval-setup --optimize`), runs a
statistically validated search instead: train/val/test splits, a significance gate
on validation (`Δ > k·SE`), and a sealed test split scored exactly once. See the
[cap-evolve guide](https://skillberry-ai.github.io/cap-evolve/) /
`website/guides/optimize-capevolve.md` for the full walkthrough, including the live
dashboard.

```bash
/eval-optimize --engine capevolve --max-usd 25   # launch, $25 hard cap
/eval-optimize --engine capevolve --status       # from any later session
/eval-optimize --engine capevolve --stop         # stop the whole campaign, not just its launcher
/eval-optimize --engine capevolve --apply        # winner onto a new git branch
```

### /eval-check

Scan the full configuration (skills, commands, CLAUDE.md, hooks) as a system. Finds content overlap, trigger collisions, CLAUDE.md duplication, type misclassification, and broken cross-component references. Produces an informational report with restructuring suggestions.

```bash
/eval-check                        # Scan and report to harness-report.md
/eval-check --include-global       # Also scan ~/.claude/CLAUDE.md
/eval-check --output my-report.md  # Custom output path
```

## Architecture

```
agent_eval/              # Python package (config, runner, state)
  config.py              # EvalConfig from eval.yaml
  state.py               # Shared state persistence
  agent/
    base.py              # EvalRunner ABC + RunResult
    claude_code.py       # Claude Code CLI runner
    stream_capture.py    # Stream-json processing + SubagentStop hook
  mlflow/
    experiment.py        # MLflow experiment setup
    trace_builder.py     # Hierarchical trace builder
  cli/
    trace_run.py         # claude-trace CLI

skills/
  eval-setup/            # Environment setup
  eval-analyze/          # Skill analysis + config generation
  eval-dataset/          # Test case generation
  eval-run/              # Evaluation execution
  eval-review/           # Interactive human review
  eval-mlflow/           # MLflow integration
  eval-optimize/         # Automated refinement loop
  eval-compare/          # Cross-run / cross-model comparison report (+ ANOVA stats section)
  eval-anova/            # DoE/ANOVA matrix experiments (orchestrates eval-run)
  eval-check/            # Full-harness configuration health check
```

## Agent Support

The harness is agent-agnostic via the `EvalRunner` abstraction. Set `runner.type` in eval.yaml:

```yaml
runner:
  type: claude-code    # default — uses claude --print

runner:
  type: cli            # opaque CLI runner — delegates to an arbitrary command
  command: "my-runner run {agent} --model {model} --workspace {workspace}"
```

The `cli` runner executes a configurable command template with placeholder substitution. See **[docs/opaque-cli-runner-contract.md](docs/opaque-cli-runner-contract.md)** for the full contract (placeholders, metrics.json format, what the command MUST and SHOULD do).

Add new runners by subclassing `EvalRunner` in `agent_eval/agent/` and registering in `RUNNERS`.

## MLflow Tracing

The same tracing used by `/eval-mlflow` is available for standalone skill runs via `claude-trace` — a drop-in replacement for `claude --print` that captures stream-json output and builds hierarchical MLflow traces. See **[TRACING.md](TRACING.md)** for full documentation.

```bash
# Install with MLflow support
pip install -e "./agent-eval-harness[mlflow]"

# Run any skill with tracing
echo "/rfe.speedrun --input batch.yaml --headless" | claude-trace --model opus
```

## Dependencies

- `pyyaml >= 6.0`
- Optional: `mlflow[genai] >= 3.5` (for `/eval-mlflow` and `claude-trace`)
- Optional: `anthropic >= 0.77` (for LLM judges, pairwise comparison, synthetic dataset generation, and hook answering)
