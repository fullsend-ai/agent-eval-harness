"""Materialize shared ``dataset.workspace.files`` entries into a workspace.

A ``WorkspaceFile`` (``{dest, source}``) entry references a project or plugin
resource that must be *copied* — never symlinked — into every case workspace.
This is the single implementation of that materialization, shared by the local
``/eval-run`` path, Harbor task packaging, and the S3/EvalHub export step, so the
trust boundary and copy semantics stay identical across substrates.

Plain string entries are per-case files handled by the case-directory copy and
are ignored here.
"""

import hashlib
import os
import shutil
import sys
from pathlib import Path

from agent_eval.config import WorkspaceFile, resolve_workspace_source

# Names the harness owns inside a case workspace (local) or a Harbor
# ``environment/`` dir; a shared file must never clobber them.
DEFAULT_RESERVED_NAMES = frozenset(
    {
        "input.yaml",
        "input.yml",
        "input.json",
        "answers.yaml",
        "annotations.yaml",
        "batch.yaml",
        ".claude",
        "hooks",
        "tool_handlers.yaml",
    }
)


def _warn_skip(dest, reason):
    print(
        f"WARNING: skipping shared workspace.files entry {dest!r}: {reason}",
        file=sys.stderr,
    )


def _ignore_symlinks(dirpath, names):
    """copytree ignore callback: drop symlinks so a nested link in a source
    directory cannot materialize a host file (CWE-59)."""
    base = Path(dirpath)
    return {name for name in names if (base / name).is_symlink()}


def _make_ignore(protected):
    """copytree ignore callback: drop symlinks and protected paths.

    A symlink is dropped without being resolved (resolving a cyclic link
    raises on Python 3.11/3.12), and nothing is resolved at all when there is
    nothing to protect.
    """
    def ignore(dirpath, names):
        skip = _ignore_symlinks(dirpath, names)
        if protected:
            skip |= {
                n for n in names
                if n not in skip and _is_protected(_real(Path(dirpath) / n), protected)
            }
        return skip

    return ignore


def iter_shared_files(config):
    """Yield the ``WorkspaceFile`` entries declared on *config*."""
    ds = getattr(config, "dataset", None)
    ws = getattr(ds, "workspace", None) if ds is not None else None
    for entry in getattr(ws, "files", None) or []:
        if isinstance(entry, WorkspaceFile):
            yield entry


# A subdirectory holding any of these is a case (input) or its grading material.
_CASE_MARKERS = (
    "input.yaml", "input.yml", "input.json", "answers.yaml", "annotations.yaml",
)


def dataset_root(config):
    """The directory case discovery scans, as configured (not symlink-resolved).

    Harbor task generation and ``workspace.py`` both list the immediate
    subdirectories of ``config.resolve_path(dataset.path)``; an empty
    ``dataset.path`` resolves to the config directory.
    """
    dataset = getattr(config, "dataset", None)
    path = getattr(dataset, "path", "") if dataset is not None else ""
    return Path(os.path.abspath(config.resolve_path(path or "")))


def dataset_protected_paths(config, *, resolved=True):
    """The eval dataset, resolved: answer keys the agent must not see.

    With ``dataset.path`` set, the dataset directory. With it empty, case
    discovery scans the config directory, so protection covers what discovery
    finds there: each immediate subdirectory holding case files
    (``input.*``, ``answers.yaml``, ``annotations.yaml``). The config
    directory itself is not protected: it is typically the project root, and
    protecting it would drop every shared source and skill root under it.

    ``resolved=False`` returns the paths as spelled, without following links.
    """
    fix = (lambda p: p.resolve()) if resolved else (lambda p: p)
    dataset = getattr(config, "dataset", None)
    path = getattr(dataset, "path", "") if dataset is not None else ""
    root = dataset_root(config)
    if path:
        return [fix(root)]
    if not root.is_dir():
        return []
    return [
        fix(d) for d in sorted(root.iterdir())
        if d.is_dir() and any((d / m).is_file() for m in _CASE_MARKERS)
    ]


def _is_protected(path, protected):
    """*path* (resolved) is, or lies inside, a protected path."""
    return any(path == p or path.is_relative_to(p) for p in protected)


def _real(path):
    """``os.path.realpath`` as a Path; never raises on a symlink cycle."""
    return Path(os.path.realpath(path))


# Files a sandboxed case legitimately carries from its dataset directory.
_CASE_INPUT_NAMES = frozenset(
    {"input.yaml", "input.yml", "input.json", "answers.yaml"})


def _file_digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dataset_fingerprints(protected_paths, exclude=()):
    """``(basename, sha256)`` of every non-empty dataset file.

    The case input files a sandbox is meant to receive are left out, and so is
    anything under *exclude* (the tree being checked, which may sit inside a
    broad dataset root). Used to spot a *copy* of an answer key: a path check
    cannot, once the bytes have been copied elsewhere.
    """
    prints = set()
    excluded = [_real(p) for p in exclude]
    for root in protected_paths:
        root = Path(root)
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if not _is_protected(_real(Path(dirpath) / d), excluded)]
            for name in filenames:
                path = Path(dirpath) / name
                if (name in _CASE_INPUT_NAMES or path.is_symlink()
                        or not path.is_file() or path.stat().st_size == 0):
                    continue
                prints.add((name, _file_digest(path)))
    return prints


def find_dataset_copies(tree, fingerprints):
    """Regular files under *tree* that are byte-identical copies of dataset files."""
    found = []
    if not fingerprints:
        return found
    names = {name for name, _ in fingerprints}
    for dirpath, dirnames, filenames in os.walk(tree):
        for name in filenames:
            if name not in names:
                continue
            path = Path(dirpath) / name
            if (name in _CASE_INPUT_NAMES or path.is_symlink()
                    or not path.is_file() or path.stat().st_size == 0):
                continue
            if (name, _file_digest(path)) in fingerprints:
                found.append(path)
    return found


def materialize_shared_files(
    target_dir, config, *, reserved_names=DEFAULT_RESERVED_NAMES,
    protected_paths=()
):
    """Copy each ``{dest, source}`` workspace.files entry into *target_dir*.

    For every :class:`~agent_eval.config.WorkspaceFile`, the ``source`` is
    resolved and containment-checked via ``resolve_workspace_source``; a source
    that is missing, dangling, or escapes the project/plugin roots is skipped
    with a warning. A ``dest`` whose leading component collides with a
    harness-reserved name is also skipped, so a shared file can never overwrite
    ``input.yaml``, ``answers.yaml``, or the generated ``.claude/`` settings.

    Directory sources are copied recursively with nested symlinks dropped (a
    single-file ``SKILL.md`` is the common case). The result is always a real
    file or directory, never a symlink, so it ports to containers and object
    storage.

    ``protected_paths`` (the dataset, for sandboxed substrates) never reach
    *target_dir*: a source inside one is skipped with a warning, and a
    directory source that contains one is copied without it.
    """
    target_dir = Path(target_dir)
    protected = [Path(p).resolve() for p in protected_paths]
    ignore = _make_ignore(protected)
    for entry in iter_shared_files(config):
        dest_rel = Path(entry.dest)
        # Defense in depth for programmatically built WorkspaceFiles that bypass
        # config-load validation: an empty/root, absolute, or traversing dest
        # would resolve to (or outside) the workspace root and clobber it.
        if (not dest_rel.parts or dest_rel.is_absolute()
                or ".." in dest_rel.parts):
            _warn_skip(entry.dest, "dest is not a named relative path")
            continue
        if dest_rel.parts[0] in reserved_names:
            _warn_skip(
                entry.dest,
                f"dest collides with a reserved name ({dest_rel.parts[0]})",
            )
            continue
        resolved = resolve_workspace_source(config, entry.source)
        if resolved is None:
            _warn_skip(
                entry.dest,
                f"source {entry.source!r} is missing or resolves outside "
                f"the project/plugin roots",
            )
            continue
        if _is_protected(resolved, protected):
            _warn_skip(entry.dest, "source is inside the protected eval dataset")
            continue
        dst = target_dir / dest_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        # Guard against a destination inside the source (e.g. source: "." with an
        # export dir under the project root): copytree would copy dst into itself,
        # recursing until disk/path limits are hit (CWE-400).
        resolved_dst = dst.resolve()
        if resolved_dst == resolved or (
            resolved.is_dir() and resolved_dst.is_relative_to(resolved)
        ):
            _warn_skip(entry.dest, "destination is inside the source (recursive copy)")
            continue
        if resolved.is_dir():
            shutil.copytree(resolved, dst, ignore=ignore, dirs_exist_ok=True)
        else:
            shutil.copy2(resolved, dst)
