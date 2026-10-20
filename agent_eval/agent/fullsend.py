"""fullsend runner — executes the skill under test through ``fullsend run``.

The eval workspace becomes fullsend's target repository and the project's own
``.fullsend/`` agent package supplies the agent definition, so an eval runs
the skill on the same path production does: an OpenShell sandbox with its
security hooks, the agent definition's system prompt, the harness's pinned
Claude Code build and ``env.sandbox`` as the only environment contract.

The run directory fullsend writes (``<output_dir>/<sandbox>/…``) is mapped
back onto :class:`RunResult`: ``metrics.json`` for the headline numbers, the
last iteration's ``output.jsonl`` (Claude Code stream-json) as ``stdout`` so
events, traces and reasoning judges work unchanged, and the iteration's
subagent transcripts copied into ``<workspace>/subagents/``.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from .base import EvalRunner, RunResult
from .claude_code import _per_model_turns
from .stream_capture import extract_usage, message_ids_for_run

_print_lock = threading.Lock()

# Seconds between SIGTERM and SIGKILL when the harness timeout expires — fullsend
# tears its sandbox down on SIGTERM, so it gets a moment before the hard kill.
_TERM_GRACE_S = 10

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ITERATION_RE = re.compile(r"^iteration-(\d+)$")
_BLOCK_VALUES = {"block", "blocked", "deny", "denied"}


class FullsendRunner(EvalRunner):
    """Runs a skill by invoking ``fullsend run <agent>`` on the workspace."""

    @classmethod
    def from_config(cls, config, *, log_prefix=None, **overrides):
        runner = config.runner
        fullsend_dir = Path(runner.fullsend_dir or ".fullsend")
        if not fullsend_dir.is_absolute():
            fullsend_dir = config.project_root / fullsend_dir
        return cls(
            agent=runner.agent,
            fullsend_dir=str(fullsend_dir.resolve()),
            binary=runner.binary or "fullsend",
            extra_args=runner.extra_args,
            runner_env=runner.env,
            execution_env=config.execution.env,
            subagent_model=overrides.get("subagent_model"),
            effort=overrides.get("effort", runner.effort),
            log_prefix=log_prefix,
        )

    def __init__(
        self,
        agent: Optional[str],
        fullsend_dir: str = ".fullsend",
        binary: str = "fullsend",
        extra_args: Optional[list] = None,
        runner_env: Optional[dict] = None,
        execution_env: Optional[dict] = None,
        subagent_model: Optional[str] = None,
        effort: Optional[str] = None,
        log_prefix: Optional[str] = None,
        **_kwargs,
    ):
        if not agent or not isinstance(agent, str):
            raise ValueError(
                "fullsend runner requires runner.agent: an agent name registered "
                "in <fullsend_dir>/config.yaml")
        self._agent = agent
        self._fullsend_dir = str(Path(fullsend_dir).resolve())
        self._binary = binary or "fullsend"
        self._extra_args = [str(a) for a in (extra_args or [])]
        self._runner_env = runner_env or {}
        self._execution_env = execution_env or {}
        self._subagent_model = subagent_model or ""
        self._effort = effort or ""
        self._log_prefix = log_prefix

    @property
    def name(self) -> str:
        return "fullsend"

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
        # system_prompt is ignored (the agent definition replaces the system
        # prompt) and max_budget_usd is a hint only (the fullsend harness has
        # its own budget).
        if not target:
            raise ValueError(
                "fullsend runner does not support prompt mode (execution.prompt); "
                "it runs a skill as FULLSEND_TASK='/<skill> <args>' — set "
                "execution.skill")

        workspace = workspace.resolve()
        materialize_symlinks(workspace)
        output_dir = workspace / "output" / "fullsend"
        output_dir.mkdir(parents=True, exist_ok=True)

        task = f"/{target} {args or ''}".rstrip()
        entries = self._env_entries(task, extra_env)

        # The env file lives outside the workspace: fullsend uploads the
        # target repository into the sandbox, and the file may carry secrets.
        fd, env_file = tempfile.mkstemp(prefix="agent-eval-fullsend-", suffix=".env")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(format_env_file(entries))

            cmd = [self._binary, "run", self._agent,
                   "--fullsend-dir", self._fullsend_dir,
                   "--target-repo", str(workspace)]
            if model:
                cmd += ["--model", model]
            cmd += ["--env-file", env_file,
                    "--output-dir", str(output_dir),
                    *self._extra_args]

            if self._log_prefix:
                with _print_lock:
                    print(f"  {self._log_prefix} | Running: {' '.join(cmd)[:120]}",
                          flush=True)

            env = os.environ.copy()
            env.update(entries)
            result = self._run(cmd, workspace, env, timeout_s)
        finally:
            try:
                os.unlink(env_file)
            except OSError:
                pass

        if isinstance(result, RunResult):  # timeout or launch failure
            return result
        returncode, console_out, console_err, duration = result

        if self._log_prefix:
            status = "OK" if returncode == 0 else f"FAIL (exit {returncode})"
            with _print_lock:
                print(f"  {self._log_prefix} | {status} ({duration:.0f}s)", flush=True)

        stderr = "\n".join(s for s in (console_out, console_err) if s)
        return self._map_run_dir(output_dir, workspace, returncode, stderr, duration)

    # ── Environment ──────────────────────────────────────────────────

    def _env_entries(self, task: str, extra_env: Optional[dict]) -> dict:
        """Variables for both the env file and the fullsend process:
        execution.env, runner.env, hook env (extra_env), then the harness's
        own keys, which always win."""
        entries = {}
        for source in (self._execution_env, self._runner_env):
            for k, v in source.items():
                if isinstance(v, str) and v.startswith("$"):
                    resolved = os.environ.get(v[1:])
                    if resolved is not None:
                        entries[k] = resolved
                elif v is not None:
                    entries[k] = str(v)
        for k, v in (extra_env or {}).items():
            entries[k] = str(v)
        entries["FULLSEND_TASK"] = task
        entries["AGENT_EVAL_SUBAGENT_MODEL"] = self._subagent_model
        entries["AGENT_EVAL_EFFORT"] = self._effort
        invalid = [k for k in entries if not _ENV_KEY_RE.match(str(k))]
        for k in invalid:
            with _print_lock:
                print(f"  WARNING: dropping invalid env var name {k!r} from the "
                      f"fullsend env file", flush=True)
            del entries[k]
        return entries

    # ── Process ──────────────────────────────────────────────────────

    def _run(self, cmd, workspace, env, timeout_s):
        start = time.monotonic()
        popen_kwargs = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(workspace),
            env=env,
        )
        if sys.platform != "win32":
            popen_kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except (OSError, subprocess.SubprocessError) as e:
            return RunResult(exit_code=-1, stdout="", stderr=str(e),
                             duration_s=time.monotonic() - start)
        try:
            out, err = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            out, err = _terminate(proc)
            return RunResult(
                exit_code=-1,
                stdout="",
                stderr="\n".join(s for s in (f"Timed out after {timeout_s}s", out, err) if s),
                duration_s=time.monotonic() - start,
            )
        return proc.returncode, out or "", err or "", time.monotonic() - start

    # ── Run directory → RunResult ────────────────────────────────────

    def _map_run_dir(self, output_dir, workspace, exit_code, stderr, duration):
        run_dir = _newest_run_dir(output_dir)
        metrics = _read_json(run_dir / "metrics.json") if run_dir else {}
        iteration = _last_iteration(run_dir) if run_dir else None

        stdout = ""
        if iteration is not None:
            stream = iteration / "output.jsonl"
            if stream.is_file():
                try:
                    stdout = stream.read_text()
                except OSError:
                    stdout = ""
            _copy_subagent_transcripts(iteration / "transcripts", workspace / "subagents")

        (token_usage, cost_usd, num_turns, stream_ids, models_seen,
         per_model_usage, stream_ids_by_model) = extract_usage(stdout.splitlines())
        subagent_dir = workspace / "subagents"
        per_model_turns = _per_model_turns(subagent_dir, stream_ids_by_model)
        message_ids = message_ids_for_run(stream_ids, subagent_dir)

        resolved_model = None
        raw_output = {"run_dir": str(run_dir) if run_dir else None}
        if metrics:
            if _is_number(metrics.get("total_cost_usd")):
                cost_usd = metrics["total_cost_usd"]
            if isinstance(metrics.get("num_turns"), int):
                num_turns = metrics["num_turns"]
            if isinstance(metrics.get("model"), str) and metrics["model"]:
                resolved_model = metrics["model"]
            tu = metrics.get("token_usage")
            if isinstance(tu, dict):
                token_usage = {"input": tu.get("input", 0), "output": tu.get("output", 0)}
                raw_output["token_usage"] = tu
            for key in ("tool_calls", "iterations"):
                if key in metrics:
                    raw_output[key] = metrics[key]
        if run_dir is not None:
            raw_output["security_files"] = _count_files(run_dir / "security")

        denials = _telemetry_denials(run_dir / "run-telemetry.jsonl") if run_dir else []

        return RunResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_s=duration,
            token_usage=token_usage,
            cost_usd=cost_usd,
            num_turns=num_turns,
            resolved_model=resolved_model,
            models_used=sorted(models_seen) if models_seen else None,
            per_model_usage=per_model_usage,
            per_model_turns=per_model_turns,
            permission_denials=denials,
            raw_output=raw_output,
            message_ids=message_ids,
        )


# ── Helpers ──────────────────────────────────────────────────────────

def materialize_symlinks(workspace: Path) -> None:
    """Replace every symlink at the workspace top level and under ``.claude/``
    with a real copy.

    fullsend uploads the target repository as a tar that preserves symlinks,
    and an absolute link target does not exist inside the sandbox. Copies
    follow nested links, drop dangling ones and exclude ``.git``; a dangling
    top-level link is removed. Only the workspace is modified — link targets
    (the project root) are read, never written.
    """
    for entry in sorted(workspace.iterdir()):
        _materialize_link(entry)
    claude_dir = workspace / ".claude"
    if claude_dir.is_dir():
        for entry in sorted(claude_dir.iterdir()):
            _materialize_link(entry)


def _materialize_link(path: Path) -> None:
    if not path.is_symlink():
        return
    target = path.resolve()
    path.unlink()
    if path.name == ".git" or not target.exists():
        return
    if target.is_dir():
        shutil.copytree(target, path, symlinks=False,
                        ignore=shutil.ignore_patterns(".git"),
                        ignore_dangling_symlinks=True)
    else:
        shutil.copy2(target, path)


def format_env_file(entries: dict) -> str:
    """Render ``KEY="value"`` dotenv lines. Values are double-quoted with
    backslash, double quote, ``$`` and newlines escaped, so arguments with
    spaces, quotes or line breaks survive and nothing is expanded."""
    lines = []
    for k, v in entries.items():
        escaped = (str(v).replace("\\", "\\\\").replace('"', '\\"')
                   .replace("$", "\\$").replace("\r", "\\r").replace("\n", "\\n"))
        lines.append(f'{k}="{escaped}"')
    return "\n".join(lines) + "\n"


def _terminate(proc):
    """Terminate the process group: SIGTERM, a grace period, then SIGKILL."""
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

    _signal(signal.SIGTERM)
    try:
        return proc.communicate(timeout=_TERM_GRACE_S)
    except subprocess.TimeoutExpired:
        pass
    _signal(getattr(signal, "SIGKILL", signal.SIGTERM))
    try:
        return proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        return "", "Process group termination hung"


def _newest_run_dir(output_dir: Path) -> Optional[Path]:
    if not output_dir.is_dir():
        return None
    dirs = [d for d in output_dir.iterdir() if d.is_dir() and not d.is_symlink()]
    if not dirs:
        return None
    return max(dirs, key=lambda d: d.stat().st_mtime)


def _last_iteration(run_dir: Path) -> Optional[Path]:
    best = None
    for d in run_dir.iterdir():
        m = _ITERATION_RE.match(d.name)
        if m and d.is_dir() and (best is None or int(m.group(1)) > best[0]):
            best = (int(m.group(1)), d)
    return best[1] if best else None


def _copy_subagent_transcripts(src: Path, dest: Path) -> None:
    if not src.is_dir():
        return
    for f in sorted(src.glob("*-agent-*.jsonl")):
        if f.is_file() and not f.is_symlink():
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest / f.name)


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        with _print_lock:
            print(f"  WARNING: failed to parse {path}: {exc}", flush=True)
        return {}
    return data if isinstance(data, dict) else {}


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _count_files(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(1 for f in path.rglob("*") if f.is_file())


def _telemetry_denials(path: Path) -> list:
    """Hook block events from ``run-telemetry.jsonl`` (best effort).

    A line counts as a block when a ``decision``/``action``/``outcome``/
    ``result`` field (top level or under ``attributes``) is block/deny, or a
    ``blocked`` field is true. Returns ``[]`` when the file is absent."""
    if not path.is_file():
        return []
    denials = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        attrs = obj.get("attributes") if isinstance(obj.get("attributes"), dict) else {}
        fields = {**attrs, **obj}
        blocked = fields.get("blocked") is True or any(
            isinstance(fields.get(k), str) and fields[k].lower() in _BLOCK_VALUES
            for k in ("decision", "action", "outcome", "result"))
        if not blocked:
            continue
        denial = {
            "tool_name": (fields.get("tool_name") or fields.get("gen_ai.tool.name")
                          or fields.get("tool") or ""),
            "tool_use_id": (fields.get("tool_use_id") or fields.get("gen_ai.tool.call.id")
                            or ""),
            "tool_input": fields.get("tool_input") or {},
        }
        reason = fields.get("reason") or fields.get("detail")
        if reason:
            denial["reason"] = reason
        denials.append(denial)
    return denials
