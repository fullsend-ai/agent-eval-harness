"""The one file coupled to cap-evolve's API/CLI/on-disk conventions.

Everything engine-agnostic (benchmark intake, candidate resolution, running one
rollout, exporting results, redacting gold) lives in ``agent_eval.optimize``
instead — this module only adds cap-evolve-specific validation, the intake
phase's project scaffolding, and the check/estimate/launch/status/apply
dispatch, plus the CLI entry point (``agent-eval-capevolve``).

Pointed at any benchmark checkout with an ``eval.yaml`` (e.g. rfe-creator),
``agent-eval-capevolve`` performs cap-evolve's ``intake`` phase itself: it
mines the benchmark, resolves every NEEDED input from the benchmark's own
files (or stops with ``BLOCKED:`` lines in ``PROJECT.md``), and writes
``.capevolve/project/`` — cap-evolve's own ``templates/project`` tree plus the
generated seam (``adapters/adapter.py``), ``capevolve.yaml``, ``PROJECT.md``
and ``intake.json``. Neither the benchmark nor cap-evolve is modified.
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv before PyYAML/cap-evolve

import argparse
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import yaml

from agent_eval.config import EvalConfig, load_raw
from agent_eval.optimize import artifact, export, intake

_SEARCH_KEYS = {
    "num_trials", "max_iterations", "stall", "max_usd", "max_metric_calls",
    "max_optimizer_usd", "stop_at_reward", "stop_condition", "store",
    "protected_paths", "target_model", "runner_model", "algorithm_skill",
    "algorithm_focus", "dataset_source", "split_ids_file", "memory_skill",
    "orchestration_mode", "capabilities", "actions", "optimizer_skill",
    "optimizer_model", "optimizer_instructions_file", "observers",
}
_SEARCH_PREFIXES = ("gate_", "split_", "optimizer_")
_BRIDGE_TEMPLATE = Path(__file__).with_name("_bridge_template.py")
# Execution substrates for each rollout. evalhub is structurally incompatible
# (no --cases: it runs the whole dataset per invocation).
SUBSTRATES = ("local", "harbor", "evalhub")
HARBOR_ENVS = ("podman", "kubernetes", "openshift")
_UNSUPPORTED_SUBSTRATES = {
    "evalhub": "structurally incompatible with per-case rollouts (no --cases; it runs "
               "the whole dataset per invocation)",
}
# A pilot launch is deliberately capped at a few iterations, not left
# open-ended: it is a 3-case smoke run, not a real campaign, and each extra
# iteration multiplies real spend (runner + judge + optimizer) across all
# three cases again.
_MAX_PILOT_ITERATIONS = 2

_RUN_DIR_POLL_TIMEOUT_S = 10.0
_RUN_DIR_POLL_INTERVAL_S = 0.2
_STEP_EVENT_KINDS = ("step", "step_indecisive", "finalize")
_MARKER = ".agent-eval-generated.json"
_CAPEVOLVE_EXCLUDE = ":(exclude).capevolve"


# ── Validation (cap-evolve-specific layer over artifact.validate_candidate_block) ──

def validate_optimize_block(raw: dict, config: EvalConfig) -> tuple[dict, list[dict]]:
    block = raw.get("optimize")
    if isinstance(block, dict):
        for key in block:
            if key in _SEARCH_KEYS or key.startswith(_SEARCH_PREFIXES):
                raise ValueError(
                    f"optimize.{key} is owned by cap-evolve search configuration; "
                    "set it with agent-eval-capevolve CLI flags instead"
                )
    if config.execution.mode not in {"case", "batch"}:
        raise ValueError("cap-evolve optimization supports execution.mode: case or batch")
    if not config.resolve_skill():
        raise ValueError("cap-evolve optimization supports skill evals; set execution.skill in eval.yaml")
    if config.execution.steps:
        raise ValueError("cap-evolve optimization does not support multi-step execution yet")
    if config.runner.type != "claude-code":
        raise ValueError("cap-evolve optimization currently supports runner.type: claude-code only")
    if not config.dataset.path:
        raise ValueError("dataset.path is required")

    block, components = artifact.validate_candidate_block(raw, config)
    # compose_reward() skips a weight whose judge produced no value, so a reward
    # naming a judge this config doesn't define (e.g. an overlay carried over
    # from another suite) would silently drop it from every measurement.
    judges = {j.name for j in config.judges}
    named = set((config.reward.weights or {}) if config.reward.judge is None else {config.reward.judge})
    unknown = sorted(named - judges)
    if unknown:
        raise ValueError(
            f"reward names judge(s) this eval config does not define: {', '.join(unknown)}; "
            f"defined: {', '.join(sorted(judges))}"
        )
    apply_path = artifact.relative_path(str(block.get("apply_to") or "").rstrip("/"), "optimize.apply_to")
    if intake.source_for_apply_to(config.project_root, config, apply_path.as_posix()) is None:
        raise ValueError(
            "optimize.apply_to must be a skill root the runner loads skills from (.claude/skills, "
            "or a runner.plugin_dirs skill root such as skills), or one skill directory inside it"
        )
    return block, components


def _read_agent_input(case_dir: Path):
    for suffix in (".yaml", ".yml", ".json"):
        path = case_dir / f"input{suffix}"
        if path.is_file() and not path.is_symlink():
            return yaml.safe_load(path.read_text(encoding="utf-8"))
    raise ValueError(f"case has no regular input.yaml, input.yml, or input.json: {case_dir}")


# ── Scaffolding (cap-evolve's intake phase) ──────────────────────────────────

def _cap_evolve_templates(skills_dir: str | None) -> Path | None:
    """cap-evolve's ``templates/project`` tree, next to its skills tree or package."""
    roots = [Path(skills_dir).resolve().parent] if skills_dir else []
    spec = importlib.util.find_spec("cap_evolve")
    if spec is not None and spec.origin:
        roots += list(Path(spec.origin).resolve().parents)
    for root in roots:
        template = root / "templates" / "project"
        if template.is_dir():
            return template
    return None


def _skill_location(project_root: Path, config: EvalConfig, apply_to: str) -> dict:
    source = intake.source_for_apply_to(project_root, config, apply_to)
    plugin = source["plugin_dir"]
    return {
        "skill_root": str(source["root"]),
        "skill_roots": [str(s["root"]) for s in intake.skill_sources(project_root, config)
                        if s["plugin_dir"] == plugin] or [str(source["root"])],
        "plugin_dir": str(plugin) if plugin is not None else None,
    }


def _check_harbor_substrate(config: EvalConfig, harbor_env: str, image: str | None) -> None:
    """NEEDED inputs of the Harbor substrate — blocked, never guessed."""
    blocked = []
    if not image:
        blocked.append({"input": "harbor task image", "why": "--runner harbor needs the task image",
                        "how": "build deploy/Containerfile (see deploy/harbor/README.md) and pass --image"})
    if importlib.util.find_spec("harbor") is None:
        blocked.append({"input": "harbor", "why": f"harbor is not importable by {sys.executable}",
                        "how": "install it into the harness venv (Python >= 3.12): pip install -e '.[all]'"})
    if harbor_env == "podman" and shutil.which("podman") is None:
        blocked.append({"input": "podman", "why": "podman is not on PATH",
                        "how": "install podman, or pass --env kubernetes|openshift"})
    if not config.models.skill:
        blocked.append({"input": "runner model", "why": "Harbor needs an explicit model for the agent",
                        "how": "set models.skill in the eval config"})
    if blocked:
        raise intake.IntakeBlocked(blocked)


def _project_dir_for(project_root: Path) -> Path:
    return Path(project_root) / ".capevolve" / "project"


def _ensure_project_dir(project_dir: Path) -> None:
    if project_dir.exists() and not (project_dir / _MARKER).exists():
        raise FileExistsError(
            f"refusing to overwrite an existing cap-evolve project without the marker: {project_dir}"
        )
    project_dir.mkdir(parents=True, exist_ok=True)


def _write_blocked(project_dir: Path, blocked: list[dict]) -> None:
    lines = ["# cap-evolve project — intake BLOCKED", "",
             "Written by `agent-eval-capevolve` (cap-evolve's intake phase). A NEEDED",
             "input could not be resolved from the benchmark; nothing was scaffolded and",
             "no budget was spent.", ""]
    lines += [f"BLOCKED: {b['input']} — {b['why']} — {b['how']}" for b in blocked]
    # A spec left by an earlier successful intake must not stay launchable.
    (project_dir / "capevolve.yaml").unlink(missing_ok=True)
    (project_dir / "PROJECT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (project_dir / "intake.json").write_text(json.dumps({
        "usd": 0.0, "tokens": 0, "seconds": 0.0, "blocked": blocked,
        "output_summary": "intake blocked: " + ", ".join(b["input"] for b in blocked),
    }, indent=2) + "\n", encoding="utf-8")
    (project_dir / _MARKER).write_text(json.dumps({"generated_by": "agent-eval-capevolve", "version": 3}) + "\n")


def _instructions(template_dir: Path | None, facts: str) -> str:
    """cap-evolve's optimizer template, placeholders intact, plus the benchmark facts."""
    template = template_dir / "optimizer" / "INSTRUCTIONS.md" if template_dir else None
    base = template.read_text(encoding="utf-8") if template and template.is_file() else ""
    return base.rstrip("\n") + ("\n\n" if base else "") + facts


def _benchmark_facts(manifest: dict) -> str:
    """Benchmark context for the proposer — deliberately without benchmark paths:
    the checkout and the run records hold gold answers and judge rationale."""
    skills = ", ".join(f"`{c['dest']}`" for c in manifest["candidate"])
    return (
        "## Benchmark facts (agent-eval-harness)\n\n"
        f"- Eval `{manifest['eval_name']}`: the skill under test is "
        f"`{manifest['execution_skill']}`, run in `{manifest['execution_mode']}` mode by "
        f"`{manifest['runner_type']}`.\n"
        f"- Editable capability: the skill package(s) {skills}, live at "
        f"`{manifest['apply_to']}/` in the agent's workspace. Edit nothing else: the dataset, "
        "the eval config and its judges, and the adapter are sealed.\n"
        "- Scoring source: the eval config's own judges. Each rollout's `summary.yaml` "
        "per-case judge results are composed into one reward by the harness's "
        "`compose_reward()` over the `reward:` block. Feedback carries judge labels "
        "(pass/fail, scores), never judge rationale, which can quote gold content.\n"
        "- Each rollout's transcript is in `./trajectories/`.\n"
        f"- The skills' scripts resolve these project paths at run time: "
        f"{', '.join(f'`{r}`' for r in manifest['runtime_resources']) or 'none'}.\n"
    )


def _project_md(manifest: dict, resolved: dict, spec: dict, runs_dir: Path, substrate: dict) -> str:
    credentials = intake.detected_credentials()
    overlay = resolved["overlay"]
    defaulted = resolved["defaulted"]
    recommended = [
        f"splits — ratio {spec['split_train']}/{spec['split_val']}/{spec['split_test']} "
        f"(seed {spec['split_seed']})" if "split_ids_file" not in spec
        else "splits — pinned in split_ids.json (pilot: one case per split)",
        f"num_trials={spec['num_trials']}" + (
            " — single-trial scores, so the significance gate will correctly reject "
            "marginal gains" if spec["num_trials"] == 1 else ""),
        f"budget — max_usd={spec['max_usd']}, max_iterations={spec['max_iterations']}, stall={spec['stall']}",
        f"optimizer — {spec['optimizer_skill']}"
        + (f" ({spec['optimizer_model']})" if spec["optimizer_model"] else ""),
        f"gate — {spec['gate_mode']}, k_se={spec['gate_k_se']}",
        f"memory_skill — {spec['memory_skill']}",
        f"target_model — {spec['target_model'] or 'unset (profile-agnostic)'}",
        "orchestration_mode — deterministic; github_integration — off",
    ]
    if "reward" in defaulted:
        recommended.append(
            "reward — every judge weighted equally with no boolean gate (the eval config "
            "has no reward: block); edit the overlay to weight what matters"
        )
    if "optimize" in defaulted:
        recommended.append(
            f"candidate — `{manifest['execution_skill']}` plus the project skills it invokes "
            "(the eval config has no optimize: block); edit the overlay to narrow it"
        )
    lines = [
        f"# cap-evolve project — {manifest['eval_name']}",
        "",
        "Written by `agent-eval-capevolve` (cap-evolve's intake phase) from the benchmark's",
        "own eval config. Regenerated on every invocation — do not hand-edit; change the",
        "eval config, its overlay, or the CLI flags instead.",
        "",
        "## What we're optimizing",
        "- Capability: skill-package",
        "- Artifact: " + ", ".join(f"`{c['source']}`" for c in manifest["candidate"]),
        f"- Live at: `{manifest['apply_to']}/` inside each rollout workspace",
        "- Allowed edits: edit",
        "",
        "## How we run the target (the RUNNER)",
        f"- Agent under test: `{manifest['runner_type']}` running skill "
        f"`{manifest['execution_skill']}` ({manifest['execution_mode']} mode), model "
        f"`{manifest['target_model'] or 'eval config default'}`",
        "- How `run_target` invokes it: `python -m agent_eval.optimize.rollout` for ONE case — "
        + (
            "the harness's own workspace.py → execute.py → collect.py → score.py, in a private "
            "workspace with the candidate overlaid"
            if substrate["runner"] == "local" else
            f"on Harbor (`--env {substrate['env']}`, image `{substrate['image']}`): a private task "
            "package generated per rollout (gold scrubbed), the candidate delivered as `--skill` "
            "(uploaded into the trial, no image rebuild), judged by the in-container verifier and "
            "read back from `summary.yaml` — never judged twice. Project `hooks/` and `.claude/` "
            "subdirectories other than skills are not shipped into the trial"
            + ("; Kubernetes/OpenShift is unverified for optimization" if substrate["env"] != "podman" else "")
        ),
        "- Credentials: " + (", ".join(credentials) + " detected" if credentials else
                             "no API-key variable detected; Claude Code's own login is used if present"),
        "",
        "## How we score",
        "- Metric: the eval config's judges composed by `compose_reward()` over its `reward:` "
        "block (reward in [0,1])",
        "- Feedback signal: per-judge labels (pass/fail, score) only — judge rationale is "
        "redacted because it can quote gold content",
        "- Infra failure or an errored judge: missing data (`raw={\"errored\": true}`), never reward 0",
        "",
        "## Data",
        f"- Source: \"adapter\" — {len(manifest['task_ids'])} cases from the eval config's dataset",
        "- Split: train/val/test — test is sealed, scored once at finalize.",
        "- Trajectories: `trajectories()` returns None (cap-evolve's per-rollout JSON, which "
        "embeds each transcript). The harness run records mix every split, so handing them to "
        f"the optimizer would leak the sealed test set; they stay at `{runs_dir}/` for humans.",
        "",
        "## Eval config",
        f"- Base: `{manifest['base_eval_config']}` (not modified)",
        (f"- Overlay: `{overlay}` (" + ("reused as-is" if resolved["reused_overlay"]
                                         else "generated: " + ", ".join(defaulted)) + ")")
        if overlay else "- Overlay: none (the eval config already declares optimize: and reward:)",
        "",
        "## Optimizer + algorithm",
        f"- Optimizer (proposer): {spec['optimizer_skill']}",
        f"- Algorithm: {spec['algorithm_skill']} (--focus {spec['algorithm_focus']})",
        f"- Budget: {spec['max_iterations']} iterations / ${spec['max_usd']}",
        "",
        "## Inputs status",
        "- NEEDED inputs resolved: tasks dataset, target agent, scorer, scoring source, "
        "trajectories path, capability artifact",
        "- RECOMMENDED inputs defaulted:",
        *[f"  - {item}" for item in recommended],
        "",
    ]
    return "\n".join(lines)


def prepare_project(
    config_path: str | Path | None = None,
    *,
    max_usd: float,
    max_iterations: int = 10,
    num_trials: int = 1,
    split_ratios: tuple[float, float, float] = (0.5, 0.25, 0.25),
    seed: int = 0,
    optimizer: str = "claude-code",
    optimizer_model: str = "",
    pilot_case_ids: tuple[str, str, str] | None = None,
    runner_budget_usd: float | None = None,
    runner: str = "local",
    harbor_env: str = "podman",
    image: str | None = None,
    skills_dir: str | None = None,
    reintake: bool = False,
) -> dict:
    """Run cap-evolve's intake phase against the benchmark in the current directory.

    Resolves the effective eval config (writing an ``extends:`` overlay when the
    benchmark lacks ``optimize:``/``reward:``), then scaffolds
    ``.capevolve/project/``. Raises :class:`intake.IntakeBlocked` — after
    recording ``BLOCKED:`` lines in ``PROJECT.md`` — when a NEEDED input cannot
    be resolved from the benchmark.
    """
    if max_usd <= 0:
        raise ValueError("--max-usd must be positive; cap-evolve uses 0 for unlimited spend")
    if max_iterations < 1 or num_trials < 1:
        raise ValueError("--max-iterations and --num-trials must be positive")
    if len(split_ratios) != 3 or any(r <= 0 for r in split_ratios) or abs(sum(split_ratios) - 1) > 1e-6:
        raise ValueError("--split-ratios must be three positive values summing to 1")
    if runner_budget_usd is not None and runner_budget_usd <= 0:
        raise ValueError("--runner-budget-usd must be positive")
    if pilot_case_ids and len(pilot_case_ids) != 3:
        raise ValueError("a pilot run uses exactly three cases (train, val, test)")
    if runner in _UNSUPPORTED_SUBSTRATES:
        raise ValueError(f"--runner {runner}: {_UNSUPPORTED_SUBSTRATES[runner]}")
    if runner == "harbor" and harbor_env not in HARBOR_ENVS:
        raise ValueError(f"--env must be one of {', '.join(HARBOR_ENVS)}")

    started = time.time()
    project_root = Path.cwd().resolve()
    project_dir = _project_dir_for(project_root)
    running = read_campaign_manifest(project_dir) if project_dir.is_dir() else None
    if running and _is_alive(running["pid"], running.get("create_time")):
        # Its rollout subprocesses re-read candidate.json/substrate.json: a
        # rewrite mid-campaign would change the cases or substrate under it.
        raise RuntimeError(
            f"a campaign is still running (pid {running['pid']}, {running.get('run_dir')}); "
            f"wait for it (--status) or stop it ({running.get('stop_hint')}) before re-running intake"
        )
    _ensure_project_dir(project_dir)
    try:
        base_config = intake.find_eval_config(project_root, config_path)
        resolved = intake.run_intake(project_root, base_config, reintake=reintake)
        config_path = Path(resolved["config_path"])
        raw, _chain = load_raw(config_path)
        config = EvalConfig.from_yaml(config_path)
        optimize, components = validate_optimize_block(raw, config)
        if runner == "harbor":
            _check_harbor_substrate(config, harbor_env, image)
    except intake.IntakeBlocked as exc:
        _write_blocked(project_dir, exc.blocked)
        raise
    except (ValueError, FileNotFoundError) as exc:
        blocked = [{"input": "eval config", "why": str(exc),
                    "how": "fix the eval config or its .capevolve overlay, then re-run"}]
        _write_blocked(project_dir, blocked)
        raise intake.IntakeBlocked(blocked) from exc

    cases_dir = config.resolve_path(config.dataset.path)
    all_case_dirs = sorted(path for path in cases_dir.iterdir() if path.is_dir()) if cases_dir.is_dir() else []
    if not all_case_dirs:
        blocked = [{"input": "tasks dataset", "why": f"dataset has no case directories: {cases_dir}",
                    "how": "generate cases with /eval-dataset or fix dataset.path"}]
        _write_blocked(project_dir, blocked)
        raise intake.IntakeBlocked(blocked)
    if pilot_case_ids:
        if len(set(pilot_case_ids)) != 3:
            raise ValueError("--pilot-case-ids must name three distinct cases")
        by_id = {path.name: path for path in all_case_dirs}
        missing = [case_id for case_id in pilot_case_ids if case_id not in by_id]
        if missing:
            raise ValueError(f"pilot case id(s) not found in dataset: {', '.join(missing)}")
        case_dirs = [by_id[case_id] for case_id in pilot_case_ids]
    else:
        case_dirs = all_case_dirs
    task_inputs = {path.name: _read_agent_input(path) for path in case_dirs}

    harness_root = Path(__file__).resolve().parents[2]
    template_dir = _cap_evolve_templates(skills_dir)
    if template_dir is not None:
        # cap-evolve's intake copies its whole templates/project tree; the
        # generated files below then replace the stubs it ships.
        shutil.copytree(template_dir, project_dir, dirs_exist_ok=True)
    adapters_dir = project_dir / "adapters"
    adapters_dir.mkdir(parents=True, exist_ok=True)

    seed_dir = project_dir / "seed_capability"
    artifact.snapshot_candidate(components, seed_dir)

    # adapters/substrate.json — narrow: only the execution substrate, nothing
    # about the dataset/candidate (a file, not env vars, because a --resume
    # days later in a new shell would otherwise inherit whatever happens to be
    # exported, silently changing substrate mid-campaign).
    substrate = {
        "runner": runner,
        "env": harbor_env if runner == "harbor" else None,
        "image": image if runner == "harbor" else None,
        "harness_python": sys.executable,
    }
    (adapters_dir / "substrate.json").write_text(
        json.dumps(substrate, indent=2) + "\n", encoding="utf-8",
    )
    # adapters/candidate.json — the task/candidate manifest the bridge and the
    # rollout subprocess both read.
    candidate_manifest = {
        "harness_root": str(harness_root),
        "project_root": str(project_root),
        "eval_config": str(config_path),
        "base_eval_config": str(resolved["base_config"]),
        "eval_name": config.eval_name(),
        "execution_skill": config.resolve_skill(),
        "execution_mode": config.execution.mode,
        "runner_type": config.runner.type,
        "target_model": config.models.skill or "",
        "task_ids": [path.name for path in case_dirs],
        "task_inputs": task_inputs,
        "apply_to": str(optimize["apply_to"]).rstrip("/"),
        # "source" (the real project path each component was seeded from) is
        # needed by apply_winner() to copy a winning candidate back — the
        # inverse of the seed_capability copy done above.
        "candidate": [
            {"dest": item["dest"], "source": item["source"], "is_dir": item["is_dir"]}
            for item in components
        ],
        "runtime_resources": intake.runtime_resources(project_root, config, components),
        # Where the candidate is live at run time: the project's .claude/skills
        # (staged into the rollout workspace) or a runner plugin's skill root
        # (a private copy of the plugin per rollout).
        **_skill_location(project_root, config, optimize["apply_to"]),
        "runner_budget_usd": runner_budget_usd,
        # Harbor's task-level agent timeout (the local runner reads it from
        # the eval config itself).
        "agent_timeout_s": config.execution.timeout,
        "feedback": optimize.get("feedback", "redacted"),
        "on_error": optimize.get("on_error", "noise"),
        "engine": "cap-evolve",
    }
    (adapters_dir / "candidate.json").write_text(
        json.dumps(candidate_manifest, indent=2, default=str) + "\n", encoding="utf-8",
    )
    shutil.copy2(_BRIDGE_TEMPLATE, adapters_dir / "aeh_bridge.py")
    (adapters_dir / "adapter.py").write_text(
        "from pathlib import Path\n"
        "from aeh_bridge import Bridge\n\n"
        "class Adapter(Bridge):\n"
        "    def __init__(self):\n"
        "        super().__init__(Path(__file__).with_name('substrate.json'))\n",
        encoding="utf-8",
    )

    optimizer_dir = project_dir / "optimizer"
    optimizer_dir.mkdir(exist_ok=True)
    instructions_path = optimizer_dir / "INSTRUCTIONS.md"
    runs_dir = export.runs_dir_for(candidate_manifest)
    instructions_path.write_text(
        _instructions(template_dir, _benchmark_facts(candidate_manifest)), encoding="utf-8",
    )

    spec = {
        "capabilities": ["skill-package"],
        "capability_path": "seed_capability",
        "actions": ["edit"],
        "optimizer_skill": optimizer,
        "optimizer_model": optimizer_model,
        "algorithm_skill": "hill-climb",
        "algorithm_focus": "all",
        "orchestration_mode": "deterministic",
        "memory_skill": "md-files",
        "dataset_source": "adapter",
        "split_seed": seed,
        "split_train": split_ratios[0],
        "split_val": split_ratios[1],
        "split_test": split_ratios[2],
        "num_trials": num_trials,
        "gate_mode": "paired",
        "gate_k_se": 1.0,
        "max_iterations": max_iterations,
        "stall": 0,
        "max_usd": max_usd,
        "store": "git",
        # Absolute: a relative value resolves project-relative under `check`
        # but cwd-relative under `run`, which then silently falls back to the
        # generic template (cap-evolve issue #252).
        "optimizer_instructions_file": str(instructions_path),
        # Deliberately empty: the benchmark checkout holds the dataset (gold
        # answers) and the eval config's judge prompts, and the proposer must
        # never read either (optimize.feedback: redacted).
        "runner_repo_path": "",
        "capability_sources": [],
        "target_model": config.models.skill or "",
        "runner_model": config.models.skill or "",
        # Tamper seal: "default" is a real cap-evolve sentinel expanding to
        # integrity.DEFAULT_PATTERNS. Omitting this key entirely leaves
        # dataset/judge/scorer files fully editable by the optimizer.
        "protected_paths": "default",
    }
    if config.mlflow.experiment:
        # Loaded automatically by cap-evolve's own baseline phase — no extra
        # invocation needed on our side.
        spec["observers"] = [{
            "backend": "mlflow",
            "tracking_uri": config.mlflow.tracking_uri,
            "experiment_name": config.mlflow.experiment,
        }]
    split_ids_path = project_dir / "split_ids.json"
    if pilot_case_ids:
        split_ids = {
            "train": [pilot_case_ids[0]],
            "val": [pilot_case_ids[1]],
            "test": [pilot_case_ids[2]],
        }
        split_ids_path.write_text(json.dumps(split_ids, indent=2) + "\n", encoding="utf-8")
        spec["split_ids_file"] = "split_ids.json"
    else:
        # A prior pilot invocation against this same project dir may have left
        # this behind. capevolve.yaml below won't reference it, but a stale
        # file is confusing to find later — remove it so the project dir only
        # ever reflects the most recent invocation.
        split_ids_path.unlink(missing_ok=True)
    (project_dir / "capevolve.yaml").write_text(
        yaml.safe_dump(spec, sort_keys=False), encoding="utf-8"
    )
    (project_dir / "PROJECT.md").write_text(
        _project_md(candidate_manifest, resolved, spec, runs_dir, substrate), encoding="utf-8",
    )
    # Read by `cap-evolve run` into the run's spend ledger. Intake here is
    # mechanical — no LLM calls — so it costs nothing.
    (project_dir / "intake.json").write_text(json.dumps({
        "usd": 0.0, "tokens": 0, "seconds": round(time.time() - started, 3),
        "output_summary": (
            f"agent-eval-harness intake: {len(case_dirs)} cases, "
            f"{len(components)} candidate component(s), overlay="
            f"{resolved['overlay'] or 'none'}"
        ),
        "implemented": ["tasks", "run_target", "score"],
    }, indent=2) + "\n", encoding="utf-8")
    (project_dir / _MARKER).write_text(json.dumps({"generated_by": "agent-eval-capevolve", "version": 3}) + "\n")

    cost_hint = _calibrate_cost_from_past_runs(config, project_root)
    result = {
        "project_dir": str(project_dir),
        "spec_path": str(project_dir / "capevolve.yaml"),
        "adapter_path": str(adapters_dir / "adapter.py"),
        "eval_config": str(config_path),
        "overlay": resolved["overlay"],
        "defaulted": resolved["defaulted"],
        "task_count": len(case_dirs),
        "candidate_components": [item["dest"] for item in components],
        "runtime_resources": candidate_manifest["runtime_resources"],
        "cap_evolve_templates": str(template_dir) if template_dir else None,
        "splits": {
            "train": round(len(case_dirs) * split_ratios[0]),
            "val": round(len(case_dirs) * split_ratios[1]),
            "test": round(len(case_dirs) * split_ratios[2]),
        },
        "gate_mode": spec["gate_mode"],
        "gate_k_se": spec["gate_k_se"],
    }
    if cost_hint is not None:
        result["observed_cost_per_rollout"] = cost_hint
    return result


def _calibrate_cost_from_past_runs(config: EvalConfig, project_root: Path) -> float | None:
    """Average runner cost of ONE case (one rollout) from this eval's prior runs.

    A run's ``cost_usd`` covers every case it ran, so it is divided by its case
    count: one for this integration's own rollouts (they carry a
    ``condition.json``), else the ``per_case`` entries. A run whose case count
    is unknown is skipped rather than guessed. Judge spend is not included.
    """
    base = Path(os.environ.get("AGENT_EVAL_RUNS_DIR", "eval/runs"))
    if not base.is_absolute():
        base = project_root / base
    eval_runs_dir = base.resolve() / config.eval_name()
    if not eval_runs_dir.is_dir():
        return None
    costs = []
    for run_dir in eval_runs_dir.iterdir():
        result_path = run_dir / "run_result.json"
        if not result_path.is_file():
            continue
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cost = data.get("cost_usd")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost <= 0:
            continue
        n_cases = 1 if (run_dir / "condition.json").is_file() else len(data.get("per_case") or {})
        if n_cases:
            costs.append(float(cost) / n_cases)
    if not costs:
        return None
    return round(sum(costs) / len(costs), 4)


# ── cap-evolve subprocess dispatch (check/estimate) ─────────────────────────

def _child_env(harness_root: Path) -> dict:
    env = dict(os.environ)
    old_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(harness_root) + (os.pathsep + old_path if old_path else "")
    env.pop("_AGENT_EVAL_BOOTSTRAP_DONE", None)
    return env


def _cap_evolve(args: list[str], *, cwd: Path, harness_root: Path, stream_stderr: bool = False) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "cap_evolve.cli", *args],
        cwd=cwd,
        env=_child_env(harness_root),
        stdout=subprocess.PIPE,
        stderr=None if stream_stderr else subprocess.PIPE,
        text=True,
    )
    # cap-evolve's stdout is a strict JSON contract; stderr is human chrome
    # only and is never parsed for results.
    if not proc.stdout.strip():
        raise RuntimeError("cap-evolve returned empty stdout")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"cap-evolve stdout was not valid JSON: {exc}") from exc
    if proc.returncode:
        raise RuntimeError(json.dumps(payload, indent=2) if payload else "cap-evolve command failed")
    return payload


# ── Process liveness (pid + creation-time marker) ───────────────────────────
# Reimplements skills/eval-run/scripts/execute.py's pidfile algorithm rather
# than importing it: skills/*/scripts/ are entry points that depend on
# agent_eval, not library code agent_eval should depend on back.

def _process_create_time(pid: int) -> str | None:
    try:
        with open("/proc/%d/stat" % pid, encoding="utf-8") as fh:
            data = fh.read()
        after = data[data.rfind(")") + 2:].split()
        return after[19]
    except (OSError, IndexError, ValueError):
        pass
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5,
                env={"TZ": "UTC", "LC_ALL": "C", "PATH": "/bin:/usr/bin"},
            )
            return out.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def _is_alive(pid: int, create_time: str | None) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else — treat as alive
    except OSError:
        return False
    if create_time is None:
        return True  # no marker to compare against; don't false-negative
    return _process_create_time(pid) == create_time


# ── Campaign manifest (the pidfile) ─────────────────────────────────────────

def _campaign_manifest_path(project_dir: Path) -> Path:
    return Path(project_dir) / "campaign.json"


def _write_campaign_manifest(project_dir: Path, manifest: dict) -> None:
    path = _campaign_manifest_path(project_dir)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, default=str)


def read_campaign_manifest(project_dir: Path) -> dict | None:
    path = _campaign_manifest_path(project_dir)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ── Launch ───────────────────────────────────────────────────────────────────

def _wait_for_new_run_dir(
    runs_root: Path, before: set[str], proc: subprocess.Popen, log_file: Path,
    *, timeout_s: float = _RUN_DIR_POLL_TIMEOUT_S, interval_s: float = _RUN_DIR_POLL_INTERVAL_S,
) -> Path:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        new = {p.name for p in runs_root.glob("run_*")} - before
        if len(new) == 1:
            return runs_root / next(iter(new))
        if len(new) > 1:
            raise RuntimeError(f"ambiguous new cap-evolve run directories: {sorted(new)}")
        if proc.poll() is not None:
            raise RuntimeError(
                f"cap-evolve exited immediately (code {proc.returncode}) before "
                f"creating a run directory; see {log_file}"
            )
        time.sleep(interval_s)
    raise RuntimeError(
        f"cap-evolve did not create a run directory within {timeout_s}s; "
        f"it may still be starting — see {log_file}"
    )


def launch_detached(
    *, project_root: Path, project_dir: Path, spec_path: str, max_usd: float,
    max_iterations: int, skills_dir: str | None, harness_root: Path,
) -> dict:
    """Launch ``cap-evolve run`` as a detached background subprocess.

    Returns the campaign manifest (including how to stop it: ``pid``) once the
    run directory cap-evolve created for this launch has been identified on
    disk; raises if one never appears.
    """
    project_dir = Path(project_dir)
    runs_root = project_dir.parent  # <project_root>/.capevolve
    before = {p.name for p in runs_root.glob("run_*")}

    command = [
        sys.executable, "-m", "cap_evolve.cli", "run",
        "--project", str(project_dir), "--spec", spec_path,
        "--max-usd", str(max_usd), "--max-iterations", str(max_iterations),
    ]
    if skills_dir:
        command += ["--skills-dir", skills_dir]

    log_file = project_dir / f"campaign-{int(time.time())}.log"
    with open(log_file, "w", encoding="utf-8") as log_handle:
        proc = subprocess.Popen(
            command,
            cwd=project_root,
            env=_child_env(harness_root),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # survive the parent's terminal/session ending
        )
        # Popen dup()s log_handle's fd into the child before exec; closing our
        # copy when this `with` block exits does not affect the child's output.

    try:
        run_dir = _wait_for_new_run_dir(runs_root, before, proc, log_file)
    except RuntimeError:
        # Without a run dir there is no campaign record, so --status could not
        # see it and a retry would start a second one: stop it instead.
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        raise

    manifest = {
        "pid": proc.pid,
        "create_time": _process_create_time(proc.pid),
        "run_dir": str(run_dir),
        "log_file": str(log_file),
        "launched_at": time.time(),
        "max_usd": max_usd,
        "max_iterations": max_iterations,
        "stop_hint": f"kill {proc.pid}",
    }
    _write_campaign_manifest(project_dir, manifest)
    return manifest


# ── Status ───────────────────────────────────────────────────────────────────

def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _tail_latest_step_event(events_path: Path, max_bytes: int = 65536) -> dict | None:
    """Return the most recent step/finalize event from events.jsonl."""
    if not events_path.is_file():
        return None
    try:
        with open(events_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            chunk = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    latest = None
    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("kind") in _STEP_EVENT_KINDS:
            latest = event
    return latest


def _count_lines(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        with open(path, encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0


def _count_indecisive(events_path: Path) -> int:
    if not events_path.is_file():
        return 0
    count = 0
    try:
        with open(events_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("kind") == "step_indecisive":
                    count += 1
    except OSError:
        pass
    return count


def read_status(project_dir: Path) -> dict:
    """Report progress of the campaign launched in *project_dir*, if any.

    Accepted/rejected/indecisive counts come from cap-evolve's own
    history.jsonl/rejected.jsonl/events.jsonl: there's no single field in
    state.json carrying this breakdown (verified against cap-evolve's source —
    a line in history.jsonl means accepted, a line in rejected.jsonl means
    rejected, and indecisive candidates appear in neither file, only as a
    step_indecisive event).
    """
    manifest = read_campaign_manifest(Path(project_dir))
    if manifest is None:
        return {"status": "not_launched"}

    run_dir = Path(manifest["run_dir"])
    alive = _is_alive(manifest["pid"], manifest.get("create_time"))
    state = _read_json(run_dir / "state.json") or {}
    final = _read_json(run_dir / "final.json")
    latest_event = _tail_latest_step_event(run_dir / "events.jsonl")

    if final is not None:
        status = "finished"
    elif alive:
        status = "running"
    else:
        status = "stopped"

    return {
        "status": status,
        "pid": manifest["pid"],
        "alive": alive,
        "run_dir": str(run_dir),
        "log_file": manifest.get("log_file"),
        "stop_hint": manifest.get("stop_hint"),
        "launched_at": manifest.get("launched_at"),
        "budget": state.get("budget"),
        "spent": state.get("spent"),
        "best_id": state.get("best_id"),
        "iterations": {
            "accepted": _count_lines(run_dir / "history.jsonl"),
            "rejected": _count_lines(run_dir / "rejected.jsonl"),
            "indecisive": _count_indecisive(run_dir / "events.jsonl"),
        },
        "latest_event": latest_event,
        "final": final,
    }


# ── Apply ────────────────────────────────────────────────────────────────────

def _git(args: list[str], cwd: Path, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True, **kwargs)


def _replace_tracked_dir(src: Path, dest: Path, repo_root: Path) -> None:
    """Make ``dest`` match the winner without ever deleting a file git cannot
    restore: tracked files the winner dropped are removed, the winner's files
    are copied in, and untracked or ignored files are left alone."""
    winner = {path.relative_to(src) for path in src.rglob("*") if path.is_file()}
    tracked = _git(["ls-files", "-z", "--", str(dest)], cwd=repo_root).stdout.split("\0")
    for rel in filter(None, tracked):
        path = repo_root / rel
        if path.relative_to(dest) not in winner:
            path.unlink()
    shutil.copytree(src, dest, dirs_exist_ok=True)


def apply_winner(*, project_root: Path, harness_manifest: dict, campaign_manifest: dict) -> dict:
    """Copy the finished campaign's winning candidate onto a new git branch.

    Requires ``final.json`` (the campaign ran to completion and burned its
    sealed test evaluation) and a clean project working tree. Leaves the
    user's current branch and working tree untouched — the applied changes
    live only on the new branch for them to review or merge themselves.
    """
    run_dir = Path(campaign_manifest["run_dir"])
    final = _read_json(run_dir / "final.json")
    if final is None:
        raise RuntimeError(
            "campaign has not finished (no final.json yet) — check --status; "
            "--apply only works on a completed campaign"
        )
    best_id = final.get("best_id")
    if not best_id:
        raise RuntimeError(f"final.json has no best_id: {run_dir / 'final.json'}")
    if best_id == "seed":
        raise RuntimeError(
            "no candidate was accepted (best_id is the seed), so there is nothing to apply; "
            f"see {run_dir / 'report.md'} for why"
        )

    best_id = str(best_id)
    candidates_root = (run_dir / "candidates").resolve()
    candidate_dir = (candidates_root / best_id).resolve()
    if Path(best_id).name != best_id or candidate_dir.parent != candidates_root:
        # final.json is campaign output, not trusted: a traversal or absolute
        # id must not select files outside the candidates tree.
        raise RuntimeError(f"final.json names an invalid winner id: {best_id!r}")
    if not candidate_dir.is_dir():
        raise RuntimeError(f"winning candidate snapshot not found: {candidate_dir}")

    # Validate every destination before anything is touched: only paths inside
    # this repository (never the repository root or .git) can be restored by
    # the git rollback below.
    repo_root = Path(_git(["rev-parse", "--show-toplevel"], cwd=project_root).stdout.strip()).resolve()
    targets = []
    for component in harness_manifest["candidate"]:
        rel = artifact.relative_path(str(component["dest"]).rstrip("/"), "candidate dest")
        src = candidate_dir / rel
        dest = Path(component["source"]).resolve()
        if dest == repo_root or not dest.is_relative_to(repo_root) \
                or repo_root / ".git" in (dest, *dest.parents):
            raise RuntimeError(f"refusing to apply a candidate outside the repository: {dest}")
        if not src.exists():
            raise RuntimeError(f"winning candidate is missing component {component['dest']}: {src}")
        targets.append((src, dest, bool(component.get("is_dir"))))

    # The campaign's own state lives in the benchmark checkout under
    # .capevolve/, which the benchmark need not gitignore: it is never part
    # of "dirty", never committed, and never cleaned away on rollback.
    dirty = _git(["status", "--porcelain", "--", ".", _CAPEVOLVE_EXCLUDE], cwd=project_root).stdout.strip()
    if dirty:
        raise RuntimeError("project working tree is not clean; commit or stash your changes before --apply")
    current_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=project_root).stdout.strip()
    if current_branch == "HEAD":
        # Detached HEAD: return to the exact commit, not to "HEAD" (which
        # would by then name the new branch).
        current_branch = _git(["rev-parse", "HEAD"], cwd=project_root).stdout.strip()

    branch = f"capevolve/{run_dir.name}"
    _git(["checkout", "-b", branch], cwd=project_root)
    committed = False
    try:
        changed = []
        for src, dest, is_dir in targets:
            if is_dir:
                _replace_tracked_dir(src, dest, repo_root)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
            changed.append(str(dest))

        _git(["add", "-A", "--", *changed], cwd=project_root)
        message = (
            f"cap-evolve: apply winning candidate {best_id}\n\n"
            f"Run: {run_dir.name}\nTest delta: {final.get('test_delta')}\n"
        )
        _git(["commit", "-m", message], cwd=project_root)
        committed = True
    finally:
        if not committed:
            # Something failed mid-copy, before any commit landed on the new
            # branch. Wipe the partial copy so switching back leaves the
            # user's original branch exactly as clean as --apply found it.
            _git(["reset", "--hard", "HEAD"], cwd=project_root)
            _git(["clean", "-fd", "--", ".", _CAPEVOLVE_EXCLUDE], cwd=project_root)
        _git(["checkout", current_branch], cwd=project_root)

    # The optimistic-bias caveat needs real split sizes. cap-evolve freezes
    # the split to run_dir/splits.json (confirmed on-disk file, not a
    # final.json field) as {"train": [ids], "val": [ids], "test": [ids]}.
    caveat = None
    splits = _read_json(run_dir / "splits.json") or {}
    train_ids = splits.get("train") or []
    val_ids = splits.get("val") or []
    test_ids = splits.get("test") or []
    total = len(train_ids) + len(val_ids) + len(test_ids)
    if total:
        seen = len(train_ids) + len(val_ids)
        caveat = (
            f"The sealed test delta ({final.get('test_delta')}) is the honest number. "
            f"A full /eval-run over the whole dataset will look better than that, because "
            f"{seen} of those {total} cases were seen during the search."
        )
    return {
        "branch": branch,
        "best_id": best_id,
        "changed_files": changed,
        "test_delta": final.get("test_delta"),
        "final": final,
        "caveat": caveat,
        "suggested_next_step": "/eval-run (confirmation run over the full dataset)",
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def _project_dir() -> Path:
    # Same path prepare_project() computes — fixed relative to cwd,
    # independent of --config, so --status/--apply need no eval.yaml.
    return Path.cwd().resolve() / ".capevolve" / "project"


def _require_project_dir() -> Path:
    project_dir = _project_dir()
    if not (project_dir / ".agent-eval-generated.json").is_file():
        raise RuntimeError(f"no cap-evolve project at {project_dir}; run --dry-run or launch first")
    return project_dir


def _cmd_status() -> dict:
    return read_status(_require_project_dir())


def _cmd_apply() -> dict:
    project_dir = _require_project_dir()
    campaign_manifest = read_campaign_manifest(project_dir)
    if campaign_manifest is None:
        raise RuntimeError("no campaign has been launched in this project; launch one first")
    harness_manifest = json.loads((project_dir / "adapters" / "candidate.json").read_text(encoding="utf-8"))
    return apply_winner(
        project_root=Path.cwd().resolve(), harness_manifest=harness_manifest, campaign_manifest=campaign_manifest,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="the benchmark's eval config (default: ./eval.yaml)")
    parser.add_argument("--max-usd", type=float, help="hard total campaign budget; required for --dry-run/launch")
    parser.add_argument("--max-iterations", type=int, default=10)
    parser.add_argument("--num-trials", type=int, default=1)
    parser.add_argument("--split-ratios", default="0.5,0.25,0.25")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--optimizer", default="claude-code",
                        help="cap-evolve optimizer name (mock for a zero-API dry run)")
    parser.add_argument("--optimizer-model", default="")
    parser.add_argument("--runner", default="local", choices=SUBSTRATES,
                        help="execution substrate for each rollout (evalhub is refused)")
    parser.add_argument("--env", default="podman", choices=HARBOR_ENVS,
                        help="Harbor environment for --runner harbor (default: podman)")
    parser.add_argument("--image", help="Harbor task image for --runner harbor")
    parser.add_argument("--skills-dir", default=os.environ.get("CAPEVOLVE_SKILLS_DIR"),
                        help="cap-evolve skills directory (default: $CAPEVOLVE_SKILLS_DIR)")
    parser.add_argument("--reintake", action="store_true",
                        help="regenerate the .capevolve eval-config overlay instead of reusing it")
    parser.add_argument("--pilot-case-ids", nargs=3, metavar=("TRAIN_ID", "VAL_ID", "TEST_ID"),
                        help="run a three-case smoke pilot with explicit train/val/test cases")
    parser.add_argument("--runner-budget-usd", type=float, help="per-target-invocation spend ceiling")
    parser.add_argument("--dry-run", action="store_true",
                        help="intake, scaffold, check, and estimate; spend nothing")
    parser.add_argument("--run", action="store_true",
                         help="explicit alias for launching (optional — bare --max-usd already launches)")
    parser.add_argument("--status", action="store_true", help="report progress of the launched campaign")
    parser.add_argument("--apply", action="store_true",
                         help="apply a finished campaign's winning candidate onto a new git branch")
    args = parser.parse_args(argv)

    explicit = sum([args.dry_run, args.status, args.apply])
    if explicit > 1:
        parser.error("choose exactly one of --dry-run, --status, or --apply")
    if explicit == 1 and args.run:
        parser.error("--run cannot be combined with --dry-run/--status/--apply")
    if args.dry_run:
        mode = "dry_run"
    elif args.status:
        mode = "status"
    elif args.apply:
        mode = "apply"
    elif args.run or args.max_usd is not None:
        mode = "launch"
    else:
        parser.error("choose --dry-run, --status, --apply, or provide --max-usd to launch")

    if mode in ("status", "apply"):
        try:
            result = _cmd_status() if mode == "status" else _cmd_apply()
            print(json.dumps(result, indent=2, default=str))
            return 0
        except Exception as exc:
            print(f"agent-eval-capevolve: {exc}", file=sys.stderr)
            return 1

    if args.runner in _UNSUPPORTED_SUBSTRATES:
        parser.error(f"--runner {args.runner}: {_UNSUPPORTED_SUBSTRATES[args.runner]}")
    if args.max_usd is None:
        # cap-evolve has no stop command; a budget cap is the only graceful
        # halt for a process that outlives the session.
        parser.error("--max-usd is required for --dry-run/launch")
    if args.pilot_case_ids and (
        not (1 <= args.max_iterations <= _MAX_PILOT_ITERATIONS) or args.num_trials != 1
    ):
        parser.error(
            f"pilot runs require --max-iterations between 1 and {_MAX_PILOT_ITERATIONS} "
            "and --num-trials 1"
        )
    try:
        ratios = tuple(float(item) for item in args.split_ratios.split(","))
    except ValueError:
        parser.error("--split-ratios must be three comma-separated numbers")

    try:
        generated = prepare_project(
            args.config,
            max_usd=args.max_usd,
            max_iterations=args.max_iterations,
            num_trials=args.num_trials,
            split_ratios=ratios,
            seed=args.seed,
            optimizer=args.optimizer,
            optimizer_model=args.optimizer_model,
            pilot_case_ids=tuple(args.pilot_case_ids) if args.pilot_case_ids else None,
            runner_budget_usd=args.runner_budget_usd,
            runner=args.runner,
            harbor_env=args.env,
            image=args.image,
            skills_dir=args.skills_dir,
            reintake=args.reintake,
        )
        project_dir = Path(generated["project_dir"])
        harness_root = Path(__file__).resolve().parents[2]
        checked = _cap_evolve(["check", str(project_dir)], cwd=Path.cwd(), harness_root=harness_root)
        if not checked.get("ok"):
            raise RuntimeError(json.dumps(checked, indent=2))
        estimate = _cap_evolve(
            ["estimate", "--project", str(project_dir), "--spec", generated["spec_path"]],
            cwd=Path.cwd(), harness_root=harness_root,
        )
        if mode == "dry_run":
            print(json.dumps({"scaffold": generated, "check": checked, "estimate": estimate}, indent=2))
            return 0

        launched = launch_detached(
            project_root=Path.cwd().resolve(),
            project_dir=project_dir,
            spec_path=generated["spec_path"],
            max_usd=args.max_usd,
            max_iterations=args.max_iterations,
            skills_dir=args.skills_dir,
            harness_root=harness_root,
        )
        print(json.dumps(
            {"scaffold": generated, "estimate": estimate, "launched": launched}, indent=2, default=str,
        ))
        return 0
    except intake.IntakeBlocked as exc:
        # Non-interactive intake discipline: a blocked-but-honest stop, with
        # the BLOCKED lines also written to .capevolve/project/PROJECT.md.
        print(json.dumps({
            "blocked": exc.blocked,
            "project_md": str(_project_dir() / "PROJECT.md"),
        }, indent=2))
        return 1
    except Exception as exc:
        print(f"agent-eval-capevolve: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
