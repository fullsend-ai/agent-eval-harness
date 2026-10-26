# The eval.yaml schema

A single `eval.yaml` in your project root drives everything. It is parsed into an
`EvalConfig` and validated at load time. **Every top-level key is optional** and has a
sensible default — a minimal config is just a `name`, what to execute, a `dataset`, and
one `judge`.

!!! tip "Describe *what*, not *where*"
    `eval.yaml` describes **what** to evaluate. The **execution backend** (Local,
    Harbor, EvalHub) is always a CLI flag (`--runner`), never a config key — so the
    same file runs unchanged everywhere.

<div class="schema-diagram" role="region" aria-label="eval.yaml schema map">
  <p class="sd-meta">
    Every top-level key is optional. <code>name</code>, <code>description</code>, and <code>title</code> are the only non-deprecated top-level scalars.
    <span class="sd-badge llm">LLM</span> marks natural-language fields interpreted by agents &amp; judges,
    <span class="sd-badge py">PY</span> marks Python code or expressions, and <code>[]</code> marks a list.
  </p>
  <div class="sd-grid">

    <div class="sd-col define">
      <div class="sd-group">Define · what to evaluate</div>
      <div class="sd-card">
        <a class="sd-key sd-link" href="../config/dataset/">dataset</a>
        <div class="sd-purpose">Where cases live and what they contain</div>
        <div class="sd-fields">
          <span class="sd-f">path</span>
          <span class="sd-f">schema<span class="sd-badge llm">LLM</span></span>
          <span class="sd-f">workspace.files[]</span>
        </div>
      </div>
      <div class="sd-card">
        <a class="sd-key sd-link" href="../config/generation/">generation</a>
        <div class="sd-purpose">How <code>/eval-dataset</code> sources cases</div>
        <div class="sd-fields">
          <span class="sd-f">strategy</span>
          <span class="sd-f">context<span class="sd-badge llm">LLM</span></span>
          <span class="sd-f">seeds[]</span>
        </div>
      </div>
      <div class="sd-card">
        <a class="sd-key sd-link" href="../config/inputs-tools/">inputs.tools[]</a>
        <div class="sd-purpose">Tool interception for headless runs</div>
        <div class="sd-fields">
          <span class="sd-f">match<span class="sd-badge llm">LLM</span></span>
          <span class="sd-f">prompt<span class="sd-badge llm">LLM</span></span>
          <span class="sd-f">prompt_file<span class="sd-badge llm">LLM</span></span>
        </div>
      </div>
      <div class="sd-card">
        <a class="sd-key sd-link" href="../config/outputs/">outputs[]</a>
        <div class="sd-purpose">Artifacts and tool calls to collect</div>
        <div class="sd-fields">
          <span class="sd-f">path</span>
          <span class="sd-f">tool</span>
          <span class="sd-f">schema<span class="sd-badge llm">LLM</span></span>
          <span class="sd-f">batch_pattern</span>
          <span class="sd-f">types</span>
        </div>
      </div>
    </div>

    <div class="sd-col execute">
      <div class="sd-group">Execute · how it runs</div>
      <div class="sd-card">
        <a class="sd-key sd-link" href="../config/execution/">execution</a>
        <div class="sd-purpose">What runs (skill <em>or</em> prompt), per case or batch</div>
        <div class="sd-fields">
          <span class="sd-f">mode</span>
          <span class="sd-f">skill</span>
          <span class="sd-f">prompt<span class="sd-badge llm">LLM</span></span>
          <span class="sd-f">arguments</span>
          <span class="sd-f">timeout</span>
          <span class="sd-f">max_budget_usd</span>
          <span class="sd-f">parallelism</span>
          <span class="sd-f">env</span>
          <span class="sd-f">symlinks</span>
        </div>
      </div>
      <div class="sd-card">
        <a class="sd-key sd-link" href="../config/runner/">runner</a>
        <div class="sd-purpose">Agent runtime and knobs</div>
        <div class="sd-fields">
          <span class="sd-f">type</span>
          <span class="sd-f">effort</span>
          <span class="sd-f">settings</span>
          <span class="sd-f">plugin_dirs[]</span>
          <span class="sd-f">env</span>
          <span class="sd-f">system_prompt<span class="sd-badge llm">LLM</span></span>
          <span class="sd-f">command</span>
          <span class="sd-f">workspace_mode</span>
        </div>
      </div>
      <div class="sd-card">
        <a class="sd-key sd-link" href="../config/models/">models</a>
        <div class="sd-purpose">Model per role (CLI flags override)</div>
        <div class="sd-fields">
          <span class="sd-f">skill</span>
          <span class="sd-f">subagent</span>
          <span class="sd-f">judge</span>
          <span class="sd-f">hook</span>
          <span class="sd-f">judge_verdict<span class="sd-badge llm">LLM</span></span>
          <span class="sd-f">providers</span>
        </div>
      </div>
      <div class="sd-card">
        <a class="sd-key sd-link" href="../config/permissions/">permissions</a>
        <div class="sd-purpose">Tool allow / deny for headless runs</div>
        <div class="sd-fields">
          <span class="sd-f">allow[]</span>
          <span class="sd-f">deny[]</span>
        </div>
      </div>
      <div class="sd-card">
        <a class="sd-key sd-link" href="../config/hooks/">hooks</a>
        <div class="sd-purpose">Lifecycle shell commands</div>
        <div class="sd-fields">
          <span class="sd-f">before_all[]</span>
          <span class="sd-f">before_each[]</span>
          <span class="sd-f">after_each[]</span>
          <span class="sd-f">before_scoring[]</span>
          <span class="sd-f">after_all[]</span>
          <span class="sd-f">before_report[]</span>
        </div>
      </div>
      <div class="sd-card deprecated">
        <div class="sd-key">skill<span class="sd-badge dep">deprecated</span></div>
        <div class="sd-purpose">Top-level <code>skill:</code> — use <code>execution.skill</code></div>
      </div>
    </div>

    <div class="sd-col score">
      <div class="sd-group">Score · how it's judged</div>
      <div class="sd-card">
        <a class="sd-key sd-link" href="../config/judges/">judges[]</a>
        <div class="sd-purpose">How each case is scored — five judge types</div>
        <div class="sd-fields">
          <span class="sd-f">name</span>
          <span class="sd-f">description<span class="sd-badge llm">LLM</span></span>
          <span class="sd-f">if<span class="sd-badge py">PY</span></span>
          <span class="sd-f">check<span class="sd-badge py">PY</span></span>
          <span class="sd-f">prompt<span class="sd-badge llm">LLM</span></span>
          <span class="sd-f">prompt_file<span class="sd-badge llm">LLM</span></span>
          <span class="sd-f">llm_rubric<span class="sd-badge llm">LLM</span></span>
          <span class="sd-f">builtin</span>
          <span class="sd-f">module<span class="sd-badge py">PY</span></span>
          <span class="sd-f">function<span class="sd-badge py">PY</span></span>
          <span class="sd-f">agent</span>
          <span class="sd-f">context[]</span>
          <span class="sd-f">model</span>
          <span class="sd-f">arguments</span>
          <span class="sd-f">samples</span>
          <span class="sd-f">examples</span>
          <span class="sd-f">score_range</span>
          <span class="sd-f">feedback_type</span>
          <span class="sd-f">verdict<span class="sd-badge llm">LLM</span></span>
        </div>
      </div>
      <div class="sd-card">
        <a class="sd-key sd-link" href="../config/thresholds/">thresholds</a>
        <div class="sd-purpose">Regression gates per judge</div>
        <div class="sd-fields">
          <span class="sd-f">min_mean</span>
          <span class="sd-f">min_pass_rate</span>
          <span class="sd-f">min_win_rate</span>
          <span class="sd-f">max_error_rate</span>
        </div>
      </div>
      <div class="sd-card">
        <a class="sd-key sd-link" href="../config/reward/">reward</a>
        <div class="sd-purpose">Collapse judges into an RL scalar in [0, 1]</div>
        <div class="sd-fields">
          <span class="sd-f">judge</span>
          <span class="sd-f">normalize</span>
          <span class="sd-f">formula<span class="sd-badge py">PY</span></span>
          <span class="sd-f">weights</span>
          <span class="sd-f">gate</span>
          <span class="sd-f">score_range</span>
          <span class="sd-f">raw[]</span>
        </div>
      </div>
      <div class="sd-card">
        <a class="sd-key sd-link" href="../config/matrix/">matrix</a>
        <div class="sd-purpose">Factorial grid <code>/eval-anova</code> fans out and tests</div>
        <div class="sd-fields">
          <span class="sd-f">factors</span>
          <span class="sd-f">replications</span>
          <span class="sd-f">analysis.correction</span>
          <span class="sd-f">analysis.per_judge</span>
        </div>
      </div>
    </div>

    <div class="sd-col observe">
      <div class="sd-group">Observe · what's captured</div>
      <div class="sd-card">
        <a class="sd-key sd-link" href="../config/mlflow/">mlflow</a>
        <div class="sd-purpose">Experiment tracking — presence opts in</div>
        <div class="sd-fields">
          <span class="sd-f">experiment</span>
          <span class="sd-f">tracking_uri</span>
          <span class="sd-f">tags</span>
        </div>
      </div>
      <div class="sd-card">
        <a class="sd-key sd-link" href="../config/traces/">traces</a>
        <div class="sd-purpose">Execution data captured for judges</div>
        <div class="sd-fields">
          <span class="sd-f">stdout</span>
          <span class="sd-f">stderr</span>
          <span class="sd-f">events</span>
          <span class="sd-f">metrics</span>
        </div>
      </div>
    </div>

  </div>
</div>

## Top-level keys

| Key | Purpose | Reference |
| --- | --- | --- |
| `name` | Experiment / run name (defaults to the file stem) | *(inline)* |
| `description` | Human-readable description | *(inline)* |
| `title` | HTML report heading (default: `Agent Eval Report`; `--title` overrides) | *(inline)* |
| `extends` | Layer this file over a base config (a *profile*) | [extends](config/extends.md) |
| `execution` | What to run and how cases are processed | [execution](config/execution.md) |
| `runner` | Agent runtime + runtime-specific knobs | [runner](config/runner.md) |
| `models` | Model per role: skill, subagent, judge, hook; `providers` registers how `provider:/` ids are served | [models](config/models.md) · [models.providers](config/providers.md) |
| `permissions` | Tool allow/deny for headless runs | [permissions](config/permissions.md) |
| `mlflow` | Experiment tracking (opt-in) | [mlflow](config/mlflow.md) |
| `dataset` | Where cases live and what they contain | [dataset](config/dataset.md) |
| `generation` | How `/eval-dataset` sources cases | [generation](config/generation.md) |
| `inputs` | Tool interception handlers (`inputs.tools`) | [inputs.tools](config/inputs-tools.md) |
| `outputs` | Artifacts / tool calls to collect | [outputs](config/outputs.md) |
| `traces` | Which execution data to capture | [traces](config/traces.md) |
| `hooks` | Lifecycle shell hooks | [hooks](config/hooks.md) |
| `judges` | How each case is scored | [judges](config/judges.md) |
| `thresholds` | Regression gates per judge | [thresholds](config/thresholds.md) |
| `reward` | Collapse judges into an RL reward scalar | [reward](config/reward.md) |
| `matrix` | Factorial design for `/eval-anova`: factors, replications, p-value correction | [matrix](config/matrix.md) |
| `skill` | **Deprecated** — use `execution.skill` | *(see below)* |

!!! warning "`skill:` at the top level is deprecated"
    A top-level `skill:` still works but is auto-normalized into `execution.skill`
    with a deprecation warning. Always author new configs with `execution.skill`.

## Two minimal configs

Which keys you set depends on whether you're testing a **skill** or a **capability**.
See [the execution model](../concepts/execution-model.md) for the difference.

=== "Skill mode"

    ```yaml
    name: my-skill-eval

    execution:
      mode: case
      skill: my-skill
      arguments: "{prompt}"

    dataset:
      path: eval/dataset/cases
      schema: "Each case has an input.yaml with a 'prompt' field."

    judges:
      - name: completeness
        prompt: "Score 1-5 how completely the output covers the request (1 = most requirements missing, 3 = basics with gaps, 5 = complete).\n\nRequest:\n{{ inputs }}\n\nOutput:\n{{ outputs }}"
        score_range: [1, 5]   # declare the scale — omitting it warns at config load
    ```

=== "Prompt mode"

    ```yaml
    name: docs-navigation-eval

    execution:
      mode: case
      prompt: "{{ input.prompt }}"

    runner:
      type: claude-code
      workspace_mode: repo   # navigate the real repository

    dataset:
      path: eval/dataset/cases
      schema: "Each case has an input.yaml with a 'prompt' field."

    judges:
      - name: used_docs
        builtin: consulted_docs
    ```

## A fully annotated config

The repository's root [`eval.yaml`](https://github.com/opendatahub-io/agent-eval-harness/blob/main/eval.yaml)
is the canonical, heavily-commented reference — every block with inline comments and
commented-out variants for all five judge types, tool interception, `batch_pattern`,
and thresholds. It's the best single file to copy from.

```yaml title="eval.yaml (excerpt)"
name: my-skill-eval
description: Evaluate the main skill pipeline
title: My Skill Eval Report   # HTML report heading (default: Agent Eval Report)

execution:
  mode: case              # per-case (default) or batch
  skill: my-skill-name    # skill to test (use `prompt:` for prompt mode)
  arguments: "{prompt}"   # resolved per case from input.yaml fields

runner:
  type: claude-code       # claude-code | cursor | codex | fullsend | cli | responses-api
  # effort: high          # accepted values are runner-specific: claude-code
  #                       # low|medium|high|xhigh|max; codex low|medium|high|xhigh;
  #                       # cursor is model-specific

models:
  skill: claude-opus-4-6  # required (or pass --model); openrouter:/<author>/<slug> activates models.providers
  judge: claude-opus-4-6  # used by LLM and pairwise judges
  # providers:            # the openrouter block behind provider:/ ids — see models.providers

permissions:
  deny:
    - "mcp__*"            # block all MCP tools during eval

mlflow:
  experiment: my-skill-eval   # opt-in: omit the block to disable tracking

dataset:
  path: eval/dataset/cases
  schema: |
    Each case has input.yaml (a 'prompt' field) and reference.md (gold output).

outputs:
  - path: artifacts
    schema: "One markdown file per case, named NNN-slug.md."

traces:
  stdout: true
  stderr: true
  events: false
  metrics: true

judges:
  - name: has_content
    check: |
      content = outputs["main_content"]
      if len(content.strip()) < 100:
          return False, f"Output too short ({len(content.strip())} chars)"
      return True, f"Output has {len(content.strip())} chars"

  - name: completeness
    prompt: "Score 1-5 how completely the output covers the request (1 = most requirements missing, 3 = basics with gaps, 5 = complete).\n\nRequest:\n{{ inputs }}\n\nOutput:\n{{ outputs }}"
    score_range: [1, 5]     # declare the scale — omitting it warns at config load

thresholds:
  has_content: { min_pass_rate: 1.0 }
  completeness: { min_mean: 3.5 }
```

## Conventions

- **Schema fields are natural language.** `dataset.schema` and `outputs[].schema` are
  documentation for the LLM agents and judges — scripts operate on file *paths*, not a
  parsed spec. There are no hardcoded field names.
- **Load-time validation is strict.** Mutually-exclusive keys (`skill` + `prompt`),
  invalid enums (`execution.mode`), and malformed reward formulas fail at load, not
  mid-run.
- **One loader.** Every reader of an eval config goes through the same loader, so a
  profile that [`extends`](config/extends.md) a base is merged identically for
  execution, scoring, reporting, Harbor bundling and validation.

## Per-key reference

<div class="grid cards" markdown>

- [**extends**](config/extends.md) — layer a profile over a base config; merge policy and `!replace`
- [**execution**](config/execution.md) — mode, skill/prompt, arguments, timeout, budget, parallelism, env, symlinks
- [**runner**](config/runner.md) — type, effort, permission_mode, settings, plugin_dirs, env, system_prompt, command, workspace_mode
- [**models**](config/models.md) — skill, subagent, judge, hook roles and precedence
- [**models.providers**](config/providers.md) — the `openrouter` block behind `provider:/` ids: keys, routing, enforcement
- [**permissions**](config/permissions.md) — allow/deny patterns and the path-based compiler
- [**mlflow**](config/mlflow.md) — experiment, tracking_uri, tags
- [**dataset**](config/dataset.md) — path, schema, workspace.files
- [**generation**](config/generation.md) — strategy, context, seeds
- [**inputs.tools**](config/inputs-tools.md) — tool interception handlers
- [**outputs**](config/outputs.md) — path vs tool, schema, batch_pattern, types
- [**traces**](config/traces.md) — stdout, stderr, events, metrics
- [**hooks**](config/hooks.md) — before/after all/each, before_scoring
- [**judges**](config/judges.md) — the five judge types and all fields, including `examples`
- [**thresholds**](config/thresholds.md) — min_mean, min_pass_rate, min_win_rate, max_error_rate
- [**reward**](config/reward.md) — single-judge and formula reward modes
- [**matrix**](config/matrix.md) — factors, replications, `analysis.correction`, `analysis.per_judge`

</div>
