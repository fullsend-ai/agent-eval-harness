"""Evaluation suite configuration loaded from eval.yaml files."""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv (module doubles as `-m` entry point)

import copy
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union
import sys

import yaml

from agent_eval.providers.openrouter.routing import RoutingSpec, RoutingTable


# --- Config overlay: `extends:` and the single raw loader (spec 014 PR-3a) -----
#
# `extends: <path relative to the file>` layers an eval config (a *profile*)
# over a base config. `load_raw` is the ONLY place that resolves it, so every
# reader — execute/score/report, Harbor task bundling, EvalHub, validate,
# discovery — sees the merged mapping and the same chain. Merge policy
# (`deep_merge(dedupe=True)`): dicts merge, scalars override, scalar lists
# extend with dedupe (base first, order kept), lists of mappings keyed by
# `name` (judges) or `id` (execution.steps) merge by key, other lists of
# mappings extend by equality, and a `!replace`-tagged list replaces the base
# list outright. `runner.settings` keeps its historical policy
# (`dedupe=False`: plain extend) through the same function.

MAX_EXTENDS_DEPTH = 8
# `id` first: `execution.steps` entries carry BOTH `id` and `name`, and `id`
# is the identity (the uniqueness constraint and the path component). Judges
# declare no `id`, so they fall through to `name`.
_LIST_MERGE_KEYS = ("id", "name")


class _ReplaceList(list):
    """A list tagged `!replace` in YAML: replaces the base value instead of
    merging with it. Stripped back to a plain list once the merge is done."""


class _ConfigLoader(yaml.SafeLoader):
    """SafeLoader plus the `!replace` tag (list values only)."""


def _construct_replace(loader, node):
    if not isinstance(node, yaml.SequenceNode):
        raise yaml.constructor.ConstructorError(
            None, None, "!replace applies to list values only", node.start_mark)
    return _ReplaceList(loader.construct_sequence(node, deep=True))


_ConfigLoader.add_constructor("!replace", _construct_replace)


def _read_config_mapping(path: Path) -> dict:
    with open(path) as f:
        # SafeLoader subclass (safe_load semantics + the `!replace` tag),
        # driven explicitly rather than through yaml.load().
        loader = _ConfigLoader(f)
        try:
            raw = loader.get_single_data() or {}
        finally:
            loader.dispose()
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid eval config (not a YAML mapping): {path}")
    return raw


def _strip_replace_markers(value):
    if isinstance(value, _ReplaceList):
        return [_strip_replace_markers(v) for v in value]
    if isinstance(value, list):
        return [_strip_replace_markers(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip_replace_markers(v) for k, v in value.items()}
    return value


def _list_merge_key(items):
    """`name`/`id` when every item is a mapping carrying that non-empty string key."""
    if not items or not all(isinstance(i, dict) for i in items):
        return None
    for key in _LIST_MERGE_KEYS:
        if all(isinstance(i.get(key), str) and i.get(key) for i in items):
            return key
    return None


def _merge_lists_dedupe(base, overlay):
    key = _list_merge_key([*base, *overlay])
    if key:
        out = [copy.deepcopy(item) for item in base]
        index = {item[key]: i for i, item in enumerate(out)}
        for item in overlay:
            if item[key] in index:
                deep_merge(out[index[item[key]]], item, dedupe=True)
            else:
                index[item[key]] = len(out)
                out.append(copy.deepcopy(item))
        return out
    out = list(base)
    for item in overlay:
        if item not in out:
            out.append(item)
    return out


def deep_merge(dst, src, *, dedupe=False):
    """Recursively merge ``src`` into ``dst`` (in place; returns ``dst``).

    Dicts merge and scalars override on both policies. Lists: ``dedupe=False``
    extends in place (the ``runner.settings`` policy — see runner.md);
    ``dedupe=True`` is the ``extends:`` overlay policy — scalar lists extend
    with dedupe, lists of mappings keyed by ``name``/``id`` merge by key (a
    same-keyed entry deep-merges over the base, new keys append), other lists
    of mappings extend by equality. A ``!replace``-tagged list replaces the
    base value whole on either policy. Note the contrast with a provider
    ``RoutingSpec`` merge, where lists always replace.
    """
    for k, v in src.items():
        cur = dst.get(k)
        if isinstance(v, _ReplaceList):
            dst[k] = _strip_replace_markers(v)
        elif isinstance(v, dict) and isinstance(cur, dict):
            deep_merge(cur, v, dedupe=dedupe)
        elif isinstance(v, list) and isinstance(cur, list):
            if dedupe:
                dst[k] = _merge_lists_dedupe(cur, v)
            else:
                cur.extend(v)
        else:
            dst[k] = v
    return dst


def load_raw(path) -> tuple[dict, list[str]]:
    """The single raw eval-config loader.

    Resolves ``extends:`` against the file's own directory (recursively, cycle
    detection, depth <= MAX_EXTENDS_DEPTH), deep-merges the overlay over its
    base with the ``extends:`` policy and returns ``(merged_mapping, chain)``:
    the mapping has no ``extends`` key and no ``!replace`` markers; ``chain``
    lists the resolved file paths root first (the base, then each overlay).
    Every reader of an eval config goes through here (pinned by
    tests/test_config_raw_readers.py).
    """
    return _load_chain(Path(path), (), 0)


def _load_chain(path: Path, seen: tuple, depth: int) -> tuple[dict, list[str]]:
    resolved = path.resolve()
    if resolved in seen:
        cycle = " -> ".join(str(p) for p in (*seen, resolved))
        raise ValueError(f"extends: cycle detected: {cycle}")
    if depth > MAX_EXTENDS_DEPTH:
        raise ValueError(
            f"extends: chain deeper than {MAX_EXTENDS_DEPTH} at {path}")
    if not resolved.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    raw = _read_config_mapping(resolved)
    base_ref = raw.pop("extends", None)
    if base_ref is None:
        return _strip_replace_markers(raw), [str(resolved)]
    if not isinstance(base_ref, str) or not base_ref.strip():
        raise ValueError(f"{path}: 'extends' must be a path string relative to the file")
    if Path(base_ref).is_absolute():
        raise ValueError(
            f"{path}: 'extends' must be relative to the file, not an absolute "
            f"path (keeps the chain portable across checkouts and containers)")
    base_path = resolved.parent / base_ref.strip()
    if not base_path.exists():
        raise FileNotFoundError(
            f"{path}: 'extends: {base_ref}' → {base_path} does not exist")
    base, chain = _load_chain(base_path, (*seen, resolved), depth + 1)
    merged = deep_merge(base, raw, dedupe=True)
    return _strip_replace_markers(merged), [*chain, str(resolved)]


def project_relative(path) -> str:
    """``path`` relative to the project root (CWD) when it lies inside it,
    else absolute — the form recorded in ``config_chain``."""
    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(resolved)


def config_chain_display(chain) -> list[str]:
    return [project_relative(p) for p in chain]


def resolve_arguments(
    template: str, input_data: dict, steps: Optional[dict] = None
) -> str:
    """Resolve a skill/prompt argument template against input.yaml data.

    ``steps`` (optional) binds the ``{{ steps.<id>.* }}`` namespace for
    multi-step execution — the accumulated results of earlier steps in the same
    case (Jinja2 style only; the brace style resolves ``input`` fields only).

    Two mutually-exclusive placeholder styles are auto-detected:

    - Jinja2 (``{{ input.field }}`` / ``{% ... %}``): rendered with ``input``
      bound to the case data.  Uses ``StrictUndefined`` so a missing required
      field raises ``ValueError`` rather than silently rendering empty.  For
      genuinely optional fields use ``{{ input.get('field', '') }}`` or the
      ``| default('')`` filter.
    - Brace (``{field}`` / ``{field?}``): ``{field}`` is required (raises
      ``KeyError`` if missing); ``{field?}`` is optional (omitted if missing).
    """
    if not template:
        return ""

    if "{{" in template or "{%" in template:
        from jinja2 import StrictUndefined, Template
        from jinja2 import UndefinedError

        try:
            result = Template(template, undefined=StrictUndefined).render(
                input=input_data, steps=steps or {}
            )
        except UndefinedError as e:
            raise ValueError(
                f"Missing required field in template: {e}. Template: {template}"
            ) from e
        return re.sub(r"[ \t]+", " ", result).strip()

    def _replacer(match):
        f = match.group(1)
        optional = f.endswith("?")
        if optional:
            f = f[:-1]
        value = input_data.get(f)
        if value is None:
            if optional:
                return ""
            raise KeyError(f"Required field '{f}' not found in input.yaml")
        return str(value)

    result = re.sub(r"\{([^}]+)\}", _replacer, template)
    return re.sub(r"[ \t]+", " ", result).strip()


def _validate_relative_path(
    value: str,
    field_name: str,
    reject_root: bool = False,
    allow_absolute: bool = False,
) -> str:
    """Reject parent-traversing paths (and optionally absolute paths).

    Args:
        reject_root: If True, also reject "." (current directory).
            Used for output paths where "." would mean the project root
            and cleaning it would delete the entire project.
        allow_absolute: If True, allow absolute paths (pass through as-is).
            Used for dataset.path which may be an absolute shared path.
    """
    if not value:
        return value
    p = Path(value)
    if ".." in p.parts:
        raise ValueError(f"{field_name} must not contain '..': {value}")
    if p.is_absolute():
        if not allow_absolute:
            raise ValueError(f"{field_name} must be a relative path: {value}")
        return value
    if reject_root and str(p) == ".":
        raise ValueError(
            f"{field_name} cannot be '.' (project root) — use a subdirectory. "
            f"Outputs must be in a named subdirectory so the harness can "
            f"identify, collect, and clean them without affecting the project."
        )
    return value


def _validate_path_segment(value: str, name: str) -> str:
    """Validate that a value is a single path segment (no directory traversal).

    Ensures the value contains no path separators (/ or \\), is not a
    relative directory reference (. or ..), and contains no control characters.
    Used to prevent path traversal attacks (CWE-22) when constructing
    filesystem paths from user-controlled input.

    Args:
        value: The path segment to validate (e.g., run_id, skill name)
        name: Parameter name for error messages

    Returns:
        The validated value

    Raises:
        ValueError: If value is not a valid single path segment
    """
    if not _is_valid_eval_name(value):
        # Provide detailed error message based on what failed
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be a non-empty string, got: {value!r}")
        if "/" in value or "\\" in value:
            raise ValueError(
                f"{name} must be a single path segment, "
                f"cannot contain path separators: {value!r}"
            )
        if value in (".", ".."):
            raise ValueError(
                f"{name} cannot be a relative directory reference: {value!r}"
            )
        # Control characters or other invalid chars
        raise ValueError(f"{name} contains invalid characters: {value!r}")
    return value


def resolve_plugin_path(configured: str, project_root, config_dir=None) -> Path:
    """Trust-boundary resolution for plugin dirs, shared by every consumer.

    This is the single implementation of the plugin-path security rules —
    the runtime runners and the eval.yaml validator must agree on them, so
    neither may carry its own copy. Relative paths use the project root, which
    matches the runner behavior before this helper existed. A path that is
    lexically inside the project may not escape it through a symlink. A path
    declared lexically outside (for example ``../shared-plugins``) is an
    explicit operator opt-in equivalent to an absolute external path.
    Existence is not checked here; callers decide how a missing directory is
    reported. ``config_dir`` remains accepted for API compatibility but is not
    a resolution base.
    """
    path = Path(configured).expanduser()
    if path.is_absolute():
        return path.resolve()
    root = Path(project_root).resolve()
    lexical = Path(os.path.abspath(root / path))
    resolved = lexical.resolve()
    if lexical.is_relative_to(root) and not resolved.is_relative_to(root):
        raise ValueError(
            "A plugin_dirs entry declared inside the project must not escape "
            f"the project root through a symlink: {configured!r} resolved to "
            f"{resolved}")
    return resolved


def resolve_plugin_dir(config, configured: str) -> Path:
    """Resolve one runner plugin directory, requiring it to exist.

    Validation happens before the first case runs so a misconfigured plugin
    fails fast rather than mid-suite.
    """
    resolved = resolve_plugin_path(configured, config.project_root,
                                   config.config_dir)
    if not resolved.is_dir():
        raise FileNotFoundError(f"Runner plugin directory not found: {resolved}")
    return resolved


def workspace_source_roots(config) -> list:
    """Directories a ``WorkspaceFile.source`` may resolve into.

    Always the project root; plus each configured ``runner.plugin_dirs`` entry
    (so a shared file may reference a live SKILL.md in a plugin). A plugin dir
    that cannot be resolved (misconfigured, escapes the project via symlink) is
    dropped rather than raised, so one bad plugin entry never blocks materializing
    a file whose source is valid.
    """
    project = Path(config.project_root).resolve()
    roots = [project]
    runner = getattr(config, "runner", None)
    config_dir = getattr(config, "config_dir", None)
    for configured in getattr(runner, "plugin_dirs", None) or []:
        try:
            roots.append(
                resolve_plugin_path(configured, project, config_dir).resolve()
            )
        except (ValueError, OSError, TypeError, RuntimeError):
            # RuntimeError: Path.resolve() on a symlink loop.
            continue
    return roots


def resolve_workspace_source(config, source: str) -> Optional[Path]:
    """Resolve a shared workspace file's ``source`` to a real, in-bounds path.

    Relative sources resolve against the project root, absolute sources as-is.
    Symlinks ARE followed (unlike per-case string entries, which skip them) —
    the point is to materialize a live SKILL.md that lives outside the case dir.
    Returns the resolved path only if it exists and its REAL location stays
    within the project root or a configured plugin dir; otherwise ``None`` so
    the caller can warn and skip (a missing, dangling, or escaping source must
    never pull a host file into the agent-visible workspace — CWE-59).
    """
    if not source or not isinstance(source, str):
        return None
    roots = workspace_source_roots(config)
    raw = Path(source).expanduser()
    project = Path(config.project_root).resolve()
    candidate = raw if raw.is_absolute() else (project / raw)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not any(resolved.is_relative_to(root) for root in roots):
        return None
    return resolved


def resolve_plugin_skill_roots(plugin_dir: str | Path) -> list[Path]:
    """Resolve the skill roots exported by one Claude plugin.

    ``.claude-plugin/plugin.json`` may override the conventional ``skills/``
    directory with a string or list in its ``skills`` field. Invalid manifests
    and missing roots fail fast: silently starting Codex without the configured
    skills would turn a setup error into a misleading model-quality failure.
    """
    plugin = Path(plugin_dir).resolve()
    manifest_path = plugin / ".claude-plugin" / "plugin.json"
    configured_roots = None
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            raise ValueError(
                f"Cannot read plugin manifest {manifest_path}: {exc}") from exc
        if not isinstance(manifest, dict):
            raise ValueError(
                f"Plugin manifest {manifest_path} must be a JSON object")
        configured_roots = manifest.get("skills")

    if configured_roots is None:
        entries = ["skills"]
    elif isinstance(configured_roots, str) and configured_roots:
        entries = [configured_roots]
    elif (isinstance(configured_roots, list) and configured_roots
          and all(isinstance(entry, str) and entry for entry in configured_roots)):
        entries = configured_roots
    else:
        raise ValueError(
            f"Plugin manifest {manifest_path} field 'skills' must be a "
            "non-empty string or list of non-empty strings")

    roots = []
    for entry in entries:
        # The manifest is third-party content; its entries must not name
        # host paths outside the plugin the operator actually opted into,
        # whether spelled absolute, with ``..``, or through a symlink.
        root = (plugin / entry).resolve()
        if not root.is_relative_to(plugin):
            raise ValueError(
                "Plugin skill roots must stay beneath the plugin directory "
                f"{plugin}: {entry!r} resolved to {root}")
        roots.append(root)
    missing = [root for root in roots if not root.is_dir()]
    if missing:
        raise FileNotFoundError(
            "Plugin skill directory not found: " + ", ".join(map(str, missing)))
    if not any(any(child.is_dir() and (child / "SKILL.md").is_file()
                       for child in root.iterdir()) for root in roots):
        raise ValueError(f"Configured plugin has no discoverable skills: {plugin}")
    return roots


@dataclass
class DiscoveryResult:
    """A discovered eval config file."""
    path: Path
    eval_name: str
    is_root: bool
    # Set when the file is a profile (`extends:`): the root config it layers
    # over. Profiles are returned only with `include_profiles=True`.
    profile_of: Optional[Path] = None


@dataclass
class WorkspaceFile:
    """A shared file provisioned into every case workspace.

    Unlike a plain string entry (a per-case path inside the case directory),
    a ``{dest, source}`` mapping references a project or plugin resource that
    is *materialized* — copied, never symlinked — into every case workspace at
    ``dest``. ``source`` is resolved at packaging time against the project root
    or a configured ``runner.plugin_dirs`` entry (symlinks in the source are
    followed, as long as the real path stays within those roots). Because the
    result is a real file, the same entry ports unchanged to Harbor task
    packages and S3/EvalHub datasets, where a committed symlink would not.
    """

    dest: str
    source: str


@dataclass
class WorkspaceConfig:
    """Workspace file provisioning for evaluation cases.

    ``files`` is a whitelist of entries copied into the agent workspace.
    Each entry is either:

    - a ``str``: a relative path inside each case directory (per-case).
      Directory entries copy recursively; file entries copy the single file.
    - a :class:`WorkspaceFile` (``{dest, source}`` mapping): a shared
      project/plugin resource materialized into every case workspace.

    Paths not listed are left behind.
    """

    files: list = field(default_factory=list)


@dataclass
class DatasetConfig:
    """Dataset location, schema, and workspace provisioning."""

    path: str = ""
    schema: str = ""
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)


@dataclass
class OutputConfig:
    """One output source with a natural language schema.

    Output types (determined by which field is set):
    - path: file artifacts in a directory on disk
    - tool: tool calls to capture from stream-json events

    Batch collection (optional):
    - batch_pattern: maps output files to cases when the skill processes
      all cases in a single invocation.  Uses {n} as a 1-based batch
      index (e.g. "RFE-{n:03d}" → "RFE-001", "RFE-002").  Files whose
      name starts with the expanded prefix are assigned to that case.
      Use "*" for shared directories (copied to every case).
    """

    path: str = ""  # File artifacts directory
    tool: str = ""  # Tool call name/pattern to capture
    schema: str = ""
    batch_pattern: str = ""  # Batch collection pattern (empty = auto-detect)
    types: dict = None  # Semantic types for artifacts (filename or glob → type)


@dataclass
class TracesConfig:
    """What execution traces to capture and make available to judges."""
    stdout: bool = True  # Capture stdout.log
    stderr: bool = True  # Capture stderr.log
    events: bool = True  # Parse JSONL into events.json
    metrics: bool = True  # Capture run_result.json metrics


@dataclass
class ToolInputConfig:
    """Handler for intercepting a tool during eval execution.

    The `match` field describes what to intercept in natural language.
    eval-analyze populates this based on skill analysis. eval-run resolves
    it to concrete patterns at workspace setup time.
    """

    match: str = ""  # Natural language: what to intercept (tools, scripts, APIs)
    prompt: str = ""  # Natural language instruction for how to handle
    prompt_file: str = ""  # External file with detailed instructions


@dataclass
class InputsConfig:
    """Tool interception configuration for headless execution."""

    tools: list = field(default_factory=list)  # List of ToolInputConfig


@dataclass
class HookEntry:
    """A single lifecycle hook command."""
    command: str = ""
    timeout: int = 120
    description: str = ""
    on_failure: str = "fail"  # "fail" | "continue"
    condition: str = ""


@dataclass
class HooksConfig:
    """Lifecycle hooks that run at defined points in the eval pipeline."""
    before_all: list = field(default_factory=list)
    before_each: list = field(default_factory=list)
    after_each: list = field(default_factory=list)
    before_step: list = field(default_factory=list)
    after_step: list = field(default_factory=list)
    before_scoring: list = field(default_factory=list)
    after_all: list = field(default_factory=list)
    before_report: list = field(default_factory=list)


@dataclass
class ExecutionConfig:
    """How the eval target is invoked against test cases.

    Modes (orthogonal to skill/prompt):
    - case (default): one invocation per test case, with case-specific
      arguments resolved from input.yaml fields via {field} placeholders.
    - batch: all cases in one invocation via batch.yaml.

    What to execute (mutually exclusive):
    - skill: skill name to invoke (e.g., 'rfe.create'). Pairs with arguments.
    - prompt: direct prompt template (e.g., '{{ input.prompt }}'). No skill wrapper.

    Examples:
    - Skill mode (case): skill: 'rfe.create', arguments: '--priority {{ input.priority }}'
    - Skill mode (batch): skill: 'rfe.speedrun', arguments: '--input batch.yaml'
    - Prompt mode (case): prompt: '{{ input.prompt }}', arguments: ''
    - Prompt mode (batch): prompt: '{{ input.prompt }}', arguments: '' (uncommon)

    Arguments template placeholders:
    - {field} → substitutes the value of 'field' from input.yaml
    - {field?} → substitutes if present, omitted if missing

    Constraints:
    - timeout: subprocess wall-clock timeout in seconds (None = harness default).
    - max_budget_usd: per-invocation cost cap (None = no cap).

    Environment:
    - env: extra environment variables injected into each case workspace's
      .claude/settings.json.  Available to both the skill and its hooks.
      Values starting with ``$`` are resolved from the caller's environment
      (e.g., ``$JIRA_TOKEN`` → ``os.environ["JIRA_TOKEN"]``).  Missing
      vars are silently omitted.  Literal values are passed through as-is.
    - project_hooks: carry the project's ``.claude/settings.json`` ``hooks``
      into each workspace's settings (appended after the harness's own
      SubagentStop/PreToolUse hooks), so a skill keeps the hooks it has in
      production — a SessionStart/compact recovery banner, a Stop guard.
      Default True; set False to run the skill with the harness hooks only.
    """

    mode: str = "case"
    skill: str = ""       # Skill name for skill mode (mutually exclusive with prompt)
    prompt: str = ""      # Prompt template for prompt mode (mutually exclusive with skill)
    arguments: str = ""
    timeout: Optional[int] = None
    max_budget_usd: Optional[float] = None
    parallelism: Optional[int] = None
    env: dict = field(default_factory=dict)
    project_hooks: bool = True
    # Multi-step pipeline. When non-empty, REPLACES skill/prompt/arguments —
    # each entry is one agent invocation run sequentially in the shared per-case
    # workspace (see StepConfig). Mutually exclusive with skill/prompt; case
    # mode only.
    steps: list = field(default_factory=list)

    def __post_init__(self):
        # Validate mode
        valid_modes = ["case", "batch"]
        if self.mode not in valid_modes:
            raise ValueError(
                f"execution.mode must be one of {valid_modes}, got: {self.mode}"
            )

        if not isinstance(self.project_hooks, bool):
            raise ValueError(
                "execution.project_hooks must be a boolean, got: "
                f"{self.project_hooks!r}"
            )

        # Validate skill/prompt mutual exclusivity
        has_skill = bool(self.skill and self.skill.strip())
        has_prompt = bool(self.prompt and self.prompt.strip())

        if has_skill and has_prompt:
            raise ValueError(
                "execution.skill and execution.prompt are mutually exclusive. "
                "Use skill for '/skill-name' invocations or prompt for direct prompts."
            )

        # Multi-step: steps replaces skill/prompt and is case-mode only.
        if self.steps:
            if has_skill or has_prompt:
                raise ValueError(
                    "execution.steps is mutually exclusive with execution.skill/"
                    "execution.prompt — put each invocation in its own step."
                )
            if self.mode != "case":
                raise ValueError(
                    "execution.steps is only supported in mode: case "
                    f"(got mode: {self.mode})."
                )
            ids = [getattr(s, "id", "") for s in self.steps]
            if any(not (i and str(i).strip()) for i in ids):
                raise ValueError(
                    "execution.steps: every step needs a non-empty 'id'."
                )
            # ids become filesystem path components (workspace, run output,
            # Harbor task) — reject separators / '.'/'..' / control chars (CWE-22).
            for i in ids:
                _validate_path_segment(str(i), "execution.steps[].id")
            if len(set(ids)) != len(ids):
                raise ValueError(
                    f"execution.steps: step ids must be unique, got {ids}."
                )

    def resolved_steps(self) -> list:
        """The pipeline as an explicit step list — one code path for the executor.

        Multi-step configs return ``steps`` verbatim.  A single skill/prompt
        config is normalized to a one-element list so the executor always loops.
        """
        if self.steps:
            return self.steps
        return [StepConfig(
            id=(self.skill or "step-1"),
            skill=self.skill,
            prompt=self.prompt,
            arguments=self.arguments,
            env=dict(self.env),
            timeout=self.timeout,
            max_budget_usd=self.max_budget_usd,
        )]



@dataclass
class RunnerConfig:
    """Which agent harness runs the skill, and runner-specific knobs.

    type: discriminator selecting the runner implementation (e.g. claude-code).
    workspace_mode: execution context (repo = run in repository, default = isolated workspace).
    Other fields are runner-specific; unused fields are harmless for runners
    that don't read them.

    env: extra environment variables injected into the runner subprocess.
    Keys are variable names, values are literal strings or ``$VAR``
    references resolved from the caller's environment.  Additive to the
    runner's built-in safe defaults (Claude Code allowlist).
    """

    type: str = "claude-code"
    command: Optional[Union[str, list]] = None  # CLI runner: command template
    workspace_mode: Optional[str] = None  # repo | None (default: isolated workspace)
    settings: dict = field(default_factory=dict)
    # Claude Code stages each entry's discoverable content into the case
    # workspace and passes the staged copy to --plugin-dir (see
    # agent.claude_code.stage_plugin_dir); Codex copies each entry's skills
    # into the workspace's .agents/skills.
    plugin_dirs: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    system_prompt: Optional[str] = None
    # Claude Code: low..max; Codex: minimal..xhigh (runner validates precisely).
    effort: Optional[str] = None
    # Claude Code: default | acceptEdits | plan | auto | dontAsk | bypassPermissions.
    # Passed as --permission-mode (a CLI flag), so it applies even in untrusted
    # isolated workspaces where settings-file permissions are trust-gated.
    permission_mode: Optional[str] = None


def _parse_runner_config(runner_raw, *, context="runner"):
    """Parse a runner block into a RunnerConfig with validation.

    Shared by the top-level ``runner:`` block, a judge's nested
    ``agent.runner:`` block, and per-step ``execution.steps[].runner:`` so all
    honor identical defaults and validation (command type-check,
    workspace_mode whitelist). ``context`` is the field path used in error
    messages.
    """
    runner_raw = runner_raw or {}
    command = runner_raw.get("command")
    if command is not None:
        valid_list = isinstance(command, list) and all(
            isinstance(x, str) for x in command
        )
        if not (isinstance(command, str) or valid_list):
            raise ValueError(f"{context}.command must be a string or list of strings")
    # Validate workspace_mode (prevent typos that silently change behavior)
    workspace_mode = runner_raw.get("workspace_mode")
    if workspace_mode is not None and workspace_mode not in ("repo",):
        raise ValueError(
            f"{context}.workspace_mode must be None or 'repo', got: {workspace_mode!r}")
    # settings.enabledPlugins."*" is the harness-interpreted wildcard that
    # steers hermetic plugin isolation (no upstream wildcard exists; it is
    # stripped before settings.json is written). Strict boolean — a string
    # like "false" silently flipping the policy would change which plugins
    # load in every case.
    enabled_plugins = (runner_raw.get("settings") or {}).get("enabledPlugins")
    if isinstance(enabled_plugins, dict) and "*" in enabled_plugins:
        if not isinstance(enabled_plugins["*"], bool):
            raise ValueError(
                f'{context}.settings.enabledPlugins."*" must be a boolean, '
                f'got: {enabled_plugins["*"]!r}')
    return RunnerConfig(
        type=runner_raw.get("type", "claude-code"),
        command=command,
        workspace_mode=workspace_mode,
        settings=runner_raw.get("settings", {}) or {},
        plugin_dirs=runner_raw.get("plugin_dirs", []) or [],
        env=runner_raw.get("env", {}) or {},
        system_prompt=runner_raw.get("system_prompt"),
        effort=runner_raw.get("effort"),
        permission_mode=runner_raw.get("permission_mode"),
    )


@dataclass
class StepConfig:
    """One step in a multi-step execution pipeline (``execution.steps[]``).

    A step is a single agent invocation (``skill`` xor ``prompt``) run in the
    shared per-case workspace.  Steps run sequentially; later steps see earlier
    steps' files on disk and can reference their results via the
    ``{{ steps.<id>.* }}`` template namespace.  Per-step ``timeout`` /
    ``max_budget_usd`` / ``runner`` fall back to the ``execution`` / top-level
    defaults when unset.
    """

    id: str = ""
    name: str = ""
    skill: str = ""       # skill xor prompt (validated per step)
    prompt: str = ""
    arguments: str = ""
    env: dict = field(default_factory=dict)
    timeout: Optional[int] = None
    max_budget_usd: Optional[float] = None
    runner: Optional[RunnerConfig] = None
    on_failure: str = "fail"  # "fail" (abort remaining steps) | "continue"

    def __post_init__(self):
        if self.on_failure not in ("fail", "continue"):
            raise ValueError(
                f"execution.steps: step '{self.id}': on_failure must be 'fail' "
                f"or 'continue', got '{self.on_failure}'")
        has_skill = bool(self.skill and self.skill.strip())
        has_prompt = bool(self.prompt and self.prompt.strip())
        if has_skill and has_prompt:
            raise ValueError(
                f"execution.steps: step '{self.id}': skill and prompt are "
                "mutually exclusive.")


@dataclass
class MlflowConfig:
    """MLflow logging target.

    experiment: experiment name. Defaults to EvalConfig.name when an
        `mlflow:` block is present but `experiment` is unset. Stays empty
        when the eval.yaml has no `mlflow:` block at all — so MLflow
        tracing/logging is opt-in via the block, not implicit from `name:`.
    tracking_uri: MLflow server URI; if unset, falls back to
        MLFLOW_TRACKING_URI env var.
    tags: tags applied to every run logged for this eval.
    """

    experiment: str = ""
    tracking_uri: Optional[str] = None
    tags: dict = field(default_factory=dict)


# --- Provider registry (spec 014) -------------------------------------------
#
# ``models.providers.<name>`` declares how a ``<name>:/<model>`` URI on any
# role is served. One kind exists (``openrouter``, Decision 1); the registry
# lives under ``models`` because it only exists to resolve those URIs
# (Decision 17). This release parses the judge-side options; the agent
# transport options documented by the spec are rejected by name until the PR
# that consumes them lands, so nothing an operator writes is silently ignored.

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_OPENROUTER_DEFAULT_BASE_URL = "https://openrouter.ai/api"
_OPENROUTER_KEYS = ("kind", "api_key_env", "management_key_env", "base_url", "attribution",
                    "background_model", "preflight", "cli_budget_inflation", "budget",
                    "routing", "judge")
_OPENROUTER_ROUTING_KEYS = ("defaults", "models", "policy", "enforcement", "guardrail")
_OPENROUTER_BUDGET_KEYS = ("run_usd", "dedicated_key")
_OPENROUTER_GUARDRAIL_KEYS = ("key_name", "providers", "revoke_on_exit", "settle_s")
# Reserved: there is one transport and no in-flight gate (spec 014 Decision 1).
_OPENROUTER_RESERVED_KEYS = ("transport", "direct", "proxy", "gateway",
                             "generation_backfill", "key_exposure_ack")
_OPENROUTER_BUDGET_RESERVED_KEYS = ("max_unpriced", "max_unpriced_ratio")
_PREFLIGHT_LEVELS = ("strict", "warn", "off")
_ROUTING_POLICIES = ("strict", "warn")
_ENFORCEMENT_LEVELS = ("audit", "key-guardrail")
_GUARDRAIL_SETTLE_MIN_S = 20
# Agent-role routing keys Claude Code cannot carry in a request (judge-only).
_AGENT_UNSENDABLE_ROUTING_KEYS = ("require_parameters", "sort", "data_collection",
                                  "zdr", "max_price")
_OPENROUTER_JUDGE_OPTION_KEYS = ("routing", "fallbacks", "max_tokens")


@dataclass
class Attribution:
    """OpenRouter app-attribution headers sent by the judge client (and, later,
    the agent): ``HTTP-Referer`` / ``X-OpenRouter-Title``."""

    referer: Optional[str] = None
    title: Optional[str] = "agent-eval-harness"
    run_id_header: bool = False


@dataclass
class JudgeClientOptions:
    """``models.providers.openrouter.judge``: the judge client's retry policy,
    concurrency cap, static ``extra_body`` and the Decision 25 pin opt-in."""

    concurrency: int = 4
    max_retries: int = 3
    timeout_s: float = 300.0
    extra_body: dict = field(default_factory=dict)
    inherit_pins: bool = False


@dataclass(frozen=True)
class GuardrailOptions:
    """``routing.guardrail``: the per-run key at ``enforcement: key-guardrail``.
    ``providers`` is ``"pinned"`` (the union of every routing key's pinned
    providers) or an explicit list of provider slugs."""

    key_name: str = "agent-eval {run_id}"
    providers: object = "pinned"
    revoke_on_exit: bool = True
    settle_s: float = 20.0


@dataclass(frozen=True)
class RoutingConfig(RoutingTable):
    """``models.providers.openrouter.routing``: ``defaults`` plus per-model
    entries, and what the harness does about them on the agent path —
    ``policy`` (what a failed post-hoc audit does to the run), ``enforcement``
    (``audit``: preflight + audit; ``key-guardrail``: a per-run key with a
    provider allow-list and a real-cost limit) and the guardrail options."""

    policy: str = "strict"
    enforcement: str = "audit"
    guardrail: GuardrailOptions = field(default_factory=GuardrailOptions)

    def pinned_keys(self) -> list:
        """Routing-table keys whose effective declaration carries pins."""
        return [key for key in self.models if self.for_model(key).is_pinned]


@dataclass
class BudgetOptions:
    """``models.providers.openrouter.budget``: ``run_usd`` is the whole-run
    real-dollar pool (post hoc at ``audit``, the per-run key's ``limit_usd``
    at ``key-guardrail``); ``dedicated_key`` asserts nothing else spends on the
    operator key during the run."""

    run_usd: Optional[float] = None
    dedicated_key: bool = False


@dataclass
class OpenRouterConfig:
    """``models.providers.openrouter`` (spec 014). Secrets are env-only:
    ``api_key_env`` / ``management_key_env`` name the variables holding the
    keys, never the keys. A declared block is inert until an effective role
    URI names it (Decision 21)."""

    kind: str = "openrouter"
    api_key_env: str = "OPENROUTER_API_KEY"
    management_key_env: str = "OPENROUTER_MANAGEMENT_KEY"
    base_url: str = _OPENROUTER_DEFAULT_BASE_URL
    attribution: Attribution = field(default_factory=Attribution)
    background_model: Optional[str] = None
    preflight: str = "strict"
    cli_budget_inflation: float = 50
    budget: BudgetOptions = field(default_factory=BudgetOptions)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    judge: JudgeClientOptions = field(default_factory=JudgeClientOptions)


@dataclass
class ProvidersConfig:
    """The provider registry under ``models.providers``."""

    openrouter: Optional[OpenRouterConfig] = None


def _finite_number(value) -> bool:
    """A real, finite number — YAML's `.nan`/`.inf` and booleans are not."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _require_mapping(value, context):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be a mapping")
    return value


def _check_keys(raw, context, allowed, *, later=(), reserved=()):
    """Unknown sub-keys are errors. Reserved keys (transport modes, in-flight
    gates) are named together with the Decision 1 pointer: they are never
    silently ignored, and one message lists every offender."""
    offenders = [f"{context}.{key}" for key in raw if key in reserved]
    if offenders:
        raise ValueError(
            f"not supported: {', '.join(offenders)} — there is a single OpenRouter "
            "transport and no in-flight gate (spec 014 Decision 1)")
    for key in raw:
        if key in allowed:
            continue
        if key in later:
            raise ValueError(
                f"{context}.{key} is not implemented yet (spec 014 rollout); "
                "remove it for now")
        raise ValueError(
            f"{context} has unknown key(s): {key} (allowed: {', '.join(allowed)})")


def _warn(message):
    import warnings

    warnings.warn(message, UserWarning, stacklevel=3)


def _env_var_name(value, context):
    """A ``*_env`` setting names an environment variable — never holds a value.
    The error text never echoes the value."""
    name = value.strip().lstrip("$") if isinstance(value, str) else ""
    if not name or not _ENV_NAME_RE.match(name):
        raise ValueError(
            f"{context} must name an environment variable (e.g. "
            "OPENROUTER_API_KEY), not hold a key value")
    return name


def _is_loopback_host(host):
    return host == "localhost" or host == "::1" or host.startswith("127.")


def _resolve_base_url(value, context):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} must be a URL string")
    url = value.strip()
    if url.startswith("$"):
        name = url[1:]
        resolved = os.environ.get(name)
        if not resolved:
            raise ValueError(f"{context} references ${name}, which is not set")
        url = resolved.strip()
    url = url.rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"{context} must be an http(s) URL")
    if url.startswith("http://"):
        # The judge client sends the key named by api_key_env (and the judged
        # material) to this host: cleartext is acceptable only for a gateway
        # on the loopback interface, never across a network.
        from urllib.parse import urlsplit
        host = (urlsplit(url).hostname or "").lower()
        if not _is_loopback_host(host):
            raise ValueError(
                f"{context} must use https — the judge client sends the key "
                "named by api_key_env to this host; plain http is allowed only "
                "for a loopback gateway (localhost, 127.0.0.0/8, ::1)")
    if url.endswith("/v1") or url.endswith("/v1/messages"):
        raise ValueError(
            f"{context} must not include the /v1 path — the harness appends "
            "/v1/… itself")
    return url


def _parse_openrouter_config(raw, context):
    raw = _require_mapping(raw, context)
    _check_keys(raw, context, _OPENROUTER_KEYS, reserved=_OPENROUTER_RESERVED_KEYS)
    cfg = OpenRouterConfig()
    if "api_key_env" in raw:
        cfg.api_key_env = _env_var_name(raw["api_key_env"], f"{context}.api_key_env")
    if "management_key_env" in raw:
        cfg.management_key_env = _env_var_name(
            raw["management_key_env"], f"{context}.management_key_env")
    if "base_url" in raw:
        cfg.base_url = _resolve_base_url(raw["base_url"], f"{context}.base_url")
    if "background_model" in raw and raw["background_model"] is not None:
        v = raw["background_model"]
        from agent_eval.providers.base import parse_agent_model

        try:
            if not isinstance(v, str):
                raise ValueError("not a string")
            model = parse_agent_model(v if ":/" in v else f"openrouter:/{v.strip()}")
            if model.provider != "openrouter":
                raise ValueError(f"provider {model.provider!r} is not openrouter")
        except ValueError as exc:
            raise ValueError(
                f"{context}.background_model must be an OpenRouter '<author>/<slug>' "
                f"id (the model behind the haiku slot and the default hook model): {exc}"
            ) from exc
        cfg.background_model = model.id
    if "preflight" in raw:
        if raw["preflight"] not in _PREFLIGHT_LEVELS:
            raise ValueError(f"{context}.preflight must be one of {list(_PREFLIGHT_LEVELS)}")
        cfg.preflight = raw["preflight"]
    if "cli_budget_inflation" in raw:
        v = raw["cli_budget_inflation"]
        if not _finite_number(v) or v < 1:
            raise ValueError(f"{context}.cli_budget_inflation must be a finite number >= 1")
        if v == 1:
            _warn(f"{context}.cli_budget_inflation is 1: Claude Code prices a "
                  "non-Anthropic model 2-60x high, so execution.max_budget_usd will "
                  "cut runs off well before the real spend reaches it")
        cfg.cli_budget_inflation = v

    budget_raw = _require_mapping(raw.get("budget"), f"{context}.budget")
    _check_keys(budget_raw, f"{context}.budget", _OPENROUTER_BUDGET_KEYS,
                reserved=_OPENROUTER_BUDGET_RESERVED_KEYS)
    budget = BudgetOptions()
    if budget_raw.get("run_usd") is not None:
        v = budget_raw["run_usd"]
        if not _finite_number(v) or v <= 0:
            raise ValueError(f"{context}.budget.run_usd must be a finite number > 0")
        budget.run_usd = float(v)
    if "dedicated_key" in budget_raw:
        if not isinstance(budget_raw["dedicated_key"], bool):
            raise ValueError(f"{context}.budget.dedicated_key must be a boolean")
        budget.dedicated_key = budget_raw["dedicated_key"]
    cfg.budget = budget

    attr = _require_mapping(raw.get("attribution"), f"{context}.attribution")
    _check_keys(attr, f"{context}.attribution", ("referer", "title", "run_id_header"))
    referer = attr.get("referer")
    title = attr.get("title", cfg.attribution.title)
    run_id_header = attr.get("run_id_header", False)
    if referer is not None and not isinstance(referer, str):
        raise ValueError(f"{context}.attribution.referer must be a string")
    if title is not None and not isinstance(title, str):
        raise ValueError(f"{context}.attribution.title must be a string")
    # Each value becomes one HTTP header line (ANTHROPIC_CUSTOM_HEADERS is
    # split on newlines): a CR/LF would smuggle an extra header.
    for name, value in (("referer", referer), ("title", title)):
        if isinstance(value, str) and any(c in value for c in "\r\n\x00"):
            raise ValueError(f"{context}.attribution.{name} must be a single line")
    if not isinstance(run_id_header, bool):
        raise ValueError(f"{context}.attribution.run_id_header must be a boolean")
    cfg.attribution = Attribution(referer=referer or None, title=title or None,
                                  run_id_header=run_id_header)

    routing_raw = raw.get("routing")
    if routing_raw is not None:
        rctx = f"{context}.routing"
        routing_raw = _require_mapping(routing_raw, rctx)
        _check_keys(routing_raw, rctx, _OPENROUTER_ROUTING_KEYS)
        table = RoutingTable.from_dict(
            {k: routing_raw.get(k) for k in ("defaults", "models") if k in routing_raw},
            context=rctx)
        policy = routing_raw.get("policy", "strict")
        if policy not in _ROUTING_POLICIES:
            raise ValueError(f"{rctx}.policy must be one of {list(_ROUTING_POLICIES)}")
        enforcement = routing_raw.get("enforcement", "audit")
        if enforcement not in _ENFORCEMENT_LEVELS:
            raise ValueError(f"{rctx}.enforcement must be one of {list(_ENFORCEMENT_LEVELS)}")
        guardrail = _parse_guardrail(routing_raw.get("guardrail"), f"{rctx}.guardrail")
        cfg.routing = RoutingConfig(defaults=table.defaults, models=table.models,
                                    policy=policy, enforcement=enforcement,
                                    guardrail=guardrail)
        if enforcement == "key-guardrail":
            problems = []
            if cfg.budget.run_usd is None:
                problems.append(f"{context}.budget.run_usd must be set (> 0): it becomes "
                                "the per-run key's limit_usd — there is no unlimited per-run key")
            explicit = isinstance(guardrail.providers, (list, tuple)) and guardrail.providers
            if not explicit and not cfg.routing.pinned_keys() and not table.defaults.is_pinned:
                problems.append(f"{rctx}.guardrail.providers must be an explicit list or "
                                "some routing key must carry pins (order/only): a guardrail "
                                "with no provider restriction is not supported — use "
                                "'audit' if only the budget is wanted")
            if problems:
                raise ValueError(f"{rctx}.enforcement: key-guardrail — " + "; ".join(problems))

    judge_raw = _require_mapping(raw.get("judge"), f"{context}.judge")
    jctx = f"{context}.judge"
    _check_keys(judge_raw, jctx,
                ("concurrency", "max_retries", "timeout_s", "extra_body", "inherit_pins"))
    opts = JudgeClientOptions()
    if "concurrency" in judge_raw:
        v = judge_raw["concurrency"]
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise ValueError(f"{jctx}.concurrency must be an integer >= 1")
        opts.concurrency = v
    if "max_retries" in judge_raw:
        v = judge_raw["max_retries"]
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError(f"{jctx}.max_retries must be an integer >= 0")
        opts.max_retries = v
    if "timeout_s" in judge_raw:
        v = judge_raw["timeout_s"]
        if not _finite_number(v) or v <= 0:
            raise ValueError(f"{jctx}.timeout_s must be a finite number > 0")
        opts.timeout_s = float(v)
    if "extra_body" in judge_raw:
        v = judge_raw["extra_body"]
        if not isinstance(v, dict):
            raise ValueError(f"{jctx}.extra_body must be a mapping")
        opts.extra_body = dict(v)
    if "inherit_pins" in judge_raw:
        v = judge_raw["inherit_pins"]
        if not isinstance(v, bool):
            raise ValueError(f"{jctx}.inherit_pins must be a boolean")
        if routing_raw is None:
            raise ValueError(
                f"{jctx}.inherit_pins has nothing to inherit — declare "
                f"{context}.routing first (spec 014 Decision 25)")
        opts.inherit_pins = v
    cfg.judge = opts
    return cfg


def _parse_guardrail(raw, context):
    raw = _require_mapping(raw, context)
    _check_keys(raw, context, _OPENROUTER_GUARDRAIL_KEYS)
    opts = GuardrailOptions()
    key_name = raw.get("key_name", opts.key_name)
    if not isinstance(key_name, str) or not key_name.strip():
        raise ValueError(f"{context}.key_name must be a non-empty string")
    providers = raw.get("providers", opts.providers)
    if isinstance(providers, str):
        if providers != "pinned":
            raise ValueError(
                f"{context}.providers must be 'pinned' or an explicit list of provider slugs")
    elif isinstance(providers, list):
        if not providers or not all(isinstance(p, str) and p.strip() for p in providers):
            raise ValueError(f"{context}.providers must be a non-empty list of provider slugs")
        from agent_eval.providers.openrouter.routing import normalize_provider

        normalised = []
        for name in providers:
            slug = normalize_provider(name)
            if slug != name:
                _warn(f"{context}.providers: {name!r} is a display name; using the slug {slug!r}")
            normalised.append(slug)
        providers = tuple(normalised)
    else:
        raise ValueError(
            f"{context}.providers must be 'pinned' or an explicit list of provider slugs")
    revoke = raw.get("revoke_on_exit", True)
    if not isinstance(revoke, bool):
        raise ValueError(f"{context}.revoke_on_exit must be a boolean")
    if revoke is False:
        raise ValueError(
            f"{context}.revoke_on_exit: false is not supported in this release — the "
            "per-run key is always revoked at run end")
    settle = raw.get("settle_s", opts.settle_s)
    if not _finite_number(settle) or settle < 0:
        raise ValueError(f"{context}.settle_s must be a finite number >= 0")
    if settle < _GUARDRAIL_SETTLE_MIN_S:
        _warn(f"{context}.settle_s is {settle}: OpenRouter's key-usage counter settles "
              f"in about {_GUARDRAIL_SETTLE_MIN_S} s (verified); a shorter wait reads a "
              "partial total")
    return GuardrailOptions(key_name=key_name.strip(), providers=providers,
                            revoke_on_exit=True, settle_s=float(settle))


def _env_surfaces(config, *, include_judges=False):
    """Authored env mappings that can reach settings.json or a subprocess env,
    labelled by their config path. The agent surfaces (execution, runner,
    steps) are what a plan owns; ``include_judges`` adds the agent judges'
    runner env, which the env-only secret rule covers too."""
    surfaces = [("execution.env", config.execution.env or {}),
                ("runner.env", getattr(config.runner, "env", None) or {}),
                ("runner.settings.env", (getattr(config.runner, "settings", None) or {}).get("env") or {})]
    for i, step in enumerate(config.execution.steps or []):
        surfaces.append((f"execution.steps[{i}].env", step.env or {}))
        if step.runner is not None:
            surfaces.append((f"execution.steps[{i}].runner.env", step.runner.env or {}))
            surfaces.append((f"execution.steps[{i}].runner.settings.env",
                             (step.runner.settings or {}).get("env") or {}))
    if include_judges:
        for jc in config.judges or []:
            runner = (getattr(jc, "agent", None) or {}).get("runner")
            if runner is None:
                continue
            label = f"judges[{jc.name}].agent.runner"
            surfaces.append((f"{label}.env", getattr(runner, "env", None) or {}))
            surfaces.append((f"{label}.settings.env",
                             (getattr(runner, "settings", None) or {}).get("env") or {}))
    return [(label, env) for label, env in surfaces if isinstance(env, dict)]


def validate_openrouter_roles(config):
    """Load-time checks for the agent roles under `models.providers.openrouter`
    (spec 014 Config validation): role URI shapes, one provider kind across the
    agent roles, runner support, the bare-id footgun, managed-key ownership on
    every env surface while a plan is active, env-only secrets, and the
    agent-path routing keys Claude Code cannot send. Errors are consolidated per
    category; advisory findings are warnings."""
    from agent_eval.prompt_backends import is_anthropic_model
    from agent_eval.providers.base import parse_agent_model
    from agent_eval.providers.env import DYNAMIC_MANAGED_KEYS, MANAGED_ENV_KEYS, settings_env_block
    from agent_eval.providers.openrouter.plan import build_plan

    orc = config.models.providers.openrouter
    declared = orc is not None
    roles = {"skill": config.models.skill, "subagent": config.models.subagent,
             "hook": config.models.hook}
    parsed = {}
    for role, uri in roles.items():
        if not uri:
            continue
        try:
            model = parse_agent_model(uri)
        except ValueError as exc:
            raise ValueError(f"models.{role}: {exc}") from exc
        if model.provider not in (None, "anthropic", "openrouter"):
            raise ValueError(
                f"models.{role}: Unsupported agent model provider {model.provider!r} in "
                f"{uri!r}. Agent roles take a bare id, 'anthropic:/…' or "
                "'openrouter:/<author>/<slug>'")
        parsed[role] = model

    skill = parsed.get("skill")
    active = skill is not None and skill.provider == "openrouter"

    # Secrets are env-only, plan or no plan: the key variables never appear as
    # authored env entries (they would be baked into task packages/settings).
    effective = orc or OpenRouterConfig()
    secret_names = {effective.api_key_env, effective.management_key_env}
    secret_refs = {f"${name}" for name in secret_names}
    leaks = []
    for label, env in _env_surfaces(config, include_judges=True):
        for key, value in env.items():
            if key in secret_names or (isinstance(value, str) and value.strip() in secret_refs):
                leaks.append(f"{label}.{key}")
    if leaks:
        level = effective.routing.enforcement
        raise ValueError(
            "remove " + ", ".join(sorted(leaks)) + f"; owned by models.providers.openrouter "
            f"(enforcement={level}) — OpenRouter keys are env-only and never authored")

    if declared and not active:
        # Bare-id footgun: pins declared for a slug that a role names bare (or a
        # bare non-Anthropic id) would silently run without the plan.
        offenders = []
        for role, model in parsed.items():
            if model.provider is not None:
                continue
            if orc.routing.has_entry(model.id) or not is_anthropic_model(model.id):
                offenders.append(f"models.{role}: {model.id!r}")
        if offenders:
            raise ValueError(
                "bare model id next to models.providers.openrouter — "
                + "; ".join(offenders)
                + " — use 'openrouter:/<id>' or drop the pins")

    if not active:
        return

    # One provider kind across the agent roles: the agent's env routes every
    # request to OpenRouter, so a bare or Anthropic subagent/hook id would be
    # sent to OpenRouter as-is and 404 (hook children inherit the env too).
    mixed = [f"models.{role}: {model.uri!r}" for role, model in parsed.items()
             if role != "skill" and model.provider != "openrouter"]
    if mixed:
        raise ValueError(
            "agent roles must share the plan's provider kind while models.skill is "
            f"'openrouter:/…' — {'; '.join(mixed)} — write 'openrouter:/<author>/<slug>' "
            "(or drop the role to inherit the skill model)")

    # Runner support: the direct OpenRouter transport is implemented for
    # claude-code (local, Harbor podman, Harbor Kubernetes).
    runner_types = [("runner.type", config.runner.type)]
    runner_types += [(f"execution.steps[{i}].runner.type", step.runner.type)
                     for i, step in enumerate(config.execution.steps or [])
                     if step.runner is not None]
    for label, rtype in runner_types:
        if rtype == "cursor":
            raise ValueError(
                f"{label}: cursor has no base-URL knob, so it cannot run "
                f"{skill.uri!r} (models.skill) through OpenRouter")
        if rtype != "claude-code":
            raise ValueError(
                f"{label}: the direct OpenRouter transport is implemented for "
                f"'claude-code' (local, Harbor podman, Harbor Kubernetes); "
                f"{rtype!r} cannot run {skill.uri!r} (models.skill)")

    # Managed-key ownership: the plan owns MANAGED_ENV_KEYS on every surface.
    plan = build_plan(config, runner="claude-code", require_key=False)
    static = settings_env_block(plan, secrets="omit")
    errors = []
    for label, env in _env_surfaces(config):
        for key, value in env.items():
            if key not in MANAGED_ENV_KEYS:
                continue
            if key in DYNAMIC_MANAGED_KEYS:
                errors.append(f"{label}.{key}")
            elif key == "ANTHROPIC_API_KEY":
                if value not in (None, ""):
                    _warn(f"{label}.ANTHROPIC_API_KEY is non-empty; it would be sent to "
                          "OpenRouter as x-api-key (which works) — the plan blanks it so a "
                          "stale Anthropic key or cached OAuth state never reaches the wire; "
                          "remove it")
            elif str(value if value is not None else "") != static.get(key, ""):
                errors.append(f"{label}.{key}")
    if errors:
        raise ValueError(
            "remove " + ", ".join(sorted(errors)) + "; owned by "
            f"models.providers.openrouter (enforcement={plan.enforcement}) — the plan's "
            "env block sets these keys for the agent, its subagents and its hooks")

    # Agent-path routing intent: keys Claude Code cannot carry in a request.
    seen_keys = set()
    for model in parsed.values():
        if model.key in seen_keys:
            continue
        seen_keys.add(model.key)
        spec = effective.routing.for_model(model.id)
        unsendable = [k for k in _AGENT_UNSENDABLE_ROUTING_KEYS if getattr(spec, k) is not None]
        if unsendable:
            _warn(f"models.providers.openrouter.routing for {model.key!r}: "
                  f"{', '.join(unsendable)} not sendable from Claude Code and ignored on "
                  "the agent path (audited keys use order/only/ignore/quantizations; sort → "
                  "the :nitro/:floor variant; data policy → account settings); judges that "
                  "inherit pins still receive the full spec")
        if spec.quantizations and not spec.is_pinned:
            _warn(f"models.providers.openrouter.routing for {model.key!r}: quantizations "
                  "without order/only — quantization is pinned indirectly through "
                  "providers; nothing to audit")


def _parse_providers(raw, context="models.providers"):
    providers = ProvidersConfig()
    raw = _require_mapping(raw, context)
    for name, block in raw.items():
        block_ctx = f"{context}.{name}"
        block_map = _require_mapping(block, block_ctx)
        kind = block_map.get("kind", name)
        if kind == "openai-compatible":
            raise ValueError(
                f"{block_ctx}: kind 'openai-compatible' is not implemented in "
                "this release (spec 014 Decision 1); use 'openai:/…' with "
                "OPENAI_BASE_URL")
        if name != "openrouter":
            raise ValueError(
                f"{block_ctx}: unknown provider; the only declared provider is "
                "'openrouter' (spec 014 Decision 1)")
        if kind != name:
            raise ValueError(f"{block_ctx}.kind must equal '{name}' (got {kind!r})")
        providers.openrouter = _parse_openrouter_config(block_map, block_ctx)
    return providers


def validate_judge_provider_options(options, model, judge_name):
    """Validate a judge's ``provider_options`` by the provider kind of its
    statically-known model. Only ``openrouter:/`` judges take options today
    (``routing``, ``fallbacks``, ``max_tokens``); the mapping stays opaque in
    ``JudgeConfig`` so the schema lives with the provider."""
    from agent_eval.prompt_backends import split_model_uri  # local: import cycle

    if options is None:
        return {}
    if not isinstance(options, dict):
        raise ValueError(f"Judge '{judge_name}': 'provider_options' must be a mapping")
    if not options:
        return {}
    provider, _bare = split_model_uri(model)
    if provider != "openrouter":
        got = f"got {model!r}" if model else "no static judge model is set"
        raise ValueError(
            f"Judge '{judge_name}': 'provider_options' requires an 'openrouter:/' "
            f"judge model (per-judge 'model:' or 'models.judge'); {got}")
    ctx = f"Judge '{judge_name}' provider_options"
    _check_keys(options, ctx, _OPENROUTER_JUDGE_OPTION_KEYS)
    if "routing" in options:
        RoutingSpec.from_dict(options["routing"], context=f"{ctx}.routing")
    if "fallbacks" in options:
        RoutingSpec.from_dict({"fallbacks": options["fallbacks"]}, context=ctx)
    if "max_tokens" in options:
        v = options["max_tokens"]
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise ValueError(f"{ctx}.max_tokens must be an integer >= 1")
    return dict(options)


@dataclass
class ModelsConfig:
    """Default models for each role.

    Precedence (high to low):
    - skill: CLI --model > models.skill (must resolve to non-empty)
    - subagent: CLI --subagent-model > models.subagent > skill model
    - judge: per-judge JudgeConfig.model > models.judge > EVAL_JUDGE_MODEL
      env var (must resolve to non-empty for LLM judges)
    """

    skill: Optional[str] = None
    subagent: Optional[str] = None
    judge: Optional[str] = None
    hook: Optional[str] = None
    # How SDK-backed LLM judges carry their verdict (spec 015): `auto` tries the
    # provider's structured output first and the forced tool call second,
    # `json_schema` / `tool` pin one transport. A judge's own `verdict:` wins.
    judge_verdict: str = "auto"
    # Provider registry for `<provider>:/<model>` URIs on the roles above
    # (spec 014). Readers address it as `config.models.providers`.
    providers: ProvidersConfig = field(default_factory=ProvidersConfig)


# The verdict transport policies of spec 015 (`models.judge_verdict`, per-judge
# `verdict`). `auto` = structured output first, forced tool second.
VERDICT_POLICIES = ("auto", "json_schema", "tool")


def _check_verdict_policy(value, label):
    if value is None or value == "":
        return "auto"
    if not isinstance(value, str) or value not in VERDICT_POLICIES:
        raise ValueError(f"{label} must be one of {', '.join(VERDICT_POLICIES)}; got {value!r}")
    return value


@dataclass
class GenerationSeed:
    """One seed in a synthetic ``generation`` block.

    Each seed produces ``count`` test cases of a given ``category`` from a
    generation prompt. The prompt is chosen by exactly one discriminator
    (mirroring judges):

    - ``builtin`` — a builtin generation prompt, e.g. ``docs/navigation``
      (from ``agent_eval/prompts/``)
    - ``prompt_file`` — a project file path, relative to the eval config
    - ``prompt`` — an inline prompt string

    ``category`` is stamped onto every generated case as ``annotations.category``.
    """
    category: str
    count: int
    builtin: str = ""
    prompt_file: str = ""
    prompt: str = ""
    description: str = ""


#: Valid ``generation.strategy`` values (case provenance).
GENERATION_STRATEGIES = ("skill", "synthetic", "from-traces")


@dataclass
class GenerationConfig:
    """Test-case generation provenance (how ``/eval-dataset`` sources cases).

    ``strategy`` selects the source: ``skill`` (agent authors from skill
    analysis — the default), ``synthetic`` (LLM generates from ``seeds`` +
    ``context``), or ``from-traces`` (extracted from MLflow production traces).
    ``seeds`` and ``context`` apply only to ``synthetic``.
    """
    strategy: str = "skill"
    context: Union[str, dict] = field(default_factory=dict)
    seeds: list = field(default_factory=list)  # List of GenerationSeed


#: Valid ``judges[].examples.source`` values — where exemplars are harvested
#: from. Only prior runs' review.yaml today; a future source (a curated file,
#: MLflow feedback) extends this tuple and the harvester dispatch.
JUDGE_EXAMPLE_SOURCES = ("reviews",)


@dataclass
class JudgeExamplesConfig:
    """Few-shot exemplars injected into an LLM/agent judge prompt.

    ``source`` selects the harvest source (``reviews``: human labels from
    prior runs' review.yaml, both the flat ``feedback`` map and the
    per-judge ``verdicts`` map — hand-authored today, no skill writes it yet).
    ``count`` caps how
    many exemplars are injected per case; ``mix`` lists which verdict
    classes are eligible (default both, so the judge sees a clear pass AND
    a clear fail whenever the pool has them).
    """

    source: str = "reviews"
    count: int = 3
    mix: list = field(default_factory=lambda: ["pass", "fail"])


@dataclass
class JudgeConfig:
    """Configuration for a single judge.

    Judge types (determined by which fields are set):
    - Inline check: `check` contains a Python snippet
    - LLM judge: `prompt`, `prompt_file`, or `llm_rubric` contains evaluation instructions
    - External code: `module` and `function` reference a Python callable
    - Builtin: `builtin` references a registered judge from agent_eval/judges/

    LLM judge fields (all compile to same internal prompt before rendering):

    Priority order: llm_rubric > prompt > prompt_file

    1. llm_rubric — Syntactic sugar for simple evaluation criteria.
       Automatically appends "{{ conversation }}" template if not present.
       Use for concise, criteria-focused judges in synthetic-generation configs.
       Example: llm_rubric: "Agent cited relevant documentation sources"

    2. prompt — Full Jinja2 template with manual control over structure.
       Use when you need multiple placeholders or complex prompt logic.
       Use {{ conversation }} for response quality, {{ tool_trace }} for behavior (navigation, tool usage).
       Example: prompt: "{{ description }}\n\nCase: {{ outputs.case_id }}\n\n{{ conversation }}"

    3. prompt_file — External file path (absolute or relative to project root).
       Use for sharing prompts across multiple judges or configs.
       File can contain either rubric-style (auto-wrapped) or full template.

    All three compile to the same internal prompt variable: llm_rubric gets
    wrapped, prompt_file gets loaded, then Jinja2 renders with case data.
    """

    name: str = ""
    description: str = ""  # What this judge checks (context for LLM judges)
    # Condition — Python expression evaluated against the outputs dict.
    # If it returns False, the judge is skipped for that case (not counted
    # in pass_rate or mean).  Example: "not annotations.get('dedup_is_duplicate')"
    condition: str = ""
    # Inline code check (returns (bool, str))
    check: str = ""
    # LLM judge fields (see docstring above for equivalence and priority)
    prompt: str = ""
    prompt_file: str = ""
    llm_rubric: str = ""
    context: list = field(
        default_factory=list
    )  # File paths loaded as supplementary context
    # Optional verdict shape: "bool" (pass/fail) vs "int"/"float" (numeric
    # score). Never inferred — an omitted value means numeric, and int-vs-float
    # is then read off `score_range` (whole bounds => integer). Only "", "bool",
    # "int", and "float" are supported; LLM/agent judges emit a bool or numeric
    # verdict (see the judge paths in score.py). Rejected at config load
    # otherwise.
    feedback_type: str = ""
    # Numeric scale [lo, hi] for this judge's value. When declared it is stated
    # in the LLM judge's system prompt and tool schema, enforced on the returned
    # value (an off-scale value is recorded as an error sample, not clamped),
    # used by the report to color per-cell bands proportionally, and used to
    # normalize this judge in the reward composition. If omitted, LLM
    # judges are told [1, 5] and nothing is enforced — an inline check returning
    # a raw count keeps returning it. Set explicitly for judges on a non-default
    # range (e.g. 0-2, 1-10, 0-100). This is the scale EVERY reward composition
    # normalizes the judge over; `reward.score_range` is only a fallback for
    # composed judges that declare none.
    score_range: Optional[list] = None
    model: str = ""  # Override model for this judge (pairwise, LLM)
    # External code judge
    module: str = ""
    function: str = ""
    # Builtin judge (resolves via BuiltinJudgeRegistry)
    builtin: str = ""
    # Arguments passed as **kwargs to Python judges, Jinja var to LLM judges
    arguments: dict = field(default_factory=dict)
    # Multi-step: scope this judge to one execution step's sub-record. Empty =
    # whole case (final workspace), the default. Must match an execution.steps id.
    step: str = ""
    # Sampling — run this judge N times per case and reduce (median/majority).
    # Only meaningful for stochastic (LLM and agent) judges; ignored otherwise.
    samples: int = 1
    # Few-shot examples harvested from prior runs' human review labels and
    # injected into the rendered prompt ({{ examples }}, or an appended
    # delimited section). LLM and agent judges only — rejected at load on
    # check/builtin/code judges, where no prompt would carry them.
    examples: Optional[JudgeExamplesConfig] = None
    # Agent judge — presence of this block upgrades an (otherwise LLM) judge to
    # a tool-using agent run through the runner abstraction, with read-only file
    # tools and a staged, isolated workspace. Permissive mapping (mirrors
    # `arguments`); recognized keys: runner (RunnerConfig), allowed_tools,
    # context, inputs, timeout, max_budget_usd. A nested `runner:` sub-block is
    # parsed into a RunnerConfig by from_yaml.
    agent: dict = field(default_factory=dict)
    # Provider-specific judge options (spec 014), validated by the provider kind
    # of the judge's model at load (`validate_judge_provider_options`). For an
    # `openrouter:/` judge: `routing` (per-judge RoutingSpec override — opts the
    # judge into pins), `fallbacks`, `max_tokens`.
    provider_options: dict = field(default_factory=dict)
    # Verdict transport override for this judge (spec 015): "" inherits
    # `models.judge_verdict`; `auto` | `json_schema` | `tool` otherwise.
    verdict: str = ""


@dataclass
class RewardConfig:
    """Reward composition from judge results for RL training.

    Two ways to produce the reward, mutually exclusive:

    1. ``judge``: a single judge whose value IS the reward. By default the
       value is used as-is, clamped to [0, 1] (for a judge that already emits
       a [0, 1] reward, e.g. a learned reward model). Set ``normalize: true``
       to instead map it from the judge's own ``score_range`` to [0, 1].
    2. ``formula`` (+ ``weights``): compose from multiple judges —
       - "weighted": weighted sum of ``weights``, each normalized over its own
         declared ``score_range`` (or clamped if listed in ``raw``).
       - "<expression>": Python expression with judge names as variables.

    When gate is True, any boolean judge that returned False zeros the reward.
    Note this gates on *every* boolean judge, independent of whether the
    formula references it — so an ``<expression>`` that uses booleans as its
    own gate (e.g. ``passed * score``) usually wants ``gate: false`` to avoid
    double-gating. ``gate`` defaults to False in ``judge`` mode.
    score_range: DEPRECATED fallback, used only for composed judges that
         declare no ``score_range`` of their own ([1, 5] when absent — read it
         through ``effective_score_range``).
    raw: list of judge names whose values are already in [0, 1] and should
         be clamped rather than normalized over any range (e.g. efficiency).
    """

    formula: str = "weighted"
    weights: dict = field(default_factory=dict)
    gate: bool = True
    # Fallback scale for composed numeric judges that declare no `score_range`
    # of their own. DEPRECATED — declare the scale on the judge instead.
    # `None` means "absent from the YAML", which is what makes the deprecation
    # warning targetable. Read it through `effective_score_range`.
    score_range: Optional[list] = None
    raw: list = field(default_factory=list)
    # Single-judge mode: name of the judge whose value is the reward.
    judge: Optional[str] = None
    # In judge mode, map the value from the judge's own score_range instead of
    # clamping as-is.
    normalize: bool = False

    @property
    def effective_score_range(self) -> list:
        """The fallback range, resolved. Never ``None``."""
        return list(self.score_range) if self.score_range else [1.0, 5.0]


def _reward_normalized_judges(reward, judge_names: set) -> set:
    """Judges whose value the reward composition normalizes over a range.

    Excludes `raw` judges, a clamped single judge, and names a formula never
    reads — none of those consult a range, so a range conflict cannot move
    them, and warning about them would be noise.
    """
    if reward.judge is not None:
        return {reward.judge} if reward.normalize else set()
    formula = (reward.formula or "").strip()
    if formula == "weighted":
        named = set(reward.weights)
    else:
        from agent_eval.harbor.reward import formula_judge_names
        named = formula_judge_names(formula) & judge_names
    return named - set(reward.raw)


def _warn_reward_range_precedence(config) -> None:
    """Warn when a written `reward.score_range` no longer governs a judge.

    Only fires when the key is present in the YAML AND a judge it would have
    normalized declares a different range of its own — i.e. only where the
    precedence change actually moves a number.
    """
    reward = config.reward
    ranges = {j.name: [float(j.score_range[0]), float(j.score_range[1])]
              for j in config.judges if getattr(j, "score_range", None)}
    composed = _reward_normalized_judges(
        reward, {j.name for j in config.judges if j.name})
    fallback = reward.effective_score_range
    shadowed = sorted(n for n in composed if n in ranges and ranges[n] != fallback)
    if not shadowed:
        return
    still = sorted(n for n in composed if n not in ranges)
    tail = (f"It still applies to {', '.join(repr(n) for n in still)}; drop it "
            "once every composed judge declares a 'score_range'."
            if still else
            "No composed judge relies on it any more — delete it.")
    import warnings
    warnings.warn(
        f"reward.score_range {fallback} is deprecated and no longer normalizes "
        + ", ".join(f"'{n}' {ranges[n]}" for n in shadowed)
        + ": a judge's own 'score_range' wins. " + tail,
        stacklevel=2)


def _fmt_num(value) -> str:
    """Render a bound without a pointless trailing .0 — config coerces to float."""
    return str(int(value)) if float(value).is_integer() else str(value)


def _warn_reward_judge_clamp(config) -> None:
    """Warn when a clamped single-judge reward is scored off [0, 1].

    `reward: {judge: x}` uses x's value as the reward directly, clamped — the
    right thing for a judge that already emits [0, 1] and wrong for every other
    scale, silently. A scale reaching 1 or beyond saturates: every value at or
    above 1 is the maximum reward. A narrower one (say [0, 0.5]) never
    saturates but never reaches the top of the reward range either. Both are
    fixed by `normalize: true`. Pre-existing; surfaced here because the same
    change makes a declared `score_range` authoritative everywhere else.
    """
    reward = config.reward
    if reward.judge is None or reward.normalize:
        return
    declared = next((j.score_range for j in config.judges
                     if j.name == reward.judge and j.score_range), None)
    if not declared or [float(declared[0]), float(declared[1])] == [0.0, 1.0]:
        return
    lo, hi = float(declared[0]), float(declared[1])
    effect = ("every score at or above 1 becomes the maximum reward"
              if hi >= 1 else
              f"the reward can never exceed {_fmt_num(hi)}")
    import warnings
    warnings.warn(
        f"reward.judge '{reward.judge}' declares score_range "
        f"[{_fmt_num(lo)}, {_fmt_num(hi)}] but 'normalize' is not set, so its "
        f"value is clamped to [0, 1] — {effect}. Set 'normalize: true' to map "
        f"it from [{_fmt_num(lo)}, {_fmt_num(hi)}].", stacklevel=2)


@dataclass
class EvalConfig:
    """Complete evaluation suite configuration.

    Structure is schema-driven: dataset and output structures are described
    in natural language. The harness interprets these descriptions via LLM
    (once, cached) to drive prepare, collect, and score steps.
    """

    name: str = ""
    description: str = ""
    skill: Optional[str] = None  # Deprecated: use execution.skill instead. Fallback for backward compat.
    permissions: dict = field(default_factory=dict)

    # Lifecycle hooks — shell commands at defined pipeline points
    hooks: HooksConfig = field(default_factory=HooksConfig)

    # Execution — how the skill is invoked (mode, arguments, timeout, budget)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)

    # Runner — which agent harness + runner-specific config
    runner: RunnerConfig = field(default_factory=RunnerConfig)

    # Models — default models for skill/subagent/judge roles
    models: ModelsConfig = field(default_factory=ModelsConfig)

    # MLflow logging target
    mlflow: MlflowConfig = field(default_factory=MlflowConfig)

    # Dataset — location, schema, and workspace file provisioning
    dataset: DatasetConfig = field(default_factory=DatasetConfig)

    # Generation — synthetic test-case generation (optional, prompt-mode)
    generation: GenerationConfig = field(default_factory=GenerationConfig)

    # Outputs — file artifacts and/or tool calls
    outputs: list = field(default_factory=list)

    # Inputs — tool interception for headless execution
    inputs: InputsConfig = field(default_factory=InputsConfig)

    # Traces — execution metadata to capture
    traces: TracesConfig = field(default_factory=TracesConfig)

    # Judges (inline checks, LLM, pairwise, external code)
    judges: list = field(default_factory=list)

    # Reward composition for RL training (optional)
    reward: Optional[RewardConfig] = None

    # Regression thresholds
    thresholds: dict = field(default_factory=dict)

    # Directory containing the eval.yaml that created this config.
    # Used as base for resolving dataset.path. None when constructed
    # programmatically (falls back to Path.cwd()).
    config_dir: Optional[Path] = None

    # Full path to the eval.yaml file (for eval_name derivation).
    # None when constructed programmatically. With `extends:` this is the ROOT
    # of the chain (the base file), so dataset.path and eval_name resolve as
    # they would for the base.
    config_path: Optional[Path] = None

    # Files that produced this config, root first, project-relative
    # (`["eval.yaml"]` for a plain config, `["eval.yaml",
    # "eval-profiles/x.yaml"]` for an overlay). Surfaced in
    # run_result.eval_params.config_chain and Harbor task.toml metadata.
    config_chain: list = field(default_factory=list)

    # Runtime overrides (set by CLI or skill, not config file)
    model: str = ""
    subagent_model: str = ""
    run_id: str = ""
    baseline: str = ""

    def __post_init__(self):
        if self.skill and not self.execution.skill:
            self.execution.skill = self.skill

    def resolve_path(self, relative: Path | str) -> Path:
        """Resolve a path relative to the config file's directory.

        Absolute paths are returned as-is. Relative paths resolve against
        config_dir (falling back to cwd when config_dir is None).
        """
        p = Path(relative)
        if p.is_absolute():
            return p
        base = self.config_dir if self.config_dir is not None else Path.cwd()
        return base / p

    def resolve_skill(self) -> Optional[str]:
        """Canonical skill name for skill mode, or None for prompt mode.

        Prefers ``execution.skill`` (the current location) and falls back to
        the deprecated top-level ``skill`` field.  Returns None when neither
        is set — i.e. prompt mode or an unconfigured target.  All execution
        substrates (local, Harbor, EvalHub) MUST resolve the target through
        this method so a config authored with only ``execution.skill`` runs
        the skill instead of silently degrading to prompt mode.
        """
        return self.execution.skill or self.skill or None

    def is_prompt_mode(self) -> bool:
        """True when the eval runs a direct prompt (no skill wrapper)."""
        return bool(self.execution.prompt and self.execution.prompt.strip())

    def eval_name(self) -> str:
        """Derive eval identifier with backward-compatible fallback chain.

        Priority order (backward-compatible with existing skill evals):
        1. skill field - preserves existing skill-based eval runs
        2. name field - allows explicit naming for prompt-mode evals
        3. directory/filename - pure path-based derivation
        4. "eval" - final fallback

        This ensures existing skill evals continue to work while enabling
        prompt mode to use either explicit names or path-based identifiers.
        """
        # Priority 1: skill field (backward compat with existing evals).
        # Resolve through resolve_skill() so execution.skill-only configs
        # still name the run after the skill under test.
        skill = self.resolve_skill()
        if skill:
            return skill

        # Priority 2: name field (explicit identifier, sanitized)
        # Skip if name == path.stem (auto-set default from from_yaml)
        if self.name and not (self.config_path and self.name == self.config_path.stem):
            # Sanitize: convert spaces to hyphens, keep only safe chars
            sanitized = self.name.lower().replace(" ", "-")
            sanitized = "".join(c for c in sanitized if c.isalnum() or c in "._-")
            if sanitized and _is_valid_eval_name(sanitized):
                return sanitized

        # Priority 3: derive from path (new behavior for prompt mode)
        if self.config_path:
            if self.config_path.name == "eval.yaml":
                # Nested: eval/user-guides/eval.yaml → "user-guides"
                # Check if grandparent directory is named "eval"
                if self.config_path.parent.parent.name == "eval":
                    return self.config_path.parent.name
                # Root: eval.yaml at project root → "eval"
                else:
                    return "eval"
            # Flat: eval/user-guides.yaml → "user-guides"
            else:
                return self.config_path.stem

        # Final fallback
        return "eval"

    @classmethod
    def from_yaml(cls, path: str | Path) -> "EvalConfig":
        """Load config from a YAML file."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config not found: {path}")

        # The single raw loader resolves `extends:`; paths below (dataset,
        # eval_name) are taken from the ROOT of the chain, the base config.
        raw, chain = load_raw(path)
        root_path = Path(chain[0])

        # Deprecation: top-level `skill:` is auto-normalized into
        # execution.skill (below) but the canonical home is the execution
        # block, symmetric with execution.prompt. Warn once per load; only
        # for a non-empty value that isn't already mirrored in execution.
        exec_raw = raw.get("execution", {})
        if raw.get("skill") and not (exec_raw.get("skill") or "").strip():
            import warnings
            warnings.warn(
                f"Top-level 'skill:' in {path} is deprecated; move it under "
                "execution.skill (it is auto-normalized for now and will be "
                "removed in a future release).",
                DeprecationWarning,
                stacklevel=2,
            )

        # Dataset
        dataset = raw.get("dataset", {})

        # Execution config — including an optional multi-step pipeline.
        steps = []
        for i, s in enumerate(exec_raw.get("steps") or []):
            if not isinstance(s, dict):
                raise ValueError(f"execution.steps[{i}] must be a mapping")
            step_runner = None
            if s.get("runner"):
                step_runner = _parse_runner_config(
                    s.get("runner"), context=f"execution.steps[{i}].runner")
            step_env = s.get("env") or {}
            if not isinstance(step_env, dict):
                raise ValueError(
                    f"execution.steps[{i}].env must be a mapping")
            step_timeout = s.get("timeout")
            if step_timeout is not None and (
                    not isinstance(step_timeout, int)
                    or isinstance(step_timeout, bool)
                    or step_timeout <= 0):
                raise ValueError(
                    f"execution.steps[{i}].timeout must be a positive integer")
            step_budget = s.get("max_budget_usd")
            if step_budget is not None and (
                    not isinstance(step_budget, (int, float))
                    or isinstance(step_budget, bool)
                    or step_budget < 0):
                raise ValueError(
                    f"execution.steps[{i}].max_budget_usd must be a "
                    "non-negative number")
            step = StepConfig(
                id=s.get("id", "") or "",
                name=s.get("name", "") or "",
                skill=s.get("skill", "") or "",
                prompt=s.get("prompt", "") or "",
                arguments=s.get("arguments", "") or "",
                env=step_env,
                timeout=step_timeout,
                max_budget_usd=step_budget,
                runner=step_runner,
                on_failure=s.get("on_failure", "fail"),
            )
            if not ((step.skill and step.skill.strip())
                    or (step.prompt and step.prompt.strip())):
                raise ValueError(
                    f"execution.steps[{i}] ('{step.id}') must set either "
                    "skill or prompt")
            steps.append(step)

        execution = ExecutionConfig(
            mode=exec_raw.get("mode", "case"),
            skill=exec_raw.get("skill", "") or raw.get("skill", ""),
            prompt=exec_raw.get("prompt", ""),
            arguments=exec_raw.get("arguments", ""),
            timeout=exec_raw.get("timeout"),
            max_budget_usd=exec_raw.get("max_budget_usd"),
            parallelism=exec_raw.get("parallelism"),
            env=exec_raw.get("env") or {},
            project_hooks=exec_raw.get("project_hooks", True),
            steps=steps,
        )

        # Runner config (block form)
        runner = _parse_runner_config(raw.get("runner"), context="runner")

        # Models block (+ the provider registry, spec 014 Decision 17)
        if "providers" in raw:
            raise ValueError(
                "'providers' moved: declare providers under `models.providers` "
                "(spec 014 Decision 17)")
        models_raw = raw.get("models", {}) or {}
        models = ModelsConfig(
            skill=models_raw.get("skill"),
            subagent=models_raw.get("subagent"),
            judge=models_raw.get("judge"),
            hook=models_raw.get("hook"),
            judge_verdict=_check_verdict_policy(models_raw.get("judge_verdict"),
                                                "models.judge_verdict"),
            providers=_parse_providers(models_raw.get("providers")),
        )

        # MLflow block. Experiment defaults to the eval's top-level
        # `name` only when an `mlflow:` block is present — so omitting
        # the block entirely leaves MLflow off (no accidental experiment
        # creation on shared tracking servers).
        has_mlflow_block = "mlflow" in raw and raw["mlflow"] is not None
        mlflow_raw = raw.get("mlflow") or {}
        if has_mlflow_block:
            experiment = mlflow_raw.get("experiment") or raw.get("name", "")
        else:
            experiment = ""
        mlflow = MlflowConfig(
            experiment=experiment,
            tracking_uri=mlflow_raw.get("tracking_uri"),
            tags=mlflow_raw.get("tags", {}) or {},
        )

        # Dataset — path, schema, and workspace file provisioning
        ws_raw = dataset.get("workspace", {}) or {}
        ws_files_raw = ws_raw.get("files", []) or []
        ws_files = []
        for i, f in enumerate(ws_files_raw):
            if isinstance(f, str):
                ws_files.append(
                    _validate_relative_path(f.rstrip("/"), "dataset.workspace.files")
                )
            elif isinstance(f, dict):
                field_ctx = f"dataset.workspace.files[{i}]"
                missing = {"dest", "source"} - set(f)
                if missing:
                    raise ValueError(
                        f"{field_ctx} is missing required key(s): "
                        f"{', '.join(sorted(missing))}"
                    )
                extra = set(f) - {"dest", "source"}
                if extra:
                    raise ValueError(
                        f"{field_ctx} has unknown key(s): {', '.join(sorted(extra))} "
                        f"(only 'dest' and 'source' are allowed)"
                    )
                dest, source = f["dest"], f["source"]
                if not isinstance(dest, str) or not dest:
                    raise ValueError(f"{field_ctx}.dest must be a non-empty string")
                if not isinstance(source, str) or not source:
                    raise ValueError(f"{field_ctx}.source must be a non-empty string")
                # dest becomes a filesystem path inside the workspace, so reject
                # traversal/absolute. source is resolved (and containment-checked)
                # at materialization time, so it may be relative or absolute here.
                ws_files.append(
                    WorkspaceFile(
                        # "/" strips to "" (which _validate_relative_path would
                        # wave through); normalize to "." so reject_root catches it.
                        dest=_validate_relative_path(
                            dest.rstrip("/") or ".", f"{field_ctx}.dest",
                            reject_root=True,
                        ),
                        source=source,
                    )
                )
            else:
                raise ValueError(
                    f"dataset.workspace.files[{i}] must be a string or a "
                    f"{{dest, source}} mapping, got {type(f).__name__}"
                )
        dataset_config = DatasetConfig(
            path=_validate_relative_path(
                dataset.get("path", ""), "dataset.path", allow_absolute=True
            ),
            schema=dataset.get("schema", ""),
            workspace=WorkspaceConfig(files=ws_files),
        )
        # Generation — synthetic test-case generation (optional) with validation
        gen_raw = raw.get("generation") or {}
        seeds = []
        for i, s in enumerate(gen_raw.get("seeds") or []):
            category = s.get("category", "")
            count = s.get("count")
            if not category or not isinstance(category, str):
                raise ValueError(
                    f"generation.seeds[{i}].category must be a non-empty string, got: {category!r}")
            # count is required — a silent default would swallow a mistyped field name
            if not isinstance(count, int) or count < 1:
                raise ValueError(
                    f"generation.seeds[{i}].count must be an integer >= 1, got: {count!r}")

            # Exactly one prompt discriminator (mirrors judges: builtin/prompt_file/prompt)
            discriminators = [
                k for k in ("builtin", "prompt_file", "prompt") if s.get(k)
            ]
            if len(discriminators) != 1:
                raise ValueError(
                    f"generation.seeds[{i}] ('{category}') must set exactly one of "
                    f"builtin / prompt_file / prompt, got: {discriminators or 'none'}")

            seeds.append(GenerationSeed(
                category=category,
                count=count,
                builtin=s.get("builtin", ""),
                prompt_file=s.get("prompt_file", ""),
                prompt=s.get("prompt", ""),
                description=s.get("description", ""),
            ))

        # Provenance: absent normalizes to 'skill' (the default source).
        strategy = gen_raw.get("strategy") or "skill"
        if strategy not in GENERATION_STRATEGIES:
            raise ValueError(
                f"generation.strategy must be one of "
                f"{', '.join(GENERATION_STRATEGIES)}, got: {strategy!r}")
        if strategy == "synthetic" and not seeds:
            raise ValueError(
                "generation.strategy is 'synthetic' but generation.seeds is empty.")
        if seeds and strategy != "synthetic":
            raise ValueError(
                f"generation.seeds are only valid with strategy: synthetic "
                f"(got strategy: {strategy}).")

        generation_config = GenerationConfig(
            strategy=strategy,
            context=gen_raw.get("context", {}),
            seeds=seeds,
        )

        config = cls(
            name=raw.get("name", root_path.stem),
            description=raw.get("description", ""),
            skill=raw.get("skill") or None,  # Convert empty string to None
            permissions=raw.get("permissions", {}),
            execution=execution,
            runner=runner,
            models=models,
            mlflow=mlflow,
            config_dir=root_path.parent,
            config_path=root_path,
            config_chain=config_chain_display(chain),
            dataset=dataset_config,
            generation=generation_config,
        )

        # Outputs (path or tool)
        for i, o in enumerate(raw.get("outputs", [])):
            config.outputs.append(
                OutputConfig(
                    path=_validate_relative_path(
                        o.get("path", ""), f"outputs[{i}].path", reject_root=True
                    ),
                    tool=o.get("tool", ""),
                    schema=o.get("schema", ""),
                    batch_pattern=o.get("batch_pattern", ""),
                    types=o.get("types") or None,
                )
            )

        # Inputs (tool interception)
        inputs_raw = raw.get("inputs", {})
        for t in inputs_raw.get("tools") or []:
            config.inputs.tools.append(
                ToolInputConfig(
                    match=t.get("match", ""),
                    prompt=t.get("prompt", ""),
                    prompt_file=t.get("prompt_file", ""),
                )
            )

        # Traces
        traces = raw.get("traces", {})
        if traces:
            config.traces = TracesConfig(
                stdout=traces.get("stdout", True),
                stderr=traces.get("stderr", True),
                events=traces.get("events", True),
                metrics=traces.get("metrics", True),
            )

        # Judges
        for j in raw.get("judges", []):
            builtin_val = j.get("builtin", "")
            if builtin_val is None:
                builtin_val = ""
            if not isinstance(builtin_val, str):
                raise ValueError(
                    f"Judge '{j.get('name', '')}': 'builtin' must be a string"
                )
            args_val = j.get("arguments")
            if args_val is None:
                args_val = {}
            elif not isinstance(args_val, dict):
                raise ValueError(
                    f"Judge '{j.get('name', '')}': 'arguments' must be a mapping"
                )
            agent_val = j.get("agent")
            if agent_val is None:
                agent_val = {}
            elif not isinstance(agent_val, dict):
                raise ValueError(
                    f"Judge '{j.get('name', '')}': 'agent' must be a mapping"
                )
            elif agent_val.get("runner") is not None:
                # Parse the nested runner: sub-block with the SAME block-parsing
                # logic as the top-level runner, so a judge's runner is fully
                # validated. Shallow-copy so the raw YAML isn't mutated.
                if not isinstance(agent_val["runner"], dict):
                    raise ValueError(
                        f"Judge '{j.get('name', '')}': 'agent.runner' must be a mapping"
                    )
                agent_val = dict(agent_val)
                agent_val["runner"] = _parse_runner_config(
                    agent_val["runner"],
                    context=f"Judge '{j.get('name', '')}': agent.runner",
                )
            score_range_val = j.get("score_range")
            if score_range_val is not None:
                jname = j.get("name", "")
                if (not isinstance(score_range_val, list)
                        or len(score_range_val) != 2):
                    raise ValueError(
                        f"Judge '{jname}': 'score_range' must be a [min, max] list")
                try:
                    lo, hi = float(score_range_val[0]), float(score_range_val[1])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"Judge '{jname}': 'score_range' values must be numeric") from exc
                if not (math.isfinite(lo) and math.isfinite(hi)) or lo >= hi:
                    raise ValueError(
                        f"Judge '{jname}': 'score_range' must be finite and "
                        "increasing [min, max]")
                score_range_val = [lo, hi]
            examples_val = j.get("examples")
            if examples_val is not None:
                jname = j.get("name", "")
                if not isinstance(examples_val, dict):
                    raise ValueError(
                        f"Judge '{jname}': 'examples' must be a mapping")
                unknown = set(examples_val) - {"source", "count", "mix"}
                if unknown:
                    raise ValueError(
                        f"Judge '{jname}': 'examples' has unknown key(s): "
                        f"{', '.join(sorted(unknown))} (only 'source', "
                        "'count' and 'mix' are allowed)")
                ex_source = examples_val.get("source", "reviews")
                if ex_source not in JUDGE_EXAMPLE_SOURCES:
                    raise ValueError(
                        f"Judge '{jname}': examples.source must be one of "
                        f"{', '.join(JUDGE_EXAMPLE_SOURCES)}, got: {ex_source!r}")
                ex_count = examples_val.get("count", 3)
                if (not isinstance(ex_count, int) or isinstance(ex_count, bool)
                        or ex_count < 1):
                    raise ValueError(
                        f"Judge '{jname}': examples.count must be an integer "
                        f">= 1, got: {ex_count!r}")
                ex_mix = examples_val.get("mix", ["pass", "fail"])
                if (not isinstance(ex_mix, list) or not ex_mix
                        or any(m not in ("pass", "fail") for m in ex_mix)
                        or len(set(ex_mix)) != len(ex_mix)):
                    raise ValueError(
                        f"Judge '{jname}': examples.mix must be a non-empty "
                        "list of unique values from 'pass'/'fail', got: "
                        f"{ex_mix!r}")
                examples_val = JudgeExamplesConfig(
                    source=ex_source, count=ex_count, mix=list(ex_mix))
            config.judges.append(
                JudgeConfig(
                    name=j.get("name", ""),
                    description=j.get("description", ""),
                    condition=j.get("if", ""),
                    check=j.get("check", ""),
                    prompt=j.get("prompt", ""),
                    prompt_file=j.get("prompt_file", ""),
                    llm_rubric=j.get("llm_rubric", ""),
                    context=j.get("context", []),
                    feedback_type=j.get("feedback_type", ""),
                    score_range=score_range_val,
                    model=j.get("model", ""),
                    module=j.get("module", ""),
                    function=j.get("function", ""),
                    builtin=builtin_val,
                    arguments=args_val,
                    step=j.get("step", "") or "",
                    samples=int(j.get("samples", 1)),
                    examples=examples_val,
                    agent=agent_val,
                    provider_options=j.get("provider_options") or {},
                    verdict=_check_verdict_policy(j.get("verdict"),
                                                  f"judge '{j.get('name', '')}' verdict")
                    if j.get("verdict") not in (None, "") else "",
                )
            )

        # Per-step judge scoping: a judge's `step:` must name a defined
        # execution step (fail loud on typos, like reward.judge validation).
        step_ids = {s.id for s in execution.steps}
        for jc in config.judges:
            if not jc.step:
                continue
            if not execution.steps:
                raise ValueError(
                    f"Judge '{jc.name}': 'step: {jc.step}' requires an "
                    "execution.steps pipeline")
            if jc.step not in step_ids:
                raise ValueError(
                    f"Judge '{jc.name}': 'step: {jc.step}' does not match any "
                    f"execution step id ({sorted(step_ids)})")

        # Scale coherence: a judge's declared scale has to agree with its
        # feedback_type and with the scorer that will actually run it. Each of
        # these used to be accepted and then quietly ignored at scoring time,
        # which is how a judge shipped scoring on a scale nobody declared.
        from agent_eval.judges import builtin_judge_kind, builtin_judge_names

        for jc in config.judges:
            builtin_kind = builtin_judge_kind(jc.builtin) if jc.builtin else None
            if jc.builtin and builtin_kind is None:
                raise ValueError(
                    f"Judge '{jc.name}': unknown builtin judge '{jc.builtin}' "
                    f"(available: {', '.join(builtin_judge_names())})")
            if jc.feedback_type not in ("", "bool", "int", "float"):
                raise ValueError(
                    f"Judge '{jc.name}': unsupported feedback_type "
                    f"'{jc.feedback_type}' — use 'bool', 'int', or 'float'. "
                    "Categorical ('str'/'Literal[...]') verdicts are not "
                    "supported by the LLM/agent judge paths.")
            if jc.feedback_type == "bool" and jc.score_range:
                raise ValueError(
                    f"Judge '{jc.name}': 'score_range' has no meaning with "
                    "'feedback_type: bool' (the verdict is pass/fail) — "
                    "drop one of the two")
            if (jc.feedback_type == "int" and jc.score_range
                    and any(float(b) != int(b) for b in jc.score_range)):
                raise ValueError(
                    f"Judge '{jc.name}': 'feedback_type: int' cannot express "
                    f"the fractional 'score_range' {jc.score_range} — use "
                    "'feedback_type: float'")
            if (builtin_kind == "llm"
                    and (jc.feedback_type not in ("", "bool") or jc.score_range)):
                raise ValueError(
                    f"Judge '{jc.name}': builtin LLM judge '{jc.builtin}' is "
                    "always scored as pass/fail, so 'feedback_type'/"
                    "'score_range' would be silently ignored")
            # `examples` only exists where the harness renders the judge's
            # own prompt — LLM and agent judges. On any other type the block
            # would be silently ignored, so reject it at load like the
            # score_range declarations above.
            if jc.examples and (jc.builtin or jc.check
                                or jc.module or jc.function):
                kind = ("builtin" if jc.builtin
                        else "check" if jc.check else "code")
                raise ValueError(
                    f"Judge '{jc.name}': 'examples' only applies to LLM and "
                    "agent judges (prompt/prompt_file/llm_rubric/agent) — on "
                    f"a {kind} judge it would be silently ignored")
            # The reserved pairwise judge never goes through the per-judge
            # prompt renderer (score.py routes it into the A/B comparison
            # flow), so an `examples` block there would be silently ignored
            # too.
            if jc.examples and jc.name == "pairwise":
                raise ValueError(
                    "Judge 'pairwise': 'examples' does not apply to the "
                    "pairwise comparison judge — its prompt is rendered by "
                    "the comparison flow, which never injects exemplars")
            # `feedback_type` is optional, and score.py's `_numeric_bounds`
            # treats anything that is not "bool" as numeric — so the judge that
            # most needs this warning is the one that declares neither field,
            # and gating on ("int", "float") alone never reached it.
            # The judge named "pairwise" is exempt: score.py routes it past
            # the numeric path by that name into the A/B/tie verdict flow, so
            # no scale — declared or defaulted — is ever applied to it.
            if (jc.feedback_type in ("int", "float", "") and not jc.score_range
                    and not jc.builtin and jc.name != "pairwise"
                    and (jc.prompt or jc.prompt_file or jc.llm_rubric)):
                import warnings
                warnings.warn(
                    f"Judge '{jc.name}': numeric judge has no 'score_range', "
                    "so it is scored on the unenforced [1, 5] default — "
                    "declare one to have the returned value checked",
                    stacklevel=2)

        # Reward composition
        if "reward" in raw:
            reward_raw = raw.get("reward")
            if not isinstance(reward_raw, dict):
                raise ValueError("reward must be a mapping when provided")
            sr = reward_raw.get("score_range")
            reward_score_range = None
            if sr is not None:
                if not isinstance(sr, list) or len(sr) != 2:
                    raise ValueError(
                        "reward.score_range must be a [min, max] list")
                try:
                    score_min = float(sr[0])
                    score_max = float(sr[1])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        "reward.score_range values must be numeric") from exc
                if (not (math.isfinite(score_min) and math.isfinite(score_max))
                        or score_min >= score_max):
                    raise ValueError(
                        "reward.score_range must be finite and increasing "
                        "[min, max]")
                reward_score_range = [score_min, score_max]
            weights = reward_raw.get("weights", {}) or {}
            if not isinstance(weights, dict):
                raise ValueError("reward.weights must be a mapping")
            try:
                weights = {str(k): float(v) for k, v in weights.items()}
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "reward.weights values must be numeric") from exc
            if any(v < 0 for v in weights.values()):
                raise ValueError("reward.weights values must be non-negative")
            raw_list = reward_raw.get("raw", []) or []
            if not isinstance(raw_list, list):
                raw_list = [raw_list]
            # Single-judge mode: one judge's value is the reward. Mutually
            # exclusive with the composition inputs.
            judge = reward_raw.get("judge")
            if judge is not None:
                if not isinstance(judge, str) or not judge.strip():
                    raise ValueError(
                        "reward.judge must be a non-empty judge name")
                conflicting = [k for k in ("formula", "weights", "raw")
                               if k in reward_raw]
                if conflicting:
                    raise ValueError(
                        "reward.judge cannot be combined with "
                        f"{'/'.join(conflicting)}")
                judge_names = {j.name for j in config.judges if j.name}
                if judge not in judge_names:
                    raise ValueError(
                        f"reward.judge '{judge}' does not match any defined "
                        "judge")
            normalize = reward_raw.get("normalize", False)
            if not isinstance(normalize, bool):
                raise ValueError("reward.normalize must be a boolean")
            # gate defaults to False in judge mode, True for composition.
            gate = reward_raw.get("gate", judge is None)
            if not isinstance(gate, bool):
                raise ValueError("reward.gate must be a boolean")
            formula = str(reward_raw.get("formula", "weighted"))
            # Validate expression formulas now so a typo or unsafe construct
            # fails loudly here, not silently as reward 0.0 on every case at
            # run time. Bare references ("weighted") are resolved at compute
            # time, so skip the expression check for them. Skipped in judge
            # mode, where formula is unused.
            if judge is None and not re.fullmatch(
                    r"[A-Za-z_][\w.\-]*", formula.strip()):
                from agent_eval.harbor.reward import validate_formula
                try:
                    validate_formula(formula)
                except ValueError as exc:
                    raise ValueError(
                        f"reward.formula is invalid: {exc}") from exc
            config.reward = RewardConfig(
                formula=formula,
                weights=weights,
                gate=gate,
                score_range=reward_score_range,
                raw=[str(r) for r in raw_list],
                judge=judge,
                normalize=normalize,
            )
            if sr is not None:
                _warn_reward_range_precedence(config)
            _warn_reward_judge_clamp(config)

        # Thresholds
        config.thresholds = raw.get("thresholds", {})

        # Hooks
        hooks_raw = raw.get("hooks", {}) or {}
        phases = ["before_all", "before_each", "after_each", "before_step",
                  "after_step", "before_scoring", "after_all", "before_report"]
        for phase in phases:
            entries = []
            for h in (hooks_raw.get(phase) or []):
                on_failure_val = h.get("on_failure", "fail")
                if on_failure_val not in ("fail", "continue"):
                    raise ValueError(
                        f"hooks.{phase}: on_failure must be 'fail' or "
                        f"'continue', got '{on_failure_val}'")
                timeout_val = h.get("timeout", 120)
                if not isinstance(timeout_val, int) or timeout_val <= 0:
                    raise ValueError(
                        f"hooks.{phase}: timeout must be a positive "
                        f"integer, got {timeout_val}")
                entries.append(HookEntry(
                    command=h.get("command", ""),
                    timeout=timeout_val,
                    description=h.get("description", ""),
                    on_failure=on_failure_val,
                    condition=h.get("condition", ""),
                ))
            setattr(config.hooks, phase, entries)

        if config.execution.mode == "batch":
            per_case = []
            if config.hooks.before_each:
                per_case.append("before_each")
            if config.hooks.after_each:
                per_case.append("after_each")
            if config.hooks.before_step:
                per_case.append("before_step")
            if config.hooks.after_step:
                per_case.append("after_step")
            if per_case:
                import warnings
                warnings.warn(
                    f"hooks.{', '.join(per_case)} ignored in batch mode "
                    f"(per-case hooks only run in case/prompt mode)",
                    stacklevel=2,
                )

        resolved_skill = config.resolve_skill()
        if resolved_skill:
            try:
                _validate_path_segment(resolved_skill, f"skill name in {path}")
            except ValueError as e:
                raise ValueError(str(e)) from e

        runners = [config.runner]
        runners.extend(
            step.runner for step in config.execution.steps if step.runner)
        for runner in runners:
            # Neither codex nor cursor supports tool interception; reject at load
            # (same altitude as codex) rather than only at runner construction.
            if runner.type in ("codex", "cursor") and config.inputs.tools:
                raise ValueError(
                    f"runner.type '{runner.type}' does not support "
                    "inputs.tools interception; use claude-code or remove "
                    "the tool interceptors")
            # codex additionally cannot enforce repository answer-key
            # protections in workspace_mode: repo (cursor can).
            if runner.type == "codex" and runner.workspace_mode == "repo":
                raise ValueError(
                    f"runner.type '{runner.type}' does not support "
                    "workspace_mode: repo because repository answer-key "
                    "protections cannot be enforced")

        # Fail fast on a judge model that cannot be routed to a provider backend.
        # Only statically-known models are checked (`models.judge` and per-judge
        # `model:`); a model supplied solely via EVAL_JUDGE_MODEL is validated
        # when the judge is built. Local import avoids an import cycle
        # (prompt_backends -> agent_eval.agent -> config).
        from agent_eval.prompt_backends import resolve_judge_backend
        # Agent judges route their model through the runner (any provider prefix
        # is stripped to the bare id), not an SDK, so an explicit non-SDK provider
        # is valid for them — validate only judges that resolve to an SDK backend.
        # models.judge is checked only when a non-agent judge could consume it.
        non_agent_judges = [jc for jc in config.judges if not jc.agent]
        judge_models = []
        if config.models.judge and non_agent_judges:
            judge_models.append(("models.judge", config.models.judge))
        for jc in non_agent_judges:
            if jc.model:
                judge_models.append((f"judge '{jc.name}' model", jc.model))
        for label, model in judge_models:
            try:
                resolve_judge_backend(model)
            except ValueError as e:
                raise ValueError(f"{label}: {e}") from e

        # OpenRouter judges (spec 014): `provider_options` is validated by the
        # provider kind of the judge's statically-known model, and an `agent:`
        # judge cannot use an `openrouter:/` model — it runs through the
        # runner, which has no OpenRouter transport yet.
        from agent_eval.prompt_backends import split_model_uri
        for jc in config.judges:
            static_model = jc.model or config.models.judge or ""
            provider, _bare = split_model_uri(static_model)
            if jc.agent and provider == "openrouter":
                raise ValueError(
                    f"Judge '{jc.name}': agent judges run through the runner, "
                    f"which cannot serve '{static_model}' yet (spec 014 "
                    "rollout); use a Claude model or 'runner:/<model>'")
            jc.provider_options = validate_judge_provider_options(
                jc.provider_options, static_model, jc.name)

        # Agent roles under an OpenRouter plan (spec 014 Config validation).
        validate_openrouter_roles(config)

        return config

    @property
    def project_root(self) -> Path:
        """Project root directory (always CWD, not the eval.yaml location)."""
        return Path.cwd()


def _is_valid_eval_name(name: object) -> bool:
    """Check that an eval name is a valid single path segment."""
    if not isinstance(name, str) or not name:
        return False
    if "/" in name or "\\" in name or name in (".", "..") or "\x00" in name:
        return False
    return all(ord(c) >= 32 for c in name)


def discover_configs(project_root: Path,
                     include_profiles: bool = False) -> list[DiscoveryResult]:
    """Scan the project for eval.yaml files across all supported layouts.

    Scan order: eval/*/eval.yaml (nested), eval/*.yaml (flat), profiles
    (eval-profiles/*.yaml, eval/profiles/*.yaml), root eval.yaml.
    Files that fail YAML parsing are skipped.

    A file with a top-level ``extends:`` is a *profile* layered over a base
    config, not a standalone eval: it is skipped by default (it must not
    register as an eval named after its stem) and returned with
    ``profile_of=<root config>`` when ``include_profiles`` is set. Its
    ``eval_name`` is the merged config's, i.e. the base's.

    Eval names use backward-compatible fallback chain:
    1. skill field (preserves existing skill-based evals)
    2. name field (explicit naming, sanitized)
    3. directory/filename (path-based derivation)

    Eval names with path separators or control characters are rejected.
    """
    results: list[DiscoveryResult] = []
    seen: set[Path] = set()
    seen_names: dict[str, Path] = {}

    def _try_add(yaml_path: Path, is_root: bool) -> None:
        resolved = yaml_path.resolve()
        if resolved in seen:
            return
        try:
            head = _read_config_mapping(resolved)
        except Exception as exc:
            print(f"Warning: skipping {yaml_path}: {exc}", file=sys.stderr)
            return
        profile_of = None
        if "extends" in head:
            if not include_profiles:
                return
            try:
                raw, chain = load_raw(resolved)
            except Exception as exc:
                print(f"Warning: skipping profile {yaml_path}: {exc}", file=sys.stderr)
                return
            profile_of = Path(chain[0])
        else:
            raw = head

        # Derive eval_name using fallback chain (same as EvalConfig.eval_name())
        eval_name = None

        # Priority 1: skill field (execution.skill canonical, top-level fallback)
        skill_ref = (raw.get("execution") or {}).get("skill") or raw.get("skill")
        if skill_ref:
            eval_name = skill_ref

        # Priority 2: name field (explicit identifier, sanitized)
        if not eval_name and raw.get("name"):
            sanitized = raw["name"].lower().replace(" ", "-")
            sanitized = "".join(c for c in sanitized if c.isalnum() or c in "._-")
            if sanitized and _is_valid_eval_name(sanitized):
                eval_name = sanitized

        # Priority 3: derive from path
        if not eval_name:
            if is_root:
                eval_name = "eval"
            elif yaml_path.name == "eval.yaml":
                # Nested: eval/api-docs/eval.yaml → "api-docs"
                eval_name = yaml_path.parent.name
            else:
                # Flat: eval/user-guides.yaml → "user-guides"
                eval_name = yaml_path.stem

        if not _is_valid_eval_name(eval_name):
            print(f"Warning: skipping {yaml_path}: invalid eval name {eval_name!r}",
                  file=sys.stderr)
            return
        # A profile shares its base's eval name by design — not a duplicate.
        if profile_of is None:
            if eval_name in seen_names:
                print(f"Warning: duplicate eval name {eval_name!r} in "
                      f"{yaml_path} (already seen in {seen_names[eval_name]})",
                      file=sys.stderr)
            seen_names[eval_name] = resolved
        seen.add(resolved)
        results.append(DiscoveryResult(
            path=resolved,
            eval_name=eval_name,
            is_root=is_root,
            profile_of=profile_of,
        ))

    eval_dir = project_root / "eval"
    if eval_dir.is_dir():
        for subdir in sorted(eval_dir.iterdir()):
            if subdir.is_dir() and not subdir.name.startswith("."):
                candidate = subdir / "eval.yaml"
                if candidate.is_file():
                    _try_add(candidate, is_root=False)
        # pathlib's glob matches dotfiles (unlike the glob module), so hidden
        # working files — .entity-map.yaml, editor droppings — would surface
        # as eval configs, and one extra "config" turns auto-selection into a
        # which-config prompt on every run.
        for candidate in sorted(eval_dir.glob("*.yaml")):
            if (candidate.is_file() and candidate.name != "eval.yaml"
                    and not candidate.name.startswith(".")):
                _try_add(candidate, is_root=False)

    # Profiles (`extends:` overlays) live next to the configs they layer over.
    for profiles_dir in (project_root / "eval-profiles", eval_dir / "profiles"):
        if profiles_dir.is_dir():
            for candidate in sorted(profiles_dir.glob("*.yaml")):
                if candidate.is_file() and not candidate.name.startswith("."):
                    _try_add(candidate, is_root=False)

    root_config = project_root / "eval.yaml"
    if root_config.is_file():
        _try_add(root_config, is_root=True)

    return sorted(results, key=lambda r: r.path)


def analysis_cache_path(config_path) -> Path:
    """Return the eval-analyze cache (``eval.md``) path for a config.

    The cache lives next to the config, named by swapping the config's
    extension to ``.md``: ``eval.yaml`` -> ``eval.md`` (root and nested
    ``eval/<name>/eval.yaml`` layouts, unchanged), and flat
    ``eval/<name>.yaml`` -> ``eval/<name>.md``. Deriving the name from the
    config filename keeps flat-layout configs from silently colliding on a
    single ``eval/eval.md``: flat configs are uniquely named (and warned on
    name collision in ``discover_configs``), so their analysis caches must be
    uniquely named too.
    """
    return Path(config_path).with_suffix(".md")


def infer_layout(configs: list[DiscoveryResult]) -> str:
    """Infer the project's eval layout from discovery results.

    Returns one of: "nested", "flat", "root", "mixed", "none".
    """
    if not configs:
        return "none"

    has_nested = False
    has_flat = False
    has_root = False

    for c in configs:
        if c.is_root:
            has_root = True
        elif c.path.name == "eval.yaml":
            has_nested = True
        else:
            has_flat = True

    patterns = sum([has_nested, has_flat, has_root])
    if patterns > 1:
        return "mixed"
    if has_nested:
        return "nested"
    if has_flat:
        return "flat"
    return "root"


# --- `python3 -m agent_eval.config --print <path>` ----------------------------

def _scalar_yaml(value) -> str:
    text = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True,
                          width=10 ** 6).strip()
    # A bare scalar document ends with the `...` end marker; drop it.
    if text.endswith("\n..."):
        text = text[:-4].rstrip()
    return text


def _item_sources(item, key, per_file, path):
    """Display names of the chain files whose list at ``path`` carries ``item``
    (by merge key when the list merges by key, else by equality)."""
    names = []
    for display, raw in per_file:
        node = raw
        for part in path:
            node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, list):
            continue
        if key and any(isinstance(i, dict) and i.get(key) == item.get(key) for i in node):
            names.append(display)
        elif not key and item in node:
            names.append(display)
    return names


def _list_replaced_by(per_file, path):
    for display, raw in per_file:
        node = raw
        for part in path:
            node = node.get(part) if isinstance(node, dict) else None
        if isinstance(node, _ReplaceList):
            return display
    return None


def _emit_merged(node, per_file, path=(), indent=0, out=None):
    out = [] if out is None else out
    pad = " " * indent
    for k, v in node.items():
        key_text = _scalar_yaml(k)
        if isinstance(v, dict) and v:
            out.append(f"{pad}{key_text}:")
            _emit_merged(v, per_file, (*path, k), indent + 2, out)
        elif isinstance(v, list) and v:
            replaced = _list_replaced_by(per_file, (*path, k))
            note = f"  # !replace from: {replaced}" if replaced else ""
            out.append(f"{pad}{key_text}:{note}")
            merge_key = _list_merge_key(v)
            for item in v:
                sources = _item_sources(item, merge_key, per_file, (*path, k))
                comment = f"  # from: {', '.join(sources)}" if sources else ""
                if isinstance(item, dict):
                    out.append(f"{pad}-{comment or ''}".rstrip() if not comment
                               else f"{pad}- #{comment[3:]}")
                    dumped = yaml.safe_dump(item, default_flow_style=False,
                                            allow_unicode=True, sort_keys=False,
                                            width=10 ** 6).rstrip("\n")
                    out.extend(f"{pad}  {line}" for line in dumped.splitlines())
                else:
                    out.append(f"{pad}- {_scalar_yaml(item)}{comment}")
        else:
            dumped = yaml.safe_dump({k: v}, default_flow_style=False,
                                    allow_unicode=True, sort_keys=False,
                                    width=10 ** 6).rstrip("\n")
            out.extend(f"{pad}{line}" for line in dumped.splitlines())
    return out


def dump_with_provenance(path) -> str:
    """Merged YAML of ``path`` with a `# from:` comment per list item naming
    the chain file(s) that contributed it (and `# !replace from:` on a list
    an overlay replaced). Scalars are not annotated: the last file wins."""
    merged, chain = load_raw(path)
    per_file = []
    for file in chain:
        raw = _read_config_mapping(Path(file))
        raw.pop("extends", None)
        per_file.append((project_relative(file), raw))
    header = ["# merged config", "# chain (root first): "
              + " <- ".join(display for display, _ in per_file)]
    return "\n".join([*header, *_emit_merged(merged, per_file)]) + "\n"


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python3 -m agent_eval.config",
        description="Inspect an eval config as the harness loads it.")
    parser.add_argument("--print", dest="print_path", metavar="PATH",
                        help="dump the merged config (extends: resolved) with "
                             "per-list provenance comments")
    args = parser.parse_args(argv)
    if not args.print_path:
        parser.print_help()
        return 2
    try:
        sys.stdout.write(dump_with_provenance(args.print_path))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
