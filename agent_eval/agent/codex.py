"""OpenAI Codex CLI runner implementation."""

import json
import os
import shutil
import signal
import subprocess
import threading
import time
import warnings
from pathlib import Path
from typing import Optional

from agent_eval.config import resolve_plugin_dir, resolve_plugin_skill_roots

from .base import EvalRunner, RunResult, resolve_env_value

_print_lock = threading.Lock()
CODEX_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh"})


class CodexRunner(EvalRunner):
    """Run a skill or prompt with ``codex exec --json``."""

    # codex-cli 0.147.0 accepts these values.  In particular, ``minimal`` is
    # valid and ``max`` (a Claude Code effort level) is not.
    _VALID_EFFORTS = CODEX_EFFORTS
    _VALID_PERMISSION_MODES = {
        "default", "acceptEdits", "plan", "auto", "dontAsk",
        "bypassPermissions",
    }

    # Environment keys safe to forward to an evaluated Codex process: the OS
    # baseline, OpenAI/Codex provider configuration, and harness telemetry.
    # Cross-provider credentials (Anthropic, GCP) are deliberately not
    # forwarded — the agent subprocess has no use for them, and judges run in
    # the harness process. Evals that need one can opt in via runner.env.
    _SAFE_ENV_KEYS = {
        "PATH", "HOME", "USER", "SHELL", "LANG", "LC_ALL", "TERM",
        "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID",
        "OPENAI_ORGANIZATION", "OPENAI_PROJECT", "OPENAI_MODEL",
        "CODEX_HOME", "CODEX_API_KEY",
        "MLFLOW_TRACKING_URI", "MLFLOW_EXPERIMENT_NAME",
        "AGENT_EVAL_RUNS_DIR",
    }

    @classmethod
    def from_config(cls, config, *, log_prefix=None, **overrides):
        plugin_dirs = [
            str(resolve_plugin_dir(config, configured))
            for configured in config.runner.plugin_dirs
        ]

        # execution.env is runtime env for every substrate; runner.env wins on
        # collisions because it is the more specific Codex configuration.
        env = {**config.execution.env, **config.runner.env}
        return cls(
            env=env,
            log_prefix=log_prefix,
            config_overrides=config.runner.settings,
            plugin_dirs=plugin_dirs,
            system_prompt=config.runner.system_prompt,
            effort=overrides.get("effort", config.runner.effort),
            permissions=overrides.get("permissions", config.permissions),
            permission_mode=overrides.get(
                "permission_mode", config.runner.permission_mode),
        )

    def __init__(
        self,
        env: Optional[dict] = None,
        log_prefix: Optional[str] = None,
        config_overrides: Optional[dict] = None,
        plugin_dirs: Optional[list] = None,
        system_prompt: Optional[str] = None,
        effort: Optional[str] = None,
        permissions: Optional[dict] = None,
        permission_mode: Optional[str] = None,
    ):
        self._env = env or {}
        self._log_prefix = log_prefix
        self._config_overrides = dict(config_overrides or {})
        self._skill_roots: list[Path] = []
        for configured in plugin_dirs or []:
            plugin_dir = Path(configured).expanduser().resolve()
            if not plugin_dir.is_dir():
                raise FileNotFoundError(
                    f"Codex plugin directory not found: {plugin_dir}")
            self._skill_roots.extend(resolve_plugin_skill_roots(plugin_dir))
        self._system_prompt = system_prompt
        self._permissions = permissions or {}
        if permission_mode is not None and permission_mode not in self._VALID_PERMISSION_MODES:
            raise ValueError(
                f"Invalid permission_mode '{permission_mode}'. "
                f"Must be one of: {sorted(self._VALID_PERMISSION_MODES)}")
        self._permission_mode = permission_mode

        # runner.settings["model_reasoning_effort"] is accepted as a fallback;
        # runner.effort is canonical and wins when both are set.
        settings_effort = self._config_overrides.get("model_reasoning_effort")
        self._effort = effort or settings_effort
        if self._effort and self._effort not in self._VALID_EFFORTS:
            raise ValueError(
                f"Invalid effort '{self._effort}'. "
                f"Must be one of: {sorted(self._VALID_EFFORTS)}")
        if self._effort:
            self._config_overrides["model_reasoning_effort"] = self._effort
        # Serialize once now so an unrepresentable settings value fails at
        # construction (config-load time) instead of surfacing per case.
        for key, value in self._config_overrides.items():
            try:
                _toml_value(value)
            except ValueError as exc:
                raise ValueError(
                    f"Invalid Codex settings value for {key!r}: {exc}") from exc

        if self._permissions:
            warnings.warn(
                "Codex cannot translate eval permissions.allow/deny rules; "
                f"the run will use the '{self._codex_sandbox_mode()}' filesystem "
                "policy, but tool-level permission rules are not enforced.",
                RuntimeWarning,
                stacklevel=2,
            )

    def _codex_sandbox_mode(self) -> str:
        """Translate Claude Code's permission-mode intent to Codex."""
        if self._permission_mode == "plan":
            return "read-only"
        # default/acceptEdits/auto/dontAsk all permit edits subject to Claude's
        # approval/tool policy. Codex exec cannot reproduce those approval
        # details, so workspace-write is the closest safe filesystem policy.
        return "workspace-write"

    @property
    def name(self) -> str:
        return "codex"

    @property
    def version(self) -> str:
        try:
            result = subprocess.run(
                ["codex", "--version"], capture_output=True, text=True, timeout=5)
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
        del settings_path  # Codex has no equivalent settings-file flag.
        if max_budget_usd not in (None, 5.0):
            warnings.warn(
                f"Codex does not enforce max_budget_usd={max_budget_usd}; "
                "timeout and external provider limits are the available caps.",
                RuntimeWarning,
                stacklevel=2,
            )

        workspace = workspace.resolve()

        if target:
            skill_name = target.rsplit(":", 1)[-1]
            prompt = f"Use the {skill_name} skill"
            if args:
                prompt += f" with arguments: {args}"
        else:
            prompt = args or ""

        effective_system_prompt = system_prompt or self._system_prompt
        if effective_system_prompt:
            prompt = f"{effective_system_prompt.rstrip()}\n\n{prompt}"

        cmd = ["codex", "exec", "--json", "--ephemeral"]
        if self._permission_mode == "bypassPermissions":
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            cmd.extend(["--sandbox", self._codex_sandbox_mode()])
        cmd.extend(["--skip-git-repo-check", "-C", str(workspace)])
        if model:
            cmd.extend(["--model", model])
        for key, value in self._config_overrides.items():
            cmd.extend(["-c", f"{key}={_toml_value(value)}"])
        # Omitting PROMPT makes Codex read it from our dedicated stdin pipe.
        # This keeps prompts out of process listings and prevents inherited
        # parent stdin from hanging a run or being appended as a <stdin> block.
        cmd.extend(["--", "-"])

        start = time.monotonic()
        staged = None
        try:
            staged = self._stage_skills(workspace)
            popen_kwargs = dict(
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(workspace),
                text=True,
                env=self._build_env(extra_env),
            )
            if os.name != "nt":
                popen_kwargs["start_new_session"] = True
            proc = subprocess.Popen(cmd, **popen_kwargs)
            try:
                stdout, stderr = proc.communicate(input=prompt, timeout=timeout_s)
            except subprocess.TimeoutExpired as exc:
                partial_stdout = _as_text(exc.stdout)
                partial_stderr = _as_text(exc.stderr)
                try:
                    if os.name != "nt":
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    final_stdout, final_stderr = proc.communicate(timeout=5)
                    stdout = _prefer_complete(final_stdout, partial_stdout)
                    stderr = _prefer_complete(final_stderr, partial_stderr)
                except subprocess.TimeoutExpired:
                    stdout = partial_stdout
                    stderr = partial_stderr
                    if stderr:
                        stderr += "\n"
                    stderr += "Process group termination hung"
                events, normalized_stdout = self._parse_events(stdout or "")
                usage = _extract_usage(events)
                timeout_message = f"Timed out after {timeout_s}s"
                if stderr:
                    timeout_message += f"\n{stderr}"
                return RunResult(
                    exit_code=-1,
                    stdout=normalized_stdout,
                    stderr=timeout_message,
                    duration_s=time.monotonic() - start,
                    cost_usd=_estimate_cost_usd(events, model),
                    token_usage=usage["token_usage"],
                    num_turns=usage["num_turns"],
                    resolved_model=model or None,
                    models_used=[model] if model else None,
                    raw_output={"events": events},
                )
            except BaseException:
                # KeyboardInterrupt or other cancellation: codex runs in its
                # own session and never receives the terminal's SIGINT, so an
                # interrupted eval would leave it running (and spending).
                try:
                    if os.name != "nt":
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                raise
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return RunResult(
                exit_code=-1, stdout="", stderr=str(exc),
                duration_s=time.monotonic() - start,
                resolved_model=model or None,
            )
        finally:
            if staged is not None:
                self._cleanup_staged_skills(workspace, staged)

        events, normalized_stdout = self._parse_events(stdout or "")
        usage = _extract_usage(events)
        return RunResult(
            exit_code=proc.returncode,
            stdout=normalized_stdout,
            stderr=stderr or "",
            duration_s=time.monotonic() - start,
            cost_usd=_estimate_cost_usd(events, model),
            token_usage=usage["token_usage"],
            num_turns=usage["num_turns"],
            resolved_model=model or None,
            models_used=[model] if model else None,
            raw_output={"events": events},
        )

    def _parse_events(self, stdout: str) -> tuple[list[dict], str]:
        """Parse mixed JSONL output while preserving the original text."""
        events: list[dict] = []
        lines = stdout.splitlines()
        for line in lines:
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            events.append(event)
            if self._log_prefix:
                progress = self._extract_progress(event)
                if progress:
                    with _print_lock:
                        print(f"  {self._log_prefix} | {progress}", flush=True)
        normalized = "\n".join(lines) + ("\n" if lines else "")
        return events, normalized

    def _stage_skills(self, workspace: Path) -> dict:
        """Copy every plugin skill into a disposable workspace-local tree.

        The returned manifest must be passed to ``_cleanup_staged_skills``.
        Existing real skill directories are left untouched; dangling symlinks
        are rejected instead of being silently trusted.  In a Git workspace,
        the temporary tree is added to the repository-local exclude file so a
        repo-mode status baseline remains clean while Codex is running.
        """
        workspace = workspace.resolve()
        skills_dest = workspace / ".agents" / "skills"
        owner_marker = workspace / ".agents" / ".agent-eval-harness-codex-skills"
        manifest = {
            "created": [], "exclude": None, "owner_marker": owner_marker,
            "active": bool(self._skill_roots), "created_tree": False,
        }
        if not self._skill_roots:
            return manifest
        try:
            if owner_marker.is_file():
                # The agent can scribble on workspace files, so an unreadable
                # marker is "unrecognized", not a crash.
                try:
                    recognized = (
                        owner_marker.read_text() == "agent-eval-harness\n")
                except (OSError, UnicodeDecodeError):
                    recognized = False
                if not recognized:
                    raise ValueError(
                        f"Refusing unrecognized staged-skill marker: {owner_marker}")
                if skills_dest.is_symlink():
                    skills_dest.unlink()
                elif skills_dest.exists():
                    shutil.rmtree(skills_dest)
                owner_marker.unlink()
            elif skills_dest.exists() or skills_dest.is_symlink():
                raise ValueError(
                    "Refusing to replace an existing workspace skill tree not "
                    f"owned by agent-eval-harness: {skills_dest}")

            manifest["exclude"] = self._ensure_agents_gitignored(workspace)
            manifest["created_tree"] = True
            skills_dest.mkdir(parents=True, exist_ok=True)
            owner_marker.write_text("agent-eval-harness\n")
            for source_root in self._skill_roots:
                # Constructor validation catches configuration errors early;
                # repeat this check in case the source vanished between cases.
                if not source_root.is_dir():
                    raise FileNotFoundError(
                        f"Codex plugin skill directory not found: {source_root}")
                for source in sorted(source_root.iterdir()):
                    if not source.is_dir() or not (source / "SKILL.md").is_file():
                        continue
                    destination = skills_dest / source.name
                    if destination.is_symlink():
                        raise ValueError(
                            f"Refusing existing symlink at staged skill path: "
                            f"{destination}")
                    if destination.exists():
                        raise ValueError(
                            "Duplicate Codex skill name across plugin roots: "
                            f"{destination.name}")
                    # Record before copying so a copytree that fails midway is
                    # removed by cleanup instead of surviving as a truncated
                    # skill that later runs would treat as complete.
                    manifest["created"].append(destination)
                    try:
                        shutil.copytree(source, destination, symlinks=False)
                    except FileExistsError:
                        # Another staging caller won the atomic directory-create
                        # race. Its directory is not ours to clean up; treat a
                        # complete real directory as idempotent.
                        manifest["created"].remove(destination)
                        if destination.is_symlink() or not destination.is_dir():
                            raise
                        continue
            return manifest
        except BaseException:
            self._cleanup_staged_skills(workspace, manifest)
            raise

    @staticmethod
    def _ensure_agents_gitignored(workspace: Path):
        """Temporarily ignore the staged tree in a Git repo; return undo data."""
        # Most case workspaces are standalone temp directories. Avoid spawning
        # Git (and avoid coupling skill staging to a Git executable) unless a
        # repository marker exists at the workspace or one of its parents.
        if not any((candidate / ".git").exists()
                   for candidate in (workspace, *workspace.parents)):
            return None
        try:
            result = subprocess.run(
                ["git", "-C", str(workspace), "rev-parse",
                 "--git-path", "info/exclude", "--show-prefix"],
                capture_output=True, text=True, timeout=5, check=True)
        except (OSError, subprocess.SubprocessError):
            return None
        lines = result.stdout.splitlines()
        exclude = Path(lines[0].strip()) if lines else Path("")
        if not lines or not lines[0].strip():
            return None
        if not exclude.is_absolute():
            exclude = workspace / exclude
        # Anchor the rule to the workspace's path inside the repo: a bare
        # "/.agents/" only matches at the repo root, while workspaces often
        # live in nested paths such as eval/runs/<case>/workspace.
        prefix = lines[1].strip() if len(lines) > 1 else ""
        marker = "# agent-eval-harness temporary Codex skills"
        rule = f"/{prefix}.agents/"
        try:
            existing = exclude.read_text() if exclude.exists() else ""
            existing_lines = existing.splitlines()
            if any(existing_lines[index:index + 2] == [marker, rule]
                   for index in range(max(len(existing_lines) - 1, 0))):
                # A prior interrupted harness run left its exact marker/rule.
                # Adopt it so this run's cleanup removes the stale entry.
                return (exclude, marker, rule)
            if rule in existing_lines:
                return None
            exclude.parent.mkdir(parents=True, exist_ok=True)
            prefix = "" if not existing or existing.endswith("\n") else "\n"
            with exclude.open("a") as stream:
                stream.write(f"{prefix}{marker}\n{rule}\n")
            return (exclude, marker, rule)
        except OSError:
            return None

    @staticmethod
    def _cleanup_staged_skills(workspace: Path, manifest: dict) -> None:
        if not manifest.get("active"):
            return
        owner_marker = manifest.get("owner_marker")
        owns_tree = False
        try:
            owns_tree = (owner_marker is not None and owner_marker.is_file()
                         and owner_marker.read_text() == "agent-eval-harness\n")
        except (OSError, UnicodeDecodeError):
            pass
        if owns_tree:
            skills_dest = workspace / ".agents" / "skills"
            if skills_dest.is_symlink():
                skills_dest.unlink(missing_ok=True)
            elif skills_dest.exists():
                shutil.rmtree(skills_dest, ignore_errors=True)
            try:
                owner_marker.unlink()
            except OSError:
                pass
        else:
            for destination in reversed(manifest.get("created", [])):
                if destination.is_symlink():
                    destination.unlink(missing_ok=True)
                elif destination.exists():
                    shutil.rmtree(destination, ignore_errors=True)
        # Remove the (now empty) staging directories only when this run owned
        # or created them: when staging *refused* to touch a pre-existing
        # unowned tree, deleting even an empty user directory would
        # contradict that refusal.
        if owns_tree or manifest.get("created_tree"):
            for directory in (workspace / ".agents" / "skills",
                              workspace / ".agents"):
                try:
                    directory.rmdir()
                except OSError:
                    pass

        exclude_info = manifest.get("exclude")
        if exclude_info:
            exclude, marker, rule = exclude_info
            try:
                lines = exclude.read_text().splitlines()
                removed = False
                kept = []
                for line in lines:
                    if not removed and line == marker:
                        removed = True
                        continue
                    if removed and line == rule:
                        removed = False
                        continue
                    kept.append(line)
                exclude.write_text("\n".join(kept) + ("\n" if kept else ""))
            except OSError:
                pass

    def _build_env(self, extra_env: Optional[dict] = None) -> dict:
        env = {key: value for key, value in os.environ.items()
               if key in self._SAFE_ENV_KEYS}
        for key, value in self._env.items():
            if value is None:
                continue
            resolved = resolve_env_value(value)
            if resolved is not None:
                env[key] = resolved
        if extra_env:
            for key, value in extra_env.items():
                if value is not None:
                    env[key] = str(value)
        return env

    @staticmethod
    def _extract_progress(event: dict) -> str:
        """Return fixed metadata only; event payloads may contain secrets."""
        event_type = event.get("type")
        item = event.get("item")
        if not isinstance(item, dict):
            item = {}
        if event_type in {"item.started", "item.completed"}:
            if item.get("type") == "command_execution":
                state = "started" if event_type == "item.started" else "completed"
                return f"Shell command {state}"
            if event_type == "item.completed" and item.get("type") == "agent_message":
                return "Agent message completed"
        if event_type in {"turn.completed", "turn_completed"}:
            return "Turn complete"
        return ""


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def _prefer_complete(final, partial) -> str:
    final_text = _as_text(final)
    partial_text = _as_text(partial)
    return final_text if len(final_text) >= len(partial_text) else partial_text


def _toml_value(value) -> str:
    """Serialize a ``-c`` override value as TOML, which codex-cli parses.

    JSON is TOML-compatible for scalars and plain arrays, but TOML inline
    tables use ``key = value`` syntax — a JSON object would fall back to a
    plain string on the Codex side.
    """
    if value is None:
        raise ValueError("Codex TOML overrides do not support null values")
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("Codex TOML override mapping keys must be strings")
        inner = ", ".join(
            f"{json.dumps(key)} = {_toml_value(item)}"
            for key, item in value.items())
        return "{" + inner + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return json.dumps(value)


def _usage_count(usage: dict, key: str) -> int:
    """Read a token count defensively; agent-influenced events may be malformed."""
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _estimate_cost_usd(events: list[dict], model: Optional[str]) -> Optional[float]:
    """Estimate run cost from per-turn usage via LiteLLM's pricing table.

    Mirrors Harbor's Codex agent so local and Harbor runs price the same
    tokens the same way: the pricing key is the model name or its last path
    segment, and each turn is priced individually because rates can depend
    on the call's context size. Anything unpriceable — litellm absent, the
    model missing from the table, a failed calculation — yields None rather
    than a guessed or partial number.
    """
    if not model:
        return None
    try:
        import litellm
    except ImportError:
        return None
    # Candidate order mirrors Harbor (full name, then everything after the
    # first slash); the bare last segment is a final fallback for
    # multi-segment identifiers like provider/region/model.
    key = next((k for k in (model, model.split("/", 1)[-1],
                            model.rsplit("/", 1)[-1])
                if litellm.model_cost.get(k)), None)
    if key is None:
        return None
    total = 0.0
    priced = False
    for event in events:
        if event.get("type") not in {"turn.completed", "turn_completed"}:
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        try:
            input_cost, output_cost = litellm.cost_per_token(
                model=key,
                prompt_tokens=_usage_count(usage, "input_tokens"),
                completion_tokens=_usage_count(usage, "output_tokens"),
                cache_read_input_tokens=_usage_count(
                    usage, "cached_input_tokens"),
            )
        except Exception:
            return None
        total += float(input_cost + output_cost)
        priced = True
    return total if priced else None


def _extract_usage(events: list[dict]) -> dict:
    """Aggregate Codex ``turn.completed`` usage records."""
    input_tokens = output_tokens = cache_read = turns = 0
    for event in events:
        if event.get("type") not in {"turn.completed", "turn_completed"}:
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        turn_input = _usage_count(usage, "input_tokens")
        turn_cache = _usage_count(usage, "cached_input_tokens")
        input_tokens += max(turn_input - turn_cache, 0)
        output_tokens += _usage_count(usage, "output_tokens")
        cache_read += turn_cache
        turns += 1
    return {
        "token_usage": {
            "input": input_tokens,
            "output": output_tokens,
            "cache_read": cache_read,
        } if turns else None,
        "num_turns": turns or None,
    }
