"""Benchmark intake: mine an agent-eval-harness project for what an optimization needs.

Engine-agnostic: nothing here knows about any particular search engine. Given a
benchmark checkout (any repo with an ``eval.yaml`` — e.g. rfe-creator) this
resolves, from the benchmark's own files only:

- the candidate: the entry skill named by ``execution.skill`` plus every
  project skill it invokes as a ``/<name>`` slash command (transitively);
- the reward: the benchmark's ``reward:`` block if it has one, otherwise an
  equal-weight composition of its own judges with no boolean gate (the default
  gate composes a boolean-heavy eval to ~0 everywhere — no gradient to climb);
- the runtime resources: the project paths a rollout workspace must carry as
  real copies so the candidate skills' scripts resolve inside the workspace.

The benchmark's files are never modified. When ``eval.yaml`` lacks
``optimize:``/``reward:``, they are written to an ``extends:`` overlay under
``.capevolve/`` so measurement stays declared in eval.yaml vocabulary while the
benchmark checkout stays untouched.

Anything that cannot be resolved from the benchmark is reported as *blocked*
with how to provide it — never guessed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from agent_eval.config import EvalConfig, load_raw

OVERLAY_NAME = "eval.optimize.yaml"
SKILLS_ROOT = Path(".claude") / "skills"
# skills/eval-run/scripts/workspace.py's default project links (``.claude`` is
# linked per subdirectory there, and copied per subdirectory by the rollout).
DEFAULT_RUNTIME_RESOURCES = ("scripts", "CLAUDE.md", ".context", "skills")
# The reserved pairwise judge compares against a baseline run and never
# produces a per-rollout value, so it cannot contribute to a reward.
_NON_REWARD_JUDGES = {"pairwise"}
_MAX_SCAN_BYTES = 1_000_000
_CREDENTIAL_VARS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_BEDROCK", "OPENROUTER_API_KEY",
)


class IntakeBlocked(Exception):
    """A NEEDED input cannot be resolved from the benchmark.

    ``blocked`` is a list of ``{"input", "why", "how"}`` entries — the reason
    the run cannot proceed and how the user can provide the missing input.
    """

    def __init__(self, blocked: list[dict]):
        self.blocked = blocked
        super().__init__("; ".join(f"{b['input']}: {b['why']}" for b in blocked))


def find_eval_config(project_root: Path, explicit: str | Path | None = None) -> Path:
    """The benchmark's eval config: ``explicit``, else ``./eval.yaml``, else the
    one root config the harness's own discovery finds (profiles excluded)."""
    if explicit:
        return Path(explicit).resolve(strict=True)
    candidate = Path(project_root) / "eval.yaml"
    if candidate.is_file():
        return candidate.resolve()
    from agent_eval.config import discover_configs

    found = sorted({d.path.resolve() for d in discover_configs(Path(project_root))})
    if len(found) == 1:
        return found[0]
    raise IntakeBlocked([{
        "input": "eval config",
        "why": (f"no eval config found under {project_root}" if not found else
                "several eval configs: " + ", ".join(str(f) for f in found)),
        "how": "run from the benchmark root or pass --config <path/to/eval.yaml>",
    }])


def overlay_path(project_root: Path) -> Path:
    return Path(project_root) / ".capevolve" / OVERLAY_NAME


# ── Candidate: entry skill + the project skills it invokes ───────────────────

def skill_sources(project_root: Path, config: EvalConfig) -> list[dict]:
    """Every place the runner loads skills from, in precedence order.

    ``{"root": <skill root>, "plugin_dir": <plugin dir or None>}`` — the
    project's ``.claude/skills`` first, then each ``runner.plugin_dirs`` entry's
    skill roots (as the harness itself resolves them).
    """
    from agent_eval.config import resolve_plugin_dir, resolve_plugin_skill_roots

    sources, seen = [], set()
    local = Path(project_root).resolve() / SKILLS_ROOT
    if local.is_dir():
        sources.append({"root": local, "plugin_dir": None})
        seen.add(local)
    for configured in getattr(config.runner, "plugin_dirs", None) or []:
        try:
            plugin = resolve_plugin_dir(config, configured)
            roots = resolve_plugin_skill_roots(plugin)
        except (ValueError, FileNotFoundError):
            continue
        for root in roots:
            if root not in seen:
                sources.append({"root": root, "plugin_dir": plugin})
                seen.add(root)
    return sources


def source_for_apply_to(project_root: Path, config: EvalConfig, apply_to: str) -> dict | None:
    """The skill source ``optimize.apply_to`` names: a skill root the runner
    loads from, or one skill directory inside it. ``None`` when it is neither."""
    project_root = Path(project_root).resolve()
    target = project_root / str(apply_to).rstrip("/")
    sources = skill_sources(project_root, config)
    local = project_root / SKILLS_ROOT
    if not any(s["root"] == local for s in sources):
        sources.insert(0, {"root": local, "plugin_dir": None})
    for source in sources:
        if target == source["root"] or target.parent == source["root"]:
            return source
    return None


def _skill_dirs(sources: list[dict]) -> dict[str, Path]:
    skills: dict[str, Path] = {}
    for source in sources:
        for child in sorted(Path(source["root"]).iterdir()):
            if (child.name not in skills and child.is_dir() and not child.is_symlink()
                    and (child / "SKILL.md").is_file()):
                skills[child.name] = child
    return skills


def _text_files(path: Path):
    paths = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
    for p in paths:
        if p.is_symlink() or ".git" in p.parts:
            continue
        try:
            if p.stat().st_size > _MAX_SCAN_BYTES:
                continue
            yield p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue


def _invokes(text: str, name: str) -> bool:
    """A ``/<name>`` slash command or a ``Skill(skill: "<name>")`` tool call."""
    name = re.escape(name)
    return re.search(
        # A trailing "." ends a sentence ("then /review.") unless a name
        # character follows it ("/rfe.create" is a different skill than "/rfe").
        rf"(?<![\w./-])/{name}(?![\w-]|\.[\w-])|skill[\"']?\s*[:=]\s*[\"']{name}[\"']", text,
    ) is not None


def skill_chain(sources: list[dict], entry: str) -> list[Path]:
    """The entry skill plus every skill it invokes, transitively (skill dirs)."""
    skills = _skill_dirs(sources)
    entry = entry.rsplit(":", 1)[-1]  # a plugin-namespaced "plugin:skill"
    if entry not in skills:
        return []
    chain, queue = [entry], [entry]
    while queue:
        text = "\n".join(_text_files(skills[queue.pop(0)]))
        for name in skills:
            if name not in chain and _invokes(text, name):
                chain.append(name)
                queue.append(name)
    return [skills[name] for name in chain]


def propose_optimize(project_root: Path, config: EvalConfig, entry: str) -> dict:
    project_root = Path(project_root).resolve()
    chain = skill_chain(skill_sources(project_root, config), entry)
    how = f"add an optimize: block (candidate + apply_to) to the eval config or to {OVERLAY_NAME}"
    if not chain:
        raise IntakeBlocked([{
            "input": "capability artifact",
            "why": f"execution.skill {entry!r} is not a skill directory in .claude/skills or "
                   "any runner.plugin_dirs skill root",
            "how": how,
        }])
    roots = {path.parent for path in chain}
    if len(roots) != 1 or not next(iter(roots)).is_relative_to(project_root):
        raise IntakeBlocked([{
            "input": "capability artifact",
            "why": "the skills it invokes span several skill roots or live outside the project: "
                   + ", ".join(str(r) for r in sorted(roots)),
            "how": how,
        }])
    root = next(iter(roots)).relative_to(project_root)
    return {
        "candidate": [{"dest": path.name, "source": (root / path.name).as_posix()} for path in chain],
        "apply_to": root.as_posix(),
        "feedback": "redacted",
        "on_error": "noise",
    }


# ── Reward: the benchmark's own judges ───────────────────────────────────────

def propose_reward(raw: dict) -> dict:
    names = [
        j["name"] for j in raw.get("judges") or []
        if isinstance(j, dict) and j.get("name") and j["name"] not in _NON_REWARD_JUDGES
    ]
    if not names:
        raise IntakeBlocked([{
            "input": "scorer",
            "why": "the eval config defines no judges that produce a per-case value",
            "how": "add judges to the eval config (see /eval-analyze) or a reward: block",
        }])
    return {"formula": "weighted", "weights": {name: 1.0 for name in names}, "gate": False}


# ── Runtime resources: what each rollout workspace carries as real copies ────

def _top_level_name(path: Path, project_root: Path) -> str | None:
    try:
        rel = Path(path).resolve().relative_to(Path(project_root).resolve())
    except ValueError:
        return None
    return rel.parts[0] if rel.parts else None


def runtime_resources(
    project_root: Path, config: EvalConfig, components: list[dict],
) -> tuple[list[str], list[str]]:
    """Project paths a rollout must copy into its workspace.

    The harness's default project links, plus every top-level project
    directory the candidate skills, project settings, or an already-included
    resource reference as a code path (``"types/..."``, ``ROOT / "types"``,
    ``../types/``), to a fixpoint — e.g. a ``types/`` dir a skill script reads
    relative to itself. Measurement material is never included: the dataset
    (gold answers), the run records, every eval config or profile (judge
    prompts and checks) and test suites (judge fixtures) stay out of the
    agent-visible workspace. Names the harness writes into the workspace
    itself (e.g. ``hooks/`` for tool interception) are never overwritten.
    """
    from agent_eval.config import discover_configs
    from agent_eval.workspace_provisioning import DEFAULT_RESERVED_NAMES

    root = Path(project_root).resolve()
    excluded = {".git", ".capevolve", ".claude", "test", "tests", *DEFAULT_RESERVED_NAMES}
    measurement = [config.resolve_path(config.dataset.path),
                   root / os.environ.get("AGENT_EVAL_RUNS_DIR", "eval/runs")]
    measurement += [d.path for d in discover_configs(root, include_profiles=True)]
    # The candidate reaches the run on its own path (staged into the workspace,
    # or a private plugin copy): a copy of the directory holding its seed
    # skills would hand the agent stale files next to the live candidate.
    candidate_homes = [Path(c["source"]) for c in components]
    for path in measurement + candidate_homes:
        name = _top_level_name(path, root)
        if name:
            excluded.add(name)
    # A real /eval-run workspace also links a project's own execution.symlinks
    # entries (spec, workspace.py:_resolve_symlink_names) on top of the
    # defaults. Without reading them here too, a benchmark that declares one
    # would get it in a normal run but silently miss it from every
    # optimization rollout — no error, just a possibly-wrong result against
    # an incomplete workspace.
    configured_symlinks = list(getattr(config.execution, "symlinks", None) or [])
    default_resources = (*DEFAULT_RUNTIME_RESOURCES, *configured_symlinks)
    entries = {
        p.name: p for p in sorted(root.iterdir())
        if p.name not in excluded and p.name in default_resources
        or (p.is_dir() and not p.is_symlink() and not p.name.startswith(".")
            and p.name not in excluded)
    }

    included = [name for name in default_resources if name in entries]
    # Unlike a default entry (silently skipped if the project doesn't have
    # it, matching workspace.py's own behavior), a configured symlink
    # missing here is a configuration mistake that would otherwise hide
    # silently — once per name, not once per rollout.
    warnings = [
        f"execution.symlinks entry {name!r} not found (or excluded as measurement "
        "material) under the project root; every optimization rollout will miss it"
        for name in configured_symlinks if name not in included
    ]
    sources = [Path(c["source"]) for c in components]
    settings = root / ".claude" / "settings.json"
    if settings.is_file():
        sources.append(settings)
    sources += [entries[name] for name in included]
    scanned: set[Path] = set()
    while sources:
        source = sources.pop()
        if source in scanned or not source.exists():
            continue
        scanned.add(source)
        text = "\n".join(_text_files(source))
        for name, path in entries.items():
            if name in included:
                continue
            if re.search(rf"(?:[\"']|\.\./){re.escape(name)}(?=[/\"'])", text):
                included.append(name)
                sources.append(path)
    return included, warnings


# ── Overlay + full intake ────────────────────────────────────────────────────

def write_overlay(project_root: Path, config_path: Path, additions: dict) -> Path:
    path = overlay_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"extends": os.path.relpath(Path(config_path).resolve(), path.parent.resolve()), **additions}
    path.write_text(
        "# Generated by agent-eval-capevolve intake. Layers the optimization target over\n"
        "# the benchmark's eval config without modifying it. Edit freely: an existing\n"
        "# overlay is reused as-is (pass --reintake to regenerate it).\n"
        + yaml.safe_dump(body, sort_keys=False),
        encoding="utf-8",
    )
    return path


def detected_credentials() -> list[str]:
    return [name for name in _CREDENTIAL_VARS if os.environ.get(name)]


def run_intake(project_root: Path, config_path: Path, *, reintake: bool = False) -> dict:
    """Resolve the effective eval config for optimization.

    Returns ``{"config_path", "base_config", "overlay", "defaulted", "reused_overlay"}``
    where ``config_path`` is the file the harness should load (the overlay when
    one is needed, else the benchmark's own eval config) and ``defaulted`` lists
    the inputs intake filled in rather than read from the benchmark.
    """
    project_root = Path(project_root).resolve()
    config_path = Path(config_path).resolve(strict=True)
    overlay = overlay_path(project_root)
    if config_path == overlay.resolve():
        return {"config_path": config_path, "base_config": config_path, "overlay": None,
                "defaulted": [], "reused_overlay": True}

    # The benchmark's own blocks always win: the overlay only ever supplies
    # what the eval config lacks, so a block added to the eval config after an
    # earlier intake is never shadowed by that intake's proposal.
    raw, _chain = load_raw(config_path)
    missing = [key for key, present in (("optimize", isinstance(raw.get("optimize"), dict)),
                                        ("reward", raw.get("reward") is not None)) if not present]
    if not missing:
        overlay.unlink(missing_ok=True)
        return {"config_path": config_path, "base_config": config_path, "overlay": None,
                "defaulted": [], "reused_overlay": False}

    kept = {}
    if overlay.is_file() and not reintake:
        try:
            layered, chain = load_raw(overlay)
        except (OSError, ValueError):  # its base moved or it no longer parses: regenerate
            layered, chain = {}, []
        # An overlay layered over a different eval config (e.g. an earlier run
        # without --config, or another profile) would silently swap the
        # models, judges, or dataset under test: re-point it, keeping edits.
        # chain is root first, the overlay last, so its own base is chain[-2].
        stale_base = len(chain) < 2 or Path(chain[-2]).resolve() != config_path
        shadows = any(key not in missing and layered.get(key) != raw.get(key)
                      for key in ("optimize", "reward"))
        if not stale_base and not shadows:
            return {"config_path": overlay.resolve(), "base_config": config_path,
                    "overlay": str(overlay), "defaulted": [], "reused_overlay": True}
        # Same base: drop the blocks the eval config now declares and keep any
        # edits to the rest (the base lacks those keys, so their merged value is
        # the overlay's). A different base (another suite or profile) has its own
        # judges and skills, so its blocks are proposed afresh, never carried over.
        if not stale_base:
            kept = {key: layered[key] for key in missing if key in layered}

    additions, defaulted = dict(kept), []
    if "optimize" in missing and "optimize" not in kept:
        entry = ((raw.get("execution") or {}).get("skill") or raw.get("skill") or "")
        if not entry:
            raise IntakeBlocked([{
                "input": "target agent",
                "why": "the eval config has no execution.skill (prompt mode is not optimizable here)",
                "how": "optimize a skill eval: set execution.skill in the eval config",
            }])
        additions["optimize"] = propose_optimize(project_root, EvalConfig.from_yaml(config_path), entry)
        defaulted.append("optimize")
    if "reward" in missing and "reward" not in kept:
        additions["reward"] = propose_reward(raw)
        defaulted.append("reward")
    written = write_overlay(project_root, config_path, additions)
    return {"config_path": written.resolve(), "base_config": config_path, "overlay": str(written),
            "defaulted": defaulted, "reused_overlay": False}
