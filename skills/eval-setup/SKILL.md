---
name: eval-setup
description: Optional environment configurator for the agent-eval-harness. Configures MLflow tracking, verifies API keys, and troubleshoots dependency issues. Detects available skills and agentic documentation (CLAUDE.md, AGENTS.md, ai-docs/) to suggest appropriate evaluation modes. Not required for basic usage — dependencies auto-install via SessionStart hook and agent_eval is available via symlinks. Use when the user wants to configure MLflow tracking, troubleshoot import errors, verify the environment, or set up a remote MLflow server. Also triggers on "configure mlflow", "set up tracking", "ModuleNotFoundError", "mlflow not installed", "missing dependencies", or "check my eval environment".
user-invocable: true
allowed-tools: Read, Bash, Glob, AskUserQuestion
---

You are an environment configurator. You verify the evaluation harness environment, configure optional integrations like MLflow, and suggest evaluation modes based on what's available in the repository. Non-destructive: skip steps that are already done, report status.

Most users can skip this skill entirely — dependencies auto-install via the plugin's SessionStart hook, and `agent_eval` is available to scripts via symlinks. This skill is useful for configuring MLflow tracking, troubleshooting dependency issues, verifying the environment, and discovering what evaluation modes are available.

The eval pipeline is: `/eval-analyze` → `/eval-dataset` → `/eval-run` → `/eval-review` or `/eval-optimize`. `/eval-mlflow` can be invoked at any point after `/eval-run`. MLflow tracing is handled by `/eval-mlflow` after a run completes. No tracing setup is needed here.

## Step 0: Parse Arguments

Parse `$ARGUMENTS` for:

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--tracking-uri <uri>` | no | auto-detect | MLflow tracking URI (skips interactive setup) |
| `--skip-mlflow` | no | false | Skip MLflow setup entirely |
| `--runs-dir <path>` | no | `eval/runs` | Directory where eval runs are stored |
| `--harbor` | no | false | Install Harbor + Kubernetes for containerized execution (~650 MB) |
| `--optimize` | no | false | Install cap-evolve-core and fetch its pinned skills tree for `/eval-optimize --engine capevolve` |

## Step 1: Install Dependencies (if needed)

Dependencies are managed in an isolated venv at `<plugin_root>/.eval-venv/`. The SessionStart hook creates this venv automatically. Scripts auto-activate it via `agent_eval._bootstrap` on import.

This step is a fallback for mid-session installs or troubleshooting. Re-run the hook's install script:

```bash
python3 "${CLAUDE_SKILL_DIR}/../../scripts/ensure_deps.py" "${CLAUDE_PLUGIN_DATA:-${XDG_STATE_HOME:-$HOME/.local/state}/agent-eval-data}"
```

To check the venv status:

```bash
VENV_PYTHON="${CLAUDE_SKILL_DIR}/../../.eval-venv/bin/python3"
test -x "$VENV_PYTHON" && echo "venv: OK" || echo "venv: MISSING"
"$VENV_PYTHON" -c "import yaml; print('pyyaml: OK')" 2>&1 || echo "pyyaml: MISSING"
```

To install additional packages manually into the venv:

```bash
VENV_DIR="${CLAUDE_SKILL_DIR}/../../.eval-venv"
# Use uv if available, otherwise venv pip
if command -v uv &>/dev/null; then
  uv pip install --python "$VENV_DIR/bin/python3" 'mlflow[genai]>=3.5' 'anthropic[vertex]>=0.77'
else
  "$VENV_DIR/bin/pip" install 'mlflow[genai]>=3.5' 'anthropic[vertex]>=0.77'
fi
```

If `--harbor` was passed, also install Harbor and the Kubernetes client
for containerized execution (`/eval-run --runner harbor`,
`/eval-dataset --harbor`):

```bash
VENV_DIR="${CLAUDE_SKILL_DIR}/../../.eval-venv"
if command -v uv &>/dev/null; then
  uv pip install --python "$VENV_DIR/bin/python3" harbor 'litellm<1.107' 'kubernetes>=29.0,<37.0'
else
  "$VENV_DIR/bin/pip" install harbor 'litellm<1.107' 'kubernetes>=29.0,<37.0'
fi
```

If `--optimize` was passed, also install cap-evolve for `/eval-optimize
--engine capevolve`. This needs nothing from the user — no cap-evolve
checkout, no path to provide. cap-evolve-core requires Python 3.11+, so check
the venv first:

```bash
VENV_DIR="${CLAUDE_SKILL_DIR}/../../.eval-venv"
"$VENV_DIR/bin/python3" -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
  && echo "venv python: OK" || echo "venv python: TOO_OLD"
```

On `TOO_OLD`, the venv was built by an older default `python3`. It holds only
installed packages, so rebuild it with a 3.11+ interpreter (find one with
`ls /opt/homebrew/bin/python3.1* /usr/local/bin/python3.1* /usr/bin/python3.1* 2>/dev/null`;
if none exists, ask the user to install one, e.g. `brew install python@3.12`):

```bash
VENV_DIR="${CLAUDE_SKILL_DIR}/../../.eval-venv"
rm -rf "$VENV_DIR"
<python3.11+> "${CLAUDE_SKILL_DIR}/../../scripts/ensure_deps.py" "${CLAUDE_PLUGIN_DATA:-${XDG_STATE_HOME:-$HOME/.local/state}/agent-eval-data}"
```

Then install the pinned engine package and fetch its matching skills tree:

```bash
VENV_DIR="${CLAUDE_SKILL_DIR}/../../.eval-venv"
if command -v uv &>/dev/null; then
  uv pip install --python "$VENV_DIR/bin/python3" -e "${CLAUDE_SKILL_DIR}/../..[optimize]"
else
  "$VENV_DIR/bin/pip" install -e "${CLAUDE_SKILL_DIR}/../..[optimize]"
fi
python3 ${CLAUDE_SKILL_DIR}/../eval-optimize/scripts/capevolve.py --setup
```

`--setup` verifies cap-evolve-core and fetches cap-evolve's skills tree
(phases, algorithms, templates — not on PyPI) at exactly the commit
`pyproject.toml` pins, into the plugin data dir. Report its `skills_dir` and
`pinned_sha`. A pin bump fetches a fresh copy, so the package and the tree
never drift; `/eval-optimize --engine capevolve` also fetches it on first use,
so this step only warms it. An `{"error": "setup_required", ...}` result means
the install above failed — show its `reason`. A user who keeps their own
cap-evolve checkout can set `CAPEVOLVE_SKILLS_DIR`; it is used only when it is
at the pinned commit.

## Step 2: Run Preflight Checks

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/check_env.py --fix
```

Review the output. If all checks pass, report success and skip to Step 6.

If checks fail, work through Steps 3–5 to fix them.

## Step 3: Configure MLflow Tracking

If `--skip-mlflow` was passed, skip this step entirely.

Check if MLflow tracking is configured:

```bash
echo "MLFLOW_TRACKING_URI=${MLFLOW_TRACKING_URI:-not set}"
```

**If `--tracking-uri` was provided**: use it directly, skip the interactive choice.

**If not set and no flag**: Ask the user which MLflow setup they want:

1. **Local server** (recommended for getting started):
   Tell the user to run the server in a separate terminal:
   ```bash
   mlflow server --port 5000
   ```
   Then set the tracking URI in this session:
   ```bash
   export MLFLOW_TRACKING_URI=http://127.0.0.1:5000
   ```
   Note: the user should add this export to their shell profile for persistence.

2. **Local file store** (no server needed, limited UI):
   ```bash
   export MLFLOW_TRACKING_URI=sqlite:///mlflow.db
   ```

3. **Remote server** (Databricks, etc.):
   Ask the user for their tracking URI and verify connectivity.

**Per-project pinning**: To pin a tracking URI to a specific eval suite (overriding the env var), set `mlflow.tracking_uri` in eval.yaml. Useful when one machine runs evals against multiple servers. The harness resolves URIs in this order: `mlflow.tracking_uri` in eval.yaml > `MLFLOW_TRACKING_URI` env var > `http://127.0.0.1:5000`.

## Step 4: Configure API Keys

Check authentication:

```bash
echo "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:+set}"
echo "ANTHROPIC_VERTEX_PROJECT_ID=${ANTHROPIC_VERTEX_PROJECT_ID:-not set}"
```

If neither is set, tell the user:
- For direct Anthropic API: `export ANTHROPIC_API_KEY=<key>`
- For Vertex AI: `export ANTHROPIC_VERTEX_PROJECT_ID=<project-id>`

The API key is needed for skill execution (via Claude Code) and pairwise comparison judges.

## Step 5: Configure Runs Directory

Check if the runs directory is configured:

```bash
echo "AGENT_EVAL_RUNS_DIR=${AGENT_EVAL_RUNS_DIR:-eval/runs}"
```

If `--runs-dir` was provided, use it. Otherwise, the default `eval/runs` is fine for most projects.

If the user wants a non-default location (e.g., larger disk, shared storage), tell them to add to their shell profile:
```bash
export AGENT_EVAL_RUNS_DIR=<path>
```

All harness scripts read this env var. The directory is created automatically by `check_env.py --fix`.

## Step 5b: Check Skill-Specific Environment Variables

If eval.yaml exists and has `execution.env` entries with `$VAR` references, those variables must be set in the caller's environment at eval-run time. Check whether they're available:

```bash
test -f eval.yaml && PYTHONPATH=${CLAUDE_SKILL_DIR}/scripts python3 -c "
from agent_eval.config import EvalConfig
config = EvalConfig.from_yaml('eval.yaml')
import os
for key, value in config.execution.env.items():
    if isinstance(value, str) and value.startswith('\$'):
        var_name = value[1:]
        status = 'set' if os.environ.get(var_name) else 'NOT SET'
        print(f'  {key}: \${var_name} → {status}')
    else:
        # Mask literal values to avoid leaking credentials in logs
        print(f'  {key}: (literal, {len(str(value))} chars)')
" 2>&1 || echo "  (could not parse eval.yaml)"
```

If any `$VAR` references are unset, warn the user — they'll need to `export` them before running `/eval-run`. Common examples: `JIRA_SERVER` for jira-emulator, `JIRA_TOKEN` for Jira API access.

## Step 6: Create MLflow Experiment

If `--skip-mlflow` was passed, skip this step.

Check if eval.yaml exists and has `mlflow.experiment` configured:

```bash
test -f eval.yaml && echo "CONFIG_EXISTS" || echo "NO_CONFIG"
```

If eval.yaml exists:

```bash
PYTHONPATH=${CLAUDE_SKILL_DIR}/scripts python3 -c "
from agent_eval.config import EvalConfig
from agent_eval.mlflow.experiment import setup_experiment, resolve_tracking_uri
config = EvalConfig.from_yaml('eval.yaml')
if config.mlflow.experiment:
    setup_experiment(config.mlflow.experiment, tracking_uri=resolve_tracking_uri(config))
    print(f'Experiment created: {config.mlflow.experiment} on {resolve_tracking_uri(config)}')
else:
    print('No mlflow.experiment in eval.yaml, skipping')
"
```

If eval.yaml doesn't exist, skip this step — it will be created by `/eval-analyze`.

## Step 7: Detect Agentic Documentation

Check if the repository has agentic documentation that can be evaluated:

```bash
HAS_AGENTIC_DOCS=false
if [ -f CLAUDE.md ] || [ -f AGENTS.md ] || [ -d ai-docs ]; then
    HAS_AGENTIC_DOCS=true
    echo "Detected agentic documentation:"
    [ -f CLAUDE.md ] && echo "  - CLAUDE.md"
    [ -f AGENTS.md ] && echo "  - AGENTS.md"
    [ -d ai-docs ] && echo "  - ai-docs/"
fi
```

## Step 8: Final Verification

Run the preflight checks again to confirm everything is set up:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/check_env.py
```

If eval.yaml exists, also validate it:

```bash
test -f eval.yaml && python3 ${CLAUDE_SKILL_DIR}/scripts/check_env.py --config eval.yaml
```

Report the final status to the user and suggest next steps.

**Present evaluation modes**:
1. **Skill-based** — analyze and test a specific skill (`--skill`)
2. **Prompt-based** — test agent capabilities directly (`--prompt`, see examples/)

**Next steps structure**:
- If eval.yaml doesn't exist:
  - If skills exist: suggest `/eval-analyze --skill <name>` (list discovered skills)
  - Also mention: `/eval-analyze --prompt <path>` for prompt-based evaluation
- If eval.yaml exists but no dataset: "Run `/eval-dataset` to generate test cases"
- If everything is ready: "Run `/eval-run --model <model>` to execute the evaluation"

Always include the full pipeline path: analyze → dataset → run → review/optimize → mlflow

## Rules

- **Non-destructive** — skip steps that are already done, don't overwrite existing config
- **Report clearly** — show what passed, what failed, and how to fix each failure
- **MLflow is optional** — the harness works without it. Don't fail setup if MLflow can't be configured.
- **Suggest the full pipeline** — after setup, the user should know the path: analyze → dataset → run → review

$ARGUMENTS
