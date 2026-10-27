"""Abstract runner interface for agent evaluation.

Each runner implementation translates the generic execute() call into
a platform-specific invocation (Claude Code CLI, Agent SDK, OpenCode, etc.).
The eval harness only interacts with runners through this interface.
"""

import os
import signal
import subprocess
import sys
import time
import warnings
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class RunResult:
    """Result of a single skill invocation."""
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    token_usage: Optional[dict] = None  # {"input": N, "output": N}
    cost_usd: Optional[float] = None
    num_turns: Optional[int] = None
    resolved_model: Optional[str] = None  # Full model ID from runtime
    models_used: Optional[list] = None   # All distinct models observed
    per_model_usage: Optional[dict] = None  # Per-model token/cost breakdown
    per_model_turns: Optional[dict] = None  # Per-model assistant turn count
    permission_denials: Optional[list] = None  # [{tool_name, tool_use_id, tool_input}]
    raw_output: Optional[dict] = None  # Runner-specific parsed output
    # Cost provenance (spec 014). `message_ids` is the cost-truth key set: every
    # assistant message id in the root stream and the subagent transcripts —
    # on a direct OpenRouter connection these are `gen-…` generation ids that
    # the backfill prices. The other fields are filled by reconcile (or by a
    # runner that knows its own number is an estimate); None = not applicable.
    message_ids: Optional[list] = None
    cost_source: Optional[str] = None
    cost_usd_estimate: Optional[float] = None
    providers: Optional[dict] = None
    error_class: Optional[str] = None
    budget: Optional[dict] = None


class EvalRunner(ABC):
    """Abstract runner -- one implementation per agent platform."""

    # ``eval_params`` keys in run_result.json that this runner's
    # ``provenance()`` records and that are logged as MLflow params; empty for
    # runners that record no run-level provenance.
    PROVENANCE_KEYS: tuple = ()

    @classmethod
    @abstractmethod
    def from_config(cls, config, *, log_prefix=None, **overrides):
        """Construct a runner from an EvalConfig.

        Each runner subclass extracts the config fields it needs.
        Overrides (e.g. resolved models, experiments) take precedence.

        Args:
            config: EvalConfig instance.
            log_prefix: Progress logging prefix (e.g. "eval", "eval:case-01").
            **overrides: Runner-specific overrides from CLI flags.
        """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this runner (e.g. 'claude-code', 'agent-sdk')."""

    def bind_provider(self, binding) -> None:
        """Attach the provider-session binding for the next ``execute()``
        (spec 014): a runner routed through a provider plan sights generation
        ids through it. Default: no provider integration."""
        return None

    @abstractmethod
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
        """Execute a skill or prompt in an isolated workspace.

        Args:
            target: Skill name (e.g. "rfe.review") for case/batch mode,
                or None for prompt mode (direct execution without skill wrapper).
            args: Skill arguments (e.g. "RHAIRFE-1109") for case/batch mode,
                or user prompt text for prompt mode.
            workspace: Pre-staged workspace directory.
            model: Model identifier (e.g. "opus", "sonnet").
            settings_path: Path to eval-specific settings file.
            system_prompt: Optional system prompt (appended).
                Each runner translates this to its platform's API
                (e.g. --append-system-prompt for Claude Code).
            max_budget_usd: Maximum API spend for this invocation.
            timeout_s: Timeout in seconds.
            extra_env: Additional env vars to inject (e.g. from hook outputs).
                Merged after execution.env, so hook env overrides static config.

        Returns:
            RunResult with exit code, output, timing, and optional usage stats.
        """

    def run_skill(
        self,
        skill_name: str,
        args: str,
        workspace: Path,
        model: str,
        settings_path: Optional[Path] = None,
        system_prompt: Optional[str] = None,
        max_budget_usd: float = 5.0,
        timeout_s: int = 600,
        extra_env: Optional[dict] = None,
    ) -> RunResult:
        """Deprecated: Use execute() instead.

        This method is provided for backward compatibility with existing code.
        It calls execute(target=skill_name, ...).
        """
        warnings.warn(
            "run_skill() is deprecated, use execute() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.execute(
            target=skill_name,
            args=args,
            workspace=workspace,
            model=model,
            settings_path=settings_path,
            system_prompt=system_prompt,
            max_budget_usd=max_budget_usd,
            timeout_s=timeout_s,
            extra_env=extra_env,
        )


def terminate_process_group(proc: subprocess.Popen, grace_s: float = 0.0,
                            final_wait_s: float = 5.0) -> tuple[str, str]:
    """Stop ``proc`` and its process group, then reap it; returns the captured
    ``(stdout, stderr)``.

    With ``grace_s`` > 0 the group first gets SIGTERM and that long to exit; the
    SIGKILL that finishes the group (a descendant may ignore SIGTERM and keep
    the pipes open) is sent while the leader is still unreaped, so the pgid
    cannot have been recycled. ``grace_s`` 0 sends SIGKILL at once."""
    def _signal(sig):
        if sys.platform != "win32":
            try:
                os.killpg(proc.pid, sig)
                return
            except ProcessLookupError:
                return
            except OSError:
                pass  # group signalling refused — signal the process itself
        try:
            if sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
        except OSError:
            pass

    kill = getattr(signal, "SIGKILL", signal.SIGTERM)
    if grace_s > 0:
        _signal(signal.SIGTERM)
        _wait_unreaped(proc, grace_s)
    # Still unreaped here (communicate/wait below does the reaping).
    _signal(kill)
    try:
        return proc.communicate(timeout=final_wait_s)
    except subprocess.TimeoutExpired:
        return "", "Process group termination hung"


def _wait_unreaped(proc: subprocess.Popen, timeout_s: float) -> None:
    """Wait up to ``timeout_s`` for the leader to exit WITHOUT reaping it, so
    its pid (the process-group id) stays reserved until the caller has
    finished signalling the group."""
    deadline = time.monotonic() + timeout_s
    waitid = getattr(os, "waitid", None)
    while time.monotonic() < deadline:
        if waitid is not None:
            try:
                if waitid(os.P_PID, proc.pid,
                          os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                    return
            except ChildProcessError:
                return
        elif proc.poll() is not None:  # no WNOWAIT: reaped, best effort
            return
        time.sleep(0.05)
