"""Generate self-contained Harbor task packages from an eval.yaml dataset.

Each task package is a directory that any Harbor agent (Claude Code, OpenCode,
Codex, etc.) can run directly via ``harbor run -p <task> --agent <agent>``.
No custom agent wrapper needed — all setup (inputs, tool interception hooks,
project resources) lives in ``environment/``, which Harbor auto-uploads to the
agent workspace at trial start.

Both entry points use this:
- ``/eval-dataset`` → ``skills/eval-dataset/scripts/harbor.py``
  (a thin CLI wrapper around :func:`generate_tasks`),
- ``/eval-run --runner harbor`` → ``agent_eval.harbor.run`` imports and calls it.

Per case it emits::

    <out>/<case-id>/
      task.toml               # env image + verifier config
      instruction.md           # resolved skill command + input context
      tests/
        test.sh                # verifier: judge -> reward bridge
        eval.yaml              # bundled config for the judge engine
      environment/             # auto-uploaded to the agent workspace by Harbor
        input.yaml             # case input
        answers.yaml           # (if present)
        <dest>                 # shared dataset.workspace.files {dest, source}
                               #   entries, resolved from the local checkout
        hooks/tools.py         # tool interceptor script (if inputs.tools configured)
        tool_handlers.yaml     # handler config (if inputs.tools configured)
        .claude/settings.json  # PreToolUse hooks + permissions (Claude Code)

Project resources (skills, scripts, .context) are baked into the task image
at a known path and staged into the workspace by the image's entrypoint or
``[environment].workdir`` setup — not by the agent.
"""

import copy
import ast
import json
import re
import shlex
import shutil
from pathlib import Path

import yaml

from agent_eval.config import EvalConfig, load_raw, resolve_arguments
from agent_eval.judges import BuiltinJudgeRegistry
from agent_eval.tools.interception import generate_interception
from agent_eval.workspace_provisioning import (
    STAGING_VERSION,
    dataset_protected_paths,
    materialize_shared_files,
)

_TEMPLATES = Path(__file__).resolve().parent / "templates"


def _render(template_name: str, mapping: dict) -> str:
    text = (_TEMPLATES / template_name).read_text()
    return re.sub(
        r"@@([A-Za-z_][A-Za-z0-9_]*)@@",
        lambda match: str(mapping.get(match.group(1), match.group(0))),
        text,
    )


def _toml_string(value: object) -> str:
    """Serialize a value as a TOML basic string.

    ``json.dumps`` produces a quoted string whose escape set (``\\"``, ``\\\\``,
    ``\\b``, ``\\f``, ``\\n``, ``\\r``, ``\\t``, ``\\uXXXX``) is a subset of
    TOML's, so the result parses as a TOML basic string. The lone exception is
    U+007F (DEL): JSON emits it raw but TOML requires control characters to be
    escaped, so escape it explicitly.
    """
    return json.dumps(str(value), ensure_ascii=False).replace("\x7f", "\\u007f")


def _task_label(config) -> str:
    """One-line label for task.toml ``[task].description``.

    The agent sees ``instruction.md`` and the full text stays in the bundled
    ``eval.yaml``, so keep this to a single ~120-char line, marking truncation
    with an ellipsis instead of silently dropping characters.
    """
    label = config.description or config.name or "agent-eval task"
    if len(label) > 120:
        label = label[:119] + "…"
    return label


def _find_input_file(case_dir: Path):
    for suffix in (".yaml", ".yml", ".json"):
        candidate = case_dir / f"input{suffix}"
        if candidate.is_file():
            return candidate
    return None


def _bundle_eval_config(config_path: Path, judge_model: str | None = None,
                        no_llm_judges: bool = False) -> dict:
    """Load the eval.yaml and sanitize it for in-container verification.

    Goes through the single raw loader, so an `extends:` overlay is bundled
    as its MERGED mapping (no `extends` key): the in-container engine never
    resolves a path relative to the container.
    """
    raw, _chain = load_raw(config_path)
    if "dataset" in raw and isinstance(raw["dataset"], dict):
        raw["dataset"] = {**raw["dataset"], "path": ""}
    if judge_model:
        raw.setdefault("models", {})["judge"] = judge_model
    if no_llm_judges:
        judges = raw.get("judges") or []
        registry = None
        if any(judge.get("builtin") for judge in judges):
            try:
                registry = BuiltinJudgeRegistry()
                registry.discover()
            except Exception as exc:
                raise RuntimeError(
                    "--no-llm-judges: cannot classify builtin judges "
                    f"(registry discovery failed: {exc})") from exc

        def calls_model(judge: dict) -> bool:
            if any(judge.get(field) for field in (
                    "prompt", "prompt_file", "llm_rubric", "agent")):
                return True
            builtin = judge.get("builtin")
            if builtin:
                try:
                    return registry.get(builtin).kind == "llm"
                except Exception:
                    return True  # fail closed, matching score.py
            return False

        kept = [judge for judge in judges if not calls_model(judge)]
        removed = {judge.get("name") for judge in judges if calls_model(judge)}
        removed.discard(None)
        raw["judges"] = kept

        # Thresholds for intentionally skipped model judges must not linger in
        # the bundled verifier config. The host orchestration rejects such a
        # run explicitly; direct bundling remains schema-valid.
        thresholds = raw.get("thresholds")
        if isinstance(thresholds, dict):
            raw["thresholds"] = {
                name: threshold for name, threshold in thresholds.items()
                if name not in removed
            }

        reward = raw.get("reward")
        if isinstance(reward, dict):
            drop_reward = reward.get("judge") in removed
            if not drop_reward:
                weights = reward.get("weights")
                if isinstance(weights, dict):
                    reward["weights"] = {
                        name: weight for name, weight in weights.items()
                        if name not in removed
                    }
                    if reward.get("formula", "weighted") == "weighted" and not reward["weights"]:
                        drop_reward = True
                raw_names = reward.get("raw")
                if isinstance(raw_names, list):
                    reward["raw"] = [name for name in raw_names if name not in removed]
                formula = reward.get("formula")
                if formula and formula != "weighted":
                    try:
                        referenced = {
                            node.id for node in ast.walk(ast.parse(str(formula)))
                            if isinstance(node, ast.Name)
                        }
                    except SyntaxError:
                        referenced = removed
                    if referenced & removed:
                        drop_reward = True
            if drop_reward:
                raw.pop("reward", None)
    return raw


def _generate_tool_interception(env_dir: Path, config: EvalConfig,
                                config_path: Path, workdir: str) -> None:
    """Generate tool interception artifacts into environment/.

    Delegates to :func:`agent_eval.tools.interception.generate_interception`
    (shared with the local workspace path). Prefers a pre-resolved
    ``tool_handlers.yaml`` alongside ``eval.yaml`` (from ``/eval-analyze``);
    falls back to heuristic extraction.
    """
    resolved = config_path.parent / "tool_handlers.yaml"
    generate_interception(
        env_dir, config,
        hooks_command=f"python3 {workdir}/hooks/tools.py",
        resolved_handlers_path=resolved if resolved.is_file() else None)


# Harbor's verifier schema requires numeric rewards. Mark unjudged steps with a
# sentinel metric and a neutral numeric placeholder; results.py maps the step
# back to value=None so it is never reported as a passing score. The trial uses
# the final step reward (strategy = final), so a normal setup step's placeholder
# does not affect the task reward.
def _unjudged_verifier(copy_outputs: str = "true") -> str:
    return (
        "#!/bin/bash\n"
        "# No judges target this task/step; mark it unjudged, not passed.\n"
        "mkdir -p /logs/verifier\n"
        f"{copy_outputs}\n"
        "printf '{\"reward\": 0.0, \"agent_eval_unjudged\": 1}\\n' "
        "> /logs/verifier/reward.json\n"
    )


def _copy_outputs_snippet(config, workdir: str) -> str:
    """Verifier shell snippet copying declared output paths to /logs/verifier.

    Output paths come from eval.yaml; quote them so whitespace or shell
    metacharacters in a path cannot break — or rewrite — the generated
    ``test.sh``.
    """
    lines = []
    for out in config.outputs:
        if out.path:
            src = shlex.quote(f"{workdir}/{out.path}")
            dst = shlex.quote(f"/logs/verifier/{out.path}")
            lines.append(
                f'mkdir -p "$(dirname {dst})" && '
                f'cp -r {src} {dst} 2>/dev/null || true')
    return "\n".join(lines) if lines else "true"


def _format_skill_instruction(skill_name: str, arguments: str,
                              runner_type: str) -> str:
    """Render an explicit skill invocation for the selected agent family."""
    if runner_type == "codex":
        # Codex discovers the injected skill by its directory/frontmatter name;
        # Claude plugin namespaces such as ``ci:`` are not part of that name.
        codex_name = skill_name.rsplit(":", 1)[-1]
        command = f"Use the {codex_name} skill"
        if arguments:
            command += f" with arguments: {arguments}"
        return command
    return f"/{skill_name} {arguments}".strip()


def _instruction_text(system_prompt, command: str) -> str:
    """Compose a task instruction the way the local runners compose prompts.

    The local runners prepend ``runner.system_prompt`` to every prompt;
    the instruction must compose the same way or a Harbor agent runs
    without the eval's charter and behaves differently from a local run
    of the same case.
    """
    if system_prompt:
        return f"{system_prompt.rstrip()}\n\n{command}"
    return command


def _render_multistep_task_toml(*, task_name, task_desc, eval_name, case_id,
                                image, workdir, step_specs,
                                reward_strategy="final", judge_mode="full",
                                config_chain=None):
    """Build a schema 1.4 multi-step task.toml (variable-length [[steps]]).

    ``step_specs`` is a list of ``(name, agent_timeout, verifier_timeout)``.
    Every string field is serialized via :func:`_toml_string` (a full TOML
    basic string, quotes included) so values with quotes, newlines, backslashes,
    or control characters round-trip; the numeric timeouts stay bare.
    """
    lines = [
        "# Harbor multi-step task generated by eval-dataset from eval.yaml.",
        'schema_version = "1.4"',
        # Root-level TaskConfig field — must precede the first table header, or
        # TOML nests it under the preceding table and Harbor ignores it.
        f'multi_step_reward_strategy = {_toml_string(reward_strategy)}',
        "",
        "[task]",
        f'name = {_toml_string(task_name)}',
        f'description = {_toml_string(task_desc)}',
        "",
        "[metadata]",
        'generated_by = "agent-eval-harness/eval-dataset"',
        f'eval_name = {_toml_string(eval_name)}',
        f'case_id = {_toml_string(case_id)}',
        f'judge_mode = {_toml_string(judge_mode)}',
        # Which config file(s) produced the bundle (root first); the reuse check
        # refuses a package built from a different chain.
        "config_chain = [" + ", ".join(_toml_string(c) for c in (config_chain or [])) + "]",
        # Which dataset filtering staged the package; the reuse check refuses
        # a package without it (staged before the dataset was kept out).
        f"dataset_filter = {STAGING_VERSION}",
        "",
        "[environment]",
        f'docker_image = {_toml_string(image)}',
        f'workdir = {_toml_string(workdir)}',
    ]
    for name, agent_timeout, verifier_timeout in step_specs:
        lines += [
            "",
            "[[steps]]",
            f'name = {_toml_string(name)}',
            "",
            "[steps.agent]",
            f"timeout_sec = {agent_timeout}",
            "",
            "[steps.verifier]",
            f"timeout_sec = {verifier_timeout}",
        ]
    return "\n".join(lines) + "\n"


def _reset_task_dir(task_dir: Path, cases_root: Path) -> None:
    """Delete a case's previous task package so regeneration starts clean.

    Refused when the package directory is a dataset case directory itself (the
    tasks directory is the dataset) or holds the dataset: removing it would
    delete cases.
    """
    if not task_dir.exists() and not task_dir.is_symlink():
        return
    real, root = task_dir.resolve(), cases_root.resolve()
    if real == root or real.parent == root or root.is_relative_to(real):
        raise ValueError(
            f"Harbor task package {task_dir} is, or holds, a dataset case "
            f"directory under {cases_root}; choose a tasks directory outside "
            "the dataset")
    if task_dir.is_symlink() or task_dir.is_file():
        task_dir.unlink()
    else:
        shutil.rmtree(task_dir)


def _write_multi_step_case_package(config, config_path, bundled_cfg, case_dir,
                                   case_id, input_file, input_data, out_dir,
                                   image, workdir, agent_timeout,
                                   verifier_timeout, no_llm_judges=False,
                                   agent_name=None):
    """Write one Harbor multi-step task package (schema 1.4) for a case.

    Emits ``task.toml`` with a ``[[steps]]`` block per step, and per-step
    ``steps/<id>/instruction.md`` + ``steps/<id>/tests/{test.sh,eval.yaml}``.
    Each step's verifier runs only the judges scoped to it; whole-case judges
    run on the final step (``multi_step_reward_strategy = final``).
    """
    steps = config.execution.steps
    final_id = steps[-1].id
    task_dir = out_dir / case_id
    task_dir.mkdir(parents=True, exist_ok=True)

    # Verifier snippet that surfaces the declared output paths to the host.
    copy_outputs = _copy_outputs_snippet(config, workdir)

    raw_judges = bundled_cfg.get("judges") or []
    for step in steps:
        sid = step.id
        is_skill = bool(step.skill and step.skill.strip())
        template = step.arguments if is_skill else step.prompt
        try:
            resolved = resolve_arguments(template, input_data) if template else ""
        except ValueError as e:
            # Only blame {{ steps.* }} when the template actually uses it — a
            # plain missing input.* field is a different bug.
            hint = (
                " {{ steps.* }} references do not round-trip to Harbor — pass "
                "state between steps via the shared workspace filesystem instead."
                if "steps." in (template or "") else "")
            raise ValueError(
                f"Harbor generation: step '{sid}' arguments cannot be resolved "
                f"from input alone ({e}).{hint}") from e
        runner_type = (step.runner.type if step.runner
                       else (agent_name or config.runner.type))
        cmd = (_format_skill_instruction(step.skill, resolved, runner_type)
               if is_skill else resolved)
        step_system_prompt = ((step.runner.system_prompt if step.runner
                               else None) or config.runner.system_prompt)
        cmd = _instruction_text(step_system_prompt, cmd)

        step_dir = task_dir / "steps" / sid
        (step_dir / "tests").mkdir(parents=True, exist_ok=True)
        (step_dir / "instruction.md").write_text(
            _render("instruction.md.tmpl", {"COMMAND": cmd}), encoding="utf-8")

        # Judges for this step: step-scoped here + whole-case on the final step.
        js = [dict(j) for j in raw_judges if j.get("step") == sid]
        if sid == final_id:
            js += [dict(j) for j in raw_judges if not j.get("step")]
        for j in js:
            j.pop("step", None)

        if js:
            step_cfg = copy.deepcopy(bundled_cfg)
            exec_cfg = step_cfg.get("execution")
            if isinstance(exec_cfg, dict):
                exec_cfg.pop("steps", None)
                exec_cfg["mode"] = "case"
            step_cfg["judges"] = js
            (step_dir / "tests" / "eval.yaml").write_text(
                yaml.safe_dump(step_cfg, sort_keys=False, allow_unicode=True),
                encoding="utf-8")
            (step_dir / "tests" / "test.sh").write_text(_render(
                "test.sh.tmpl", {"WORKDIR": workdir, "COPY_OUTPUTS": copy_outputs}),
                encoding="utf-8")
        else:
            (step_dir / "tests" / "test.sh").write_text(
                _unjudged_verifier(copy_outputs), encoding="utf-8")
        (step_dir / "tests" / "test.sh").chmod(0o755)

    # Per-step agent timeout honors step.timeout when set (verifier uses the
    # task-level default — there is no per-step verifier timeout field).
    step_specs = [
        (s.id, (s.timeout if s.timeout is not None else agent_timeout),
         verifier_timeout)
        for s in steps
    ]
    (task_dir / "task.toml").write_text(_render_multistep_task_toml(
        task_name=f"{config.name or 'eval'}/{case_id}",
        task_desc=_task_label(config),
        eval_name=config.name, case_id=case_id, image=image, workdir=workdir,
        step_specs=step_specs,
        judge_mode="deterministic-only" if no_llm_judges else "full",
        config_chain=list(getattr(config, "config_chain", None) or [])),
        encoding="utf-8")

    # environment/ — auto-uploaded to the workspace by Harbor.
    env_dir = task_dir / "environment"
    env_dir.mkdir(parents=True, exist_ok=True)
    if input_file:
        shutil.copy2(input_file, env_dir / input_file.name)
    answers = case_dir / "answers.yaml"
    if answers.is_file():
        shutil.copy2(answers, env_dir / "answers.yaml")
    # Shared {dest, source} workspace.files, resolved from the local checkout
    # and copied in so the file travels inside the task package.
    materialize_shared_files(
        env_dir, config, protected_paths=dataset_protected_paths(config))
    _generate_tool_interception(env_dir, config, config_path, workdir)
    return task_dir


def generate_tasks(
    config: EvalConfig,
    config_path: Path,
    out_dir: Path,
    image: str,
    *,
    arguments: str | None = None,
    skill: str | None = None,
    workdir: str = "/workspace",
    cases: list[str] | None = None,
    verifier_timeout: float = 300.0,
    agent_timeout: float = 1800.0,
    judge_model: str | None = None,
    no_llm_judges: bool = False,
    agent_name: str | None = None,
) -> list[Path]:
    """Generate one self-contained Harbor task package per dataset case."""
    # workdir is interpolated into generated shell (test.sh) and TOML;
    # constrain it to a plain absolute path so a config value cannot
    # smuggle shell metacharacters into the verifier script.
    if not workdir.startswith("/") or not re.fullmatch(r"[A-Za-z0-9/_.\-]+", workdir):
        raise ValueError(
            "Harbor workdir must be an absolute path without shell "
            f"metacharacters: {workdir!r}")

    args_template = arguments if arguments is not None else config.execution.arguments
    # Resolve through EvalConfig.resolve_skill() so configs authored with only
    # execution.skill (no top-level skill) still generate /skill tasks rather
    # than silently degrading to prompt mode. When no skill resolves and the
    # config declares execution.prompt, generate a direct prompt task instead.
    skill_name = skill if skill is not None else config.resolve_skill()
    prompt_template = (
        config.execution.prompt
        if (skill is None and skill_name is None and config.is_prompt_mode())
        else None
    )

    cases_root = config.resolve_path(config.dataset.path)
    if not cases_root.is_dir():
        raise FileNotFoundError(f"Dataset path not found: {cases_root}")

    case_dirs = sorted(d for d in cases_root.iterdir() if d.is_dir())
    if cases:
        wanted = set(cases)
        case_dirs = [d for d in case_dirs if d.name in wanted]
    if not case_dirs:
        raise ValueError("No matching cases found")

    bundled_cfg = _bundle_eval_config(
        config_path, judge_model=judge_model, no_llm_judges=no_llm_judges)
    out_dir.mkdir(parents=True, exist_ok=True)
    generated: list[Path] = []

    for case_dir in case_dirs:
        case_id = case_dir.name
        input_file = _find_input_file(case_dir)
        input_data = {}
        if input_file:
            input_data = yaml.safe_load(input_file.read_text()) or {}

        # Regeneration replaces a case's package outright: whatever an earlier
        # run staged in it (an answer key included) must not survive.
        _reset_task_dir(out_dir / case_id, cases_root)

        # Multi-step pipeline → schema 1.4 [[steps]] package.
        if config.execution.steps:
            task_dir = _write_multi_step_case_package(
                config, Path(config_path), bundled_cfg, case_dir, case_id,
                input_file, input_data, out_dir, image, workdir,
                agent_timeout, verifier_timeout, no_llm_judges,
                agent_name=agent_name)
            generated.append(task_dir)
            print(f"  {case_id}: {task_dir}")
            continue

        if prompt_template:
            command = resolve_arguments(prompt_template, input_data)
        else:
            resolved_args = resolve_arguments(args_template, input_data) if args_template else ""
            command = (_format_skill_instruction(
                skill_name, resolved_args, agent_name or config.runner.type)
                if skill_name else resolved_args)
        command = _instruction_text(config.runner.system_prompt, command)

        task_dir = out_dir / case_id
        (task_dir / "tests").mkdir(parents=True, exist_ok=True)
        env_dir = task_dir / "environment"
        env_dir.mkdir(parents=True, exist_ok=True)

        # task.description is a display-only label — Harbor shows the agent
        # task.toml — every string-valued field is TOML-serialized so names,
        # descriptions, and paths containing quotes, newlines, or backslashes
        # round-trip through the parser; the numeric timeouts stay bare.
        (task_dir / "task.toml").write_text(_render("task.toml.tmpl", {
            "TASK_NAME": _toml_string(f"{config.name or 'eval'}/{case_id}"),
            "TASK_DESC": _toml_string(_task_label(config)),
            "EVAL_NAME": _toml_string(config.name),
            "CASE_ID": _toml_string(case_id),
            "JUDGE_MODE": _toml_string(
                "deterministic-only" if no_llm_judges else "full"),
            # Which config file(s) produced the bundle (root first).
            "CONFIG_CHAIN": "[" + ", ".join(
                _toml_string(c) for c in (getattr(config, "config_chain", None) or [])) + "]",
            "DATASET_FILTER": str(STAGING_VERSION),
            "IMAGE": _toml_string(image),
            "WORKDIR": _toml_string(workdir),
            "VERIFIER_TIMEOUT": verifier_timeout,
            "AGENT_TIMEOUT": agent_timeout,
        }), encoding="utf-8")

        # instruction.md
        (task_dir / "instruction.md").write_text(_render("instruction.md.tmpl", {
            "COMMAND": command,
        }), encoding="utf-8")

        # tests/test.sh + bundled eval.yaml (verifier)
        copy_outputs = _copy_outputs_snippet(config, workdir)
        if bundled_cfg.get("judges"):
            verifier = _render("test.sh.tmpl", {
                "WORKDIR": workdir,
                "COPY_OUTPUTS": copy_outputs,
            })
        else:
            verifier = _unjudged_verifier(copy_outputs)
        (task_dir / "tests" / "test.sh").write_text(
            verifier, encoding="utf-8")
        (task_dir / "tests" / "test.sh").chmod(0o755)
        if bundled_cfg.get("judges"):
            (task_dir / "tests" / "eval.yaml").write_text(
                yaml.safe_dump(bundled_cfg, sort_keys=False, allow_unicode=True),
                encoding="utf-8")

        # environment/ — auto-uploaded to the workspace by Harbor
        if input_file:
            shutil.copy2(input_file, env_dir / input_file.name)
        answers = case_dir / "answers.yaml"
        if answers.is_file():
            shutil.copy2(answers, env_dir / "answers.yaml")
        # Shared {dest, source} workspace.files, resolved from the local
        # checkout and copied in so the file travels inside the task package.
        materialize_shared_files(
            env_dir, config, protected_paths=dataset_protected_paths(config))

        # Batch mode: create a 1-item batch.yaml so the same --input batch.yaml
        # argument works per-case in Harbor as it does in local batch runs.
        if config.execution.mode == "batch" and input_data:
            entries = input_data if isinstance(input_data, list) else [input_data]
            (env_dir / "batch.yaml").write_text(
                yaml.safe_dump(entries, sort_keys=False, allow_unicode=True),
                encoding="utf-8")

        # Tool interception (hooks + .claude/settings.json)
        _generate_tool_interception(env_dir, config, Path(config_path), workdir)

        generated.append(task_dir)
        print(f"  {case_id}: {task_dir}")

    return generated
