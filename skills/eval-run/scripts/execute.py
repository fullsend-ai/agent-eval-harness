#!/usr/bin/env python3
"""Execute a skill headlessly via the configured agent runner.

Delegates to the agent_eval.agent abstraction so the same script works
with Claude Code, OpenCode, Agent SDK, or any other registered runner.

Usage:
    python3 ${CLAUDE_SKILL_DIR}/scripts/execute.py \\
        --workspace /tmp/agent-eval/test-001 \\
        --skill rfe.speedrun \\
        --skill-args "--input batch.yaml --headless --dry-run" \\
        --model opus \\
        --output eval/runs/test-001 \\
        [--agent claude-code] \\
        [--subagent-model sonnet] \\
        [--max-budget 100] \\
        [--timeout 3600] \\
        [--mlflow-experiment my-eval]
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import argparse
import atexit
import copy
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ── Stop-guard pidfile ───────────────────────────────────────────────────
# Defined and invoked *before* the heavy agent_eval imports below, and before
# argparse/config load, because the window this closes is a race: the
# orchestrator can launch us with run_in_background and end its turn
# immediately, and the Stop guard hook can only block that teardown once
# execute.pid exists. Every millisecond before the write is a millisecond the
# guard is blind — and module import is the dominant cost (~25 ms warm, far
# more on a cold container), so waiting until main() would leave the guard
# weakest at exactly the moment it is needed most.

_PIDFILE_WRITTEN = False


def _process_create_time(pid):
    """Best-effort, stable per-process creation marker used to detect pid reuse.

    Returns a string identity for the process currently at ``pid`` (Linux boot
    tick count, or the macOS start timestamp), or ``None`` if it cannot be
    determined. Must match the Stop guard hook's derivation
    (``scripts/eval_stop_guard.py``) for the reuse check to work, so the two
    implementations are kept identical.

    The macOS ``ps`` env is pinned because ``lstart`` renders a *formatted
    local* timestamp: the same pid yields "Fri Aug 21 12:34:22 2026" plainly,
    "…16:34:22…" under ``TZ=UTC`` and "ven. 21 août…" under a French locale.
    This process records the marker under the launching shell's env while the
    hook re-derives it under Claude Code's, so any ``TZ``/``LC_TIME`` export
    would make every live run look pid-reused and silently disable the guard.
    """
    try:
        with open("/proc/%d/stat" % pid, encoding="utf-8") as fh:
            data = fh.read()
        # comm (field 2) is parenthesised and may itself contain spaces/parens;
        # starttime is field 22 -> index 19 after the final ')'.
        after = data[data.rfind(")") + 2:].split()
        return after[19]
    except (OSError, IndexError, ValueError):
        pass
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5,
                env={"TZ": "UTC", "LC_ALL": "C", "PATH": "/bin:/usr/bin"},
            )
            return out.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def _write_pidfile(output_dir):
    """Write ``<output_dir>/execute.pid`` so the Stop guard hook can tell this
    executor is still alive.

    The hook blocks the orchestrator from ending its turn (which would tear down
    the session and kill this process before ``run_result.json`` is written)
    while an ``execute.pid`` names a live process with no result beside it. The
    file records the pid plus a process-creation marker so a reused pid can't be
    mistaken for this run, and is removed at exit.

    Idempotent: only the first call writes, so the early call below and the
    fallback in ``main()`` never double-register the cleanup.
    """
    global _PIDFILE_WRITTEN
    if _PIDFILE_WRITTEN:
        return
    pid = os.getpid()
    pidfile = Path(output_dir) / "execute.pid"
    try:
        # O_NOFOLLOW: the guard reads this path with no-follow semantics, so the
        # writer must match. Otherwise a symlink pre-planted at execute.pid by
        # anyone with write access to the runs tree would be followed, letting
        # this process truncate an arbitrary file it can write (CWE-59).
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(pidfile, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"pid": pid, "create_time": _process_create_time(pid)}, fh)
    except OSError:
        return  # non-fatal: guard just won't see this run
    _PIDFILE_WRITTEN = True

    def _remove():
        # Only reap our own pidfile. Run dirs get reused, so a straggler from a
        # previous attempt dying late must not delete the live pidfile of the
        # run that replaced it — that would blind the guard to an active run.
        try:
            with open(pidfile, encoding="utf-8") as fh:
                if json.load(fh).get("pid") != pid:
                    return
        except (OSError, ValueError):
            return
        try:
            os.remove(pidfile)
        except OSError:
            pass
    atexit.register(_remove)


def _early_pidfile(argv):
    """Write the pidfile straight from ``argv``, before anything else runs.

    ``--output`` is scraped by hand rather than via argparse so the write does
    not wait on the parser, the config load, or the imports below. Best effort:
    on any miss ``main()`` still calls ``_write_pidfile`` once ``args.output``
    is parsed, so the guard degrades to a slightly later write, never to none.
    """
    out = None
    for i, token in enumerate(argv):
        if token == "--output" and i + 1 < len(argv):
            out = argv[i + 1]
            break
        if token.startswith("--output="):
            out = token.split("=", 1)[1]
            break
    if not out:
        return
    try:
        directory = Path(out)
        directory.mkdir(parents=True, exist_ok=True)
        _write_pidfile(directory)
        _warn_if_unscannable(directory)
    except OSError:
        pass


def _warn_if_unscannable(output_dir):
    """Warn when the Stop guard will not find this run's pidfile.

    The hook scans ``$AGENT_EVAL_RUNS_DIR`` (read from *its own* env, so an
    inline ``AGENT_EVAL_RUNS_DIR=… python3 execute.py`` never reaches it) or
    ``<cwd>/eval/runs``. An ``--output`` outside both — or a bounded-glob depth
    of more than three below the root — leaves the run silently unguarded. Say
    so once, rather than letting the protection be absent without a trace.

    False-negative: the roots are computed from *this process's* env and cwd,
    which is exactly what diverges from the hook's view when the two disagree
    (e.g. ``cd subproj && execute.py``). Nothing better is computable here —
    execute.py cannot know the session's cwd.
    """
    try:
        target = output_dir.resolve()
        roots = []
        env_root = os.environ.get("AGENT_EVAL_RUNS_DIR")
        if env_root:
            roots.append(Path(env_root).resolve())
        roots.append((Path.cwd() / "eval" / "runs").resolve())
        for root in roots:
            if target == root or root in target.parents:
                if len(target.relative_to(root).parts) <= 3:
                    return
        print(
            f"WARNING: {target} is not under a directory the eval Stop guard "
            f"scans ({', '.join(str(r) for r in roots)}), so it will not "
            f"protect this run from being killed mid-flight. Set "
            f"AGENT_EVAL_RUNS_DIR in the environment the agent itself runs in, "
            f"or place --output under one of those roots.",
            file=sys.stderr,
        )
    except (OSError, ValueError):
        pass


# Guarded so importing execute.py (tests do) never touches the filesystem.
if __name__ == "__main__":
    _early_pidfile(sys.argv[1:])

from agent_eval.agent import RUNNERS  # noqa: E402 — after the early pidfile write
from agent_eval.providers.reconcile import (  # noqa: E402
    aggregate_case_costs,
    write_run_result as _reconciled_write,
)
from agent_eval.hooks import (  # noqa: E402 — after the early pidfile write
    HookError, build_hook_env, collect_hook_outputs,
    run_hooks, run_hooks_safe, save_hook_data,
)

GIT_BIN = shutil.which("git")

_HARNESS_SYSTEM_PROMPT = (
    "You are running inside an evaluation harness. Tool interception hooks "
    "and permission controls are in place for safety. You MUST NOT: "
    "rename, copy, or move scripts to bypass tool filters; "
    "modify .claude/settings.json or hook scripts; "
    "disable or work around any harness controls; "
    "use alternative paths or commands to avoid blocked operations. "
    "If a tool call is blocked, report it and continue with the remaining "
    "pipeline steps. Do not attempt workarounds."
)


class _Tee:
    """Mirror writes to a real stream and a log file, flushing both.

    Installed on ``sys.stdout``/``sys.stderr`` so execute.py populates the
    background-command output stream (the real stdout/stderr that Claude
    Code's viewer displays) AND a stable ``<output_dir>/console.log`` for
    tailing. This removes any reason to redirect with ``>`` (which would
    divert the real stream and leave the background viewer blank).
    """

    def __init__(self, stream, logfile):
        self._stream = stream
        self._logfile = logfile

    def write(self, data):
        n = self._stream.write(data)
        try:
            self._stream.flush()
        except Exception:
            pass
        try:
            self._logfile.write(data)
            self._logfile.flush()
        except Exception:
            pass  # logging is best-effort; never break the run
        return n

    def flush(self):
        for s in (self._stream, self._logfile):
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self):
        try:
            return self._stream.isatty()
        except Exception:
            return False

    def fileno(self):
        return self._stream.fileno()

    def __getattr__(self, name):
        # Delegate everything else (encoding, writable, buffer, …) to the
        # real stream so the Tee is a faithful stand-in.
        return getattr(self._stream, name)


def _fd_path(fd):
    """Best-effort absolute path backing a file descriptor, or None."""
    try:
        if sys.platform == "darwin":
            import fcntl
            F_GETPATH = 50  # macOS fcntl command: resolve an fd to its path
            raw = fcntl.fcntl(fd, F_GETPATH, b"\0" * 1024)
            return os.fsdecode(raw.split(b"\0", 1)[0])
        return os.readlink(f"/proc/self/fd/{fd}")
    except (OSError, ValueError, ImportError):
        return None


def _setup_console_log(output_dir):
    """Mirror console output to ``<output_dir>/console.log`` and warn on a
    stdout redirect into the run directory.

    Claude Code's background-command viewer displays the launched process's
    own stdout/stderr stream. Redirecting that stream to a file
    (``python3 execute.py … > run/execute.log 2>&1``) leaves the viewer
    blank. Teeing here gives a stable, tailable log without any redirect;
    the guard flags the specific footgun of redirecting into the run dir
    (the harness's own capture file lives in a separate temp tasks dir, so
    this never false-fires on a correct background launch).

    Returns the console.log Path, or None if it could not be opened.
    """
    console_log = output_dir / "console.log"
    try:
        fh = open(console_log, "w", buffering=1, encoding="utf-8", errors="replace")
    except OSError:
        return None
    sys.stdout = _Tee(sys.stdout, fh)
    sys.stderr = _Tee(sys.stderr, fh)
    import atexit
    atexit.register(fh.flush)

    fd1 = _fd_path(1)
    if fd1:
        try:
            p = Path(fd1).resolve()
            out = output_dir.resolve()
            if out == p or out in p.parents:
                print(
                    f"WARNING: execute.py stdout is redirected to {p}, inside the run "
                    f"directory. Claude Code's background-command viewer shows the "
                    f"process's own stdout stream and will appear BLANK. Re-launch "
                    f"WITHOUT a '>' redirect (see eval-run SKILL.md Step 4); the live "
                    f"console is already mirrored to {console_log}.",
                    file=sys.stderr,
                )
        except OSError:
            pass
    return console_log


def _resolve_permissions(config):
    """Return the permission rules to pass to the runner.

    User-authored ``permissions`` (allow AND deny) are honored in every
    execution mode. Deny rules meant to protect an in-repo checkout simply
    do not match isolated /tmp workspaces, so there is no need to strip them
    — silently dropping deny rules disabled protections the eval author had
    explicitly configured (e.g. ``deny: ["WebFetch"]`` in batch/case mode).

    Args:
        config: EvalConfig with permissions

    Returns:
        Permissions dict safe to pass to the runner (a shallow copy)
    """
    filtered = dict(config.permissions) if config.permissions else {}

    return filtered


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--skill", default=None,
                        help="Skill name for case/batch mode (required for case/batch, omit for prompt mode)")
    parser.add_argument("--skill-args", default=None,
                        help="Skill arguments (default: from eval.yaml execution.arguments)")
    parser.add_argument("--model", default=None,
                        help="Skill model (default: from eval.yaml models.skill)")
    parser.add_argument("--output", required=True)
    parser.add_argument("--config", required=True,
                        help="Path to eval.yaml")
    parser.add_argument("--agent", default=None,
                        help="Agent runner override (default: from runner.type)")
    parser.add_argument("--subagent-model", default=None)
    parser.add_argument("--max-budget", type=float, default=None)
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--mlflow-experiment", default=None)
    parser.add_argument("--effort", default=None,
                        choices=["minimal", "low", "medium", "high", "xhigh", "max"],
                        help="Agent reasoning effort (validated for the selected runner; "
                             "default: eval.yaml runner.effort)")
    parser.add_argument("--parallelism", type=int, default=None,
                        help="Max parallel case executions (default: from eval.yaml or sequential)")
    parser.add_argument("--run-id", default=None,
                        help="Run identifier (for hook env vars and log paths)")
    parser.add_argument("--strict-cost", action="store_true",
                        help="Exit 2 when a provider plan is active and the run's cost "
                             "could not be reconciled (cost_source unavailable) or the "
                             "run budget was exceeded post hoc")
    parser.add_argument("--strict-routing", action="store_true",
                        help="Exit 2 when the routing audit found violations or is incomplete")
    parser.add_argument("--allow-estimate", action="store_true",
                        help="Under a provider plan, write cost_source runner:estimate instead "
                             "of unavailable when no truth source landed (offline development only)")
    parser.add_argument("--input-override", action="append", default=None,
                        metavar="KEY=VALUE",
                        help="Merge KEY=VALUE into every case's input.yaml before "
                             "execution (repeatable). Lets a caller inject run-level "
                             "values — e.g. matrix factor levels — so they resolve as "
                             "{KEY} in a cli runner command or {{ input.KEY }} in "
                             "arguments. Case fields win only if not overridden.")
    args = parser.parse_args()
    _RECONCILE_OPTS.update(strict_cost=args.strict_cost, strict_routing=args.strict_routing,
                           allow_estimate=args.allow_estimate)

    from agent_eval.config import EvalConfig
    config = EvalConfig.from_yaml(args.config)

    # Determine if prompt mode (execution.prompt is set)
    is_prompt_mode = config.is_prompt_mode()
    # Multi-step: execution.steps drives per-step targets; no single skill.
    is_multi_step = bool(config.execution.steps)

    # Resolve target (skill name or None for prompt/multi-step mode)
    # Priority: CLI --skill > config (execution.skill → top-level skill)
    target = args.skill or config.resolve_skill()

    # For prompt or multi-step mode, force target=None (the executor resolves
    # per-step targets); otherwise a skill is required.
    if is_prompt_mode or is_multi_step:
        target = None
    elif not target:
        # Not prompt mode and no skill specified
        print("ERROR: skill required when execution.prompt is not set. "
              "Set --skill, execution.skill, or execution.prompt in eval.yaml.",
              file=sys.stderr)
        sys.exit(1)

    # Resolve model: CLI > config; required to be set somewhere.
    model = args.model or config.models.skill
    if not model:
        print("ERROR: no model specified. Set --model or models.skill in eval.yaml.",
              file=sys.stderr)
        sys.exit(1)
    subagent_model = args.subagent_model or config.models.subagent or model

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Mirror all console output to <output_dir>/console.log so callers never
    # need to redirect stdout with '>' (which blanks the background-command
    # viewer). The real stdout/stderr streams still receive everything.
    _setup_console_log(output_dir)

    # Fallback for the Stop-guard pidfile: normally already written by
    # _early_pidfile() before the imports at the top of this module. No-op
    # unless that scrape missed (e.g. an --output form it didn't recognise).
    _write_pidfile(output_dir)

    # Resolve skill args: CLI override > config > empty
    # Treat empty/whitespace-only strings as unset (normalize before fallback)
    # For prompt mode, use execution.prompt; for skill mode, use execution.arguments
    if is_multi_step:
        skill_args = ""  # per-step arguments are resolved in the step loop
    elif is_prompt_mode:
        skill_args = (args.skill_args or "").strip() or config.execution.prompt
    else:
        skill_args = (args.skill_args or "").strip() or config.execution.arguments

    # Resolve {prompt} placeholder from batch.yaml
    if skill_args and "{prompt}" in skill_args:
        batch_path = Path(args.workspace) / "batch.yaml"
        if batch_path.exists():
            import yaml as _yaml
            with open(batch_path) as _f:
                batch = _yaml.safe_load(_f) or []
            if isinstance(batch, list) and batch:
                entry = batch[0]
                prompt_text = entry.get("prompt", "") if isinstance(entry, dict) else str(entry)
                skill_args = skill_args.replace("{prompt}", prompt_text.strip())

    # Build runner
    agent = args.agent or config.runner.type
    if agent not in RUNNERS:
        print(f"ERROR: unknown runner '{agent}'. Available: {list(RUNNERS.keys())}",
              file=sys.stderr)
        sys.exit(1)
    if agent in {"codex", "cursor", "fullsend"}:
        # The config loader/runner enforces these for a declared runner;
        # an --agent override must not slip past the same guarantees.
        if config.inputs.tools:
            print(f"ERROR: runner '{agent}' does not support inputs.tools "
                  "interception; use claude-code or remove the tool "
                  "interceptors", file=sys.stderr)
            sys.exit(1)
        if (agent in {"codex", "fullsend"}
                and getattr(config.runner, "workspace_mode", None) == "repo"):
            print(f"ERROR: runner '{agent}' does not support workspace_mode: "
                  "repo because repository answer-key protections cannot "
                  "be enforced", file=sys.stderr)
            sys.exit(1)
    runner_cls = RUNNERS[agent]

    # Provider plan (spec 014): built iff the effective skill model is an
    # openrouter:/ URI; preflight before any spend; one session (backfill
    # worker, key-usage reads) shared by every case and closed in the finally
    # — on every exit path (SystemExit, KeyboardInterrupt, exceptions).
    plan = _build_provider_plan(config, args, agent, output_dir)
    session = None
    if plan is not None:
        try:
            session = _start_provider_session(
                plan, config, output_dir,
                args.parallelism if args.parallelism is not None else config.execution.parallelism)
        except BaseException:
            plan.close()                 # a per-run key must not outlive a failed startup (exit 2 included)
            raise
    try:

        mlflow_experiment = args.mlflow_experiment or config.mlflow.experiment
        effort = args.effort or config.runner.effort

        # Honor user-authored permissions (allow + deny) in every mode.
        filtered_permissions = _resolve_permissions(config)

        runner = runner_cls.from_config(
            config,
            log_prefix="eval",
            subagent_model=subagent_model,
            mlflow_experiment=mlflow_experiment,
            mlflow_tracking_uri=config.mlflow.tracking_uri,
            effort=effort,
            permissions=filtered_permissions,
            **_runner_overrides(),
        )

        # Resolve timeout and budget: CLI override > config > defaults.
        # Use explicit None checks so that 0 is preserved (an operator who
        # passes --timeout 0 or sets max_budget_usd: 0 in the config gets
        # exactly that, not the default).
        timeout_s = (args.timeout if args.timeout is not None
                     else config.execution.timeout if config.execution.timeout is not None
                     else 3600)
        max_budget = (args.max_budget if args.max_budget is not None
                      else config.execution.max_budget_usd if config.execution.max_budget_usd is not None
                      else 100.0)

        # Compose system prompt: runner.system_prompt (if any) + harness prompt.
        # Skip the harness safety prompt for the opaque CLI runner — it references
        # tool interception hooks and permission controls that don't exist there.
        existing_prompt = (config.runner.system_prompt or "").strip()
        if agent == "cli":
            system_prompt = existing_prompt or None
        else:
            system_prompt = "\n\n".join(p for p in [existing_prompt, _HARNESS_SYSTEM_PROMPT] if p)

        # Capture user-facing eval parameters that defined this run, for the report.
        eval_params = _build_eval_params(args, config, skill_args, max_budget, timeout_s, effort,
                                         plan=plan)

        # ── Per-case execution (case mode) ────────────────
        # Case mode executes once per case with separate workspaces
        # (Works for both skill and prompt execution)
        if config.execution.mode == "case":
            parallelism = (args.parallelism if args.parallelism is not None
                           else config.execution.parallelism)
            _execute_per_case(args, config, runner, runner_cls,
                              output_dir, max_budget, timeout_s,
                              model, mlflow_experiment, system_prompt,
                              skill_args_template=skill_args,
                              eval_params=eval_params,
                              parallelism=parallelism,
                              effort=effort,
                              subagent_model=subagent_model,
                              target=target)
            return

        # ── Batch execution (below) ──────────────────────────────────────
        exec_label = f"/{target} {skill_args}" if target else skill_args
        print(f"Executing: {exec_label}", file=sys.stderr)
        print(f"Agent: {runner.name} | Model: {model}", file=sys.stderr)
        print(f"Workspace: {args.workspace}", file=sys.stderr)

        # Build hook environment for batch mode
        run_id = args.run_id or ""
        hook_env = build_hook_env(
            workspace=args.workspace,
            run_id=run_id,
            config_path=str(Path(args.config).resolve()),
            project_root=str(Path.cwd()),
            model=model,
            plan=plan, run_dir=output_dir,
        )
        log_dir = output_dir / "hooks"

        try:
            # Run before_all hooks and collect outputs
            global_hook_outputs = {}
            if config.hooks.before_all:
                print("Running before_all hooks...", file=sys.stderr)
                run_hooks(config.hooks.before_all, env=hook_env,
                          cwd=Path.cwd(), log_dir=log_dir,
                          phase_name="before_all")
                global_hook_outputs = collect_hook_outputs(Path(args.workspace))

            save_hook_data(output_dir, global_hook_outputs.get("data"))

            # Set MLflow environment in the workspace settings
            if mlflow_experiment:
                from agent_eval.mlflow.experiment import inject_tracing_env
                inject_tracing_env(args.workspace, project_root=Path.cwd(),
                                   tracking_uri=config.mlflow.tracking_uri,
                                   experiment_name=mlflow_experiment)

            workspace_settings = Path(args.workspace) / ".claude" / "settings.json"
            settings_path = workspace_settings if workspace_settings.exists() else None

            _bind_provider(runner)
            result = runner.execute(
                target=target,
                args=skill_args,
                workspace=Path(args.workspace),
                model=model,
                settings_path=settings_path,
                system_prompt=system_prompt,
                max_budget_usd=max_budget,
                timeout_s=timeout_s,
                extra_env=global_hook_outputs.get("env") or None,
            )

            exit_code = _save_result(result, args, output_dir, runner, model, eval_params=eval_params)

            # Copy batch input files to output dir for MLflow artifact logging.
            _copy_input_files_batch(Path(args.workspace), output_dir)

            sys.exit(exit_code)
        finally:
            if config.hooks.after_all:
                print("Running after_all hooks...", file=sys.stderr)
                run_hooks_safe(config.hooks.after_all, env=hook_env,
                               cwd=Path.cwd(), log_dir=log_dir,
                               phase_name="after_all")
    finally:
        if session is not None:
            session.close()


def _resolve_arguments(template, case_data, steps=None):
    """Resolve {field} or {{ field }} placeholders from case input data.

    ``steps`` (optional) binds the ``{{ steps.<id>.* }}`` namespace for
    multi-step execution — the accumulated results of earlier steps in the
    same case (Jinja2 syntax only).

    Supports two template syntaxes:
    - Simple: {field} or {field?} for optional fields
    - Jinja2: {{ input.field }} with full Jinja2 expressions

    The syntax is auto-detected: if template contains '{{', uses Jinja2;
    otherwise uses simple regex substitution.

    Args:
        template: String with placeholders
        case_data: Dict from input.yaml with field values

    Returns:
        String with all placeholders replaced

    Raises:
        ValueError: If required fields are missing
    """
    import re

    # Auto-detect Jinja2 syntax
    if '{{' in template or '{%' in template:
        # Use Jinja2 rendering
        try:
            from jinja2 import StrictUndefined, Template, UndefinedError
        except ImportError:
            raise ImportError(
                "Jinja2 is required for {{ }} template syntax. "
                "Install with: pip install jinja2"
            )

        try:
            # StrictUndefined makes missing required fields raise rather than
            # render to an empty string (honoring the docstring contract).
            # Genuinely optional fields should use {{ input.get('x', '') }}
            # or the `| default('')` filter.
            jinja_template = Template(template, undefined=StrictUndefined)
            # Render with input.* (and steps.* for multi-step) namespaces.
            result = jinja_template.render(input=case_data, steps=steps or {})
            return result.strip()
        except UndefinedError as e:
            raise ValueError(
                f"Undefined variable in Jinja2 template: {e}. "
                f"Template: {template}"
            )

    # Fall back to simple {field} regex substitution
    missing = []

    def _replace(m):
        field = m.group(1)
        optional = field.endswith("?")
        if optional:
            field = field[:-1]
        if field not in case_data:
            if optional:
                return ""
            missing.append(field)
            return ""
        value = case_data[field]
        if value is None or (isinstance(value, str) and not value.strip()):
            if optional:
                return ""
            missing.append(field)
            return ""
        return str(value).strip()

    result = re.sub(r'\{([\w-]+\??)\}', _replace, template).strip()
    if missing:
        raise ValueError(
            f"Missing required fields in input.yaml: {', '.join(missing)}. "
            f"Template: {template}")
    return result


def _build_eval_params(args, config, skill_args, max_budget, timeout_s, effort=None, plan=None):
    """Snapshot the user-facing eval parameters that defined this run.

    Surfaced in the HTML report so reviewers can see *what was run* without
    inspecting the harness invocation. Only includes parameters that are
    meaningful to a reader: the dataset/skill args, budget caps, execution
    mode, and optional flags actually set. Resolved values (effort, budget,
    timeout) are passed in so the snapshot reflects what actually ran, not
    just what was overridden via CLI."""
    params = {
        "execution_mode": config.execution.mode,
        "max_budget_usd": max_budget,
        "timeout_s": timeout_s,
    }
    # Which config file(s) defined the run (root first) — one entry for a
    # plain eval.yaml, more when an `extends:` profile was used.
    chain = list(getattr(config, "config_chain", None) or [])
    if chain:
        params["config_chain"] = chain
    # skill is optional for prompt mode
    target = args.skill or config.resolve_skill()
    if target:
        params["skill"] = target
    if skill_args:
        params["skill_args"] = skill_args
    if effort:
        params["effort"] = effort
    if getattr(args, "mlflow_experiment", None):
        params["mlflow_experiment"] = args.mlflow_experiment
    if plan is not None:
        from agent_eval.providers.openrouter.preflight import table_sha

        params["provider"] = {
            "name": plan.kind, "kind": plan.kind, "transport": plan.transport,
            "runner": plan.runner, "base_url": plan.base_url,
            "routing_sha": table_sha(plan.routing), "routing_enforcement": plan.enforcement,
            "background_model": plan.background_model, "key_exposed_to_agent": True,
            "key_scope": plan.key_scope,
            "key_hash": f"sha256:{plan.key_hash}" if plan.key_hash else None,
        }
        # The CLI's cap is the only in-flight cap at `audit`; a cap <= 0 emits
        # no flag at all (the CLI rejects 0), recorded as null.
        cap = (round(max_budget * float(plan.cli_budget_inflation), 6)
               if isinstance(max_budget, (int, float)) and max_budget > 0 else None)
        params["budget"] = {"cli_cap_usd": cap, "invocation_usd": max_budget,
                            "run_usd": plan.budget_run_usd,
                            "enforcement": "key-guardrail" if plan.enforcement == "key-guardrail"
                            else "cli-estimate"}
    return params


def _resolve_step_env(env):
    """Resolve a step's env dict, expanding ``$VAR`` from the caller's
    environment (mirrors ExecutionConfig.env semantics). Missing vars omitted."""
    out = {}
    for k, v in (env or {}).items():
        if isinstance(v, str) and v.startswith("$"):
            val = os.environ.get(v[1:])
            if val is not None:
                out[k] = val
        elif v is not None:
            out[k] = str(v)
    return out


def _extract_last_assistant_text(stdout, limit=4000):
    """Best-effort final assistant message from a run's stdout, for the
    ``{{ steps.<id>.output }}`` template var. Parses claude-code stream-json;
    falls back to the raw stdout tail for other runners."""
    if not stdout:
        return ""
    texts = []
    saw_json = False
    for line in stdout.splitlines():
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        saw_json = True
        if obj.get("type") == "result" and isinstance(obj.get("result"), str):
            texts.append(obj["result"])
        elif obj.get("type") == "assistant" and not obj.get("parent_tool_use_id"):
            msg = obj.get("message") or {}
            for block in (msg.get("content") or []):
                if isinstance(block, dict) and block.get("type") == "text":
                    if (block.get("text") or "").strip():
                        texts.append(block["text"])
        elif obj.get("type") == "item.completed":
            item = obj.get("item") or {}
            if (isinstance(item, dict) and item.get("type") == "agent_message"
                    and isinstance(item.get("text"), str)
                    and item["text"].strip()):
                texts.append(item["text"])
    text = (texts[-1] if texts else ("" if saw_json else stdout)).strip()
    return text[:limit]


def _cost_label(cost, source=None, estimate=None):
    """Render a per-case cost, honestly showing unknown as such. Under a
    provider plan the source is named (`$X (openrouter:generation)`), and an
    unreconciled case shows the runner's estimate as such."""
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        return f"${cost:.2f} ({source})" if source and source != "runner:reported" else f"${cost:.2f}"
    if isinstance(estimate, (int, float)) and not isinstance(estimate, bool):
        return f"cost n/a (est. ${estimate:.2f}, unreconciled)"
    return "cost n/a"


# --- Reconcile-at-write (spec 014) --------------------------------------------
#
# `write_run_result` is the ONLY code in this file that opens run_result.json:
# every write site below goes through it (guarded by
# tests/test_execute_write_sites.py), so the cost-provenance fields are written
# on the crash and step paths too — where the runner's estimate is most
# misleading. The provider plan and the strict flags are process-wide state set
# by main(); with no plan and no ledger file the payload is written unchanged.

_PROVIDER_PLAN = None
_PROVIDER_SESSION = None
_SESSION_FACTORY = None      # tests inject a ProviderSession subclass with fake timing
_RECONCILE_OPTS = {"strict_cost": False, "strict_routing": False, "allow_estimate": False}


def set_provider_plan(plan, session=None):
    global _PROVIDER_PLAN, _PROVIDER_SESSION
    _PROVIDER_PLAN = plan
    _PROVIDER_SESSION = session


def write_run_result(path, payload, *, plan=None, ledger=None, key_usage=None):
    """Reconcile ``payload`` against the run's ledger and write it. Delegates to
    the shared helper so Harbor's writer and this one cannot drift."""
    plan = plan if plan is not None else _PROVIDER_PLAN
    session = _PROVIDER_SESSION
    catalog = session.catalog if session is not None else None
    if plan is not None and session is not None and session.snapshot_ref:
        # The audit joins against the preflight's frozen catalog view.
        routing = payload.get("routing") if isinstance(payload.get("routing"), dict) else {}
        if not routing.get("snapshot"):
            payload = {**payload, "routing": {**routing, "snapshot": session.snapshot_ref}}
    return _reconciled_write(Path(path), payload, plan=plan, ledger=ledger,
                             key_usage=key_usage, catalog=catalog,
                             allow_estimate=_RECONCILE_OPTS["allow_estimate"])


def _runner_overrides():
    """Constructor overrides every runner instance receives under a plan."""
    if _PROVIDER_PLAN is None:
        return {}
    return {"provider_plan": _PROVIDER_PLAN, "run_id": _PROVIDER_PLAN.run_id}


def _bind_provider(runner, case_id=None, step_id=None):
    """Hand the runner the session binding it sights generation ids through
    for one case/step. A no-op without a plan or for a runner without
    provider integration (duck-typed runners keep working)."""
    bind = getattr(runner, "bind_provider", None)
    if bind is None:
        return
    bind(_PROVIDER_SESSION.bind(case_id, step_id) if _PROVIDER_SESSION is not None else None)


def _build_provider_plan(config, args, agent, output_dir):
    """Activation rule (spec 014, Decision 21): the plan exists iff the
    *effective* skill model (CLI > config) is an ``openrouter:/`` URI. A
    declared block no role uses is inert (one WARNING). Exit 2 on a plan the
    config cannot build (missing key, non-OpenRouter subagent/hook, runner)."""
    from agent_eval.providers.base import ConfigError
    from agent_eval.providers.openrouter.plan import build_plan, effective_roles, plan_is_active

    roles = effective_roles(config, {"skill": args.model, "subagent": args.subagent_model})
    declared = getattr(getattr(config.models, "providers", None), "openrouter", None) is not None
    if not plan_is_active(roles):
        if declared:
            print("WARNING: models.providers.openrouter is declared but no effective role "
                  "or CLI model uses openrouter:/ — routing table inactive", file=sys.stderr)
        return None
    # The config loader rejects a non-claude-code per-step runner when the
    # config's own skill model is openrouter:/; a plan activated by the
    # --model / --agent CLI overrides is only visible here.
    step_types = sorted({getattr(getattr(step, "runner", None), "type", None)
                         for step in (config.execution.steps or [])} - {None, "claude-code"})
    if agent != "claude-code" or step_types:
        offenders = ", ".join(([f"runner '{agent}'"] if agent != "claude-code" else [])
                              + [f"execution.steps[].runner.type '{t}'" for t in step_types])
        print(f"ERROR: the direct OpenRouter transport is implemented for the claude-code "
              f"runner; {offenders} cannot run {roles['skill']!r}", file=sys.stderr)
        sys.exit(2)
    try:
        return build_plan(config, roles, runner="claude-code",
                          run_id=args.run_id or output_dir.name)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)


def _start_provider_session(plan, config, output_dir, parallelism):
    """Preflight (before any spend), then the run-scoped session: the
    ``/generation`` backfill worker and the key-usage "before" read. The
    session is attached to the plan so ``plan.close()`` reaches it."""
    from agent_eval.config import OpenRouterConfig
    from agent_eval.providers.base import ConfigError
    from agent_eval.providers.openrouter.plan import judge_routing_keys, judge_verdict_policies
    from agent_eval.providers.openrouter.preflight import run_preflight
    from agent_eval.providers.openrouter.session import ProviderSession

    orc = getattr(getattr(config.models, "providers", None), "openrouter", None) or OpenRouterConfig()
    try:
        pre = run_preflight(plan, level=orc.preflight, run_dir=output_dir,
                            judges=judge_routing_keys(config),
                            verdict=getattr(config.models, "judge_verdict", "auto"),
                            judge_verdicts=judge_verdict_policies(config))
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
    for warning in pre.warnings:
        print(f"WARNING: preflight: {warning}", file=sys.stderr)
    factory = _SESSION_FACTORY or ProviderSession
    session = factory(plan, output_dir, parallelism=parallelism or 1, snapshot=pre.snapshot).start()
    plan.attach(session)
    set_provider_plan(plan, session=session)
    degraded = f" (degraded: {pre.degraded_reason})" if pre.degraded_reason else ""
    scope = "per-run" if plan.key_scope == "per-run" else "operator"
    print(f"Provider: openrouter direct | model: {plan.skill.id} | enforcement: {plan.enforcement} | "
          f"preflight: {orc.preflight}{degraded} | key exposed to agent: {scope} key "
          f"sha256:{plan.key_hash} (enforcement: {plan.enforcement})", file=sys.stderr)
    return session


def _finish_provider_run(run_meta):
    """Run end, before the strict flags judge the final result: drain and
    retry the backfill, settle and read the key usage, then the last
    reconcile pass over every ``run_result.json`` of the run."""
    session = _PROVIDER_SESSION
    if session is None:
        return run_meta
    session.finish()
    final = session.reconcile_run(allow_estimate=_RECONCILE_OPTS["allow_estimate"])
    return final if final is not None else run_meta


def _live_markers(payload):
    """Per-case progress-line markers derived from the reconciled payload:
    ids still inside the backfill window, routing violations already visible,
    the per-run key's limit."""
    marks = []
    cov = payload.get("cost_coverage") if isinstance(payload.get("cost_coverage"), dict) else None
    if cov:
        pending = ((cov.get("requests") or 0) - (cov.get("requests_priced") or 0)
                   - (cov.get("requests_missing_cost") or 0))
        if pending > 0:
            marks.append(f"cost pending: {pending} ids")
    routing = payload.get("routing") if isinstance(payload.get("routing"), dict) else {}
    if routing.get("violations"):
        marks.append(f"routing: {len(routing['violations'])} violations")
    budget = payload.get("budget") if isinstance(payload.get("budget"), dict) else {}
    if budget.get("exceeded_reason") == "limit_usd":
        marks.append("budget: limit_usd")
    return "".join(f" [{m}]" for m in marks)


def _apply_strict_exit(exit_code, strict_exit):
    """A strict-flag failure turns a *successful* run into exit 2; a run that
    already failed keeps its own code (a runner timeout's -1 must not be
    flattened to 0 or masked by 2)."""
    if strict_exit and exit_code == 0:
        return strict_exit
    return exit_code


def _strict_failures(run_meta):
    """Why the strict flags fail the final run-level result: a plan is active
    and cost is unavailable, the run budget was exceeded post hoc, or the
    routing audit found violations / is incomplete. Empty when nothing fails."""
    if _PROVIDER_PLAN is None:
        return []
    reasons = []
    if _RECONCILE_OPTS["strict_cost"]:
        if run_meta.get("cost_source") == "unavailable":
            reasons.append("cost_source unavailable")
        budget = run_meta.get("budget") or {}
        if budget.get("exceeded") == "run":
            reasons.append(f"run budget exceeded by ${budget.get('overshoot_usd')}")
    if _RECONCILE_OPTS["strict_routing"]:
        routing = run_meta.get("routing") or {}
        if routing.get("violations"):
            reasons.append(f"{len(routing['violations'])} routing violation(s)")
        if routing.get("audit_complete") is False:
            reasons.append("routing audit incomplete")
    return reasons


def _strict_exit_code(run_meta):
    reasons = _strict_failures(run_meta)
    if not reasons:
        return None
    print("STRICT: " + "; ".join(reasons), file=sys.stderr)
    return 2


def _finalize_strict(path, run_meta, exit_code):
    """Apply the strict flags to the final run-level result AND persist the
    outcome: when they turn a successful run into exit 2, run_result.json is
    rewritten with the final exit_code and the reasons, so readers never see a
    success the command itself reported as a failure. Returns (run_meta, code)."""
    reasons = _strict_failures(run_meta)
    final = _apply_strict_exit(exit_code, 2 if reasons else None)
    if reasons:
        print("STRICT: " + "; ".join(reasons), file=sys.stderr)
    if final != run_meta.get("exit_code") or (reasons and not run_meta.get("strict_failures")):
        run_meta = dict(run_meta, exit_code=final, strict_failures=reasons)
        run_meta = write_run_result(path, run_meta)
    return run_meta, final


def _sum_reported_costs(case_results):
    """Sum measured case costs, preserving unknown when none were reported."""
    reported = [
        result["cost_usd"] for result in case_results.values()
        if isinstance(result.get("cost_usd"), (int, float))
        and not isinstance(result.get("cost_usd"), bool)
    ]
    return sum(reported) if reported else None


def _list_step_output_files(case_ws, config):
    """Relative paths of files under the configured output dirs after a step
    (best-effort, for ``{{ steps.<id>.files }}``)."""
    paths = []
    for o in (getattr(config, "outputs", None) or []):
        p = o.get("path") if isinstance(o, dict) else getattr(o, "path", None)
        if p:
            paths.append(p)
    if not paths:
        paths = ["output"]
    files = []
    for rel in paths:
        base = case_ws / rel
        if base.is_dir():
            for f in sorted(base.rglob("*")):
                if f.is_file() and not f.is_symlink():
                    try:
                        files.append(str(f.relative_to(case_ws)))
                    except ValueError:
                        pass
    return files


def _aggregate_step_metrics(step_metrics):
    """Roll per-step RunResult dicts up into one case-level result dict."""
    results = list(step_metrics.values())
    agg_tokens = {}
    for r in results:
        for k, v in (r.get("token_usage") or {}).items():
            if isinstance(v, (int, float)):
                agg_tokens[k] = agg_tokens.get(k, 0) + v
    agg_pm = {}
    for r in results:
        for m, stats in (r.get("per_model_usage") or {}).items():
            agg_pm.setdefault(m, {})
            for k, v in (stats or {}).items():
                if isinstance(v, (int, float)):
                    agg_pm[m][k] = agg_pm[m].get(k, 0) + v
    agg_pmt = {}
    for r in results:
        for m, t in (r.get("per_model_turns") or {}).items():
            if isinstance(t, (int, float)):
                agg_pmt[m] = agg_pmt.get(m, 0) + t
    has_cost = any(r.get("cost_usd") is not None for r in results)
    # A case fails if any step did. max() would mask a negative failure code
    # (e.g. a runner timeout of -1) behind a later 0, so surface the first
    # non-zero exit code instead.
    exit_codes = [r.get("exit_code") or 0 for r in results]
    worst_exit = next((ec for ec in exit_codes if ec != 0), 0)
    # Concatenate step denial lists in step order (dedup by tool_use_id in
    # case a step result was double-counted) so the case-level entry reports
    # denials like a single-step case does, instead of hiding them in steps.
    agg_denials, seen_ids = [], set()
    for r in results:
        for d in (r.get("permission_denials") or []):
            duid = d.get("tool_use_id") if isinstance(d, dict) else None
            if duid is not None and duid in seen_ids:
                continue
            if duid is not None:
                seen_ids.add(duid)
            agg_denials.append(d)
    return {
        "exit_code": worst_exit,
        "duration_s": round(sum(r.get("duration_s") or 0 for r in results), 1),
        "token_usage": agg_tokens or None,
        "cost_usd": (round(sum(r.get("cost_usd") or 0 for r in results), 4)
                     if has_cost else None),
        "num_turns": sum(r.get("num_turns") or 0 for r in results) or None,
        "per_model_usage": agg_pm or None,
        "per_model_turns": agg_pmt or None,
        "permission_denials": agg_denials,
        "message_ids": sorted({i for r in results for i in (r.get("message_ids") or [])}),
    }


def _run_single_case_in_repo(runner, skill_name, case_ws, output_dir,
                              skill_args_template, model, mlflow_experiment,
                              mlflow_tracking_uri, system_prompt, max_budget,
                              timeout_s, total_cases, index):
    """Execute in-repo mode: agent runs in repo root, I/O in case_ws.

    Thread-safe: case_ws is case-specific.
    Returns (case_id, result_dict) or (case_id, None) if workspace missing.
    """
    import yaml as _yaml
    import subprocess

    if not case_ws.exists():
        print(f"  [{index}/{total_cases}] {case_ws.name}: SKIP (workspace missing)",
              file=sys.stderr)
        return case_ws.name, None

    # Read metadata
    meta_file = case_ws / "_metadata.yaml"
    if not meta_file.exists():
        print(f"  [{index}/{total_cases}] {case_ws.name}: SKIP (metadata missing)",
              file=sys.stderr)
        return case_ws.name, None

    meta = _yaml.safe_load(meta_file.read_text())
    case_id = meta["case_id"]
    repo_cwd = Path(meta["repo_cwd"])

    # Resolve git (raises RuntimeError if not found)
    git_bin = _resolve_git()

    # Snapshot repo state before execution
    repo_before = subprocess.run(
        [git_bin, "status", "--porcelain"],
        cwd=repo_cwd,
        capture_output=True,
        text=True,
        check=True
    ).stdout

    # Resolve arguments
    case_args = skill_args_template
    input_path = case_ws / "input.yaml"
    if input_path.exists() and case_args:
        case_data = _yaml.safe_load(input_path.read_text()) or {}
        if isinstance(case_data, dict):
            case_args = _resolve_arguments(case_args, case_data)

    if mlflow_experiment:
        from agent_eval.mlflow.experiment import inject_tracing_env
        inject_tracing_env(str(case_ws), project_root=repo_cwd,
                           tracking_uri=mlflow_tracking_uri,
                           experiment_name=mlflow_experiment)

    case_settings = case_ws / ".claude" / "settings.json"
    settings_path = case_settings if case_settings.exists() else None

    exec_label = f"/{skill_name} {case_args}" if skill_name else case_args
    print(f"  [{index}/{total_cases}] {case_id}: {exec_label}",
          file=sys.stderr)

    # Execute in repo with case_ws-based settings
    _bind_provider(runner, case_id)
    result = runner.execute(
        target=skill_name,
        args=case_args,
        workspace=repo_cwd,  # Agent runs in REPO
        model=model,
        settings_path=settings_path,  # Settings from case_ws
        system_prompt=system_prompt,
        max_budget_usd=max_budget,
        timeout_s=timeout_s,
    )

    # Verify repo wasn't modified
    repo_after = subprocess.run(
        [git_bin, "status", "--porcelain"],
        cwd=repo_cwd,
        capture_output=True,
        text=True,
        check=True
    ).stdout

    repo_dirty = repo_before != repo_after
    if repo_dirty:
        modified_files = [line for line in repo_after.split('\n')
                          if line and line not in repo_before.split('\n')]
        print(f"    ERROR: {case_id} modified repo:", file=sys.stderr)
        for line in modified_files[:5]:
            print(f"      {line}", file=sys.stderr)
        # Save evidence. Record this case as a failed (repo-modified) result
        # below and continue — a single case violating the read-only contract
        # must not abort the whole run and discard the other cases' results.
        (case_ws / "repo_modifications.txt").write_text(
            f"BEFORE:\n{repo_before}\n\nAFTER:\n{repo_after}\n"
        )

    # Write stdout/stderr to workspace output (for collect.py and judges)
    ws_output = case_ws / "output"
    ws_output.mkdir(parents=True, exist_ok=True)

    if result.stdout:
        (ws_output / "stdout.log").write_text(result.stdout)
    if result.stderr:
        (ws_output / "stderr.log").write_text(result.stderr)

    # Collect outputs to run directory
    case_output = output_dir / "cases" / case_id
    case_output.mkdir(parents=True, exist_ok=True)

    # Copy input.yaml, rejecting symlinks to prevent CWE-59 (path traversal)
    if input_path.exists() and not input_path.is_symlink():
        shutil.copy2(input_path, case_output / "input.yaml")

    # Copy all outputs from case workspace (including stdout/stderr we just wrote)
    # Reject symlinks to prevent CWE-59 (arbitrary file disclosure)
    if ws_output.exists():
        for item in ws_output.iterdir():
            if item.is_file() and not item.is_symlink():
                shutil.copy2(item, case_output / item.name)

    # Copy subagent transcripts (reject symlinks to prevent CWE-59)
    ws_subagents = case_ws / "subagents"
    if ws_subagents.exists() and ws_subagents.is_dir():
        out_subagents = case_output / "subagents"
        out_subagents.mkdir(exist_ok=True)
        for f in ws_subagents.iterdir():
            if f.is_file() and not f.is_symlink() and f.suffix == ".jsonl":
                shutil.copy2(f, out_subagents / f.name)

    case_result = {
        "exit_code": max(result.exit_code, 1) if repo_dirty else result.exit_code,
        "duration_s": round(result.duration_s, 1),
        "token_usage": result.token_usage,
        "cost_usd": result.cost_usd,
        "num_turns": result.num_turns,
        "per_model_usage": result.per_model_usage,
        "per_model_turns": result.per_model_turns,
        # Denials happen in the runner's stream but were never persisted:
        # a real run's only denial (a workspace-escape attempt) survived
        # solely in raw stdout.log, invisible to every downstream reader.
        "permission_denials": result.permission_denials or [],
        "repo_modified": repo_dirty,
        "message_ids": result.message_ids or [],
        "cost_source": result.cost_source,
        "error_class": result.error_class,
        "budget": result.budget,
    }
    # run_result write-site 1: case mode, repo-mode per-case result
    case_result = write_run_result(case_output / "run_result.json", case_result)

    status = "OK" if result.exit_code == 0 else f"FAIL (exit {result.exit_code})"
    if repo_before != repo_after:
        status += " [REPO MODIFIED]"
    print(f"    → {case_id}: {status} | {result.duration_s:.0f}s | "
          f"{_cost_label(case_result.get('cost_usd'), case_result.get('cost_source'), case_result.get('cost_usd_estimate'))}",
          file=sys.stderr)

    return case_id, case_result


def _run_single_case(runner, skill_name, case_id, case_ws, output_dir,
                     skill_args_template, model, mlflow_experiment,
                     mlflow_tracking_uri, system_prompt, max_budget, timeout_s,
                     total_cases, index, config=None, hook_env=None,
                     global_hook_outputs=None, subagent_model=None):
    """Execute and collect results for a single test case.

    Thread-safe: all I/O is to case-specific directories.
    Returns (case_id, result_dict) or (case_id, None) if workspace missing.
    """
    import yaml as _yaml

    if not case_ws.exists():
        print(f"  [{index}/{total_cases}] {case_id}: SKIP (workspace missing)",
              file=sys.stderr)
        return case_id, None

    # Multi-step pipeline: delegate to the step loop. The single-step path
    # below is unchanged.
    if config is not None and getattr(config.execution, "steps", None):
        return _run_multi_step_case(
            runner, case_id, case_ws, output_dir, model, mlflow_experiment,
            mlflow_tracking_uri, system_prompt, max_budget, timeout_s,
            total_cases, index, config, hook_env=hook_env,
            global_hook_outputs=global_hook_outputs,
            subagent_model=subagent_model)

    case_args = skill_args_template
    input_path = case_ws / "input.yaml"
    if input_path.exists() and case_args:
        case_data = _yaml.safe_load(input_path.read_text()) or {}
        if isinstance(case_data, dict):
            case_args = _resolve_arguments(case_args, case_data)

    if mlflow_experiment:
        from agent_eval.mlflow.experiment import inject_tracing_env
        inject_tracing_env(str(case_ws), project_root=Path.cwd(),
                           tracking_uri=mlflow_tracking_uri,
                           experiment_name=mlflow_experiment)

    case_settings = case_ws / ".claude" / "settings.json"
    settings_path = case_settings if case_settings.exists() else None

    exec_label = f"/{skill_name} {case_args}" if skill_name else case_args
    print(f"  [{index}/{total_cases}] {case_id}: {exec_label}",
          file=sys.stderr)

    # Per-case hooks: build case-specific env, run before_each/after_each.
    # The entire block is wrapped in try/finally so that after_each cleanup
    # hooks fire even if before_each or run_skill raises.
    case_hook_env = None
    merged_hook_data = {}
    merged_env = {}
    result = None
    error_msg = ""
    try:
        if config and config.hooks and hook_env:
            dataset_path = config.dataset.path
            case_source_dir = str(config.resolve_path(dataset_path) / case_id) if dataset_path else ""
            case_hook_env = {
                **hook_env,
                "CASE_ID": case_id,
                "CASE_WORKSPACE": str(case_ws.resolve()),
                "CASE_SOURCE_DIR": case_source_dir,
                "CASE_INPUT": str((case_ws / "input.yaml").resolve()),
            }
            log_dir = output_dir / "hooks"
            if config.hooks.before_each:
                run_hooks(config.hooks.before_each, env=case_hook_env,
                          cwd=case_ws, log_dir=log_dir,
                          phase_name="before_each", case_id=case_id)

            # Collect hook outputs and merge with global
            case_outputs = collect_hook_outputs(case_ws)
            global_env = (global_hook_outputs or {}).get("env", {})
            case_env = case_outputs.get("env", {})
            merged_env = {**global_env, **case_env}

            if merged_env:
                case_hook_env.update({k: str(v) for k, v in merged_env.items()})

            global_data = (global_hook_outputs or {}).get("data", {})
            case_data_out = case_outputs.get("data", {})
            merged_hook_data = {**global_data, **case_data_out}

        _bind_provider(runner, case_id)
        result = runner.execute(
            target=skill_name,
            args=case_args,
            workspace=case_ws,
            model=model,
            settings_path=settings_path,
            system_prompt=system_prompt,
            max_budget_usd=max_budget,
            timeout_s=timeout_s,
            extra_env=merged_env or None,
        )
    except Exception as exc:
        print(f"    → {case_id}: ERROR ({exc})", file=sys.stderr)
        result = None
        error_msg = str(exc)
    finally:
        # Run after_each hooks (guaranteed, like after_all)
        if config and config.hooks and config.hooks.after_each and case_hook_env:
            log_dir = output_dir / "hooks"
            run_hooks_safe(config.hooks.after_each, env=case_hook_env,
                           cwd=case_ws, log_dir=log_dir,
                           phase_name="after_each", case_id=case_id)

    if result is None:
        case_output = output_dir / "cases" / case_id
        case_output.mkdir(parents=True, exist_ok=True)
        failed_result = {
            "exit_code": 1,
            "duration_s": 0,
            "token_usage": None,
            "cost_usd": None,
            "permission_denials": [],  # case crashed before a result existed
            "num_turns": None,
            "per_model_usage": None,
            "per_model_turns": None,
            "error": error_msg,
            "message_ids": [],
        }
        # run_result write-site 2: case mode, runner exception (crash path)
        failed_result = write_run_result(case_output / "run_result.json", failed_result)
        return case_id, failed_result

    # Write stdout/stderr to workspace output (for collect.py and judges)
    ws_output = case_ws / "output"
    ws_output.mkdir(parents=True, exist_ok=True)

    if result.stdout:
        (ws_output / "stdout.log").write_text(result.stdout)
    if result.stderr:
        (ws_output / "stderr.log").write_text(result.stderr)

    # Also write immediately to case output for instant access
    case_output = output_dir / "cases" / case_id
    case_output.mkdir(parents=True, exist_ok=True)

    if result.stdout:
        (case_output / "stdout.log").write_text(result.stdout)
    if result.stderr:
        (case_output / "stderr.log").write_text(result.stderr)

    # Save hook output data for judges
    save_hook_data(case_output, merged_hook_data)

    if input_path.exists() and not input_path.is_symlink():
        shutil.copy2(input_path, case_output / "input.yaml")

    ws_subagents = case_ws / "subagents"
    if ws_subagents.exists() and ws_subagents.is_dir():
        out_subagents = case_output / "subagents"
        out_subagents.mkdir(exist_ok=True)
        for f in ws_subagents.iterdir():
            if f.is_file() and not f.is_symlink() and f.suffix == ".jsonl":
                shutil.copy2(f, out_subagents / f.name)

    case_result = {
        "exit_code": result.exit_code,
        "duration_s": round(result.duration_s, 1),
        "token_usage": result.token_usage,
        "cost_usd": result.cost_usd,
        "num_turns": result.num_turns,
        "per_model_usage": result.per_model_usage,
        "per_model_turns": result.per_model_turns,
        # Denials happen in the runner's stream but were never persisted:
        # a real run's only denial (a workspace-escape attempt) survived
        # solely in raw stdout.log, invisible to every downstream reader.
        "permission_denials": result.permission_denials or [],
        "message_ids": result.message_ids or [],
        "cost_source": result.cost_source,
        "error_class": result.error_class,
        "budget": result.budget,
    }
    # run_result write-site 3: case mode, per-case result
    case_result = write_run_result(case_output / "run_result.json", case_result)

    status = "OK" if result.exit_code == 0 else f"FAIL (exit {result.exit_code})"
    print(f"    → {case_id}: {status} | {result.duration_s:.0f}s | "
          f"{_cost_label(case_result.get('cost_usd'), case_result.get('cost_source'), case_result.get('cost_usd_estimate'))}"
          f"{_live_markers(case_result)}",
          file=sys.stderr)

    return case_id, case_result


def _run_multi_step_case(runner, case_id, case_ws, output_dir, model,
                         mlflow_experiment, mlflow_tracking_uri, system_prompt,
                         max_budget, timeout_s, total_cases, index, config,
                         hook_env=None, global_hook_outputs=None,
                         subagent_model=None):
    """Execute a multi-step pipeline for one case.

    Steps run sequentially in the shared case workspace, so files written by
    step N are visible to N+1.  Each step is one ``runner.execute()``; per-step
    stdout/metrics are saved (for step-scoped judges) and the case metrics are
    the roll-up.  before_each/after_each wrap the whole case; before_step/
    after_step wrap each step.  Thread-safe: all I/O is case-specific.
    """
    import yaml as _yaml

    steps = config.execution.steps
    case_data = {}
    input_path = case_ws / "input.yaml"
    if input_path.exists():
        loaded = _yaml.safe_load(input_path.read_text()) or {}
        if isinstance(loaded, dict):
            case_data = loaded

    if mlflow_experiment:
        from agent_eval.mlflow.experiment import inject_tracing_env
        inject_tracing_env(str(case_ws), project_root=Path.cwd(),
                           tracking_uri=mlflow_tracking_uri,
                           experiment_name=mlflow_experiment)

    case_settings = case_ws / ".claude" / "settings.json"
    settings_path = case_settings if case_settings.exists() else None

    log_dir = output_dir / "hooks"
    case_output = output_dir / "cases" / case_id
    case_output.mkdir(parents=True, exist_ok=True)

    has_hooks = bool(config.hooks and hook_env)
    case_hook_env = None
    base_env = {}
    merged_hook_data = {}
    steps_ctx = {}
    step_metrics = {}
    last_result = None
    error_msg = ""
    aborted_at = None

    try:
        if has_hooks:
            dataset_path = config.dataset.path
            case_source_dir = (str(config.resolve_path(dataset_path) / case_id)
                               if dataset_path else "")
            case_hook_env = {
                **hook_env,
                "CASE_ID": case_id,
                "CASE_WORKSPACE": str(case_ws.resolve()),
                "CASE_SOURCE_DIR": case_source_dir,
                "CASE_INPUT": str((case_ws / "input.yaml").resolve()),
            }
            if config.hooks.before_each:
                run_hooks(config.hooks.before_each, env=case_hook_env,
                          cwd=case_ws, log_dir=log_dir,
                          phase_name="before_each", case_id=case_id)
            case_outputs = collect_hook_outputs(case_ws)
            base_env = {**(global_hook_outputs or {}).get("env", {}),
                        **case_outputs.get("env", {})}
            if base_env:
                case_hook_env.update({k: str(v) for k, v in base_env.items()})
            merged_hook_data = {**(global_hook_outputs or {}).get("data", {}),
                                **case_outputs.get("data", {})}

        for si, step in enumerate(steps, 1):
            step_id = step.id or f"step-{si}"
            is_skill = bool(step.skill and step.skill.strip())
            template = step.arguments if is_skill else step.prompt
            step_target = step.skill if is_skill else None
            resolved = (_resolve_arguments(template, case_data, steps=steps_ctx)
                        if template else "")

            step_env = dict(base_env)
            step_env.update(_resolve_step_env(step.env))

            step_runner = runner
            if getattr(step, "runner", None):
                rtype = step.runner.type
                if rtype not in RUNNERS:
                    raise ValueError(
                        f"step '{step_id}': unknown runner '{rtype}'. "
                        f"Available: {list(RUNNERS)}")
                step_cfg = copy.copy(config)
                step_cfg.runner = step.runner
                if rtype in {"codex", "cursor", "fullsend"}:
                    if config.inputs.tools:
                        raise ValueError(
                            f"runner '{rtype}' does not support inputs.tools "
                            "interception")
                    if (rtype in {"codex", "fullsend"}
                            and step.runner.workspace_mode == "repo"):
                        raise ValueError(
                            f"runner '{rtype}' does not support "
                            "workspace_mode: repo")
                step_runner = RUNNERS[rtype].from_config(
                    step_cfg, log_prefix=f"eval:{case_id}",
                    subagent_model=subagent_model,
                    mlflow_experiment=mlflow_experiment,
                    mlflow_tracking_uri=mlflow_tracking_uri,
                    permissions=_resolve_permissions(config),
                    effort=step.runner.effort,
                    **_runner_overrides())

            step_timeout = (step.timeout if step.timeout is not None
                            else timeout_s)
            step_budget = (step.max_budget_usd if step.max_budget_usd is not None
                           else max_budget)

            step_hook_env = None
            if case_hook_env is not None:
                step_hook_env = {**case_hook_env, "STEP_ID": step_id,
                                 "STEP_INDEX": str(si)}
                step_hook_env.update(
                    {k: str(v) for k, v in step_env.items() if v is not None})

            # Log the step target/id only — the resolved args may embed
            # $VAR-expanded secrets or prior-step model output (CWE-532).
            print(f"  [{index}/{total_cases}] {case_id} · step {si}/{len(steps)} "
                  f"({step_id}): {'/' + step_target if step_target else 'prompt'}",
                  file=sys.stderr)

            step_result = None
            try:
                if step_hook_env is not None and config.hooks.before_step:
                    run_hooks(config.hooks.before_step, env=step_hook_env,
                              cwd=case_ws, log_dir=log_dir,
                              phase_name=f"before_step:{step_id}",
                              case_id=case_id)
                    hout = collect_hook_outputs(case_ws)
                    if hout.get("env"):
                        step_env.update(
                            {k: str(v) for k, v in hout["env"].items()})
                step_system_prompt = system_prompt
                if getattr(step, "runner", None) and step.runner.system_prompt:
                    # Same precedence Harbor task generation applies: a
                    # per-step runner override wins over the eval default.
                    step_system_prompt = step.runner.system_prompt
                _bind_provider(step_runner, case_id, step_id)
                step_result = step_runner.execute(
                    target=step_target,
                    args=resolved,
                    workspace=case_ws,
                    model=model,
                    settings_path=settings_path,
                    system_prompt=step_system_prompt,
                    max_budget_usd=step_budget,
                    timeout_s=step_timeout,
                    extra_env=step_env or None,
                )
            finally:
                if step_hook_env is not None and config.hooks.after_step:
                    ec = (str(step_result.exit_code)
                          if step_result is not None else "1")
                    run_hooks_safe(
                        config.hooks.after_step,
                        env={**step_hook_env, "STEP_EXIT_CODE": ec},
                        cwd=case_ws, log_dir=log_dir,
                        phase_name=f"after_step:{step_id}", case_id=case_id)

            last_result = step_result
            step_dir = case_output / "steps" / step_id
            step_dir.mkdir(parents=True, exist_ok=True)
            if step_result.stdout:
                (step_dir / "stdout.log").write_text(step_result.stdout)
            if step_result.stderr:
                (step_dir / "stderr.log").write_text(step_result.stderr)

            step_metrics[step_id] = {
                "exit_code": step_result.exit_code,
                "duration_s": round(step_result.duration_s, 1),
                "token_usage": step_result.token_usage,
                "cost_usd": step_result.cost_usd,
                "num_turns": step_result.num_turns,
                "per_model_usage": step_result.per_model_usage,
                "per_model_turns": step_result.per_model_turns,
                "permission_denials": step_result.permission_denials or [],
                "message_ids": step_result.message_ids or [],
                "cost_source": step_result.cost_source,
                "error_class": step_result.error_class,
                "budget": step_result.budget,
            }
            steps_ctx[step_id] = {
                "output": _extract_last_assistant_text(step_result.stdout),
                "exit_code": step_result.exit_code,
                "files": _list_step_output_files(case_ws, config),
            }
            status = ("OK" if step_result.exit_code == 0
                      else f"FAIL (exit {step_result.exit_code})")
            print(f"    → {case_id} step {step_id}: {status} | "
                  f"{step_result.duration_s:.0f}s | "
                  f"{_cost_label(step_result.cost_usd)}", file=sys.stderr)

            if step_result.exit_code != 0 and step.on_failure == "fail":
                aborted_at = step_id
                break

    except Exception as exc:
        print(f"    → {case_id}: ERROR ({exc})", file=sys.stderr)
        error_msg = str(exc)
    finally:
        if has_hooks and config.hooks.after_each and case_hook_env is not None:
            run_hooks_safe(config.hooks.after_each, env=case_hook_env,
                           cwd=case_ws, log_dir=log_dir,
                           phase_name="after_each", case_id=case_id)

    if last_result is None:
        failed = {
            "exit_code": 1, "duration_s": 0, "token_usage": None,
            "cost_usd": None, "num_turns": None, "per_model_usage": None,
            "per_model_turns": None, "error": error_msg,
            "permission_denials": [],  # case crashed before a result existed
            "steps": step_metrics or None,
            "message_ids": sorted({i for m in (step_metrics or {}).values()
                                   for i in (m.get("message_ids") or [])}),
        }
        # run_result write-site 4: multi-step, failed result with partial steps
        failed = write_run_result(case_output / "run_result.json", failed)
        return case_id, failed

    # Whole-case stdout/stderr = the final step (for collect.py + default judges).
    ws_output = case_ws / "output"
    ws_output.mkdir(parents=True, exist_ok=True)
    if last_result.stdout:
        (ws_output / "stdout.log").write_text(last_result.stdout)
        (case_output / "stdout.log").write_text(last_result.stdout)
    if last_result.stderr:
        (ws_output / "stderr.log").write_text(last_result.stderr)
        (case_output / "stderr.log").write_text(last_result.stderr)

    save_hook_data(case_output, merged_hook_data)
    if input_path.exists() and not input_path.is_symlink():
        shutil.copy2(input_path, case_output / "input.yaml")

    ws_subagents = case_ws / "subagents"
    if ws_subagents.exists() and ws_subagents.is_dir():
        out_subagents = case_output / "subagents"
        out_subagents.mkdir(exist_ok=True)
        for f in ws_subagents.iterdir():
            if f.is_file() and not f.is_symlink() and f.suffix == ".jsonl":
                shutil.copy2(f, out_subagents / f.name)

    case_result = _aggregate_step_metrics(step_metrics)
    case_result["steps"] = step_metrics
    if error_msg:
        case_result["error"] = error_msg
        case_result["exit_code"] = max(case_result.get("exit_code", 0), 1)
    if aborted_at:
        case_result["aborted_at_step"] = aborted_at
    # run_result write-site 5: multi-step, final case result
    case_result = write_run_result(case_output / "run_result.json", case_result)

    print(f"    → {case_id}: {len(step_metrics)} step(s) | "
          f"{case_result['duration_s']:.0f}s | "
          f"{_cost_label(case_result.get('cost_usd'))}", file=sys.stderr)
    return case_id, case_result


def _resolve_git():
    """Lazily resolve git executable path.

    Raises RuntimeError if git is not found. Only called when repo
    verification is actually needed (in-repo mode), not for all executions.
    """
    git_bin = shutil.which("git")
    if not git_bin:
        raise RuntimeError(
            "git executable not found in PATH. In-repo execution mode requires git "
            "for repository state verification."
        )
    return git_bin


def _snapshot_repo_state(repo_root):
    """Capture git status output as baseline for verification."""
    git_bin = _resolve_git()
    result = subprocess.run(
        [git_bin, "status", "--porcelain"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True
    )
    return result.stdout


def _verify_repo_unchanged(repo_root, initial_state):
    """Verify repo is in same state as initial snapshot."""
    current_state = _snapshot_repo_state(repo_root)
    if current_state != initial_state:
        print("WARNING: Repository state changed during execution", file=sys.stderr)
        print(f"Initial state:\n{initial_state}", file=sys.stderr)
        print(f"Current state:\n{current_state}", file=sys.stderr)
        return False
    return True


def _parse_input_overrides(items):
    """Parse repeated ``KEY=VALUE`` strings into a dict (values stay strings)."""
    out = {}
    for item in (items or []):
        if "=" not in item:
            print(f"WARNING: ignoring malformed --input-override {item!r} "
                  "(expected KEY=VALUE)", file=sys.stderr)
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        if key:
            out[key] = value
    return out


def _merge_input_overrides(input_path, overrides):
    """Merge ``overrides`` into a case's input.yaml (overrides win). No-op when
    the file is missing/symlinked/non-mapping."""
    import yaml as _yaml
    if not overrides or not input_path.exists() or input_path.is_symlink():
        return
    try:
        data = _yaml.safe_load(input_path.read_text()) or {}
    except _yaml.YAMLError:
        return
    if not isinstance(data, dict):
        return
    data.update(overrides)
    input_path.write_text(_yaml.safe_dump(data, default_flow_style=False, sort_keys=False))


def _execute_per_case(args, config, runner, runner_cls,
                      output_dir, max_budget, timeout_s,
                      model, mlflow_experiment, system_prompt="",
                      skill_args_template=None, eval_params=None,
                      parallelism=None, effort=None, subagent_model=None,
                      target=None):
    """Execute the skill once per case with case-specific arguments."""
    import yaml as _yaml

    if skill_args_template is None:
        skill_args_template = config.execution.arguments

    workspace = Path(args.workspace)
    case_order_path = workspace / "case_order.yaml"
    if not case_order_path.exists():
        print("ERROR: no case_order.yaml in workspace", file=sys.stderr)
        sys.exit(1)

    with open(case_order_path) as f:
        case_order = _yaml.safe_load(f) or []

    # Merge any --input-override KEY=VALUE into each case's input.yaml before
    # execution, so run-level values (e.g. matrix factor levels) resolve as
    # {KEY} in a cli runner command or {{ input.KEY }} in arguments.
    overrides = _parse_input_overrides(getattr(args, "input_override", None))
    if overrides:
        for entry in case_order:
            cid = entry if isinstance(entry, str) else entry["case_id"]
            _merge_input_overrides(workspace / "cases" / cid / "input.yaml", overrides)

    # Detect in-repo mode: check if first case has _metadata.yaml with mode: in-repo
    in_repo_mode = False
    initial_repo_state = None
    repo_root = None

    if case_order:
        first_case_id = case_order[0] if isinstance(case_order[0], str) else case_order[0]["case_id"]
        first_case_ws = workspace / "cases" / first_case_id
        metadata_path = first_case_ws / "_metadata.yaml"

        if metadata_path.exists():
            with open(metadata_path) as f:
                meta = _yaml.safe_load(f) or {}
                if meta.get("mode") == "in-repo":
                    in_repo_mode = True
                    repo_root = Path(meta.get("repo_cwd", Path.cwd()))
                    initial_repo_state = _snapshot_repo_state(repo_root)
                    print(f"In-repo mode detected | Repo: {repo_root}", file=sys.stderr)

    # Multi-step pipelines run in isolated per-case workspaces; in-repo mode
    # (shared repo cwd) is not supported for them in v1.
    if in_repo_mode and config.execution.steps:
        print("ERROR: execution.steps is not supported in in-repo mode "
              "(steps run in an isolated per-case workspace).", file=sys.stderr)
        sys.exit(1)

    # Force sequential execution for in-repo mode to prevent cross-case contamination
    # (all agents share the same repo root, so parallel writes would corrupt state)
    if in_repo_mode:
        effective_parallelism = 1
        if parallelism and parallelism > 1:
            print("WARNING: Forcing parallelism=1 for in-repo mode (shared repo state)", file=sys.stderr)
    else:
        effective_parallelism = min(parallelism, len(case_order)) if parallelism and parallelism > 1 else 1
    parallel_label = f", parallelism={effective_parallelism}" if effective_parallelism > 1 else ""
    skill_label = (f"/{target}" if target
                   else ("steps" if config.execution.steps else "prompt"))
    print(f"Executing: {skill_label} (per-case, {len(case_order)} cases{parallel_label})",
          file=sys.stderr)
    print(f"Agent: {runner.name} | Model: {model}", file=sys.stderr)

    # Build hook environment
    run_id = args.run_id or ""
    hook_env = build_hook_env(
        workspace=str(workspace),
        run_id=run_id,
        config_path=str(Path(args.config).resolve()),
        project_root=str(Path.cwd()),
        model=model,
        plan=_PROVIDER_PLAN, run_dir=output_dir,
    )
    log_dir = output_dir / "hooks"

    case_results = {}
    wall_clock_start = time.monotonic()

    try:
        # Run before_all hooks and collect outputs
        global_hook_outputs = {}
        if config.hooks.before_all:
            print("Running before_all hooks...", file=sys.stderr)
            run_hooks(config.hooks.before_all, env=hook_env,
                      cwd=Path.cwd(), log_dir=log_dir,
                      phase_name="before_all")
            global_hook_outputs = collect_hook_outputs(workspace)

        if effective_parallelism > 1:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            futures = {}
            # Permissions are identical for every case; resolve once.
            case_permissions = _resolve_permissions(config)
            with ThreadPoolExecutor(max_workers=effective_parallelism) as pool:
                for i, entry in enumerate(case_order, 1):
                    case_id = entry if isinstance(entry, str) else entry["case_id"]
                    case_ws = workspace / "cases" / case_id
                    case_runner = runner_cls.from_config(
                        config,
                        log_prefix=f"eval:{case_id}",
                        subagent_model=subagent_model,
                        mlflow_experiment=mlflow_experiment,
                        mlflow_tracking_uri=config.mlflow.tracking_uri,
                        effort=effort,
                        permissions=case_permissions,
                        **_runner_overrides(),
                    )

                    # Route to in-repo or regular execution based on mode
                    if in_repo_mode:
                        # In-repo mode: agent runs in repo, I/O in case_ws
                        # NO hooks (not yet implemented, see docs/in-repo-hooks-future-enhancement.md)
                        fut = pool.submit(
                            _run_single_case_in_repo, case_runner, target, case_ws,
                            output_dir, skill_args_template, model, mlflow_experiment,
                            config.mlflow.tracking_uri, system_prompt, max_budget, timeout_s,
                            len(case_order), i)
                    else:
                        # Regular isolated workspace mode with hooks
                        fut = pool.submit(
                            _run_single_case, case_runner, target, case_id,
                            case_ws, output_dir, skill_args_template, model,
                            mlflow_experiment, config.mlflow.tracking_uri,
                            system_prompt, max_budget, timeout_s,
                            len(case_order), i,
                            config=config, hook_env=hook_env,
                            global_hook_outputs=global_hook_outputs,
                            subagent_model=subagent_model)
                    futures[fut] = case_id

                for fut in as_completed(futures):
                    case_id, result = fut.result()
                    if result is not None:
                        case_results[case_id] = result
        else:
            for i, entry in enumerate(case_order, 1):
                case_id = entry if isinstance(entry, str) else entry["case_id"]
                case_ws = workspace / "cases" / case_id

                # Route to in-repo or regular execution based on mode
                if in_repo_mode:
                    # In-repo mode: agent runs in repo, I/O in case_ws
                    # NO hooks (not yet implemented, see docs/in-repo-hooks-future-enhancement.md)
                    # Isolate per-case failures (git errors, etc.) so one bad
                    # case is recorded as failed instead of aborting the run.
                    try:
                        case_id, result = _run_single_case_in_repo(
                            runner, target, case_ws, output_dir,
                            skill_args_template, model, mlflow_experiment,
                            config.mlflow.tracking_uri, system_prompt,
                            max_budget, timeout_s, len(case_order), i)
                    except Exception as exc:
                        print(f"    → {case_id}: ERROR ({exc})", file=sys.stderr)
                        result = {
                            "exit_code": 1,
                            "duration_s": 0,
                            "token_usage": None,
                            "cost_usd": None,
                            "permission_denials": [],  # crashed pre-result
                            "num_turns": None,
                            "per_model_usage": None,
                            "per_model_turns": None,
                            "error": str(exc),
                        }
                        case_output = output_dir / "cases" / case_id
                        case_output.mkdir(parents=True, exist_ok=True)
                        result["message_ids"] = []
                        # run_result write-site 6: parallel case exception result
                        result = write_run_result(case_output / "run_result.json", result)
                else:
                    # Regular isolated workspace mode with hooks
                    case_id, result = _run_single_case(
                        runner, target, case_id, case_ws, output_dir,
                        skill_args_template, model, mlflow_experiment,
                        config.mlflow.tracking_uri, system_prompt,
                        max_budget, timeout_s, len(case_order), i,
                        config=config, hook_env=hook_env,
                        global_hook_outputs=global_hook_outputs,
                        subagent_model=subagent_model)
                if result is not None:
                    case_results[case_id] = result
    finally:
        if config.hooks.after_all:
            print("Running after_all hooks...", file=sys.stderr)
            run_hooks_safe(config.hooks.after_all, env=hook_env,
                           cwd=Path.cwd(), log_dir=log_dir,
                           phase_name="after_all")

    wall_clock_s = round(time.monotonic() - wall_clock_start, 1)

    # Final repo verification for in-repo mode
    # Use 'is not None' check to handle empty string baseline (clean repo)
    repo_verification_failed = False
    if in_repo_mode and repo_root and initial_repo_state is not None:
        repo_clean = _verify_repo_unchanged(repo_root, initial_repo_state)
        if not repo_clean:
            print("ERROR: Repository was modified during in-repo execution", file=sys.stderr)
            print("This violates the in-repo mode contract - no repo files should change", file=sys.stderr)
            repo_verification_failed = True
        else:
            print("✓ Repository state verified: no changes detected", file=sys.stderr)

    # Aggregate metrics across cases. Under a provider plan the per-case
    # values are reconciled (never the estimate) and a partial sum is not
    # spend: one unpriced case makes the run total null (null-cost arithmetic).
    total_duration = sum(r["duration_s"] for r in case_results.values())
    estimate_total = None
    if _PROVIDER_PLAN is not None:
        total_cost, cases_priced = aggregate_case_costs(case_results)
        # The runner's own per-case numbers stay visible as one run estimate;
        # reconcile never derives it from an already-reconciled value.
        estimates = [r.get("cost_usd_estimate") for r in case_results.values()
                     if isinstance(r.get("cost_usd_estimate"), (int, float))]
        estimate_total = round(sum(estimates), 4) if estimates else None
    else:
        total_cost, cases_priced = _sum_reported_costs(case_results), None
    total_turns = sum(r.get("num_turns") or 0 for r in case_results.values())
    worst_exit = max((r["exit_code"] for r in case_results.values()), default=0)
    # Ensure exit code reflects repo verification failure
    if repo_verification_failed:
        worst_exit = max(worst_exit, 1)

    agg_tokens = {}
    for r in case_results.values():
        tu = r.get("token_usage") or {}
        for k, v in tu.items():
            if isinstance(v, (int, float)):
                agg_tokens[k] = agg_tokens.get(k, 0) + v

    agg_per_model = {}
    for r in case_results.values():
        pmu = r.get("per_model_usage") or {}
        for m, stats in pmu.items():
            if m not in agg_per_model:
                agg_per_model[m] = {}
            for k, v in stats.items():
                if isinstance(v, (int, float)):
                    agg_per_model[m][k] = agg_per_model[m].get(k, 0) + v

    agg_per_model_turns = {}
    for r in case_results.values():
        pmt = r.get("per_model_turns") or {}
        for m, t in pmt.items():
            if isinstance(t, (int, float)):
                agg_per_model_turns[m] = agg_per_model_turns.get(m, 0) + t

    agg_denials = [d for r in case_results.values()
                   for d in (r.get("permission_denials") or [])]
    run_meta = {
        "exit_code": worst_exit,
        "duration_s": round(total_duration, 1),
        "wall_clock_s": wall_clock_s,
        "cost_usd": round(total_cost, 2) if total_cost is not None else None,
        "token_usage": agg_tokens or None,
        "num_turns": total_turns or None,
        "per_model_usage": agg_per_model or None,
        "per_model_turns": agg_per_model_turns or None,
        "permission_denials": agg_denials,
        "num_cases": len(case_results),
        "model": model,
        "agent": runner.name,
        "agent_version": getattr(runner, "version", ""),
        "execution_mode": config.execution.mode,
        "eval_params": eval_params or {},
        "per_case": case_results,
        "message_ids": sorted({i for r in case_results.values()
                               for i in (r.get("message_ids") or [])}),
    }
    if cases_priced is not None:
        run_meta["cases_priced"] = cases_priced
    if estimate_total is not None:
        run_meta["cost_usd_estimate"] = estimate_total
    # run_result write-site 7: case-mode aggregate
    run_meta = write_run_result(output_dir / "run_result.json", run_meta)
    run_meta = _finish_provider_run(run_meta)
    run_meta, worst_exit = _finalize_strict(output_dir / "run_result.json", run_meta, worst_exit)
    total_cost = run_meta.get("cost_usd")

    print(f"EXIT: {worst_exit}")
    print(f"DURATION: {wall_clock_s:.0f}s wall-clock, {total_duration:.0f}s total")
    print(f"COST: ${total_cost:.2f} total" if isinstance(total_cost, (int, float))
          else "COST: unavailable")
    print(f"CASES: {len(case_results)} "
          f"({sum(1 for r in case_results.values() if r['exit_code'] == 0)} OK, "
          f"{sum(1 for r in case_results.values() if r['exit_code'] != 0)} FAIL)")

    sys.exit(worst_exit)


def _copy_input_files_batch(workspace, output_dir):
    """Copy batch.yaml and case_order.yaml to output dir for MLflow artifact logging."""
    for name in ("batch.yaml", "case_order.yaml"):
        src = workspace / name
        if src.exists() and not src.is_symlink():
            shutil.copy2(src, output_dir / name)


def _save_result(result, args, output_dir, runner, model, eval_params=None):
    """Save batch execution results (stdout, stderr, run_result.json)."""
    if result.stdout:
        (output_dir / "stdout.log").write_text(result.stdout)
    if result.stderr:
        (output_dir / "stderr.log").write_text(result.stderr)

    # Copy subagent transcripts captured by the SubagentStop hook.
    # Only copy regular .jsonl files — reject symlinks (CWE-59).
    ws_subagents = Path(args.workspace) / "subagents"
    if ws_subagents.exists() and ws_subagents.is_dir():
        out_subagents = output_dir / "subagents"
        out_subagents.mkdir(exist_ok=True)
        for f in ws_subagents.iterdir():
            if f.is_file() and not f.is_symlink() and f.suffix == ".jsonl":
                shutil.copy2(f, out_subagents / f.name)

    full_model = result.resolved_model or model
    models_used = result.models_used or []
    # Claude Code annotates the parent model with bracketed suffixes like
    # "[1m]" (the 1M-context variant), but server-side per-message model
    # fields drop the suffix. Compare on the base name so subagents using
    # the same effective model don't get flagged as a distinct subagent.
    def _base(name):
        i = name.find("[")
        return name[:i] if i >= 0 else name
    full_base = _base(full_model)
    subagent_models = [m for m in models_used if _base(m) != full_base]
    subagent_model_str = ", ".join(subagent_models) if subagent_models else full_model

    run_meta = {
        "exit_code": result.exit_code,
        "duration_s": round(result.duration_s, 1),
        "token_usage": result.token_usage,
        "cost_usd": result.cost_usd,
        "per_model_usage": result.per_model_usage,
        "num_turns": result.num_turns,
        "per_model_turns": result.per_model_turns,
        # Denials happen in the runner's stream but were never persisted:
        # a real run's only denial (a workspace-escape attempt) survived
        # solely in raw stdout.log, invisible to every downstream reader.
        "permission_denials": result.permission_denials or [],
        "model": full_model,
        "subagent_model": subagent_model_str,
        "agent": runner.name,
        "agent_version": getattr(runner, "version", ""),
        "execution_mode": "batch",
        "eval_params": eval_params or {},
        "message_ids": result.message_ids or [],
        "cost_source": result.cost_source,
        "error_class": result.error_class,
        "budget": result.budget,
    }
    run_result_path = output_dir / "run_result.json"
    # run_result write-site 8: batch mode result
    run_meta = write_run_result(run_result_path, run_meta)
    run_meta = _finish_provider_run(run_meta)
    run_meta, exit_code = _finalize_strict(run_result_path, run_meta, result.exit_code)

    # Verify the file is valid JSON.

    with open(run_result_path) as f:
        json.load(f)

    print(f"EXIT: {exit_code}")
    print(f"DURATION: {result.duration_s:.0f}s")
    cost = run_meta.get("cost_usd")
    if isinstance(cost, (int, float)):
        print(f"COST: ${cost:.2f}")
    elif result.cost_usd or run_meta.get("cost_source"):
        print("COST: unavailable")
    return exit_code


if __name__ == "__main__":
    main()
