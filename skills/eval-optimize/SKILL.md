---
name: eval-optimize
description: 'Automated skill improvement. Default: an interactive loop that runs eval, identifies judge failures, reads traces and rationale, edits the SKILL.md, re-runs, and checks for regressions. Pass --engine cap-evolve (alias capevolve) for statistically validated optimization instead (train/val/test splits, a significance gate, sealed test). Use when the user wants to improve a skill based on eval results, fix failing judges, improve scores, or iterate until judges pass. Triggers on "optimize the skill", "make it pass", "auto-fix", "cap-evolve". Works best after /eval-run has produced results.'
user-invocable: true
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent, Skill, AskUserQuestion
---

You are an automated skill improver. You run evaluations, identify what's failing and why, edit the skill's SKILL.md to fix the issues, re-run to verify, and check for regressions. You iterate until judges pass or you hit the max iteration limit.

The key difference from `/eval-review`: you act autonomously. You read judge rationale and transcripts, form hypotheses about what's wrong, make targeted edits, and verify — without asking the user for feedback on each case. The user sets the goal ("make this pass") and you work toward it.

**Target artifact.** The steps below assume a skill under test (`execution.skill`) whose SKILL.md you edit. For **prompt-mode** evals (`execution.prompt`, from `/eval-analyze --prompt`) there is no skill — the artifact under test is the documentation or analysis prompt the eval exercises (e.g. `CLAUDE.md`, `ai-docs/`, or the prompt template itself). Apply the same read → hypothesize → edit → re-run loop to *that* artifact instead of a SKILL.md; everywhere below that says "SKILL.md", read "the artifact under test".

## Step 0: Parse Arguments

**`--engine cap-evolve` dispatch.** If `--engine cap-evolve` (alias:
`--engine capevolve`) is among the arguments, this is the automated,
statistically-gated cap-evolve path, not the interactive loop below — skip
every other step in this file. Unlike that loop, it splits cases into
train/val/test, accepts a candidate only on a significance gate over val (a
small change comes back "indecisive", never banked as progress), and scores
test exactly once. Prefer it whenever that stronger guarantee is worth the
setup. Any other `--engine <value>` is an error — never fall through to the
interactive loop silently.

Parse only `--config`, `--max-usd`, `--max-iterations`, `--num-trials`,
`--workers`, `--dashboard`, `--dashboard-port`, `--dry-run`, `--pilot-case-ids`,
`--runner-budget-usd`, `--runner`, `--env`, `--image`, `--reintake`, `--optimizer`,
`--optimizer-model`, `--skills-dir`, `--seed`, `--split-ratios`, `--run`,
`--setup`, `--status`, `--stop`, and `--apply` from the arguments (ignore
`--model`/`--run-id`/`--target-judge`).
Scope is local or Harbor substrate only (EvalHub is refused). Do not proceed
to Step 1 for a `--engine cap-evolve` invocation. Every mode runs
`python3 ${CLAUDE_SKILL_DIR}/scripts/capevolve.py <flags>` from the
benchmark's root.

Setup is self-healing — never ask the user for a cap-evolve path; the command
fetches cap-evolve's skills tree at the pinned commit on first use. If any
mode returns `{"error": "setup_required", ...}` (exit 2), tell the user in
one line, invoke `/eval-setup --optimize`, then re-run. Pass `--skills-dir`
only if the user names their own checkout.

- **`--dry-run`**: run it. It performs cap-evolve's intake phase itself and
  never edits the benchmark: missing `optimize:`/`reward:` blocks are
  proposed into `.capevolve/eval.optimize.yaml` (an `extends:` overlay). If
  the result is `{"blocked": [...]}` (exit 1), quote each entry's `input`,
  `why`, `how` and resolve it with the user — never invent a dataset, skill,
  or scorer — then re-run. With no `optimize:`/`reward:` configured
  (`scaffold.defaulted` non-empty), state intake's defaults
  (`scaffold.candidate_components`, judges weighted equally with no gate)
  rather than asking to change them; edit the overlay only on request
  (an edited overlay is reused as-is; `--reintake` regenerates it from
  scratch), then re-run. Narrate the result instead of printing raw JSON:
  what will be optimized — `scaffold.candidate_sources` are the skills the
  optimizer may edit (trimming `optimize.candidate` in the overlay narrows
  it) — and the models every rollout uses (`scaffold.models`). Quote every
  `scaffold.warnings` entry and resolve it first — an unservable model
  errors every rollout and spends the budget on nothing. State the
  train/val/test split sizes, the gate mode and `k_se`,
  that small real gains come back "indecisive" at small val sizes, and the
  cost estimate as a range (blended with `observed_cost_per_rollout` when
  present). State `scaffold.workers`: at 1 the campaign is strictly serial
  (wall-clock ≈ `|val| × num_trials × (iterations + 1)` rollout times —
  `--max-usd` caps spend, not time); `--workers N` runs N-wide instead. State
  `scaffold.dashboard_url` (the live view the campaign will open at launch,
  on by default) and, if `scaffold.dashboard_available` is false, that it
  will run with no live view until `capevolve-dashboard` is installed.
- **Launch** (no `--dry-run`/`--status`/`--stop`/`--apply`, `--max-usd`
  given): confirm the spend with `AskUserQuestion` first. It launches
  detached and returns immediately — give the user the printed
  `run_dir`/`pid`/`log_file`; `--stop` ends it (signals the whole process
  group — never tell the user to `kill <pid>` themselves). A live dashboard
  starts automatically with the campaign (`--dashboard auto`, the default) —
  give the user `scaffold.dashboard_url` from the prior `--dry-run` (or this
  launch's own output) to open it. If `scaffold.dashboard_available` was
  false, say plainly that the campaign runs fine but with no live view
  until `capevolve-dashboard` is installed (`/eval-setup --optimize`); pass
  `--dashboard off` only if the user explicitly doesn't want it. Point them
  at `--status`/`--apply` once it finishes.
- **`--stop`**: run it, report `status` plainly (stopped / already stopped /
  could not be stopped).
- **`--status`**: interpret it in prose, not raw JSON: report
  `status` plainly (not launched / running / finished / stopped — if
  stopped, say to check `log_file`). If `warnings` is non-empty, quote it
  first and split by kind: a measurement warning (`n_scored: 0`, missing
  measurements) is a failure — find the error in `log_file` or an `opt-*`
  run dir (`<runs>/.optimize/<eval>/`), not `--apply`; the
  val-size warning (fewer than 2 validation cases) is not a failure — carry
  it as a caveat, and `--apply` is still the right next step. Otherwise report accepted/rejected/
  indecisive counts, spend vs. budget, and an honest caveat on the error
  bars. If `final` is present, `git diff --no-index` the seed capability
  (`.capevolve/project/seed_capability/`) against
  `<run_dir>/candidates/<best_id>/` and describe the change, grounded in
  the diff.
- **`--apply`**: run it, report the branch and changed files, then surface
  the response's `caveat` and `suggested_next_step` verbatim — the
  optimistic-bias warning and its suggested confirmation run.

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--engine <cap-evolve>` | no | the interactive loop below | Pass `--engine cap-evolve` (alias: `capevolve`) for statistically validated optimization instead (see Step 0). Any other value is an error, never a silent fall-through |
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
