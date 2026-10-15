"""Run ONE optimization case end to end: workspace -> execute -> collect -> judge -> reward.

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
from agent_eval.optimize import artifact, export, redact


def _child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    if extra:
        env.update(extra)
    # Do this last: the sentinel records bootstrap completion in this process,
    # not in a newly spawned process.
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
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(_script(manifest, name)), *map(str, args)]
    proc = subprocess.run(
        cmd,
        cwd=manifest["project_root"],
        env=_child_env(env_overrides),
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


def _materialize_plugin_layout(manifest: dict, runtime_workspace: Path) -> None:
    """Keep plugin-relative script/type paths inside Claude's workspace boundary.

    Plugin-style skills resolve ``scripts/`` and ``types/`` relative to their
    third-parent plugin root. A normal workspace's symlinks point back to the
    source checkout, which Claude's read-block rejects for nested skill paths.
    Copy these small plugin resources locally instead.
    """
    project_root = Path(manifest["project_root"])
    for name in ("scripts", "types", "CLAUDE.md", ".context", "overlays"):
        source = project_root / name
        if source.exists():
            _copy_tree_local(source, runtime_workspace / name, allowed_root=project_root)

    rubric = project_root / "artifacts" / "rfe-rubric.md"
    if rubric.is_file():
        _copy_tree_local(
            rubric, runtime_workspace / "artifacts" / rubric.name,
            allowed_root=project_root,
        )

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


def run_case(manifest: dict, task_id: str, candidate_dir: Path, seed: int) -> dict:
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
    run_id = f"opt-{task_id}-{candidate_dir.name}-{seed}-{uuid.uuid4().hex[:8]}"
    workspace = _case_workspace(run_id)
    run_dir = export.runs_dir_for(manifest) / run_id
    started = time.time()

    _run_script(
        manifest,
        "workspace.py",
        "--config", manifest["eval_config"],
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
    _materialize_plugin_layout(manifest, candidate_workspace)
    artifact.stage_candidate(
        candidate_workspace,
        candidate_dir,
        manifest["apply_to"],
        manifest["candidate"],
    )

    execute_args = [
        "--workspace", str(workspace),
        "--config", manifest["eval_config"],
        "--output", str(run_dir),
        "--run-id", run_id,
        "--parallelism", "1",
    ]
    runner_budget = manifest.get("runner_budget_usd")
    if runner_budget is not None:
        execute_args += ["--max-budget", str(runner_budget)]
    _run_script(
        manifest,
        "execute.py",
        *execute_args,
        allow_nonzero=True,
        env_overrides={"RFE_SKIP_BOOTSTRAP": "1"},
    )
    _run_script(
        manifest,
        "collect.py",
        "--config", manifest["eval_config"],
        "--workspace", workspace,
        "--output", run_dir,
    )
    _run_script(
        manifest,
        "score.py",
        "judges",
        "--run-id", run_id,
        "--config", manifest["eval_config"],
        "--workspace", workspace,
        env_overrides=(
            {"AGENT_EVAL_RUNNER_JUDGE_MAX_BUDGET_USD": str(manifest["judge_budget_usd"])}
            if manifest.get("judge_budget_usd") is not None else None
        ),
    )
    shutil.rmtree(workspace, ignore_errors=True)

    summary = yaml.safe_load((run_dir / "summary.yaml").read_text(encoding="utf-8")) or {}
    cost, tokens, cost_source = export.read_usage(run_dir, summary)
    try:
        run_result = json.loads((run_dir / "run_result.json").read_text(encoding="utf-8"))
        case_result = (run_result.get("per_case") or {}).get(task_id) or {}
    except (OSError, ValueError):
        case_result = {}
    execution_error = case_result.get("error") or case_result.get("error_class")

    error_message, reward, feedback = score_from_summary(
        task_id, summary, EvalConfig.from_yaml(manifest["eval_config"]), execution_error=bool(execution_error),
    )

    condition = {
        "engine": manifest.get("engine", "optimize"),
        "candidate_id": candidate_dir.name,
        "task_id": task_id,
        "seed": seed,
        "harness_run_id": run_id,
        "duration_s": round(time.time() - started, 3),
    }
    export.write_condition(run_dir, condition)
    output_file = run_dir / "cases" / task_id / "stdout.log"
    output = output_file.read_text(encoding="utf-8", errors="replace") if output_file.is_file() else ""
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
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        payload = run_case(manifest, args.task_id, Path(args.candidate_dir), args.seed)
        print(json.dumps(payload, default=str))
        return 0
    except Exception as exc:  # JSON on stdout is the rollout protocol.
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
