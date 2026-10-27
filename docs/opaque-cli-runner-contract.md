# Opaque CLI Runner Contract

The opaque CLI runner (`runner.type: cli`) delegates execution to an arbitrary
command. This document defines the contract between the harness and the external
command so that downstream features (judges, scoring, reporting, regression
detection, MLflow) work correctly.

To run a skill through `fullsend run`, use the first-class `runner.type: fullsend`
rather than wrapping fullsend in a `cli` command. It maps fullsend's run directory
(stream-json, `metrics.json`, subagent transcripts) onto the result for you (see
[runners → fullsend](../website/concepts/runners.md#fullsend)).

## Placeholders

The command template receives these placeholders, resolved before execution:

| Placeholder | Value | Source |
|---|---|---|
| `{agent}` | Skill name or agent definition path | `run_skill(skill_name=...)` |
| `{workspace}` | Absolute path to the case workspace | Created by `workspace.py` |
| `{output_dir}` | Absolute path to `{workspace}/output` | Created by the opaque CLI runner |
| `{model}` | Model identifier (e.g. `sonnet`, `gpt-4`) | `--model` CLI flag or `models.skill` |
| `{subagent_model}` | Subagent model identifier (empty if unset) | `models.subagent` or `--subagent-model` |
| `{timeout}` | Timeout in seconds (string) | `execution.timeout` or harness default |
| `{max_budget_usd}` | Budget cap (string) | `execution.max_budget_usd` or harness default |
| `{effort}` | Reasoning effort level (empty if unset) | `runner.effort` or `--effort` |
| `{system_prompt}` | System prompt text (empty if unset) | `runner.system_prompt` |
| `{args}` | Resolved skill arguments | `execution.arguments` with `{field}` substitution |
| `{field}` | Any field from `input.yaml` | Case-specific; does not override builtins above |

## Contract: what the command MUST do

| Requirement | Why | What breaks without it |
|---|---|---|
| Exit 0 on success, non-zero on failure | Exit code drives pass/fail status in scoring and reporting. `-1` means timeout. | Cases always show as FAIL; worst-exit-code aggregation inflates failure counts |
| Write artifact files to `{output_dir}` or to the paths declared in `outputs[*].path` | `collect.py` scans these paths to gather artifacts for judges | Judges receive an empty `files` dict; all file-based checks fail |
| Finish before `{timeout}` seconds | The harness kills the process at the deadline via `subprocess.run(timeout=...)` | Process is killed; result recorded as exit `-1` with "Timed out" stderr |

## Contract: what the command SHOULD do

| Recommendation | Why | What degrades without it |
|---|---|---|
| Use `{model}` for model selection | The harness resolves `--model` / `models.skill` into this placeholder; it's the user's chosen model for this eval | Command uses its own default; eval results don't reflect the model the user asked for |
| Use `{subagent_model}` for subagents | Resolved from `models.subagent`; controls delegation model in multi-agent setups | Subagents use wrong model; cost/quality mismatch vs what user configured |
| Honor `{max_budget_usd}` | The harness passes this as a hint but **cannot enforce it** for opaque CLI runners | Command may overspend; no server-side guardrail like Claude Code's `--max-budget-usd` |
| Use `{effort}` for reasoning effort | Resolved from `runner.effort` / `--effort`; controls quality-vs-speed tradeoff | Command ignores effort setting; eval doesn't test the intended effort level |
| Use `{system_prompt}` if applicable | Resolved from `runner.system_prompt`; provides behavioral context to the agent | Agent runs without the system prompt the eval was designed to test |
| Write `{output_dir}/metrics.json` | Only way the harness gets token/cost data from an opaque CLI runner | `token_usage`, `cost_usd`, `num_turns` all report as `None`; cost tables empty in reports; no budget tracking |
| Write output to stdout | `execute.py` captures stdout to `stdout.log`; judges can access it via `outputs["stdout"]` | Judges that inspect stdout (e.g. checking for specific output text) see empty string |
| Write errors to stderr | Captured to `stderr.log`; shown in HTML report for failed cases | Failures lack diagnostic context in reports |
| Run in `{workspace}` as cwd | The harness sets `cwd` to workspace, but the command can change it; input.yaml and symlinked resources are there | Command can't find input files or project resources |

## metrics.json format

Write to `{output_dir}/metrics.json`. All fields are optional:

```json
{
  "token_usage": {"input": 1500, "output": 800},
  "cost_usd": 0.03,
  "num_turns": 4,
  "model": "claude-sonnet-4-20250514",
  "models_used": ["claude-sonnet-4-20250514", "claude-haiku-4-5-20251001"],
  "per_model_usage": {
    "claude-sonnet-4-20250514": {"input": 1200, "output": 600, "cost_usd": 0.025},
    "claude-haiku-4-5-20251001": {"input": 300, "output": 200, "cost_usd": 0.005}
  },
  "per_model_turns": {
    "claude-sonnet-4-20250514": 3,
    "claude-haiku-4-5-20251001": 1
  }
}
```

| Field | Type | Used by |
|---|---|---|
| `token_usage` | `{input: int, output: int}` | Report token breakdown; MLflow metrics; cost-per-turn calculation |
| `cost_usd` | `float` | Report cost column; MLflow run metric; regression cost comparison |
| `num_turns` | `int` | Report turn count; cost-per-turn ratio |
| `model` | `str` | Report model column; MLflow run parameter; `resolved_model` in RunResult |
| `models_used` | `list[str]` | Subagent model detection in reports |
| `per_model_usage` | `{model: {input, output, cost_usd}}` | Per-model cost breakdown in reports and MLflow |
| `per_model_turns` | `{model: int}` | Per-model turn breakdown in reports |

## What the harness provides in the workspace

Before the command runs, the workspace contains:

| Path | Description | Relevant to opaque CLI runner? |
|---|---|---|
| `input.yaml` | Case input data (fields available as `{field}` placeholders) | Yes — primary input |
| `output/` | Pre-created output directory (`{output_dir}`) | Yes — write artifacts here |
| Output dirs from `outputs[*].path` | Pre-created directories matching eval.yaml | Yes — alternative artifact locations |
| `.claude/settings.json` | Permissions, hooks, env vars | No — Claude Code specific |
| `.claude/hooks/tools.py` | Tool interception script | No — Claude Code specific |
| `subagents/` | Subagent transcript capture dir | No — Claude Code specific |
| Symlinks to `scripts/`, `skills/`, `CLAUDE.md`, `.context/`, plus any `execution.symlinks` entries | Project resources | Maybe — depends on the external command |

## What the harness reads after execution

| Source | Collected by | Consumed by |
|---|---|---|
| Process stdout | `execute.py` → `stdout.log` | Judges (via `outputs["stdout"]`); tool call extraction; MLflow trace builder |
| Process stderr | `execute.py` → `stderr.log` | Judges (via `outputs["stderr"]`); HTML report error display |
| Process exit code | `execute.py` → `run_result.json` | Report pass/fail; worst-exit aggregation; MLflow metric |
| Files in `outputs[*].path` dirs | `collect.py` → `cases/{id}/{path}/` | Judges (via `outputs["files"]`); convenience keys like `outputs["{dirname}_content"]` |
| Files modified in-place in workspace | `collect.py` via `git diff` | Judges (via `outputs["modified_files"]`) |
| `{output_dir}/metrics.json` | `cli_runner.py` → RunResult fields | Report metrics; MLflow logging; regression comparison |

## What judges receive

Judges get an `outputs` dict with these keys (all populated from the above sources):

| Key | Type | Source |
|---|---|---|
| `files` | `{relative_path: content_str}` | Collected artifacts from output dirs |
| `modified_files` | `{relative_path: content_str}` | In-place edits detected by git diff |
| `{dirname}_content` | `str` | First file's content from each output dir (convenience) |
| `{dirname}_file` | `str` | Absolute path to that file |
| `stdout` | `str` | stdout.log (if `traces.stdout` enabled) |
| `stderr` | `str` | stderr.log (if `traces.stderr` enabled) |
| `exit_code` | `int` | From run_result.json (if `traces.metrics` enabled) |
| `duration_s` | `float` | From run_result.json |
| `token_usage` | `dict` | From run_result.json (originally from metrics.json) |
| `cost_usd` | `float` | From run_result.json |
| `num_turns` | `int` | From run_result.json |
| `annotations` | `dict` | From dataset `annotations.yaml` (not runner-produced) |
| `case_dir` | `str` | Absolute path to collected case output |
| `input_path` | `str` | Path to input.yaml in case output dir |

## Environment

| Behavior | Opaque CLI runner | Claude Code runner |
|---|---|---|
| Inherits caller's env | Full `os.environ` (see security note below) | Filtered to safe allowlist |
| `execution.env` vars | Injected via `_build_env()` with `$VAR` resolution | Injected into `.claude/settings.json` env block |
| `runner.env` | No effect (full env already inherited) | Injects explicit key/value pairs on top of safe allowlist (`$VAR` resolved from caller) |

**Security note on environment inheritance:** The opaque CLI runner inherits the
full caller environment because commands are provided by the eval author (not
untrusted input). The Claude Code runner uses a strict allowlist because it
executes arbitrary agent-generated tool calls. Use `runner.env` to forward
additional env vars (e.g., `ANTHROPIC_AUTH_TOKEN`) beyond the built-in defaults.

## Features that don't work with the opaque CLI runner

| Feature | Why |
|---|---|
| Budget enforcement | Harness can't throttle an opaque process; `{max_budget_usd}` is advisory |
| Tool interception / AskUserQuestion answering | Requires Claude Code PreToolUse hooks |
| Stream-json trace building | Requires Claude Code `--output-format stream-json` |
| Subagent transcript capture | Requires Claude Code SubagentStop hook |
| Permission denial detection | Requires Claude Code stream-json events |
| Real-time progress logging | Requires stream-json event parsing |
