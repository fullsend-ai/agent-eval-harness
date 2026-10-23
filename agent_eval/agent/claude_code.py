"""Claude Code CLI runner implementation."""

import errno
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Optional

from .base import EvalRunner, RunResult
from .stream_capture import (
    make_prompt_event, inject_timestamp, extract_usage,
    count_subagent_turns, combine_per_model_turns,
    setup_subagent_hook,
    assistant_message_id,
    classify_stream_errors,
    message_ids_for_run,
)
from agent_eval.tools.permissions import compile_permission_rules
from agent_eval.config import (
    resolve_env_value, resolve_plugin_dir, resolve_plugin_skill_roots)
from agent_eval.providers.base import parse_agent_model
from agent_eval.providers.env import MANAGED_ENV_KEYS, settings_env_block

_print_lock = threading.Lock()

# Conventional directories plugin discovery reads besides the manifest and
# the manifest-declared skill roots. Copied only when present.
# scripts/ is included because skills commonly invoke
# ${CLAUDE_PLUGIN_ROOT}/scripts/... or ${CLAUDE_SKILL_DIR}/../../scripts/...
# at runtime; a staged copy without it would break those plugins.
_PLUGIN_OPTIONAL_DIRS = ("commands", "agents", "hooks", "scripts")
# Optional plugin-root files Claude Code loads besides the manifest and
# conventional directories. Copied only when present.
_PLUGIN_OPTIONAL_FILES = (".mcp.json",)

# Bulk plugin discovery never reads — keeps the staged copy small.
_PLUGIN_IGNORE = shutil.ignore_patterns(".git", "node_modules", "__pycache__")


def _plugin_ignore(plugin: Path):
    """copytree ignore callback: bulk dirs, plus any symlink whose resolved
    target escapes the plugin. ``symlinks=False`` MATERIALIZES link targets,
    so a third-party plugin could otherwise plant a link to a host file
    (credentials, source data) and have staging copy it into the workspace
    where the agent can read it (CWE-59 -> CWE-200).
    """
    def ignore(src, names):
        ignored = set(_PLUGIN_IGNORE(src, names))
        for name in names:
            if name in ignored:
                continue
            entry = Path(src) / name
            if entry.is_symlink():
                try:
                    resolved = entry.resolve(strict=True)
                except (OSError, RuntimeError):
                    # Dangling or looping (loops raise RuntimeError on
                    # Python 3.11/3.12, OSError after) — not stageable.
                    ignored.add(name)
                    continue
                if not resolved.is_relative_to(plugin):
                    ignored.add(name)
        return ignored
    return ignore


def stage_plugin_dir(plugin_dir: Path, workspace: Path) -> Path:
    """Copy one plugin's discoverable content into the case workspace.

    WHY: ``--plugin-dir <path>`` lands verbatim in the session's system
    context — the stream-json init event registers the plugin under that
    path. When the configured path points outside the workspace (typically
    at the project repo under evaluation), the agent can follow it and
    read or write the real project: ``additionalDirectories`` gates the
    file tools, but Bash is not path-scoped, so a disclosed path is a
    standing escape vector out of the isolated workspace. Staging the
    plugin inside the throwaway workspace and passing THAT path keeps the
    real location out of the session entirely.

    Copies only what plugin discovery and execution need: the
    ``.claude-plugin/`` manifest, every skill root declared by the
    manifest (via ``resolve_plugin_skill_roots``, which validates
    containment), the conventional ``_PLUGIN_OPTIONAL_DIRS`` when they
    exist at the plugin root, and ``_PLUGIN_OPTIONAL_FILES`` (``.mcp.json``).
    Isolated MCP coverage is that conventional config file plus servers
    already reachable without extra trees (HTTP/npx/uvx, or code under
    ``scripts/``). Local stdio trees (``servers/``, …) and a custom
    ``mcpServers`` path in the manifest are not staged.
    Symlinks are not reproduced — in-plugin targets are copied as content,
    dangling ones are skipped, and links escaping the plugin are refused —
    so the staged tree cannot point back outside the workspace. Idempotent
    per workspace: an existing destination is reused; a partial copy never
    becomes the destination (copy into a temp sibling, then rename).
    """
    plugin = Path(plugin_dir).resolve()
    dest = workspace / ".staged-plugins" / plugin.name
    if dest.exists():
        return dest

    sources = [plugin / ".claude-plugin"]
    # A Claude plugin may legitimately export no skills (commands, agents or
    # hooks only). Tolerate exactly that layout — no 'skills' declaration in
    # the manifest AND no conventional skills/ directory — and let every
    # other error from resolve_plugin_skill_roots propagate: a malformed
    # manifest or a declared-but-missing root staged "successfully" would
    # only resurface later as an undiscoverable slash command, the silent
    # failure mode this staging exists to prevent.
    declares_skills = False
    manifest_path = plugin / ".claude-plugin" / "plugin.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            # Malformed manifest: let the authoritative resolver raise its
            # own, clearer error below.
            manifest = None
            declares_skills = True
        if isinstance(manifest, dict) and manifest.get("skills") is not None:
            declares_skills = True
    if declares_skills or (plugin / "skills").is_dir():
        sources.extend(resolve_plugin_skill_roots(plugin))
    sources.extend(plugin / name for name in _PLUGIN_OPTIONAL_DIRS)

    staging = dest.parent / f".{plugin.name}.partial"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        for source in sources:
            if not source.is_dir():
                continue
            # copytree with symlinks=False follows a source dir that is
            # ITSELF a symlink, and the ignore callback only sees entries
            # inside walked directories — an escaping link at the copy root
            # (e.g. scripts -> ~/.secrets) would be materialized wholesale.
            # Apply the same containment rule to the roots.
            try:
                resolved = source.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if not resolved.is_relative_to(plugin):
                continue
            # Copy from the CHECKED canonical path, not the symlink: copying
            # from `source` would re-follow the link at copy time, letting a
            # concurrent writer swap it between check and use (CWE-367).
            shutil.copytree(
                resolved, staging / source.relative_to(plugin),
                symlinks=False, ignore=_plugin_ignore(plugin),
                ignore_dangling_symlinks=True, dirs_exist_ok=True)
        for name in _PLUGIN_OPTIONAL_FILES:
            source = plugin / name
            if not source.is_file():
                continue
            try:
                resolved = source.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if not resolved.is_file() or not resolved.is_relative_to(plugin):
                continue
            shutil.copy2(resolved, staging / name)
        try:
            staging.replace(dest)
        except OSError:
            # Another stager won the rename race; its complete copy stands.
            if dest.is_dir():
                shutil.rmtree(staging, ignore_errors=True)
                return dest
            raise
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return dest


# Public effort vocabulary — importable by Harbor orchestration without
# reaching into a protected class attribute (mirrors CODEX_EFFORTS).
CLAUDE_CODE_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


class ClaudeCodeRunner(EvalRunner):
    """Runs skills using the Claude Code CLI in non-interactive mode."""

    _VALID_EFFORTS = CLAUDE_CODE_EFFORTS
    _VALID_PERMISSION_MODES = {
        "default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions",
        # Every unlisted tool call becomes a prompt, which `--print` has nobody
        # to answer — see `_VALID_PERMISSION_PROMPTS`. Agent judges run under
        # it. (`manual` is in `claude --help`'s choices on 2.1.299; `default`
        # is not, but is still accepted.)
        "manual",
    }
    # `--permission-prompts none` means "nobody answers a prompt, so anything
    # that would prompt is denied". Paired with a non-permissive mode it is
    # what makes an allow list authoritative under `--print`; without it an
    # unlisted tool simply runs (#239).
    _VALID_PERMISSION_PROMPTS = {"host", "none"}

    @classmethod
    def from_config(cls, config, *, log_prefix=None, **overrides):
        resolved_plugin_dirs = [
            str(resolve_plugin_dir(config, configured))
            for configured in config.runner.plugin_dirs
        ]
        return cls(
            permissions=overrides.get("permissions", config.permissions),
            plugin_dirs=resolved_plugin_dirs,
            workspace_mode=config.runner.workspace_mode,
            env=config.runner.env,
            system_prompt=config.runner.system_prompt,
            subagent_model=overrides.get("subagent_model"),
            mlflow_experiment=overrides.get("mlflow_experiment"),
            mlflow_tracking_uri=overrides.get("mlflow_tracking_uri"),
            effort=overrides.get("effort", config.runner.effort),
            permission_mode=overrides.get(
                "permission_mode", config.runner.permission_mode),
            # Deliberately override-only: this is a judge-side lever with no
            # eval.yaml surface, so a skill-under-test cannot weaken itself
            # into the permissive posture an agent judge is moved out of.
            permission_prompts=overrides.get("permission_prompts"),
            log_prefix=log_prefix,
            provider_plan=overrides.get("provider_plan"),
            run_id=overrides.get("run_id"),
        )

    def __init__(
        self,
        permissions: Optional[dict] = None,
        subagent_model: Optional[str] = None,
        plugin_dirs: Optional[list] = None,
        workspace_mode: Optional[str] = None,
        env: Optional[dict] = None,
        system_prompt: Optional[str] = None,
        mlflow_experiment: Optional[str] = None,
        mlflow_tracking_uri: Optional[str] = None,
        log_prefix: Optional[str] = None,
        effort: Optional[str] = None,
        permission_mode: Optional[str] = None,
        permission_prompts: Optional[str] = None,
        provider_plan=None,
        run_id: Optional[str] = None,
    ):
        # spec 014: the plan (an OpenRouter-routed run) is a constructor
        # argument — it decides the env the CLI starts with, the id on the
        # wire and the budget flag; the per-case binding arrives per execute().
        self._plan = provider_plan
        self._run_id = run_id
        self._binding = None
        self._permissions = permissions or {}
        self._subagent_model = subagent_model
        self._plugin_dirs = plugin_dirs or []
        self._workspace_mode = workspace_mode
        self._env = env or {}
        self._system_prompt = system_prompt
        self._mlflow_experiment = mlflow_experiment
        self._mlflow_tracking_uri = mlflow_tracking_uri
        self._log_prefix = log_prefix
        if effort and effort not in self._VALID_EFFORTS:
            raise ValueError(
                f"Invalid effort '{effort}'. "
                f"Must be one of: {sorted(self._VALID_EFFORTS)}")
        self._effort = effort
        if permission_mode is not None and (
            not isinstance(permission_mode, str)
            or permission_mode not in self._VALID_PERMISSION_MODES
        ):
            raise ValueError(
                f"Invalid permission_mode '{permission_mode}'. "
                f"Must be one of: {sorted(self._VALID_PERMISSION_MODES)}")
        self._permission_mode = permission_mode
        if permission_prompts is not None and (
            not isinstance(permission_prompts, str)
            or permission_prompts not in self._VALID_PERMISSION_PROMPTS
        ):
            raise ValueError(
                f"Invalid permission_prompts '{permission_prompts}'. "
                f"Must be one of: {sorted(self._VALID_PERMISSION_PROMPTS)}")
        self._permission_prompts = permission_prompts

    def bind_provider(self, binding) -> None:
        """The provider session's per-case binding: generation ids are sighted
        through it as the stream is read, the hook's ids after the run."""
        self._binding = binding

    @property
    def name(self) -> str:
        return "claude-code"

    @property
    def version(self) -> str:
        """Get the Claude Code CLI version."""
        try:
            result = subprocess.run(
                ["claude", "--version"], capture_output=True, text=True, timeout=5)
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
        plan = self._plan
        cmd = [
            "claude",
            "--print",
            "--model", _wire_model(model, plan),
            "--output-format", "stream-json" if self._log_prefix else "json",
            # Session persistence must stay ON so subagent transcript files
            # survive long enough for the SubagentStop hook to copy them.
            # The session directory is cleaned up post-run (see below).
        ]
        cap_flag = _cli_budget_flag(max_budget_usd, plan)
        if cap_flag is not None:
            cmd.extend(["--max-budget-usd", cap_flag])
        if self._log_prefix:
            cmd.append("--verbose")

        if self._effort:
            cmd.extend(["--effort", self._effort])

        if self._permission_mode:
            cmd.extend(["--permission-mode", self._permission_mode])
        if self._permission_prompts:
            cmd.extend(["--permission-prompts", self._permission_prompts])

        plugin_dirs = self._plugin_dirs
        if plugin_dirs:
            # Always pass a workspace-local copy so the real plugin path never
            # enters the session context (see stage_plugin_dir). A plugin that
            # already lives inside the workspace is passed through unchanged,
            # and workspace_mode: repo skips staging entirely — the workspace
            # IS the project there, so there is nothing to isolate and staging
            # would write junk into the user's repo.
            try:
                plugin_dirs = self._staged_plugin_dirs(workspace)
            except (OSError, ValueError, FileNotFoundError) as e:
                return RunResult(
                    exit_code=-1, stdout="",
                    stderr=f"Plugin staging failed: {e}", duration_s=0.0,
                )
        for plugin_dir in plugin_dirs:
            cmd.extend(["--plugin-dir", str(plugin_dir)])

        effective_prompt = system_prompt or self._system_prompt
        if effective_prompt:
            cmd.extend(["--append-system-prompt", effective_prompt])

        # Permissions: handle both simple and path-based formats
        # If path-based (list of dicts), create a temporary settings file
        deny = self._permissions.get("deny", [])
        allow = self._permissions.get("allow", [])

        temp_settings_file = None
        # Check if ANY element is path-based (dict), not just the first
        has_path_based = (
            any(isinstance(item, dict) for item in deny) if deny else False
        ) or (
            any(isinstance(item, dict) for item in allow) if allow else False
        )

        if has_path_based or plan is not None:
            # One settings overlay carries what must beat every other layer:
            # the path-based permission rules compiled from eval.yaml and,
            # under a provider plan, the Direct transport env block (the plan
            # wins for the managed keys; spec 014). It is applied last via
            # --settings and removed in the finally below.
            temp_settings_file = self._write_settings_overlay(
                workspace, settings_path,
                deny if has_path_based else [], allow if has_path_based else [], plan)
            settings_path = temp_settings_file
        if not has_path_based:
            # Simple format - use CLI flags directly
            if deny:
                cmd.extend(["--disallowed-tools", ",".join(deny)])
            if allow:
                cmd.extend(["--allowed-tools", ",".join(allow)])

        # Add --settings flag after all permission mutations
        if settings_path:
            cmd.extend(["--settings", str(settings_path)])

        # Build the prompt (passed via stdin)
        # For case/batch mode: /{skill} {args}
        # For prompt mode: {args} (direct prompt, no skill wrapper)
        if target:
            prompt = f"/{target}"
            if args:
                prompt += f" {args}"
        else:
            prompt = args or ""

        start = time.monotonic()
        stdout_lines = []
        deadline = start + timeout_s
        timed_out = False

        # Track temp settings file for cleanup
        cleanup_settings = temp_settings_file
        cost_source = None
        proc = None
        stream_ids_seen = []          # every assistant id parsed, sighted or not

        try:
            # Inside the try: the overlay already holds the key, so a failure
            # here (e.g. the hook-ids directory cannot be created) must still
            # reach the finally that removes it.
            env = self._build_env(extra_env=extra_env)
            cost_source = _runner_cost_source(env, plan)
            msg_index = 0
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(workspace),
                text=True,
                env=env,
            )

            proc.stdin.write(prompt)
            proc.stdin.close()

            # Watchdog thread: kill the process when the deadline passes.
            # The stdout readline loop blocks during extended thinking, so
            # an in-loop check never fires.  Killing the process closes
            # stdout, which unblocks the for-loop.
            def _watchdog():
                nonlocal timed_out
                remaining = max(0, deadline - time.monotonic())
                try:
                    proc.wait(timeout=remaining if remaining > 0 else 0.1)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    proc.kill()

            watchdog = threading.Thread(target=_watchdog, daemon=True)
            watchdog.start()

            # Inject synthetic user event for the prompt
            if self._log_prefix:
                stdout_lines.append(make_prompt_event(prompt))

            result_obj = None
            resolved_model = None
            permission_denials = 0
            result_denials = []

            for line in proc.stdout:
                if time.monotonic() > deadline:
                    raise subprocess.TimeoutExpired(cmd, timeout_s)
                line = line.rstrip("\n")
                if not line.strip():
                    stdout_lines.append(line)
                    continue
                if self._log_prefix:
                    try:
                        line = inject_timestamp(line)
                        obj = json.loads(line)
                        if (not resolved_model
                                and obj.get("type") == "system"
                                and obj.get("subtype") == "init"):
                            resolved_model = obj.get("model")
                        if self._binding is not None:
                            # Sight generation ids as the stream is read, so
                            # the backfill starts before the process exits.
                            mid, mecho = assistant_message_id(obj)
                            if mid:
                                msg_index += 1
                                stream_ids_seen.append(mid)     # before sight: it is billed either way
                                self._binding.sight(mid, message_index=msg_index,
                                                    model_echo=mecho)
                        msg = _extract_progress(obj, estimate=plan is not None)
                        if msg:
                            if msg.startswith("PERMISSION DENIED"):
                                permission_denials += 1
                            with _print_lock:
                                print(f"  {self._log_prefix} | {msg}", flush=True)
                        if obj.get("type") == "result":
                            result_obj = obj
                            # A session the CLI resumes (e.g. after background
                            # task notifications) emits one result event PER
                            # segment, each carrying only that segment's
                            # denials. Keeping just the last event drops every
                            # earlier segment's list — a real run lost 7
                            # denials that way, hiding a workspace escape from
                            # run_result.json.
                            seg = obj.get("permission_denials")
                            if isinstance(seg, list):
                                result_denials.extend(seg)
                    except json.JSONDecodeError:
                        pass
                stdout_lines.append(line)

            stderr = proc.stderr.read()
            proc.wait(timeout=5)
            if timed_out:
                raise subprocess.TimeoutExpired(cmd, timeout_s)

        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            duration = time.monotonic() - start
            (token_usage, cost_usd, num_turns, stream_ids, models_seen,
             per_model_usage, stream_ids_by_model) = extract_usage(stdout_lines)
            # Add subagent turns from captured transcripts, deduplicating
            # against IDs already seen in the stream
            subagent_turns = count_subagent_turns(workspace / "subagents", already_seen=stream_ids)
            if subagent_turns:
                num_turns = (num_turns or 0) + subagent_turns
            per_model_turns = combine_per_model_turns(
                workspace / "subagents", stream_ids_by_model)
            # An evaluator timeout can land after the CLI already emitted
            # usage data — same under-reporting risk as the bg-kill path.
            cost_usd = _billed_cost(cost_usd, per_model_usage)
            timeout_stderr = f"Timed out after {timeout_s}s"
            denial_list = _extract_denial_list(result_obj, permission_denials, result_denials)
            if denial_list:
                timeout_stderr += (f"\nWARNING: {len(denial_list)} permission "
                                   f"denial(s) detected during execution")
            message_ids = message_ids_for_run(stream_ids, workspace / "subagents")

            return RunResult(
                exit_code=-1,
                stdout="\n".join(stdout_lines),
                stderr=timeout_stderr,
                duration_s=duration,
                token_usage=token_usage,
                cost_usd=cost_usd,
                num_turns=num_turns,
                resolved_model=resolved_model,
                models_used=sorted(models_seen) if models_seen else None,
                per_model_usage=per_model_usage,
                per_model_turns=per_model_turns,
                permission_denials=denial_list,
                message_ids=message_ids,
                **self._provenance(stdout_lines, message_ids, cost_usd, cost_source),
            )
        except Exception as e:
            duration = time.monotonic() - start
            # Stop and reap the CLI first: a child left running would keep
            # producing billed generations nobody reads (CWE-772). Drain what
            # it already wrote — those ids are billed too — then keep every
            # streamed id as the result's cost-truth key set and let the
            # binding finish (subagent transcripts, hook ids), or the partial
            # run's spend would stay unpriced and unaudited.
            if proc is not None:
                stdout_lines.extend(_reap(proc))
            stream_ids = set(extract_usage(stdout_lines)[3]) | set(stream_ids_seen)
            message_ids = message_ids_for_run(stream_ids, workspace / "subagents")
            return RunResult(
                exit_code=-1, stdout="", stderr=str(e), duration_s=duration,
                message_ids=message_ids,
                **self._provenance(stdout_lines, message_ids, None, cost_source),
            )
        finally:
            # The overlay may hold the plan's literal key: gone on every path
            # (normal, timeout, KeyboardInterrupt, post-processing errors).
            if cleanup_settings is not None:
                try:
                    cleanup_settings.unlink()
                except OSError:
                    pass

        duration = time.monotonic() - start
        stdout_text = "\n".join(stdout_lines)

        # Clean up session directory now that SubagentStop hooks have fired
        # and copied transcripts.  Without this, session files accumulate
        # in ~/.claude/projects/ for every eval run.
        self._cleanup_session(workspace)

        # Extract usage from collected stream-json lines
        raw_output = result_obj
        if not result_obj and stdout_text.strip():
            try:
                result_obj = json.loads(stdout_text)
                raw_output = result_obj
            except json.JSONDecodeError:
                pass

        (token_usage, cost_usd, num_turns, stream_ids, models_seen,
         per_model_usage, stream_ids_by_model) = extract_usage(stdout_lines)
        if not cost_usd and isinstance(result_obj, dict):
            cost_usd = result_obj.get("total_cost_usd")

        cost_usd = _billed_cost(cost_usd, per_model_usage)

        # Add subagent turns from captured transcripts, deduplicating
        # against IDs already seen in the stream (Claude Code >= 2.1.108
        # streams subagent messages in stdout too)
        subagent_turns = count_subagent_turns(workspace / "subagents", already_seen=stream_ids)
        if subagent_turns:
            num_turns = (num_turns or 0) + subagent_turns
        per_model_turns = combine_per_model_turns(
            workspace / "subagents", stream_ids_by_model)

        denial_list = _extract_denial_list(result_obj, permission_denials, result_denials)
        if denial_list:
            denial_msg = (f"\nWARNING: {len(denial_list)} permission "
                          f"denial(s) detected during execution")
            stderr = (stderr or "") + denial_msg

        # An unrecognised slash command is reported by the CLI as a successful
        # run. Fail the case instead of letting a never-started skill look like
        # a skill that produced nothing.
        exit_code = proc.returncode
        # The CLI kills background tasks that outlive the final turn by the
        # bg-wait ceiling (default 600s) and still exits 0 with a "success"
        # result event. For pipeline skills whose real work happens in
        # background agents, that is a dead case wearing an OK label: on a
        # real run the killed agent left half-written artifacts and the case
        # was published as "OK | exit 0". Fail it honestly.
        if _BG_KILL_RE.search(stderr or "") and exit_code == 0:
            exit_code = 1
            stderr = (stderr or "") + (
                "\nERROR: the CLI terminated still-running background tasks at "
                "the bg-wait ceiling — their work is incomplete and artifacts "
                "may be half-written. Raise CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS "
                "(0 = wait indefinitely) for long-running pipeline skills, via "
                "the environment or runner.env in eval.yaml."
            )
        unknown_command = _detect_unknown_command(result_obj)
        if unknown_command and exit_code == 0:
            exit_code = 1
            stderr = (stderr or "") + (
                f"\nERROR: the agent did not recognise '{unknown_command}' "
                f"(0 turns, no work performed). The skill is not discoverable "
                f"at runtime — check the skill name, and set runner.plugin_dirs "
                f"if it is packaged as a plugin rather than living in "
                f".claude/skills."
            )

        message_ids = message_ids_for_run(stream_ids, workspace / "subagents")
        return RunResult(
            exit_code=exit_code,
            stdout=stdout_text,
            stderr=stderr or "",
            duration_s=duration,
            token_usage=token_usage,
            cost_usd=cost_usd,
            num_turns=num_turns,
            resolved_model=resolved_model,
            models_used=sorted(models_seen) if models_seen else None,
            per_model_usage=per_model_usage,
            per_model_turns=per_model_turns,
            permission_denials=denial_list,
            raw_output=raw_output,
            message_ids=message_ids,
            **self._provenance(stdout_lines, message_ids, cost_usd, cost_source),
        )

    def _provenance(self, stdout_lines, message_ids, cost_usd, cost_source) -> dict:
        """The cost-provenance RunResult fields (spec 014): hand the binding
        every id the stream did not show (subagent transcripts, the hook's own
        ids), classify the run's visible provider errors, and label the CLI's
        own number — an estimate under a plan (Claude Code prices at Anthropic
        rates), ``runner:reported`` on Anthropic/Vertex."""
        if self._binding is not None:
            self._binding.after_run(message_ids)
        error_class, budget = classify_stream_errors(stdout_lines)
        return {
            "cost_source": cost_source,
            "cost_usd_estimate": cost_usd if self._plan is not None else None,
            "error_class": error_class,
            "budget": budget,
        }

    def _write_settings_overlay(self, workspace, settings_path, deny, allow, plan) -> Path:
        """Write the per-run settings overlay and return its path.

        Written next to the case settings file when there is one (the case
        workspace, never the repo in in-repo mode), else in the workspace.
        Path-based ``deny``/``allow`` rules are compiled and merged over the
        existing rules; under a ``plan`` the ``env`` block becomes
        ``{**existing_env, **settings_env_block(plan, secrets="literal")}`` —
        the plan wins for the managed keys, everything else is kept — and the
        file is 0600 because it holds the inference key.
        """
        base = Path(settings_path) if settings_path and Path(settings_path).exists() else None
        target_dir = base.parent if base is not None else Path(workspace)
        overlay = target_dir / (".eval-overlay.json" if plan is not None else ".eval-permissions.json")
        settings_config: dict = {}
        if base is not None:
            try:
                settings_config = json.loads(base.read_text())
            except Exception:
                settings_config = {}
            if not isinstance(settings_config, dict):
                settings_config = {}
        if deny or allow:
            # Compile eval.yaml deny/allow rules into Claude Code patterns via
            # the shared compiler (gitignore-recursive paths; Bash skipped as
            # a no-op), merging with any rules already in the workspace
            # settings (e.g. the repo-write protection).
            perms = settings_config.setdefault("permissions", {})
            if deny:
                existing = perms.get("deny")
                merged = list(existing) if isinstance(existing, list) else []
                for pattern in compile_permission_rules(deny, harden_bash=True):
                    if pattern not in merged:
                        merged.append(pattern)
                perms["deny"] = merged
            if allow:
                existing = perms.get("allow")
                merged = list(existing) if isinstance(existing, list) else []
                for pattern in compile_permission_rules(allow):
                    if pattern not in merged:
                        merged.append(pattern)
                perms["allow"] = merged
        if plan is not None:
            existing_env = settings_config.get("env")
            existing_env = dict(existing_env) if isinstance(existing_env, dict) else {}
            block = {k: v for k, v in settings_env_block(plan, secrets="literal").items()
                     if v is not None}
            settings_config["env"] = {**existing_env, **block}
        data = json.dumps(settings_config, indent=2)
        # The agent under test can write to the workspace (and a multi-step
        # case shares it between steps), and the file holds the literal
        # inference key: never follow a symlink, never write through a
        # hardlink, never truncate an entry we did not create (CWE-59 /
        # CWE-367). A stale regular overlay from a run that never reached its
        # finally is removed; anything else at the path is refused, and the
        # create itself is exclusive so nothing can slip in between.
        if overlay.is_symlink():
            raise RuntimeError(f"refusing to write the settings overlay: {overlay} is a symlink")
        try:
            existing = overlay.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if existing.st_nlink > 1 or not stat.S_ISREG(existing.st_mode):
                raise RuntimeError(
                    f"refusing to write the settings overlay: {overlay} is a pre-existing "
                    f"entry with {existing.st_nlink} link(s)")
            overlay.unlink()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(overlay, flags, 0o600)
        except OSError as exc:
            if exc.errno in (errno.EEXIST, errno.ELOOP, errno.EMLINK):
                raise RuntimeError(
                    f"refusing to write the settings overlay: {overlay} appeared between "
                    "check and create") from exc
            raise
        with os.fdopen(fd, "w") as f:
            if plan is not None:
                os.fchmod(fd, 0o600)      # O_CREAT's mode is umask-masked / ignored on reuse
            f.write(data)
        return overlay

    def _staged_plugin_dirs(self, workspace: Path) -> list:
        """Stage every configured plugin into the workspace; return the copies.

        The staged path is keyed by the plugin's directory name, so two
        different plugins sharing a basename would silently collapse into
        one copy — fail loud instead.
        """
        # workspace_mode: repo runs in the user's real repository: there is
        # no isolation boundary for staging to defend (the session already
        # has the project), and staging an external plugin would write
        # .staged-plugins/ into the repo — polluting it and reading back as
        # a spurious repo modification. Pass every configured path through.
        if self._workspace_mode == "repo":
            return [str(Path(p).resolve()) for p in self._plugin_dirs]
        staged = []
        seen: dict = {}
        ws = Path(workspace).resolve()
        for configured in self._plugin_dirs:
            plugin = Path(configured).resolve()
            # A plugin already inside the workspace is passed through: its
            # path discloses nothing outside the sandbox, and re-staging it
            # would be pointless.
            if plugin == ws or plugin.is_relative_to(ws):
                staged.append(str(plugin))
                continue
            previous = seen.setdefault(plugin.name, plugin)
            if previous != plugin:
                raise ValueError(
                    "plugin staging cannot stage two different plugins with "
                    f"the same directory name: {previous} and {plugin}")
            staged.append(str(stage_plugin_dir(plugin, workspace)))
        return staged

    @staticmethod
    def _cleanup_session(workspace: Path) -> None:
        """Remove the Claude Code session directory for a workspace.

        Claude Code stores sessions under ~/.claude/projects/<encoded-path>/.
        The path encoding replaces '/' with '-' and prepends '-'.
        """
        projects_dir = Path.home() / ".claude" / "projects"
        if not projects_dir.exists():
            return
        encoded = "-" + str(workspace).replace("/", "-")
        session_dir = projects_dir / encoded
        if session_dir.exists() and session_dir.is_dir():
            shutil.rmtree(session_dir, ignore_errors=True)

    # Environment keys safe to forward to evaluated skills
    _SAFE_ENV_KEYS = {
        "PATH", "HOME", "USER", "SHELL", "LANG", "LC_ALL", "TERM",
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL", "ANTHROPIC_VERTEX_PROJECT_ID",
        "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "CLOUD_ML_REGION", "CLAUDE_CODE_USE_VERTEX",
        # OpenShell >= 0.1.2 has no metadata server in the sandbox: agentic-ci's
        # env script points Claude Code at the Vertex host directly and hands it
        # a placeholder bearer token that the supervisor proxy swaps for the real
        # one (CLAUDE_CODE_SKIP_VERTEX_AUTH=1, ANTHROPIC_AUTH_TOKEN=<placeholder>,
        # ANTHROPIC_VERTEX_BASE_URL=<host>/v1). Dropping the two switches here
        # sends Claude Code back to Application Default Credentials, which fail.
        "CLAUDE_CODE_SKIP_VERTEX_AUTH", "ANTHROPIC_VERTEX_BASE_URL",
        "CLAUDE_CODE_MAX_RETRIES",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_CODE_SUBAGENT_MODEL",
        # The bg-kill failure note tells users to raise this; an exact-name
        # allowlist would otherwise swallow the export and make that advice
        # a lie. runner.env also works and wins on collision.
        "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS",
        "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT",
        "CLOUDSDK_CONFIG", "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
        "MLFLOW_TRACKING_URI", "MLFLOW_EXPERIMENT_NAME",
        "AGENT_EVAL_RUNS_DIR",
    }

    # Forwarded from the ambient environment only without a provider plan: the
    # plan blanks every Vertex switch, so the agent has no use for the
    # operator's Google credential locations, and none of them belongs in the
    # environment of an agent whose model traffic goes to a third party (the
    # Kubernetes runner already skips the credentials mount under a plan).
    # An explicit ``runner.env`` entry still passes: that is the operator's
    # decision, not an ambient leak.
    _VERTEX_CREDENTIAL_KEYS = frozenset({
        "GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CONFIG",
        "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
    })
    # The OpenShell placeholder switches above: Vertex routing details with no
    # use under a plan, which blanks Vertex, so they stay home with the
    # credential locations.
    _VERTEX_PLACEHOLDER_KEYS = frozenset({
        "CLAUDE_CODE_SKIP_VERTEX_AUTH", "ANTHROPIC_VERTEX_BASE_URL",
    })

    def _build_env(self, extra_env=None):
        """Build subprocess environment with allowlisted keys only.

        Under a provider plan (spec 014) every managed key is removed from the
        ambient copy first — routing must not depend on a settings-env empty
        string overriding a non-empty process value, and the host's real
        ``ANTHROPIC_API_KEY``/``ANTHROPIC_AUTH_TOKEN`` can never reach the CLI,
        its subagents or its hook children. The provider key variables are
        not in the allowlist: the agent sees the inference key only as
        ``ANTHROPIC_AUTH_TOKEN`` in the overlay. The Google credential
        locations (``_VERTEX_CREDENTIAL_KEYS``) are not forwarded either.
        """
        ambient = self._SAFE_ENV_KEYS
        if self._plan is not None:
            ambient = ambient - self._VERTEX_CREDENTIAL_KEYS - self._VERTEX_PLACEHOLDER_KEYS
        env = {k: v for k, v in os.environ.items() if k in ambient}
        for k, v in self._env.items():
            if v is None:
                continue
            resolved = resolve_env_value(v)
            if resolved is not None:
                env[k] = resolved
        if extra_env:
            for k, v in extra_env.items():
                env[k] = str(v)
        if self._subagent_model and self._plan is None:
            # Under a plan the overlay owns the alias (bare id, plan wins).
            env["CLAUDE_CODE_SUBAGENT_MODEL"] = self._subagent_model
        if self._binding is not None and getattr(self._binding, "hook_ids_path", None):
            hook_ids = Path(self._binding.hook_ids_path)
            hook_ids.parent.mkdir(parents=True, exist_ok=True)
            env["AGENT_EVAL_HOOK_IDS"] = str(hook_ids)
        if self._mlflow_experiment:
            env["MLFLOW_EXPERIMENT_NAME"] = self._mlflow_experiment
        if self._mlflow_tracking_uri:
            env["MLFLOW_TRACKING_URI"] = self._mlflow_tracking_uri
        if self._plan is not None:
            # After every merge: runner.env is validated at load, but a hook's
            # runtime `.hook-outputs.yaml` env (extra_env) is not — nothing may
            # put a managed key back into the process env (CWE-15).
            for k in MANAGED_ENV_KEYS:
                env.pop(k, None)
        return env


def _reap(proc, timeout_s: float = 10.0) -> list:
    """Kill a still-running CLI, drain the rest of its stdout (the lines may
    carry billed generation ids) and wait for it — all bounded by
    ``timeout_s``. A background descendant (an async hook) may keep the CLI's
    stdout open after the CLI is dead, so the drain runs in a daemon thread
    and whatever it read by the deadline is what gets accounted for.
    Returns the drained lines."""
    lines: list = []
    try:
        if proc.poll() is None:
            proc.kill()
    except OSError:
        pass
    if proc.stdout is not None:
        def _drain():
            try:
                for line in proc.stdout:
                    lines.append(line.rstrip("\n"))
            except (OSError, ValueError):
                pass

        drain = threading.Thread(target=_drain, name="claude-reap-drain", daemon=True)
        drain.start()
        drain.join(timeout_s)
    try:
        proc.wait(timeout=timeout_s)
    except (subprocess.TimeoutExpired, OSError):
        pass
    return list(lines)


def _wire_model(model, plan):
    """The id on the wire: the bare ``slug:variants`` under a plan (the URI
    scheme is the harness's, not the CLI's); the value as given otherwise."""
    if plan is None or not model:
        return model
    try:
        return parse_agent_model(model).id
    except ValueError:
        return model


def _cli_budget_flag(max_budget_usd, plan):
    """``--max-budget-usd`` value. Without a plan: unchanged. Under a plan the
    cap is multiplied by ``cli_budget_inflation`` (the CLI enforces it on its
    Anthropic-priced estimate, 2–60× the real OpenRouter cost) and a cap
    ``<= 0``/``None`` emits **no flag** — the CLI rejects ``0`` (probe #23)."""
    if plan is None:
        return str(max_budget_usd)
    if max_budget_usd is None or max_budget_usd <= 0:
        return None
    return str(round(max_budget_usd * float(plan.cli_budget_inflation), 6))


def _runner_cost_source(env, plan):
    """What the CLI's ``total_cost_usd`` is: an estimate under a plan or behind
    an operator endpoint (any ``ANTHROPIC_BASE_URL`` host other than
    ``api.anthropic.com`` in the effective env), ``runner:reported`` otherwise."""
    if plan is not None:
        return "runner:estimate"
    base = (env or {}).get("ANTHROPIC_BASE_URL") or ""
    host = urllib.parse.urlsplit(base).hostname if base else None
    return "runner:estimate" if host and host != "api.anthropic.com" else "runner:reported"


def _billed_cost(cost_usd, per_model_usage):
    """The larger of conversation cost and per-model billed cost.

    total_cost_usd covers the CONVERSATION; modelUsage covers every token
    billed, including a background agent killed after the final turn (or
    still running at an evaluator timeout). Normally they agree to the cent —
    when modelUsage is higher, the difference is real spend the conversation
    never saw (a real case published $0.30 while burning $1.47).
    """
    per_model_total = sum(
        (v or {}).get("cost_usd") or 0
        for v in (per_model_usage or {}).values())
    if per_model_total and per_model_total > (cost_usd or 0) + 0.01:
        return per_model_total
    return cost_usd


_UNKNOWN_COMMAND_RE = re.compile(r"^Unknown command:\s*(/\S+)")

# Emitted on stderr when the CLI gives up waiting for background tasks
# (message text as of Claude Code 2.1.x; keep the match loose).
_BG_KILL_RE = re.compile(r"Background tasks still running after .*terminating", re.S)


def _detect_unknown_command(result_obj) -> Optional[str]:
    """Return the slash command the CLI did not recognise, if that is what happened.

    Claude Code answers an unrecognised slash command with plain text and still
    reports success::

        {"type": "result", "subtype": "success", "is_error": false,
         "num_turns": 0, "total_cost_usd": 0, "result": "Unknown command: /x"}

    The process exits 0, so an eval whose skill never resolves (a plugin-packaged
    skill with no runner.plugin_dirs, a typo, a plugin that failed to load) finishes
    in seconds with every case marked OK and no artifacts — indistinguishable from a
    skill that ran and produced nothing.

    ``num_turns`` guards the match: a real run that merely quotes the phrase has
    turns, an unrecognised command never does.

    The guard suppresses only on *positive evidence of work*. A missing, null or
    non-numeric count is deliberately not read as "zero turns", but neither does
    it suppress — the leading-phrase match below is the actual signal. Demanding
    a literal integer ``0`` would trade a far-fetched false positive (a partial
    payload whose result text also begins with "Unknown command: /") for a false
    negative that silently restores the original green-but-broken run if the
    payload shape ever changes.
    """
    if not isinstance(result_obj, dict):
        return None
    turns = result_obj.get("num_turns")
    if isinstance(turns, (int, float)) and turns > 0:
        return None
    text = result_obj.get("result")
    if not isinstance(text, str):
        return None
    match = _UNKNOWN_COMMAND_RE.match(text.strip())
    return match.group(1) if match else None


def _extract_denial_list(result_obj, streaming_count, collected=None):
    """Build the permission_denials list for RunResult.

    Prefers the structured ``permission_denials`` arrays from the CLI
    ``result`` events (available since Claude Code 2.x). ``collected`` is the
    union across ALL result events of the session — a resumed session emits
    one result event per segment, each with only that segment's denials, so
    reading only the final event under-reports. Deduplicated by tool_use_id
    where present, in case a CLI version ever reports cumulatively. Falls back
    to a synthetic list derived from the streaming keyword counter when no
    result event carried denials (e.g. timeout before a result is emitted).
    """
    denials = list(collected) if collected else []
    if not denials and isinstance(result_obj, dict):
        final = result_obj.get("permission_denials")
        if isinstance(final, list):
            denials = list(final)
    if denials:
        seen = set()
        unique = []
        for d in denials:
            key = d.get("tool_use_id") if isinstance(d, dict) else None
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            unique.append(d)
        return unique
    if streaming_count:
        return [{"tool_name": "unknown"}] * streaming_count
    return None


def _sanitize_for_log(text: str, max_len: int = 80) -> str:
    """Strip newlines and non-printable characters from text for safe logging."""
    text = text.replace("\r", " ").replace("\n", " ")
    text = "".join(ch for ch in text if ch.isprintable())
    return text[:max_len]


def _is_permission_denial(text: str) -> bool:
    """Check if a tool_result error text indicates a permission denial."""
    lower = text.lower()
    return any(phrase in lower for phrase in (
        "permission denied", "not allowed", "disallowed",
        "not permitted", "user denied",
    ))


def _extract_progress(obj: dict, estimate: bool = False) -> str:
    """Extract a human-readable progress message from a stream-json event.
    ``estimate`` labels the result line's cost as the CLI's estimate (under a
    provider plan nobody should read it as spend)."""
    t = obj.get("type")

    if t == "user":
        msg = obj.get("message", {})
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_result" and block.get("is_error"):
                    c = block.get("content", "")
                    if isinstance(c, str):
                        text = c
                    elif isinstance(c, list):
                        text = " ".join(
                            x.get("text", "") for x in c if isinstance(x, dict))
                    else:
                        text = ""
                    if text and _is_permission_denial(text):
                        return f"PERMISSION DENIED: {_sanitize_for_log(text)}"
        return ""

    elif t == "assistant":
        # Skip foreground subagent messages to avoid duplicate progress lines
        if obj.get("parent_tool_use_id"):
            return ""
        msg = obj.get("message", {})
        for block in msg.get("content", []):
            if block.get("type") == "tool_use":
                tool = block.get("name", "")
                inp = block.get("input", {})
                if tool == "Skill":
                    return f"Invoking /{inp.get('skill', '?')}"
                elif tool == "Bash":
                    cmd = inp.get("command", "")[:60]
                    return f"Running: {cmd}"
                elif tool in ("Write", "Edit"):
                    path = inp.get("file_path", "")
                    return f"{tool}: {path.split('/')[-1] if path else '?'}"
                elif tool == "Read":
                    path = inp.get("file_path", "")
                    return f"Reading: {path.split('/')[-1] if path else '?'}"
                else:
                    return f"Tool: {tool}"
            elif block.get("type") == "text":
                text = block.get("text", "").strip()
                if text and len(text) < 100:
                    return text
    elif t == "result":
        cost = obj.get("total_cost_usd", 0)
        turns = obj.get("num_turns", 0)
        return f"Done ({turns} turns, {'est. ' if estimate else ''}${cost:.2f})"

    return ""
