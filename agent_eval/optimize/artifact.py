"""Engine-agnostic candidate handling: resolve, validate, snapshot, overlay.

Knows nothing about any particular search engine. ``optimize:`` in eval.yaml
is the vocabulary defined here (``candidate``, ``apply_to``, ``feedback``,
``on_error``) — an engine module layers its own additional restrictions on
top of :func:`validate_candidate_block`, it does not redefine these.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from agent_eval.config import EvalConfig, resolve_workspace_source

_ALLOWED_KEYS = {"candidate", "apply_to", "feedback", "on_error"}
_ALLOWED_FEEDBACK = {"redacted"}
_ALLOWED_ON_ERROR = {"noise"}


def relative_path(value: str, label: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"{label} must be a non-empty relative path without '..': {value!r}")
    return path


def validate_candidate_block(raw: dict, config: EvalConfig) -> tuple[dict, list[dict]]:
    """Validate the generic ``optimize:`` vocabulary and resolve its candidate files.

    Returns ``(block, components)`` where each component is
    ``{"dest": "<posix path>", "source": "<resolved absolute path>", "is_dir": bool}``.
    """
    block = raw.get("optimize")
    if not isinstance(block, dict):
        raise ValueError("eval.yaml needs an 'optimize:' mapping to define the optimization target")
    for key in block:
        if key not in _ALLOWED_KEYS:
            raise ValueError(f"unknown optimize key {key!r}; allowed keys: {sorted(_ALLOWED_KEYS)}")
    if raw.get("reward") is None or config.reward is None:
        raise ValueError("optimization requires an explicit eval.yaml 'reward:' composition")

    apply_to = str(block.get("apply_to") or "")
    relative_path(apply_to.rstrip("/"), "optimize.apply_to")
    if block.get("feedback", "redacted") not in _ALLOWED_FEEDBACK:
        raise ValueError(f"optimize.feedback must be one of {sorted(_ALLOWED_FEEDBACK)}")
    if block.get("on_error", "noise") not in _ALLOWED_ON_ERROR:
        raise ValueError(f"optimize.on_error must be one of {sorted(_ALLOWED_ON_ERROR)}")

    entries = block.get("candidate")
    if not isinstance(entries, list) or not entries:
        raise ValueError("optimize.candidate must be a non-empty list of {dest, source} mappings")
    components = []
    destinations = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {"dest", "source"}:
            raise ValueError(f"optimize.candidate[{i}] must contain only 'dest' and 'source'")
        dest = relative_path(str(entry["dest"]).rstrip("/"), f"optimize.candidate[{i}].dest")
        resolved_source = resolve_workspace_source(config, str(entry["source"]))
        if resolved_source is None or not resolved_source.exists():
            raise ValueError(
                f"optimize.candidate[{i}].source is missing or outside project/plugin roots: "
                f"{entry['source']!r}"
            )
        destinations.append(dest)
        components.append({
            "dest": dest.as_posix(), "source": str(resolved_source), "is_dir": resolved_source.is_dir(),
        })

    for i, left in enumerate(destinations):
        for right in destinations[i + 1:]:
            if left == right or left in right.parents or right in left.parents:
                raise ValueError(f"overlapping optimize.candidate destinations: {left} and {right}")
    if not any(
        Path(c["dest"]).name.lower() == "skill.md"
        or (c["is_dir"] and (Path(c["source"]) / "SKILL.md").is_file())
        for c in components
    ):
        raise ValueError(
            "optimize.candidate requires a component named SKILL.md or a skill directory containing one"
        )
    return block, components


def _copy_without_symlinks(source: Path, destination: Path) -> list[Path]:
    """Copy *source* into *destination*, leaving every symlink behind.

    A symlink *inside* the tree is skipped, not fatal. Skill packages in this
    repo carry plumbing symlinks by convention — CLAUDE.md mandates
    ``skills/<skill>/scripts/agent_eval -> ../../../agent_eval`` — so refusing
    one made the harness's own skills unusable as an optimization candidate.
    They are also exactly what must not travel: the snapshot is copied on into
    per-rollout workspaces and into containers, where a link pointing out of
    the tree either dangles or escapes it.

    The *root* being a symlink stays a hard error. That one is an explicit
    ``optimize.candidate.source`` aimed at a link rather than incidental
    plumbing, and silently snapshotting nothing would be the worse failure.

    Returns the symlinks that were skipped, so the caller can report them.
    """
    if source.is_symlink():
        raise ValueError(f"candidate seed source must not be a symlink: {source}")
    skipped: list[Path] = []
    if source.is_dir():
        def _ignore(directory, names):
            dropped = {name for name in names if Path(directory, name).is_symlink()}
            skipped.extend(Path(directory, name) for name in sorted(dropped))
            return dropped

        shutil.copytree(source, destination, ignore=_ignore)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return skipped


def snapshot_candidate(components: list[dict], destination: Path) -> list[str]:
    """Copy each resolved candidate component into *destination* (the seed capability).

    Returns the source-relative paths of any symlinks left behind.
    """
    destination = Path(destination)
    if destination.exists():
        resolved = destination.resolve()
        if resolved in (Path(resolved.root), Path.home().resolve()):
            raise ValueError(f"refusing to delete a protected directory: {resolved}")
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    skipped: list[str] = []
    for component in components:
        source = Path(component["source"])
        for link in _copy_without_symlinks(source, destination / component["dest"]):
            # Named by where it would have landed in the snapshot, so the
            # path means the same thing for every component regardless of
            # where on disk that component was seeded from.
            skipped.append((Path(component["dest"]) / link.relative_to(source)).as_posix())
    return skipped


# ── Overlay: stage a candidate into an isolated rollout workspace ("make it live") ──

def _materialize_link(path: Path) -> None:
    """Replace a workspace symlink with a local copy before overlaying a candidate."""
    if not path.is_symlink():
        return
    target = path.resolve(strict=True)
    path.unlink()
    if target.is_dir():
        shutil.copytree(target, path, symlinks=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, path)


def _reject_symlinks(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"candidate files must not contain symlinks: {path}")
    if path.is_dir():
        for child in path.rglob("*"):
            if child.is_symlink():
                raise ValueError(f"candidate files must not contain symlinks: {child}")


def restore_plumbing(staged_root: Path, prefix: str, plumbing_links: list[dict], mode: str) -> list[Path]:
    """Put back the symlinks every copy on the way into a rollout left out.

    ``plumbing_links`` is intake's list (``intake.plumbing_links``): the
    project-authored symlinks, as project-relative paths, whose target
    resolves inside the project and outside its measurement material.
    Nothing copied into a rollout carries one — ``_copy_tree_local``, the
    candidate snapshot and the runner's own plugin staging all drop or
    materialize links — but the skill still needs what they point at to run
    (this repo's own ``scripts/agent_eval -> ../../../agent_eval`` is the
    import plumbing every skill script depends on).

    *staged_root* mirrors the project directory *prefix* (``""`` for the
    project root itself): a link at project path ``<prefix>/<rest>`` is put
    back at ``<staged_root>/<rest>``; links outside *prefix* are not this
    tree's business. Call it after the candidate overlay, since that
    replaces whole directories.

    ``mode`` is what the agent would have had in a normal ``/eval-run`` of
    the same layout. ``"symlink"`` for the case workspace, whose
    ``.claude/skills`` a normal workspace build symlinks to the project — a
    link is also what ``agent_eval._bootstrap`` needs, since it finds the
    harness venv relative to its own *real* path. ``"copy"`` for a plugin:
    the runner re-stages every plugin into the workspace with
    ``symlinks=False`` (in-plugin targets become content, links that escape
    the plugin are refused — ``claude_code.stage_plugin_dir``), so a link to
    the checkout would be dropped on the way, and content is exactly what a
    normal run of a plugin layout gets; likewise for a tree uploaded into a
    container, where there is no checkout to point at.

    The candidate wins on collision: if the proposer put real content at a
    link's path, or removed the directory that held it, nothing is restored
    there — that is its edit to make.
    """
    if mode not in ("symlink", "copy"):
        raise ValueError(f"plumbing mode must be 'symlink' or 'copy', not {mode!r}")
    staged_root = Path(staged_root)
    # "." is what relative_to() yields for the project root itself.
    base = relative_path(prefix, "plumbing prefix") if prefix not in ("", ".") else Path()
    restored: list[Path] = []
    for link in plumbing_links:
        rel = relative_path(str(link["path"]), "plumbing link")
        if base.parts:
            if not rel.is_relative_to(base):
                continue
            rel = rel.relative_to(base)
        location = staged_root / rel
        if location.exists() or location.is_symlink() or not location.parent.is_dir():
            continue
        target = Path(link["target"])
        if target.is_symlink() or not target.exists():
            continue
        if mode == "symlink":
            location.symlink_to(target, target_is_directory=target.is_dir())
        elif target.is_dir():
            # The same filter every other copy into a rollout applies
            # (_copy_tree_local, the runner's plugin staging): no nested
            # repositories or bytecode caches travel, and no links.
            bulk = shutil.ignore_patterns(".git", "node_modules", "__pycache__")
            shutil.copytree(
                target, location, symlinks=False,
                ignore=lambda directory, names: (
                    set(bulk(directory, names)) | {n for n in names if Path(directory, n).is_symlink()}),
            )
        else:
            shutil.copy2(target, location)
        restored.append(location)
    return restored


def stage_candidate(
    case_workspace: Path,
    candidate_dir: Path,
    apply_to: str,
    components: list[dict],
) -> None:
    """Copy candidate components into a private case workspace.

    A normal workspace build symlinks project skill directories. Any such link
    along ``apply_to`` is detached into a workspace-local copy before candidate
    files are written, so concurrent rollouts never mutate the project.
    Plumbing links are put back afterwards by the caller
    (:func:`restore_plumbing`), since this replaces whole directories.
    """
    root = Path(case_workspace).resolve()
    candidate = Path(candidate_dir).resolve(strict=True)
    if not candidate.is_dir():
        raise ValueError(f"candidate is not a directory: {candidate}")

    target_rel = relative_path(apply_to.rstrip("/"), "optimize.apply_to")
    current = root
    for part in target_rel.parts:
        next_path = current / part
        _materialize_link(next_path)
        if next_path.exists() and not next_path.is_dir():
            raise ValueError(f"candidate destination parent is not a directory: {next_path}")
        next_path.mkdir(parents=True, exist_ok=True)
        if not next_path.resolve().is_relative_to(root):
            raise ValueError(f"candidate destination escapes the case workspace: {next_path}")
        current = next_path

    for component in components:
        rel = relative_path(str(component["dest"]).rstrip("/"), "optimize.candidate.dest")
        src = candidate / rel
        for depth in range(1, len(rel.parts)):
            # A proposer-made link at an intermediate directory would pull
            # files from outside the candidate into the workspace.
            if (candidate / Path(*rel.parts[:depth])).is_symlink():
                raise ValueError(f"candidate files must not contain symlinks: {candidate / Path(*rel.parts[:depth])}")
        if not src.exists():
            raise FileNotFoundError(f"candidate is missing configured component {rel}")
        _reject_symlinks(src)
        dst_parent = current
        for part in rel.parts[:-1]:
            next_path = dst_parent / part
            _materialize_link(next_path)
            if next_path.exists() and not next_path.is_dir():
                raise ValueError(f"candidate component parent is not a directory: {next_path}")
            next_path.mkdir(parents=True, exist_ok=True)
            if not next_path.resolve().is_relative_to(root):
                raise ValueError(f"candidate component escapes the case workspace: {next_path}")
            dst_parent = next_path
        dst = dst_parent / rel.name
        # Each configured component is replaced as a unit so deleted references
        # do not survive from the seed version in the staged workspace.
        _materialize_link(dst)
        if dst.is_dir():
            shutil.rmtree(dst)
        elif dst.exists():
            dst.unlink()
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
