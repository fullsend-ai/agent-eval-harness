# Writing custom judges

Judges turn a case's collected outputs into a score. This page shows how to write each
of the five judge types by hand, which variables they receive, and how to parameterize
and conditionally skip them. For the conceptual overview see
[Judges](../concepts/judges.md); for every field, the
[judges reference](../reference/config/judges.md).

## The five types

A judge's *type* is inferred from which field you set — you never declare it.

```mermaid
flowchart TD
    J[judge entry] --> B{which field?}
    B -->|builtin| BI[library judge]
    B -->|check| CK[inline Python]
    B -->|prompt / prompt_file / llm_rubric<br/>+ agent| AG[agent judge]
    B -->|prompt / prompt_file / llm_rubric| LLM[LLM judge]
    B -->|module + function| EXT[external code]
```

| Field(s) | Type | Runs | Returns |
| --- | --- | --- | --- |
| `builtin` | Library judge | Deterministic Python | `(bool or number, str)` |
| `check` | Inline Python | Deterministic | `(bool or number, str)` |
| `prompt` / `prompt_file` / `llm_rubric` | LLM judge | An API call to `models.judge` | pass/fail, or a score on the judge's `score_range` (`1–5` when undeclared) |
| the above **+ `agent`** | Agent judge | A tool-using agent run via the runner | pass/fail or a numeric score |
| `module` + `function` | External code | Your imported callable | `(bool or number, str)` |

!!! warning "One type per judge"
    The type fields are mutually exclusive. Setting `builtin` alongside any of
    `check` / `prompt` / `prompt_file` / `module` / `function` fails at config load.

Every judge — regardless of type — receives the same `outputs` record for the case: the
files it produced, its trace, metrics, annotations, and convenience keys. Numeric
(score) judges aggregate to a **mean**; boolean judges to a **pass rate**.

## Inline `check` judges

The most direct type: a snippet of Python whose body becomes a function. Two names are
in scope — `outputs` (the case record) and `arguments` (this judge's `arguments` dict,
`{}` if unset). Return a `(value, rationale)` tuple where `value` is a `bool` or a
number and `rationale` is a human-readable string shown in the report.

```yaml title="eval.yaml"
judges:
  - name: has_content
    description: Output is non-empty and substantial.
    check: |
      content = outputs.get("main_content", "")
      if len(content.strip()) < 100:
          return False, f"Output too short ({len(content.strip())} chars)"
      return True, f"Output has {len(content.strip())} chars"
```

!!! tip "Use `outputs.get(...)` with a default"
    A case that produced no artifacts won't have every convenience key. Reaching for a
    missing key with `outputs["main_content"]` raises `KeyError`, which the runner
    records as a judge *error* (no value) rather than a fail. `outputs.get("main_content", "")`
    degrades gracefully.

The record exposes far more than files. Common keys for `check` judges:

| Key | Contents |
| --- | --- |
| `outputs["files"]` | `{relative_path: text}` for every collected artifact |
| `outputs["<dir>_content"]` / `outputs["<dir>_file"]` | First file's text / path per `outputs[].path` dir |
| `outputs["tool_calls"]` | Tool calls matching an `outputs[].tool` pattern (`{"name", "input"}`) |
| `outputs["cost_usd"]`, `["num_turns"]`, `["duration_s"]`, `["token_usage"]`, `["exit_code"]` | Trace metrics (require `traces.metrics: true`) |
| `outputs["events"]` | Parsed event stream (require `traces.events: true`) |
| `outputs["conversation"]` | Root-level assistant text |
| `outputs["stdout"]`, `["stderr"]` | Captured logs (require `traces.stdout` / `stderr`) |
| `outputs["annotations"]` | Per-case metadata from `annotations.yaml` |

A metrics-driven check reads straight from the trace:

```yaml
judges:
  - name: cost_reasonable
    description: Cost per case stays under budget.
    check: |
      cost = outputs.get("cost_usd", 0) or 0
      if cost > arguments.get("max_usd", 0.50):
          return False, f"Cost ${cost:.2f} exceeds limit"
      return True, f"Cost ${cost:.2f}"
    arguments:
      max_usd: 0.50
```

## External `module` / `function` judges

When a check outgrows an inline snippet — it needs helpers, imports, or its own tests —
move it into a Python module in your project and point `module` + `function` at it. The
module is imported with your project root on `sys.path`. The function is called with the
record as the `outputs` keyword argument, and any `arguments` are spread in as
`**kwargs`, so mirror the builtin-judge signature:

```python title="eval/judges/schema_checks.py"
import json

def check_schema(outputs, **kwargs):
    required = kwargs.get("required_fields", [])
    raw = outputs.get("main_content", "")
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        return False, f"Invalid JSON: {e}"
    missing = [f for f in required if f not in doc]
    if missing:
        return False, f"Missing fields: {', '.join(missing)}"
    return True, "Schema valid"
```

```yaml title="eval.yaml"
judges:
  - name: schema_valid
    description: Output parses and has the required fields.
    module: eval.judges.schema_checks
    function: check_schema
    arguments:
      required_fields: [title, priority, labels]
```

!!! note "Return contract is shared"
    Builtin, `check`, and `module` judges all return the same shape: a
    `(value, rationale)` tuple (value = `bool` or number). Returning a bare value with no
    rationale works too, but the report shows an empty rationale. The library judge
    [`cost_budget`](https://github.com/opendatahub-io/agent-eval-harness/blob/main/agent_eval/judges/efficiency/cost_budget.py)
    is a minimal example of the same signature.

!!! warning "A declared `score_range` is enforced on these judges too"
    `score_range` is not just report colouring. A `builtin`, `check`, or `module` judge
    whose returned number falls outside its declared range — or is non-finite — is
    recorded as an **error sample** (`value: null` plus an `error`, and a `WARNING` line
    on stderr) and left out of the judge's mean. The value is never rounded or clamped:
    rounding only happens where a *model's* answer is parsed. So declare
    `score_range: [0, 8]` only if the judge genuinely cannot return 9; leave the range
    off for an unbounded count and the judge keeps emitting whatever it computes.

## LLM judges

For qualitative criteria, hand the case to a model. Choose one of three fields — they
all compile to the same Jinja2 template, then render against the case record:

=== "llm_rubric"

    Shortest form for a single criterion. If your text doesn't already reference
    `{{ conversation }}`, the agent's response is appended automatically.

    ```yaml
    judges:
      - name: cited_sources
        feedback_type: bool     # one yes/no criterion — no scale to declare
        llm_rubric: "The agent cited relevant documentation sources."
    ```

=== "prompt"

    Full control over the template and placeholders. Boolean is the default shape —
    one failure mode, explicit PASS/FAIL definitions:

    ```yaml
    judges:
      - name: covers_all_inputs
        description: >-
          Every requirement in the request is addressed. A code check can't
          verify this — the requirements are prose, not a schema.
        feedback_type: bool
        prompt: |
          The skill was asked to produce a document answering the request below.

          ## Request
          {{ inputs }}

          ## Artifact under evaluation
          {{ outputs }}

          ## What you are checking
          Whether any requirement in the request is left unaddressed.

          ## PASS
          Every requirement has a corresponding section or statement in the artifact.

          ## FAIL
          At least one requirement has no counterpart — name it in your rationale.
    ```

    A numeric scale is the exception, for a criterion that is genuinely graded — where
    a pass/fail boundary would throw away meaningful partial credit. Declare
    `score_range` and define every level:

    ```yaml
    judges:
      - name: completeness
        description: How completely the output covers the request (partial credit matters).
        score_range: [1, 5]     # declare the scale — omitting it warns at config load
        prompt: |
          How completely does the generated output cover the request?

          # Request
          {{ inputs }}

          # Output
          {{ outputs }}

          Score 1-5: 1 = most requirements missing, 3 = covers the basics
          with gaps, 5 = every requirement addressed.
    ```

=== "prompt_file"

    Reuse a prompt across judges/configs. Path is relative to the project root.
    `context` files are appended to the prompt for shared rubrics.

    ```yaml
    judges:
      - name: detailed_quality
        score_range: [1, 5]
        prompt_file: eval/prompts/quality-judge.md
        context:
          - eval/prompts/scoring-rubric.md
    ```

### Available Jinja variables

The prompt is rendered with these variables:

| Variable | Value |
| --- | --- |
| `{{ outputs }}` | The record. Bare, it renders every artifact file as text; use `{{ outputs.files }}`, `{{ outputs.cost_usd }}`, etc. for structured access |
| `{{ conversation }}` | Root-level assistant **visible** text from the event stream (excludes extended-thinking) |
| `{{ reasoning }}` | `{{ conversation }}` plus the model's extended-thinking (chain-of-thought), for reasoning-quality judges |
| `{{ inputs }}` | The case's `input.yaml`, formatted as `**key**: value` |
| `{{ tool_trace }}` | Chronological trace of tool calls (Read, Bash, …) |
| `{{ evidence }}` | Summary of verifiable tool evidence (turns, cost, files read/written) — derived only if referenced |
| `{{ annotations }}` | Case annotations as formatted text; also `{{ annotations.get('category') }}` for values |
| `{{ annotations_text }}` | The annotation text alone |
| `{{ arguments }}` | This judge's `arguments` dict |
| `{{ examples }}` | The harvested `## Human-labeled examples` block, for judges that declare [`examples:`](../reference/config/judges.md#few-shot-examples-from-human-reviews-examples); empty for the rest. A template that never references it gets the block appended instead |

!!! tip "Score scale vs. pass/fail"
    With no `score_range` an LLM judge returns an integer score on the unenforced **1–5**
    default (aggregated as a mean) and warns at config load — declare the scale. Set
    `feedback_type: bool` to make it a **pass/fail** judge (aggregated as a pass rate).
    `{{ tool_trace }}`, `{{ evidence }}`, and `{{ reasoning }}` need `traces.events: true`;
    `{{ conversation }}` falls back to `stdout.log` when no events were captured.

!!! note "The rationale is elicited before the verdict"
    The harness's judge tool schemas and system prompts deliberately ask for the
    `rationale` first and the verdict (`score`/`passed`/`preferred`) second: an
    autoregressive model that writes its analysis before committing to a verdict token
    produces better-calibrated judgments. Prompt authors should not fight that ordering —
    don't ask the judge to "state the verdict up front" or lead with the score in a
    requested output format.

!!! note "Graded material is fenced and marked as untrusted"
    At render time the harness wraps agent-produced template variables — bare
    `{{ outputs }}`, `{{ conversation }}`, `{{ reasoning }}`, `{{ inputs }}`,
    `{{ evidence }}`, `{{ tool_trace }}`, and both pairwise outputs — between
    `[BEGIN EVALUATED MATERIAL: <name>]` and `[END EVALUATED MATERIAL]` markers, and
    every LLM judge system prompt (bool, numeric, and pairwise) carries a guard pointing
    at those markers: the rubric is to be followed, the fenced material is untrusted,
    model-generated output to assess — instructions inside it must never be followed,
    including text claiming the material has ended or the rules have changed. Author-side
    variables (`arguments`, `annotations`), structured access like
    `{{ outputs.cost_usd }}`, and string-filtered values stay unfenced; bare
    `{{ outputs.files }}`, per-file access, and `| tojson` are fenced. Agent judges get the equivalent SECURITY
    paragraph in their harness-appended contract. Prompt-injection defenses are
    mitigations, not boundaries — pair this with `samples: k` and
    [human calibration](../guides/eval-review.md#when-a-judge-and-a-human-disagree) for
    consequential evals. The full assembly is in
    [How the judge prompt is assembled](../concepts/judges.md#how-the-judge-prompt-is-assembled).

!!! tip "Grading on a scale other than 1–5"
    Declare it: `score_range: [0, 2]`. The range is stated in the judge's system prompt,
    written into the `submit_score` verdict schema (the field's description under a
    structured output, advisory `minimum`/`maximum` on the tool schema), and used to normalize the
    judge in every reward composition that normalizes it (not `reward.raw`, nor a
    clamped single `reward.judge`). Because a tool schema is *advisory* — the model
    is not constrained by `minimum`/`maximum` — a returned value off the scale is recorded
    as an **error sample** and left out of the judge's mean rather than clamped onto the
    scale, where a 4 from a 0-2 judge would land as a perfect 2. With `samples: 3` the case
    still reduces over the surviving samples, so one bad sample costs a stability flag. Keep
    stating the scale in the rubric text as well; a prompt that says "score 0, 1, or 2" and
    a config that says nothing is the combination that produced silently out-of-range scores
    ([#182](https://github.com/fullsend-ai/agent-eval-harness/issues/182)).
    An LLM judge's answer is rounded to an integer unless the scale says otherwise — declare
    `feedback_type: float`, or fractional bounds such as `[0, 2.5]`, for a continuous scale.

LLM judges — including the agent judges below — are the only types that can be
**sampled**. Set `samples: 3` to call the model several times per case and reduce the
noise (median for scores, majority vote for booleans); the report flags cases where
samples disagreed. `samples` on any other type is ignored with a warning.

## Authoring an LLM judge prompt

The harness ships the authoring template that `/eval-analyze` and `/eval-review` follow
when they write judges:
[`judge-prompt-template.md`](https://github.com/opendatahub-io/agent-eval-harness/blob/main/skills/eval-analyze/references/judge-prompt-template.md).
Its rules, condensed.

### Before writing: the selection ladder

An LLM judge is the last resort, not the first. Work down the ladder and stop at the
first rung that fits:

1. **builtin** — tested, versioned, no code ([library](../reference/builtin-judges.md)).
2. **inline `check`** — anything code can verify deterministically.
3. **LLM judge, `feedback_type: bool`** — one pass/fail question.
4. **LLM judge, numeric** — only when the criterion is genuinely graded.

Every LLM judge must be able to answer *why can't a code check verify this?* Put the
answer in its `description`; if there isn't one, move up the ladder.

### One criterion per judge

Each judge detects exactly **one failure mode**. "Completeness, clarity, accuracy" is
three judges, not one — every downstream consumer works per judge:
[thresholds](../concepts/thresholds.md) gate per judge, [`reward:`](../concepts/reward-api.md)
weights per judge, and [`/eval-anova`](../guides/eval-anova.md) compares per judge. A
blended score can't be gated, weighted, or compared without unblending it.

The same goes for severity. Instead of one ordinal scale ("1 = dangerously wrong … 5 =
perfect"), use **tiered boolean judges**:

```yaml
judges:
  - name: factually_wrong        # FAIL if any claim contradicts the source
    feedback_type: bool
    prompt_file: eval/prompts/factually-wrong.md
  - name: dangerously_wrong      # FAIL only if a wrong claim could cause harm
    feedback_type: bool
    prompt_file: eval/prompts/dangerously-wrong.md
```

Ordinal scales are hard to calibrate — raters disagree about a 3 vs a 4, and the judge
inherits that noise. A pass/fail boundary keeps every failure actionable: a failed
`dangerously_wrong` means one specific thing.

### Minimal context

Hand the judge exactly the variable(s) the criterion grades: `{{ outputs }}` for produced
files, `{{ conversation }}` for the response, `{{ evidence }}` / `{{ tool_trace }}` for
behaviour, plus `{{ inputs }}` when the criterion compares output against input. Never
`{{ outputs }}` **and** `{{ conversation }}` by default — extra context invites the judge
to grade things the criterion never asked about.

!!! note "Stdout-only skills"
    When the skill produces no files, bare `{{ outputs }}` renders empty. Any judge that
    would use it — builtins included — must use `{{ conversation }}` instead; inline
    `check` judges read `outputs.get("conversation", "")`.

### The skeleton (boolean judge)

```text
<TASK CONTEXT — one or two sentences: what the skill was asked to produce.>

## Artifact under evaluation
{{ outputs }}

## What you are checking
<The ONE failure mode this judge exists to catch, in one sentence.>

## PASS
<Observable properties of an artifact that passes — a positive definition,
 not "no problems".>

## FAIL
<What the failure mode concretely looks like — enumerate the forms you
 expect. Ask the judge to cite the offending content in its rationale.>

## Examples
PASS example: <short excerpt>            — <why it passes>
FAIL example: <short excerpt>            — <which part fails and why>
Borderline:   <excerpt near the boundary> — <which side, and the deciding property>
```

These **in-prompt examples** are static — you write them. Real excerpts beat invented
ones: prior runs under `$AGENT_EVAL_RUNS_DIR/<eval-name>/<run-id>/cases/` are the best
source, and `/eval-review` surfaces exactly the judge-human disagreements the borderline
slot should encode. Quote short excerpts, redacted of secrets, framed as data to grade.

Numeric judges keep the same structure but replace PASS/FAIL with per-level definitions:
every level in terms of observable properties a second rater could apply and land on the
same number; adjacent levels differing by something observable (if they don't, collapse
the levels or decompose into tiered booleans); and `score_range` declared on the judge.

### What NOT to put in the prompt

- **JSON or response-format boilerplate.** The harness hands the provider the verdict
  schema itself — `submit_evaluation` for boolean judges, `submit_score` for numeric
  ones, as a structured output or a forced tool call — so the shape and a rationale are
  already enforced. "Respond with JSON {...}" fights that schema and is ignored at best.
- **The numeric bounds.** A declared `score_range` is stated in the system prompt and
  verdict schema automatically. The prompt defines what each level *means*.
- **Your own untrusted-data guard.** The harness fences the artifact variables and puts
  the guard in every LLM judge's system prompt — see
  [How the judge prompt is assembled](../concepts/judges.md#how-the-judge-prompt-is-assembled).
  Put the artifact under an explicit heading and stop there.
- **A second criterion.** If the prompt says "also check…", it's two judges.

### Calibrating from human labels

In-prompt examples fix the boundary once; **harvested examples (`examples:`)** keep it
anchored to real human verdicts as the eval evolves. Label cases with `/eval-review`,
then declare `examples: {source: reviews}` on the judge and the harness injects clear
pass/fail anchors from prior runs' `review.yaml` into its prompt — never the case being
judged. The loop and the anchor rules are in
[Review results → Turn labels into calibration](../guides/eval-review.md#turn-labels-into-calibration);
the field reference is in
[judges config](../reference/config/judges.md#few-shot-examples-from-human-reviews-examples).

## Agent judges

Of the types above, only **LLM** judges (`prompt`/`prompt_file`/`llm_rubric`, and LLM
`builtin`s) make a model call — a single, stateless one from the prompt text they're
handed; `check`, external `module`/`function`, and Python `builtin` judges run
deterministic code with no model call. An **agent judge** upgrades an LLM judge into a
*tool-using* run: add an `agent:` block and the judge executes through the
[runner abstraction](../concepts/runners.md) in a staged, isolated workspace, where it may
take **multiple tool-using turns** — `Read`/`Grep`/`Glob` over the case's artifacts **and
reference docs** — before it decides. Reach for it when a verdict must *look something up*
rather than guess.

The judge still takes its instructions from `prompt` / `prompt_file` / `llm_rubric`, and
reuses `model`, `feedback_type`, `score_range`, `samples`, `if`, and thresholds exactly
like an LLM judge — the `agent:` block is the only thing that changes how it runs. Its
inputs are the case's collected output files (filtered by `agent.inputs`, default all)
plus each `agent.context` dir copied under `./.context/`; the spot it is meant to write
is `./output/`, for the verdict file. What it *can* reach is set by `agent.allowed_tools`
(default `[Read(**), Edit(output/**)]`), enforced because the judge runs deny-by-default on
the Claude Code runner — reads work inside the staged workspace, anything reaching outside
it is refused — backed by `agent.denied_tools` for runners without an equivalent mode.

Say you grade whether a strategy's architecture claims are real. A text-only LLM judge
has no way to check component names, CRDs, or API fields against the platform docs, so it
trusts the output and inflates the score to full marks. An agent judge greps the actual
architecture-context docs and marks down invented components:

```yaml title="eval.yaml"
judges:
  - name: architecture_score
    description: Component/CRD/API claims check out against the real arch-context docs.
    prompt_file: eval/prompts/architecture-agent-judge.md
    model: claude-opus-4-8
    feedback_type: int
    score_range: [0, 2]
    samples: 3
    agent:
      runner: { type: claude-code }             # per-judge runner (default claude-code)
      allowed_tools: [Read(**), Edit(output/**)]  # the default
      context: [.context/architecture-context]  # copied under ./.context/
      inputs: [strat-tasks]                      # which output dirs to stage (default: all)
      # timeout: 420        # default: execution.timeout or 600s
      # max_budget_usd: 2.0 # per-judge-run cap (default 2.0)
```

The prompt file holds only the criteria — the harness appends the output contract and an
untrusted-data guard for you:

```markdown title="eval/prompts/architecture-agent-judge.md"
Grade the strategy in ./strat-tasks/. For every component, CRD, and API it names,
Grep ./.context/architecture-context/ to confirm it exists. Score 0 (claims are
invented) to 2 (every claim verified).
```

The agent ends by writing its verdict to `./output/score.json` — numeric here because
`feedback_type: int`:

```json title="output/score.json"
{"rationale": "TrainingRuntime CRD and the Kueue integration verified against arch-context; the referenced ModelMeshRouter component does not exist.", "score": 1}
```

!!! tip "Output contract, and the stdout fallback"
    Write `{"rationale": "…", "score": <number>}` for numeric judges or
    `{"rationale": "…", "passed": <bool>}` when `feedback_type: bool`. If `score.json` is
    missing, the harness parses the last such JSON object from the run's stdout; if
    neither yields a value the case records an **error** sample — an agent judge never
    silently passes.

!!! warning "`Bash` needs a sandboxed runner"
    The default `allowed_tools` is `[Read(**), Edit(output/**)]`. Only add `Bash`
    (e.g. to run the tests a change touched) on a runner with OS-level sandboxing — no
    network, cleaned credentials, path confinement — because the judge reads untrusted
    case artifacts. `..`-bearing artifact paths are containment-checked, and
    `runner.workspace_mode: repo` is rejected for agent judges: they must run in the
    isolated staged workspace.

## `arguments`: parameterizing a judge

`arguments` is a plain dict, reused by every type — but consumed differently:

- **Python judges** (`builtin`, `check`, `module`) — spread in as `**kwargs` (`check`
  judges also see it as the `arguments` variable).
- **LLM judges** — exposed as `{{ arguments }}` inside the template.

This lets one judge implementation serve many thresholds without duplicating code — see
the `max_usd` and `required_fields` examples above.

## `if`: skipping a judge per case

Give a judge an `if:` expression to run it only on the cases where it's meaningful. The
expression is evaluated against `annotations` and `outputs`; when it's false the judge is
**skipped** — the case isn't counted in that judge's mean or pass rate.

```yaml title="eval.yaml"
judges:
  - name: dedup_correct
    if: "annotations.get('is_duplicate', False)"   # only duplicate cases
    feedback_type: bool
    prompt: "Did the agent correctly flag the input as a duplicate?"

  - name: completeness
    if: "not annotations.get('skip_quality', False)"
    feedback_type: int
    score_range: [1, 5]
    prompt: "Score 1-5 how completely the output covers the request (1 = most requirements missing, 3 = basics with gaps, 5 = complete).\n\nRequest:\n{{ inputs }}\n\nOutput:\n{{ outputs }}"
```

!!! warning "`if` runs in a restricted sandbox"
    The `if` expression is evaluated with **no builtins** — only `annotations` and
    `outputs` are in scope. Keep it to simple attribute/`.get()` lookups and boolean
    logic. Put anything heavier inside the judge body (`check`/`module`), which runs with
    full builtins.

## Where to go next

<div class="grid cards" markdown>

-   :material-book-open-variant: **Judge concepts**

    ---

    Judge types, aggregation, and how scoring fits the pipeline.

    [:octicons-arrow-right-24: Judges](../concepts/judges.md)

-   :material-format-list-bulleted: **Full field reference**

    ---

    Every judge field, precedence rule, and validation.

    [:octicons-arrow-right-24: judges config](../reference/config/judges.md)

-   :material-package-variant: **Builtin judges**

    ---

    Library judges you can reference by name before writing your own.

    [:octicons-arrow-right-24: Builtin judges](../reference/builtin-judges.md)

-   :material-dice-multiple: **Sampling & pairwise**

    ---

    Reduce judge noise and run A/B comparisons between runs.

    [:octicons-arrow-right-24: Pairwise & sampling](../concepts/pairwise-and-sampling.md)

-   :material-account-check: **Calibrate with human labels**

    ---

    Triage judge-human disagreements and feed the labels back as anchors.

    [:octicons-arrow-right-24: /eval-review](../guides/eval-review.md#when-a-judge-and-a-human-disagree)

</div>
