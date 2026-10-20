# Judge Prompt Template

Use this template when authoring the prompt for an LLM judge — an inline
`prompt:`, a `prompt_file:`, or an agent judge's instructions. It is the
companion to `eval-yaml-template.md` (which covers the config fields around
the prompt); this file covers the prompt text itself.

## Before writing: the selection ladder

An LLM judge is the last resort, not the first. Work down the ladder and stop
at the first rung that fits:

1. **builtin** — tested, versioned, no code (`list_builtins.py`)
2. **inline check** — anything code can verify deterministically
3. **LLM judge, `feedback_type: bool`** — one pass/fail question
4. **LLM judge, numeric** — only when the criterion is genuinely graded

Every LLM judge must be able to answer: *why can't a code check verify this?*
Put the answer in the judge's `description`. If there isn't one, move up the
ladder.

## One criterion per judge

Each judge detects exactly ONE failure mode. "Completeness, clarity, accuracy"
is three judges, not one — every downstream consumer works per judge:

- `thresholds` gate per judge — a blended score can't tell you *which*
  criterion regressed;
- `reward:` composition weights per judge — you can't weight clarity 0.2 and
  accuracy 0.8 inside one number;
- `/eval-anova` compares per judge — a condition that improves accuracy but
  hurts clarity nets out invisible in a blended score.

Decomposition also applies to severity. Instead of one ordinal scale
("1 = dangerously wrong … 5 = perfect"), use tiered boolean judges:

```yaml
  - name: factually_wrong        # FAIL if any claim contradicts the source
    feedback_type: bool
    prompt: ...
  - name: dangerously_wrong      # FAIL only if a wrong claim could cause harm
    feedback_type: bool
    prompt: ...
```

Ordinal scales are hard to calibrate — raters disagree about a 3 vs a 4, and
the judge inherits that noise. A binary boundary keeps every failure
actionable: a failed `dangerously_wrong` means one specific thing.

## Prompt structure (boolean judge — the default)

```text
<TASK CONTEXT — one or two sentences: what the skill was asked to produce.
 Enough to interpret the artifact; no more.>

<THE ARTIFACT — exactly the template variable(s) the criterion grades:
 {{ outputs }} for produced files, {{ conversation }} for the response,
 {{ evidence }} / {{ tool_trace }} for behavior, plus {{ inputs }} when the
 criterion compares output against input. Never {{ outputs }} +
 {{ conversation }} by default — extra context invites the judge to grade
 things the criterion never asked about. Put it under an explicit heading
 (e.g. "## Artifact under evaluation"). The harness fences every rendered
 artifact variable between [BEGIN EVALUATED MATERIAL: <name>] and
 [END EVALUATED MATERIAL] markers and appends an untrusted-data guard to the
 system prompt of every LLM judge — bool, numeric, and pairwise; agent judges
 get it in their contract — so don't write your own guard. Author-side
 variables ({{ arguments }}, {{ annotations }}) stay unfenced; don't pipe a
 fenced variable through string filters, which can strip or alter the
 markers.>

## What you are checking

<The ONE failure mode this judge exists to catch, in one sentence.>

## PASS

<Observable properties of an artifact that passes — a positive definition,
 not "no problems".>

## FAIL

<What the failure mode concretely looks like in this artifact — enumerate
 the forms you expect. Instruct the judge to cite the offending content in
 its rationale.>

## Examples

PASS example: <short excerpt or description of a passing artifact>
  — <one line: why it passes>
FAIL example: <short excerpt of a failing artifact>
  — <one line: which part fails and why>
Borderline: <excerpt near the boundary>
  — <one line: which side it lands on and the deciding property>
```

## Stdout-only skills

When a skill produces no file artifacts (`stdout_only: true` in the analysis),
bare `{{ outputs }}` renders empty — there are no files to list. Any judge that
would normally use bare `{{ outputs }}`, including builtins, must use
`{{ conversation }}` instead. Inline `check` judges should use
`outputs.get("conversation", "")`. Judges on other variables
(`{{ tool_trace }}`, `{{ evidence }}`, etc.) are unaffected.

## Filling the example slots

Real examples beat invented ones. Prior runs are the best source: look under
`$AGENT_EVAL_RUNS_DIR/<eval-name>/<run-id>/cases/<case>/` for actual artifacts
that pass, fail, and sit near the boundary, and quote short excerpts. If no
runs exist yet, write plausible examples and replace them with real excerpts
after the first `/eval-run` — `/eval-review` surfaces exactly the judge-human
disagreements the borderline slot should encode.

## Numeric judges (the exception)

Only when the criterion is genuinely graded — partial credit is meaningful
and a pass/fail boundary would discard it. The structure is the same (task
context, one artifact, ONE criterion), but PASS/FAIL becomes per-level
definitions:

- define every level in terms of observable properties of the artifact — a
  definition a second rater could apply and land on the same number;
- adjacent levels must differ by something observable. If they don't, the
  scale has too many levels: collapse them, or decompose into tiered boolean
  judges instead;
- declare `score_range` on the judge (see `eval-yaml-template.md`).

## What NOT to put in the prompt

- **JSON or response-format boilerplate.** The harness hands the provider the
  verdict schema itself — `submit_evaluation` (pass/fail + rationale) for
  boolean judges, `submit_score` (score + rationale) for numeric ones, as a
  structured output or a forced tool call — so the shape and a rationale are
  already enforced. "Respond with JSON {...}" fights that schema and is
  ignored at best.
- **The numeric bounds.** A declared `score_range` is stated in the judge's
  system prompt and verdict schema automatically. The prompt's job is defining
  what each level *means*, not restating the scale.
- **Your own untrusted-data guard.** The harness fences the rendered artifact
  variables and carries the guard in every LLM judge's system prompt (and in
  the agent judge contract). Put the artifact under an explicit heading and
  stop there.
- **A second criterion.** If the prompt says "also check…", it's two judges.
