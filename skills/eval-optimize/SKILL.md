---
name: eval-optimize
description: Automated skill improvement loop. Runs eval, identifies judge failures, reads traces and rationale, edits the SKILL.md to fix issues, re-runs to verify, and checks for regressions. Use when the user wants to automatically improve a skill based on eval results, fix failing judges, make the skill better, auto-fix quality issues, improve scores, or iterate until all judges pass. Triggers on "optimize the skill", "make it pass", "auto-fix", "improve the scores", "why is it failing". Works best after /eval-run has produced results to learn from.
user-invocable: true
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent, Skill, AskUserQuestion
---

You are an automated skill improver. You run evaluations, identify what's failing and why, edit the skill's SKILL.md to fix the issues, re-run to verify, and check for regressions. You iterate until judges pass or you hit the max iteration limit.

The key difference from `/eval-review`: you act autonomously. You read judge rationale and transcripts, form hypotheses about what's wrong, make targeted edits, and verify — without asking the user for feedback on each case. The user sets the goal ("make this pass") and you work toward it.

**Target artifact.** The steps below assume a skill under test (`execution.skill`) whose SKILL.md you edit. For **prompt-mode** evals (`execution.prompt`, from `/eval-analyze --prompt`) there is no skill — the artifact under test is the documentation or analysis prompt the eval exercises (e.g. `CLAUDE.md`, `ai-docs/`, or the prompt template itself). Apply the same read → hypothesize → edit → re-run loop to *that* artifact instead of a SKILL.md; everywhere below that says "SKILL.md", read "the artifact under test".

## Step 0: Parse Arguments

**`--engine capevolve` dispatch.** If `--engine capevolve` is among the
arguments, this is the automated, statistically-gated cap-evolve path, not
the interactive loop below — skip every other step in this file. Parse only
`--config`, `--max-usd`, `--max-iterations`, `--num-trials`, `--dry-run`,
`--pilot-case-ids`, `--runner-budget-usd`, `--runner`, `--env`, `--image`, `--reintake`,
`--optimizer`, `--optimizer-model`, `--skills-dir`, `--status`, and `--apply`
from the arguments (ignore `--model`/`--run-id`/`--target-judge`, which don't
apply here). See `docs/capevolve-poc.md` for what each mode does and the
current scope limits (local or Harbor substrate; EvalHub is refused). Do not proceed to Step 1 below for a
`--engine capevolve` invocation. The underlying command for every mode is
`python3 ${CLAUDE_SKILL_DIR}/scripts/capevolve.py <flags>`, run from the
benchmark's root (`--config` defaults to the benchmark's eval config).

If `--skills-dir` wasn't given, check `tmp/optimize-config.yaml` for a
`capevolve_skills_dir` value (written by `/eval-setup --optimize`) and use
that instead of asking the user again.

- **`--dry-run`**: run it. The command performs cap-evolve's intake phase
  itself and never edits the benchmark: missing `optimize:`/`reward:` blocks
  are proposed into `.capevolve/eval.optimize.yaml` (an `extends:` overlay).
  If the result is `{"blocked": [...]}` (exit 1), quote each entry's `input`,
  `why`, and `how` to the user and resolve it with them — never invent a
  dataset, skill, or scorer to get past it — then re-run. When the eval config
  has no `optimize:`/`reward:` (`scaffold.defaulted` is non-empty), the run
  uses intake's defaults as-is: tell the user what they are
  (`scaffold.candidate_components`, and every judge weighted equally with no
  pass/fail gate) without asking whether to change them. Edit the overlay only
  if the user asks to (an edited overlay is reused; `--reintake` regenerates
  it), then re-run `--dry-run`. Then narrate the
  result — don't just print the JSON: state the train/val/test split sizes, the gate
  mode and `k_se`, the qualitative point that smaller real gains come back
  "indecisive" rather than "accept" at small val sizes, and the cost estimate
  as a **range**, blended with `observed_cost_per_rollout` when the response
  includes one (calibrated from this eval's own past runs).
- **Launch** (no `--dry-run`/`--status`/`--apply`, `--max-usd` given): before
  running the command, confirm the spend with the user via `AskUserQuestion`
  — state the budget, the iteration/trial counts, and the cost estimate from
  the prior `--dry-run` if you have it in context. Only run the launch command
  after they confirm. It launches the campaign **detached** and returns
  immediately — it does not block waiting for the result. Tell the user that,
  give them the printed `run_dir`/`pid`/`log_file`, and how to stop it
  (the response's `stop_hint`, e.g. `kill <pid>`). Point them at `--status`
  (from any later session — it needs no `--config`, since the campaign runs
  detached from this one) to check progress and `--apply` once it finishes.
- **`--status`**: run it, then interpret the result in prose rather than
  printing raw JSON: report `status` plainly (not launched / running /
  finished / stopped — if stopped, say to check `log_file`), the
  accepted/rejected/indecisive iteration counts, spend vs. budget, and an
  honest caveat on the error bars (the `spent`/`budget` figures carry no
  variance info by themselves, and gains near the gate's resolution are
  uncertain). If `final` is present, also summarize what the winning
  candidate changed: `git diff` the seed capability
  (`.capevolve/project/seed_capability/`) against
  `<run_dir>/candidates/<best_id>/` and describe the change in a sentence or
  two, grounded in the diff, not invented.
- **`--apply`**: run it, report the branch name and changed files, then
  surface the response's `caveat` and `suggested_next_step` verbatim as the
  closing guidance — this is the doc's optimistic-bias warning (the sealed
  test delta is honest; a full `/eval-run` will look better because some
  cases were seen during the search) and its suggested confirmation run.

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--config <path>` | no | auto-discover | Path to eval config |
| `--model <model>` | no | `models.skill` from eval.yaml | Model to use for eval runs (overrides config default) |
| `--max-iterations <N>` | no | 3 | Stop after N improvement cycles |
| `--run-id <id>` | no | auto-generated | Base run ID (iterations append `-iter-N`) |
| `--target-judge <name>` | no | all judges | Focus on a specific failing judge |

### Config Discovery

If `--config` was explicitly provided, use that path directly. Otherwise, auto-discover:

```bash
python3 ${CLAUDE_SKILL_DIR}/../../scripts/discover.py
```

- **1 config found**: auto-select it as `<config>`
- **Multiple configs found**: present the list and ask the user which eval to optimize
- **No configs found**: suggest running `/eval-analyze` first

After selecting a config, read its `skill` field to set `<eval-name>` (used in `$AGENT_EVAL_RUNS_DIR/<eval-name>/<id>` paths below).

```bash
mkdir -p tmp
python3 ${CLAUDE_SKILL_DIR}/scripts/agent_eval/state.py init tmp/optimize-config.yaml \
  model=<model> max_iterations=<N> run_id=<id> target_judge=<judge>
```

## Step 1: Initial Eval Run

If no recent eval results exist, run the eval suite first:

```text
Use the Skill tool to invoke /eval-run --run-id <id>-iter-0 --config <config> [--model <model>]
```

Pass `--model` only if the user provided one — otherwise let `/eval-run` fall back to `models.skill` from eval.yaml. Pass the same model on every iteration for comparable results.

If results already exist (the user just ran `/eval-run`), skip this and use the existing run.

Read the results:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/agent_eval/state.py read $AGENT_EVAL_RUNS_DIR/<eval-name>/<id>-iter-0/summary.yaml
```

If all judges pass, report success and exit — nothing to improve.

## Step 2: Identify Failures

From `summary.yaml`, identify:

1. **Which judges failed** — and on which cases
2. **Failure rationale** — what did each judge say about why it failed?
3. **Failure patterns** — does one judge fail everywhere (systematic) or only on specific cases (input-dependent)?

Also check for human feedback — these catch things judges miss:

```bash
test -f $AGENT_EVAL_RUNS_DIR/<eval-name>/<id>/review.yaml && echo "REVIEW_EXISTS" || echo "NO_REVIEW"
```

If `review.yaml` exists, read its `feedback` section (human feedback from `/eval-review`) and `mlflow_feedback` section (annotations pulled from MLflow UI). Human feedback is higher-signal than judge rationale — prioritize issues the user flagged.

If `--target-judge` was specified, focus only on that judge's failures.

Build a failure map, noting each judge's type (`judge_type` in results: `builtin`, `check`, `llm`, `code`) — the type determines what you can do about it:

- **builtin**: versioned, shared judges from `agent_eval/judges/`. Don't edit their code — suggest adjusting `arguments:` in eval.yaml (e.g., raising `max_cost_usd` for `cost_budget`)
- **check**: inline Python in eval.yaml. Read the snippet to understand exactly what's checked — failures are deterministic and reproducible
- **llm**: LLM prompt judges. Read the prompt to understand scoring criteria — the failure may be in the skill output or in an overly strict prompt
- **code**: external Python module. Read the function to understand the validation logic

```
judge_name (type) → [case_id, case_id, ...] → rationale for each
human_review → [case_id, ...] → user comment for each
```

## Step 3: Analyze Root Causes

For each failure pattern, investigate why the skill produces bad output:

1. **Read the skill's SKILL.md** — locate it via eval.yaml's `skill` field:
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/../eval-analyze/scripts/find_skills.py --name <skill>
   ```

2. **Read transcripts** (if available) — transcripts can be very large, so delegate to an Agent. Check `run_result.json` for `execution_mode`: in `case` mode, each case has its own transcript at `$AGENT_EVAL_RUNS_DIR/<eval-name>/<id>/cases/<case>/stdout.log`; in `batch` mode, there's one at `$AGENT_EVAL_RUNS_DIR/<eval-name>/<id>/stdout.log`. Focus on the failing cases.

   Include the specific judge failure in the prompt so the agent traces the causal chain rather than producing a generic summary:
   ```text
   Agent tool, subagent_type="Explore": "Read the transcript at <path>.
   The judge '<judge_name>' (<judge_type>) failed this case with rationale:
   '<rationale from summary.yaml>'

   Find evidence explaining WHY this failure happened:
   - Where in the transcript did the skill handle (or skip) the relevant task?
   - What instructions from SKILL.md led to this behavior?
   - Did the skill attempt the right thing but produce wrong output, or skip it entirely?
   - If it tried multiple approaches, which one stuck and why?"
   ```

   In `batch` mode, failures across cases may interact — ask the agent to check whether earlier cases' processing affected later ones.

3. **Read failing case outputs** — use an Explore agent to examine the actual output files. Include what the judge expected so the comparison is targeted:
   ```text
   Agent tool, subagent_type="Explore": "Read the outputs in $AGENT_EVAL_RUNS_DIR/<eval-name>/<id>/cases/<failing_case>/.
   The judge '<judge_name>' failed with: '<rationale>'.
   Compare the actual output against this expectation — what specifically is missing or wrong?"
   ```

4. **Form hypotheses** — connect the judge rationale + transcript evidence + output examination to specific parts of the SKILL.md. Be specific: "The judge says the output is missing acceptance criteria. The transcript shows the skill skipped Step 4 of the pipeline. Step 4 in SKILL.md says 'optionally add acceptance criteria' — the word 'optionally' is the problem."

## Step 4: Edit the Skill

Apply targeted fixes to the SKILL.md. For each edit:

- **Ground it in evidence** — cite the judge name, failing cases, and transcript evidence
- **Be surgical** — change the minimum needed. Don't rewrite sections that are working.
- **Explain the why** — use the Skill Creator's principle: explain to the model why the change matters, rather than adding rigid MUSTs
- **Don't overfit** — if only 1 of 20 cases fails, the fix should be general enough to help without breaking the other 19

Show each edit before applying. If the change is risky (could affect passing cases), note it.

**Execution mode context**: check `execution.mode` in eval.yaml. In `case` mode, each case runs in its own isolated workspace with all case files copied in — the skill receives case-specific arguments resolved from input.yaml. In `batch` mode, all cases are in one workspace via batch.yaml. Your edits must work for the configured mode.

## Step 5: Re-Run and Verify

Re-run eval with the baseline flag. If only a subset of cases failed, target them with `--cases` for faster verification. Once targeted cases pass, do a final full run (all cases) to check for regressions.

Consider `--no-llm-judges` when you only need to verify structural fixes — it skips LLM API calls and runs only deterministic judges (check, Python builtins), which is faster and cheaper.

```text
# Targeted re-run (failing cases only)
Use the Skill tool to invoke /eval-run --run-id <id>-iter-<N> --cases <failing-case-id> [<failing-case-id> ...] --baseline <id>-iter-<N-1> --config <config> [--model <model>]

# Full re-run (all cases) — use for final verification
Use the Skill tool to invoke /eval-run --run-id <id>-iter-<N> --baseline <id>-iter-<N-1> --config <config> [--model <model>]
```

Read the new results:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/agent_eval/state.py read $AGENT_EVAL_RUNS_DIR/<eval-name>/<id>-iter-<N>/summary.yaml
```

Check:
- **Fixed**: did the targeted failures pass now?
- **Regressions**: did any previously passing cases/judges now fail?
- **Score improvement**: did aggregate scores improve?

## Step 6: Handle Regressions

If the fix caused regressions (previously passing cases now fail):

1. **Assess severity** — is the regression worse than the original failure?
2. **If minor** — the fix is still a net positive, continue
3. **If major** — revert the edit and try a different approach. The skill's instructions may need a different framing rather than a different rule.
4. **If stuck** — report to the user what you tried and why it didn't work. Suggest `/eval-review --run-id <id>` for human input on the tricky cases.

## Step 7: Iterate or Report

If failures remain and iterations < max:
- Go back to Step 2 with the new results
- Each iteration should target different failures or try different approaches for persistent ones

If all judges pass:
- Report success: which edits fixed which failures, how many iterations it took
- Show the final summary.yaml scores

If max iterations reached with failures remaining:
- Report what was fixed and what couldn't be fixed
- For persistent failures, explain what you tried and why it didn't work
- Suggest `/eval-review --run-id <final-id>` for human assessment of the remaining issues
- Suggest `/eval-dataset` to add cases if failures suggest missing test coverage (augments the existing dataset)

In all cases (include `--config <config>` if a non-default config was used):
- Suggest `/eval-mlflow --run-id <final-id>` to log the optimization results to MLflow for tracking.

## Rules

- **Never make broad, generic changes** — every edit must be grounded in a specific failure with evidence from judges and transcripts
- **Check for regressions after every edit** — a fix that breaks other cases is not a fix
- **Stop after max iterations** — don't loop forever. Report what couldn't be fixed.
- **Don't modify test cases or judges** — the eval harness is the ground truth. If you think a judge is wrong, report it but don't change it. Builtin judges (from `agent_eval/judges/`) are versioned and shared — never edit their code. If a builtin judge's behavior needs adjustment, suggest changing its `arguments:` in eval.yaml instead. For inline check or LLM prompt judges, suggest improvements to the user but don't edit eval.yaml yourself.
- **Don't modify eval.yaml** — your job is to improve the skill, not the evaluation config. If judges or arguments need updating, suggest it to the user.
- **Try different approaches** — if the same type of edit fails twice, try a fundamentally different framing. Explain why instead of adding more rules.

$ARGUMENTS
