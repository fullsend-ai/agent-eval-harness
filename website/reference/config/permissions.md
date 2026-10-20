# permissions

The `permissions` block constrains which tools the agent may use during a
**headless** run. It accepts two lists — `allow` and `deny` — and each entry may
be a simple string pattern or a path-based dict. The harness compiles these into
the exact patterns Claude Code's `settings.json` expects.

```yaml
permissions:
  deny:
    - "mcp__*"                              # simple: block all MCP tools
    - { path: "eval/", tools: ["Read", "Bash"] }   # path-based: protect the answer key
  allow:
    - "Skill"
    - { path: "artifacts/", tools: ["Write"] }
```

!!! note "Cursor deny-only configurations"
    When `runner.type: cursor` has only `permissions.deny`, the harness writes a
    broad Cursor `permissions.allow` list alongside those denies. Cursor's
    project-local `.cursor/cli.json` schema requires `allow`; omitting it makes
    the generated file invalid. If neither list is configured, the harness
    writes no project permission file and Cursor's normal defaults apply — and an
    **existing** project `.cursor/cli.json` stays in effect. The harness does not
    disable a pre-existing config, so in `workspace_mode: repo` it is not a
    security boundary: a checked-in `.cursor/cli.json` can still grant access to
    protected files. When `permissions.allow` is set but **no** rule maps to a
    Cursor pattern (an unmapped tool, or a path-scoped allow Cursor can't
    express), the harness writes an empty allowlist — which Cursor reads as
    deny-everything — and emits a `RuntimeWarning`; it never silently widens
    access.

## Two forms

| Form | Shape | Use for |
| --- | --- | --- |
| **Simple** | a string — a tool name or a pre-formed pattern (`"Skill"`, `"mcp__*"`, `"Edit(artifacts/**)"`) | Coarse tool allow/deny; passed through **as-is**. |
| **Path-based** | a dict `{ path: <dir-or-file>, tools: [<tool>, …] }` | Scoping file tools to (or away from) a directory — e.g. keeping a prompt-mode agent out of the answer key. |

You can mix both forms in the same list. The presence of *any* dict switches the
whole block onto the path-based code path (see [How they compile](#how-they-compile)).

Both lists also land, compiled the same way, in the workspace `.claude/settings.json`
the local runner writes — with or without tool interception, in batch, per-case and
repo mode — so the workspace settings carry the same rules the runner passes on the
command line: one file to inspect, and the one the [absolute Bash
twins](#relative-bash-rules-get-an-absolute-workspace-twin) are added to. (In earlier
releases a path-based rule in `allow` aborted the workspace builders before the agent
started, and `deny` reached the settings only through the interception generator; with
simple rules the main agent still received both lists through the runner's CLI flags.)

## Path-based rules and the compiler

Path-based rules are normalized by
[`compile_permission_rules`](https://github.com/opendatahub-io/agent-eval-harness/blob/main/agent_eval/tools/permissions.py)
in `agent_eval/tools/permissions.py`. The rules that shape the conversion:

### Only file-scoped tools can take a path

A path only makes sense for tools whose argument *is* a file. The compiler
recognizes exactly these (`PATH_SCOPED_TOOLS`):

```python
PATH_SCOPED_TOOLS = ("Read", "Edit", "Write", "Grep", "Glob")
```

Any other tool listed in `tools` is silently dropped from a path-based rule
(with one exception for `Bash` in deny lists — below).

### Paths use gitignore syntax

A **directory** (trailing `/`) is expanded to `dir/**` so it matches
**recursively**; a **file** is emitted verbatim.

| `path` | Emitted for e.g. `Read` |
| --- | --- |
| `eval/` | `Read(eval/**)` |
| `eval.yaml` | `Read(eval.yaml)` |

!!! warning "`dir/` needs the `**` — `dir/*` would only match direct children"
    This is why the compiler appends `**` for you. If you write a pre-formed
    simple pattern by hand, remember `Read(eval/*)` matches only the files
    directly inside `eval/`, not nested paths.

### Why Bash can't be path-scoped

`Bash(...)` matches the **command string**, not a file path — so a path-based
rule can never be expressed as `Bash(some/path)`. The compiler therefore skips
`Bash` entirely.

For **deny** lists this leaves a hole: an agent could still `cat eval/answers.yaml`
via Bash. The compiler closes it with `harden_bash` — when a deny path rule lists
`Bash`, it *also* emits `Read` and `Edit` for that path:

```yaml
permissions:
  deny:
    - { path: "eval/", tools: ["Bash"] }
```

compiles (deny, `harden_bash=True`) to:

```json
["Read(eval/**)", "Edit(eval/**)"]
```

!!! note "`Read`/`Edit` deny covers built-in file tools *and* common bash file commands"
    A `Read`/`Edit` deny blocks the built-in `Read`/`Edit`/`Write` tools **and**
    recognized bash file commands (`cat`, `head`, `tail`, `sed`). It does **not**
    cover arbitrary subprocess reads such as `python3 -c ...` or `awk` — those
    need an OS `sandbox` denyRead, outside this block.

!!! tip "`harden_bash` is deny-only"
    Hardening runs only for **deny** lists. It is deliberately off for `allow`
    lists so listing `Bash` on an allow path never over-grants `Read`/`Edit`.

Results are deduplicated with original order preserved.

### Relative Bash rules get an absolute-workspace twin

Claude Code matches a Bash rule literally up to its first `*`, so a project rule such
as `Bash(python3 scripts/foo.py *)` never matches the
`python3 <workspace>/scripts/foo.py ...` form weaker models emit for the same script,
and in a headless run that denial ends the session. The local claude-code runner knows
the directory the agent runs in when it writes the workspace settings — the isolated
workspace in batch and per-case mode, the project checkout in repo mode — so for every
Bash rule carried over from the project's `.claude/settings.json` (allow and deny) and
every rule in `permissions.allow` (plus the deny list the settings hold by then) whose
first path-looking argument is project-relative, it adds the same rule with that
argument rewritten to the literal `<workspace>/<path>` (a leading `./` dropped, a
trailing `:*` kept, and the real path added when the path it was given is a symlink). An
allow twin is added only when the target exists under the workspace (a project resource
the harness symlinked or copied there) and is never a wildcard: it allows the same
program on the same file and nothing more, so an allow rule that is already absolute,
has no path, globs the path or puts a `*` before it is left alone (a `*` before the
script name would also match `-c '<code>'`). A deny twin is added regardless and keeps
the source rule's wildcards, since it can only remove permission: `Bash(python3
scripts/*)` in the deny list keeps denying the twins of every allowed script. A
workspace path that cannot be spliced into a rule verbatim (whitespace, rule
metacharacters) disables the twins with a warning. This makes
a blanket `Bash(python3 *)` unnecessary for the absolute-path case. Harbor task packages
carry only `eval.yaml`'s own permissions and are not twinned.

## How they compile

The runner ([`agent_eval/agent/claude_code.py`](https://github.com/opendatahub-io/agent-eval-harness/blob/main/agent_eval/agent/claude_code.py))
picks one of two paths depending on whether **any** entry (in `allow` *or*
`deny`) is a dict:

```mermaid
flowchart TD
    A[permissions.allow / deny] --> B{Any entry a<br/>path-based dict?}
    B -->|No| C[Simple only]
    B -->|Yes| D[Path-based present]
    C --> E["CLI flags:<br/>--allowed-tools / --disallowed-tools"]
    D --> F["compile_permission_rules()<br/>(deny: harden_bash=True)"]
    F --> G["merge into .eval-permissions.json<br/>--settings &lt;file&gt;"]
```

=== "Simple only → CLI flags"

    When every entry is a string, the lists are passed straight through as
    comma-separated CLI flags — no file is written:

    ```bash
    claude --print \
      --disallowed-tools "mcp__*" \
      --allowed-tools "Skill,Read"
    ```

=== "Path-based → generated settings file"

    When any entry is a dict, the runner compiles **both** lists, merges them
    into a generated `.eval-permissions.json` (alongside any existing workspace
    `settings.json`, e.g. repo-write protection), and points the CLI at it:

    ```json title=".eval-permissions.json"
    {
      "permissions": {
        "deny": ["Read(eval/**)", "Edit(eval/**)"],
        "allow": ["Edit(artifacts/**)"]
      }
    }
    ```

    ```bash
    claude --print --settings .eval-permissions.json
    ```

!!! note "Where the file is written"
    `.eval-permissions.json` is written next to the case workspace's settings
    file (`case_ws/.claude/`) when one exists, otherwise into the workspace root.
    This avoids mutating the repository when the workspace *is* the repo root
    (`runner.workspace_mode: repo`). When an OpenRouter provider plan is active the
    same file is named `.eval-overlay.json`, is written with mode 0600 and also
    carries the plan's `env` block; it exists whether or not path-based rules do.

The same compiler is used to emit valid rules for both the local runner and the
Harbor task package, so path-based protection behaves identically across
[execution backends](../../concepts/backends.md).

## Common patterns

```yaml
permissions:
  # Block all MCP tools during eval (simple)
  deny:
    - "mcp__*"

  # Keep a prompt-mode / workspace_mode: repo agent out of the answer key
    - { path: "eval/",      tools: ["Read", "Edit", "Bash"] }
    - { path: "eval.yaml",  tools: ["Read"] }
    - { path: "eval.md",    tools: ["Read"] }
    - { path: "tmp/",       tools: ["Read", "Edit"] }
```

!!! tip "Auditing what was actually denied"
    Every tool call denied by these rules is persisted per case in
    `run_result.json` as `permission_denials` (`[{tool_name, tool_use_id,
    tool_input}]` — inside `per_case` entries in case mode, top-level in batch
    mode). Check it after a run to spot over-strict rules (e.g. a nested
    `Skill` call missing from `permissions.allow`) or attempted answer-key
    reads, instead of grepping `stdout.log`. See
    [runs directory](../runs-directory.md).

!!! tip "Answer-key protection is why path rules exist"
    Prompt-mode evals and `workspace_mode: repo` runs execute the agent inside
    the real repository, where the dataset, `eval.yaml`, and reference outputs
    live. Path-based deny rules stop the agent from reading its own answer key.
    See [tool interception](../../concepts/tool-interception.md) and
    [runners](../../concepts/runners.md).

## See also

<div class="grid cards" markdown>

- [**eval.yaml schema**](../eval-yaml.md) — all top-level keys
- [**inputs.tools**](inputs-tools.md) — intercepting tool calls at runtime
- [**runner**](runner.md) — `workspace_mode` and where the agent executes
- [**Backends**](../../concepts/backends.md) — Local, Harbor, EvalHub

</div>
