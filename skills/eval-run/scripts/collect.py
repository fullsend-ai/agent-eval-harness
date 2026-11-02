#!/usr/bin/env python3
"""Collect artifacts from workspace and distribute to per-case directories.

Reads output paths from eval.yaml. Maps files to cases using
case_order.yaml (positional: Nth file group → Nth case).

Usage:
    python3 ${CLAUDE_SKILL_DIR}/scripts/collect.py \\
        --config eval.yaml \\
        --workspace /tmp/agent-eval/test-001 \\
        --output eval/runs/test-001
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from agent_eval.config import EvalConfig
from agent_eval.events import (
    DEFAULT_RESULT_CAP, parse_stream_events, merge_subagent_transcripts,
)
from score import _escape_staging_text

# Files/dirs created by the harness infrastructure, not by the skill
_HARNESS_PATHS = {
    ".claude", ".git", ".work", ".staged-plugins", "subagents", "hooks",
    "stdout.log", "stderr.log",
    "run_result.json", "batch.yaml", "case_order.yaml",
}


UNLISTABLE_KEY = "_unlistable"


def _confined(root: Path, path: Path) -> bool:
    """True if *path* sits under *root* with no symlink on the way.

    Decided from the path components before anything is traversed or copied
    (the same order `score.py::_resolve_under` uses): a symlinked output
    root, or a symlinked ancestor of it, would otherwise hand `os.scandir`
    a directory outside the workspace and pull its files into the artifacts.
    An `OSError` while checking counts as "not confined".
    """
    try:
        cur = root
        for part in path.relative_to(root).parts:
            cur = cur / part
            if cur.is_symlink():
                return False
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def _note_lost(results, case_id, rel):
    """Record that *rel* could not be collected for *case_id*.

    `collection.json` is read by `score.py::load_case_record`, which raises on
    a recorded loss. Both the path and the case id come from the agent's
    workspace, so they are escaped before they reach the log
    (`_escape_staging_text`): a newline or a leading ``::`` must not forge a
    log line or a workflow command.
    """
    lost = results.setdefault(case_id, {}).setdefault(UNLISTABLE_KEY, [])
    rel = str(rel)
    if rel not in lost:
        lost.append(rel)
    print(f"  WARNING: {_escape_staging_text(case_id)}: cannot collect "
          f"{_escape_staging_text(rel)} — its contents are not collected",
          file=sys.stderr)


def _walk_output_files(src: Path, root: Path = None):
    """``(files, unlistable)`` under *src*.

    `Path.rglob` does **not** raise on a directory it cannot open: it yields
    the entry and nothing beneath it. The workspace is agent-written, so a
    `chmod 000` on an output sub-directory silently kept its contents out of
    the case directory — where the scoring pass's own listability probe
    (`score.py::_require_listable`, #276) would have caught them. Collection
    runs earlier, so it has to notice.

    Symlinked directories are not descended into, matching what `rglob` did.
    When *root* is given, *src* itself must be confined under it (see
    `_confined`); otherwise nothing is traversed and *src* is reported as
    lost, because evidence the agent routed through a link cannot be
    collected safely.
    """
    if root is not None and not _confined(root, src):
        return [], [src]
    files, unlistable = [], []
    stack = [src]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                children = sorted(entries, key=lambda e: e.name)
        except OSError:
            unlistable.append(directory)
            continue
        for entry in children:
            path = Path(entry.path)
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append(path)
            elif entry.is_file(follow_symlinks=False):
                files.append(path)
    return sorted(files), sorted(unlistable)


def _safe_path_component(value, field):
    """Reject path components that escape the parent directory."""
    p = Path(str(value))
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"{field} must be a relative path without '..': {value}")
    return str(p)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    config = EvalConfig.from_yaml(args.config)
    workspace = Path(args.workspace)
    output_dir = Path(args.output)

    # Per-case mode: outputs are already in case workspaces
    # (Works for both skill and prompt execution in case mode)
    if config.execution.mode == "case":
        _collect_per_case(workspace, output_dir, config)
        return

    # ── Batch mode (below) ───────────────────────────────────────

    # Load case order
    order_path = workspace / "case_order.yaml"
    if not order_path.exists():
        print("ERROR: no case_order.yaml in workspace", file=sys.stderr)
        sys.exit(1)

    with open(order_path) as f:
        case_order = yaml.safe_load(f)

    output_dir.mkdir(parents=True, exist_ok=True)
    results = {}

    # Validate case IDs from case_order
    for entry in case_order:
        cid = entry["case_id"] if isinstance(entry, dict) else entry
        _safe_path_component(cid, "case_id")

    # Collect from each output path defined in config (directory or file)
    for output_cfg in config.outputs:
        out_path = _safe_path_component(output_cfg.path or ".", "output path")
        src = workspace / out_path
        if not src.exists():
            continue

        # Handle single-file output paths
        if src.is_file():
            files = [src]
            src_dir = src.parent
            if not _confined(workspace, src):
                files, unlistable = [], [src]
            else:
                unlistable = []
        else:
            src_dir = src
            # Get all files in the output directory (including subdirs).
            # Not `rglob`: it silently skips a directory it cannot open.
            files, unlistable = _walk_output_files(src_dir, workspace)

        # Batch output reaches cases through `batch_pattern` / grouping, so a
        # directory we could not list cannot be attributed to one case: any
        # file inside it might have mapped to any of them. Record it against
        # EVERY case in case_order. Over-attributing is the safe direction —
        # a false block is recoverable (fix the directory, re-collect), a
        # silent loss is the bug this guards against.
        for d in unlistable:
            for entry in case_order:
                cid = entry["case_id"] if isinstance(entry, dict) else entry
                _note_lost(results, cid, d.relative_to(workspace))

        if not files:
            continue

        if output_cfg.batch_pattern:
            # Batch mode: use the configured pattern to map files to cases
            mapping = _collect_batch(files, case_order, output_cfg.batch_pattern)
        else:
            # Auto-detect mode: group files by common prefix
            groups = _group_files(files, len(case_order))
            mapping = {}
            for i, group in enumerate(groups):
                if i >= len(case_order):
                    break
                entry = case_order[i]
                cid = entry["case_id"] if isinstance(entry, dict) else entry
                mapping[cid] = group

        for case_id, matched_files in mapping.items():
            if not matched_files:
                continue
            case_output = output_dir / "cases" / case_id / out_path
            case_output.mkdir(parents=True, exist_ok=True)

            for src_file in matched_files:
                if not _confined(workspace, src_file):
                    _note_lost(results, case_id,
                               src_file.relative_to(workspace))
                    continue
                # Preserve subdirectory structure relative to src_dir
                rel = src_file.relative_to(src_dir)
                dest = case_output / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dest)

            results.setdefault(case_id, {})[out_path] = len(matched_files)

    # Save collection summary
    with open(output_dir / "collection.json", "w") as f:
        json.dump(results, f, indent=2)

    for case_id, info in sorted(results.items()):
        counts = ", ".join(f"{k}={v}" for k, v in info.items())
        print(f"  {case_id}: {counts}")

    # Generate events.json from run-level stdout.log (shared across all cases)
    if config.traces.events:
        _generate_events_json(workspace, output_dir, config)
        # In batch mode there is no per-case transcript; route each case's own
        # subagent transcripts into a per-case events.json so judges that read
        # {{ conversation }} see that case's reasoning, not the shared whole-batch
        # trace. Unmapped cases keep the run-root fallback.
        # Routing keys on the batch_pattern's prefix (e.g. "RFE-" from
        # "RFE-{n:03d}"). If placeholder outputs disagree on that prefix, one
        # can't be chosen reliably — warn and use the first (unmapped cases
        # still fall back to the run-root trace).
        placeholder_bps = [o.batch_pattern for o in config.outputs
                           if o.batch_pattern and "{" in o.batch_pattern]
        prefixes = {b.split("{", 1)[0] for b in placeholder_bps}
        if len(prefixes) > 1:
            print(f"  WARNING: outputs use different batch-pattern prefixes "
                  f"{sorted(prefixes)}; per-case trace routing uses the first",
                  file=sys.stderr)
        bp = placeholder_bps[0] if placeholder_bps else None
        routed = _route_subagent_events_per_case(
            workspace, output_dir, case_order, bp)
        if routed:
            print(f"  routed per-case subagent traces for {routed} cases")

    if not results:
        print("WARNING: no artifacts collected", file=sys.stderr)


def _collect_per_case(workspace, output_dir, config):
    """Collect artifacts from per-case workspaces.

    In per-case mode, each case has its own workspace at
    workspace/cases/{case_id}/. The skill writes outputs relative to
    its cwd (the case workspace). We scan for output files matching
    the configured output paths and copy them to the run output dir.
    """
    cases_dir = workspace / "cases"
    if not cases_dir.exists():
        print("ERROR: no cases/ directory in workspace", file=sys.stderr)
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)
    results = {}

    for case_dir in sorted(d for d in cases_dir.iterdir() if d.is_dir()):
        case_id = case_dir.name

        for output_cfg in config.outputs:
            if not output_cfg.path:
                continue  # Skip tool-only outputs (no filesystem path)
            out_path = _safe_path_component(output_cfg.path, "output path")
            src = case_dir / out_path

            if not src.exists():
                continue

            if src.is_file():
                if _confined(case_dir, src):
                    files, unlistable = [src], []
                else:
                    files, unlistable = [], [src]
                src_base = src.parent
            else:
                files, unlistable = _walk_output_files(src, case_dir)
                src_base = src
            for d in unlistable:
                _note_lost(results, case_id, d.relative_to(case_dir))

            if not files:
                continue

            case_output = output_dir / "cases" / case_id / out_path
            case_output.mkdir(parents=True, exist_ok=True)

            for f in files:
                # Reject symlinks (CWE-59) and anything that does not resolve
                # under the case root through a link-free path.
                if f.is_symlink():
                    continue
                if not _confined(case_dir, f):
                    _note_lost(results, case_id, f.relative_to(case_dir))
                    continue
                rel = f.relative_to(src_base)
                dest = case_output / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)

            results.setdefault(case_id, {})[out_path] = len(files)

        # Collect files modified in-place during execution
        modified = _collect_modified_files(case_dir, config)
        if modified:
            mod_output = output_dir / "cases" / case_id / "_modified"
            mod_output.mkdir(parents=True, exist_ok=True)
            for rel_path, abs_path in modified:
                dest = mod_output / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(abs_path, dest)
            results.setdefault(case_id, {})["_modified"] = len(modified)

        # Generate events.json from stdout.log
        if config.traces.events:
            _generate_events_json(case_dir, output_dir / "cases" / case_id,
                                  config)

    # Save collection summary
    with open(output_dir / "collection.json", "w") as f:
        json.dump(results, f, indent=2)

    for case_id, info in sorted(results.items()):
        counts = ", ".join(f"{k}={v}" for k, v in info.items())
        print(f"  {case_id}: {counts}")

    if not results:
        print("WARNING: no artifacts collected", file=sys.stderr)


def _subagent_entity_num(transcript_path, prefix):
    """Map a subagent transcript to the entity number it worked on, or None.

    Batch runs assign each subagent an id like ``<prefix><n>`` (the numbering
    the output batch_pattern uses). The opening task message carries it either
    as an explicit ``ID=<prefix><n>`` or as the first ``<prefix><n>`` mention.
    Returns the integer ``<n>``, or None when no id is present — in which case
    the caller leaves that case on the shared run-root trace (never worse than
    the prior behaviour).
    """
    import re
    esc = re.escape(prefix)
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
    except OSError:
        return None
    try:
        content = json.loads(first).get("message", {}).get("content", "")
    except (json.JSONDecodeError, ValueError, AttributeError):
        content = ""
    if isinstance(content, list):
        content = " ".join(
            c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    content = str(content)
    m = (re.search(r'ID=' + esc + r'0*(\d+)', content)
         or re.search(esc + r'0*(\d+)', content))
    return int(m.group(1)) if m else None


def _route_subagent_events_per_case(workspace, output_dir, case_order, batch_pattern):
    """Write a per-case events.json from each case's own subagent transcripts.

    In batch mode there is no per-case transcript, so judges that read
    {{ conversation }} otherwise fall back to the shared whole-batch trace and
    score every case against the same text. This maps each subagent transcript
    to its case via the ``<prefix><n>`` id it processed (same numbering the
    batch_pattern uses) and merges that case's transcripts into
    cases/<case>/events.json, which score.py prefers over the run-root fallback.
    Cases with no mapped transcript are left untouched (they keep the shared
    fallback). Returns the number of cases given a per-case trace.
    """
    subdir = workspace / "subagents"
    if not subdir.is_dir() or not batch_pattern or "{" not in batch_pattern:
        return 0
    prefix = batch_pattern.split("{", 1)[0]
    if not prefix:
        return 0
    from collections import defaultdict
    groups = defaultdict(list)
    total = unmapped = 0
    for tp in sorted(subdir.glob("*.jsonl")):
        total += 1
        n = _subagent_entity_num(tp, prefix)
        if n is not None:
            groups[n].append(tp)
        else:
            unmapped += 1
    if unmapped:
        print(f"  WARNING: {unmapped}/{total} subagent transcripts had no "
              f"'{prefix}<n>' id marker; left on the shared run-root trace",
              file=sys.stderr)
    if not groups:
        return 0

    routed = 0
    batch_idx = 1
    for entry in case_order:
        cid = entry["case_id"] if isinstance(entry, dict) else entry
        count = entry.get("entry_count", 1) if isinstance(entry, dict) else 1
        tps = [t for n in range(batch_idx, batch_idx + count)
               for t in groups.get(n, [])]
        batch_idx += count
        if not tps:
            continue
        case_dir = output_dir / "cases" / _safe_path_component(cid, "case_id")
        case_sub = case_dir / "subagents"
        case_sub.mkdir(parents=True, exist_ok=True)
        for tp in tps:
            shutil.copy2(tp, case_sub / tp.name)
        events = merge_subagent_transcripts([], str(case_sub),
                                            result_cap=DEFAULT_RESULT_CAP)
        dest = case_dir / "events.json"
        fd, tmp_path = tempfile.mkstemp(dir=str(case_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(events, f, indent=2)
            os.rename(tmp_path, str(dest))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        routed += 1
    return routed


def _generate_events_json(case_dir, output_case_dir, config):
    """Parse stdout.log into events.json using atomic write."""
    # In case mode, execute.py writes stdout.log to the output directory,
    # not the workspace. Check there first, fall back to workspace.
    stdout_path = output_case_dir / "stdout.log"
    if not stdout_path.exists():
        stdout_path = case_dir / "stdout.log"
    if not stdout_path.exists():
        output_case_dir.mkdir(parents=True, exist_ok=True)
        dest = output_case_dir / "events.json"
        fd, tmp_path = tempfile.mkstemp(dir=str(output_case_dir), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump([], f)
        os.rename(tmp_path, str(dest))
        return

    try:
        stdout_text = stdout_path.read_text()
    except OSError:
        return

    events = parse_stream_events(stdout_text)

    subagent_dir = case_dir / "subagents"
    if subagent_dir.is_dir():
        events = merge_subagent_transcripts(events, str(subagent_dir),
                                            result_cap=DEFAULT_RESULT_CAP)

    output_case_dir.mkdir(parents=True, exist_ok=True)
    dest = output_case_dir / "events.json"
    fd, tmp_path = tempfile.mkstemp(dir=str(output_case_dir), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(events, f, indent=2)
        os.rename(tmp_path, str(dest))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _collect_modified_files(case_dir, config):
    """Find files modified or created during skill execution.

    Uses git diff against the initial commit (created by workspace.py)
    to detect in-place edits. Filters out harness infrastructure and
    configured output directories.
    """
    git_dir = case_dir / ".git"
    if not git_dir.exists():
        return []

    output_prefixes = {
        tuple(Path(o.path).parts)
        for o in config.outputs if o.path
    }

    try:
        # Modified tracked files
        diff = subprocess.run(
            ["git", "-C", str(case_dir), "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        # New untracked files (excluding harness paths)
        untracked = subprocess.run(
            ["git", "-C", str(case_dir), "ls-files",
             "--others", "--exclude-standard"],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"  {case_dir.name}: no modified files (git diff failed: {e})",
              file=sys.stderr)
        return []

    all_changed = set()
    for line in (diff.stdout + untracked.stdout).splitlines():
        line = line.strip()
        if line:
            all_changed.add(line)

    result = []
    for rel in sorted(all_changed):
        parts = Path(rel).parts
        if not parts:
            continue
        if ".." in parts:
            continue
        if parts[0] in _HARNESS_PATHS:
            continue
        if any(parts[:len(op)] == op for op in output_prefixes):
            continue
        candidate = case_dir / rel
        if candidate.is_symlink():
            continue
        has_symlink_ancestor = False
        for p in candidate.parents:
            if p == case_dir:
                break
            if p.is_symlink():
                has_symlink_ancestor = True
                break
        if has_symlink_ancestor:
            continue
        abs_path = candidate.resolve()
        if not abs_path.is_relative_to(case_dir.resolve()):
            continue
        if abs_path.is_file():
            result.append((rel, abs_path))

    return result


def _collect_batch(files, case_order, batch_pattern):
    """Map files to cases using a batch_pattern from eval.yaml.

    Args:
        files: sorted list of Path objects in the output directory
        case_order: list of {"case_id": str, "entry_count": int} dicts
        batch_pattern: pattern with {n} placeholder (1-based index),
                       or "*" for shared directories

    Returns:
        dict mapping case_id → list of matching Path objects
    """
    # Shared: all files go to every case
    if batch_pattern == "*":
        return {
            (e["case_id"] if isinstance(e, dict) else e): list(files)
            for e in case_order
        }

    result = {}
    batch_idx = 1
    for entry in case_order:
        case_id = entry["case_id"] if isinstance(entry, dict) else entry
        count = entry.get("entry_count", 1) if isinstance(entry, dict) else 1

        matching = []
        for n in range(batch_idx, batch_idx + count):
            try:
                prefix = batch_pattern.format(n=n)
            except (KeyError, ValueError):
                prefix = batch_pattern.replace("{n}", str(n))
            matching.extend(f for f in files if f.name.startswith(prefix))

        if matching:
            result[case_id] = matching
        batch_idx += count

    return result


def _group_files(files, num_cases):
    """Group files into per-case bundles.

    Tries to detect a common ID prefix pattern (e.g., "RFE-001", "TASK-002").
    If found, groups by prefix. Otherwise, distributes one file per case.
    """
    import re

    # Try to find a common prefix pattern: WORD-NNN at start of filename
    prefixes = {}
    for f in files:
        match = re.match(r'^([A-Za-z]+-\d+)', f.stem)
        if match:
            prefix = match.group(1)
            prefixes.setdefault(prefix, []).append(f)

    # If we found prefix groups matching the case count, use them
    if prefixes and len(prefixes) >= num_cases * 0.5:
        return [prefixes[k] for k in sorted(prefixes.keys())]

    # Fallback: one file per case (positional)
    return [[f] for f in files]


if __name__ == "__main__":
    main()
