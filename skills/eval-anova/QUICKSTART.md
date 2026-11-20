# eval-anova — Quickstart

Run a full-factorial (DoE) experiment comparing agent configs (models / effort / prompts)
across shared test cases, then analyze with repeated-measures ANOVA and render a
model-comparison report.

## 0. Prerequisites
- The agent-eval-harness with the eval-anova skill installed.
- Python 3.11+ and `uv` (or venv).
- Anthropic API access (direct or Vertex AI).

## 1. Install
    uv venv && source .venv/bin/activate
    uv pip install -e ".[anova]"      # scipy, statsmodels, pandas, pingouin

## 2. Environment

**Option A: Google Cloud Vertex AI (recommended for teams)**

    export CLAUDE_CODE_USE_VERTEX=1
    export CLOUD_ML_REGION=global
    export ANTHROPIC_VERTEX_PROJECT_ID=your-project-id
    gcloud auth application-default login        # one-time auth

Both agent runs (via `claude --print`) and LLM judges (via `AnthropicVertex`) use these credentials.

**Option B: Direct Anthropic API**

    export ANTHROPIC_API_KEY=sk-...

**Common (both options)**

    export RHAI_RESULTS_REPO=/path/to/results    # optional archive; local fallback if unset
    # export AGENT_EVAL_RUNS_DIR=eval/runs       # optional, this is the default

## 3. Create an eval with a matrix
Put this at `eval/<name>/eval.yaml` (or project-root `eval.yaml`):

    skill: none                       # or the skill you're testing
    execution:
      mode: case
      arguments: "{prompt}"
      timeout: 120
    runner:
      type: claude-code
    dataset:
      path: dataset/cases             # relative to THIS eval.yaml
      schema: "each case dir has input.yaml with a 'prompt' field"
    judges:
      - name: passes
        check: "..."                  # how each cell is scored (composite pass/fail)
    matrix:
      factors:
        model:
          - claude-opus-4-8
          - claude-sonnet-4-6
          - claude-haiku-4-5-20251001
      replications: 3                 # 1 = noisy screening; 3 = decent; 5+ = high confidence

Add cases under `eval/<name>/dataset/cases/<case>/input.yaml`.
See `references/matrix-schema.md` for the full matrix schema.

## 4. Validate + estimate cost (no execution)
    python3 ${CLAUDE_SKILL_DIR}/scripts/orchestrate.py --config eval/<name>/eval.yaml --dry-run

## 5. Run it
    python3 ${CLAUDE_SKILL_DIR}/scripts/orchestrate.py --config eval/<name>/eval.yaml
Each matrix cell runs `/eval-run` once, so every condition lands as a standard run under
`<runs-dir>/<run-id>/` (with its `summary.yaml` + a `condition.json`). The orchestrator then
writes `<runs-dir>/anova.json` and renders the `/eval-compare` comparison report (which
includes the ANOVA/Pareto statistics section). `<runs-dir>` is the directory holding all of
the config's runs; print it rather than building it yourself:

    python3 ${CLAUDE_SKILL_DIR}/../../scripts/discover.py --runs-dir --config eval/<name>/eval.yaml

## 6. Re-analyze without re-running
    python3 ${CLAUDE_SKILL_DIR}/scripts/orchestrate.py --config eval/<name>/eval.yaml --analyze-only
This re-reads the existing runs' `summary.yaml` files → recomputes `anova.json` → re-renders. It
also works over runs produced elsewhere (e.g. a CI fan-out of `/eval-run`).

## 7. Render reports manually (optional)
    # Comparison (leaderboard, heatmap, + stats section when anova.json exists):
    python3 ${CLAUDE_PLUGIN_ROOT}/skills/eval-compare/scripts/compare.py generate <runs-dir>
    # Statistics-forward deep view for one experiment:
    python3 ${CLAUDE_SKILL_DIR}/scripts/report.py <runs-dir>   # reads anova.json

## Notes
- Use current model IDs (Opus 4.8 `claude-opus-4-8`, Sonnet 4.6 `claude-sonnet-4-6`,
  Haiku 4.5 `claude-haiku-4-5-20251001`).
- Repeated-measures ANOVA assumes the SAME cases run under every condition (it blocks on case
  difficulty). Keep your case set fixed across conditions.
- Sanity-check scoring first: if most cells are 0.0, the grader/judge is probably misconfigured
  — fix that before trusting any ANOVA result.
