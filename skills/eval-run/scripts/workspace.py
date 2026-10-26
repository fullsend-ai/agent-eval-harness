#!/usr/bin/env python3
"""Prepare an isolated workspace for skill evaluation.

Reads eval.yaml for dataset path and output directories.
For each case, includes the full input file content in batch.yaml —
no field extraction or schema interpretation.

Usage:
    python3 ${CLAUDE_SKILL_DIR}/scripts/workspace.py \\
        --config eval.yaml \\
        --run-id test-001 \\
        [--cases case-001] \\
        [--symlinks scripts,.claude,CLAUDE.md]

Project resources are linked into each workspace from DEFAULT_SYMLINKS plus
the config's ``execution.symlinks`` entries; ``--symlinks`` replaces both.
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from agent_eval.config import EvalConfig, deep_merge
from agent_eval.tools.interception import extract_tool_patterns
from agent_eval.tools.permissions import compile_permission_rules
from agent_eval.workspace_provisioning import materialize_shared_files
from workspace_files import _copy_input_files

# Resolve git executable to absolute path to prevent PATH hijacking (CWE-426)
# Validated at runtime in main(), not at import time (safe for tests/imports)
GIT_BIN = shutil.which("git")

# Top-level project entries linked into every staged workspace. A config adds
# to this list through ``execution.symlinks``; ``--symlinks`` replaces it.
DEFAULT_SYMLINKS = ["scripts", ".claude", "CLAUDE.md", ".context", "skills"]


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--cases", nargs="*", default=None)
    parser.add_argument(
        "--symlinks",
        default=None,
        help="Comma-separated dirs/files to symlink into workspace; replaces "
        "the defaults (scripts,.claude,CLAUDE.md,.context,skills) AND the "
        "config's execution.symlinks",
    )
    args = parser.parse_args()

    # Validate git is available (required for workspace creation)
    if GIT_BIN is None:
        print("ERROR: git executable not found in PATH", file=sys.stderr)
        sys.exit(1)

    config = EvalConfig.from_yaml(args.config)

    cases_dir = config.resolve_path(config.dataset.path)
    if not cases_dir.exists():
        print(f"ERROR: dataset path not found: {cases_dir}", file=sys.stderr)
        sys.exit(1)

    # Find cases (each subdirectory is a case)
    case_dirs = sorted(d for d in cases_dir.iterdir() if d.is_dir())
    if args.cases:
        filter_set = set(args.cases)
        case_dirs = [c for c in case_dirs if c.name in filter_set]

    if not case_dirs:
        print("ERROR: no cases found", file=sys.stderr)
        sys.exit(1)

    # Validate run-id
    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.run_id):
        print("ERROR: run-id must match [A-Za-z0-9._-]+", file=sys.stderr)
        sys.exit(1)

    # Auto-tag every model request with the run-id for per-run cost attribution.
    # Configs whose execution.env references $EVAL_RUN_HEADER (e.g. eval-openrouter.yaml
    # -> ANTHROPIC_CUSTOM_HEADERS) pick this up via _inject_env; other configs ignore it.
    # Overriding here guarantees the tag always matches --run-id (no manual export).
    os.environ["EVAL_RUN_HEADER"] = f"x-eval-run-id: {args.run_id}"

    # Create workspace in secure temp directory
    base_dir = (Path(tempfile.gettempdir()) / "agent-eval").resolve()
    workspace = (base_dir / args.run_id).resolve()
    if base_dir not in workspace.parents and workspace != base_dir:
        print("ERROR: invalid run-id path", file=sys.stderr)
        sys.exit(1)
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, mode=0o700)

    # Initialize a bare git repo so Claude Code subagents can discover
    # the project root and load .claude/settings.json with the expanded
    # permission patterns (e.g. /private/tmp variants on macOS).
    subprocess.run([GIT_BIN, "init", "-q", str(workspace)], check=True)

    # Branch on execution mode
    # Case mode: one invocation per test case (works for both skill and prompt)
    # Batch mode: one invocation for all cases via batch.yaml
    # For prompt mode (execution.prompt set) with workspace_mode: repo, use in-repo execution
    if config.execution.mode == "case":
        workspace_mode = getattr(config.runner, "workspace_mode", None)
        is_prompt_mode = config.is_prompt_mode()
        if is_prompt_mode and workspace_mode == "repo":
            _create_in_repo_workspace(workspace, case_dirs, config, args)
        else:
            _create_per_case_workspace(workspace, case_dirs, config, args)
        return

    # ── Batch mode (below) ───────────────────────────────────────

    if config.dataset.workspace.files:
        print(
            "WARNING: dataset.workspace.files is ignored in batch mode — "
            "files are per-case but batch mode uses a shared workspace. "
            "Use execution.mode: case instead.",
            file=sys.stderr,
        )

    # Create output directories from config
    for output in config.outputs:
        if output.path and output.path != ".":
            out = workspace / output.path
            # If the path has a file extension (e.g., review-report.html),
            # create the parent directory instead of treating it as a dir.
            if out.suffix:
                out.parent.mkdir(parents=True, exist_ok=True)
            else:
                out.mkdir(parents=True, exist_ok=True)

    # Build batch entries — include full input file content per case
    batch_entries = []
    case_order = []

    for case_dir in case_dirs:
        # Find the input file (first .yaml or .json in the case dir)
        input_content = _read_input(case_dir, config)
        if input_content is None:
            continue

        # Flatten list inputs so batch.yaml is a single flat list
        if isinstance(input_content, list):
            batch_entries.extend(input_content)
            case_order.append(
                {"case_id": case_dir.name, "entry_count": len(input_content)}
            )
        else:
            batch_entries.append(input_content)
            case_order.append({"case_id": case_dir.name, "entry_count": 1})

    # Write batch.yaml
    with open(workspace / "batch.yaml", "w") as f:
        yaml.dump(
            batch_entries, f, default_flow_style=False, allow_unicode=True, width=120
        )

    # Write case order
    with open(workspace / "case_order.yaml", "w") as f:
        yaml.dump(case_order, f, default_flow_style=False)

    # Symlink project resources into workspace. .claude is always skipped —
    # we create our own settings.json (for the SubagentStop hook at minimum,
    # plus tool interception if configured); symlinking it would write into
    # the project's .claude/ directory instead.
    project_root = Path.cwd()
    symlink_names, configured = _resolve_symlink_names(config, args)
    _link_project_entries(workspace, project_root, symlink_names, configured, set())

    # .claude is skipped for hooks, so symlink its subdirectories (skills/, …)
    claude_dir = project_root / ".claude"
    if claude_dir.is_dir():
        for sub in claude_dir.iterdir():
            if sub.is_dir() and sub.name != "settings.json":
                link = workspace / ".claude" / sub.name
                if not link.exists():
                    link.parent.mkdir(parents=True, exist_ok=True)
                    link.symlink_to(sub.resolve())

    # Generate tool interception hooks if inputs.tools configured
    if config.inputs.tools:
        _setup_tool_hooks(workspace, config)
    else:
        # Even without tool interception, set up SubagentStop hook
        # to capture background agent transcripts for tracing.
        _setup_subagent_only_hook(workspace, config)

    print(f"WORKSPACE: {workspace}")
    print(f"CASES: {len(case_dirs)}")
    print(f"BATCH: {workspace / 'batch.yaml'}")


def _create_per_case_workspace(workspace, case_dirs, config, args):
    """Create a separate workspace per case for per-case execution.

    Each case gets its own workspace subdirectory with:
    - All files from the dataset case directory (input.yaml, strategy.md, etc.)
    - Symlinked project resources (scripts, skills, .context, CLAUDE.md)
    - Tool interception hooks and SubagentStop hook
    - Output directories from eval.yaml
    """
    project_root = Path.cwd()
    symlink_names, configured = _resolve_symlink_names(config, args)
    # A configured entry missing from the project warns once, not once per case.
    warned_missing = set()

    case_order = []

    for case_dir in case_dirs:
        case_id = case_dir.name
        case_ws = workspace / "cases" / case_id
        case_ws.mkdir(parents=True, exist_ok=True)
        subprocess.run([GIT_BIN, "init", "-q", str(case_ws)], check=True)

        # Copy only the input file and answers.yaml into the workspace.
        # Companion files (e.g., source code for autofix skills) should
        # use dataset.input_files_dir (see #70) to explicitly declare
        # which files the skill needs. Everything else (gold standards,
        # reference docs, annotations) is evaluation material.
        input_src = _find_input_file(case_dir)
        if input_src:
            shutil.copy2(input_src, case_ws / input_src.name)
        answers_src = case_dir / "answers.yaml"
        if answers_src.is_file():
            shutil.copy2(answers_src, case_ws / "answers.yaml")

        # Copy input files directory if present (e.g., source code for the
        # agent to work on). Contents are placed at the workspace root,
        # preserving relative paths within the directory.
        _copy_input_files(case_dir, case_ws, config)
        # Materialize shared {dest, source} workspace.files entries (e.g. a
        # live SKILL.md in a plugin) as real files in the case workspace.
        materialize_shared_files(case_ws, config)

        # Create output directories
        for output in config.outputs:
            if output.path and output.path != ".":
                out = case_ws / output.path
                if out.suffix:
                    out.parent.mkdir(parents=True, exist_ok=True)
                else:
                    out.mkdir(parents=True, exist_ok=True)

        # Snapshot initial state so collect.py can diff for in-place edits
        _git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "eval-harness",
            "GIT_AUTHOR_EMAIL": "eval@harness",
            "GIT_COMMITTER_NAME": "eval-harness",
            "GIT_COMMITTER_EMAIL": "eval@harness",
        }
        subprocess.run(
            [GIT_BIN, "-C", str(case_ws), "add", "-A"], check=True, capture_output=True
        )
        subprocess.run(
            [
                GIT_BIN,
                "-C",
                str(case_ws),
                "commit",
                "-q",
                "-m",
                "initial",
                "--allow-empty",
            ],
            check=True,
            capture_output=True,
            env=_git_env,
        )

        # Symlink project resources (skip .claude — we create our own)
        _link_project_entries(
            case_ws, project_root, symlink_names, configured, warned_missing
        )

        # Symlink .claude subdirectories (skills/, etc.)
        claude_dir = project_root / ".claude"
        if claude_dir.is_dir():
            for sub in claude_dir.iterdir():
                if sub.is_dir() and sub.name != "settings.json":
                    link = case_ws / ".claude" / sub.name
                    if not link.exists():
                        link.parent.mkdir(parents=True, exist_ok=True)
                        link.symlink_to(sub.resolve())

        # Set up hooks (tool interception + SubagentStop)
        if config.inputs.tools:
            _setup_tool_hooks(case_ws, config)
        else:
            _setup_subagent_only_hook(case_ws, config)

        case_order.append({"case_id": case_id})

    # Write case order at the parent workspace level
    with open(workspace / "case_order.yaml", "w") as f:
        yaml.dump(case_order, f, default_flow_style=False)

    print(f"WORKSPACE: {workspace}")
    print("MODE: case")
    print(f"CASES: {len(case_dirs)}")
    for entry in case_order:
        print(f"  {entry['case_id']}: {workspace / 'cases' / entry['case_id']}")


def _resolve_symlink_names(config, args):
    """The project entries to link into a workspace, and which came from config.

    Defaults first, then ``execution.symlinks`` (deduped, order kept). A
    ``--symlinks`` CLI value replaces both. Returns ``(names, configured)``
    where *configured* holds the non-default names: a default that is absent
    from the project is skipped silently (as it always was), a configured one
    is skipped with a warning.
    """
    if args.symlinks:
        return [s.strip() for s in args.symlinks.split(",") if s.strip()], set()
    extra = list(getattr(config.execution, "symlinks", None) or [])
    names = list(DEFAULT_SYMLINKS)
    for name in extra:
        if name not in names:
            names.append(name)
    return names, set(extra) - set(DEFAULT_SYMLINKS)


def _link_project_entries(dest, project_root, names, configured, warned):
    """Symlink each of *names* from *project_root* into *dest*.

    ``.claude`` is always skipped (the workspace gets its own settings.json).
    An existing link is left alone. A missing entry is skipped — silently for
    a default, with one warning per run (tracked in *warned*) for a configured
    ``execution.symlinks`` entry, so a typo surfaces without failing the run.
    """
    for name in names:
        if name == ".claude":
            continue
        p = Path(name)
        if p.is_absolute() or ".." in p.parts:
            print(f"WARNING: skipping invalid symlink entry: {name}", file=sys.stderr)
            continue
        target = project_root / name
        link = dest / name
        if target.exists():
            if not link.exists():
                link.symlink_to(target.resolve())
        elif name in configured and name not in warned:
            warned.add(name)
            print(
                f"WARNING: execution.symlinks entry {name!r} not found in "
                f"{project_root}; not linked into the workspace",
                file=sys.stderr,
            )


def _create_in_repo_workspace(workspace, case_dirs, config, args):
    """For prompt-mode documentation evals: run agents in the repo itself.

    Agents navigate the real repo structure (ai-docs/, docs/, pkg/, etc.)
    but all I/O (input.yaml, outputs, logs) goes to workspace/cases/case-NNN/.

    This tests: "Can agents use our documentation as deployed in the repo?"

    Safety measures:
    - Write protection via permissions.deny prevents repo modification
    - Git status check after each case verifies repo cleanliness
    - All outputs collected to workspace/cases/case-NNN/, never written to repo
    """
    import json as _json

    project_root = Path.cwd()

    # Snapshot repo state before any agent runs
    repo_snapshot = _snapshot_repo_state(project_root)
    (workspace / "repo_snapshot_before.txt").write_text(repo_snapshot)

    # Create case directories (same structure as regular mode)
    cases_dir = workspace / "cases"
    cases_dir.mkdir(parents=True, exist_ok=True)

    case_order = []

    for case_dir in case_dirs:
        case_id = case_dir.name
        case_ws = cases_dir / case_id
        case_ws.mkdir(parents=True, exist_ok=True)

        # Copy input.yaml to case workspace (reject symlinks to prevent CWE-59)
        input_src = _find_input_file(case_dir)
        if input_src:
            if input_src.is_symlink():
                print(f"ERROR: Refusing to copy symlink: {input_src}", file=sys.stderr)
                sys.exit(1)
            shutil.copy2(input_src, case_ws / input_src.name)

        # Create output directories in case workspace
        for output in config.outputs:
            if output.path and output.path != ".":
                out = case_ws / output.path
                if out.suffix:
                    out.parent.mkdir(parents=True, exist_ok=True)
                else:
                    out.mkdir(parents=True, exist_ok=True)

        # Create .claude/settings.json with write protection
        _create_repo_mode_settings(case_ws, project_root, config)

        # Store metadata marking this as in-repo mode
        case_meta = {
            "case_id": case_id,
            "mode": "in-repo",
            "repo_cwd": str(project_root),  # Agent runs here, not in case_ws
        }
        with open(case_ws / "_metadata.yaml", "w") as f:
            yaml.dump(case_meta, f)

        case_order.append({"case_id": case_id})

    # Write case order at workspace level (same format as regular mode)
    with open(workspace / "case_order.yaml", "w") as f:
        yaml.dump(case_order, f, default_flow_style=False)

    print(f"WORKSPACE: {workspace}")
    print("MODE: in-repo (documentation eval)")
    print(f"REPO: {project_root}")
    print(f"CASES: {len(case_dirs)}")
    for entry in case_order:
        print(f"  {entry['case_id']}: workspace/cases/{entry['case_id']}")


def _snapshot_repo_state(repo_root):
    """Capture git status output as baseline for verification."""
    result = subprocess.run(
        [GIT_BIN, "status", "--porcelain"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True
    )
    return result.stdout


def _create_repo_mode_settings(case_ws, project_root, config):
    """Create settings.json that prevents repo modifications.

    Strategy:
    - Allow Read, Grep, Glob anywhere (agents need to navigate docs)
    - Deny Write, Edit to repo paths (prevent modification)
    - Allow Write, Edit to case workspace (outputs go here)
    - Deny git commands that would modify repo
    """
    import json as _json

    settings = {}

    # Start with project permissions
    # The agent runs in the project checkout (case_ws holds only its I/O), so
    # the absolute twins of the project's relative Bash rules root there.
    _carry_over_permissions(settings, project_root)
    _merge_harness_permissions(settings, config, project_root)

    # Add repo write protection
    perms = settings.setdefault("permissions", {})
    deny_list = perms.setdefault("deny", [])

    # Prevent writes to repo (use string patterns that Claude Code understands)
    repo_str = str(project_root)
    deny_patterns = [
        f"Write({repo_str}/**)",
        f"Edit({repo_str}/**)",
        # Prevent git operations that modify repo
        "Bash(git add*)",
        "Bash(git commit*)",
        "Bash(git push*)",
        "Bash(git checkout*)",
        "Bash(git reset*)",
        "Bash(git restore*)",
        "Bash(git stash*)",
        "Bash(git clean*)",
        "Bash(git apply*)",
        "Bash(git rm*)",
        "Bash(git mv*)",
        # Prevent common file manipulation commands that bypass Write/Edit
        "Bash(echo *>*)",
        "Bash(echo *>>*)",
        "Bash(cp * " + repo_str + "*)",
        "Bash(mv * " + repo_str + "*)",
        "Bash(sed -i*)",
        "Bash(rm *)",
    ]

    for pattern in deny_patterns:
        if pattern not in deny_list:
            deny_list.append(pattern)

    # Allow writes to case workspace
    case_ws_str = str(case_ws)
    allow_list = perms.setdefault("allow", [])
    allow_patterns = [
        f"Write({case_ws_str}/**)",
        f"Edit({case_ws_str}/**)",
    ]

    for pattern in allow_patterns:
        if pattern not in allow_list:
            allow_list.append(pattern)

    # Grant access to project root for reading
    perms.setdefault("additionalDirectories", []).append(repo_str)

    # Add SubagentStop hook to capture transcripts
    from agent_eval.agent.stream_capture import setup_subagent_hook
    subagent_dir = str((case_ws / "subagents").resolve())
    setup_subagent_hook(settings, subagent_dir)

    # Set up tool interception hooks if needed
    if config.inputs.tools:
        _setup_in_repo_tool_hooks(case_ws, config, settings)

    # Carry over the project's own hooks (after the harness's, before env)
    _carry_over_hooks(settings, config)

    # Inject execution.env
    _inject_env(settings, config)

    # Apply user-provided runner.settings
    _apply_runner_settings(settings, config)

    # Write settings.json to case workspace
    settings_dir = case_ws / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    with open(settings_dir / "settings.json", "w") as f:
        _json.dump(settings, f, indent=2)


def _setup_in_repo_tool_hooks(case_ws, config, settings):
    """Set up tool interception hooks for in-repo mode."""
    # Build handler config
    handlers = []
    hook_matchers = set()

    for tool_cfg in config.inputs.tools:
        handler = {"match": tool_cfg.match}
        patterns = extract_tool_patterns(tool_cfg.match)
        handler["patterns"] = patterns
        if tool_cfg.prompt:
            handler["prompt"] = tool_cfg.prompt
        if tool_cfg.prompt_file:
            handler["prompt_file"] = tool_cfg.prompt_file
        handlers.append(handler)
        hook_matchers.update(patterns)

    # Write tool_handlers.yaml to case workspace
    handler_data = {"handlers": handlers}
    from agent_eval.providers.openrouter.plan import hook_model_for

    hook_model = hook_model_for(config)
    if hook_model:
        handler_data["hook_model"] = hook_model
    with open(case_ws / "tool_handlers.yaml", "w") as f:
        yaml.dump(handler_data, f, default_flow_style=False)

    # Copy interceptor script to case workspace
    hooks_dir = case_ws / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    interceptor_src = Path(__file__).parent / "tools.py"
    if interceptor_src.exists():
        shutil.copy2(interceptor_src, hooks_dir / "tools.py")

    # Add PreToolUse hooks to settings
    settings.setdefault("hooks", {})["PreToolUse"] = []
    for matcher in sorted(hook_matchers):
        settings["hooks"]["PreToolUse"].append({
            "matcher": matcher,
            "hooks": [{
                "type": "command",
                "command": f"python3 {case_ws}/hooks/tools.py",
            }],
        })

    print(f"HOOKS: {len(hook_matchers)} tool interceptors configured (in-repo mode)")


def _find_input_file(case_dir):
    """Find the input file in a case directory. Returns Path or None."""
    for suffix in (".yaml", ".yml", ".json"):
        candidate = case_dir / f"input{suffix}"
        if candidate.is_file():
            return candidate
    return None


def _read_input(case_dir, config=None):
    """Read the input file from a case directory.

    Returns the parsed content (dict) or None if no input file found.
    Prefers files named 'input.*', then falls back to first parseable file.
    Skips known non-input files like answers.yaml and reference.*.
    When *config* is provided, workspace file roots are also skipped.
    """
    _SKIP_NAMES = {"answers", "reference", "expected", "gold"}
    if config is not None:
        ws = getattr(getattr(config, "dataset", None), "workspace", None)
        _SKIP_NAMES = _SKIP_NAMES | {
            Path(f).parts[0] for f in (getattr(ws, "files", None) or [])
        }

    # First pass: look for a file named 'input.*'
    for suffix in (".yaml", ".yml", ".json"):
        candidate = case_dir / f"input{suffix}"
        if candidate.is_file():
            data = _parse_file(candidate)
            if data is not None:
                return data

    # Second pass: first parseable data file, skipping known non-inputs
    for name in sorted(case_dir.iterdir()):
        if not name.is_file() or name.stem in _SKIP_NAMES:
            continue
        if name.suffix in (".yaml", ".yml", ".json"):
            data = _parse_file(name)
            if data is not None:
                return data
    return None


def _parse_file(path):
    """Parse a YAML or JSON file, returning the data or None on error."""
    try:
        if path.suffix in (".yaml", ".yml"):
            with open(path) as f:
                return yaml.safe_load(f)
        elif path.suffix == ".json":
            import json

            with open(path) as f:
                return json.load(f)
    except Exception as e:
        print(f"WARNING: failed to parse {path}: {e}", file=sys.stderr)
    return None


def _expand_symlink_permissions(allow_list):
    """Add resolved-path variants for permission patterns with symlinked dirs.

    On macOS, /tmp is a symlink to /private/tmp.  Claude Code resolves file
    paths to their canonical form before matching permission patterns, so
    ``Write(/tmp/rfe-assess/**)`` won't match a write to the real path
    ``/private/tmp/rfe-assess/...``.  This function detects such cases and
    adds the resolved variant alongside the original.
    """
    extras = []
    for pattern in allow_list:
        m = re.match(r"(Write|Edit|Bash)\((.+)\)", pattern)
        if not m:
            continue
        tool, glob_path = m.groups()
        # Extract the directory prefix (everything before the first glob char)
        prefix = re.split(r"[*?]", glob_path, maxsplit=1)[0].rstrip("/")
        if not prefix or not prefix.startswith("/"):
            continue
        resolved = str(Path(prefix).resolve())
        if resolved != prefix:
            resolved_pattern = f"{tool}({glob_path.replace(prefix, resolved)})"
            if resolved_pattern not in allow_list:
                extras.append(resolved_pattern)
    return allow_list + extras


_BASH_RULE = re.compile(r"^Bash\((.+)\)$")
# A workspace path that could not be spliced into a rule verbatim: whitespace
# splits the rule's tokens, the others are rule or shell metacharacters.
_UNSAFE_BASE = re.compile(r"""[\s*?\[\]'"$`]""")


def _rule_path_token(tokens, index):
    """``(path, suffix)`` for a Bash rule token: the ``:*`` suffix Claude Code
    treats as a trailing `` *`` (recognized at the end of a rule only) is split
    off so the path can be inspected and rebuilt with it."""
    token = tokens[index]
    if index == len(tokens) - 1 and token.endswith(":*"):
        return token[:-2], ":*"
    return token, ""


def _relative_path_index(tokens, *, allow_glob=False):
    """Index of the first token of a Bash rule that names a project-relative
    path (``scripts/foo.py``), or ``None`` when no token does.

    For an allow rule (``allow_glob=False``) a wildcard anywhere up to and
    including the path aborts: a ``*`` before the path would also match
    ``-c '<code>'`` and a globbed path is not one file, so such a rule is
    never widened.  For a deny rule (``allow_glob=True``) wildcards are kept
    as they are — a deny twin can only remove permission, and a relative deny
    such as ``Bash(python3 scripts/*)`` must keep covering the absolute twins
    of the allow rules it narrows."""
    for index in range(len(tokens)):
        path, _suffix = _rule_path_token(tokens, index)
        if not allow_glob and ("*" in path or "?" in path or "[" in path):
            return None
        if ("/" in path and not path.startswith(("/", "-", "~", "$", "'", '"'))
                and ".." not in path.split("/")):
            return index
    return None


def _expand_workspace_bash_permissions(rules, workspace, *, require_exists=True):
    """Add an absolute-workspace twin for every relative-path Bash rule.

    Claude Code matches a Bash rule literally up to its first ``*``, so a
    project rule such as ``Bash(python3 scripts/foo.py *)`` never matches the
    ``python3 <workspace>/scripts/foo.py ...`` form weaker models emit for the
    same script — and in headless mode that denial ends the run.  The
    directory the agent runs in is known here, so for each rule whose first
    path-looking argument is project-relative the same rule is added with that
    argument rewritten to the literal ``<workspace>/<arg>`` (the ``./`` prefix
    dropped, everything else verbatim, a trailing ``:*`` kept) and, when the
    given path is itself a symlink, its real path.  Never a wildcard: the twin
    allows — or denies — the same program on the same file and nothing more.
    Allow rules that are already absolute, carry no path, glob the path or
    put a ``*`` before it are left alone.

    ``require_exists`` (allow lists) adds a twin only when the target exists
    under *workspace* — a project resource the harness symlinked or copied
    there.  Deny lists pass ``False``: a deny twin can only remove permission,
    it must exist whenever the matching allow twin does, and it keeps the
    source rule's wildcards (``Bash(python3 scripts/*)`` denies the twin of
    every allowed script), so the target need not exist either.
    """
    if workspace is None:
        return rules
    ws = Path(workspace)
    bases = [ws]
    real = Path(os.path.realpath(ws))
    if real != ws:
        bases.append(real)
    unsafe = [b for b in bases if _UNSAFE_BASE.search(str(b))]
    if unsafe:
        print(f"WARNING: not adding absolute-path permission twins: workspace path "
              f"{str(unsafe[0])!r} cannot be spliced into a rule verbatim", file=sys.stderr)
        return rules
    extras = []
    for pattern in rules:
        m = _BASH_RULE.match(pattern) if isinstance(pattern, str) else None
        if not m:
            continue
        tokens = m.group(1).split(" ")
        index = _relative_path_index(tokens, allow_glob=not require_exists)
        if index is None:
            continue
        path, suffix = _rule_path_token(tokens, index)
        if require_exists and not (ws / path).exists():
            continue
        verbatim = path[2:] if path.startswith("./") else path
        for base in bases:
            twin_tokens = list(tokens)
            twin_tokens[index] = f"{base}/{verbatim}{suffix}"
            twin = f"Bash({' '.join(twin_tokens)})"
            if twin not in rules and twin not in extras:
                extras.append(twin)
    return rules + extras


def _inject_env(settings, config):
    """Inject execution.env into settings.json env block.

    Values starting with ``$`` are resolved from ``os.environ``.
    Missing env vars are silently omitted.  Literal values pass through.
    """
    if not config.execution.env:
        return
    env_block = settings.setdefault("env", {})
    for key, value in config.execution.env.items():
        if value is None:
            # A YAML null is "unset", not the string "None" (parity with the
            # runner env, interception and Harbor carriers).
            continue
        if isinstance(value, str) and value.startswith("$"):
            resolved = os.environ.get(value[1:])
            if resolved is not None:
                env_block[key] = resolved
        else:
            env_block[key] = str(value)


def _carry_over_permissions(settings, workspace=None):
    """Copy project permissions (allow, deny, additionalDirectories) into settings.

    With *workspace* (the directory the agent runs in), every relative-path
    Bash rule — allow and deny — also gets its absolute twin
    (:func:`_expand_workspace_bash_permissions`)."""
    import json as _json

    project_settings = Path.cwd() / ".claude" / "settings.json"
    if not project_settings.exists():
        return
    try:
        with open(project_settings) as f:
            proj = _json.load(f)
    except (_json.JSONDecodeError, OSError):
        return

    proj_perms = proj.get("permissions", {})
    if proj_perms.get("allow"):
        allow_list = _expand_symlink_permissions(list(proj_perms["allow"]))
        allow_list = _expand_workspace_bash_permissions(allow_list, workspace)
        settings.setdefault("permissions", {})["allow"] = allow_list
    if proj_perms.get("deny"):
        # Add to whatever deny list the settings hold (eval.yaml's, written by
        # the interception generator) — a deny is never dropped by another.
        existing_deny = settings.setdefault("permissions", {}).setdefault("deny", [])
        for rule in _expand_workspace_bash_permissions(
                _expand_symlink_permissions(list(proj_perms["deny"])),
                workspace, require_exists=False):
            if rule not in existing_deny:
                existing_deny.append(rule)
    if proj_perms.get("additionalDirectories"):
        dirs = list(proj_perms["additionalDirectories"])
        for d in list(dirs):
            resolved = str(Path(d).resolve())
            if resolved != d and resolved not in dirs:
                dirs.append(resolved)
        settings.setdefault("permissions", {}).setdefault(
            "additionalDirectories", []
        ).extend(dirs)


def _carry_over_hooks(settings, config):
    """Append the project's settings hooks to the run settings.

    ``.claude/settings.json`` in the project (the same file the permission
    carry-over reads) may declare ``hooks`` the skill under test relies on:
    a ``SessionStart``/``compact`` recovery banner, a ``Stop`` guard.  Left
    out of the workspace settings, the skill runs under eval without the
    machinery it has in production, and the eval measures something else.

    Each event's handler groups are deep-copied and **appended** after the
    harness's own hooks (``SubagentStop`` capture, ``PreToolUse``
    interception), so those keep running first; ``runner.settings`` is
    merged later and still wins.  Hook commands run with the session cwd —
    the case workspace — exactly as in the checkout, so a relative command
    needs the file it names to be linked or copied into the workspace.
    Disabled with ``execution.project_hooks: false``.  A malformed ``hooks``
    block (or a malformed event/group inside it) is skipped, never raised.
    """
    import copy
    import json as _json

    if not getattr(config.execution, "project_hooks", True):
        return
    project_settings = Path.cwd() / ".claude" / "settings.json"
    if not project_settings.exists():
        return
    try:
        with open(project_settings) as f:
            proj = _json.load(f)
    except (_json.JSONDecodeError, OSError):
        return
    proj_hooks = proj.get("hooks") if isinstance(proj, dict) else None
    if not isinstance(proj_hooks, dict):
        return

    hooks = settings.setdefault("hooks", {})
    for event, groups in proj_hooks.items():
        if not isinstance(groups, list):
            continue
        # A group is a mapping whose ``hooks`` member is the handler list;
        # anything else would serialize into settings the CLI rejects.
        carried = [copy.deepcopy(g) for g in groups
                   if isinstance(g, dict) and isinstance(g.get("hooks"), list)]
        if carried:
            hooks.setdefault(event, []).extend(carried)

def _merge_harness_permissions(settings, config, workspace=None):
    """Merge eval.yaml ``permissions`` into the workspace settings.

    The runner also passes these lists on the command line (or, for
    path-based rules, in its ``--settings`` overlay); holding them in the
    workspace settings as well gives one file to inspect — with or without
    tool interception, whose generator writes both lists only when
    ``inputs.tools`` is set — and the place where relative-path Bash rules get
    their absolute twins.  Both lists go through the same compiler as the
    Harbor task packages (:func:`compile_permission_rules`: path-based rules
    become Claude Code patterns, Bash hardening on deny) and are merged with
    dedupe into whatever the settings already hold (the generator's, the
    project's).  Deny gets every variant allow gets — the resolved-path form
    for a symlinked absolute prefix and, with *workspace*, the absolute Bash
    twins (:func:`_expand_workspace_bash_permissions`) — so a deny keeps
    covering what the widened allow covers.
    """
    cfg_perms = (config.permissions or {}) if hasattr(config, "permissions") else {}
    perms = settings.setdefault("permissions", {})
    deny = compile_permission_rules(cfg_perms.get("deny"), harden_bash=True)
    if deny or perms.get("deny"):
        merged_deny = list(perms.get("deny") or [])
        for rule in deny:
            if rule not in merged_deny:
                merged_deny.append(rule)
        # A deny gets every variant an allow would: the resolved-path form for a
        # symlinked absolute prefix (/tmp -> /private/tmp), then the Bash twins.
        perms["deny"] = _expand_workspace_bash_permissions(
            _expand_symlink_permissions(merged_deny), workspace, require_exists=False)
    allow = compile_permission_rules(cfg_perms.get("allow"))
    if not allow:
        return
    harness_allow = _expand_workspace_bash_permissions(
        _expand_symlink_permissions(allow), workspace)
    existing = perms.setdefault("allow", [])
    for pattern in harness_allow:
        if pattern not in existing:
            existing.append(pattern)


def _deep_merge(dst, src):
    """Recursively merge src into dst. Lists are extended, dicts merged.

    One implementation shared with the `extends:` config overlay
    (`agent_eval.config.deep_merge`); `runner.settings` keeps the plain-extend
    list policy (`dedupe=False`), byte-identical to the historical behaviour.
    """
    return deep_merge(dst, src, dedupe=False)


def _apply_runner_settings(settings, config):
    """Merge eval.yaml `runner.settings` into the workspace settings dict.

    Lets users add Claude Code settings (model defaults, env, MCP servers,
    etc.) to a runner without forking the harness. Merged after harness
    defaults so user overrides win for scalar keys; lists are extended.

    Plugin hermeticity: in an isolated workspace (workspace_mode is not
    "repo") the operator's user-installed plugins are disabled by DEFAULT —
    the harness synthesizes `enabledPlugins: {id: false}` for every entry of
    the installed-plugin registry (see agent_eval.tools.hermetic) and merges
    it first, so explicit `runner.settings.enabledPlugins` entries win.
    Isolation is the workspace's contract; the operator's personal plugins
    loading into case sessions is contamination (issue #193). repo mode runs
    in the user's real environment, so nothing is disabled there.

    The `enabledPlugins` pseudo-entry `"*"` makes the policy explicit:
    `"*": false` forces hermeticity (even in repo mode), `"*": true` opts
    out. It is harness-interpreted — upstream has no wildcard — and is
    stripped here so the CLI never sees a key it does not understand.
    """
    user_settings = dict(getattr(config.runner, "settings", None) or {})
    enabled_plugins = user_settings.get("enabledPlugins")
    wildcard = None
    if isinstance(enabled_plugins, dict) and "*" in enabled_plugins:
        enabled_plugins = dict(enabled_plugins)
        wildcard = enabled_plugins.pop("*")
        user_settings["enabledPlugins"] = enabled_plugins
    isolated = getattr(config.runner, "workspace_mode", None) != "repo"
    if wildcard is False or (wildcard is None and isolated):
        from agent_eval.tools.hermetic import hermetic_enabled_plugins

        denylist = hermetic_enabled_plugins()
        if denylist:
            _deep_merge(settings, {"enabledPlugins": denylist})
    if user_settings:
        _deep_merge(settings, user_settings)


def _setup_subagent_only_hook(workspace, config):
    """Set up SubagentStop hook without tool interception.

    When there are no inputs.tools, we still need the SubagentStop hook
    to capture background agent transcripts for tracing. This creates
    a minimal .claude/settings.json with the hook, the project's
    permissions and the project's own hooks.
    """
    import json as _json

    settings_dir = workspace / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)

    settings = {}

    # Carry over project permissions (allow, deny, additionalDirectories)
    _carry_over_permissions(settings, workspace)
    _merge_harness_permissions(settings, config, workspace)

    # Grant project root access
    project_root = str(Path.cwd().resolve())
    settings.setdefault("permissions", {}).setdefault(
        "additionalDirectories", []
    ).append(project_root)

    # Add SubagentStop hook
    from agent_eval.agent.stream_capture import setup_subagent_hook

    subagent_dir = str((workspace / "subagents").resolve())
    setup_subagent_hook(settings, subagent_dir)

    # Carry over the project's own hooks (after the harness's, before env)
    _carry_over_hooks(settings, config)

    # Inject execution.env into settings
    _inject_env(settings, config)

    # Apply user-provided runner.settings last so they can override defaults
    _apply_runner_settings(settings, config)

    with open(settings_dir / "settings.json", "w") as f:
        _json.dump(settings, f, indent=2)

    print("HOOKS: SubagentStop configured (subagent capture)")


def _setup_tool_hooks(workspace, config):
    """Generate settings.json and tool_handlers.yaml for tool interception.

    Delegates the core interception artifacts (handlers, hooks, interceptor
    script) to :func:`agent_eval.tools.interception.generate_interception`,
    then layers workspace-specific settings on top (project permissions,
    subagent capture, project hooks, execution env, runner settings).
    """
    import json as _json
    from agent_eval.tools.interception import generate_interception

    hooks_command = f"python3 '{workspace}/hooks/tools.py'"
    resolved = config.config_dir / "tool_handlers.yaml" if config.config_dir else None
    hook_matchers = generate_interception(
        workspace, config, hooks_command,
        resolved_handlers_path=resolved if resolved and resolved.is_file() else None)

    # The shared module wrote .claude/settings.json with hooks + eval.yaml
    # permissions + execution.env. Now layer workspace-specific settings.
    settings_path = workspace / ".claude" / "settings.json"
    settings = {}
    if settings_path.exists():
        try:
            settings = _json.loads(settings_path.read_text())
        except (_json.JSONDecodeError, OSError):
            pass

    # Carry over project permissions (allow, deny, additionalDirectories)
    _carry_over_permissions(settings, workspace)
    _merge_harness_permissions(settings, config, workspace)

    # Grant access to the project root so symlinked resources can be read.
    project_root = str(Path.cwd().resolve())
    settings.setdefault("permissions", {}).setdefault(
        "additionalDirectories", []
    ).append(project_root)

    # Add SubagentStop hook to capture background agent transcripts.
    from agent_eval.agent.stream_capture import setup_subagent_hook
    subagent_dir = str((workspace / "subagents").resolve())
    setup_subagent_hook(settings, subagent_dir)

    # Carry over the project's own hooks (after the harness's, before env)
    _carry_over_hooks(settings, config)

    # Inject execution.env into settings
    _inject_env(settings, config)

    # Apply user-provided runner.settings last so they can override defaults
    _apply_runner_settings(settings, config)

    with open(settings_path, "w") as f:
        _json.dump(settings, f, indent=2)

    print(f"HOOKS: {len(hook_matchers)} tool interceptors configured")


if __name__ == "__main__":
    main()
