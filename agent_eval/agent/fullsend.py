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

:meth:`FullsendRunner.provenance` records what executed a run (fullsend and
OpenShell versions, sandbox image, agent harness commit) for the run-level
``run_result.json``; every field fails soft to ``None`` with a warning.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

import yaml

from agent_eval.config import resolve_env_value, workspace_source_roots
from .base import EvalRunner, RunResult, terminate_process_group
from .stream_capture import (
    count_subagent_turns, extract_usage, message_ids_for_run,
    combine_per_model_turns,
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
# Read bounds for files the sandboxed agent's run directory contains.
_MAX_JSON_BYTES = 1_000_000
_MAX_TELEMETRY_BYTES = 10_000_000

# Run provenance (recorded in run_result.json eval_params and as MLflow params).
PROVENANCE_KEYS = (
    "fullsend_agent", "fullsend_version", "openshell_version", "sandbox_image",
    "sandbox_image_source", "sandbox_arch", "agent_harness_commit",
)
_PROBE_TIMEOUT_S = 15
_MAX_CONSOLE_SCAN_CHARS = 2_000_000
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# fullsend's console line naming the sandbox image it resolved, e.g.
# ``Image: ghcr.io/org/sandbox@sha256:…`` (any leading bullet or indent).
_IMAGE_LINE_RE = re.compile(r"^\W*Image:\s*(\S+)(.*)$")
_ARCH_VALUES = r"(amd64|arm64|x86_64|aarch64)"
_ARCH_LINE_RE = re.compile(
    r"^\W*(?:Sandbox\s+)?Arch(?:itecture)?:\s*" + _ARCH_VALUES + r"\b", re.IGNORECASE)
_ARCH_TOKEN_RE = re.compile(r"\b" + _ARCH_VALUES + r"\b")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class FullsendRunner(EvalRunner):
    """Runs a skill by invoking ``fullsend run <agent>`` on the workspace."""

    PROVENANCE_KEYS = PROVENANCE_KEYS

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
        unrestricted_symlink_copy: bool = False,
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
            float(_DEFAULT_TERM_GRACE_S) if term_grace_s is None
            else float(term_grace_s))
        self._log_prefix = log_prefix
        # Roots a workspace symlink may resolve into (project root + plugin
        # dirs). No roots means an empty allowlist: only the invocation
        # workspace itself is allowed. ``unrestricted_symlink_copy`` is the
        # explicit opt-in that disables the containment check (None here).
        self._allowed_roots = (
            None if unrestricted_symlink_copy
            else [Path(r).resolve() for r in (allowed_roots or [])])

    @property
    def name(self) -> str:
        return "fullsend"

    def provenance(self, console_texts=()) -> dict:
        """What executed this run, for ``eval_params`` in ``run_result.json``.

        ``console_texts`` are the captured console outputs (``RunResult.stderr``)
        of the run's invocations. ``sandbox_image`` is the ``Image:`` line
        fullsend printed there — authoritative, since ``FULLSEND_SANDBOX_IMAGE``
        in the fullsend environment replaces the harness's pinned image — and
        falls back to ``image:`` in the agent's harness file (registered in
        ``<fullsend_dir>/config.yaml``; see :func:`harness_file_image`) only
        when no console output has that line (``sandbox_image_source`` says
        which). ``sandbox_arch`` is set only when fullsend printed it. Every
        field is ``None`` when it cannot be determined, with a warning; this
        never raises."""
        # Probe with the environment fullsend itself runs in (execution.env and
        # runner.env over the process environment), so a PATH override that
        # selects a pinned fullsend/OpenShell install is the one recorded.
        probe_env = os.environ.copy()
        probe_env.update(self._env_entries("", None))
        probe_env.pop("FULLSEND_TASK", None)

        fullsend_version = probe_version(
            self._binary, ("fullsend", Path(self._binary).name), env=probe_env)
        if fullsend_version is None:
            _warn_provenance(f"could not read the version of {self._binary!r} "
                             f"(tried 'version' and '--version')")

        openshell = shutil.which("openshell", path=probe_env.get("PATH"))
        openshell_version = (
            probe_version(openshell, ("openshell",), env=probe_env)
            if openshell else None)
        if openshell is None:
            _warn_provenance("openshell is not on PATH; openshell_version is null")
        elif openshell_version is None:
            _warn_provenance(f"could not read the version of {openshell!r}")

        image = arch = None
        for text in console_texts or ():
            if not isinstance(text, str) or not text:
                continue
            found, found_arch = parse_sandbox_image(text[:_MAX_CONSOLE_SCAN_CHARS])
            if found and image is None:
                image = found
            elif found and found != image:
                _warn_provenance(f"invocations of this run printed different sandbox "
                                 f"images ({image} and {found}); recording the first")
            if found_arch and arch is None:
                arch = found_arch
        source = "fullsend-output" if image else None
        if image is None:
            image = harness_file_image(self._fullsend_dir, self._agent)
            if image:
                source = "harness-file"
                _warn_provenance(
                    "fullsend printed no 'Image:' line; sandbox_image is read from "
                    "the harness file and does not reflect a FULLSEND_SANDBOX_IMAGE "
                    "override")
            else:
                _warn_provenance("no sandbox image in fullsend's console output or "
                                 "the harness file; sandbox_image is null")

        commit = git_head(self._fullsend_dir)
        if commit is None:
            _warn_provenance(f"{self._fullsend_dir} is not inside a git checkout; "
                             f"agent_harness_commit is null")

        return {
            "fullsend_agent": self._agent,
            "fullsend_version": fullsend_version,
            "openshell_version": openshell_version,
            "sandbox_image": image,
            "sandbox_image_source": source,
            "sandbox_arch": arch,
            "agent_harness_commit": commit,
        }

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

        if _no_post_script(self._extra_args):
            return RunResult(
                exit_code=-1, stdout="", duration_s=0.0,
                stderr=("fullsend runner: runner.extra_args contains "
                        "--no-post-script, so the project's post-script (which "
                        "copies the agent's outputs into the workspace) would not "
                        "run and no artifacts could be collected; remove it"))

        workspace = workspace.resolve()
        original_settings = None
        try:
            try:
                # Sanitise before materialising: a workspace link that resolves
                # to .claude/settings.json (the workspace is an allowed root)
                # is copied as a regular file, and that copy must already be
                # free of the host-only env, harness hooks and directory grants. The
                # sanitiser refuses a symlinked canonical path, so nothing
                # unsanitised is left for the materialisation to copy.
                original_settings = _sanitize_settings(workspace)
                # Links resolving inside this invocation's own workspace (e.g.
                # a relative link in an isolated workspace) are always allowed.
                roots = (None if self._allowed_roots is None
                         else [*self._allowed_roots, workspace])
                materialize_symlinks(workspace, roots)
                output_dir = _run_base(workspace)
            except (OSError, ValueError, shutil.Error) as e:
                return RunResult(
                    exit_code=-1, stdout="", stderr=str(e), duration_s=0.0)
            return self._launch(
                target, args, workspace, output_dir, model, timeout_s, extra_env)
        finally:
            # The workspace is shared with later (non-fullsend) pipeline
            # steps, which still need the harness hooks, env and directory
            # grants.
            _restore_settings(workspace, original_settings)

    def _launch(self, target, args, workspace, output_dir, model, timeout_s,
                extra_env) -> RunResult:
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
        returncode, console_out, console_err, duration = self._run(
            cmd, workspace, env, timeout_s)

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
            return -1, "", str(e), time.monotonic() - start
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
        except BaseException:
            # fullsend runs in its own session, so the terminal's SIGINT never
            # reaches it: stop the group (and its sandbox) before re-raising.
            terminate_process_group(proc, self._term_grace_s)
            raise
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

            # extract_usage describes one invocation (each result event
            # overwrites the last), so read every iteration's stream on its own
            # and sum the iterations.
            (token_usage, cost_usd, num_turns, stream_ids, models_seen,
             per_model_usage, stream_ids_by_model) = _sum_iteration_usage(streams)
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

_TOKEN_KEYS = ("input", "output", "cache_read", "cache_create")


def _add_tokens(total: Optional[dict], part: Optional[dict]) -> Optional[dict]:
    if not part:
        return total
    total = total or dict.fromkeys(_TOKEN_KEYS, 0)
    for key in _TOKEN_KEYS:
        total[key] += part.get(key) or 0
    return total


def _sum_iteration_usage(streams: list) -> tuple:
    """:func:`extract_usage` over each iteration's stream, summed.

    Returns the same tuple shape as ``extract_usage``. Token totals, cost and
    the per-model breakdown add up across iterations; a partly finished
    iteration (no ``result`` event) still contributes the tokens of its
    assistant events. Message ids, models and per-model ids are unions, so
    turn counting and subagent-transcript deduplication span the whole
    invocation."""
    token_usage = None
    cost_usd = None
    per_model_usage = None
    stream_ids = set()
    models_seen = set()
    ids_by_model = {}
    for text in streams:
        (tokens, cost, _turns, ids, models, per_model,
         by_model) = extract_usage(text.splitlines())
        token_usage = _add_tokens(token_usage, tokens)
        if cost is not None:
            cost_usd = (cost_usd or 0) + cost
        if per_model:
            per_model_usage = per_model_usage or {}
            for model, stats in per_model.items():
                merged = per_model_usage.setdefault(
                    model, {**dict.fromkeys(_TOKEN_KEYS, 0), "cost_usd": None})
                _add_tokens(merged, stats)
                if stats.get("cost_usd") is not None:
                    merged["cost_usd"] = (merged["cost_usd"] or 0) + stats["cost_usd"]
        stream_ids |= ids
        models_seen |= models
        for model, model_ids in (by_model or {}).items():
            ids_by_model.setdefault(model, set()).update(model_ids)
    return (token_usage, cost_usd, len(stream_ids) or None, stream_ids,
            models_seen, per_model_usage, ids_by_model or None)


_FALSE_SPELLINGS = frozenset({"false", "0", "no", "off", "n", "f"})


def _no_post_script(extra_args) -> bool:
    """Whether ``runner.extra_args`` asks fullsend to skip the post-script.

    Matches the bare ``--no-post-script`` and the ``--no-post-script=<value>``
    form; any value other than a false-y spelling (``false``, ``0``, ``no``,
    ``off``, ``n``, ``f``) enables it, so a misspelt value fails the case
    rather than letting a run without artifacts through."""
    for arg in extra_args or ():
        if not isinstance(arg, str):
            continue
        if arg == "--no-post-script":
            return True
        if arg.startswith("--no-post-script="):
            value = arg.split("=", 1)[1].strip().lower()
            if value not in _FALSE_SPELLINGS:
                return True
    return False


def _restore_settings(workspace: Path,
                      original: Optional[tuple[bytes, Optional[int]]]) -> None:
    """Put back the ``.claude/settings.json`` bytes and permission bits saved by
    ``_sanitize_settings`` (no-op when nothing was changed). The mode is the
    one recorded before the run, never the post-run file's — the run could have
    loosened it — and falls back to owner-only when none was recorded."""
    if original is None:
        return
    content, original_mode = original
    claude_dir = workspace / ".claude"
    target = claude_dir / "settings.json"
    tmp_name = None
    try:
        # The run (or the project's post-script) may have replaced either path
        # with a link: refuse a link in the directory chain, and replace the
        # file entry itself rather than writing through whatever it points at.
        if _has_symlink_component(workspace, claude_dir) or not claude_dir.is_dir():
            raise OSError(f"{claude_dir} is not a regular directory")
        mode = 0o600 if original_mode is None else original_mode
        fd, tmp_name = tempfile.mkstemp(dir=claude_dir, prefix=".settings-")
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, target)  # replaces a symlink entry, never follows it
        tmp_name = None
    except OSError as exc:
        with _print_lock:
            print(f"  WARNING: could not restore {workspace}/.claude/settings.json "
                  f"after the fullsend run ({exc}); later steps in this "
                  f"workspace will run without its hooks and env", flush=True)
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _sanitize_settings(
        workspace: Path) -> Optional[tuple[bytes, Optional[int]]]:
    """Drop the host-only parts of the workspace ``.claude/settings.json``.

    The harness writes ``env`` (execution.env, already passed to fullsend through
    its own environment), its own hook entries (``SubagentStop`` capture and
    ``PreToolUse`` interception, whose commands run a script from the workspace's
    own ``hooks/`` directory, which does not exist in the sandbox) and
    ``permissions.additionalDirectories`` (host directories) for the other
    runners. fullsend uploads the settings file with the repository, and the
    agent definition owns the sandbox's environment, so those are removed here.
    The project's own hooks, carried over from ``<project>/.claude/settings.json``
    by ``workspace.py`` (relative commands such as ``python3 scripts/...`` that
    resolve inside the uploaded repository), are kept: production fullsend runs
    load exactly those from the checkout, and stripping them would run the eval
    with less recovery machinery than production. See ``_is_harness_hook``.
    Raises ``ValueError`` when the file exists but cannot be parsed, or when it
    or its ``.claude`` directory is a symlink, so the run is refused instead of
    uploading the file unsanitised.

    Returns ``(original bytes, original permission bits)`` when the file was
    rewritten (so the caller can restore both for later pipeline steps), else
    ``None``. The mode is ``None`` if it could not be read."""
    path = workspace / ".claude" / "settings.json"
    if _has_symlink_component(workspace, path):
        # Fail closed: a symlinked .claude or settings.json cannot be sanitised
        # in place (writing would follow the link), and once materialised its
        # copy would be uploaded unsanitised, env credentials included.
        raise ValueError(
            f"{path} or its .claude directory is a symlink; the fullsend runner "
            "cannot sanitise it before upload to the sandbox — replace the link "
            "with a regular file or directory")
    if not path.is_file():
        return None
    try:
        original = path.read_bytes()
        settings = json.loads(original.decode("utf-8"))
    except (OSError, ValueError) as exc:  # UnicodeDecodeError is a ValueError
        # Fail closed: an unreadable file may still carry literal env
        # credentials, and fullsend would upload it unchanged.
        raise ValueError(
            f"cannot sanitise {path} before upload to the fullsend sandbox "
            f"({exc}); fix or remove the file") from exc
    if not isinstance(settings, dict):
        return None
    changed = False
    if "env" in settings:
        del settings["env"]
        changed = True
    if "hooks" in settings:
        kept = _project_hooks(settings["hooks"], workspace)
        if kept != settings["hooks"]:
            changed = True
            if kept:
                settings["hooks"] = kept
            else:
                del settings["hooks"]
    perms = settings.get("permissions")
    if isinstance(perms, dict) and "additionalDirectories" in perms:
        del perms["additionalDirectories"]
        changed = True
    if not changed:
        return None
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = None
    try:
        path.write_text(json.dumps(settings, indent=2))
    except OSError:
        path.write_bytes(original)  # best effort: never leave a partial rewrite
        raise
    return original, mode


def _project_hooks(hooks, workspace: Path):
    """The ``hooks`` block without the harness-injected entries.

    Each event's list keeps only the groups ``_is_harness_hook`` rejects; an
    event left empty is dropped. A block that is not a mapping is returned
    as-is (the CLI rejects it either way)."""
    if not isinstance(hooks, dict):
        return hooks
    roots = _harness_hook_dirs(workspace)
    kept = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            kept[event] = groups
            continue
        remaining = [g for g in groups if not _is_harness_hook(g, workspace, roots)]
        if remaining:
            kept[event] = remaining
    return kept


def _harness_hook_dirs(workspace: Path) -> list:
    """The workspace's ``hooks/`` directory, resolved and as given: workspace.py
    may have written the path before symlink resolution (``/tmp`` versus
    ``/private/tmp`` on macOS)."""
    return [workspace.resolve() / "hooks", workspace / "hooks"]


def _is_harness_hook(group, workspace: Path, hook_dirs: Optional[list] = None) -> bool:
    """Whether a hook group was injected by the harness for the other runners.

    ``workspace.py`` sets no marker on its entries, but every one it writes
    (``setup_subagent_hook``, ``generate_interception``) runs a script from the
    staged workspace's own ``hooks/`` directory
    (``python3 '<workspace>/hooks/subagent_stop.py'``,
    ``python3 '<workspace>/hooks/tools.py'``). The project's hooks are copied
    verbatim from the checkout and never run a script from there. So a group is
    the harness's only when an absolute token of one of its command handlers
    resolves to a path under ``<workspace>/hooks``; every other hook, absolute
    paths included (``/usr/bin/python3 scripts/guard.py``), is kept. A command
    that does not tokenise is checked for ``/``-led tokens textually."""
    if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
        return False
    roots = _harness_hook_dirs(workspace) if hook_dirs is None else hook_dirs
    for handler in group["hooks"]:
        if not isinstance(handler, dict):
            continue
        command = handler.get("command")
        if isinstance(command, str) and _runs_workspace_hook(command, roots):
            return True
    return False


_ABS_TOKEN = re.compile(r"""(?:^|[\s"'=])(/[^\s"']*)""")


def _runs_workspace_hook(command: str, hook_dirs: list) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = _ABS_TOKEN.findall(command)
    for tok in tokens:
        if not tok.startswith("/"):
            continue
        real = _resolve_link(Path(tok))
        if real is not None and any(
                real == root or root in real.parents for root in hook_dirs):
            return True
    return False


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
    # The workspace's own settings file is sanitised before this runs, so a
    # top-level link to it may be copied; a link to any other settings file, or
    # one to it from inside a copied tree, may not.
    canonical = _resolve_link(workspace / ".claude" / "settings.json")
    settings_ids = _settings_identities(workspace, allowed_roots)
    for entry in sorted(workspace.iterdir()):
        _materialize_link(entry, allowed_roots, canonical, settings_ids)
    claude_dir = workspace / ".claude"
    if claude_dir.is_dir():
        for entry in sorted(claude_dir.iterdir()):
            _materialize_link(entry, allowed_roots, canonical, settings_ids)


def _resolve_link(path: Path) -> Optional[Path]:
    try:
        return path.resolve()
    except (OSError, RuntimeError):  # unreadable or looping link
        return None


def _settings_identities(workspace: Path, allowed_roots: Optional[list]) -> frozenset:
    """Resolved paths of the Claude Code settings files and ``.claude``
    directories of every allowed root and of the workspace, so a link is
    recognised by what it points at whatever its own name or its target's
    name (``.claude`` may itself be a link to a differently named directory,
    and ``settings.local.json`` a link to a file elsewhere)."""
    roots = [workspace, *(allowed_roots or [])]
    found = set()
    for root in roots:
        claude = root / ".claude"
        real_claude = _resolve_link(claude)
        if real_claude is not None and real_claude.is_dir():
            found.add(real_claude)
        for name in _SETTINGS_FILE_NAMES:
            candidate = claude / name
            real = _resolve_link(candidate)
            if real is not None and candidate.exists():
                found.add(real)
        found |= _nested_settings_identities(root)
    return frozenset(found)


def _nested_settings_identities(root: Path) -> set:
    """Resolved targets of every ``settings.json`` / ``settings.local.json``
    inside a nested ``.claude`` directory under ``root``, so an alias reaching
    such a file through the link's own target is recognised like a direct link.
    ``.git`` is skipped and directory links are not followed by the walk. The
    one exception is a ``.claude`` entry that is itself a link: it is resolved
    and the directory plus the settings files directly inside it are
    registered (nothing beneath them is followed)."""
    found = set()
    for dirpath, dirnames, _files in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        if ".claude" in dirnames:
            linked = Path(dirpath) / ".claude"
            real_dir = _resolve_link(linked) if linked.is_symlink() else None
            if real_dir is not None and real_dir.is_dir():
                found.add(real_dir)
                for name in _SETTINGS_FILE_NAMES:
                    candidate = real_dir / name
                    real = _resolve_link(candidate)
                    if real is not None and candidate.exists():
                        found.add(real)
        if Path(dirpath).name != ".claude":
            continue
        for name in _SETTINGS_FILE_NAMES:
            candidate = Path(dirpath) / name
            real = _resolve_link(candidate)
            if real is not None and candidate.exists():
                found.add(real)
    return found


def _within_roots(path: Path, allowed_roots: Optional[list]) -> bool:
    if allowed_roots is None:
        return True
    return any(path == root or root in path.parents for root in allowed_roots)


def _materialize_link(path: Path, allowed_roots: Optional[list] = None,
                      canonical_settings: Optional[Path] = None,
                      settings_ids: frozenset = frozenset()) -> None:
    if not path.is_symlink():
        return
    if _is_settings_destination(path):
        _warn_settings_link(path)
        path.unlink()
        return
    target = _resolve_link(path)
    if path.name == ".git" or target is None or not target.exists():
        path.unlink()
        return
    if not _within_roots(target, allowed_roots):
        _warn_outside_roots(path)
        path.unlink()
        return
    # A link to its own directory or an ancestor of it (``alias -> .``) would
    # copy the staging directory into itself without end: the copy is staged
    # next to the link, inside the target.
    parent = _resolve_link(path.parent)
    if parent is not None and (target == parent or target in parent.parents):
        _warn_self_referential(path)
        path.unlink()
        return
    # A link to a Claude Code settings file (or ``.claude`` directory) other
    # than this workspace's own (already sanitised) settings.json would upload
    # host settings the sanitiser never saw, e.g.
    # <project>/.claude/settings.local.json.
    if ((_is_settings_file(target) or target in settings_ids)
            and (canonical_settings is None or target != canonical_settings)):
        _warn_settings_link(path)
        path.unlink()
        return
    # A link to any directory named ``.claude`` (a nested one inside the
    # project, say, however the link itself is named) is a settings container.
    if _is_claude_dir(target):
        _warn_settings_container(path)
        path.unlink()
        return
    if not (target.is_dir() or target.is_file()):
        path.unlink()
        return
    scratch = Path(tempfile.mkdtemp(prefix=f".{path.name}.fullsend-", dir=str(path.parent)))
    try:
        staged = scratch / path.name
        if target.is_dir():
            _copy_tree(target, staged, allowed_roots, (target,), scratch.resolve(),
                       settings_ids)
        else:
            shutil.copy2(target, staged)
        path.unlink()
        os.replace(staged, path)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _copy_tree(src: Path, dest: Path, allowed_roots, active: tuple,
               staging: Optional[Path] = None,
               settings_ids: frozenset = frozenset()) -> None:
    """Copy ``src`` into ``dest`` as real files, following symlinks.

    Each nested link resolves relative to its own parent. Dangling links,
    links leaving ``allowed_roots``, links back into a directory already being
    copied (cycles) and the ``staging`` directory this copy is being built in
    are skipped; ``.git`` is excluded. So is every Claude Code settings file
    (host settings the sanitiser never saw): an entry whose destination would
    be ``.claude/settings.json`` or ``.claude/settings.local.json``, a file
    that is one of ``settings_ids`` and a link resolving to one of them (a
    ``.claude`` directory included), regular or linked and however the tree
    was reached. Entries are also judged by what they resolve to: a resolved
    ``.claude/settings*.json`` file is skipped, and a resolved directory
    named ``.claude`` is not copied at all."""
    dest.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir()):
        if entry.name == ".git":
            continue
        if _is_settings_destination(dest / entry.name):
            _warn_settings_entry(entry)
            continue
        real = entry
        if entry.is_symlink():
            real = _resolve_link(entry)
            if real is None or not real.exists():
                continue
            if not _within_roots(real, allowed_roots):
                _warn_outside_roots(entry)
                continue
        # Decide by source identity too, whether or not the entry is a link:
        # a resolved ``.claude/settings*.json`` file is never copied, and a
        # resolved ``.claude`` directory is a settings container that is not
        # copied at all.
        if _is_settings_file(real):
            if entry.is_symlink():
                _warn_settings_link(entry)
            else:
                _warn_settings_entry(entry)
            continue
        if _is_claude_dir(real):
            _warn_settings_container(entry)
            continue
        if real in settings_ids and (entry.is_symlink() or real.is_file()):
            _warn_settings_entry(entry)
            continue
        if real.is_dir():
            if staging is not None and _resolve_link(real) == staging:
                continue
            if real not in active:
                _copy_tree(real, dest / entry.name, allowed_roots, active + (real,),
                           staging, settings_ids)
        elif real.is_file():
            shutil.copy2(real, dest / entry.name)


def _warn_outside_roots(path: Path) -> None:
    with _print_lock:
        print(f"  WARNING: not copying symlink {path.name!r} into the fullsend "
              f"workspace: its target is outside the project and plugin "
              f"directories", flush=True)


_SETTINGS_FILE_NAMES = frozenset({"settings.json", "settings.local.json"})


def _is_settings_file(real: Path) -> bool:
    """``real`` (a resolved link target) is a Claude Code settings file: a
    file named ``settings.json`` or ``settings.local.json`` whose parent
    directory is ``.claude``."""
    return (real.name in _SETTINGS_FILE_NAMES and real.parent.name == ".claude"
            and real.is_file())


def _is_claude_dir(real: Path) -> bool:
    """``real`` (a resolved path) is a directory named ``.claude``: a settings
    container, whatever the name of the link that reached it."""
    return real.name == ".claude" and real.is_dir()


def _is_settings_destination(path: Path) -> bool:
    """``path`` (where an entry will land) is ``<...>/.claude/settings.json`` or
    ``.../settings.local.json``."""
    return path.name in _SETTINGS_FILE_NAMES and path.parent.name == ".claude"


def _warn_settings_entry(path: Path) -> None:
    with _print_lock:
        print(f"  WARNING: not copying {path.name!r} into the fullsend "
              f"workspace: it is a .claude settings file, which would be "
              f"uploaded unsanitised", flush=True)


def _warn_settings_link(path: Path) -> None:
    with _print_lock:
        print(f"  WARNING: not copying symlink {path.name!r} into the fullsend "
              f"workspace: its target is a .claude settings file, which would "
              f"be uploaded unsanitised", flush=True)


def _warn_settings_container(path: Path) -> None:
    with _print_lock:
        print(f"  WARNING: not copying {path.name!r} into the fullsend "
              f"workspace: it is, or links to, a .claude directory, which "
              f"may hold settings that would be uploaded unsanitised",
              flush=True)


def _warn_self_referential(path: Path) -> None:
    with _print_lock:
        print(f"  WARNING: not copying symlink {path.name!r} into the fullsend "
              f"workspace: its target is the link's own directory or an "
              f"ancestor of it", flush=True)


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


def _regular_file(path: Path, max_bytes: int) -> bool:
    """True for a regular file (never a symlink) of at most ``max_bytes``.

    The run directory is written by the sandboxed agent's post-script, so its
    files are untrusted: a link could point at a host file and a huge file
    could exhaust memory."""
    try:
        if path.is_symlink() or not path.is_file():
            return False
        return path.stat().st_size <= max_bytes
    except OSError:
        return False


def _read_json(path: Path) -> dict:
    if not _regular_file(path, _MAX_JSON_BYTES):
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
    """Regular files under ``path``; a symlinked directory or file is not
    followed or counted."""
    if path.is_symlink() or not path.is_dir():
        return 0
    return sum(
        1 for _root, _dirs, names in os.walk(path, followlinks=False)
        for name in names if not (Path(_root) / name).is_symlink())


def _telemetry_denials(path: Path) -> list:
    """Hook block events from ``run-telemetry.jsonl`` (best effort).

    A line counts as a block when a ``decision``/``action``/``outcome``/
    ``result`` field (top level or under ``attributes``) is block/deny, or a
    ``blocked`` field is true. Returns ``[]`` when the file is absent."""
    if path.is_symlink() or not path.is_file():
        return []
    denials = []
    try:
        # Bounded read; a line cut at the limit simply fails to parse.
        with open(path, "r", errors="replace") as fh:
            lines = fh.read(_MAX_TELEMETRY_BYTES).splitlines()
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


# ── Run provenance ───────────────────────────────────────────────────

def _warn_provenance(message: str) -> None:
    with _print_lock:
        print(f"  WARNING: fullsend provenance: {message}", flush=True)


def probe_version(binary: Optional[str], names=(), env=None) -> Optional[str]:
    """``<binary> version``, falling back to ``<binary> --version``, run with
    ``env`` (default: the process environment).

    Returns the first non-empty output line with a leading tool name (any of
    ``names``) and ``version`` word removed — ``fullsend version 212d119`` gives
    ``212d119`` — or ``None`` when the binary is missing, both probes fail, or
    they print nothing."""
    if not binary:
        return None
    for args in (["version"], ["--version"]):
        try:
            proc = subprocess.run(
                [binary, *args], capture_output=True, text=True,
                stdin=subprocess.DEVNULL, timeout=_PROBE_TIMEOUT_S, env=env)
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        if proc.returncode != 0:
            continue
        version = _version_text(proc.stdout or proc.stderr or "", names)
        if version:
            return version
    return None


def _version_text(text: str, names=()) -> Optional[str]:
    for raw in text.splitlines():
        line = _ANSI_RE.sub("", raw).strip()
        if not line:
            continue
        prefixes = "|".join(re.escape(n) for n in names if n)
        if prefixes:
            line = re.sub(rf"^(?:{prefixes})\s+", "", line, flags=re.IGNORECASE)
        line = re.sub(r"^version\b[:\s]*", "", line, flags=re.IGNORECASE).strip()
        return line[:200] or None
    return None


def parse_sandbox_image(console: str) -> tuple:
    """``(image, arch)`` from fullsend's console output.

    ``image`` is the value of the first ``Image:`` line; ``arch`` is an
    architecture (amd64, arm64, …) printed on that line or on an
    ``Arch:``/``Architecture:`` line. Either is ``None`` when absent."""
    image = arch = None
    for raw in console.splitlines():
        line = _ANSI_RE.sub("", raw).strip()
        if image is None:
            m = _IMAGE_LINE_RE.match(line)
            if m:
                image = m.group(1)
                token = _ARCH_TOKEN_RE.search(m.group(2))
                if token and arch is None:
                    arch = token.group(1)
                continue
        if arch is None:
            m = _ARCH_LINE_RE.match(line)
            if m:
                arch = m.group(1).lower()
        if image is not None and arch is not None:
            break
    return image, arch


def _read_yaml_mapping(path: Path) -> Optional[dict]:
    """The mapping in a YAML file, or ``None`` (with a warning when the file
    exists but cannot be parsed) when it is absent, unreadable or not a mapping."""
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text())
    except OSError:
        _warn_provenance(f"{path} could not be read")
        return None
    except (ValueError, yaml.YAMLError):
        # No parser text: it can quote the file's contents.
        _warn_provenance(f"{path} could not be parsed")
        return None
    return data if isinstance(data, dict) else None


_URL_SOURCE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")

# Bound on the harness files read while following a ``base:`` chain.
_MAX_BASE_CHAIN = 16


def _image_of(root: Path, path: Path) -> Optional[str]:
    """The first non-empty ``image:`` walking the harness file ``path`` and
    then its ``base:`` chain, the way fullsend composes a child harness: a
    local ``base:`` resolves relative to the directory of the file naming it,
    and an empty ``image:`` does not override the base's.

    ``None`` when a file in the chain is missing, unreadable or resolves
    outside ``root``, when a ``base:`` is a URL or an absolute path, when the
    chain loops, or when no file in it sets an image."""
    seen = set()
    while len(seen) < _MAX_BASE_CHAIN:
        resolved = _contained(root, path)
        if resolved is None or resolved in seen:
            return None
        seen.add(resolved)
        data = _read_yaml_mapping(resolved)
        if data is None:
            return None
        image = data.get("image")
        if isinstance(image, str) and image.strip():
            return image.strip()
        base = data.get("base")
        if not isinstance(base, str) or not base.strip():
            return None
        base = base.strip()
        if _URL_SOURCE_RE.match(base) or Path(base).is_absolute():
            return None
        path = path.parent / base
    return None


def _registered_agent_entry(config: dict, agent: str) -> Optional[dict]:
    """The last enabled ``agents[]`` entry of ``<fullsend_dir>/config.yaml``
    whose ``name`` or source stem (``#`` fragment stripped) equals ``agent``,
    case-insensitively — fullsend's ``findConfigAgentEntry`` lookup."""
    entries = config.get("agents")
    if not isinstance(entries, list):
        return None
    wanted = agent.casefold()
    match = None
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("enabled") is False:
            continue
        names = []
        if isinstance(entry.get("name"), str):
            names.append(entry["name"])
        source = entry.get("source")
        if isinstance(source, str) and source:
            names.append(Path(source.split("#", 1)[0]).stem)
        if any(n.casefold() == wanted for n in names):
            match = entry
    return match


def _contained(root: Path, path: Path) -> Optional[Path]:
    """``path`` resolved (symlinks followed), or ``None`` when it resolves
    outside ``root`` or cannot be resolved (a looping or unreadable link)."""
    resolved = _resolve_link(path)
    resolved_root = _resolve_link(root)
    if resolved is None or resolved_root is None:
        return None
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        return None
    return resolved


def harness_file_image(fullsend_dir, agent: str) -> Optional[str]:
    """``image:`` of the harness file fullsend would load for ``agent``.

    A per-repo install registers the agent in ``<fullsend_dir>/config.yaml``
    under ``agents[].source``, and fullsend resolves the harness as
    ``<fullsend_dir>/<source>``. A matching entry decides the answer: a
    relative ``source`` that stays inside ``fullsend_dir`` gives that file's
    ``image:``; a URL source, an override-only entry (no ``source``) or an
    unreadable file gives ``None``. Only when no entry matches is
    ``<fullsend_dir>/harness/<agent>.yaml`` (``.yml`` when no ``.yaml`` exists)
    tried. A file without an ``image:`` takes it from its ``base:`` chain
    (see :func:`_image_of`). ``None`` when nothing yields an image."""
    if not agent or not isinstance(agent, str) or Path(agent).name != agent:
        return None
    root = Path(fullsend_dir)
    config_path = _contained(root, root / "config.yaml")
    config = _read_yaml_mapping(config_path) if config_path else None
    entry = _registered_agent_entry(config, agent) if config else None
    if entry is not None:
        source = entry.get("source")
        if not isinstance(source, str):
            return None
        source = source.split("#", 1)[0].strip()
        if not source or _URL_SOURCE_RE.match(source) or Path(source).is_absolute():
            return None
        return _image_of(root, root / source)
    for suffix in (".yaml", ".yml"):
        candidate = root / "harness" / f"{agent}{suffix}"
        if candidate.is_file():
            return _image_of(root, candidate)
    return None


def git_head(directory) -> Optional[str]:
    """``git rev-parse HEAD`` of the checkout containing ``directory``, or
    ``None`` when it is not inside one (or git is unavailable)."""
    if not directory or not Path(directory).is_dir():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "HEAD"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
            timeout=_PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    sha = (proc.stdout or "").strip()
    return sha if proc.returncode == 0 and _GIT_SHA_RE.match(sha) else None
