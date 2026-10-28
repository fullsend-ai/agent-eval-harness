# Data Pipeline Reference

How data flows from dataset cases through execution, collection, and scoring.

## 1. Dataset → Workspace

**Input**: `dataset.path` from eval.yaml pointing to a directory of case subdirectories.

**What workspace.py does**:
- Reads each case directory. Finds the first `.yaml`/`.yml`/`.json` file and loads its full content.
- Builds `batch.yaml` — a list of one entry per case, each entry being the full parsed content of the input file. No field extraction — the entire input file content is included.
- Builds `case_order.yaml` — maps position to case ID (`[{case_id: "case-001-name"}, ...]`)
- Creates output directories from `outputs[].path` in eval.yaml
- Provisions `dataset.workspace.files` (case mode): per-case string paths are copied from the
  case dir (a listed file symlink is materialized as a real file); shared `{dest, source}`
  entries are copied — never symlinked — into the case at `dest`, with `source` resolved
  against the project root or `runner.plugin_dirs`. The same materialization
  (`agent_eval/workspace_provisioning.py::materialize_shared_files`) is reused by the Harbor
  task packager and the S3/EvalHub export step, so shared files behave identically everywhere.
- Symlinks project resources (`.claude/`, `CLAUDE.md`, `scripts/`, `skills/`, `.context/`, plus any top-level entries listed in `execution.symlinks` — reserved harness-written names are rejected at config load; a configured entry that is missing or resolves outside the project root and plugin dirs is skipped with a warning, and one whose workspace path already exists fails staging; `--symlinks` replaces the list)
- If `inputs.tools` configured, generates `tool_handlers.yaml` and `.claude/settings.json` with hooks

**Workspace structure**:
```
/tmp/agent-eval/{run-id}/
  batch.yaml              # Full input data per case
  case_order.yaml         # Position → case ID mapping
  {output_dirs}/          # Empty dirs for skill outputs
  .claude/settings.json   # Generated hook config (if inputs.tools)
  hooks/tools.py          # Hook script (if inputs.tools)
  tool_handlers.yaml      # Handler config (if inputs.tools)
  scripts/ → symlink
  .claude/ → symlink (or generated)
  CLAUDE.md → symlink
  skills/ → symlink
  .staged-plugins/        # Staged copies of out-of-workspace runner.plugin_dirs
                          # (claude-code runner; skipped in workspace_mode: repo)
```

### Case Mode (execution.mode: case)

When `execution.mode` is `case` (default), workspace.py creates a separate workspace per case:

```
/tmp/agent-eval/{run-id}/
  case_order.yaml         # [{case_id: "case-001-name"}, ...]
  cases/
    case-001-name/
      input.yaml          # Copied from dataset
      strategy.md         # Copied from dataset (companion files)
      answers.yaml        # Copied from dataset (if present)
      {output_dirs}/      # Empty dirs for skill outputs
      .claude/settings.json  # Generated (hooks + permissions + the synthesized
                             # enabledPlugins denylist: installed plugins are
                             # disabled by default in isolated workspaces)
      subagents/           # SubagentStop hook target
      .staged-plugins/     # Staged copies of out-of-workspace runner.plugin_dirs
      scripts/ → symlink
      CLAUDE.md → symlink
      skills/ → symlink
    case-002-name/
      ...
```

Each case gets ALL files from the dataset case directory (not just input.yaml), plus symlinked project resources and hooks. The skill runs in the case workspace as its working directory.

## 2. Workspace → Execution

**What execute.py does**:
- Invokes the skill via the configured runner (e.g., `claude --print`)
- Passes `batch.yaml` content as the skill prompt via stdin
- Captures stdout (stream-json events) and stderr
- Writes `stdout.log` and `stderr.log` to `$AGENT_EVAL_RUNS_DIR/{id}/`
- Parses the final `result` event for token usage and cost; `cost_usd` is the **billed** cost — the conversation `total_cost_usd`, raised to the per-model `modelUsage` sum when that exceeds it by more than $0.01 (background agents killed after the final turn, or still running at an evaluator timeout, burn billed tokens the conversation total never sees)
- Writes `run_result.json` with exit_code, duration_s, token_usage, cost_usd, and permission_denials (the CLI's structured denied-tool-call objects `{tool_name, tool_use_id, tool_input}`; `[]` when none) — `exit_code` reflects case truth, not the raw process code: the runner reports 1 when the CLI killed background tasks that outlived the final turn (or ran an unknown command) even though the process exited 0, with the reason appended as an ERROR note in stderr

**What the skill sees (batch mode)**:
- Working directory: the workspace
- Input: batch.yaml content (via skill prompt)
- Output directories: created and ready
- Hooks: if configured, AskUserQuestion gets auto-answered, external services get checked

**What the skill sees (case mode)**:
- Working directory: the case workspace (`workspace/cases/{case_id}/`)
- Input: case-specific arguments resolved from `execution.arguments` template + all case files on disk
- Output directories: created and ready
- Hooks: per-case hooks with SubagentStop for transcript capture

**Case mode execution**:
- execute.py iterates `case_order.yaml`, invoking the skill once per case
- When `execution.parallelism` > 1 (or `--parallelism` CLI), cases run concurrently via thread pool; each case gets a per-case log prefix (e.g., `eval:case-003`)
- Each case gets its own stdout.log, stderr.log, and subagent transcripts
- Results are saved to `$AGENT_EVAL_RUNS_DIR/{id}/cases/{case_id}/stdout.log`
- run_result.json includes `execution_mode: "case"`, `per_case` breakdown, `duration_s` (sum of per-case durations), and `wall_clock_s` (actual elapsed time). Each `per_case` entry (and each step entry under a multi-step case's `steps`) carries its own `permission_denials` list; the run-level aggregate and the case-level entry of a multi-step case carry the concatenation

## 3. Execution → Collection

**What collect.py does**:
- Reads `case_order.yaml` to know which case is at which position
- For each `outputs[].path` in eval.yaml, scans the workspace output directory
- Groups files by detected prefix pattern (e.g., "RFE-001", "RFE-002") or by position
- Copies each group's files to `$AGENT_EVAL_RUNS_DIR/{id}/cases/{case-id}/{output-path}/`
- Writes `collection.json` with per-case artifact counts

**Per-case output structure**:
```
$AGENT_EVAL_RUNS_DIR/{id}/
  run_result.json         # Execution metadata
  stdout.log              # Full stdout
  stderr.log              # Full stderr
  collection.json         # Per-case artifact counts
  cases/
    case-001-name/
      artifacts/          # Files from outputs[0].path
        RFE-001-slug.md
      artifacts/reviews/  # Files from outputs[1].path
        RFE-001-review.md
      _modified/          # In-place edits (auto-detected via git diff)
        source.md
    case-002-name/
      artifacts/
        RFE-002-slug.md
      _modified/
        source.md
```

### In-place file edits

Skills that modify input files using the Edit tool (rather than writing to an output directory) are handled automatically. `workspace.py` creates a git commit of the initial workspace state before execution. After execution, `collect.py` runs `git diff HEAD` (plus `git ls-files --others` for new files) to detect modified files and copies them to a `_modified/` directory under each case's run output. No extra `outputs` config is needed. Harness-created paths (`.claude/`, `.git/`, `.work/`, `.staged-plugins/`, `subagents/`, `hooks/`, logs) and directories declared in `outputs[].path` are excluded, so only files the skill itself modified or created appear in `_modified/`.

## 4. Collection → Scoring

**What score.py's `load_case_record()` builds for each case**:

```python
{
    # --- File artifacts (from outputs with path) ---
    "files": {
        "artifacts/RFE-001-slug.md": "<full file content>",
        "artifacts/reviews/RFE-001-review.md": "<full file content>",
        # In-place edits (auto-collected via git diff)
        "_modified/source.md": "<edited file content>",
    },
    "artifacts_content": "<content of first file in artifacts/>",
    "artifacts_file": "/path/to/artifacts/RFE-001-slug.md",
    "reviews_content": "<content of first file in reviews/>",

    # --- In-place edits (convenience access) ---
    # Same content as files["_modified/..."] but keyed by filename only.
    # Use this for direct access: outputs.get("modified_files", {}).get("source.md")
    "modified_files": {
        "source.md": "<edited file content>",
    },

    # --- Conversation text (convenience key for check judges) ---
    # Pre-extracted root-level assistant text from events. Use this instead
    # of manually parsing events for assistant text.
    "conversation": "Let me check...\n\nHere are the results...",

    # --- Structured events (if traces.events enabled, default: true) ---
    # Flat list of typed event dicts parsed from stdout.log JSONL.
    # Event types: assistant, tool_result, system, result.
    # Subagent events carry parent_tool_use_id and agent_id tags.
    # Tool results include content (capped at 50K chars by default).
    "events": [
        {"type": "assistant", "text": "Let me check...", "tools": [...], "timestamp": "..."},
        {"type": "tool_result", "tool_use_id": "tu_001", "tool_name": "Read",
         "content": "file contents", "is_error": False, "timestamp": "..."},
        {"type": "result", "cost_usd": 0.15, "num_turns": 12, "timestamp": None},
    ],

    # --- Tool calls (from outputs with tool, derived from events) ---
    "tool_calls": [
        {
            "name": "mcp__atlassian__create_issue",
            "input": {"title": "...", "description": "..."}
        }
    ],

    # --- Execution metadata (if traces.metrics enabled) ---
    "exit_code": 0,
    "duration_s": 45.2,
    "token_usage": {"input": 5000, "output": 2000},
    "cost_usd": 0.15,
    "num_turns": 12,

    # --- Logs (if traces.stderr enabled) ---
    "stderr": "<full stderr.log content>",

    # --- Hook data (from hooks that wrote $AGENT_EVAL_HOOK_OUTPUT) ---
    "hook_outputs": {"expected_count": 3},

    # --- Per-case hook results (only when before_each/after_each ran) ---
    "hooks": {"after_each": [{"description": "Save the PR diff",
                              "exit_code": -1, "timed_out": True,
                              "skipped": False, "duration_s": 120.0,
                              "on_failure": "continue",
                              "log": "hooks/after_each.case-001.0.log"}]},

    # --- Annotations (from dataset case directory) ---
    "annotations": {"expected_voice": "technical", "difficulty": "easy"},

    # --- Context ---
    "case_dir": "/absolute/path/to/case"
}
```

**Template variables in LLM judges**: All LLM judge prompts (both `prompt`/`prompt_file` and `builtin` LLM judges) are rendered with Jinja2. Available variables:
- `{{ outputs }}` — renders ALL file entries including `_modified/` as markdown sections
- `{{ conversation }}` — root-level assistant **visible** text from the JSONL event stream (excludes subagent text, tool calls, and extended-thinking)
- `{{ reasoning }}` — like `{{ conversation }}` but also includes the model's extended-thinking (chain-of-thought), for reasoning-quality judges; capped at ~400K chars. Needs `traces.events: true`
- `{{ inputs }}` — the case's `input.yaml` formatted as `**key**: value` per field (nested dict/list values are `yaml.safe_dump`ed for readability)
- `{{ evidence }}` — a structured summary of what the agent actually did (turn count, cost, tools invoked, scripts executed, files read/written), derived from the parsed event stream. Extracted lazily and cached, only when the prompt references `{{ evidence }}`. Runner-agnostic — matches tool-name / input-key aliases across Claude Code, opencode, codex, responses-api
- `{{ tool_trace }}` — chronological trace of tool calls (Read, Bash, Agent, etc.) with key inputs. Use for behavior judges (navigation, tool usage)
- `{{ annotations }}` — dataset annotations from `annotations.yaml`
- `{{ arguments }}` — judge arguments from eval.yaml's `arguments:` field (dict)

Use `{{ arguments.key }}` to parameterize prompts without editing the prompt text. For example, `{{ arguments.strictness | default('medium') }}` lets users control judge behavior via eval.yaml.

**Key naming convention for convenience keys**: for an output with `path: "artifacts/rfe-tasks"`, the convenience key is `rfe-tasks_content` (the last directory component + `_content`). For `path: "."`, the key is `main_content`.

## 5. Scoring → Judges

### Five judge types

The harness supports five judge kinds, each discriminated by which field is set in eval.yaml. Each is its own subsection below.

#### Builtin judge (`builtin` field)
- Resolves via `BuiltinJudgeRegistry` from `agent_eval/judges/` package
- Python builtins (`.py`): receive `(outputs, **arguments)`, return `(bool|number, str)`
- LLM builtins (`.md`): Jinja2 prompt templates, rendered with `arguments` and `outputs`
- Auto-discovered from `agent_eval/judges/{category}/` directories
- Parameterized via `arguments:` field in eval.yaml

#### Inline check (`check` field)
- Python snippet wrapped in a function by `score.py`
- Receives `(outputs, arguments)` — full record dict + arguments from eval.yaml
- Must return `(bool|number, str)` — pass/fail + rationale, or score + rationale
- Example accessing file content: `outputs["artifacts_content"]`
- Example accessing traces: `outputs.get("cost_usd", 0)`
- Example with arguments: `limit = arguments.get("max_chars", 10000)`

#### LLM judge (`prompt` or `prompt_file` field)
- All prompts are Jinja2 rendered with variables: `{{ outputs }}`, `{{ conversation }}`, `{{ reasoning }}`, `{{ inputs }}`, `{{ evidence }}`, `{{ tool_trace }}`, `{{ annotations }}`, `{{ arguments }}`
- `context` files are appended to the prompt
- `arguments:` field makes prompts parameterizable without editing the prompt text
- Returns `{"rationale": "...", "score": N}` or `{"rationale": "...", "passed": bool}` — the verdict schema (a structured output or the forced tool, whichever transport carries it) elicits the rationale *before* the verdict so the model reasons before committing. Anthropic and OpenAI judges use a structured API response; runner-backed judges (`runner:/…` models) must emit exactly that one JSON object on stdout, without a conversational preamble. The `output/score.json` file contract applies only to `agent:` judges

#### External code judge (`module` + `function` field)
- Imported via `importlib` from the project
- Receives `(outputs=dict, **arguments)` when `arguments:` is set
- Must return `(bool|number, str)` tuple

#### Agent judge (`agent` field)
A tool-using judge run through the runner abstraction.
- Discriminated by an `agent:` block; still takes its instructions from `prompt`/`prompt_file`/`llm_rubric` (checked *before* the LLM branch, so `agent:` upgrades an otherwise-LLM judge). Reuses `model`, `feedback_type`, `score_range`, `samples`, `if`, `thresholds`.
- Per invocation the harness: (1) stages an isolated workspace — the case's declared `outputs[].path` dirs and `_modified/` (filtered by `agent.inputs`, default all) are **copied from the case directory** as real files, never loaded into memory: binary files arrive intact, a link inside an output dir becomes a regular file, and one resolving outside that dir is skipped with a line on stderr. When every judge is an agent judge whose prompt does not render `{{ outputs }}` (and no `if:` names `outputs`), scoring leaves `outputs["files"]` empty instead of reading the trees. Each `agent.context` dir/file is **copied** under `./.context/` (never symlinked, so a judge write can't follow a link back to real project files, CWE-59/829). `./output/` is **writable** for the verdict file. The judge reads everything in `agent.context`, so point `agent.context` only at **trusted, author-controlled** paths; a context entry whose *content* cannot be copied (an unreadable file, an unlistable directory) fails the judge rather than leaving it to grade material with holes in it, while an entry with nothing to copy by nature (dangling link, socket, `latest -> .` loop, a link resolving outside the entry) is skipped with a line on stderr. Output paths are containment-checked, so one resolving outside the case dir is never staged (CWE-22), and case artifacts named `output/` or `.context/` are never staged over the reserved verdict and context dirs; (2) instantiates the runner from `RUNNERS[agent.runner.type]` (default `claude-code`) with `permissions={"allow": agent.allowed_tools, "deny": agent.denied_tools}` (defaults `[Read(**), Edit(output/**)]` and `[Bash, WebFetch, WebSearch, Agent, Edit, MultiEdit, NotebookEdit]`; on the Claude Code runner the judge also gets `--permission-mode manual --permission-prompts none`, which is what makes a rule the policy does not cover a refusal rather than a silent success (the read-only tools still work inside the working directory whether or not the list names them; the gate is scope, not enumeration) — a BARE `Read`/`Grep`/`Glob` would remove the CLI's working-directory confinement, and `Write(<path>)` matches nothing, so writes are scoped with `Edit(<path>)`; the **default** deny has anything `allowed_tools` names subtracted from it, plus the rest of that tool's capability group (`Write`/`Edit`/`MultiEdit`/`NotebookEdit`, `Agent`/`Task`) — a deny beats an allow, so `Bash` left denied would take away what `Bash(pytest:*)` asks for, and `Edit` left denied is a `Write(**)` deny on Cursor that takes the verdict file away — while a **written** `denied_tools` is used outright with no subtraction, and `Write` is excluded from the default because no default-allowed tool can write the verdict otherwise) — the judge gets its OWN runner + tool policy, independent of the skill-under-test, via a shallow `EvalConfig` copy carrying the judge's `RunnerConfig` and permissions; (3) runs one **prompt-mode** turn (`execute(target=None, args=<rendered instructions + output contract>, …)`); (4) reads the verdict; (5) tears down the temp workspace.
- **Output contract**: the judge writes `./output/score.json` — `{"rationale": "…", "score": <number>}` (numeric) or `{"rationale": "…", "passed": <bool>}` (bool), rationale first so the judge articulates its assessment before committing to the verdict. `feedback_type` selects which; the numeric spec is generated from the judge's effective bounds, so it reads `<integer in [1, 5]>` when no `score_range` is declared and `<integer|number in [lo, hi]>` when one is. A declared range is enforced, not clamped — an off-scale verdict is recorded as an error sample and dropped from the mean. The harness appends this contract + an untrusted-data guard to the prompt automatically. Fallback: if `score.json` is absent, parse the last `{"score"|"passed", …}` JSON object from stdout; if neither yields a value, record an error sample (never silently pass).
- `agent.runner:` is parsed exactly like the top-level `runner:` block. `agent.timeout` (default `execution.timeout` or harness default) and `agent.max_budget_usd` (default `2.0`) bound each run.
- Use for grading that requires **looking something up** (verify architecture/API/CRD claims against real docs, run touched tests with `allowed_tools: [..., Bash]`), where a text-only LLM judge would guess and inflate. The default `allowed_tools` is `[Read(**), Edit(output/**)]`; **enable `Bash` only on a runner that provides OS-level sandboxing — no network, cleaned host credentials, path confinement — because the judge reads untrusted case artifacts (CWE-78/CWE-200).** `runner.workspace_mode: repo` is rejected for agent judges (they must run in the isolated staged workspace, CWE-829).

### How aggregation works

For each judge across all cases:
- **Boolean values**: aggregated as `pass_rate` (fraction True)
- **Numeric values**: aggregated as `mean`
- Results written to `summary.yaml` with `per_case` and `aggregated` sections
- `summary.yaml` also carries `judge_usage` (judge cost, tokens, verdict transport counters) and `total_cost_usd`

### Gating `min_win_rate` and the pairwise `max_error_rate`

`min_win_rate` reads the `pairwise` section that `score.py pairwise --baseline` writes, so only `score.py regression` evaluates it — `score.py judges` runs first and skips it. The `pairwise` judge's `max_error_rate` is read from the same section (`errors / cases_compared`), so it is gated at the same point. When `thresholds` sets either, run the gate after the comparison:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/score.py regression \
  --run-id <id> \
  --config <config>
```

## Traces Configuration

| `traces` field | What it controls | Where data comes from | What judges access |
|----------------|------------------|-----------------------|-------------------|
| `stdout: true` | Keep raw stdout.log on disk | `stdout.log` in case/run dir | Debugging only (not loaded into record) |
| `stderr: true` | Capture full stderr | `stderr.log` in case/run dir | `outputs["stderr"]` |
| `events: true` | Parse JSONL into events.json | Parsed from stdout.log at collection time | `outputs["events"]` (list of event dicts). Tool results/inputs capped at 50K chars |
| `metrics: true` | Capture execution metadata | `run_result.json` | `outputs["exit_code"]`, `["duration_s"]`, `["cost_usd"]`, `["num_turns"]`, `["token_usage"]` |

> `exit_code` reflects case truth, not the raw process code: the runner reports 1 when the CLI killed background tasks that outlived the final turn (or ran an unknown command) even though the process exited 0 — the reason is appended as an ERROR note in `outputs["stderr"]`. `cost_usd` is billed cost — the conversation total, raised to the per-model `modelUsage` sum when that exceeds it by more than $0.01 — so it includes tokens burned by killed or still-running background agents. `permission_denials` is persisted in run_result.json but NOT loaded into the judge record — there is no `outputs["permission_denials"]`; a check judge that needs it can read `Path(outputs["case_dir"]) / "run_result.json"`.

Note: `events.json` is generated at collection time by `collect.py` and includes merged subagent transcripts from `subagents/*.jsonl`. Tool calls in `outputs["tool_calls"]` are derived from events.

### Important: stdout format for Claude Code runner

When `runner.type: claude-code`, `outputs["stdout"]` contains the **raw JSONL event stream** from `claude --print`, not plain text. Each line is a JSON object with `type` (`user`, `assistant`, `system`, `result`). Assistant text is in objects like:

```json
{"type": "assistant", "message": {"content": [{"type": "text", "text": "..."}]}}
```

**For check judges**: use `outputs["events"]` to access structured events. Filter by type and iterate:

```python
# Get all root-level assistant text
texts = [e["text"] for e in outputs.get("events", [])
         if e["type"] == "assistant" and not e.get("parent_tool_use_id") and e.get("text")]

# Get tool call sequence
tools = []
for e in outputs.get("events", []):
    if e["type"] == "assistant" and not e.get("parent_tool_use_id"):
        tools.extend(t["name"] for t in e.get("tools", []))

# Separate root from subagent events
root_events = [e for e in outputs.get("events", []) if not e.get("parent_tool_use_id")]
sub_events = [e for e in outputs.get("events", []) if e.get("parent_tool_use_id")]
```

**For LLM judges**: use `{{ conversation }}` to render root-level assistant text (excludes subagent text and tool calls). Use `{{ tool_trace }}` to show which tools the agent called (Read, Bash, Agent, etc.) — essential for judges evaluating agent behavior like documentation navigation. Use `{{ outputs }}` for file artifacts and modified files. If the skill edits files in-place, the rewritten content appears in `{{ outputs }}` via the `_modified/` collection.
