"""Run ONE optimization case end to end: workspace -> execute -> collect -> judge -> reward.

Two substrates produce the same run dir: local (the eval-run scripts, judged
by score.py) and Harbor (a private task package per rollout, judged by the
in-container verifier).

Engine-agnostic: nothing here knows about any particular search engine. The
returned rollout's ``metadata`` already carries the composed reward and the
redacted feedback string, so an engine's ``score()`` method can be a pure read
of what this already computed — no second invocation, no further judging.

``compose_reward()`` (``agent_eval.harbor.reward``) is the only reward
composition path — the same function Harbor's reward bridge and
``/eval-anova`` consume. Nothing here recomputes or overrides it.
"""

import agent_eval._bootstrap  # noqa: F401 — activate harness venv before third-party imports

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import yaml

from agent_eval.config import EvalConfig
from agent_eval.optimize import artifact, export, intake, redact


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    # The sentinel records bootstrap completion in this process, not in a
    # newly spawned process.
    env.pop("_AGENT_EVAL_BOOTSTRAP_DONE", None)
    return env


def _script(manifest: dict, name: str) -> Path:
    path = Path(manifest["harness_root"]) / "skills" / "eval-run" / "scripts" / name
    if not path.is_file():
        raise FileNotFoundError(f"agent-eval-harness script not found: {path}")
    return path


def _run_script(
    manifest: dict,
    name: str,
    *args: str,
    allow_nonzero: bool = False,
) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(_script(manifest, name)), *map(str, args)]
    proc = subprocess.run(
        cmd,
        cwd=manifest["project_root"],
        env=_child_env(),
        capture_output=True,
        text=True,
    )
    if proc.returncode and not allow_nonzero:
        detail = (proc.stderr or proc.stdout or "").strip()[-5000:]
        raise RuntimeError(f"{name} failed ({proc.returncode}): {detail}")
    if proc.returncode and allow_nonzero:
        output_dir = None
        for i, token in enumerate(args[:-1]):
            if token == "--output":
                output_dir = Path(args[i + 1])
                break
        if output_dir is None or not (output_dir / "run_result.json").is_file():
            detail = (proc.stderr or proc.stdout or "").strip()[-5000:]
            raise RuntimeError(f"{name} failed before recording a run result: {detail}")
    return proc


def _safe_component(value: str, label: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or len(path.parts) != 1:
        raise ValueError(f"invalid {label}: {value!r}")
    return path.name


def _case_workspace(run_id: str) -> Path:
    return Path(tempfile.gettempdir()).resolve() / "agent-eval" / run_id


def _copy_tree_local(source: Path, destination: Path, *, allowed_root: Path) -> None:
    """Materialize a project resource into the runtime workspace without links."""
    source = Path(source)
    source = source.resolve(strict=True)
    allowed_root = Path(allowed_root).resolve()
    if not source.is_relative_to(allowed_root):
        raise ValueError(f"project runtime resource resolves outside project root: {source}")
    if source.is_dir():
        def _ignore_symlinks(directory, names):
            base = Path(directory)
            return {name for name in names if (base / name).is_symlink()}
    destination = Path(destination)
    if destination.is_symlink():
        destination.unlink()
    elif destination.is_dir():
        shutil.rmtree(destination)
    elif destination.exists():
        destination.unlink()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(
            source, destination, symlinks=False,
            ignore=lambda directory, names: (
                set(shutil.ignore_patterns(".git")(directory, names))
                | _ignore_symlinks(directory, names)
            ),
        )
    else:
        shutil.copy2(source, destination)


def _materialize_runtime_resources(manifest: dict, runtime_workspace: Path) -> None:
    """Keep plugin-relative script paths inside Claude's workspace boundary.

    Plugin-style skills resolve project resources (``scripts/``, a types
    registry, ...) relative to their third-parent plugin root. A normal
    workspace's symlinks point back to the source checkout, which Claude's
    read-block rejects for nested skill paths. Copy the resources intake
    resolved for this benchmark (``runtime_resources`` in the manifest) locally
    instead.
    """
    from agent_eval.workspace_provisioning import DEFAULT_RESERVED_NAMES

    project_root = Path(manifest["project_root"])
    names = manifest.get("runtime_resources")
    for name in intake.DEFAULT_RUNTIME_RESOURCES if names is None else names:
        if name in DEFAULT_RESERVED_NAMES:
            # Written by the harness itself (tool-interception hooks, inputs);
            # replacing it would break every hooked tool call.
            continue
        source = project_root / _safe_component(name, "runtime resource")
        if source.exists():
            _copy_tree_local(source, runtime_workspace / name, allowed_root=project_root)

    # A normal workspace build links .claude subdirectories separately from its
    # --symlinks list. Make those local too (notably the skill tree and agents).
    project_claude = project_root / ".claude"
    workspace_claude = runtime_workspace / ".claude"
    if project_claude.is_dir():
        workspace_claude.mkdir(parents=True, exist_ok=True)
        for source in project_claude.iterdir():
            if source.name == "settings.json" or not source.is_dir():
                continue
            _copy_tree_local(
                source, workspace_claude / source.name, allowed_root=project_root
            )


def score_from_summary(
    task_id: str, summary: dict, config: EvalConfig, *, execution_error: bool,
) -> tuple[str | None, float, str]:
    """Turn a collected summary.yaml into ``(error_message, reward, feedback)``.

    Pulled out of :func:`run_case` so the reward-composition/redaction step —
    the part most worth testing precisely — is callable without actually
    running a case's workspace/execute/collect pipeline.
    """
    if execution_error:
        return "harness runner failed before producing a measurement", 0.0, ""
    per_case = (summary.get("per_case") or {}).get(task_id)
    if not isinstance(per_case, dict) or not per_case:
        return "harness produced no judge results; treat this as missing data", 0.0, ""
    if any(isinstance(result, dict) and result.get("error") for result in per_case.values()):
        return "at least one harness judge failed; treat this evaluation as missing data", 0.0, ""

    from agent_eval.harbor.reward import compose_reward, judge_ranges
    if config.reward is None:
        raise ValueError("optimization requires an explicit eval.yaml 'reward:' configuration")
    reward, _metrics = compose_reward(per_case, judge_ranges=judge_ranges(config), reward_cfg=config.reward)
    # Judge rationale can contain expected/gold content. The proposer only
    # ever sees score labels and the target-agent transcript.
    feedback = redact.build_redacted_feedback(per_case, reward)
    return None, reward, feedback


# ── Making the candidate live ────────────────────────────────────────────────

# The parts of a Claude plugin the runner loads besides its skills.
_PLUGIN_COMPONENTS = (".claude-plugin", "agents", "commands", "hooks", ".mcp.json")


def _copy_skills(roots: list, destination: Path) -> None:
    """Copy every skill directory under ``roots`` (first root wins on a name)."""
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for skill in sorted(root.iterdir()):
            if (skill.is_dir() and not skill.is_symlink() and (skill / "SKILL.md").is_file()
                    and not (destination / skill.name).exists()):
                _copy_tree_local(skill, destination / skill.name, allowed_root=root)


def _private_plugin(manifest: dict, candidate_dir: Path, plugin: Path) -> None:
    """A private copy of the runner plugin that owns the candidate, with the
    candidate overlaid — the real plugin is never mutated, so concurrent
    rollouts stay isolated."""
    from agent_eval.workspace_provisioning import DEFAULT_RESERVED_NAMES

    source = Path(manifest["plugin_dir"])
    project_root = Path(manifest["project_root"])
    plugin.mkdir(parents=True)
    for name in _PLUGIN_COMPONENTS:
        if (source / name).exists():
            _copy_tree_local(source / name, plugin / name, allowed_root=source)
    for root in manifest["skill_roots"]:
        _copy_skills([root], plugin / Path(root).relative_to(source))
    if source == project_root:
        # Plugin-relative script paths (``../../scripts``) resolve inside the
        # plugin root, so the runtime resources travel with it.
        for name in manifest.get("runtime_resources") or []:
            if name not in DEFAULT_RESERVED_NAMES and (project_root / name).exists() \
                    and not (plugin / name).exists():
                _copy_tree_local(project_root / name, plugin / name, allowed_root=project_root)
    artifact.stage_candidate(
        plugin, candidate_dir, (project_root / manifest["apply_to"]).relative_to(source).as_posix(),
        manifest["candidate"],
    )


def _skills_plugin(manifest: dict, candidate_dir: Path, plugin: Path) -> None:
    """A private plugin root whose ``skills/`` holds every skill of the
    candidate's skill source with the candidate overlaid — forwarded to
    Harbor's ``--skill`` (uploaded into the trial, no image rebuild)."""
    project_root = Path(manifest["project_root"])
    skill_root = Path(manifest.get("skill_root") or project_root / intake.SKILLS_ROOT)
    (plugin / "skills").mkdir(parents=True)
    _copy_skills(manifest.get("skill_roots") or [skill_root], plugin / "skills")
    apply_rel = (project_root / manifest["apply_to"]).relative_to(skill_root)
    artifact.stage_candidate(plugin, candidate_dir, (Path("skills") / apply_rel).as_posix(), manifest["candidate"])


def _rollout_config(manifest: dict, private: Path, plugin: Path, files: list | None = None) -> Path:
    """A per-rollout ``extends:`` overlay of the eval config that makes the
    private plugin the runner's only plugin (``!replace``: the real one must
    not load alongside it), plus optional shared workspace files."""
    extends = os.path.relpath(Path(manifest["eval_config"]).resolve(), private.resolve())
    text = (f"extends: {json.dumps(extends)}\n"
            f"runner:\n  plugin_dirs: !replace\n  - {json.dumps(str(plugin))}\n")
    if files:
        text += yaml.safe_dump({"dataset": {"workspace": {"files": files}}}, sort_keys=False)
    path = private / "eval.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _execute_local(manifest: dict, task_id: str, candidate_dir: Path, run_id: str, run_dir: Path) -> tuple[bool, str]:
    """Local substrate: workspace.py -> execute.py -> collect.py -> score.py judges.

    Returns ``(execution_error, transcript)``; ``run_dir`` ends up holding the
    run's ``run_result.json`` and ``summary.yaml``.
    """
    workspace = _case_workspace(run_id)
    config_path = manifest["eval_config"]
    private = None
    if manifest.get("plugin_dir"):
        # The candidate lives in a runner plugin: run against a private copy.
        private = workspace.with_name(f"{run_id}.plugin")
        private.mkdir(parents=True)
        _private_plugin(manifest, candidate_dir, private / "plugin")
        config_path = str(_rollout_config(manifest, private, private / "plugin"))
    try:
        _execute_local_steps(manifest, task_id, candidate_dir, run_id, run_dir, workspace, config_path,
                             stage_in_workspace=private is None)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        if private is not None:
            shutil.rmtree(private, ignore_errors=True)

    try:
        run_result = json.loads((run_dir / "run_result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        run_result = {}
    # Case mode records an infra failure per case; batch mode records it once
    # for the run, in a top-level error_class (exit_code mirrors the skill's
    # own exit, not infra health, so it is not an error signal here).
    case_result = (run_result.get("per_case") or {}).get(task_id) or {}
    infra_error = case_result.get("error") or case_result.get("error_class")
    if manifest["execution_mode"] == "batch":
        infra_error = infra_error or run_result.get("error_class")
    # Case mode writes the transcript per case; batch mode writes it once for
    # the run — which, for a one-case rollout, is this case's transcript.
    output = ""
    for output_file in (run_dir / "cases" / task_id / "stdout.log", run_dir / "stdout.log"):
        if output_file.is_file():
            output = output_file.read_text(encoding="utf-8", errors="replace")
            break
    return bool(infra_error), output


def _execute_local_steps(
    manifest: dict, task_id: str, candidate_dir: Path, run_id: str, run_dir: Path,
    workspace: Path, config_path: str, *, stage_in_workspace: bool,
) -> None:
    _run_script(
        manifest,
        "workspace.py",
        "--config", config_path,
        "--run-id", run_id,
        "--cases", task_id,
        "--symlinks", "__optimize_no_project_resource_links__",
    )
    if manifest["execution_mode"] == "case":
        candidate_workspace = workspace / "cases" / task_id
        if not candidate_workspace.is_dir():
            raise RuntimeError(f"workspace builder did not create case workspace: {candidate_workspace}")
        redact.remove_gold(candidate_workspace)
    else:
        candidate_workspace = workspace
    _materialize_runtime_resources(manifest, candidate_workspace)
    if stage_in_workspace:
        artifact.stage_candidate(
            candidate_workspace,
            candidate_dir,
            manifest["apply_to"],
            manifest["candidate"],
        )

    execute_args = [
        "--workspace", str(workspace),
        "--config", config_path,
        "--output", str(run_dir),
        "--run-id", run_id,
        "--parallelism", "1",
    ]
    runner_budget = manifest.get("runner_budget_usd")
    if runner_budget is not None:
        execute_args += ["--max-budget", str(runner_budget)]
    _run_script(manifest, "execute.py", *execute_args, allow_nonzero=True)
    _run_script(
        manifest,
        "collect.py",
        "--config", config_path,
        "--workspace", workspace,
        "--output", run_dir,
    )
    try:
        _run_script(
            manifest,
            "score.py",
            "judges",
            "--run-id", run_id,
            "--config", config_path,
            "--workspace", workspace,
        )
    except RuntimeError:
        # score.py's own regression-detection gate (eval.yaml `thresholds:`)
        # can exit nonzero even when it wrote a usable summary.yaml with real
        # per-judge data — e.g. one judge erroring trips a configured
        # min_mean/min_pass_rate threshold. A nonzero exit here does not by
        # itself mean "no measurement": swallow it and let the
        # score_from_summary() read below inspect whatever summary.yaml
        # score.py actually produced, instead of crashing the whole rollout
        # on an exit code that doesn't speak to data availability.
        pass
    try:
        # The same report.html a plain /eval-run leaves (no model calls; the
        # Harbor substrate's agent_eval.harbor.run writes its own).
        _run_script(manifest, "report.py", "--run-id", run_id, "--config", config_path)
    except RuntimeError:
        pass  # a report is a convenience, never a reason to lose the measurement


# ── Harbor substrate ─────────────────────────────────────────────────────────

def _run_harness(manifest: dict, cmd: list, *, allow_nonzero: bool = False) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [sys.executable, *map(str, cmd)],
        cwd=manifest["project_root"],
        env=_child_env(),
        capture_output=True,
        text=True,
    )
    if proc.returncode and not allow_nonzero:
        detail = (proc.stderr or proc.stdout or "").strip()[-5000:]
        raise RuntimeError(f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''} failed ({proc.returncode}): {detail}")
    return proc


def _harbor_files(manifest: dict) -> list:
    """The runtime resources as shared workspace files: Harbor uploads them
    into the trial workspace (the local path copies them directly)."""
    from agent_eval.workspace_provisioning import DEFAULT_RESERVED_NAMES

    project_root = Path(manifest["project_root"])
    return [
        {"dest": name, "source": str(project_root / name)}
        for name in manifest.get("runtime_resources") or []
        if name not in DEFAULT_RESERVED_NAMES and (project_root / name).exists()
    ]


def _harbor_transcript(job_dir: str | None, run_dir: Path) -> str:
    """The trial's agent transcript, read only from inside this run's own
    Harbor jobs dir (``job_dir`` comes from run_result.json, not trusted)."""
    jobs_root = (run_dir / "harbor-jobs").resolve()
    if not job_dir:
        return ""
    job = Path(job_dir).resolve()
    if not job.is_dir() or not job.is_relative_to(jobs_root):
        return ""
    for pattern in ("*/agent/claude-code.txt", "*/agent/*.txt", "*/agent/trajectory.json"):
        for path in sorted(job.glob(pattern)):
            if not path.is_symlink() and path.resolve().is_relative_to(jobs_root):
                return path.read_text(encoding="utf-8", errors="replace")
    return ""


def _execute_harbor(
    manifest: dict, substrate: dict, task_id: str, candidate_dir: Path, run_id: str, run_dir: Path,
) -> tuple[bool, str]:
    """Harbor substrate: one private task package per rollout, judged in-container.

    The package is generated with the harness's own task generator, scrubbed of
    gold, and run through ``agent_eval.harbor.run`` — which writes the same
    ``run_result.json``/``summary.yaml`` shape as the local scorer. Judging
    already happened in the Harbor verifier, so nothing is judged again here.
    """
    if not substrate.get("image"):
        raise ValueError("the harbor substrate needs a task image (agent-eval-capevolve --image)")
    private = _case_workspace(run_id)
    private.mkdir(parents=True)
    try:
        plugin = private / "plugin"
        _skills_plugin(manifest, candidate_dir, plugin)
        config_path = _rollout_config(manifest, private, plugin, _harbor_files(manifest))
        tasks_dir = private / "tasks"
        harness_root = Path(manifest["harness_root"])
        _run_harness(manifest, [
            harness_root / "skills" / "eval-dataset" / "scripts" / "harbor.py",
            "--config", config_path, "--out", tasks_dir, "--image", substrate["image"],
            "--cases", task_id, "--agent-timeout", manifest.get("agent_timeout_s") or 1800,
        ])
        for package in tasks_dir.iterdir():
            redact.remove_gold(package / "environment")
        run_cmd = [
            "-m", "agent_eval.harbor.run",
            "--config", config_path, "--model", manifest["target_model"],
            "--output", run_dir, "--tasks-dir", tasks_dir, "--jobs-dir", run_dir / "harbor-jobs",
            "--env", substrate.get("env") or "podman", "--n-concurrent", "1",
        ]
        # Nonzero also means "regression thresholds tripped", which still
        # wrote a usable summary.yaml — the read below decides.
        _run_harness(manifest, run_cmd, allow_nonzero=True)
    finally:
        shutil.rmtree(private, ignore_errors=True)

    try:
        run_result = json.loads((run_dir / "run_result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True, ""
    failed = {entry[0] for entry in (run_result.get("trial_errors") or []) + (run_result.get("infra_errors") or [])
              if isinstance(entry, (list, tuple)) and entry}
    return task_id in failed, _harbor_transcript(run_result.get("harbor_job_dir"), run_dir)


def _mark_unconditional_judge_errors(summary: dict, task_id: str, config: EvalConfig) -> None:
    """Harbor's summary carries an errored in-container judge as ``value: None``
    with the error in ``rationale``. A judge without an ``if:`` condition can't
    have been skipped, so mark it errored — missing data, never a silent drop
    out of the reward composition."""
    per_case = (summary.get("per_case") or {}).get(task_id)
    if not isinstance(per_case, dict):
        return
    unconditional = {j.name for j in config.judges if not j.condition}
    for name, result in per_case.items():
        if isinstance(result, dict) and result.get("value") is None and name in unconditional:
            result["error"] = result.get("rationale") or "judge produced no value"


def run_case(manifest: dict, task_id: str, candidate_dir: Path, seed: int, substrate: dict | None = None) -> dict:
    """Run one case end to end and return ``{"rollout": {...}}``.

    A judging failure is treated identically to an execution failure: both set
    the returned rollout's ``error`` field, which an engine's own
    ``run_target()`` exception handling (and the search engine itself) already
    treats as missing data, never a real zero.
    """
    task_id = _safe_component(task_id, "task id")
    if task_id not in manifest["task_ids"]:
        raise ValueError(f"unknown case id: {task_id}")
    candidate_dir = Path(candidate_dir).resolve(strict=True)
    substrate = substrate or {"runner": "local"}
    run_id = f"opt-{task_id}-{candidate_dir.name}-{seed}-{uuid.uuid4().hex[:8]}"
    run_dir = export.runs_dir_for(manifest) / run_id
    started = time.time()
    config = EvalConfig.from_yaml(manifest["eval_config"])

    if substrate.get("runner") == "harbor":
        execution_error, output = _execute_harbor(manifest, substrate, task_id, candidate_dir, run_id, run_dir)
    else:
        execution_error, output = _execute_local(manifest, task_id, candidate_dir, run_id, run_dir)

    summary_path = run_dir / "summary.yaml"
    summary = (yaml.safe_load(summary_path.read_text(encoding="utf-8")) or {}) if summary_path.is_file() else {}
    if substrate.get("runner") == "harbor":
        _mark_unconditional_judge_errors(summary, task_id, config)
    cost, tokens, cost_source = export.read_usage(run_dir, summary)
    error_message, reward, feedback = score_from_summary(task_id, summary, config, execution_error=execution_error)

    condition = {
        "engine": manifest.get("engine", "optimize"),
        "substrate": substrate.get("runner", "local"),
        "candidate_id": candidate_dir.name,
        "task_id": task_id,
        "seed": seed,
        "harness_run_id": run_id,
        "duration_s": round(time.time() - started, 3),
    }
    export.write_condition(run_dir, condition)
    rollout = {
        "task_id": task_id,
        "output": output,
        "trace": output,
        "cost_usd": cost,
        "tokens": tokens,
        "error": error_message,
        "metadata": {
            "harness_run_id": run_id,
            "cost_source": cost_source,
            "reward": reward,
            "feedback": feedback,
        },
    }
    return {"rollout": rollout}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--candidate-dir", required=True)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        manifest_path = Path(args.manifest)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        substrate_path = manifest_path.with_name("substrate.json")
        substrate = json.loads(substrate_path.read_text(encoding="utf-8")) if substrate_path.is_file() else None
        payload = run_case(manifest, args.task_id, Path(args.candidate_dir), args.seed, substrate)
        print(json.dumps(payload, default=str))
        return 0
    except Exception as exc:  # JSON on stdout is the rollout protocol.
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
