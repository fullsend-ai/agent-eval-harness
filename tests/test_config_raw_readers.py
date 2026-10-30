"""Every eval-config reader goes through the single raw loader (spec 014 PR-3a).

`agent_eval.config.load_raw` is the only place that resolves `extends:`. A
reader that parses an eval config with a bare `yaml.safe_load` would see an
overlay's own keys instead of the merged config, so this test pins the set of
YAML call sites in the source tree: adding one means either routing it
through `load_raw` or extending the allow-list below with the reason the
file it reads is not an eval config.
"""

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agent_eval.config import discover_configs  # noqa: E402

# file -> {enclosing function: what the call reads}. Everything here parses
# something other than an eval config (datasets, summaries, frontmatter,
# state files, handler manifests) — or is the loader itself. Both
# `yaml.safe_load(...)`-style calls and explicitly driven Loaders
# (`Loader(stream).get_single_data()`) count as reads.
ALLOWED_YAML_READERS = {
    "agent_eval/config.py": {"_read_config_mapping": "the single raw loader"},
    "agent_eval/agent/cli_runner.py": {"execute": "case input.yaml"},
    "agent_eval/agent/fullsend.py": {
        "_read_yaml_mapping": "fullsend <fullsend_dir>/config.yaml (agent registry) and the harness file's image:"},
    "agent_eval/evalhub/adapter.py": {"_run_cases": "case input.yaml"},
    "agent_eval/engines/capevolve.py": {"_read_agent_input": "case input.yaml"},
    "agent_eval/examples.py": {"harvest_review_examples": "review.yaml"},
    "agent_eval/harbor/tasks.py": {"generate_tasks": "case input.yaml"},
    "agent_eval/hooks.py": {"_read_hook_output_file": "hook output YAML"},
    "agent_eval/optimize/rollout.py": {"run_case": "summary.yaml"},
    "agent_eval/state.py": {"main": "tmp/ state files"},
    "agent_eval/tools/interception.py": {"generate_interception": "tool_handlers.yaml"},
    "scripts/ensure_deps.py": {"_read": "stdlib fallback when agent_eval cannot import (follows extends itself)"},
    "skills/eval-analyze/scripts/assess_skills.py": {"_parse_frontmatter": "SKILL.md frontmatter"},
    "skills/eval-analyze/scripts/find_skills.py": {"find_skill": "SKILL.md frontmatter",
                                                   "list_skills": "SKILL.md frontmatter"},
    "skills/eval-analyze/scripts/validate_eval.py": {
        "validate_config": "syntax-only first pass for the friendly YAML error; structure runs on load_raw",
        "_validate_field_consistency": "dataset annotations",
        "validate_memory": "memory frontmatter"},
    "skills/eval-anova/scripts/analyze.py": {"load_conditions_from_runs": "summary.yaml"},
    "skills/eval-check/scripts/harness_inventory.py": {
        "_parse_frontmatter_description": "SKILL.md frontmatter",
        "find_eval_configs": "inventory of eval.yaml-named files as written (profiles are named otherwise)"},
    "skills/eval-check/scripts/reference_checker.py": {
        "_parse_frontmatter": "SKILL.md frontmatter",
        "find_eval_configs": "reference check of eval.yaml-named files as written"},
    "skills/eval-compare/scripts/compare.py": {"load_yaml": "summary.yaml"},
    "skills/eval-mlflow/scripts/attach_feedback.py": {"_pull_feedback": "summary/review YAML",
                                                     "_push_feedback": "review.yaml"},
    "skills/eval-mlflow/scripts/from_traces.py": {"_attach_input_artifacts": "batch/input YAML"},
    "skills/eval-mlflow/scripts/log_results.py": {"main": "summary.yaml"},
    "skills/eval-mlflow/scripts/sync_dataset.py": {"_extract_field": "case input.yaml"},
    "skills/eval-run/scripts/collect.py": {"main": "batch case order"},
    "skills/eval-run/scripts/execute.py": {
        "main": "batch.yaml", "_execute_per_case": "case order / meta",
        "_merge_input_overrides": "case input.yaml", "_run_multi_step_case": "case input.yaml",
        "_run_single_case": "case input.yaml", "_run_single_case_in_repo": "case input.yaml"},
    "skills/eval-run/scripts/report.py": {"_load_yaml": "summary/review/annotations (the config goes through load_raw)",
                                          "_parse_analysis_frontmatter": "analysis.md frontmatter"},
    "skills/eval-run/scripts/score.py": {
        "load_case_record": "annotations / input.yaml / hook outputs",
        "_extract_yaml_frontmatter_keys": "artifact frontmatter",
        "_merge_summary": "summary.yaml", "cmd_regression": "summary.yaml"},
    "skills/eval-run/scripts/tools.py": {"main": "tool_handlers.yaml"},
    "skills/eval-run/scripts/workspace.py": {"_parse_file": "handler/settings files"},
}


def _yaml_call_sites():
    files = [*REPO_ROOT.glob("agent_eval/**/*.py"), *REPO_ROOT.glob("scripts/*.py"),
             *REPO_ROOT.glob("skills/*/scripts/*.py")]
    sites = {}
    for f in sorted(files):
        rel = str(f.relative_to(REPO_ROOT))
        if "/tests/" in rel or ".eval-venv" in rel:
            continue
        tree = ast.parse(f.read_text(), filename=rel)
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            is_module_call = (node.func.attr in ("safe_load", "load", "safe_load_all", "load_all")
                              and isinstance(node.func.value, ast.Name)
                              and node.func.value.id in ("yaml", "_yaml"))
            # A Loader driven explicitly (`Loader(stream).get_single_data()`)
            # is a YAML read too — the form the two extends-aware readers use.
            is_loader_call = node.func.attr in ("get_single_data", "get_data")
            if not (is_module_call or is_loader_call):
                continue
            fn = node
            while fn in parents and not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn = parents[fn]
            name = fn.name if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) else "<module>"
            sites.setdefault(rel, set()).add(name)
    return sites


def test_every_yaml_reader_is_accounted_for():
    sites = _yaml_call_sites()
    unknown = {f: sorted(n for n in names if n not in ALLOWED_YAML_READERS.get(f, {}))
               for f, names in sites.items()}
    unknown = {f: n for f, n in unknown.items() if n}
    assert not unknown, (
        "New YAML reader(s) outside load_raw: "
        f"{unknown}. Eval configs must be read through agent_eval.config.load_raw "
        "(so `extends:` resolves); anything else needs an ALLOWED_YAML_READERS entry.")


def test_allow_list_names_only_live_sites():
    sites = _yaml_call_sites()
    stale = {f: sorted(n for n in names if n not in sites.get(f, set()))
             for f, names in ALLOWED_YAML_READERS.items()}
    stale = {f: n for f, n in stale.items() if n}
    assert not stale, f"ALLOWED_YAML_READERS lists sites that no longer read YAML: {stale}"


def test_only_load_raw_reads_the_extends_key():
    """`raw.get("extends")` / `raw["extends"]` outside config.py would be a
    second resolver; ensure_deps' stdlib fallback is the documented exception."""
    offenders = []
    for f in [*REPO_ROOT.glob("agent_eval/**/*.py"), *REPO_ROOT.glob("skills/*/scripts/*.py")]:
        rel = str(f.relative_to(REPO_ROOT))
        if rel == "agent_eval/config.py" or "/tests/" in rel:
            continue
        text = f.read_text()
        if re.search(r"""(\.get\(|\.pop\(|\[)\s*["']extends["']""", text):
            offenders.append(rel)
    assert offenders == [], offenders


def test_discover_configs_skips_profiles_unless_asked(tmp_path, capsys):
    (tmp_path / "eval.yaml").write_text("name: base\nexecution:\n  skill: base-skill\n")
    (tmp_path / "eval-profiles").mkdir()
    (tmp_path / "eval-profiles" / "glm.yaml").write_text(
        "extends: ../eval.yaml\nmodels:\n  skill: openrouter:/z-ai/glm-5.2\n")
    (tmp_path / "eval" / "profiles").mkdir(parents=True)
    (tmp_path / "eval" / "profiles" / "fast.yaml").write_text(
        "extends: ../../eval.yaml\nexecution:\n  timeout: 5\n")

    default = discover_configs(tmp_path)
    assert [r.eval_name for r in default] == ["base-skill"]
    assert default[0].profile_of is None

    with_profiles = discover_configs(tmp_path, include_profiles=True)
    by_name = {r.path.name: r for r in with_profiles}
    assert set(by_name) == {"eval.yaml", "glm.yaml", "fast.yaml"}
    for profile in ("glm.yaml", "fast.yaml"):
        assert by_name[profile].eval_name == "base-skill"      # the base's name
        assert by_name[profile].profile_of == (tmp_path / "eval.yaml").resolve()
        assert by_name[profile].is_root is False
    # A profile sharing its base's eval name is not a duplicate.
    assert "duplicate eval name" not in capsys.readouterr().err


def test_discover_configs_skips_a_broken_profile_with_a_warning(tmp_path, capsys):
    (tmp_path / "eval.yaml").write_text("name: base\nexecution:\n  skill: s\n")
    (tmp_path / "eval-profiles").mkdir()
    (tmp_path / "eval-profiles" / "bad.yaml").write_text("extends: ../missing.yaml\n")
    results = discover_configs(tmp_path, include_profiles=True)
    assert [r.path.name for r in results] == ["eval.yaml"]
    assert "skipping profile" in capsys.readouterr().err


def test_ensure_deps_follows_extends_on_both_paths(tmp_path, monkeypatch):
    """An overlay that adds an `openrouter:/` judge pulls `openai` even when
    its base does not — through load_raw, and through the stdlib fallback
    used before the venv exists."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_ensure_deps_extends", REPO_ROOT / "scripts" / "ensure_deps.py")
    ensure_deps = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ensure_deps)

    (tmp_path / "eval.yaml").write_text(
        "name: base\nexecution:\n  skill: s\njudges:\n  - {name: chk, check: 'return True'}\n")
    (tmp_path / "eval").mkdir()
    (tmp_path / "eval" / "openrouter-x.yaml").write_text(
        "extends: ../eval.yaml\nmodels:\n  judge: openrouter:/z-ai/glm-5.2\n"
        "judges:\n  - {name: q, prompt: rate it}\n")

    def specs(path):
        return [spec for spec, _ in ensure_deps._deps_for_config(path)]

    base_specs = specs(tmp_path / "eval.yaml")
    assert not any("openai" in s for s in base_specs)
    overlay_specs = specs(tmp_path / "eval" / "openrouter-x.yaml")
    assert any(s.startswith("openai") for s in overlay_specs), overlay_specs
    assert any(s.startswith("anthropic") for s in overlay_specs)

    # Stdlib fallback: make the harness loader unimportable.
    monkeypatch.setitem(sys.modules, "agent_eval.config", None)
    fallback_specs = specs(tmp_path / "eval" / "openrouter-x.yaml")
    assert any(s.startswith("openai") for s in fallback_specs), fallback_specs

    # The fallback accepts `!replace` (a plain SafeLoader would reject the tag
    # and drop to the lossy minimal parser) and honours its meaning: the
    # overlay's judge list replaces the base's.
    (tmp_path / "eval" / "openrouter-replace.yaml").write_text(
        "extends: ../eval.yaml\nmodels:\n  judge: openrouter:/z-ai/glm-5.2\n"
        "judges: !replace\n  - {name: q, prompt: rate it}\n")
    replaced = ensure_deps._load_config_following_extends(
        tmp_path / "eval" / "openrouter-replace.yaml")
    assert [j["name"] for j in replaced["judges"]] == ["q"]
    assert replaced["models"]["judge"] == "openrouter:/z-ai/glm-5.2"
    replace_specs = specs(tmp_path / "eval" / "openrouter-replace.yaml")
    assert any(s.startswith("openai") for s in replace_specs), replace_specs
    assert any(s.startswith("anthropic") for s in replace_specs)

    # Discovery for the dependency scan includes profiles.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delitem(sys.modules, "agent_eval.config", raising=False)
    found = {p.name for p in ensure_deps._find_eval_yamls(tmp_path)}
    assert {"eval.yaml", "openrouter-x.yaml"} <= found
