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
subagent transcripts copied into ``<workspace>/subagents/``. Each configured
``outputs[*].path`` that fullsend exported under ``iteration-N/output/`` is
copied back to the same workspace-relative path for ``collect.py``.
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

from agent_eval.config import workspace_source_roots
from .base import EvalRunner, RunResult
from .stream_capture import (
    count_subagent_turns, extract_usage, message_ids_for_run,
    per_model_turns as combine_per_model_turns,
)

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
            allowed_roots=workspace_source_roots(config),
            output_paths=[o.path for o in config.outputs if o.path],
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
        allowed_roots: Optional[list] = None,
        output_paths: Optional[list] = None,
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
        # Roots a workspace symlink may resolve into (project root + plugin
        # dirs); None disables the containment check.
        self._allowed_roots = (
            None if allowed_roots is None
            else [Path(r).resolve() for r in allowed_roots])
        # Workspace-relative paths of the configured outputs[*].path, restored
        # from fullsend's exported output after the run.
        self._output_paths = [str(p) for p in (output_paths or [])]

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
        materialize_symlinks(workspace, self._allowed_roots)
        # A fresh output dir per invocation: a run that fails before fullsend
        # creates its run directory must not pick up an earlier invocation's.
        try:
            output_dir = _fresh_output_dir(workspace)
        except (OSError, ValueError) as e:
            return RunResult(exit_code=-1, stdout="", stderr=str(e), duration_s=0.0)

        task = f"/{target} {args or ''}".rstrip()
        entries = self._env_entries(task, extra_env)

        # The variables travel through the inherited process environment, which
        # fullsend reads for its ${VAR} references — no env file, so no dotenv
        # quoting to match and no secrets written to disk.
        cmd = [self._binary, "run", self._agent,
               "--fullsend-dir", self._fullsend_dir,
               "--target-repo", str(workspace)]
        if model:
            cmd += ["--model", model]
        cmd += ["--output-dir", str(output_dir), *self._extra_args]

        if self._log_prefix:
            with _print_lock:
                print(f"  {self._log_prefix} | Running: {' '.join(cmd)[:120]}",
                      flush=True)

        env = os.environ.copy()
        env.update(entries)
        result = self._run(cmd, workspace, env, timeout_s)

        if isinstance(result, RunResult):  # launch failure
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
        """Variables for the fullsend process:
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
                      f"fullsend environment", flush=True)
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
            # Exit code -1 with a "Timed out" stderr; the caller still maps
            # whatever the run directory holds (usage, stream, artifacts).
            timeout_err = "\n".join(
                s for s in (f"Timed out after {timeout_s}s", out, err) if s)
            return -1, "", timeout_err, time.monotonic() - start
        return proc.returncode, out or "", err or "", time.monotonic() - start

    # ── Run directory → RunResult ────────────────────────────────────

    def _restore_outputs(self, exported: Path, workspace: Path) -> None:
        """Copy the artifacts fullsend exported back into the eval workspace.

        fullsend leaves ``--target-repo`` untouched and exports the agent's
        outputs under ``iteration-N/output/`` keeping workspace-relative paths.
        ``collect.py`` reads each configured ``outputs[*].path`` from the
        workspace, so each such path found in the export is copied to the same
        relative location. Symlinks are never copied or followed — a source or
        destination with a symlink at any component is skipped — and paths that
        escape the workspace are ignored."""
        if not exported.is_dir() or exported.is_symlink():
            return
        for rel in self._output_paths:
            rel_path = Path(rel)
            if rel_path.is_absolute() or ".." in rel_path.parts:
                continue
            src = exported / rel_path
            dest = workspace / rel_path
            if _has_symlink_component(exported, src) or not src.exists():
                continue
            if _has_symlink_component(workspace, dest):
                continue
            if src.is_dir():
                _copy_exported_tree(src, dest)
            elif src.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)

    def _map_run_dir(self, output_dir, workspace, exit_code, stderr, duration):
        run_dir = _newest_run_dir(output_dir)
        invocation_subagents = output_dir / "subagents"
        metrics = _read_json(run_dir / "metrics.json") if run_dir else {}
        iteration = _last_iteration(run_dir) if run_dir else None

        stdout = ""
        if iteration is not None:
            stream = iteration / "output.jsonl"
            if stream.is_file() and not stream.is_symlink():
                try:
                    stdout = stream.read_text()
                except OSError:
                    stdout = ""
            # Stage this invocation's transcripts so per-model turns and message
            # ids cover only this run, then add them to the shared workspace dir.
            _copy_subagent_transcripts(
                iteration / "transcripts", invocation_subagents, workspace)
            _copy_subagent_transcripts(
                iteration / "transcripts", workspace / "subagents", workspace)
            self._restore_outputs(iteration / "output", workspace)

        (token_usage, cost_usd, num_turns, stream_ids, models_seen,
         per_model_usage, stream_ids_by_model) = extract_usage(stdout.splitlines())
        subagent_dir = invocation_subagents
        # Transcript-only subagent turns count toward num_turns too (a valid
        # metrics.json num_turns still overrides below).
        subagent_turns = count_subagent_turns(subagent_dir, already_seen=stream_ids)
        if subagent_turns:
            num_turns = (num_turns or 0) + subagent_turns
        per_model_turns = combine_per_model_turns(subagent_dir, stream_ids_by_model)
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

def materialize_symlinks(workspace: Path, allowed_roots: Optional[list] = None) -> None:
    """Replace every symlink at the workspace top level and under ``.claude/``
    with a real copy.

    fullsend uploads the target repository as a tar that preserves symlinks,
    and an absolute link target does not exist inside the sandbox. Copies
    follow nested links (resolved relative to the link's own directory), drop
    dangling ones and exclude ``.git``; a dangling top-level link is removed.
    With ``allowed_roots`` set, a link whose real target lies outside every
    root is dropped instead of copied, so host files never reach the sandbox.
    Only the workspace is modified — link targets are read, never written.
    """
    for entry in sorted(workspace.iterdir()):
        _materialize_link(entry, allowed_roots)
    claude_dir = workspace / ".claude"
    if claude_dir.is_dir():
        for entry in sorted(claude_dir.iterdir()):
            _materialize_link(entry, allowed_roots)


def _resolve_link(path: Path) -> Optional[Path]:
    try:
        return path.resolve()
    except (OSError, RuntimeError):  # unreadable or looping link
        return None


def _within_roots(path: Path, allowed_roots: Optional[list]) -> bool:
    if allowed_roots is None:
        return True
    return any(path == root or root in path.parents for root in allowed_roots)


def _materialize_link(path: Path, allowed_roots: Optional[list] = None) -> None:
    if not path.is_symlink():
        return
    target = _resolve_link(path)
    path.unlink()
    if path.name == ".git" or target is None or not target.exists():
        return
    if not _within_roots(target, allowed_roots):
        _warn_outside_roots(path)
        return
    if target.is_dir():
        _copy_tree(target, path, allowed_roots, (target,))
    else:
        shutil.copy2(target, path)


def _copy_tree(src: Path, dest: Path, allowed_roots, active: tuple) -> None:
    """Copy ``src`` into ``dest`` as real files, following symlinks.

    Each nested link resolves relative to its own parent. Dangling links,
    links leaving ``allowed_roots`` and links back into a directory already
    being copied (cycles) are skipped; ``.git`` is excluded."""
    dest.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir()):
        if entry.name == ".git":
            continue
        real = entry
        if entry.is_symlink():
            real = _resolve_link(entry)
            if real is None or not real.exists():
                continue
            if not _within_roots(real, allowed_roots):
                _warn_outside_roots(entry)
                continue
        if real.is_dir():
            if real not in active:
                _copy_tree(real, dest / entry.name, allowed_roots, active + (real,))
        elif real.is_file():
            shutil.copy2(real, dest / entry.name)


def _warn_outside_roots(path: Path) -> None:
    with _print_lock:
        print(f"  WARNING: not copying symlink {path.name!r} into the fullsend "
              f"workspace: its target is outside the project and plugin "
              f"directories", flush=True)


def _fresh_output_dir(workspace: Path) -> Path:
    """A new empty ``<workspace>/output/fullsend/run-*`` directory for one
    invocation. Refuses a base that resolves outside the workspace."""
    base = workspace / "output" / "fullsend"
    base.mkdir(parents=True, exist_ok=True)
    real_base = base.resolve()
    if workspace != real_base and workspace not in real_base.parents:
        raise ValueError(
            f"fullsend output directory {base} resolves outside the workspace")
    return Path(tempfile.mkdtemp(prefix="run-", dir=str(real_base)))


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

    kill = getattr(signal, "SIGKILL", signal.SIGTERM)
    _signal(signal.SIGTERM)
    try:
        captured = proc.communicate(timeout=_TERM_GRACE_S)
    except subprocess.TimeoutExpired:
        pass
    else:
        # The direct child exited, but a descendant in the group may have
        # ignored SIGTERM and closed its pipes — finish the group off.
        _signal(kill)
        return captured
    _signal(kill)
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
        if m and d.is_dir() and not d.is_symlink() and (best is None or int(m.group(1)) > best[0]):
            best = (int(m.group(1)), d)
    return best[1] if best else None


def _has_symlink_component(root: Path, path: Path) -> bool:
    """True when ``path`` or any ancestor between ``root`` (exclusive) and
    ``path`` is a symlink, so reading or writing it could leave ``root``."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return True
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _copy_exported_tree(src: Path, dest: Path) -> None:
    """Copy regular files under ``src`` into ``dest``, skipping symlinks on
    either side."""
    if dest.is_symlink():
        return
    dest.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir()):
        target = dest / entry.name
        if entry.is_symlink() or target.is_symlink():
            continue
        if entry.is_dir():
            _copy_exported_tree(entry, target)
        elif entry.is_file():
            shutil.copy2(entry, target)


def _copy_subagent_transcripts(src: Path, dest: Path, workspace: Path) -> None:
    """Copy subagent transcripts from ``src`` into ``dest`` (under
    ``workspace``), never following a symlink at the source directory, any
    destination component, or an existing destination file."""
    if src.is_symlink() or not src.is_dir():
        return
    if _has_symlink_component(workspace, dest):
        return
    for f in sorted(src.glob("*-agent-*.jsonl")):
        if f.is_file() and not f.is_symlink():
            target = dest / f.name
            # An existing destination must be a regular file: a symlink or a
            # directory (copy2 would write inside it, following a nested link).
            if target.is_symlink() or (target.exists() and not target.is_file()):
                continue
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)


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
