# Runners (agent runtimes)

A **runner** is the *agent runtime* that actually executes a case — the CLI, API, or
process that turns a skill invocation or prompt into work. It is selected in
`eval.yaml` with `runner.type`, and every runner returns the same normalized
`RunResult` so the rest of the harness (collection, scoring, reporting, MLflow) is
runtime-agnostic.

!!! important "Runner ≠ backend"
    A **runner** is *what agent runtime* runs a case (`claude-code`, `cursor`,
    `codex`, `fullsend`, `cli`, `responses-api`) — chosen in `eval.yaml` via `runner.type`. A **backend** is
    *where* the eval runs (Local, [Harbor](../guides/harbor.md),
    [EvalHub](../guides/evalhub.md)) — always chosen by a **CLI flag** (`--runner`),
    never in the config. The same `eval.yaml` runs unchanged across all three
    backends. See [Execution backends](backends.md) for that axis. A third axis,
    the **model provider** (which vendor serves the model ids in `models:`), is
    covered in [Model providers](providers.md).

!!! note "Runners also back `agent` judges"
    The same abstraction runs judges, not just cases. An
    [`agent`](../reference/config/judges.md) judge carries its own per-judge `runner:`
    block (parsed like the top-level `runner:`, default `claude-code`) and runs the judge
    itself as a tool-using agent in an isolated staged workspace under its own tool
    policy — independent of the runner used for the skill under test. See
    [judges](../reference/config/judges.md).

```mermaid
flowchart TD
    C["eval.yaml → runner.type"] --> R{RUNNERS registry}
    R -->|claude-code| CC["ClaudeCodeRunner<br/>claude --print"]
    R -->|cursor| CU["CursorAgentRunner<br/>cursor-agent --print"]
    R -->|codex| CX["CodexRunner<br/>codex exec"]
    R -->|cli| CLI["CliRunner<br/>arbitrary command"]
    R -->|fullsend| FS["FullsendRunner<br/>fullsend run"]
    R -->|responses-api| RA["ResponsesAPIRunner<br/>OpenAI Responses API"]
    CC --> RR["RunResult"]
    CU --> RR
    CX --> RR
    CLI --> RR
    FS --> RR
    RA --> RR
    RR --> S["collect → judges → report → MLflow"]
```

## The registry

`runner.type` is a discriminator resolved against a registry in
[`agent_eval/agent/__init__.py`](https://github.com/opendatahub-io/agent-eval-harness/blob/main/agent_eval/agent/__init__.py).

| `runner.type` | Class | Runtime | Notes |
| --- | --- | --- | --- |
| `claude-code` | `ClaudeCodeRunner` | Claude Code CLI (`claude --print`) | **Default.** Full-fidelity: stream-json traces, budget cap, tool interception, subagent capture, permission-denial detection |
| `cursor` | `CursorAgentRunner` | Cursor Agent CLI (`cursor-agent --print`) | Uses the local Cursor login/account, embeds `SKILL.md` prompts, supports isolated and repo-mode execution with project-local permission rules; rejects `inputs.tools`; unsupported native settings warn explicitly; local backend only |
| `codex` | `CodexRunner` | Codex CLI (`codex exec --json`) | Native Codex execution with copied skill staging, sandbox-mode mapping, and JSONL usage parsing |
| `cli` | `CliRunner` | Any command you provide | Opaque: harness only sees exit code, stdout/stderr, and an optional `metrics.json` |
| `fullsend` | `FullsendRunner` | `fullsend run <agent>` (OpenShell sandbox) | Runs the skill through the project's own `.fullsend/` agent, the production path; maps fullsend's run directory (`metrics.json`, stream-json, subagent transcripts) back onto `RunResult`; skill mode only; local backend only |
| `responses-api` | `ResponsesAPIRunner` | OpenAI Responses API (Shell tool + Skills API) | Apples-to-apples cross-runtime comparison; needs `pip install agent-eval-harness[openai]` |

The default is `claude-code`, so `runner:` can be omitted entirely.

## The contract: `EvalRunner` and `RunResult`

Every runner subclasses the `EvalRunner` ABC
([`agent_eval/agent/base.py`](https://github.com/opendatahub-io/agent-eval-harness/blob/main/agent_eval/agent/base.py))
and implements three members:

| Member | Purpose |
| --- | --- |
| `from_config(config, *, log_prefix=None, **overrides)` | Classmethod factory; each subclass pulls the config fields it needs. CLI overrides (resolved models, effort, permissions) take precedence. |
| `name` | Short identifier (`"claude-code"`, `"cursor"`, `"codex"`, `"cli"`, `"fullsend"`, `"responses-api"`). |
| `execute(target, args, workspace, model, ...)` | Run one case in a pre-staged workspace and return a `RunResult`. |

`execute()` takes a uniform signature. The two most important arguments encode
[skill vs prompt mode](../guides/skill-vs-prompt.md):

- **`target`** — the skill name (e.g. `"rfe.review"`) in case/batch mode, or `None`
  in prompt mode. Each runner renders its native skill-invocation syntax when `target`
  is set; all pass `args` verbatim when it is `None`.
- **`args`** — resolved skill arguments, or the raw prompt text in prompt mode.

Other arguments: `workspace` (staged case dir), `model`, `settings_path`,
`system_prompt` (each runner maps this to its runtime — `--append-system-prompt` for
Claude Code, a `developer` message for the Responses API), `max_budget_usd`,
`timeout_s`, and `extra_env` (merged *after* `execution.env`, so hook-produced env
overrides static config).

!!! note "`run_skill()` is deprecated"
    The ABC still exposes `run_skill(skill_name=...)`, which forwards to
    `execute(target=skill_name, ...)` with a `DeprecationWarning`. Call `execute()`.

Whatever the runtime, results are normalized into one `RunResult` dataclass:

| Field | Type | Meaning |
| --- | --- | --- |
| `exit_code` | `int` | `0` = success; `-1` = timeout or harness-level failure. Runners may convert a process exit 0 into `1` when the run demonstrably failed — e.g. the claude-code runner fails a case whose background tasks the CLI killed at the bg-wait ceiling, or whose slash command was unknown |
| `stdout` / `stderr` | `str` | Captured output |
| `duration_s` | `float` | Wall-clock seconds |
| `token_usage` | `dict` | `{"input": N, "output": N}` |
| `cost_usd` | `float` | Billed API spend — the conversation `total_cost_usd`, raised to the per-model usage sum when that exceeds it by more than $0.01 (background agents burning tokens the conversation total never saw) |
| `num_turns` | `int` | Assistant turns (incl. subagents where captured) |
| `resolved_model` | `str` | Full model ID observed at runtime |
| `models_used` | `list` | All distinct models observed |
| `per_model_usage` / `per_model_turns` | `dict` | Per-model token/cost and turn breakdowns |
| `permission_denials` | `list` | `[{tool_name, tool_use_id, tool_input}]` |
| `raw_output` | `dict` | Runner-specific parsed output |

Fields a runtime can't provide stay `None` — reports and MLflow simply omit them.

## `claude-code` (default)

`ClaudeCodeRunner`
([`claude_code.py`](https://github.com/opendatahub-io/agent-eval-harness/blob/main/agent_eval/agent/claude_code.py))
shells out to the Claude Code CLI in non-interactive mode. The invocation is roughly:

```bash
claude --print \
  --model "$MODEL" \
  --output-format stream-json \   # 'json' when no live logging
  --max-budget-usd "$BUDGET" \
  --verbose \                      # only with live logging
  --effort high \                  # only if runner.effort set
  --plugin-dir <dir> \             # per runner.plugin_dirs entry (see Plugin staging)
  --append-system-prompt "..." \   # if system_prompt set
  --settings <path>                # permissions/hooks/env
# the prompt (/skill args, or raw prompt) is piped on stdin
```

The runner reads the stream line by line, injecting timestamps and printing live
progress (skill invocations, tool calls, permission denials, final cost). A watchdog
thread kills the process at the deadline. From the stream it extracts usage, cost,
turn counts, resolved model(s), and the structured `permission_denials` array from
the `result` event, which is persisted per case into `run_result.json`
(see [runs directory](../reference/runs-directory.md)). Reported `cost_usd` is the *billed* cost: the conversation's
`total_cost_usd`, raised to the per-model `modelUsage` sum when that exceeds it
by more than $0.01 (background agents killed after the final turn — or still running at an evaluator
timeout — burn billed tokens the conversation total never sees), so it can exceed
the cost the CLI printed.

**Highlights specific to this runner:**

- **Provider** — an Anthropic model through whatever Claude Code itself is
  configured for (direct API key, an `ANTHROPIC_BASE_URL` gateway, or Vertex —
  all forwarded by the allowlist below; the harness never routes it), or
  `openrouter:/<author>/<slug>`, which the harness serves itself through a
  provider plan. Agent roles accept nothing else; the caveats (bare ids on agent
  roles, `openrouter:/` on this runner only) are in
  [Model providers → Limitations](providers.md#limitations). See
  [Model providers](providers.md#three-provider-families) and
  [Running on OpenRouter](../guides/openrouter.md#direct-transport-no-proxy).
- **Plugin staging** — every `runner.plugin_dirs` entry outside the workspace is
  copied into `<workspace>/.staged-plugins/` and `--plugin-dir` receives the copy,
  so the real plugin path never enters session context; in-workspace entries
  pass through unchanged. Claude Code can skip staging in `workspace_mode: repo`;
  Codex rejects that mode. Cursor uses a temporary project-local `.cursor/cli.json`
  permission file while the agent runs and restores the repository's original file
  afterward.
- **Plugin hermeticity** — isolated workspaces disable all of the operator's
  user-installed plugins by default (a synthesized `enabledPlugins` denylist in
  the workspace settings; explicit `runner.settings.enabledPlugins` entries win,
  and the harness-interpreted `"*"` pseudo-entry forces or opts out of the
  policy). Plugins under test are immune — `--plugin-dir` registers them outside
  the installed registry. See [runner settings](../reference/config/runner.md).
- **Background-task truth** — the CLI waits up to `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`
  (default 600 s) for background tasks that outlive the final turn, then kills
  them but still exits 0. The runner detects the kill and fails the case:
  `exit_code` becomes `1` and an ERROR note is appended to stderr — artifacts
  from the killed agent may be half-written. For long-running pipeline skills,
  raise the ceiling (`0` = wait indefinitely) via `runner.env:` or an exported
  variable (the key is on the env allowlist).
- **Budget** is enforced server-side via `--max-budget-usd`.
- **Permissions** — simple string patterns become `--allowed-tools` /
  `--disallowed-tools`; path-based rules are compiled into a temporary
  `.eval-permissions.json` merged with the workspace settings. See
  [permissions](../reference/config/permissions.md). Under an OpenRouter plan the
  same overlay (then named `.eval-overlay.json`, mode 0600) also carries the
  agent's env block — see [Running on OpenRouter](../guides/openrouter.md#direct-transport-no-proxy)
  and [Model providers](providers.md#credentials-by-backend).
- **Subagents** — session persistence stays on so `SubagentStop` hooks can copy
  subagent transcripts; the session dir under `~/.claude/projects/` is cleaned up
  post-run.
- Full [tool interception](tool-interception.md), [lifecycle hooks](lifecycle-hooks.md),
  and [stream-json tracing](tracing.md) all require this runner.

### The environment allowlist

Unlike the opaque CLI runner, the Claude Code runner does **not** inherit your full
environment — it executes agent-generated tool calls, so it forwards only an
**exact-name** allowlist (`_SAFE_ENV_KEYS`; no prefix matching):

| Group | Forwarded when set |
| --- | --- |
| OS baseline | `PATH`, `HOME`, `USER`, `SHELL`, `LANG`, `LC_ALL`, `TERM` |
| Anthropic credentials & endpoint | `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` |
| Model aliases | `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL` |
| Vertex | `CLAUDE_CODE_USE_VERTEX`, `ANTHROPIC_VERTEX_PROJECT_ID`, `CLOUD_ML_REGION`, `CLAUDE_CODE_SKIP_VERTEX_AUTH`, `ANTHROPIC_VERTEX_BASE_URL`, `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_CLOUD_PROJECT`, `CLOUDSDK_CONFIG`, `CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE` |
| Claude Code tuning | `CLAUDE_CODE_MAX_RETRIES`, `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` |
| Harness | `MLFLOW_TRACKING_URI`, `MLFLOW_EXPERIMENT_NAME`, `AGENT_EVAL_RUNS_DIR` |

**Not** on the list: `CLAUDE_CODE_USE_BEDROCK`, `AWS_REGION` and every other `AWS_*`
credential — Bedrock on this runner needs explicit `runner.env` entries (only the
Harbor Podman backend forwards them from the host); `OPENROUTER_*` (under a plan the
agent sees the key only as `ANTHROPIC_AUTH_TOKEN` in the overlay); and `OPENAI_*`.
Under an OpenRouter plan the Google credential locations and the two OpenShell
Vertex switches (`CLAUDE_CODE_SKIP_VERTEX_AUTH`, `ANTHROPIC_VERTEX_BASE_URL`) are
dropped from the ambient copy too, and every plan-managed key is removed after all
merges, so nothing can put the host's `ANTHROPIC_API_KEY` back. `runner.env` entries
win over ambient values on collision. The per-backend picture is on
[Model providers](providers.md#credentials-by-backend).

!!! warning "Forwarding extra env vars"
    To pass anything *not* on the allowlist (e.g. a `JIRA_TOKEN` the skill needs), add
    it under `runner.env` or `execution.env`. Values starting with `$` are resolved
    from the caller's environment (`JIRA_TOKEN: $JIRA_TOKEN`). See
    [environment variables](../reference/environment-variables.md).

## `cursor`

`CursorAgentRunner`
([`cursor_agent.py`](https://github.com/opendatahub-io/agent-eval-harness/blob/main/agent_eval/agent/cursor_agent.py))
shells out to `cursor-agent --print` using the machine's local Cursor
login/account. It embeds the resolved `SKILL.md` in the prompt and copies plugin
skills into the case workspace; it supports both the isolated workspace and
`workspace_mode: repo`.

```yaml title="eval.yaml"
runner:
  type: cursor
  effort: high          # model-specific — appended to the model id
  env:
    CURSOR_API_KEY: $CURSOR_API_KEY
```

- **Provider** — Cursor's own account. The only provider variables on its
  exact-name allowlist are `CURSOR_API_KEY`, `CURSOR_API_ENDPOINT` and
  `CURSOR_AGENT_BIN`; the model id is whatever `cursor-agent --model` accepts
  (the harness appends `[effort=…]`), so `provider:/` ids do not apply, and an
  `openrouter:/` skill model is rejected at config load for this runner
  ("cursor has no base-URL knob"). Anthropic and OpenAI credentials are not
  forwarded — add them under `runner.env` if the skill itself needs them.
- **Local backend only.** The Harbor and EvalHub base image ships no
  `cursor-agent` CLI, so Harbor has no cursor agent and EvalHub rejects the
  config at load. Use `claude-code` or `codex` there.
- **`effort` is model-specific** — appended to the model id, not the
  `low|medium|high|xhigh|max` scale that claude-code/codex use.
- **`inputs.tools` is rejected** at config load (like codex). `workspace_mode:
  repo` *is* supported (unlike codex).
- **Permissions map to a subset.** Allow/deny rules are written to a
  project-local `.cursor/cli.json`; rules Cursor cannot express (some tools,
  path-scoped allows) are dropped with a warning, and an allow list that maps to
  nothing is fail-closed (deny-all, with a warning). An existing
  `.cursor/cli.json` stays in effect, so it is **not** a security boundary in
  `workspace_mode: repo`.
- **`max_budget_usd` is not enforced** — the Cursor CLI has no budget flag; bound
  spend with `execution.timeout`.
- **Env & settings.** Provider credentials (`CURSOR_API_KEY`,
  `CURSOR_API_ENDPOINT`) go under `runner.env`; `runner.settings` accepts only
  `binary` (other native flags warn and are ignored).

## `codex`

`CodexRunner`
([`codex.py`](https://github.com/opendatahub-io/agent-eval-harness/blob/main/agent_eval/agent/codex.py))
shells out to `codex exec --json`. The prompt is sent on stdin, and each configured
plugin's skills are copied into the case workspace under `.agents/skills` for the
duration of the run. Manifest-declared custom skill roots are supported. This works
directly on the host; Harbor is optional.

```yaml title="eval.yaml"
runner:
  type: codex
  effort: xhigh       # minimal | low | medium | high | xhigh
  plugin_dirs:
    - ./plugins/payload-analysis
```

Codex permission modes preserve the intent of the Claude Code configuration using
the controls available in `codex exec`: `plan` uses `read-only`, explicit
`bypassPermissions` uses the dangerous bypass flag, and all other modes use
`workspace-write`. Fine-grained Claude Code tool allow/deny rules do not have an exact
Codex equivalent, so the runner warns rather than silently claiming they are enforced.
Likewise, Codex CLI does not expose the harness's dollar budget cap; a non-default
`max_budget_usd` produces a warning. Codex JSONL reports tokens but not cost, so the
runner estimates `cost_usd` from per-turn usage via LiteLLM's pricing table — the
same mechanism Harbor's Codex agent uses, so local and Harbor runs price the same
tokens the same way. When `litellm` is not installed (the `harbor` extra includes
it) or the model has no pricing entry, `cost_usd` is `null` rather than a guess.

Codex does not support `inputs.tools` interception. It also rejects
`workspace_mode: repo`, where the harness cannot enforce repository answer-key
protections. Use the default isolated workspace for Codex evals.

**Provider.** OpenAI, through Codex's own configuration. Like the Claude Code runner,
Codex starts from an exact-name allowlist rather than the full host environment; the
provider names on it are `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_ORG_ID`,
`OPENAI_ORGANIZATION`, `OPENAI_PROJECT`, `OPENAI_MODEL`, `CODEX_HOME` and
`CODEX_API_KEY` (plus the OS baseline, the MLflow variables and `AGENT_EVAL_RUNS_DIR`).
Anthropic and GCP credentials are deliberately not forwarded — LLM judges run in the
harness process, not in the agent's. The model id is passed verbatim to
`codex exec --model`, so `provider:/` ids do not apply and an `openrouter:/` skill
model is rejected at config load. Add anything else under `runner.env` or
`execution.env`; `$VAR` values are resolved from the caller.

## `cli` (opaque CLI runner)

`CliRunner`
([`cli_runner.py`](https://github.com/opendatahub-io/agent-eval-harness/blob/main/agent_eval/agent/cli_runner.py))
delegates to an arbitrary `runner.command` — any string (shell-parsed) or list of
args. This is how you evaluate opaque runtimes (OpenCode or a bespoke
harness). The full contract lives in
[`docs/opaque-cli-runner-contract.md`](https://github.com/opendatahub-io/agent-eval-harness/blob/main/docs/opaque-cli-runner-contract.md);
see also the [OpenCode cookbook](../cookbook/cross-runner-opencode.md).

```yaml title="eval.yaml"
runner:
  type: cli
  command: "my-runner run {agent} --model {model} --out {output_dir}"
```

**Provider.** Whatever the command does. `{model}` is substituted raw (from `--model`
or `models.skill`) and is opaque to the harness — no `provider:/` routing happens, the
command inherits your full `os.environ` plus `execution.env` (`runner.env` is not
applied by this runner), and credentials are whatever you exported. An `openrouter:/`
skill model is rejected at config load for this runner.

### Placeholders

The command template is resolved before execution (string values are `shlex.quote`d):

| Placeholder | Value |
| --- | --- |
| `{agent}` | Skill name (case/batch) or empty (prompt mode) |
| `{workspace}` | Absolute case-workspace path |
| `{output_dir}` | Absolute `{workspace}/output` (pre-created) |
| `{model}` | Model id (`--model` / `models.skill`) |
| `{subagent_model}` | `models.subagent` (empty if unset) |
| `{timeout}` | Timeout in seconds |
| `{max_budget_usd}` | Budget cap (**advisory — not enforced**) |
| `{effort}` | `runner.effort` (empty if unset) |
| `{system_prompt}` | `runner.system_prompt` (empty if unset) |
| `{args}` | Resolved skill arguments |
| `{field}` | Any field from the case's `input.yaml` (never overrides a builtin) |

### What the command must do

| Requirement | Why |
| --- | --- |
| Exit `0` on success, non-zero on failure | Exit code drives pass/fail in scoring |
| Write artifacts to `{output_dir}` (or declared `outputs[*].path`) | `collect.py` scans these for judges |
| Finish before `{timeout}` seconds | Harness SIGKILLs the process group at the deadline (records exit `-1`) |

### `metrics.json` — the only way to report cost

Because the process is opaque, token/cost data must be written by the command to
`{output_dir}/metrics.json`. Without it, `token_usage`, `cost_usd`, and `num_turns`
report as `None` and cost tables in the report are empty. All fields are optional:

```json title="{output_dir}/metrics.json"
{
  "token_usage": {"input": 1500, "output": 800},
  "cost_usd": 0.03,
  "num_turns": 4,
  "model": "claude-sonnet-4-20250514",
  "models_used": ["claude-sonnet-4-20250514", "claude-haiku-4-5-20251001"],
  "per_model_usage": {
    "claude-sonnet-4-20250514": {"input": 1200, "output": 600, "cost_usd": 0.025}
  },
  "per_model_turns": {"claude-sonnet-4-20250514": 3}
}
```

!!! warning "Claude-Code-only features don't apply"
    Budget enforcement, tool interception / AskUserQuestion answering, stream-json
    tracing, subagent transcript capture, permission-denial detection, and real-time
    progress logging all require the `claude-code` runner. The opaque runner inherits
    your **full** `os.environ` (commands come from the eval author, not untrusted
    input); `execution.env` adds keys on top with `$VAR` resolution, and `runner.env`
    is a no-op here (see [runner](../reference/config/runner.md)).

## `fullsend`

`FullsendRunner`
([`fullsend.py`](https://github.com/opendatahub-io/agent-eval-harness/blob/main/agent_eval/agent/fullsend.py))
executes the skill under test with `fullsend run`, against the project's own
`.fullsend/` agent package, with the eval workspace as the target repository. The
skill then runs the way production runs it: in an OpenShell sandbox with the
Tirith and SSRF hooks, under the agent definition's system prompt, on the harness's
pinned Claude Code build, with `env.sandbox` as the only environment contract.

```yaml title="eval.yaml"
runner:
  type: fullsend
  agent: rfe-creator-eval   # required: an agent registered in <fullsend_dir>/config.yaml
  fullsend_dir: .fullsend   # default; resolved against the project root
  binary: fullsend          # default: fullsend on PATH; a relative path with a
                            # separator resolves against the project root
  extra_args: []            # appended verbatim to `fullsend run`
  term_grace_s: 30          # default; seconds from SIGTERM to SIGKILL on timeout
  env: {}                   # extra variables for the fullsend process
```

**Prerequisites.** fullsend ≥ 0.48, a running OpenShell gateway with podman, and the
agent's own credentials on the host (for rfe-creator: a Vertex service-account key).

Per case (or once per batch) the runner:

1. **Materialises the workspace.** fullsend uploads the target repository as a tar
   that keeps symlinks, and an absolute link target does not exist inside the
   sandbox. Every symlink at the workspace top level and under `.claude/` is
   replaced by a real copy (nested links followed relative to the link's own
   directory, dangling links dropped, `.git` excluded). A link whose real target
   lies outside the project root and the configured `runner.plugin_dirs` is
   dropped with a warning, so host files never reach the sandbox; so is a link,
   top-level or nested, whose target is a `.claude/settings.json` or `.claude/settings.local.json`
   file under any root, which the rewrite of step 2 never saw (the
   workspace's own, already rewritten, file is the one exception) (a regular file of
   that name reached without a link is part of the tree you chose to stage and is
   copied). The project root
   is never modified. Each copy is built under a temporary name and swapped in last,
   and a failure is reported as a failed case (exit `-1`).
2. **Rewrites the workspace `.claude/settings.json`** for this runner. The harness
   writes `env`, its own hook entries (the `SubagentStop` transcript capture and
   the `PreToolUse` interception hooks) and `permissions.additionalDirectories` for
   the other runners; they name host paths and values that mean nothing in the
   sandbox, and fullsend uploads the file with the repository, so the runner removes
   all three for the duration of the run and puts the original file back
   afterwards, so a later non-fullsend step in the same workspace keeps its hooks,
   env and directory grants. The project's own hooks — the ones `execution.project_hooks`
   carries over from `<project>/.claude/settings.json`, such as a `SessionStart`
   recovery hook or a `Stop` guard — are kept: a production fullsend run loads
   exactly those from the checkout, so the eval keeps the same recovery machinery.
   The two are told apart by their command: a harness hook runs a script by its
   host-absolute path under the workspace's `hooks/` directory, a project hook
   runs a relative command (`python3 scripts/...`) that resolves inside the
   uploaded repository, so a hook entry is removed when any of its commands
   names an absolute path and kept otherwise; an event whose entries were all
   removed is dropped. The rewrite happens before the copies of step 1 are
   made, so a workspace link that resolves to the file is copied already stripped;
   a `.claude` directory or `settings.json` that is itself a symlink cannot be
   stripped in place and fails the case. Because the interception hooks never
   reach the sandbox, `inputs.tools` interception is not available with this
   runner and is rejected at config load.
3. **Sets the environment** of the fullsend process, on top of the caller's
   environment (no env file is written, so no dotenv quoting applies). It holds
   `FULLSEND_TASK="/<skill> <args>"`, `AGENT_EVAL_SUBAGENT_MODEL` (only when a
   subagent model is set, never empty), plus every `execution.env`, `runner.env`
   and hook-output entry (`$VAR` and `${VAR}` values resolve from the caller). When
   `models.subagent` is set and nothing under `<fullsend_dir>` references
   `AGENT_EVAL_SUBAGENT_MODEL`, the runner warns that the value will not take effect.
4. **Invokes** `<binary> run <agent> --fullsend-dir <abs> --target-repo <workspace>
   [--model <model>] [--effort <effort>] --output-dir <workspace>/.fullsend-runs
   <extra_args>` from the workspace. `--effort` is passed only when `runner.effort`
   / `--effort` is set. The output directory is a top-level child of the workspace,
   so fullsend leaves it out of the target-repo upload and a run never sees an
   earlier run's files; the run directory is the one fullsend creates during this
   invocation, so a run that fails before fullsend writes anything maps nothing.
5. **Maps the run directory** onto `RunResult`: `cost_usd`, `num_turns`,
   `resolved_model` and `token_usage` (`input`, `output`, `cache_read`,
   `cache_create`) from `metrics.json`, with `tool_calls`, `iterations` and the count
   of `security/` files in `raw_output`. `stdout` is the concatenation of every
   iteration's `output.jsonl` (Claude Code stream-json), so events, MLflow traces and
   reasoning judges work unchanged. fullsend's own console output goes to `stderr`.
   Per-model usage, turns and message ids come from those streams plus every
   iteration's subagent transcripts (`transcripts/*-agent-*.jsonl`), which are copied
   into `<workspace>/subagents/` (a name already taken by an earlier iteration gets an
   `itN-` prefix). Hook block events in `run-telemetry.jsonl` become
   `permission_denials` (best effort). Per-model turns and message ids cover only the
   current invocation's transcripts. The exit code is fullsend's.
6. **Collects artifacts from the workspace.** Repo-relative `outputs[*].path` reach
   the workspace the way they do in production: the project's post-script, which
   `fullsend run` executes after the agent, copies the agent's outputs into
   `--target-repo` (the eval workspace), where `collect.py` reads them. The runner
   never reads `iteration-N/output/` (fullsend's own export, including
   `agent-result.json`) as the repository. A run whose post-script did not execute
   produces no artifacts, so `--no-post-script` in `runner.extra_args` is rejected as
   a failed case.

!!! warning "Timeouts, budget and env references"
    - Keep the fullsend harness's `timeout_minutes` **below** `execution.timeout`.
      When the eval's timeout expires, the runner sends SIGTERM to the process group
      (fullsend then tears its sandbox down), waits `runner.term_grace_s` (default
      30 s, above a podman sandbox teardown), signals SIGKILL to the group only while
      the leader is still unreaped, and records exit `-1` ("Timed out"). Raise
      `term_grace_s` if your sandbox needs longer to stop. The run directory is
      still mapped after a timeout: usage, stream stdout, subagent transcripts and
      artifacts already copied into the workspace are kept.
    - `max_budget_usd` is a hint only. The fullsend harness enforces its own budget.
    - fullsend aborts before the pre-script when a `${VAR}` the harness file
      references is unset on the host. An agent's eval harness may reference only
      the variables listed in step 3 or variables from the caller's environment.
      (Variables travel through the process environment, so none of the values
      are quoted or escaped.)
      fullsend 0.48 has an `--effort` flag, which the runner uses; it has no
      subagent-model flag, so that value travels as
      `${AGENT_EVAL_SUBAGENT_MODEL}`, which the agent definition must reference.

Prompt mode (`execution.prompt`) is unsupported: it is rejected at config load, as
is `type: fullsend` on an agent judge's `agent.runner`. `runner.system_prompt`
and the harness system prompt are ignored, because the agent definition replaces
the system prompt. Tool interception (`inputs.tools`) does not reach the sandbox
(the project's own hooks do). There is no Harbor or EvalHub equivalent.

## `responses-api`

`ResponsesAPIRunner`
([`responses_api.py`](https://github.com/opendatahub-io/agent-eval-harness/blob/main/agent_eval/agent/responses_api.py))
runs the *same* harness skills via the OpenAI Responses API — Shell tool plus the
Skills API — in hosted containers, for apples-to-apples comparison against
`claude-code` (same skill, same cases, different runtime). Per case it:

1. uploads the skill directory → `skill_id` (cached process-wide across cases),
2. creates an isolated container, uploads the workspace files,
3. executes via `POST /v1/responses` with the `shell` tool bound to the container,
4. downloads new/modified files back into the workspace,
5. deletes the container.

Configure it under `runner.settings`:

```yaml title="eval.yaml"
runner:
  type: responses-api
  settings:
    base_url: "..."          # or OPENAI_BASE_URL
    api_key: "..."           # or OPENAI_API_KEY
    default_model: "..."     # or OPENAI_MODEL, or --model
    memory_limit_mb: 512     # snapped to nearest tier (1g/4g/16g/64g)
    network_policy: { ... }  # optional container network policy
```

!!! note "Not wired"
    `settings_path` and `max_budget_usd` are accepted for ABC compatibility but the
    Responses API exposes no equivalent knobs, so they have no effect here.

**Provider.** An OpenAI-compatible endpoint: `runner.settings.base_url` / `api_key` /
`default_model`, each falling back to `OPENAI_BASE_URL` / `OPENAI_API_KEY` /
`OPENAI_MODEL`; `--model` (or `models.skill`) overrides `default_model` per run.
`provider:/` ids do not apply, and an `openrouter:/` skill model is rejected at config
load for this runner.

!!! warning "`settings.api_key` is a secret in config"
    It is the one credential the harness accepts from `eval.yaml` rather than the
    environment. Prefer `OPENAI_API_KEY` so the config stays shareable.

## Choosing a runner

=== "Test a Claude Code skill"

    Use the default. You get budget caps, tool interception, and full tracing for free.

    ```yaml
    # runner: block can be omitted entirely
    ```

=== "Evaluate a different agent runtime"

    Wrap it in a command with the opaque CLI runner and emit `metrics.json` for cost.

    ```yaml
    runner:
      type: cli
      command: "opencode run {agent} --model {model}"
    ```

=== "Evaluate a Codex skill locally"

    Use the native Codex runner. Harbor is only needed when you want container or
    cluster isolation.

    ```yaml
    runner:
      type: codex
      effort: xhigh
    ```

=== "Compare Claude vs OpenAI on the same skill"

    Run once with `claude-code`, once with `responses-api`, and diff the reports.

    ```yaml
    runner:
      type: responses-api
      settings: { default_model: gpt-5 }
    ```

## Adding a runner

Runners are pluggable — the harness only ever talks to the ABC:

1. Subclass `EvalRunner` and implement `from_config()`, `name`, and `execute()`,
   returning a normalized `RunResult`.
2. Register it in the `RUNNERS` dict in `agent_eval/agent/__init__.py` under a new
   `runner.type` key, and add the same key to the `RUNNER_TYPES` frozenset in
   `agent_eval/config.py`, which is what config load checks.
3. Populate as many `RunResult` fields as your runtime can; leave the rest `None`.

Once registered in both places, that `runner.type` is accepted at config load (a type
missing from `RUNNER_TYPES` is rejected, even if it is in `RUNNERS`) and works on the
Local backend. EvalHub dispatches through `RUNNERS`
directly, but registration does not imply EvalHub support (`fullsend`, for one, is
local-only). [Harbor](../guides/harbor.md) instead maps `runner.type` to a Harbor
agent name, so a runtime that is not in that mapping is reached there with
`--agent <harbor-agent>`, not through this registry.

## See also

<div class="grid cards" markdown>

- [**Execution backends**](backends.md) — Local vs Harbor vs EvalHub (the `--runner` flag)
- [**Model providers**](providers.md) — which model ids and credentials each runner can use
- [**runner (config reference)**](../reference/config/runner.md) — every `runner.*` key
- [**Skill vs prompt mode**](../guides/skill-vs-prompt.md) — what `target`/`args` encode
- [**Cross-runner: OpenCode**](../cookbook/cross-runner-opencode.md) — an opaque CLI runner end to end

</div>
