#!/usr/bin/env python3
"""Pre-execution check that state directories and previous runs are clean.

Between eval runs, stale state files in tmp/ can contaminate results.
This script checks for stale state and previous run output, reports
what it finds, and optionally cleans them.

Output paths from eval.yaml are NOT checked here — they are relative
to the workspace (created fresh each run), not the project directory.

Usage:
    # Check only (exit 1 if dirty)
    python3 ${CLAUDE_SKILL_DIR}/scripts/preflight.py --config eval.yaml

    # Check and clean
    python3 ${CLAUDE_SKILL_DIR}/scripts/preflight.py --config eval.yaml --clean

    # Also check if eval/runs/<id> already exists
    python3 ${CLAUDE_SKILL_DIR}/scripts/preflight.py --config eval.yaml --run-id 2026-04-11-opus

    # Also verify a baseline run-id exists (exit 2 if not, lists nearby runs)
    python3 ${CLAUDE_SKILL_DIR}/scripts/preflight.py --config eval.yaml --baseline 2026-04-10-opus
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import argparse
import shutil
import sys
from pathlib import Path

from agent_eval.config import EvalConfig, _validate_path_segment
from agent_eval.run_location import RunLocationError, resolve_run_dir
from agent_eval.run_location import runs_root as _runs_base


# State directories that skills write to between runs
STATE_DIRS = ["tmp"]

# Files directly under a state directory that a skill owns and overwrites
# itself (Step 0 of eval-run, eval-optimize and eval-analyze; eval-mlflow's
# schema mapping). They are the current invocation's state — or a parent
# /eval-optimize loop's — not stale artifacts, so they are neither reported
# nor cleaned.
OWNED_STATE_FILES = frozenset({
    "eval-config.yaml", "optimize-config.yaml",
    "analyze-config.yaml", "schema_mapping.json",
})

# What a run directory directly contains. An explicit --run-dir is only cleaned
# when it carries one, so a runs root or an eval-name directory (which hold only
# run subdirectories) is never removed by mistake.
RUN_MARKERS = ["run_result.json", "summary.yaml", "collection.json",
               "console.log", "execute.pid"]


def _is_dangerous_path(path: Path) -> bool:
    """Reject paths that are too dangerous to clean.

    Blocks the home directory and the filesystem root. Does NOT use
    a parents check (every absolute path has / as a parent).
    """
    resolved = path.resolve()
    return resolved in {Path.home().resolve(), Path("/").resolve()}


def _has_run_marker(path: Path) -> bool:
    """True when *path* is itself a run directory (not a parent of run directories)."""
    return (any((path / m).is_file() for m in RUN_MARKERS)
            or (path / "cases").is_dir())


def _find_files(directory: Path) -> list[Path]:
    """Recursively find all files in a directory."""
    if not directory.exists():
        return []
    if directory.is_file():
        return [directory]
    return sorted(f for f in directory.rglob("*") if f.is_file())


def _is_owned_state(path: Path, state_path: Path) -> bool:
    """True for a skill-owned state file directly under *state_path*."""
    return path.parent == state_path and path.name in OWNED_STATE_FILES


def _remove_files(files: list[Path], root: Path) -> None:
    """Unlink *files*, then prune directories below *root* left empty.

    *root* itself is kept. Only the reported files are removed, so anything
    excluded from the report (the owned state files) survives the clean.
    """
    real_root = root.resolve()
    # Never unlink through a symlinked directory component: a file whose real
    # parent lies outside *root* is left alone.
    files = [f for f in files if f.parent.resolve().is_relative_to(real_root)]
    for f in files:
        f.unlink(missing_ok=True)
    for f in files:
        parent = f.parent
        while parent != root and parent.is_relative_to(root):
            try:
                parent.rmdir()
            except OSError:
                break  # not empty (or already gone along another path)
            parent = parent.parent


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--clean", action="store_true",
                        help="Remove stale state files and previous run output")
    parser.add_argument("--force", action="store_true",
                        help="Skip confirmation prompt (for non-interactive use)")
    parser.add_argument("--run-id", default=None,
                        help="Also check if the run directory "
                             "($AGENT_EVAL_RUNS_DIR/<eval_name>/<id>) already exists")
    parser.add_argument("--run-dir", default=None,
                        help="Check this run directory instead (its basename is the run "
                             "id; a conflicting --run-id is an error). A --baseline "
                             "still resolves under $AGENT_EVAL_RUNS_DIR/<eval_name>/.")
    parser.add_argument("--baseline", default=None,
                        help="Verify the given baseline run-id exists before continuing")
    args = parser.parse_args()

    # Validate run_id / baseline to prevent path traversal (CWE-22)
    if args.run_id is not None:
        _validate_path_segment(args.run_id, "--run-id")
    if args.baseline is not None:
        _validate_path_segment(args.baseline, "--baseline")

    config = EvalConfig.from_yaml(args.config)
    project_root = Path.cwd()
    runs_dir = _runs_base() / config.eval_name()
    # The run directory comes from the resolver every eval-run script shares,
    # so preflight checks exactly the directory execute.py will write.
    run_dir = None
    if args.run_id or args.run_dir:
        try:
            run_dir, args.run_id = resolve_run_dir(config, args.run_id, args.run_dir)
        except RunLocationError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)

    # 0. Baseline existence — fail fast before any expensive setup
    if args.baseline:
        baseline_path = project_root / runs_dir / args.baseline
        if not (baseline_path.is_dir() and any(baseline_path.iterdir())):
            print(f"MISSING_BASELINE: {runs_dir}/{args.baseline}", file=sys.stderr)
            # List nearby runs to help typo recovery
            runs_root = project_root / runs_dir
            if runs_root.is_dir():
                siblings = sorted(p.name for p in runs_root.iterdir() if p.is_dir())
                if siblings:
                    print(f"Available runs in {runs_dir}/:", file=sys.stderr)
                    for s in siblings[-20:]:
                        print(f"  {s}", file=sys.stderr)
                else:
                    print(f"No runs found in {runs_dir}/", file=sys.stderr)
            else:
                print(f"Runs directory does not exist: {runs_dir}/", file=sys.stderr)
            sys.exit(2)

    dirty = {}  # path_label -> list of files

    # 1. State directories (tmp/)
    for state_dir in STATE_DIRS:
        state_path = project_root / state_dir
        # A symlinked state directory would make the scan and the file-granular
        # clean follow the link and unlink files outside the project's state.
        # Refuse before scanning or cleaning anything, even with --force.
        if state_path.is_symlink():
            print(f"ERROR: {state_dir} is a symlink; refusing to scan or clean "
                  f"it. Remove the link by hand or replace it with a real "
                  f"directory.", file=sys.stderr)
            sys.exit(1)
        files = [f for f in _find_files(state_path)
                 if not _is_owned_state(f, state_path)]
        if files:
            dirty[state_dir] = files

    # 2. Previous run output
    run_exists = False
    if args.run_id:
        run_path = project_root / run_dir
        if run_path.exists() and any(run_path.iterdir()):
            run_exists = True

    # Report
    if not dirty and not run_exists:
        print("CLEAN")
        sys.exit(0)

    print("DIRTY")
    total = 0
    for label, files in sorted(dirty.items()):
        count = len(files)
        total += count
        examples = [f.name for f in files[:5]]
        suffix = f" (+{count - 5} more)" if count > 5 else ""
        print(f"  {label}: {count} files — {', '.join(examples)}{suffix}")

    if run_exists:
        run_path = project_root / run_dir
        run_files = _find_files(run_path)
        print(f"  {run_dir}: {len(run_files)} files (previous run)")

    if not args.clean:
        print(f"\n{total} stale files from previous run(s).")
        sys.exit(1)

    # shutil.rmtree refuses a symlinked directory; fail up front, before any
    # state directory has been cleaned, rather than with a traceback halfway.
    if run_exists and (project_root / run_dir).is_symlink():
        print(f"ERROR: {run_dir} is a symlink; refusing to clean it. Pass the "
              f"real directory as --run-dir (or remove the link by hand).",
              file=sys.stderr)
        sys.exit(1)

    # Decide whether the run directory may be removed *before* any file is
    # deleted: cleaning an enclosing state directory removes the run's markers
    # (summary.yaml, ...) and must not turn the run into "not a run directory".
    run_skip_reason = None
    if run_exists and args.run_id:
        run_path = project_root / run_dir
        # An explicit --run-dir can name anything; never remove the project
        # (or a directory containing it) along with the run.
        if (_is_dangerous_path(run_path)
                or project_root.resolve().is_relative_to(run_path.resolve())):
            run_skip_reason = "dangerous"
        elif args.run_dir and run_path.is_dir() and not _has_run_marker(run_path):
            run_skip_reason = "not a run directory"

    # Confirm before cleaning — require --force in non-interactive mode
    targets = sorted(dirty.keys())
    if run_exists:
        targets.append(f"{run_dir}")
    print(f"\nWill clean: {', '.join(targets)}")
    if not args.force:
        if not sys.stdin.isatty():
            print("--clean requires --force in non-interactive mode.",
                  file=sys.stderr)
            sys.exit(1)
        answer = input("Proceed? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            sys.exit(1)

    # Clean
    refused = []
    for label, files in dirty.items():
        path = project_root / label
        if _is_dangerous_path(path):
            print(f"  SKIPPED (dangerous): {label}", file=sys.stderr)
            refused.append(label)
            continue
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            # File-granular: remove what was reported, never the owned state.
            _remove_files(files, path)
        print(f"  cleaned: {label}")

    if run_exists and args.run_id:
        run_path = project_root / run_dir
        if run_skip_reason == "dangerous":
            print(f"  SKIPPED (dangerous): {run_dir}", file=sys.stderr)
            refused.append(str(run_dir))
        elif run_skip_reason == "not a run directory":
            print(f"  SKIPPED (not a run directory): {run_dir} holds none of "
                  f"{', '.join(RUN_MARKERS)} or a cases/ subdirectory",
                  file=sys.stderr)
            refused.append(str(run_dir))
        elif run_path.exists():
            shutil.rmtree(run_path)
            print(f"  cleaned: {run_dir}")
        else:
            # Already removed with an enclosing state directory (e.g. tmp/).
            print(f"  cleaned: {run_dir} (removed with enclosing directory)")

    # A refused target still holds stale artifacts; don't report CLEAN.
    if refused:
        print(f"Not cleaned (refused): {', '.join(refused)}", file=sys.stderr)
        sys.exit(1)

    print("CLEAN")


if __name__ == "__main__":
    main()
