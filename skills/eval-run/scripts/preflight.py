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


def _is_dangerous_path(path: Path) -> bool:
    """Reject paths that are too dangerous to clean.

    Blocks the home directory and the filesystem root. Does NOT use
    a parents check (every absolute path has / as a parent).
    """
    resolved = path.resolve()
    return resolved in {Path.home().resolve(), Path("/").resolve()}


def _find_files(directory: Path) -> list[Path]:
    """Recursively find all files in a directory."""
    if not directory.exists():
        return []
    if directory.is_file():
        return [directory]
    return sorted(f for f in directory.rglob("*") if f.is_file())


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
        files = _find_files(state_path)
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
            shutil.rmtree(path)
            path.mkdir(parents=True, exist_ok=True)
        print(f"  cleaned: {label}")

    if run_exists and args.run_id:
        run_path = project_root / run_dir
        # An explicit --run-dir can name anything; never remove the project
        # (or a directory containing it) along with the run.
        if (_is_dangerous_path(run_path)
                or project_root.resolve().is_relative_to(run_path.resolve())):
            print(f"  SKIPPED (dangerous): {run_dir}", file=sys.stderr)
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
