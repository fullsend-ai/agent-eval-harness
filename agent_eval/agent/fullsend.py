"""fullsend runner — executes the skill under test through ``fullsend run``.

The eval workspace becomes fullsend's target repository and the project's own
``.fullsend/`` agent package supplies the agent definition, so an eval runs
the skill on the same path production does: an OpenShell sandbox with its
security hooks, the agent definition's system prompt, the harness's pinned
Claude Code build and ``env.sandbox`` as the only environment contract.

Artifacts reach the workspace the way they do in production: the project's
post-script (``fullsend run`` runs it after the agent) copies the agent's
outputs into ``--target-repo``, which is the eval workspace, where
``collect.py`` reads each configured ``outputs[*].path``. A run whose
post-script did not execute (``--no-post-script``) therefore produces no
artifacts and is reported as a failed case.

The run directory fullsend writes (``<output_dir>/<sandbox>/…``) is mapped
back onto :class:`RunResult`: ``metrics.json`` for the headline numbers, the
concatenated ``output.jsonl`` streams of every iteration (Claude Code
stream-json) as ``stdout`` so events, traces and reasoning judges work
unchanged, and every iteration's subagent transcripts copied into
``<workspace>/subagents/``. ``agent-result.json`` and the other files under
``iteration-N/output/`` are fullsend's own export and are not read.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from agent_eval.config import workspace_source_roots
from .base import EvalRunner, RunResult, resolve_env_value, terminate_process_group
from .stream_capture import (
    count_subagent_turns, extract_usage, message_ids_for_run,
    per_model_turns as combine_per_model_turns,
)

_print_lock = threading.Lock()

# Seconds between SIGTERM and SIGKILL when the harness timeout expires. fullsend
# tears its OpenShell sandbox down on SIGTERM (a podman sandbox takes several
# seconds to stop), so the default outlasts that; ``runner.term_grace_s``
# overrides it.
_DEFAULT_TERM_GRACE_S = 30

# Top-level workspace directory holding fullsend's run output. fullsend leaves
# the ``--output-dir`` tree out of the target-repo upload only when it is a
# top-level child of the target, so a run never uploads an earlier run's files.
RUN_BASE_DIRNAME = ".fullsend-runs"

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ITERATION_RE = re.compile(r"^iteration-(\d+)$")
_BLOCK_VALUES = {"block", "blocked", "deny", "denied"}
_SUBAGENT_MODEL_VAR = "AGENT_EVAL_SUBAGENT_MODEL"


class FullsendRunner(EvalRunner):
    """Runs a skill by invoking ``fullsend run <agent>`` on the workspace."""

    @classmethod
    def from_config(cls, config, *, log_prefix=None, **overrides):
        runner = config.runner
        fullsend_dir = Path(runner.fullsend_dir or ".fullsend")
        if not fullsend_dir.is_absolute():
            fullsend_dir = config.project_root / fullsend_dir
        binary = runner.binary or "fullsend"
        # A relative binary containing a path separator is a project path (like
        # fullsend_dir); a bare name is looked up on PATH.
        if not Path(binary).is_absolute() and (
                "/" in binary or (os.sep != "/" and os.sep in binary)):
            binary = str((config.project_root / binary).resolve())
        return cls(
            agent=runner.agent,
            fullsend_dir=str(fullsend_dir.resolve()),
            binary=binary,
            extra_args=runner.extra_args,
            runner_env=runner.env,
            execution_env=config.execution.env,
            subagent_model=overrides.get("subagent_model"),
            effort=overrides.get("effort", runner.effort),
            term_grace_s=getattr(runner, "term_grace_s", None),
            log_prefix=log_prefix,
            allowed_roots=workspace_source_roots(config),
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
        term_grace_s: Optional[float] = None,
        log_prefix: Optional[str] = None,
        allowed_roots: Optional[list] = None,
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
        self._term_grace_s = (
            float(term_grace_s) if term_grace_s else float(_DEFAULT_TERM_GRACE_S))
        self._log_prefix = log_prefix
        # Roots a workspace symlink may resolve into (project root + plugin
        # dirs); None disables the containment check.
        self._allowed_roots = (
            None if allowed_roots is None
            else [Path(r).resolve() for r in allowed_roots])

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

        if "--no-post-script" in self._extra_args:
            return RunResult(
                exit_code=-1, stdout="", duration_s=0.0,
                stderr=("fullsend runner: runner.extra_args contains "
                        "--no-post-script, so the project's post-script (which "
                        "copies the agent's outputs into the workspace) would not "
                        "run and no artifacts could be collected; remove it"))

        workspace = workspace.resolve()
        try:
            materialize_symlinks(workspace, self._allowed_roots)
            _sanitize_settings(workspace)
            output_dir = _run_base(workspace)
        except (OSError, ValueError, shutil.Error) as e:
            return RunResult(exit_code=-1, stdout="", stderr=str(e), duration_s=0.0)
        # fullsend names each run directory itself; remember what is already
        # there so a run that fails before creating one maps nothing.
        known = {d.name for d in output_dir.iterdir()}

        self._warn_subagent_model_unreferenced()

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
        if self._effort:
            cmd += ["--effort", self._effort]
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
        return self._map_run_dir(
            output_dir, workspace, returncode, stderr, duration, known=known)

    # ── Environment ──────────────────────────────────────────────────

    def _warn_subagent_model_unreferenced(self) -> None:
        """models.subagent only reaches the agent through the
        AGENT_EVAL_SUBAGENT_MODEL variable; warn when nothing under the
        .fullsend directory references it."""
        if not self._subagent_model:
            return
        root = Path(self._fullsend_dir)
        try:
            for f in root.rglob("*"):
                if f.is_file() and not f.is_symlink() and f.stat().st_size < 1_000_000:
                    try:
                        if _SUBAGENT_MODEL_VAR in f.read_text(errors="ignore"):
                            return
                    except OSError:
                        continue
        except OSError:
            pass
        with _print_lock:
            print(f"  WARNING: models.subagent is set but nothing under "
                  f"{self._fullsend_dir} references ${_SUBAGENT_MODEL_VAR}; the "
                  f"subagent model will not take effect in the fullsend sandbox "
                  f"(reference it from the agent definition, e.g. as "
                  f"CLAUDE_CODE_SUBAGENT_MODEL)", flush=True)

    def _env_entries(self, task: str, extra_env: Optional[dict]) -> dict:
        """Variables for the fullsend process:
        execution.env, runner.env, hook env (extra_env), then the harness's
        own keys, which always win."""
        entries = {}
        for source in (self._execution_env, self._runner_env):
            for k, v in source.items():
                resolved = resolve_env_value(v)
                if resolved is not None:
                    entries[k] = resolved
        for k, v in (extra_env or {}).items():
            entries[k] = str(v)
        entries["FULLSEND_TASK"] = task
        if self._subagent_model:
            entries[_SUBAGENT_MODEL_VAR] = self._subagent_model
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
            # SIGTERM lets fullsend tear its sandbox down; the group is killed
            # only while the leader is still unreaped (see base.py).
            out, err = terminate_process_group(proc, self._term_grace_s)
            # Exit code -1 with a "Timed out" stderr; the caller still maps
            # whatever the run directory holds (usage, stream, artifacts).
            timeout_err = "\n".join(
                s for s in (f"Timed out after {timeout_s}s", out, err) if s)
            return -1, "", timeout_err, time.monotonic() - start
        return proc.returncode, out or "", err or "", time.monotonic() - start

    # ── Run directory → RunResult ────────────────────────────────────

    def _map_run_dir(self, output_dir, workspace, exit_code, stderr, duration,
                     known=frozenset()):
        run_dir = _newest_run_dir(output_dir, known)
        metrics = _read_json(run_dir / "metrics.json") if run_dir else {}
        iterations = _iterations(run_dir) if run_dir else []

        streams = []
        with tempfile.TemporaryDirectory(prefix="fullsend-subagents-") as staging:
            # Stage this invocation's transcripts (every iteration) so per-model
            # turns and message ids cover only this run, then add them to the
            # shared workspace dir.
            invocation_subagents = Path(staging)
            for number, iteration in iterations:
                stream = iteration / "output.jsonl"
                if stream.is_file() and not stream.is_symlink():
                    try:
                        text = stream.read_text()
                    except OSError:
                        text = ""
                    if text:
                        streams.append(text if text.endswith("\n") else text + "\n")
                _copy_subagent_transcripts(
                    iteration / "transcripts", invocation_subagents, invocation_subagents,
                    conflict_prefix=f"it{number}-")
            _copy_subagent_transcripts(
                invocation_subagents, workspace / "subagents", workspace)
            stdout = "".join(streams)

            (token_usage, cost_usd, num_turns, stream_ids, models_seen,
             per_model_usage, stream_ids_by_model) = extract_usage(stdout.splitlines())
            # Transcript-only subagent turns count toward num_turns too (a valid
            # metrics.json num_turns still overrides below).
            subagent_turns = count_subagent_turns(
                invocation_subagents, already_seen=stream_ids)
            if subagent_turns:
                num_turns = (num_turns or 0) + subagent_turns
            per_model_turns = combine_per_model_turns(
                invocation_subagents, stream_ids_by_model)
            message_ids = message_ids_for_run(stream_ids, invocation_subagents)

        resolved_model = None
        raw_output = {"run_dir": str(run_dir) if run_dir else None}
        if metrics:
            if _is_number(metrics.get("total_cost_usd")):
                cost_usd = metrics["total_cost_usd"]
            if isinstance(metrics.get("num_turns"), int):
                num_turns = metrics["num_turns"]
            if isinstance(metrics.get("model"), str) and metrics["model"]:
                resolved_model = metrics["model"]
            usage = metrics.get("token_usage")
            if isinstance(usage, dict):
                token_usage = {
                    "input": usage.get("input", 0),
                    "output": usage.get("output", 0),
                    "cache_read": usage.get("cache_read", 0),
                    "cache_create": usage.get(
                        "cache_creation", usage.get("cache_create", 0)),
                }
                raw_output["token_usage"] = usage
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

def _sanitize_settings(workspace: Path) -> None:
    """Drop the host-only parts of the workspace ``.claude/settings.json``.

    The harness writes ``env`` (execution.env, already passed to fullsend through
    its own environment), its hook commands (host paths that do not exist in the
    sandbox) and ``permissions.additionalDirectories`` (host directories) for
    the other runners. fullsend uploads the settings file with the repository,
    and the agent definition owns the sandbox's environment and hooks, so those
    keys are removed here."""
    path = workspace / ".claude" / "settings.json"
    if _has_symlink_component(workspace, path) or not path.is_file():
        return
    try:
        settings = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        with _print_lock:
            print(f"  WARNING: not sanitising {path}: {exc}", flush=True)
        return
    if not isinstance(settings, dict):
        return
    changed = False
    for key in ("env", "hooks"):
        if key in settings:
            del settings[key]
            changed = True
    perms = settings.get("permissions")
    if isinstance(perms, dict) and "additionalDirectories" in perms:
        del perms["additionalDirectories"]
        changed = True
    if changed:
        path.write_text(json.dumps(settings, indent=2))


def materialize_symlinks(workspace: Path, allowed_roots: Optional[list] = None) -> None:
    """Replace every symlink at the workspace top level and under ``.claude/``
    with a real copy.

    fullsend uploads the target repository as a tar that preserves symlinks,
    and an absolute link target does not exist inside the sandbox. Copies
    follow nested links (resolved relative to the link's own directory), drop
    dangling ones and exclude ``.git``; a dangling top-level link is removed.
    With ``allowed_roots`` set, a link whose real target lies outside every
    root is dropped instead of copied, so host files never reach the sandbox.
    Only the workspace is modified — link targets are read, never written. Each
    copy is built under a temporary name and swapped in for the link last, so
    a failed copy leaves the link in place.
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
    if path.name == ".git" or target is None or not target.exists():
        path.unlink()
        return
    if not _within_roots(target, allowed_roots):
        _warn_outside_roots(path)
        path.unlink()
        return
    if not (target.is_dir() or target.is_file()):
        path.unlink()
        return
    scratch = Path(tempfile.mkdtemp(prefix=f".{path.name}.fullsend-", dir=str(path.parent)))
    try:
        staged = scratch / path.name
        if target.is_dir():
            _copy_tree(target, staged, allowed_roots, (target,))
        else:
            shutil.copy2(target, staged)
        path.unlink()
        os.replace(staged, path)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


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


def _run_base(workspace: Path) -> Path:
    """``<workspace>/.fullsend-runs``: the ``--output-dir`` for every invocation.

    A top-level child of the workspace, so fullsend's upload of the target
    repository leaves it out and an invocation never sees an earlier run's
    files. Refuses a base that resolves outside the workspace."""
    base = workspace / RUN_BASE_DIRNAME
    base.mkdir(parents=True, exist_ok=True)
    real_base = base.resolve()
    if real_base.parent != workspace:
        raise ValueError(
            f"fullsend output directory {base} resolves outside the workspace")
    return real_base


def _newest_run_dir(output_dir: Path, known=frozenset()) -> Optional[Path]:
    """Newest run directory under ``output_dir`` not listed in ``known`` (the
    entries that existed before this invocation)."""
    if not output_dir.is_dir():
        return None
    dirs = [d for d in output_dir.iterdir()
            if d.is_dir() and not d.is_symlink() and d.name not in known]
    if not dirs:
        return None
    return max(dirs, key=lambda d: d.stat().st_mtime)


def _iterations(run_dir: Path) -> list:
    """``[(number, path)]`` of the run's ``iteration-N`` directories, in order."""
    found = []
    for d in run_dir.iterdir():
        m = _ITERATION_RE.match(d.name)
        if m and d.is_dir() and not d.is_symlink():
            found.append((int(m.group(1)), d))
    return sorted(found, key=lambda item: item[0])


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


def _copy_subagent_transcripts(src: Path, dest: Path, workspace: Path,
                               conflict_prefix: str = "") -> None:
    """Copy subagent transcripts from ``src`` into ``dest`` (under
    ``workspace``), never following a symlink at the source directory, any
    destination component, or an existing destination file. With
    ``conflict_prefix``, a file whose name is already present in ``dest`` is
    copied under the prefixed name instead of overwriting it."""
    if src.is_symlink() or not src.is_dir():
        return
    if _has_symlink_component(workspace, dest):
        return
    for f in sorted(src.glob("*-agent-*.jsonl")):
        if f.is_file() and not f.is_symlink():
            target = dest / f.name
            if conflict_prefix and target.exists():
                target = dest / f"{conflict_prefix}{f.name}"
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
