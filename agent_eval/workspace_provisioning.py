"""Materialize shared ``dataset.workspace.files`` entries into a workspace.

A ``WorkspaceFile`` (``{dest, source}``) entry references a project or plugin
resource that must be *copied* — never symlinked — into every case workspace.
This is the single implementation of that materialization, shared by the local
``/eval-run`` path, Harbor task packaging, and the S3/EvalHub export step, so the
trust boundary and copy semantics stay identical across substrates.

Plain string entries are per-case files handled by the case-directory copy and
are ignored here.
"""

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


def iter_shared_files(config):
    """Yield the ``WorkspaceFile`` entries declared on *config*."""
    ds = getattr(config, "dataset", None)
    ws = getattr(ds, "workspace", None) if ds is not None else None
    for entry in getattr(ws, "files", None) or []:
        if isinstance(entry, WorkspaceFile):
            yield entry


def dataset_protected_paths(config):
    """The eval dataset directory, resolved: answer keys the agent must not see.

    ``[]`` when ``dataset.path`` is unset or empty. An empty path would resolve
    to the config directory, which is not a dataset: protecting it would drop
    every shared source and link under the project, so no protection applies.
    """
    dataset = getattr(config, "dataset", None)
    path = getattr(dataset, "path", "") if dataset is not None else ""
    if not path:
        return []
    return [config.resolve_path(path).resolve()]


def _is_protected(path, protected):
    """*path* (resolved) is, or lies inside, a protected path."""
    return any(path == p or path.is_relative_to(p) for p in protected)


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
            def ignore(dirpath, names):
                skip = _ignore_symlinks(dirpath, names)
                skip |= {
                    n for n in names
                    if _is_protected((Path(dirpath) / n).resolve(), protected)
                }
                return skip

            shutil.copytree(resolved, dst, ignore=ignore, dirs_exist_ok=True)
        else:
            shutil.copy2(resolved, dst)
