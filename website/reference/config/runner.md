# runner

The `runner` block selects **which agent runtime executes your skill or prompt** and
carries runtime-specific knobs. Its `type` is a discriminator; the remaining fields are
read selectively. Fields a runner doesn't understand are ignored, and runners may warn
when an unsupported setting would otherwise be easy to miss.

!!! tip "Runtime, not backend"
    `runner.type` picks the *agent* (Claude Code, Cursor, Codex, an opaque CLI, fullsend, or the
    OpenAI Responses API). The **execution backend** (Local, Harbor, EvalHub) is a separate `--runner`
    CLI flag, never a config key. See [backends](../../concepts/backends.md) and the
    [runners concept](../../concepts/runners.md) for the distinction.

## The six runner types

```mermaid
flowchart TD
    T{runner.type}
    T -->|claude-code<br/>default| CC[ClaudeCodeRunner<br/>claude --print]
    T -->|cursor| CU[CursorAgentRunner<br/>cursor-agent --print]
    T -->|codex| CX[CodexRunner<br/>codex exec]
    T -->|cli| CLI[CliRunner<br/>arbitrary command template]
    T -->|fullsend| FS[FullsendRunner<br/>fullsend run]
    T -->|responses-api| RA[ResponsesAPIRunner<br/>OpenAI Responses + Skills API]
```

| `type` | Runtime | Use it for |
| --- | --- | --- |
| `claude-code` *(default)* | Claude Code CLI in headless mode (`claude --print --output-format …`) | The primary path — full tracing, tool interception, permission enforcement, subagent capture |
| `cursor` | Cursor Agent CLI in headless mode (`cursor-agent --print --output-format …`) | Local Cursor-account execution with Cursor-managed model access and plugin loading. **Local backend only** — not available in EvalHub or Harbor. The Cursor CLI has no budget flag, so `max_budget_usd` is not enforced; bound spend with `execution.timeout` |
| `codex` | Codex CLI in non-interactive mode (`codex exec --json`) | Native Codex execution, skill staging, JSONL usage parsing, and sandbox-mode mapping |
| `cli` | Any command you provide, via a placeholder template | Wrapping OpenCode, a custom agent, or a shell script. See the [opaque CLI runner contract](https://github.com/opendatahub-io/agent-eval-harness/blob/main/docs/opaque-cli-runner-contract.md) |
| `fullsend` | `fullsend run <agent>`: the project's own `.fullsend/` agent in an OpenShell sandbox | Running the skill under test on the production path (sandbox, security hooks, agent definition, pinned Claude Code). Skill mode and **local backend only** |
| `responses-api` | OpenAI Responses API with the Shell tool + Skills API | Apples-to-apples comparison of the *same* skill on an OpenAI model |

!!! note "Which model ids a runner accepts"
    `claude-code` takes a bare Anthropic id, `anthropic:/…` or `openrouter:/<author>/<slug>`
    — it is the only runner the harness-managed OpenRouter plan supports. The others
    pass the resolved skill model to their own CLI or API and authenticate through their
    own environment (`CURSOR_API_KEY`, `OPENAI_*`). See
    [Model providers → Which role accepts which provider](../../concepts/providers.md#which-role-accepts-which-provider).

## Field reference

Not every runner reads every field. The matrix below shows where each field lands.

| Field | Type | `claude-code` | `cursor` | `codex` | `cli` | `responses-api` | `fullsend` |
| --- | --- | :---: | :---: | :---: | :---: | :---: | :---: |
| `type` | `str` | discriminator | discriminator | discriminator | discriminator | discriminator | discriminator |
| `effort` | `str` (enum) | `--effort` flag | appended to a base `--model` as `[effort=…]`; existing Cursor effort variants pass through | `model_reasoning_effort` | `{effort}` placeholder | — | `--effort` flag on `fullsend run` |
| `permission_mode` | `str` (enum) | `--permission-mode` flag (also accepts `manual`, claude-code-only) | `plan` → `--mode plan`; `bypassPermissions` → `--force` | mapped to Codex sandbox mode | — | — | — |
| `settings` | `dict` | merged into workspace `.claude/settings.json` | `binary` only; other keys warn and are ignored | `-c` config overrides for `codex exec` | — | connection settings (see below) | — |
| `plugin_dirs` | `list[str]` | one `--plugin-dir` per entry (workspace-staged copy for out-of-workspace paths) | one `--plugin-dir` per entry (workspace-staged copy for out-of-workspace paths) | skills copied into `.agents/skills` | — | — | — |
| `env` | `dict` | injected on the safe allowlist | injected into the subprocess env | injected on the safe allowlist | — (uses `execution.env`) | — | injected into the subprocess env |
| `system_prompt` | `str` | `--append-system-prompt` | prepended to the prompt | prepended to the prompt | `{system_prompt}` placeholder | `developer` message | — (the agent definition replaces it) |
| `command` | `str` \| `list[str]` | — | — | — | **required** — command template | — | — |
| `workspace_mode` | `None` \| `"repo"` | harness-level (all runners) | harness-level | rejected | harness-level | harness-level | — |
| `agent` | `str` | — | — | — | — | — | **required** — agent name in `<fullsend_dir>/config.yaml` |
| `fullsend_dir` | `str` | — | — | — | — | — | `--fullsend-dir` (default `.fullsend`) |
| `binary` | `str` | — | — | — | — | — | executable (default `fullsend` on PATH) |
| `extra_args` | `list[str]` | — | — | — | — | — | appended to `fullsend run` |
| `term_grace_s` | `float` | — | — | — | — | — | seconds from SIGTERM to SIGKILL on timeout (default 30) |

!!! warning "Unset ≠ empty behavior"
    A field ignored by the active runner is harmless — it just does nothing. But two
    fields are easy to misplace: `runner.env` has **no effect** on the `cli` runner
    (which inherits the full caller environment and reads `execution.env` for
    additions), and `runner.settings` means **completely different things** to
    each runtime (see below). Cursor recognizes only `binary` in that mapping and
    warns for other keys.

### `type`

Selects the runner implementation. One of `claude-code` (default), `cursor`,
`codex`, `cli`, `fullsend`, or `responses-api`. Any other value fails config
validation with an error naming the field (`runner.type`,
`execution.steps[N].runner.type`, or a judge's `agent.runner.type`).

### `effort`

Reasoning-effort level for the agent. The accepted values are runner-specific:

| Runner | Valid values |
| --- | --- |
| `claude-code` | `low`, `medium`, `high`, `xhigh`, `max` |
| `codex` | `minimal`, `low`, `medium`, `high`, `xhigh` |
| `cursor` | Passed as the Cursor model's `effort` parameter; accepted values depend on the selected model |
| `fullsend` | Forwarded as `--effort` to `fullsend run`; fullsend validates the accepted values |

An invalid value raises at construction time for `claude-code` and `codex`; for
`fullsend` the harness does not validate it and leaves that to `fullsend run`. Cursor
does not have a standalone `--effort` flag; it appends the value to the model ID,
for example `gpt-5.4[effort=high]`. If the model is already a Cursor catalog
variant such as `gpt-5.4-medium`, the runner passes that ID through unchanged
instead of producing an invalid compound ID such as
`gpt-5.4-medium[effort=medium]`. The selected Cursor model validates whether
the parameter is accepted. The CLI `--effort` flag overrides this field.
For the `cli` runner it is exposed as the `{effort}` placeholder (empty string if
unset); `responses-api` ignores it.

```yaml
runner:
  type: claude-code
  effort: high        # low | medium | high | xhigh | max
```

### `permission_mode`

Claude Code permission mode, passed as the `--permission-mode` CLI flag. Because
it is a CLI flag (not a settings-file key), it applies even in the untrusted,
isolated per-case workspaces where `.claude/settings.json` `permissions.allow` /
`additionalDirectories` are trust-gated and the trust dialog can't appear in
headless mode. One of:

| Value | `default` | `acceptEdits` | `plan` | `auto` | `dontAsk` | `bypassPermissions` | `manual` |
| --- | --- | --- | --- | --- | --- | --- | --- |

`manual` turns every unlisted tool call into a permission prompt. It is
**claude-code-only**: Cursor and Codex reject it. Agent judges are forced onto it
(together with `--permission-prompts none`, so a prompt nobody can answer is an
automatic denial), which is what makes a judge's `allowed_tools` authoritative —
see [judges](judges.md).

An invalid value raises at construction time for `claude-code` and `codex`; `cli` and
`responses-api` ignore the field. For a prompt-free, deny-by-default headless
run, pair `dontAsk` (allows only what's pre-approved) with a complete
[`permissions.allow`](permissions.md) list (fed to `--allowed-tools`, also
trust-independent). `bypassPermissions` skips all prompts — isolated
environments (containers/VMs) only.

Codex preserves the same intent using its available sandbox modes: `plan` maps to
`read-only`; `bypassPermissions` maps to Codex's explicit dangerous bypass; and the
remaining modes map to `workspace-write`. Codex cannot translate Claude Code's
fine-grained tool allow/deny rules exactly, so the runner emits a warning when such
rules are configured.

Cursor maps `plan` to `cursor-agent --mode plan` and
`bypassPermissions` to `cursor-agent --force`. The other common modes have no
exact Cursor equivalent and produce an explicit warning before Cursor's default
approval behavior is used. Prompt-only Cursor invocations clear `permission_mode`
so judges and synthetic generation are not launched in plan mode.

```yaml
runner:
  type: claude-code
  permission_mode: dontAsk   # default | acceptEdits | plan | auto | dontAsk | bypassPermissions | manual (claude-code only)
```

### `settings`

A `dict` whose meaning depends on the runner:

=== "claude-code"

    Merged into each case workspace's generated `.claude/settings.json` (after the
    harness defaults, so your scalars win and lists are extended). Use it to add
    Claude Code settings — model defaults, `env`, MCP servers — without forking the
    harness. The same merge implementation backs the [`extends`](extends.md)
    config overlay, where lists additionally dedupe and `judges`/`steps` merge by key.

    ```yaml
    runner:
      type: claude-code
      settings:
        env:
          MY_FLAG: "1"
        # any valid .claude/settings.json keys
    ```

    **Plugin hermeticity.** In an isolated workspace the operator's
    user-installed plugins (from `~/.claude/plugins/installed_plugins.json`)
    are disabled **by default**: the harness synthesizes
    `enabledPlugins: {<id>: false}` for every installed plugin and merges it
    before your `settings`, so an explicit entry re-enables a plugin with one
    line. `workspace_mode: repo` disables nothing — that session runs in your
    real environment. Plugins under test are immune: `--plugin-dir` plugins
    register as `<name>@inline` and never appear in the registry.

    The `enabledPlugins` pseudo-entry `"*"` steers the policy explicitly:

    ```yaml
    runner:
      settings:
        enabledPlugins:
          "*": false                        # force hermeticity (also in repo mode)
          "memsearch@my-marketplace": true  # explicit entries win
    ```

    `"*"` is **harness-interpreted** — upstream Claude Code has no
    `enabledPlugins` wildcard — and is stripped before `settings.json` is
    written, so the CLI never sees it. `"*": true` opts out of hermeticity
    entirely. Configs that relied on an installed plugin silently loading
    into case sessions fail loudly (`Unknown command`) after upgrading; the
    fix is one explicit entry or a `plugin_dirs` entry.

=== "cursor"

    Cursor recognizes `binary` to select a specific `cursor-agent` executable.
    Other Cursor-specific connection, trust, header, MCP, and filesystem flags
    are intentionally not part of the harness runner contract; configuring one
    produces a warning and the key is ignored. Use `runner.env` for provider
    environment variables such as `CURSOR_API_KEY` or `CURSOR_API_ENDPOINT`.

    ```yaml
    runner:
      type: cursor
      settings:
        binary: /opt/cursor-agent
    ```

=== "responses-api"

    Connection and container settings for the OpenAI Responses API. Recognized keys:
    `base_url`, `api_key`, `default_model`, `network_policy`, `memory_limit_mb`
    (default `512`). Missing `base_url` / `api_key` / `default_model` fall back to the
    `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `OPENAI_MODEL` env vars.

    ```yaml
    runner:
      type: responses-api
      settings:
        default_model: gpt-5
        memory_limit_mb: 4096
    ```

    !!! warning "`settings.api_key` is a secret authored in your config"
        The runner reads `api_key` from `runner.settings` as written — the one place a
        provider credential can live in `eval.yaml`, outside the env-only rule that
        applies to `OPENROUTER_*`. Leave it unset and export `OPENAI_API_KEY` instead;
        the runner falls back to it.

=== "codex"

    Each key is passed to `codex exec` as a `-c key=value` config override.
    `model_reasoning_effort` is also accepted here as a fallback when
    `runner.effort` is unset; the top-level `effort` field takes precedence
    and is validated against Codex's effort values.

    ```yaml
    runner:
      type: codex
      settings:
        model_reasoning_effort: xhigh
    ```

The `cli` runner ignores `settings`.

### `plugin_dirs`

`claude-code`, `cursor`, and `codex`. These runners copy plugin content into the case workspace
rather than exposing the configured path to the session. Claude Code stages each
entry's discoverable content (manifest, skill roots, `commands/`, `agents/`,
`hooks/`, `scripts/`) into `.staged-plugins/` and passes the staged copy to
`--plugin-dir` — the configured path would otherwise land verbatim in session
context, where Bash (not path-gated) can follow it out of the workspace. An entry
already inside the workspace is passed through unchanged, and
`workspace_mode: repo` skips staging for Claude Code and Cursor — the workspace is
the real project there, so there is nothing to isolate. Cursor applies common
permissions through a temporary project-local `.cursor/cli.json` and restores the
original file after the run; in isolated mode it follows the same `--plugin-dir`
staging path and embeds the selected `SKILL.md` in its prompt because Cursor does
not consume the harness's slash-command target directly. Codex copies
each plugin's skills into the case workspace's `.agents/skills` directory for the
duration of the run. Relative paths are always
resolved from the project root.
A lexically external path such as `../shared-skills` is an explicit opt-in like an
absolute path; a path declared inside the project may not escape through a symlink.

```yaml
runner:
  type: claude-code
  plugin_dirs:
    - ./my-plugin
    - ../shared-skills
```

### `env`

`claude-code`, `cursor`, `codex`, and `fullsend`. Extra environment variables injected into the
runner subprocess, **additive** to the runner's built-in safe allowlist (`PATH`, `HOME`,
provider credentials, `MLFLOW_TRACKING_URI`, …). A value starting with `$` is resolved
from the caller's environment; missing vars are dropped. Cursor reads its provider
credentials (e.g. `CURSOR_API_KEY`, `CURSOR_API_ENDPOINT`) from here — not from
`execution.env`, which has a different scope.

```yaml
runner:
  type: claude-code
  env:
    ANTHROPIC_AUTH_TOKEN: $ANTHROPIC_AUTH_TOKEN   # forward from caller
    FEATURE_FLAG: "enabled"                        # literal
```

!!! note "Where env vars belong"
    `runner.env` forwards vars **into the agent runtime**. To make a var available to
    the skill *and its hooks* inside each case workspace, use
    [`execution.env`](execution.md) instead. The `cli` runner reads only
    `execution.env` — `runner.env` is a no-op there. See
    [environment variables](../environment-variables.md).

!!! note "Managed keys under an OpenRouter plan"
    While `models.skill` is `openrouter:/…`, the provider plan owns the agent's routing
    variables on `runner.env` and `runner.settings.env` as on every other env surface,
    and its block reaches the agent through the runner's own channel (overlay,
    `--agent-env`, pod spec + Secret), winning over `runner.env` — the rule is spelled
    out once in [models.providers → Validation](providers.md#validation).

### `system_prompt`

Extra system-prompt text prepended to the agent's context.

- `claude-code` — passed via `--append-system-prompt`, composed with the harness prompt.
- `cursor` / `codex` — prepended to the user prompt.
- `cli` — exposed as the `{system_prompt}` placeholder in the command template.
- `responses-api` — sent as a `developer` role message.

```yaml
runner:
  system_prompt: "You are being evaluated. Follow the skill instructions exactly."
```

### `command`

`cli` only, and **required** for it. A command template — a string (shell-parsed via
`shlex`) or a list of arguments (safer; no shell parsing). Placeholders are substituted
before execution and string values are shell-quoted.

Common placeholders (full list in the
[contract](https://github.com/opendatahub-io/agent-eval-harness/blob/main/docs/opaque-cli-runner-contract.md)):

| Placeholder | Value |
| --- | --- |
| `{agent}` | Skill name (empty in prompt mode) |
| `{workspace}` | Absolute case workspace path |
| `{output_dir}` | `{workspace}/output` (write artifacts here) |
| `{model}` | Resolved model (`--model` or `models.skill`) |
| `{subagent_model}` | Subagent model (empty if unset) |
| `{args}` | Resolved `execution.arguments` |
| `{effort}` / `{system_prompt}` | From `runner.effort` / `runner.system_prompt` |
| `{timeout}` / `{max_budget_usd}` | From `execution` (budget is advisory only) |
| `{field}` | Any field from the case `input.yaml` |

```yaml
runner:
  type: cli
  command: "opencode run --model {model} --cwd {workspace} '/{agent} {args}'"
```

!!! warning "Contract obligations"
    An opaque command **must** exit non-zero on failure and write artifacts to
    `{output_dir}` (or the `outputs[*].path` dirs). To surface token/cost data it must
    write `{output_dir}/metrics.json` — otherwise cost tables are empty. Tool
    interception, stream-json tracing, subagent capture, and budget *enforcement* are
    Claude-Code-only and do not work with `cli`. See the
    [cross-runner cookbook](../../cookbook/cross-runner-opencode.md).

### `agent`, `fullsend_dir`, `binary`, `extra_args`, `term_grace_s`

`fullsend` only. `agent` is **required** with `type: fullsend` (a missing value fails
config validation naming `runner.agent`): the name of an agent registered in
`<fullsend_dir>/config.yaml`. `fullsend_dir` defaults to `.fullsend` and resolves
against the project root. `binary` defaults to `fullsend` on `PATH`; a relative
value containing a path separator resolves against the project root. `extra_args`
(a list of strings) is appended verbatim to the command; `--no-post-script` is
rejected, because the project's post-script is what copies the agent's outputs into
the workspace. `term_grace_s` (positive number, default 30) is the time between
SIGTERM and SIGKILL when the eval timeout expires. `type: fullsend` is rejected at
load time on `execution.prompt` configs and on an agent judge's `agent.runner`
(prompt mode is unsupported):

```text
<binary> run <agent> --fullsend-dir <abs fullsend_dir> --target-repo <abs workspace>
  [--model <model>] [--effort <effort>] --output-dir <workspace>/.fullsend-runs <extra_args>
```

The process environment carries `FULLSEND_TASK="/<skill> <args>"`,
`AGENT_EVAL_SUBAGENT_MODEL` (only when a subagent model is set; the agent definition
must reference it, or the runner warns), and every `execution.env`, `runner.env` and
hook-output entry, on top of the caller's environment. The workspace
`.claude/settings.json` is stripped of `env`, the harness's own hook entries
(`SubagentStop` capture and `PreToolUse` interception, recognised because their
command runs a script from the workspace's own `hooks/` directory) and `permissions.additionalDirectories` before the upload
and before symlinks are materialised; the project's own hooks (carried over by
`execution.project_hooks`) stay in the file, as in production; a
symlinked `.claude` or `settings.json` fails the case, and `.claude` settings files
and `.claude` directories, however reached, are never copied; only the workspace's own
top-level settings link is. Artifacts reach the workspace
through the project's post-script copy into `--target-repo`; `iteration-N/output/`
is not read. All iterations' streams and subagent transcripts are mapped. fullsend
aborts before the pre-script when a `${VAR}` the agent's harness references is unset
on the host, so that harness may only reference these variables or ones from the
caller's environment. Requires fullsend ≥ 0.48, a running OpenShell gateway with
podman, and the agent's own credentials on the host. Keep the fullsend harness's
`timeout_minutes` below `execution.timeout`. `max_budget_usd` is a hint only, because
the fullsend harness enforces its own budget. See the
[runners concept](../../concepts/runners.md#fullsend) for the result mapping.

```yaml
runner:
  type: fullsend
  agent: rfe-creator-eval
  fullsend_dir: .fullsend
  extra_args: [--keep-sandbox]
```

#### Run provenance

A fullsend run records what executed it, so results stay comparable across runs.
The fields go into `eval_params` in the run-level `run_result.json`. The report's
Run Configuration header shows them, and `/eval-mlflow` logs the non-null ones as
MLflow run parameters. The fields are captured once per run:

| Field | Source |
|---|---|
| `fullsend_agent` | `runner.agent` |
| `fullsend_version` | `<binary> version`, falling back to `<binary> --version` |
| `openshell_version` | `openshell version` / `openshell --version` when `openshell` is on `PATH` |
| `sandbox_image` | The `Image:` line in fullsend's console output (the run's captured `stderr.log`). Falls back to `image:` in the agent's harness file only when no invocation printed that line. The harness file is the one `<fullsend_dir>/config.yaml` registers: the last enabled `agents[]` entry whose `name` or source file name (without extension or `#` fragment) matches `runner.agent`, case-insensitively, resolved as `<fullsend_dir>/<source>`. A URL source, an override-only entry (no `source`) or a path outside `fullsend_dir` gives `null`. `<fullsend_dir>/harness/<agent>.yaml` is read only when no entry matches. A child harness without its own (non-empty) `image:` takes the first one on its `base:` chain. A local `base:` resolves relative to the file that names it. A URL or absolute `base:`, a base outside `fullsend_dir`, or a cycle gives `null` |
| `sandbox_image_source` | `"fullsend-output"` or `"harness-file"`, depending on where `sandbox_image` came from |
| `sandbox_arch` | The architecture fullsend printed next to the image or on an `Arch:` line. fullsend prints none today, so this stays `null`; the field is kept for when it does |
| `agent_harness_commit` | `git rev-parse HEAD` of `fullsend_dir` when it is inside a git checkout |

The console output is authoritative for the image: `FULLSEND_SANDBOX_IMAGE` in
fullsend's environment (the runner passes the caller's environment through)
replaces the harness's pinned image. `FULLSEND_SANDBOX_ARCH` selects the
architecture of the fullsend binary copied into the sandbox, not the image's.

Provenance reflects the top-level `runner:` block only; step-level runner
overrides are not recorded. The version probes run with `execution.env` and
`runner.env` applied, not a step's or a hook's environment.

Every field is optional. A missing tool or a failed parse records `null` and
prints a warning, and never fails the run. Other runners do not write these keys
at all.

### `workspace_mode`

Execution context. Validated at load time — only `None` or `"repo"` are accepted (a
typo raises rather than silently changing behavior). Claude Code, Cursor, and the
opaque runner honor `repo`; Codex rejects it because the harness cannot enforce
repository protections there.

| Value | Meaning |
| --- | --- |
| *unset* (`None`) | **Isolated workspace** (default) — each case runs in its own temp workspace with symlinked project resources |
| `repo` | **In-repo** — the agent runs against the real repository checkout |

`workspace_mode: repo` is meaningful for **prompt mode** evals using Claude Code or
Cursor that need the agent to navigate the actual repository (e.g. agentic-docs
testing). Pair it with
[`permissions`](permissions.md) to keep the agent from writing to the repo.

```yaml
execution:
  prompt: "{{ input.prompt }}"

runner:
  type: claude-code
  workspace_mode: repo    # navigate the real repository
```

## Examples

=== "Claude Code (default)"

    ```yaml
    runner:
      type: claude-code
      effort: high
      plugin_dirs:
        - ./my-plugin
      env:
        ANTHROPIC_AUTH_TOKEN: $ANTHROPIC_AUTH_TOKEN
    ```

=== "Codex"

    ```yaml
    runner:
      type: codex
      effort: xhigh
      plugin_dirs:
        - ./my-plugin
      env:
        OPENAI_API_KEY: $OPENAI_API_KEY
    ```

=== "Opaque CLI"

    ```yaml
    runner:
      type: cli
      command:
        - opencode
        - run
        - --model
        - "{model}"
        - "/{agent} {args}"
    ```

=== "fullsend"

    ```yaml
    runner:
      type: fullsend
      agent: rfe-creator-eval
      env:
        GOOGLE_APPLICATION_CREDENTIALS: $GOOGLE_APPLICATION_CREDENTIALS
    ```

=== "Responses API"

    ```yaml
    runner:
      type: responses-api
      settings:
        default_model: gpt-5
        memory_limit_mb: 4096
    ```

## See also

<div class="grid cards" markdown>

- [**runners concept**](../../concepts/runners.md) — how runtimes differ and when to reach for each
- [**models**](models.md) — model-per-role and the `{model}` / `{subagent_model}` resolution
- [**execution**](execution.md) — `execution.env`, timeout, budget, parallelism
- [**permissions**](permissions.md) — allow/deny, essential for `workspace_mode: repo`
- [**headless execution**](../../guides/headless.md) — how the Claude Code runner is driven non-interactively

</div>
