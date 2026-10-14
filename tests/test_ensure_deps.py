"""Tests for scripts/ensure_deps.py dependency resolution.

The SessionStart hook installs into .eval-venv whatever the project's eval.yaml
files imply. Resolving against a single config silently under-installed for
projects holding several evals, so the union behaviour is what these pin down.
"""

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_ensure_deps():
    """Load scripts/ensure_deps.py by path — scripts/ is not a package."""
    path = REPO_ROOT / "scripts" / "ensure_deps.py"
    spec = importlib.util.spec_from_file_location("_ensure_deps_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ensure_deps = _load_ensure_deps()


def _write(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body))
    return path


MLFLOW_EVAL = """
    name: with-mlflow
    execution:
      skill: a
    dataset:
      path: cases
    mlflow:
      experiment: exp
    """

JUDGE_EVAL = """
    name: with-judge
    execution:
      skill: b
    dataset:
      path: cases
    judges:
      - name: quality
        prompt: "Rate {{ inputs }}"
    """

PLAIN_EVAL = """
    name: plain
    execution:
      skill: c
    dataset:
      path: cases
    judges:
      - name: exists
        check: "True"
    """


def _specs(deps):
    return [spec for spec, _ in deps]


class TestDepsForSingleConfig:
    def test_pyyaml_is_always_present(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert "pyyaml>=6.0" in _specs(ensure_deps._resolve_deps(tmp_path))

    def test_no_config_yields_only_pyyaml(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert _specs(ensure_deps._resolve_deps(tmp_path)) == ["pyyaml>=6.0"]

    def test_mlflow_block_pulls_mlflow(self, tmp_path):
        specs = _specs(ensure_deps._deps_for_config(
            _write(tmp_path / "eval.yaml", MLFLOW_EVAL)))
        assert "mlflow[genai]>=3.5" in specs
        assert "anthropic[vertex]>=0.77" not in specs

    def test_llm_judge_pulls_anthropic_and_jinja2(self, tmp_path):
        specs = _specs(ensure_deps._deps_for_config(
            _write(tmp_path / "eval.yaml", JUDGE_EVAL)))
        assert "anthropic[vertex]>=0.77" in specs
        assert "jinja2>=3.0" in specs
        assert "mlflow[genai]>=3.5" not in specs

    def test_check_only_judge_pulls_neither(self, tmp_path):
        assert ensure_deps._deps_for_config(
            _write(tmp_path / "eval.yaml", PLAIN_EVAL)) == []

    def test_unparseable_config_contributes_nothing(self, tmp_path):
        bad = _write(tmp_path / "eval.yaml", "{{{ not yaml at all\n")
        assert ensure_deps._deps_for_config(bad) == []

    def test_missing_file_contributes_nothing(self, tmp_path):
        assert ensure_deps._deps_for_config(tmp_path / "nope.yaml") == []


class TestUnionAcrossConfigs:
    """The regression: a project with several evals under eval/<name>/eval.yaml."""

    @pytest.fixture
    def multi_config_project(self, tmp_path, monkeypatch):
        # Named so that the mlflow one does NOT sort first — under the old
        # configs[0] behaviour whichever landed first decided the whole dep set.
        _write(tmp_path / "eval" / "aaa-judge" / "eval.yaml", JUDGE_EVAL)
        _write(tmp_path / "eval" / "zzz-mlflow" / "eval.yaml", MLFLOW_EVAL)
        monkeypatch.chdir(tmp_path)
        return tmp_path

    def test_deps_are_the_union_not_the_first_config(self, multi_config_project):
        specs = _specs(ensure_deps._resolve_deps(multi_config_project))
        assert "anthropic[vertex]>=0.77" in specs, "LLM-judge config's deps missing"
        assert "jinja2>=3.0" in specs
        assert "mlflow[genai]>=3.5" in specs, (
            "mlflow config was ignored — deps resolved from one config only")

    def test_discovery_finds_every_config(self, multi_config_project):
        found = ensure_deps._find_eval_yamls(multi_config_project)
        assert len(found) == 2, f"expected both configs, got {found}"

    def test_shared_deps_are_not_duplicated(self, tmp_path, monkeypatch):
        _write(tmp_path / "eval" / "one" / "eval.yaml", MLFLOW_EVAL)
        _write(tmp_path / "eval" / "two" / "eval.yaml", MLFLOW_EVAL)
        monkeypatch.chdir(tmp_path)
        specs = _specs(ensure_deps._resolve_deps(tmp_path))
        assert specs.count("mlflow[genai]>=3.5") == 1
        assert specs.count("pyyaml>=6.0") == 1

    def test_one_broken_config_does_not_sink_the_others(self, tmp_path, monkeypatch):
        _write(tmp_path / "eval" / "broken" / "eval.yaml", "{{{ not yaml\n")
        _write(tmp_path / "eval" / "good" / "eval.yaml", MLFLOW_EVAL)
        monkeypatch.chdir(tmp_path)
        assert "mlflow[genai]>=3.5" in _specs(ensure_deps._resolve_deps(tmp_path))

    def test_stamp_is_order_independent(self, tmp_path):
        """The install cache keys on the dep set; discovery order must not churn it."""
        a = [("pyyaml>=6.0", "yaml"), ("mlflow[genai]>=3.5", "mlflow")]
        assert ensure_deps._compute_stamp(a) == ensure_deps._compute_stamp(a[::-1])


class TestFallbackWhenDiscoveryFails:
    def test_root_eval_yaml_is_found(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write(tmp_path / "eval.yaml", MLFLOW_EVAL)
        assert "mlflow[genai]>=3.5" in _specs(ensure_deps._resolve_deps(tmp_path))

    def test_falls_back_to_a_single_candidate(self, tmp_path, monkeypatch):
        """cwd/eval.yaml and plugin_root/eval.yaml are alternatives, not siblings.

        Discovery is forced to fail so the fallback is what answers; the nested
        config exists purely to prove the fallback ran (discovery would have
        returned it too, so a one-element result can only come from the fallback).
        """
        monkeypatch.setitem(sys.modules, "agent_eval.config", None)  # import raises
        monkeypatch.chdir(tmp_path)
        _write(tmp_path / "eval.yaml", MLFLOW_EVAL)
        _write(tmp_path / "eval" / "nested" / "eval.yaml", JUDGE_EVAL)
        plugin_root = tmp_path / "plugin"
        _write(plugin_root / "eval.yaml", JUDGE_EVAL)

        assert ensure_deps._find_eval_yamls(plugin_root) == [tmp_path / "eval.yaml"]

        # Control: with discovery working, both configs are returned.
        monkeypatch.undo()
        monkeypatch.chdir(tmp_path)
        assert len(ensure_deps._find_eval_yamls(plugin_root)) == 2

    def test_discovery_failure_is_reported(self, tmp_path, monkeypatch, capsys):
        """Degrading to one config is the exact under-installation this function
        exists to prevent, so it must not happen silently."""
        monkeypatch.setitem(sys.modules, "agent_eval.config", None)
        monkeypatch.chdir(tmp_path)
        _write(tmp_path / "eval.yaml", MLFLOW_EVAL)

        ensure_deps._find_eval_yamls(tmp_path)

        err = capsys.readouterr().err
        assert "discovery failed" in err
        assert "missing deps" in err

    def test_no_warning_when_discovery_works(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        _write(tmp_path / "eval" / "one" / "eval.yaml", MLFLOW_EVAL)
        ensure_deps._find_eval_yamls(tmp_path)
        assert "discovery failed" not in capsys.readouterr().err


class TestOpenAIBackendClassifier:
    """`_needs_openai_backend` mirrors resolve_judge_backend (spec 013/014):
    `openai:/`, `openrouter:/` and any non-Claude bare id need the OpenAI SDK."""

    @pytest.mark.parametrize("model", [
        "openrouter:/z-ai/glm-5.2", "openai:/gpt-4o", "gpt-4o", "o5-mini",
        "my-gateway-model", "vendor/model",
    ])
    def test_openai_sdk_models(self, model):
        assert ensure_deps._needs_openai_backend(model) is True

    @pytest.mark.parametrize("model", [
        "sonnet", "claude-opus-4-8", "anthropic:/claude-sonnet-4-5",
        "runner:/gpt-5.4-medium", "", None,
    ])
    def test_non_openai_models(self, model):
        assert ensure_deps._needs_openai_backend(model) is False

    def test_openrouter_judge_pulls_openai(self, tmp_path):
        eval_yaml = tmp_path / "eval.yaml"
        eval_yaml.write_text(textwrap.dedent("""
            name: t
            execution:
              skill: s
            models:
              judge: openrouter:/z-ai/glm-5.2
            judges:
              - {name: j, prompt: rate it}
        """))
        deps = ensure_deps._deps_for_config(eval_yaml)
        assert any("openai" in str(dep) for dep in deps), deps

    def test_provider_package_is_stdlib_only(self):
        """No httpx/openrouter extra: the provider package imports with httpx
        blocked (checked in a subprocess so this process's modules stay put)."""
        import subprocess

        code = ("import sys; sys.modules['httpx'] = None; "
                "import agent_eval.providers.openrouter, agent_eval.config; print('ok')")
        proc = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0 and proc.stdout.strip() == "ok", proc.stderr


class TestPyYAMLBootstrap:
    """The config scan must read real YAML: on a host without PyYAML the
    first run installs it into .eval-venv before resolving dependencies, and
    the venv's site-packages are made importable for that scan."""

    def test_import_yaml_reaches_into_the_venv(self, tmp_path, monkeypatch):
        site = tmp_path / ensure_deps.VENV_DIR_NAME / "lib" / "python3.99" / "site-packages"
        site.mkdir(parents=True)
        monkeypatch.setitem(sys.modules, "yaml", None)       # host has no PyYAML
        monkeypatch.setattr(sys, "path", list(sys.path))
        assert ensure_deps._import_yaml(tmp_path) is None     # nothing there either
        assert str(site) in sys.path                          # but the venv was tried

    def test_first_run_installs_pyyaml_before_the_scan(self, tmp_path, monkeypatch):
        calls = []
        state = {"yaml": None}
        monkeypatch.setattr(ensure_deps, "_import_yaml", lambda root: state["yaml"])
        monkeypatch.setattr(ensure_deps, "_ensure_venv", lambda d: calls.append("venv"))

        def fake_install(venv_dir, specs):
            calls.append(("install", tuple(specs)))
            state["yaml"] = object()                          # now importable

        monkeypatch.setattr(ensure_deps, "_install_deps", fake_install)
        monkeypatch.setattr(ensure_deps, "_resolve_deps",
                            lambda root: (calls.append("resolve") or [("pyyaml>=6.0", "yaml")]))
        monkeypatch.setattr(ensure_deps, "_all_importable", lambda py, deps: False)
        monkeypatch.setattr(sys, "argv", ["ensure_deps.py"])
        ensure_deps.main()
        assert calls[:3] == ["venv", ("install", ("pyyaml>=6.0",)), "resolve"]

    def test_host_with_pyyaml_scans_immediately(self, monkeypatch):
        calls = []
        monkeypatch.setattr(ensure_deps, "_import_yaml", lambda root: object())
        monkeypatch.setattr(ensure_deps, "_ensure_venv", lambda d: calls.append("venv"))
        monkeypatch.setattr(ensure_deps, "_install_deps",
                            lambda d, specs: calls.append(("install", tuple(specs))))
        monkeypatch.setattr(ensure_deps, "_resolve_deps",
                            lambda root: (calls.append("resolve") or [("pyyaml>=6.0", "yaml")]))
        monkeypatch.setattr(sys, "argv", ["ensure_deps.py"])
        ensure_deps.main()
        assert calls[0] == "resolve"
