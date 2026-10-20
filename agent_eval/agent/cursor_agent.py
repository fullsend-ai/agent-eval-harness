"""Cursor Agent CLI runner.

Cursor is one of the CLI backends behind the common :class:`EvalRunner`
interface.  The runner deliberately owns only the translation that is
specific to Cursor:

* Cursor's executable and command-line syntax;
* embedding a ``SKILL.md`` because Cursor does not consume Claude-style slash
  commands from the harness; and
* translating the common effort and permission settings to Cursor's controls.

Process execution, environment handling, plugin staging, timeouts, and result
collection follow the same small contract as the Claude Code and Codex
runners.  Cursor-only connection, trust, MCP, and filesystem-hardening knobs
are not accepted here; use the common runner fields instead.
"""

import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agent_eval.config import resolve_plugin_dir, resolve_plugin_skill_roots

from .base import EvalRunner, RunResult, resolve_env_value
from .claude_code import stage_plugin_dir


@dataclass
class CursorStreamSummary:
    """Metrics parsed from Cursor's stream-json result."""

    resolved_model: Optional[str] = None
    models_used: Optional[list[str]] = None
    result_obj: Optional[dict] = None
    num_turns: Optional[int] = None
    per_model_turns: Optional[dict] = None
    token_usage: Optional[dict] = None
    per_model_usage: Optional[dict] = None
    cost_usd: Optional[float] = None


@dataclass
class _PermissionSnapshot:
    """The small amount of state needed to restore ``.cursor/cli.json``."""

    path: Path
    original: Optional[bytes]
    original_mode: Optional[int]
    created_dir: bool
    parent_real: Path
    workspace_root: Path


_SAFE_ENV_KEYS = {
    "PATH", "HOME", "USER", "SHELL", "LANG", "LC_ALL", "TERM",
    "CURSOR_API_KEY", "CURSOR_API_ENDPOINT", "CURSOR_AGENT_BIN",
    "MLFLOW_TRACKING_URI", "MLFLOW_EXPERIMENT_NAME", "AGENT_EVAL_RUNS_DIR",
}
_CURSOR_PERMISSION_RE = re.compile(
    r"^\s*(Read|Grep|Glob|Edit|Write|Bash|Shell|WebFetch|WebSearch|Mcp)\((.*)\)\s*$"
)
_CURSOR_MCP_RE = re.compile(
    r"^mcp__(\*|[A-Za-z0-9._-]+)__(\*|[A-Za-z0-9._-]+)$"
)
_CURSOR_NATIVE_MCP_RE = re.compile(
    r"^Mcp\((?:\*|[A-Za-z0-9._-]+):(?:\*|[A-Za-z0-9._-]+)\)$"
)
# Cursor exposes some parameterized models as legacy IDs such as
# ``gpt-5.4-medium``.  Adding ``[effort=medium]`` to one of those IDs creates
# a different, unsupported model selection.
_CURSOR_EFFORT_VARIANT_RE = re.compile(
    r"-(?:minimal|low|medium|high|xhigh|max)(?:-fast)?(?:$|\[)",
    re.IGNORECASE,
)
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Cursor requires ``permissions.allow`` whenever a project-level CLI config
# contains a permissions object.  This is the closest representation of the
# harness's default "allow everything unless denied" semantics.  Keep this
# list in sync with the Cursor permission categories we translate below.
_CURSOR_ALL_PERMISSION_PATTERNS = [
    "Read(**)",
    "Write(**)",
    "Shell(**)",
    "WebFetch(*)",
    "Mcp(*:*)",
]


def _discover_cursor_agent(configured: Optional[str] = None) -> str:
    """Resolve the Cursor executable or raise a useful configuration error."""
    candidates = [configured, os.environ.get("CURSOR_AGENT_BIN")]
    path_candidate = shutil.which("cursor-agent")
    if path_candidate:
        candidates.append(path_candidate)
    for candidate in candidates:
        if not candidate:
            continue
        candidate = Path(candidate).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    raise FileNotFoundError(
        "cursor-agent binary not found. Set runner.settings.binary, "
        "CURSOR_AGENT_BIN, or install Cursor Agent."
    )


def _cursor_usage_dict(usage: dict) -> dict:
    """Normalize Cursor token field names to the harness schema."""
    def token_count(name: str) -> int | float:
        value = usage.get(name, 0)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    return {
        "input": token_count("inputTokens"),
        "output": token_count("outputTokens"),
        "cache_read": token_count("cacheReadTokens"),
        "cache_create": token_count("cacheWriteTokens"),
    }


def _parse_cursor_stream(stdout_text: str) -> CursorStreamSummary:
    """Extract usage, turns, model names, and the terminal result from JSONL."""
    resolved_model = None
    models_seen: set[str] = set()
    result_obj = None
    assistant_seen = False
    assistant_models: set[str] = set()
    token_usage = None
    per_model_usage = None
    cost_usd = None

    for line in stdout_text.splitlines():
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue

        event_type = obj.get("type")
        if event_type == "system" and obj.get("subtype") == "init":
            model = obj.get("model")
            if isinstance(model, str) and model:
                resolved_model = model
                models_seen.add(model)
        elif event_type == "assistant":
            message = obj.get("message", {})
            if not isinstance(message, dict):
                continue
            assistant_seen = True
            model = message.get("model") or resolved_model
            if isinstance(model, str) and model:
                models_seen.add(model)
                assistant_models.add(model)
        elif event_type == "result":
            result_obj = obj
            # Cursor's result event carries no cost field. A numeric
            # total_cost_usd is the signal for a *Claude*-shaped result (see
            # events._looks_like_cursor_object), so it is not read as a Cursor
            # cost here — doing so contradicted the stream detector.
            usage = obj.get("usage")
            if isinstance(usage, dict):
                token_usage = _cursor_usage_dict(usage)

    if result_obj is not None or assistant_seen:
        # Key per-model usage and turns by the SAME model identity, so one run's
        # tokens are not split from its turn count during aggregation. Prefer the
        # assistant-message model (what per_model_turns uses); the init event's
        # model string can differ in case/spacing (e.g. "GPT-5.4 Medium" vs
        # "gpt-5.4-medium"). Fall back to the init model, then a constant.
        primary_model = (next(iter(sorted(assistant_models)), None)
                         or resolved_model or "cursor-agent")
        per_model_turns = {
            model: 1 for model in (assistant_models or {primary_model})
        }
        if token_usage is not None:
            per_model_usage = {
                primary_model: {**token_usage, "cost_usd": cost_usd},
            }
    else:
        per_model_turns = None

    return CursorStreamSummary(
        resolved_model=resolved_model,
        models_used=sorted(models_seen) if models_seen else None,
        result_obj=result_obj,
        num_turns=1 if result_obj is not None or assistant_seen else None,
        per_model_turns=per_model_turns,
        token_usage=token_usage,
        per_model_usage=per_model_usage,
        cost_usd=cost_usd,
    )


def _cursor_path_pattern(path: str) -> str:
    """Convert a harness directory path to Cursor's recursive path syntax."""
    return f"{path.rstrip('/')}" + "/**" if path.endswith("/") else path


def _cursor_mcp_pattern(rule: str) -> Optional[str]:
    """Translate ``mcp__server__tool`` to Cursor's MCP spelling."""
    value = rule.strip()
    if value in {"mcp__*", "mcp__*__*"}:
        return "Mcp(*:*)"
    match = _CURSOR_MCP_RE.fullmatch(value)
    if not match:
        return None
    return f"Mcp({match.group(1)}:{match.group(2)})"


def _cursor_shell_pattern(argument: str) -> str:
    """Keep the first shell word and its configured argument restriction."""
    if not argument.strip() or argument.strip() in {"*", "**"}:
        return "**"
    tokens = shlex.split(argument)
    if not tokens:
        return "**"
    command = tokens[0]
    if len(tokens) > 1:
        command += ":" + " ".join(tokens[1:])
    return command


def _cursor_permission_patterns(
    rules, *, deny: bool = False, strict: bool = False,
) -> list[str]:
    """Translate common harness permission rules to Cursor patterns.

    Cursor has no exact equivalent for every Claude tool.  ``Grep`` maps to
    ``Read`` and ``Edit`` maps to ``Write``.  ``Glob`` deliberately does not
    grant ``Read``: Cursor has no separate Glob permission, and treating it as
    Read would overgrant file contents.  In strict mode, rules that cannot be
    represented safely raise instead of silently weakening an allowlist.
    """
    patterns: list[str] = []
    seen: set[str] = set()

    def add(pattern: str) -> None:
        if pattern and pattern not in seen:
            seen.add(pattern)
            patterns.append(pattern)

    def unsupported(message: str, *, enforce: bool = True) -> None:
        if strict and enforce:
            raise ValueError(message)
        warnings.warn(
            f"{message}; ignoring it",
            RuntimeWarning,
            stacklevel=3,
        )

    def add_tool(tool: str, argument: str = "") -> None:
        if tool in {"Read", "Grep"}:
            add(f"Read({_cursor_path_pattern(argument or '**')})")
        elif tool == "Glob":
            # Cursor does not expose Glob as a permission type.  It can still
            # list names, but it must never be translated into content read.
            # For a deny, Read is the conservative (more restrictive)
            # approximation; an allow is intentionally a no-op.
            if deny:
                add(f"Read({_cursor_path_pattern(argument or '**')})")
        elif tool in {"Edit", "Write", "MultiEdit", "NotebookEdit"}:
            # Cursor has one `Write` permission for every file mutation, so
            # the Claude-only editor spellings map onto it like `Edit` does.
            # Safe because the harness lifts a default deny for the whole
            # write capability at once (score.py::_TOOL_CAPABILITY_GROUPS):
            # these names never reach a deny list while `Write` is allowed,
            # so this cannot turn into a `Write(**)` deny that beats the
            # allow and takes the judge's verdict file away.
            add(f"Write({_cursor_path_pattern(argument or '**')})")
        elif tool in {"Bash", "Shell"}:
            add(f"Shell({_cursor_shell_pattern(argument)})")
        elif tool in {"WebFetch", "WebSearch"}:
            # Cursor uses one domain allowlist for both web operations.
            add(f"WebFetch({argument or '*'})")
        elif tool == "Mcp":
            if not argument:
                add("Mcp(*:*)")
            elif _CURSOR_NATIVE_MCP_RE.fullmatch(f"Mcp({argument})"):
                add(f"Mcp({argument})")
            else:
                unsupported(
                    f"Cursor cannot translate MCP permission rule 'Mcp({argument})'"
                )
        elif tool in {"Agent", "Task"}:
            # Cursor has no sub-agent permission.  Denying one is a no-op
            # (nothing to confine), so stay quiet; allowing one cannot be
            # represented, so say so.
            if not deny:
                unsupported(f"Cursor has no permission mapping for {tool!r}")
        else:
            unsupported(f"Cursor has no permission mapping for {tool!r}")

    path_denies = {
        "Bash": "Shell(**)",
        "Shell": "Shell(**)",
        "WebFetch": "WebFetch(*)",
        "WebSearch": "WebFetch(*)",
        "Mcp": "Mcp(*:*)",
    }
    for rule in rules or []:
        if isinstance(rule, dict):
            path = rule.get("path")
            tools = rule.get("tools", [])
            if not isinstance(path, str) or not path:
                unsupported(
                    f"Cursor cannot translate malformed permission rule {rule!r}",
                    enforce=False,
                )
                continue
            if not isinstance(tools, (list, tuple, set)):
                unsupported(
                    f"Cursor cannot translate malformed permission rule {rule!r}",
                    enforce=False,
                )
                continue
            for tool in tools:
                if isinstance(tool, str) and tool in {
                    "Read", "Grep", "Glob", "Edit", "Write",
                    "MultiEdit", "NotebookEdit",
                }:
                    add_tool(tool, path)
                elif isinstance(tool, str) and tool in path_denies:
                    if deny:
                        add(path_denies[tool])
                    else:
                        unsupported(
                            f"Cursor cannot path-scope {tool}; ignoring rule {rule!r}",
                            enforce=False,
                        )
                elif isinstance(tool, str):
                    add_tool(tool)
            continue

        if not isinstance(rule, str):
            unsupported(f"Cursor cannot translate permission rule {rule!r}")
            continue

        mcp = _cursor_mcp_pattern(rule)
        if mcp:
            add(mcp)
            continue
        if _CURSOR_NATIVE_MCP_RE.fullmatch(rule.strip()):
            add(rule.strip())
            continue

        match = _CURSOR_PERMISSION_RE.fullmatch(rule)
        if match:
            add_tool(match.group(1), match.group(2))
            continue

        if rule.strip() in {
            "Read", "Grep", "Glob", "Edit", "Write", "Bash", "Shell",
            "WebFetch", "WebSearch", "Mcp", "Agent", "Task",
            "MultiEdit", "NotebookEdit",
        }:
            add_tool(rule.strip())
            continue

        unsupported(f"Cursor has no permission mapping for {rule.strip()!r}")

    return patterns


def _cursor_allowed_capabilities(patterns) -> set[str]:
    """Return capabilities represented by emitted Cursor allow patterns.

    Path-scoped rules that Cursor cannot represent must not count as allowed
    capabilities: doing so would suppress the synthesized deny for that tool.
    """
    capabilities: set[str] = set()
    for pattern in patterns:
        match = _CURSOR_PERMISSION_RE.fullmatch(pattern)
        if not match:
            continue
        tool = match.group(1)
        if tool == "Mcp":
            capabilities.add("Mcp")
        elif tool in {"Read", "Grep", "Glob"}:
            capabilities.add("Read")
        elif tool in {"Edit", "Write"}:
            capabilities.add("Write")
        elif tool in {"Bash", "Shell"}:
            capabilities.add("Shell")
        elif tool in {"WebFetch", "WebSearch"}:
            capabilities.add("WebFetch")
    return capabilities


def _validated_permission_parent(snapshot: _PermissionSnapshot) -> Path:
    """Return the config parent only while it remains the original directory.

    Permission setup temporarily writes inside the case workspace.  Checking
    the parent before cleanup prevents a workspace-local path from being
    redirected through a symlink (or a replaced directory) to an arbitrary
    location.
    """
    parent = snapshot.path.parent
    if parent.is_symlink():
        raise OSError(f"Cursor permission parent is a symlink: {parent}")
    parent_real = parent.resolve(strict=True)
    if not parent_real.is_dir():
        raise OSError(f"Cursor permission parent is not a directory: {parent}")
    if snapshot.parent_real is not None and parent_real != snapshot.parent_real:
        raise OSError(f"Cursor permission parent changed: {parent}")
    if (snapshot.workspace_root is not None
            and not parent_real.is_relative_to(snapshot.workspace_root)):
        raise OSError(f"Cursor permission parent escapes workspace: {parent}")
    return parent


def _atomic_write_permission_file(
    path: Path, data: bytes, mode: Optional[int] = None,
) -> None:
    """Replace a Cursor permission file without following the destination.

    The temporary file is created in the already-validated parent and
    ``os.replace`` replaces the directory entry itself.  Thus an agent that
    swaps ``cli.json`` for a symlink or hardlink cannot redirect the restore
    write into another file.
    """
    parent = path.parent
    if parent.is_symlink():
        raise OSError(f"Cursor permission parent is a symlink: {parent}")
    temp_fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(parent))
    temp_path = Path(temp_name)
    fd = temp_fd
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


class CursorAgentRunner(EvalRunner):
    """Run a skill or prompt with the local Cursor Agent CLI."""

    _VALID_PERMISSION_MODES = {
        "default", "acceptEdits", "plan", "auto", "dontAsk",
        "bypassPermissions",
    }

    @classmethod
    def from_config(cls, config, *, log_prefix=None, **overrides):
        if getattr(getattr(config, "inputs", None), "tools", None):
            raise ValueError(
                "Cursor runner does not support inputs.tools interception; "
                "use claude-code or remove the tool interceptors")

        settings = dict(config.runner.settings or {})
        unsupported = sorted(key for key in settings if key != "binary")
        if unsupported:
            warnings.warn(
                "Cursor runner ignores unsupported runner.settings keys: "
                + ", ".join(unsupported),
                RuntimeWarning,
                stacklevel=2,
            )

        plugin_dirs = [
            str(resolve_plugin_dir(config, configured))
            for configured in config.runner.plugin_dirs
        ]
        return cls(
            binary=settings.get("binary"),
            plugin_dirs=plugin_dirs,
            workspace_mode=config.runner.workspace_mode,
            env={**config.execution.env, **config.runner.env},
            system_prompt=config.runner.system_prompt,
            effort=overrides.get("effort", config.runner.effort),
            permission_mode=overrides.get(
                "permission_mode", config.runner.permission_mode),
            permissions=overrides.get("permissions", config.permissions),
            log_prefix=log_prefix,
        )

    def __init__(
        self,
        binary: Optional[str] = None,
        plugin_dirs: Optional[list] = None,
        workspace_mode: Optional[str] = None,
        env: Optional[dict] = None,
        system_prompt: Optional[str] = None,
        effort: Optional[str] = None,
        permission_mode: Optional[str] = None,
        permissions: Optional[dict] = None,
        log_prefix: Optional[str] = None,
    ):
        self._binary = _discover_cursor_agent(binary)
        self._plugin_dirs = list(plugin_dirs or [])
        self._workspace_mode = workspace_mode
        self._env = dict(env or {})
        self._system_prompt = system_prompt

        if effort is not None and (not isinstance(effort, str) or not effort.strip()):
            raise ValueError("Cursor effort must be a non-empty string")
        self._effort = effort

        if permission_mode is not None and permission_mode not in self._VALID_PERMISSION_MODES:
            raise ValueError(
                f"Invalid permission_mode '{permission_mode}'. "
                f"Must be one of: {sorted(self._VALID_PERMISSION_MODES)}")
        self._permission_mode = permission_mode
        if permission_mode in {"default", "acceptEdits", "auto", "dontAsk"}:
            warnings.warn(
                f"Cursor has no exact permission_mode={permission_mode!r} "
                "equivalent; using Cursor's default approval behavior.",
                RuntimeWarning,
                stacklevel=2,
            )

        self._permissions = dict(permissions or {})
        for name in ("allow", "deny"):
            value = self._permissions.get(name)
            if value is not None and not isinstance(value, list):
                raise ValueError(f"Cursor permissions.{name} must be a list")

    @property
    def name(self) -> str:
        return "cursor"

    @property
    def version(self) -> str:
        """Return ``cursor-agent --version`` output, or empty on failure."""
        try:
            result = subprocess.run(
                [self._binary, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                env=self._build_env(),
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except Exception:
            return ""

    def execute(
        self,
        target: Optional[str],
        args: str,
        workspace: Path,
        model: str,
        settings_path: Optional[Path] = None,
        system_prompt: Optional[str] = None,
        max_budget_usd: float = 5.0,
        timeout_s: int = 600,
        extra_env: Optional[dict] = None,
    ) -> RunResult:
        del settings_path  # Cursor has no settings-file flag in headless mode.
        workspace = Path(workspace).resolve()
        if max_budget_usd not in (None, 5.0):
            warnings.warn(
                f"Cursor Agent does not enforce max_budget_usd={max_budget_usd}; "
                "external provider limits are the available cap.",
                RuntimeWarning,
                stacklevel=2,
            )

        start = time.monotonic()
        process = None
        permission_snapshot = None
        try:
            staged_plugin_dirs = self._staged_plugin_dirs(workspace)
            prompt = self._build_prompt(
                target, args, system_prompt, workspace, staged_plugin_dirs)
            command = self._build_command(
                workspace, model, staged_plugin_dirs=staged_plugin_dirs)
            permission_snapshot = self._prepare_permissions(workspace)

            popen_kwargs = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "cwd": str(workspace),
                "text": True,
                "env": self._build_env(extra_env),
            }
            if os.name != "nt":
                popen_kwargs["start_new_session"] = True
            process = subprocess.Popen(command, **popen_kwargs)
            try:
                stdout, stderr = process.communicate(
                    input=prompt, timeout=timeout_s)
            except subprocess.TimeoutExpired as exc:
                partial_stdout = _as_text(exc.output)
                partial_stderr = _as_text(exc.stderr)
                self._terminate_process(process)
                try:
                    final_stdout, final_stderr = process.communicate(timeout=5)
                    stdout = final_stdout or partial_stdout
                    stderr = final_stderr or partial_stderr
                except subprocess.TimeoutExpired:
                    stdout, stderr = partial_stdout, partial_stderr
                summary = _parse_cursor_stream(stdout or "")
                return self._make_result(
                    -1,
                    stdout or "",
                    self._timeout_stderr(stderr or "", timeout_s),
                    time.monotonic() - start,
                    summary,
                )
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return RunResult(
                exit_code=-1,
                stdout="",
                stderr=str(exc),
                duration_s=time.monotonic() - start,
            )
        except BaseException:
            if process is not None:
                self._terminate_process(process)
            raise
        finally:
            self._restore_permissions(permission_snapshot)

        summary = _parse_cursor_stream(stdout or "")
        exit_code = process.returncode
        result_obj = summary.result_obj or {}
        if exit_code == 0 and result_obj.get("is_error"):
            exit_code = 1
        return self._make_result(
            exit_code,
            stdout or "",
            stderr or "",
            time.monotonic() - start,
            summary,
        )

    def _build_env(self, extra_env: Optional[dict] = None) -> dict:
        """Build the same allowlisted baseline used by the CLI runners."""
        env = {key: value for key, value in os.environ.items()
               if key in _SAFE_ENV_KEYS}

        def apply(values) -> None:
            for key, value in (values or {}).items():
                if not isinstance(key, str) or not _ENV_NAME_RE.fullmatch(key):
                    continue
                if value is None:
                    continue
                resolved = resolve_env_value(value)
                if resolved is not None:
                    env[key] = resolved

        apply(self._env)
        # Hook-produced variables are intentionally additive, as with Claude
        # Code.  The CLI receives an argv list, so these values are not shell
        # source text.
        apply(extra_env)
        return env

    def _build_command(
        self,
        workspace: Path,
        model: str,
        staged_plugin_dirs: Optional[list[str]] = None,
    ) -> list[str]:
        """Build only the Cursor options corresponding to common runner fields."""
        command = [
            self._binary,
            "--print",
            "--output-format", "stream-json",
            "--workspace", str(workspace),
        ]
        if model:
            command.extend(["--model", _model_with_effort(model, self._effort)])
        if self._permission_mode == "plan":
            command.extend(["--mode", "plan"])
        else:
            # Cursor's print mode requires --force for headless write-capable
            # runs.  Permission deny rules still take precedence over allows.
            command.append("--force")
        for plugin_dir in staged_plugin_dirs or []:
            command.extend(["--plugin-dir", str(plugin_dir)])
        return command

    def _build_prompt(
        self,
        target: Optional[str],
        args: str,
        system_prompt: Optional[str],
        workspace: Optional[Path] = None,
        staged_plugin_dirs: Optional[list[str]] = None,
    ) -> str:
        """Build a direct prompt, embedding the requested skill instructions."""
        prompt = f"Use the {target} skill" if target else (args or "")
        if target:
            skill_text = self._find_skill_text(
                target, workspace, staged_plugin_dirs)
            if not skill_text:
                raise FileNotFoundError(
                    f"Cursor skill instructions not found for target {target!r}")
            prompt += (
                " and follow these instructions:\n\n"
                "--- SKILL.md ---\n"
                f"{skill_text}\n"
                "--- END SKILL.md ---"
            )
            if args:
                prompt += f" with arguments: {args}"

        effective_system_prompt = system_prompt or self._system_prompt
        if effective_system_prompt:
            policy = (
                "--- HARNESS RUNTIME INSTRUCTIONS ---\n"
                f"{effective_system_prompt.rstrip()}\n"
                "--- END HARNESS RUNTIME INSTRUCTIONS ---"
            )
            prompt = f"{policy}\n\n{prompt}" if prompt else policy

        return prompt

    @staticmethod
    def _skill_references(skill_reference: str) -> list[tuple[str, Optional[str]]]:
        """Return local skill name plus optional plugin namespace."""
        if not isinstance(skill_reference, str):
            return []
        namespace, separator, local_name = skill_reference.partition(":")
        if separator:
            if (not namespace or not local_name or ":" in local_name
                    or Path(namespace).name != namespace
                    or Path(local_name).name != local_name
                    or local_name in {".", ".."}):
                return []
            return [(local_name, namespace)]

        if Path(skill_reference).name != skill_reference:
            return []
        return [(skill_reference, None)]

    @staticmethod
    def _plugin_namespaces(plugin: Path) -> set[str]:
        namespaces = {plugin.name}
        manifest = plugin / ".claude-plugin" / "plugin.json"
        try:
            metadata = json.loads(manifest.read_text())
            name = metadata.get("name") if isinstance(metadata, dict) else None
            if isinstance(name, str) and name and Path(name).name == name:
                namespaces.add(name)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            pass
        return namespaces

    @classmethod
    def _find_skill_text(
        cls,
        skill_reference: str,
        workspace: Optional[Path],
        staged_plugin_dirs: Optional[list[str]],
    ) -> str:
        """Find a skill in the workspace or one of the configured plugins."""
        if workspace is None:
            return ""
        references = cls._skill_references(skill_reference)
        if not references:
            return ""
        workspace = Path(workspace).resolve()
        roots: list[tuple[Path, Optional[set[str]]]] = [
            (workspace / "skills", None),
            (workspace / ".agents" / "skills", None),
            (workspace / ".claude" / "skills", None),
        ]
        for configured in staged_plugin_dirs or []:
            plugin = Path(configured).resolve()
            try:
                plugin_roots = resolve_plugin_skill_roots(plugin)
            except (OSError, ValueError):
                # The CLI can still load a plugin containing no discoverable
                # skills; it should not make prompt-mode setup fail here.
                plugin_roots = []
            namespaces = cls._plugin_namespaces(plugin)
            roots.extend((root, namespaces) for root in plugin_roots)

        for skill_name, requested_namespace in references:
            for root, namespaces in roots:
                if requested_namespace is not None:
                    if namespaces is None or requested_namespace not in namespaces:
                        continue
                skill_path = root / skill_name / "SKILL.md"
                if not skill_path.is_file():
                    continue
                # Resolve and confine to the root before reading: a repo could
                # plant a `skills/<name>/SKILL.md` symlink — or a `..`-laden
                # skill name — pointing at a host file, which `_build_prompt`
                # would then embed in the agent/judge prompt (CWE-59 / CWE-22).
                try:
                    resolved = skill_path.resolve(strict=True)
                except OSError:
                    continue
                root_resolved = root.resolve()
                if resolved != root_resolved and root_resolved not in resolved.parents:
                    continue
                try:
                    return resolved.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
        return ""

    def _staged_plugin_dirs(self, workspace: Path) -> list[str]:
        """Stage external plugins unless the workspace is the real repo."""
        configured = [Path(path).resolve() for path in self._plugin_dirs]
        workspace = Path(workspace).resolve()

        # Repo mode has no isolation boundary. Passing an external plugin path
        # through avoids writing .staged-plugins/ into the user's repository,
        # which would both dirty the repo and be reported as an agent change.
        if self._workspace_mode == "repo":
            for plugin in configured:
                if not plugin.is_dir():
                    raise FileNotFoundError(
                        f"Runner plugin directory not found: {plugin}")
            return [str(plugin) for plugin in configured]

        staged = []
        seen: dict[str, Path] = {}
        for plugin in configured:
            if not plugin.is_dir():
                raise FileNotFoundError(f"Runner plugin directory not found: {plugin}")
            if plugin == workspace or plugin.is_relative_to(workspace):
                staged.append(str(plugin))
            else:
                previous = seen.setdefault(plugin.name, plugin)
                if previous != plugin:
                    raise ValueError(
                        "plugin staging cannot stage two different plugins with "
                        f"the same directory name: {previous} and {plugin}")
                staged.append(str(stage_plugin_dir(plugin, workspace)))
        return staged

    def _prepare_permissions(self, workspace: Path) -> Optional[_PermissionSnapshot]:
        """Apply common allow/deny rules through Cursor's local config file."""
        allow_rules = self._permissions.get("allow")
        deny_rules = self._permissions.get("deny")
        # An explicitly empty allowlist has the common harness meaning
        # "unrestricted".  Do not write an empty Cursor allowlist, which
        # would instead deny every capability after the synthesized denies.
        if not allow_rules and not deny_rules:
            return None

        workspace_root = Path(workspace).resolve()
        if not workspace_root.is_dir():
            raise ValueError(f"Cursor workspace is not a directory: {workspace}")

        cursor_dir = workspace_root / ".cursor"
        if cursor_dir.is_symlink():
            raise ValueError(f"Cursor permission directory must not be a symlink: {cursor_dir}")
        if cursor_dir.exists() and not cursor_dir.is_dir():
            raise ValueError(f"Cursor permission path is not a directory: {cursor_dir}")
        directory_created = not cursor_dir.exists()
        cursor_dir.mkdir(parents=True, exist_ok=True)
        parent_real = cursor_dir.resolve(strict=True)
        if not parent_real.is_relative_to(workspace_root):
            raise ValueError(f"Cursor permission directory escapes workspace: {cursor_dir}")

        config_path = cursor_dir / "cli.json"
        original = None
        original_mode = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(config_path, flags)
            try:
                file_stat = os.fstat(fd)
                if stat.S_ISLNK(file_stat.st_mode):
                    raise ValueError(
                        f"Cursor permission config must not be a symlink: {config_path}")
                if not stat.S_ISREG(file_stat.st_mode):
                    raise ValueError(
                        f"Cursor permission config must be a regular file: {config_path}")
                if file_stat.st_nlink > 1:
                    raise ValueError(
                        f"Cursor permission config must not be hardlinked: {config_path}")
                with os.fdopen(fd, "rb") as handle:
                    fd = None
                    original = handle.read()
                original_mode = stat.S_IMODE(file_stat.st_mode)
            finally:
                if fd is not None:
                    os.close(fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ValueError(
                f"Could not safely inspect Cursor permission config {config_path}: {exc}") from exc

        snapshot = _PermissionSnapshot(
            config_path, original, original_mode, directory_created,
            parent_real, workspace_root,
        )
        try:
            config = json.loads(original) if original else {}
            if not isinstance(config, dict):
                raise ValueError(f"Cursor config must be a JSON object: {config_path}")
            permissions = config.get("permissions") or {}
            if not isinstance(permissions, dict):
                raise ValueError("Cursor config permissions must be a JSON object")

            if allow_rules:
                allow_patterns = _cursor_permission_patterns(allow_rules, strict=True)
                if not allow_patterns:
                    # A non-empty allow list that maps to zero Cursor patterns
                    # (tools Cursor does not model, or only path-scoped rules) is
                    # written as an empty allowlist, which Cursor reads as
                    # deny-everything. That fail-closed behavior is intentional;
                    # warn so it is not a silent lock-out.
                    warnings.warn(
                        f"Cursor permissions.allow {allow_rules} did not map to "
                        "any Cursor permission pattern; the agent will be denied "
                        "every capability.",
                        RuntimeWarning, stacklevel=2)
                permissions["allow"] = allow_patterns
                capabilities = _cursor_allowed_capabilities(allow_patterns)
                missing = {
                    "Read": "Read(**)",
                    "Write": "Write(**)",
                    "Shell": "Shell(**)",
                    "WebFetch": "WebFetch(*)",
                    "Mcp": "Mcp(*:*)",
                }
                deny_rules = list(deny_rules or [])
                for capability, pattern in missing.items():
                    if capability not in capabilities:
                        deny_rules.append(pattern)
            elif deny_rules is not None and "allow" not in permissions:
                # A deny-only eval is intentionally unrestricted apart from
                # its denies.  Cursor's project config schema still requires
                # an allow list, so spell out the broad default rather than
                # emitting an invalid config or an empty (deny-all) list.
                permissions["allow"] = list(_CURSOR_ALL_PERMISSION_PATTERNS)
            if deny_rules is not None:
                existing_deny = permissions.get("deny")
                merged = list(existing_deny) if isinstance(existing_deny, list) else []
                for pattern in _cursor_permission_patterns(deny_rules, deny=True):
                    if pattern not in merged:
                        merged.append(pattern)
                permissions["deny"] = merged
            config["permissions"] = permissions
            _atomic_write_permission_file(
                config_path,
                (json.dumps(config, indent=2) + "\n").encode(),
            )
        except BaseException:
            self._restore_permissions(snapshot)
            raise
        return snapshot

    @staticmethod
    def _restore_permissions(snapshot: Optional[_PermissionSnapshot]) -> None:
        """Restore or remove the temporary Cursor config."""
        if snapshot is None:
            return
        try:
            parent = _validated_permission_parent(snapshot)
            if snapshot.original is None:
                if snapshot.path.exists() or snapshot.path.is_symlink():
                    snapshot.path.unlink()
                if snapshot.created_dir:
                    parent.rmdir()
            else:
                _atomic_write_permission_file(
                    snapshot.path, snapshot.original, snapshot.original_mode)
        except (OSError, RuntimeError) as exc:
            warnings.warn(
                f"Could not restore Cursor permission config {snapshot.path}: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )

    @staticmethod
    def _terminate_process(process) -> None:
        """Terminate the process group, matching the other CLI runners."""
        if process is None:
            return
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (AttributeError, ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except (AttributeError, ProcessLookupError, PermissionError, OSError):
                pass

    @staticmethod
    def _timeout_stderr(stderr: str, timeout_s: int) -> str:
        message = f"Timed out after {timeout_s}s"
        return f"{message}\n{stderr}" if stderr else message

    @staticmethod
    def _make_result(
        exit_code: int,
        stdout: str,
        stderr: str,
        duration_s: float,
        summary: CursorStreamSummary,
    ) -> RunResult:
        return RunResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_s=duration_s,
            token_usage=summary.token_usage,
            cost_usd=summary.cost_usd,
            num_turns=summary.num_turns,
            resolved_model=summary.resolved_model,
            models_used=summary.models_used,
            per_model_usage=summary.per_model_usage,
            per_model_turns=summary.per_model_turns,
            raw_output=summary.result_obj,
        )


def _model_with_effort(model: str, effort: Optional[str]) -> str:
    """Apply Cursor's effort override without re-parameterizing a variant ID."""
    if not effort:
        return model
    if _CURSOR_EFFORT_VARIANT_RE.search(model):
        # IDs such as gpt-5.4-medium are already catalog variants.  Cursor
        # resolves the legacy ID directly; appending another parameter makes
        # it an unknown model string (for example, ...medium[effort=medium]).
        return model
    if model.endswith("]") and "[" in model:
        base, parameters = model[:-1].split("[", 1)
        parts = [part.strip() for part in parameters.split(",") if part.strip()]
        replaced = False
        for index, part in enumerate(parts):
            if part.split("=", 1)[0].strip() == "effort":
                parts[index] = f"effort={effort}"
                replaced = True
        if not replaced:
            parts.append(f"effort={effort}")
        return f"{base}[{','.join(parts)}]"
    return f"{model}[effort={effort}]"


def _as_text(value) -> str:
    """Normalize ``TimeoutExpired`` output in text and bytes subprocess modes."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)
