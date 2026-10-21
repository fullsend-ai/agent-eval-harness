"""Opaque CLI runner — delegates execution to an arbitrary command."""

import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Union

import yaml

from agent_eval.config import resolve_env_value

from .base import EvalRunner, RunResult

_print_lock = threading.Lock()


class CliRunner(EvalRunner):
    """Runs skills by executing a user-provided command template.

    The command template can use placeholders that are resolved at runtime:
    - {agent}          — skill name or agent definition path
    - {workspace}      — workspace directory path (absolute)
    - {output_dir}     — output directory for artifacts (absolute, {workspace}/output)
    - {model}          — model identifier (from --model or models.skill)
    - {subagent_model} — subagent model (from models.subagent; empty if unset)
    - {timeout}        — timeout in seconds
    - {max_budget_usd} — budget cap (advisory — not enforced by harness)
    - {effort}         — reasoning effort level (from runner.effort; empty if unset)
    - {system_prompt}  — system prompt text (from runner.system_prompt; empty if unset)
    - {config_dir}     — directory containing the eval.yaml (absolute); lets the
                         command reference eval-relative helper files (scripts, mcp configs)
    - {args}           — resolved skill arguments string
    - {field}          — any field from the case's input.yaml

    The command can be a string (shell-parsed) or a list of arguments.
    See docs/opaque-cli-runner-contract.md for the full contract.
    """

    @classmethod
    def from_config(cls, config, *, log_prefix=None, **overrides):
        return cls(
            command=config.runner.command,
            env=config.execution.env,
            log_prefix=log_prefix,
            subagent_model=overrides.get("subagent_model"),
            effort=overrides.get("effort", config.runner.effort),
            system_prompt=config.runner.system_prompt,
            config_dir=str(config.resolve_path(".")),
        )

    def __init__(
        self,
        command: Union[str, list],
        env: Optional[dict] = None,
        log_prefix: Optional[str] = None,
        subagent_model: Optional[str] = None,
        effort: Optional[str] = None,
        system_prompt: Optional[str] = None,
        config_dir: Optional[str] = None,
        **_kwargs,
    ):
        if not command:
            raise ValueError(
                "CLI runner requires a 'command' in the runner config. "
                "Example: runner:\n  type: cli\n  command: \"my-runner run {agent}\"")
        if not isinstance(command, (str, list)):
            raise TypeError(
                f"runner.command must be a string or list, got {type(command).__name__}")
        self._command = command
        self._extra_env = env or {}
        self._log_prefix = log_prefix
        self._subagent_model = subagent_model or ""
        self._effort = effort or ""
        self._system_prompt = system_prompt or ""
        self._config_dir = config_dir or ""

    @property
    def name(self) -> str:
        return "cli"

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
        workspace = workspace.resolve()
        output_dir = workspace / "output"
        output_dir.mkdir(parents=True, exist_ok=True)

        # Build placeholder values (all paths absolute to avoid cwd issues)
        placeholders = {
            "agent": target or "",  # skill name for case/batch, empty for prompt mode
            "workspace": str(workspace),
            "output_dir": str(output_dir),
            "model": model or "",
            "timeout": str(timeout_s),
            "max_budget_usd": str(max_budget_usd),
            "args": args or "",
            "subagent_model": self._subagent_model,
            "effort": self._effort,
            "system_prompt": system_prompt or self._system_prompt,
            "config_dir": self._config_dir,
        }

        # Load input.yaml fields if present
        input_path = workspace / "input.yaml"
        if input_path.exists():
            try:
                input_data = yaml.safe_load(input_path.read_text()) or {}
                if isinstance(input_data, dict):
                    for k, v in input_data.items():
                        if k not in placeholders:
                            placeholders[k] = str(v) if v is not None else ""
            except (yaml.YAMLError, OSError, ValueError) as exc:
                with _print_lock:
                    print(f"  WARNING: failed to parse {input_path}: {exc}",
                          flush=True)

        cmd = self._resolve_command(placeholders)

        # Always use list form to avoid shell=True (CWE-78)
        if isinstance(cmd, str):
            cmd = shlex.split(cmd)

        if self._log_prefix:
            cmd_str = " ".join(cmd)
            with _print_lock:
                print(f"  {self._log_prefix} | Running: {cmd_str[:120]}", flush=True)

        env = self._build_env(extra_env=extra_env)
        start = time.monotonic()

        try:
            popen_kwargs = dict(
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(workspace),
                env=env,
            )
            if sys.platform != "win32":
                popen_kwargs["start_new_session"] = True
            proc = subprocess.Popen(cmd, **popen_kwargs)
            try:
                stdout, stderr = proc.communicate(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                # Kill the entire process group so child processes don't linger
                if sys.platform != "win32":
                    import signal
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
                try:
                    stdout, stderr = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    stdout, stderr = "", "Process group termination hung"
                duration = time.monotonic() - start
                return RunResult(
                    exit_code=-1,
                    stdout=stdout or "",
                    stderr=f"Timed out after {timeout_s}s",
                    duration_s=duration,
                )
            duration = time.monotonic() - start
        except (OSError, subprocess.SubprocessError) as e:
            duration = time.monotonic() - start
            return RunResult(
                exit_code=-1,
                stdout="",
                stderr=str(e),
                duration_s=duration,
            )

        if self._log_prefix:
            status = "OK" if proc.returncode == 0 else f"FAIL (exit {proc.returncode})"

            with _print_lock:
                print(f"  {self._log_prefix} | {status} ({duration:.0f}s)", flush=True)

        # Parse optional metrics.json for token/cost data
        metrics = self._parse_metrics(output_dir)

        return RunResult(
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_s=duration,
            token_usage=metrics.get("token_usage"),
            cost_usd=metrics.get("cost_usd"),
            num_turns=metrics.get("num_turns"),
            resolved_model=metrics.get("model"),
            models_used=metrics.get("models_used"),
            per_model_usage=metrics.get("per_model_usage"),
            per_model_turns=metrics.get("per_model_turns"),
        )

    def _resolve_command(self, placeholders: dict):
        """Resolve placeholders in the command template."""
        # String commands are shlex.split() after resolution, so quote
        # placeholder values to preserve tokens with spaces/special chars.
        # List commands don't need quoting — each element is already a token.
        needs_quoting = isinstance(self._command, str)

        def _sub(template: str) -> str:
            def _replace(m):
                key = m.group(1)
                if key in placeholders:
                    value = placeholders[key]
                    return shlex.quote(value) if needs_quoting else value
                return m.group(0)  # leave unresolved placeholders as-is
            return re.sub(r'\{([\w-]+)\}', _replace, template)

        if isinstance(self._command, list):
            return [_sub(part) for part in self._command]
        return _sub(self._command)

    def _build_env(self, extra_env=None) -> dict:
        """Build subprocess environment: inherit env, add extras."""
        env = os.environ.copy()
        for k, v in self._extra_env.items():
            resolved = resolve_env_value(v)
            if resolved is not None:
                env[k] = resolved
        if extra_env:
            for k, v in extra_env.items():
                env[k] = str(v)
        return env

    @staticmethod
    def _parse_metrics(output_dir: Path) -> dict:
        """Parse optional metrics.json from the output directory.

        Expected format:
        {
            "token_usage": {"input": N, "output": N},
            "cost_usd": 0.05,
            "num_turns": 3,
            "model": "gpt-4",
            "models_used": ["gpt-4", "gpt-3.5-turbo"],
            "per_model_usage": {"gpt-4": {"input": N, "output": N, "cost_usd": 0.04}},
            "per_model_turns": {"gpt-4": 2, "gpt-3.5-turbo": 1}
        }

        All fields are optional. Returns empty dict if file doesn't exist
        or can't be parsed.
        """
        metrics_path = output_dir / "metrics.json"
        if not metrics_path.exists():
            return {}
        try:
            with open(metrics_path) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            return data
        except (json.JSONDecodeError, OSError) as exc:
            with _print_lock:
                print(f"  WARNING: failed to parse {metrics_path}: {exc}",
                      flush=True)
            return {}
