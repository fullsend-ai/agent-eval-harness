"""Lifecycle hook executor for eval pipelines.

Runs user-defined shell commands at well-defined points in the eval
lifecycle (before_all, before_each, after_each, before_scoring, after_all).
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv before 3p imports
import hashlib
import os
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

import yaml

from agent_eval.config import HookEntry


def _hook_label(hook: HookEntry) -> str:
    """One short name for a hook, used by the logs, the exception and the
    `hooks` record — they disagreed before, so a multiline command read
    differently depending on where you saw it."""
    return hook.description or hook.command.split("\n")[0][:60]


class HookError(Exception):
    """Raised when a hook fails and on_failure is 'fail'."""

    def __init__(self, hook: HookEntry, phase: str, exit_code: int,
                 case_id: Optional[str] = None, timed_out: bool = False):
        self.hook = hook
        self.phase = phase
        self.exit_code = exit_code
        self.case_id = case_id
        self.timed_out = timed_out
        label = _hook_label(hook)
        detail = "timed out" if timed_out else f"exit code {exit_code}"
        super().__init__(f"Hook failed in {phase}: {label} ({detail})")


@dataclass
class HookResult:
    """Result of a single hook execution."""
    hook: HookEntry
    phase: str
    case_id: Optional[str]
    exit_code: int
    duration_s: float
    skipped: bool
    timed_out: bool
    log_file: Optional[Path]


def _kill_process(proc: subprocess.Popen):
    """Send SIGTERM to the process group, then SIGKILL if needed."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    except OSError:
        pass


def _run_condition(condition: str, env: dict, cwd: Path) -> bool:
    """Check a hook condition. Returns True if the hook should run."""
    try:
        result = subprocess.run(
            ["bash", "-c", condition],
            env=env, cwd=str(cwd),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=30,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _safe_log_component(value: str) -> str:
    """Sanitize a string for use in log filenames (CWE-22)."""
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)


def _case_component(value: str) -> str:
    """`_safe_log_component` plus a digest, so distinct cases cannot collide.

    Sanitizing alone is many-to-one: `case:a` and `case?a` both become
    `case_a`. That only cost a shared log file before. The drop file is
    read and unlinked, so under `parallelism` two colliding cases would
    take each other's `env`/`data` — or lose it.
    """
    digest = hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()
    return f"{_safe_log_component(value)}-{digest[:8]}"


# A hook's drop file is read back into the agent's environment and into the
# judges' record, so the harness has to be able to tell a hook wrote it. The
# agent's working directory cannot carry that: by the time an `after_each` or
# a second `before_step` runs, the agent has had write access to it. Hooks get
# a path under the run directory instead, which no agent can reach.
HOOK_OUTPUT_ENV = "AGENT_EVAL_HOOK_OUTPUT"

# A drop file holds `env` and `data` mappings. Anything of this size is a
# mistake or an attempt to wedge the reader, and reading it would block the
# case either way.
_MAX_HOOK_OUTPUT_BYTES = 4 * 1024 * 1024


def hook_output_path(log_dir: Path, phase_name: str,
                     case_id: Optional[str] = None) -> Path:
    """Where hooks in this phase write their outputs (`HOOK_OUTPUT_ENV`).

    One file per phase and case, alongside that phase's logs: the last writer
    wins, which is the semantics the shared `.hook-outputs.yaml` already had.

    ABSOLUTE, always. `--output` is routinely relative — the documented
    default is `$AGENT_EVAL_RUNS_DIR`, i.e. `eval/runs` — and the harness gets
    away with that for log files because it opens them itself. A hook does
    not: it runs with the case workspace as its cwd, so a relative path here
    would send `> "$AGENT_EVAL_HOOK_OUTPUT"` into `<case-ws>/eval/runs/...`
    while the harness read the path relative to its own cwd.
    """
    suffix = f".{_case_component(case_id)}" if case_id else ""
    return Path(log_dir).resolve() / f"{phase_name}{suffix}.outputs.yaml"


def _read_hook_output_file(path: Path) -> Optional[dict]:
    """Consume one drop file. ``None`` if there was nothing usable to read.

    Rejects anything that is not a regular file — a symlink would let a
    planted link copy an arbitrary host file into the judges' record, and a
    FIFO would hang the case inside the `finally` that calls this.

    Checked on the open descriptor rather than the path. On the working
    directory fallback the file sits where the agent can still reach it, so
    stat-then-open leaves a window to swap a regular file for a symlink or a
    FIFO in between. `O_NOFOLLOW` refuses the link, `O_NONBLOCK` means a FIFO
    fails or returns immediately instead of blocking, and `fstat` sizes the
    descriptor we actually hold.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(
            os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        if st.st_size > _MAX_HOOK_OUTPUT_BYTES:
            print(f"  hook outputs: {path} is {st.st_size} bytes "
                  f"(limit {_MAX_HOOK_OUTPUT_BYTES}); ignored",
                  file=sys.stderr)
            _unlink_quietly(path)
            return {}
        raw = os.read(fd, _MAX_HOOK_OUTPUT_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    _unlink_quietly(path)
    try:
        content = yaml.safe_load(raw.decode("utf-8")) or {}
    except (yaml.YAMLError, UnicodeDecodeError, ValueError):
        content = {}
    if not isinstance(content, dict):
        # `yaml.safe_load("- a")` is a list, and every caller does
        # `.get("data")` on what comes back.
        print(f"  hook outputs: {path} is not a mapping; ignored",
              file=sys.stderr)
        return {}
    return content


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def run_hooks(
    entries: list[HookEntry],
    env: dict[str, str],
    cwd: Path,
    log_dir: Path,
    phase_name: str,
    case_id: Optional[str] = None,
    results: Optional[list] = None,
) -> list[HookResult]:
    """Run hooks sequentially. Returns results. Raises HookError on failure.

    Args:
        entries: Hook entries to run in order.
        env: Environment variables for the hook process.
        cwd: Working directory for the hook.
        log_dir: Directory to write hook log files.
        phase_name: Hook phase name (e.g., "before_all").
        case_id: Case ID for per-case hooks (used in log filenames).
        results: Optional list to append each HookResult to as it is
            produced, so a caller still sees the hooks that ran (including
            the failing one) when HookError is raised.

    Returns:
        List of HookResult for each hook.

    Raises:
        HookError: If a hook fails and on_failure is "fail".
    """
    if not entries:
        return []

    log_dir.mkdir(parents=True, exist_ok=True)
    if results is None:
        results = []

    # Every entry in the phase shares one drop file, so a hook can hand the
    # harness `env`/`data` without writing into the agent's directory.
    env = {**env, HOOK_OUTPUT_ENV: str(hook_output_path(
        log_dir, phase_name, case_id))}

    for i, hook in enumerate(entries):
        label = _hook_label(hook)
        suffix = f".{_safe_log_component(case_id)}" if case_id else ""
        log_file = log_dir / f"{phase_name}{suffix}.{i}.log"

        if hook.condition:
            if not _run_condition(hook.condition, env, cwd):
                print(f"  hook: {label} ... SKIP (condition)", file=sys.stderr)
                results.append(HookResult(
                    hook=hook, phase=phase_name, case_id=case_id,
                    exit_code=0, duration_s=0.0, skipped=True,
                    timed_out=False, log_file=None,
                ))
                continue

        start = time.monotonic()
        timed_out = False
        exit_code = -1

        try:
            with open(log_file, "w") as log_fh:
                proc = subprocess.Popen(
                    ["bash", "-c", hook.command],
                    env=env, cwd=str(cwd),
                    stdout=log_fh, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    proc.wait(timeout=hook.timeout)
                    exit_code = proc.returncode
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _kill_process(proc)
                    exit_code = -1
        except OSError as e:
            with open(log_file, "a") as log_fh:
                log_fh.write(f"\nHook failed to start: {e}\n")
            exit_code = -1

        duration = round(time.monotonic() - start, 1)

        if timed_out:
            status = f"TIMEOUT ({hook.timeout}s)"
        elif exit_code == 0:
            status = "OK"
        else:
            status = f"FAIL (exit {exit_code})"
        print(f"  hook: {label} ... {status}", file=sys.stderr)

        results.append(HookResult(
            hook=hook, phase=phase_name, case_id=case_id,
            exit_code=exit_code, duration_s=duration,
            skipped=False, timed_out=timed_out, log_file=log_file,
        ))

        failed = timed_out or exit_code != 0
        if failed and effective_on_failure(hook, phase_name) == "fail":
            raise HookError(hook, phase_name, exit_code,
                            case_id=case_id, timed_out=timed_out)

    return results


def run_hooks_safe(
    entries: list[HookEntry],
    env: dict[str, str],
    cwd: Path,
    log_dir: Path,
    phase_name: str,
    case_id: Optional[str] = None,
) -> list[HookResult]:
    """Like run_hooks but never raises — all failures use continue semantics.

    Used for guaranteed phases (after_each, after_all, ...) which must run
    every entry even on failure. Each returned HookResult carries the entry
    as configured (its own ``on_failure``), so a caller can apply that
    setting with a narrower scope than the whole run.
    """
    if not entries:
        return []

    override = [replace(hook, on_failure="continue") for hook in entries]
    results = run_hooks(override, env, cwd, log_dir, phase_name, case_id)
    for result, hook in zip(results, entries):
        result.hook = hook
    return results


def effective_on_failure(hook: HookEntry, phase_name: str) -> str:
    """The `on_failure` in force for `hook` in `phase_name`.

    `after_each` has always run with continue semantics, so leaving it unset
    there has to keep meaning `continue`; everywhere else the default is
    `fail`. Resolving it here rather than in the YAML parser is what keeps a
    `HookEntry` built in Python behaving like the same entry written in
    `eval.yaml` — the parser cannot stamp a phase default onto a dataclass
    whose default has to be phase-independent.
    """
    if hook.on_failure is not None:
        return hook.on_failure
    return "continue" if phase_name == "after_each" else "fail"


def hook_failed(result: HookResult) -> bool:
    """True when a hook ran and exited non-zero or timed out."""
    return not result.skipped and (result.timed_out or result.exit_code != 0)


def describe_hook_failure(result: HookResult, index: int) -> str:
    """One-line summary of a failed hook, e.g.
    ``after_each[1]: Save the PR diff (timed out)``."""
    detail = ("timed out" if result.timed_out
              else f"exit code {result.exit_code}")
    return f"{result.phase}[{index}]: {_hook_label(result.hook)} ({detail})"


def hook_results_record(results_by_phase: dict, base_dir: Path) -> dict:
    """Serialize per-phase HookResult lists for ``run_result.json``.

    Returns ``{"hooks": {phase: [...]}}``, or ``{}`` when no phase produced a
    result. Log paths are relative to *base_dir* (the run directory).

    Deliberately no flattened `hook_failures` list beside it. Which entries
    failed is derivable from these (`hook_failed`), and a judge-facing field
    that names the hook phase would be the third spelling of "this case did
    not produce a usable result", after `incomplete_cases` and the marker
    files evals hand-roll today. That belongs in one shared field, not one
    per subsystem — see issue #277.
    """
    hooks = {}
    for phase, results in results_by_phase.items():
        if not results:
            continue
        entries = []
        for i, r in enumerate(results):
            log = None
            if r.log_file is not None:
                try:
                    log = str(Path(r.log_file).relative_to(base_dir))
                except ValueError:
                    log = str(r.log_file)
            entries.append({
                "description": _hook_label(r.hook),
                "exit_code": r.exit_code,
                "timed_out": r.timed_out,
                "skipped": r.skipped,
                "duration_s": r.duration_s,
                "on_failure": effective_on_failure(r.hook, phase),
                "log": log,
            })
        hooks[phase] = entries
    if not hooks:
        return {}
    return {"hooks": hooks}


def build_hook_env(
    workspace: str,
    run_id: str,
    config_path: str,
    project_root: str,
    model: str,
    case_id: Optional[str] = None,
    case_workspace: Optional[str] = None,
    case_source_dir: Optional[str] = None,
    case_input: Optional[str] = None,
    plan=None,
    run_dir=None,
) -> dict[str, str]:
    """Build environment dict with harness-injected variables.

    Under a provider ``plan`` (spec 014) the hooks learn where the cost ledger
    is and which enforcement level runs, and the provider's own key variables
    are dropped from the copied environment: lifecycle hooks do not talk to
    the provider, and the management key must never reach a subprocess.
    """
    env = dict(os.environ)
    if plan is not None:
        if run_dir is not None:
            env["AGENT_EVAL_COST_LEDGER"] = str(Path(run_dir) / "provider" / "ledger.jsonl")
        env["AGENT_EVAL_PROVIDER"] = plan.kind
        env["AGENT_EVAL_ROUTING_ENFORCEMENT"] = plan.enforcement
        for name in (plan.key_env, getattr(plan, "management_key_env", None)):
            if name:
                env.pop(name, None)
    env["AGENT_EVAL_WORKSPACE"] = workspace
    env["AGENT_EVAL_RUN_ID"] = run_id
    env["AGENT_EVAL_CONFIG"] = config_path
    env["AGENT_EVAL_PROJECT_ROOT"] = project_root
    env["AGENT_EVAL_MODEL"] = model

    if case_id is not None:
        env["CASE_ID"] = case_id
    if case_workspace is not None:
        env["CASE_WORKSPACE"] = case_workspace
    if case_source_dir is not None:
        env["CASE_SOURCE_DIR"] = case_source_dir
    if case_input is not None:
        env["CASE_INPUT"] = case_input

    return env


def collect_hook_outputs(cwd: Path, *, trusted_path: Optional[Path] = None,
                         allow_cwd: bool = True) -> dict:
    """Read and remove a hook's drop file.

    ``trusted_path`` (`hook_output_path`) is read first and is the only
    source an agent cannot write to. ``cwd`` is then searched for the
    historical ``.hook-outputs.yaml``/``.json`` so hooks written before
    `HOOK_OUTPUT_ENV` existed keep working.

    Pass ``allow_cwd=False`` for any phase that runs AFTER an agent turn. The
    working directory is the agent's by then, so a drop file found there may
    be the agent's rather than the hook's — and what comes back from here
    reaches both the next step's environment and the judges.

    Returns the parsed mapping (with 'env' and/or 'data' keys) or {}.
    """
    return collect_hook_outputs_with_source(
        cwd, trusted_path=trusted_path, allow_cwd=allow_cwd)[0]


def collect_hook_outputs_with_source(
        cwd: Path, *, trusted_path: Optional[Path] = None,
        allow_cwd: bool = True) -> tuple[dict, Optional[str]]:
    """`collect_hook_outputs`, plus where the mapping came from.

    ``"trusted"`` or ``"cwd"`` (``None`` when nothing was read). A caller that
    feeds this into something more dangerous than the judges' record — the
    NEXT step's environment, say — can use it to accept `data` from the
    backward-compatible location while refusing `env`.
    """
    if trusted_path is not None:
        content = _read_hook_output_file(Path(trusted_path))
        if content is not None:
            return content, "trusted"
    if not allow_cwd:
        return {}, None
    for name in (".hook-outputs.yaml", ".hook-outputs.json"):
        content = _read_hook_output_file(Path(cwd) / name)
        if content is not None:
            return content, "cwd"
    return {}, None


def save_hook_data(output_dir, data):
    """Save hook output data dict for judges to read later."""
    if not data:
        return
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "hook_outputs.yaml").write_text(
        yaml.dump(data, default_flow_style=False))


def main():
    """CLI entry point for standalone hook invocation."""
    import argparse
    from agent_eval.config import EvalConfig

    parser = argparse.ArgumentParser(
        description="Run lifecycle hooks from eval.yaml")
    parser.add_argument("--config", default="eval.yaml",
                        help="Path to eval.yaml")
    parser.add_argument("--phase", required=True,
                        choices=["before_all", "before_each", "after_each",
                                 "before_scoring", "after_all", "before_report"],
                        help="Hook phase to run")
    parser.add_argument("--workspace", required=True,
                        help="Workspace root path")
    parser.add_argument("--run-id", required=True, help="Run ID")
    parser.add_argument("--model", required=True, help="Skill model")
    parser.add_argument("--output", required=True,
                        help="Output/run directory for hook logs")
    parser.add_argument("--case-id", default=None,
                        help="Case ID (for per-case hooks)")
    parser.add_argument("--case-workspace", default=None,
                        help="Case workspace path (for per-case hooks)")
    parser.add_argument("--case-source-dir", default=None,
                        help="Case source directory (for per-case hooks)")
    args = parser.parse_args()

    config = EvalConfig.from_yaml(args.config)
    entries = getattr(config.hooks, args.phase, [])

    if not entries:
        return

    env = build_hook_env(
        workspace=args.workspace,
        run_id=args.run_id,
        config_path=str(Path(args.config).resolve()),
        project_root=str(Path.cwd()),
        model=args.model,
        case_id=args.case_id,
        case_workspace=args.case_workspace,
        case_source_dir=args.case_source_dir,
        case_input=(str(Path(args.case_workspace) / "input.yaml")
                    if args.case_workspace else None),
    )

    cwd = Path(args.case_workspace) if args.case_workspace else Path.cwd()
    log_dir = Path(args.output) / "hooks"

    phase = args.phase
    # Guaranteed phases run every entry even when one fails. `after_each`
    # joins them: a teardown that stops half way leaves the next case with
    # the previous one's state, and the in-process runner has always run it
    # this way (`run_hooks_safe`). Exit non-zero afterwards if an entry that
    # asked to fail did, so a caller driving the CLI still learns about it.
    if phase in ("after_all", "before_report", "after_each"):
        results = run_hooks_safe(entries, env, cwd, log_dir, phase,
                                 case_id=args.case_id)
        # Only `after_each` reports a failure upwards. `after_all` and
        # `before_report` are documented as "on_failure has no effect", and
        # they exited 0 before this, so making a failing cleanup hook fail
        # the command would be a change nobody asked for.
        failed = [describe_hook_failure(r, i)
                  for i, r in enumerate(results)
                  if phase == "after_each" and hook_failed(r)
                  and effective_on_failure(r.hook, phase) == "fail"]
        if failed:
            print("ERROR: " + "; ".join(failed), file=sys.stderr)
            sys.exit(1)
    else:
        try:
            run_hooks(entries, env, cwd, log_dir, phase,
                      case_id=args.case_id)
        except HookError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
