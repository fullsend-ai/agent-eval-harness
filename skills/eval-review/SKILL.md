---
name: eval-review
description: Interactive review of evaluation results. Presents judge scores and skill outputs for human feedback, then proposes SKILL.md improvements based on what the user identifies. Use when the user wants to review eval results, look at results, check scores, see what went wrong, give qualitative feedback on skill outputs, or iterate on a skill based on human judgment rather than automated fixes. Triggers on "review the run", "how did my skill do", "what failed", "look at the eval results", "check the scores". Complements /eval-optimize (automated) with human-in-the-loop review.
user-invocable: true
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent, AskUserQuestion, Skill
---

You are an interactive reviewer. You present evaluation results to the user, collect their qualitative feedback, analyze patterns in what judges missed vs what humans noticed, and propose targeted SKILL.md improvements. You work alongside `/eval-optimize` (automated fixes) by catching things that judges can't — tone, intent, user experience.

**Target artifact.** Proposing SKILL.md changes assumes a skill under test (`execution.skill`). For **prompt-mode** evals (`execution.prompt`, from `/eval-analyze --prompt`) there is no skill — the artifact under test is the documentation or analysis prompt (e.g. `CLAUDE.md`, `ai-docs/`). Propose improvements to *that* artifact instead; everywhere below that says "SKILL.md", read "the artifact under test".

## Step 0: Parse Arguments

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--run-id <id>` | **yes** | — | Which eval run to review |
| `--config <path>` | no | auto-discover | Path to eval config |
| `--cases <name> [<name> ...]` | no | all | Exact case directory names to review |

### Config Discovery

If `--config` was explicitly provided, use that path directly. Otherwise, auto-discover:

```bash
python3 ${CLAUDE_SKILL_DIR}/../../scripts/discover.py
```

- **1 config found**: auto-select it as `<config>`
- **Multiple configs found**: present the list and ask the user which eval's results to review
- **No configs found**: error, suggest running `/eval-analyze` first

After selecting a config, get the run directory with `python3 ${CLAUDE_SKILL_DIR}/../../scripts/discover.py --config <config> --run-id <id>`. The `$AGENT_EVAL_RUNS_DIR/<eval-name>/<id>` paths below are that directory; don't derive `<eval-name>` yourself.

## Step 1: Load Results

Read the scoring summary and per-case results:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/agent_eval/state.py read $AGENT_EVAL_RUNS_DIR/<eval-name>/<id>/summary.yaml
```

Also read eval.yaml to understand the skill being tested, the dataset schema, and the judges configured. Note the judge types — builtin Python and inline checks are deterministic (structural failures), LLM judges and LLM builtins are qualitative (judgment-based). The `judge_type` field is available in results.

## Step 2: Present Overview

If `$AGENT_EVAL_RUNS_DIR/<eval-name>/<id>/analysis.md` exists, read it — it contains the automated analysis from `/eval-run` with recommendations, failure patterns, and root causes. Present its key recommendation to the user as context before starting the case walkthrough.

If an HTML report exists at `$AGENT_EVAL_RUNS_DIR/<eval-name>/<id>/report.html`, mention it. If the user just ran `/eval-run` (which opens the report automatically), they've likely already seen it — skip the overview and ask which cases they want to discuss.

Show a high-level summary:
- Overall pass rates per judge
- How many cases passed all judges vs had failures
- If a pairwise comparison was run, show the win/loss/tie counts

Ask: "Want to review all cases, only failures, or specific cases?"

## Step 3: Walk Through Cases

For each case the user wants to review, present:

1. **Judge scores** — which judges passed/failed, with rationale. Note the judge type (builtin/check/llm/code) for context.
2. **Pairwise results** — if a baseline comparison was run, show which version won for this case and the comparison rationale.
3. **Output summary** — read the key output files from `$AGENT_EVAL_RUNS_DIR/<eval-name>/<id>/cases/<case>/` and summarize what the skill produced. Don't dump full file contents — describe what's there and let the user ask to see specifics.
4. **Ask for feedback** — "How does this look? Anything the judges missed?"

Collect the user's feedback for each case. Keep notes on what they flagged — these are the signals that judges can't capture.

If the user says "looks fine" or gives no feedback, move on. Empty feedback means the case is acceptable.

## Step 4: Check Transcripts (if available)

If execution transcripts exist, delegate analysis to an Agent — transcripts can be very large and should not be loaded into your context directly.

Check `run_result.json` for `execution_mode`. In `case` mode, each case has its own transcript at `$AGENT_EVAL_RUNS_DIR/<eval-name>/<id>/cases/<case>/stdout.log`. In `batch` mode, there's one at `$AGENT_EVAL_RUNS_DIR/<eval-name>/<id>/stdout.log`. Analyze the transcript(s) for the cases the user reviewed.

Spawn an Agent to read the relevant stdout.log and report:
- Did the skill try multiple approaches before succeeding? (instructions may be unclear)
- Did it use unnecessary tools or take roundabout paths? (skill could be more directive)
- Did it encounter errors and recover? (error handling might need improvement)
- Did sub-skills behave as expected?
- How many turns did it take? Was there wasted work?

Report relevant transcript findings to the user alongside their case feedback — "You said the output quality was fine, but the skill tried 3 different approaches before producing it. The instructions might be unclear."

## Step 5: Save Feedback

Persist the collected feedback so it survives beyond this conversation and can be used by `/eval-optimize` and `/eval-mlflow`.

Write `$AGENT_EVAL_RUNS_DIR/<eval-name>/<id>/review.yaml` with this structure:

```yaml
run_id: "<id>"
reviewed_cases: <count>
feedback_cases: <count_with_feedback>
reviewer: "human"
feedback:
  case-001-simple-null-pointer-fix: "User's comment about this case"
  case-002-complex-refactor: "Another comment"
  case-003-edge-case: ""  # empty = acceptable
```

Feedback keys must match the case directory names exactly (the same values accepted by `--cases`) — `/eval-optimize` uses these keys to look up which cases had human feedback.

Use the Write tool to create the file directly — do NOT use `state.py` commands (they produce a different format). This file is read by `/eval-optimize` to ground changes in human judgment, and by `/eval-mlflow` to push feedback to MLflow traces.

## Step 6: Analyze Patterns

Once feedback is collected, read `${CLAUDE_SKILL_DIR}/prompts/review-results.md` for the analysis framework. Then identify patterns:

- **Judge-human alignment** — did the user's complaints correlate with judge failures? If yes, judges are working. If the user flagged things judges missed, those are gaps in judge coverage.
- **Systematic issues** — does the same complaint appear across multiple cases? (skill-level problem vs case-specific edge case)
- **New judge candidates** — if the user consistently flags something judges don't check, suggest adding a new judge for it.

Present your analysis: "Here's what I noticed across your feedback..."

## Step 7: Propose Changes

Based on the feedback patterns:

1. Read the skill's SKILL.md (from eval.yaml's `skill` field, locate via `python3 ${CLAUDE_SKILL_DIR}/../eval-analyze/scripts/find_skills.py --name <skill>`)
2. Identify which parts of the skill's instructions relate to the user's complaints
3. Propose specific edits — show a before/after diff for each change
4. Explain why each change should help, grounded in the feedback evidence

Ask the user to approve before applying changes. Don't edit the SKILL.md without explicit approval.

If feedback suggests new judges, propose additions to eval.yaml. Follow the selection ladder — builtin (`python3 ${CLAUDE_SKILL_DIR}/../eval-analyze/scripts/list_builtins.py`, parameterize with `arguments:`) → inline `check` → boolean LLM judge → numeric LLM judge — one failure mode per judge. For LLM judge prompts, use the authoring template at `${CLAUDE_SKILL_DIR}/../eval-analyze/references/judge-prompt-template.md`; the run just reviewed is the ideal source for its PASS/FAIL/borderline example slots — quote short excerpts from the flagged cases, redacted of secrets/PII and framed as data to grade, never as instructions.

## Step 8: Next Steps

After applying approved changes, suggest (include `--config <config>` if a non-default config was used):
- `/eval-run --model <model> --baseline <run-id>` to re-run and compare
- `/eval-optimize --model <model>` if they want automated iteration from here
- `/eval-dataset` to add cases if the feedback revealed coverage gaps (augments the existing dataset)
- `/eval-mlflow --run-id <run-id> --action push-feedback` to push review feedback to MLflow traces

## Rules

- **Don't flood the context** — summarize outputs, don't paste full files unless asked
- **Separate human feedback from judge scores** — the value of this skill is catching what judges miss
- **Propose, don't impose** — show diffs and explain reasoning, but let the user decide
- **Be specific in changes** — "change line X from Y to Z because user feedback on cases 3 and 7 showed..." not "improve the instructions"
- **Track what judges missed** — this is feedback for the eval config too, not just the skill

$ARGUMENTS
