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
import errno
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from agent_eval.config import EvalConfig
from agent_eval.events import (
    DEFAULT_RESULT_CAP, parse_stream_events, merge_subagent_transcripts,
)
from score import UNLISTABLE_KEY, escape_log_text

# Files/dirs created by the harness infrastructure, not by the skill
_HARNESS_PATHS = {
    ".claude", ".git", ".work", ".staged-plugins", "subagents", "hooks",
    "stdout.log", "stderr.log",
    "run_result.json", "batch.yaml", "case_order.yaml",
}

# Every open beneath an agent-written root goes through these flags.
# `O_NOFOLLOW` refuses a symlink as the final component and `O_DIRECTORY` a
# non-directory; opening one component at a time relative to its parent's
# descriptor means no component is ever resolved through a link. `O_NONBLOCK`
# stops a FIFO swapped in for a file from wedging collection — the same
# treatment as `agent_eval/hooks.py::_read_hook_output_file` (#264).
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_ROOT_FLAGS = os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC
_DIR_FLAGS = _ROOT_FLAGS | os.O_NOFOLLOW
_FILE_FLAGS = (os.O_RDONLY | os.O_NOFOLLOW | _CLOEXEC
               | getattr(os, "O_NONBLOCK", 0))


def _open_dir_at(root_fd, parts):
    """A descriptor for the directory *parts* beneath *root_fd*, link-free.

    Each component is opened relative to its parent's descriptor with
    `O_NOFOLLOW | O_DIRECTORY`, so a symlink anywhere on the way — including
    one swapped in after an earlier look — fails the open (`ENOTDIR` /
    `ELOOP`) instead of being followed. Confinement is a property of how the
    path is opened rather than of a check made beforehand, which is what
    closes the window a pathname check leaves before the pathname is reused.
    Raises `OSError`; the caller owns the returned descriptor.
    """
    fd = os.dup(root_fd)
    try:
        for part in parts:
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _locate_at(root_fd, parts):
    """What the declared output *parts* beneath *root_fd* is, before any
    traversal or copy.

    ``None`` when it does not exist. That is decided following links, as
    `Path.exists()` did, so a missing output or a dangling link is still
    "nothing to collect" rather than a loss. Otherwise ``"dir"``, ``"file"``,
    or ``"refused"`` when it exists but cannot be reached link-free: a
    symlinked output root or ancestor, a non-regular file, or a component
    that cannot be opened. The caller records a refused output as lost.
    """
    if not parts:
        return "dir"
    try:
        os.stat(os.path.join(*parts), dir_fd=root_fd)
    except OSError as e:
        if e.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
            return None
        return "refused"
    try:
        parent = _open_dir_at(root_fd, parts[:-1])
    except OSError:
        return "refused"
    try:
        try:
            fd = os.open(parts[-1], _DIR_FLAGS, dir_fd=parent)
        except NotADirectoryError:
            pass  # a file — or a symlink, which Linux reports the same way
        except OSError:
            return "refused"
        else:
            os.close(fd)
            return "dir"
        try:
            fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=parent)
        except OSError:
            return "refused"
        try:
            return "file" if stat.S_ISREG(os.fstat(fd).st_mode) else "refused"
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _dirent_kind(entry):
    """``"dir"``, ``"file"`` or ``None`` (a symlink, or anything else).

    Never follows a link. On a descriptor-based `os.scandir` the type comes
    from the directory entry itself, falling back to an `lstat` relative to
    the same descriptor.
    """
    if entry.is_symlink():
        return None
    if entry.is_dir(follow_symlinks=False):
        return "dir"
    if entry.is_file(follow_symlinks=False):
        return "file"
    return None


def _walk_at(root_fd, parts=()):
    """``(files, unlistable)`` beneath the directory *parts* of *root_fd*,
    as paths relative to *root_fd*.

    `Path.rglob` does **not** raise on a directory it cannot open: it yields
    the entry and nothing beneath it. The workspace is agent-written, so a
    `chmod 000` on an output sub-directory silently kept its contents out of
    the case directory — where the scoring pass's own listability probe
    (`score.py::_require_listable`, #276) would have caught them. Collection
    runs earlier, so it has to notice: a directory that cannot be opened or
    listed is returned in *unlistable*, never dropped.

    Symlinks are neither descended into nor returned, matching what `rglob`
    did. Every directory is opened through `_open_dir_at`, so one swapped
    for a link mid-walk is unlistable rather than followed.
    """
    files, unlistable = [], []
    stack = [tuple(parts)]
    while stack:
        cur = stack.pop()
        try:
            fd = _open_dir_at(root_fd, cur)
            try:
                with os.scandir(fd) as entries:
                    children = sorted((e.name, _dirent_kind(e))
                                      for e in entries)
            finally:
                os.close(fd)
        except OSError:
            unlistable.append(Path(*cur))
            continue
        for name, kind in children:
            if kind == "dir":
                stack.append(cur + (name,))
            elif kind == "file":
                files.append(Path(*cur, name))
    return sorted(files), sorted(unlistable)


def _copy_at(root_fd, parts, dest):
    """Copy the regular file *parts* beneath *root_fd* to *dest*.

    False — nothing copied — when it cannot be opened link-free or is not a
    regular file. The bytes are read from the descriptor that was opened and
    checked with `fstat`, never from a second open by pathname: a swap of the
    file, or of any directory above it, for a symlink before the open is
    refused by the open, and a swap after it changes nothing, because the
    file is already held. Mode and timestamps carry over as `shutil.copy2`
    did.
    """
    try:
        parent = _open_dir_at(root_fd, parts[:-1])
        try:
            fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=parent)
        finally:
            os.close(parent)
    except OSError:
        return False
    with os.fdopen(fd, "rb") as src:
        st = os.fstat(src.fileno())
        if not stat.S_ISREG(st.st_mode):
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
    os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.chmod(dest, stat.S_IMODE(st.st_mode))
    return True


def _note_lost(results, output_dir, case_id, rel):
    """Record that *rel* could not be collected for *case_id*.

    A loss has two halves, and this is the only place either happens:
    the manifest entry, and a `cases/<case_id>` directory under *output_dir*.
    `score.py::_get_case_dirs` enumerates directories, not `collection.json`,
    so a case whose artifacts were all lost would otherwise have no directory
    and leave the denominator instead of counting as incomplete.

    `collection.json` is read by `score.py::load_case_record`, which raises on
    a recorded loss. Both the path and the case id come from the agent's
    workspace, so they are escaped before they reach the log
    (`escape_log_text`): a newline or a leading ``::`` must not forge a
    log line or a workflow command.
    """
    (output_dir / "cases" / case_id).mkdir(parents=True, exist_ok=True)
    lost = results.setdefault(case_id, {}).setdefault(UNLISTABLE_KEY, [])
    rel = str(rel)
    if rel not in lost:
        lost.append(rel)
    print(f"  WARNING: {escape_log_text(case_id)}: cannot collect "
          f"{escape_log_text(rel)} — its contents are not collected",
          file=sys.stderr)


def _walk_output_files(src: Path, root: Path = None):
    """``(files, unlistable)`` under *src*, as paths — `_walk_at` for a
    caller holding a pathname rather than a descriptor.

    When *root* is given, *src* is reached from it link-free, so a symlinked
    *src* or ancestor is reported unlistable and nothing beneath it is
    traversed: evidence the agent routed through a link cannot be collected
    safely.
    """
    src = Path(src)
    base = Path(root) if root is not None else src
    try:
        parts = src.relative_to(base).parts
        base_fd = os.open(base, _ROOT_FLAGS)
    except (OSError, ValueError):
        return [], [src]
    try:
        files, unlistable = _walk_at(base_fd, parts)
    finally:
        os.close(base_fd)
    return [base / f for f in files], [base / d for d in unlistable]


def _collect_outputs(root_fd, outputs, output_dir, results, loss_cases,
                     assign):
    """Collect every declared output beneath one source root.

    The only collection routine. Batch mode (`main`) and per-case mode
    (`_collect_per_case`) differ in their arguments, not in their code, so
    an invariant stated here holds for both — three findings on #291 were
    "the per-case loop had it, the batch loop did not".

    *root_fd*: a descriptor for the source root — the workspace in batch
    mode, ``cases/<id>`` in per-case mode. Everything beneath it is opened
    through `_open_dir_at` / `_copy_at`, never by pathname.

    *loss_cases*: the cases a loss beneath this root is charged to. Per-case,
    the one case. Batch, every case in case_order: output reaches cases
    through `batch_pattern` / grouping, so a directory that could not be
    listed cannot be attributed — any file inside might have mapped to any
    of them. Over-attributing is the safe direction: a false block is
    recoverable (fix the directory, re-collect), a silent loss is the bug.

    *assign(files, output_cfg)*: maps the collected files (paths relative to
    the root) to ``{case_id: [files]}`` — positional / `batch_pattern` for
    batch, one-to-one for per-case.

    Invariants, each held here and nowhere else:

    - a tool-only output (no filesystem path) is skipped;
    - what an output root is, and whether it can be reached link-free, is
      decided before anything beneath it is traversed or copied, and a
      refused root is a loss, not a skip (`_locate_at`);
    - a directory that cannot be listed is recorded, never silently skipped
      (`_walk_at`);
    - nothing is read except through a descriptor opened link-free
      (`_copy_at`), and a file that cannot be is recorded;
    - every loss goes through `_note_lost`, which also keeps the case in
      the run and escapes the agent-controlled path before it is logged.
    """
    for output_cfg in outputs:
        if not output_cfg.path:
            # Tool-only output: no filesystem path. Defaulting it to "."
            # walked the whole root, so an unreadable directory unrelated to
            # any declared output — a private scratch dir — was recorded as
            # lost evidence against every case and blocked the run.
            continue
        out_path = _safe_path_component(output_cfg.path, "output path")
        parts = Path(out_path).parts
        kind = _locate_at(root_fd, parts)
        if kind is None:
            continue
        if kind == "dir":
            files, unlistable = _walk_at(root_fd, parts)
            base = len(parts)
        elif kind == "file":
            files, unlistable = [Path(out_path)], []
            base = len(parts) - 1
        else:
            files, unlistable, base = [], [Path(out_path)], 0

        for d in unlistable:
            for case_id in loss_cases:
                _note_lost(results, output_dir, case_id, d)

        if not files:
            continue

        for case_id, matched_files in assign(files, output_cfg).items():
            if not matched_files:
                continue
            case_output = output_dir / "cases" / case_id / out_path
            case_output.mkdir(parents=True, exist_ok=True)
            for rel in matched_files:
                # Preserve subdirectory structure below the output root.
                dest = case_output.joinpath(*rel.parts[base:])
                if not _copy_at(root_fd, rel.parts, dest):
                    _note_lost(results, output_dir, case_id, rel)
            results.setdefault(case_id, {})[out_path] = len(matched_files)


def _map_batch(files, output_cfg, case_order):
    """Batch `assign` for `_collect_outputs`: the output's `batch_pattern`
    when configured, otherwise prefix grouping mapped positionally."""
    if output_cfg.batch_pattern:
        return _collect_batch(files, case_order, output_cfg.batch_pattern)
    mapping = {}
    for entry, group in zip(case_order, _group_files(files, len(case_order))):
        cid = entry["case_id"] if isinstance(entry, dict) else entry
        mapping[cid] = group
    return mapping


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
    case_ids = []
    for entry in case_order:
        cid = entry["case_id"] if isinstance(entry, dict) else entry
        _safe_path_component(cid, "case_id")
        case_ids.append(cid)

    # The workspace root is the harness's own (`--workspace`), so it is
    # opened as given; everything beneath it is agent-written.
    ws_fd = os.open(workspace, _ROOT_FLAGS)
    try:
        _collect_outputs(
            ws_fd, config.outputs, output_dir, results, case_ids,
            lambda files, output_cfg: _map_batch(files, output_cfg,
                                                 case_order))
    finally:
        os.close(ws_fd)

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

    ws_fd = os.open(workspace, _ROOT_FLAGS)
    try:
        for case_dir in sorted(d for d in cases_dir.iterdir() if d.is_dir()):
            case_id = case_dir.name
            # The case directory is the source root, and it is agent-written
            # too: `is_dir()` follows symlinks, so `cases/<id> -> /outside`
            # reaches here. Opening it link-free from the workspace refuses
            # that (and a `cases` link above it) before anything is read.
            try:
                case_fd = _open_dir_at(ws_fd, ("cases", case_id))
            except OSError:
                # Record it like every other refused path, and keep the case
                # in the run. Warning and moving on would hand the agent a
                # suppression: replace your case directory with a symlink and
                # the unfavourable artifacts are simply never collected —
                # and with no `cases/<id>` for `_get_case_dirs` to find, the
                # case would leave the denominator instead of being marked
                # incomplete.
                _note_lost(results, output_dir, case_id, case_id)
                continue
            try:
                _collect_outputs(
                    case_fd, config.outputs, output_dir, results, [case_id],
                    lambda files, _output_cfg, cid=case_id: {cid: files})

                # Collect files modified in-place during execution. A path
                # `_copy_at` refuses — swapped for a link since the git scan
                # vetted it — is skipped, as the scan skips links itself.
                modified = _collect_modified_files(case_dir, config)
                if modified:
                    mod_output = output_dir / "cases" / case_id / "_modified"
                    mod_output.mkdir(parents=True, exist_ok=True)
                    for rel_path, _abs_path in modified:
                        _copy_at(case_fd, Path(rel_path).parts,
                                 mod_output / rel_path)
                    results.setdefault(case_id, {})["_modified"] = len(modified)
            finally:
                os.close(case_fd)

            # Generate events.json from stdout.log
            if config.traces.events:
                _generate_events_json(case_dir, output_dir / "cases" / case_id,
                                      config)
    finally:
        os.close(ws_fd)

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
