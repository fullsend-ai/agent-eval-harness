"""Dataset (answer-key) protection across the Harbor launch surface (#345).

Beyond the task package itself: ``--skill`` uploads, explicit ``--mount``
sources, reused or regenerated packages, and the isolation metadata handed to
the podman environment. Every check that matters is a Bash-style hunt
(``grep -r``/``find``/``cat``) over what the container would receive.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from agent_eval.config import EvalConfig
from agent_eval.harbor import run as run_mod
from agent_eval.harbor import tasks as gen
from agent_eval.workspace_provisioning import (
    dataset_fingerprints,
    dataset_protected_paths,
    find_dataset_copies,
    materialize_shared_files,
)

KEY = "ANSWER-KEY-b41e"
DATASET = "skills/demo/evals/cases"


@pytest.fixture(autouse=True)
def _project_cwd(tmp_path, monkeypatch):
    (tmp_path / "project").mkdir()
    monkeypatch.chdir(tmp_path / "project")


def bash_finds_key(root: Path) -> str:
    out = subprocess.run(
        ["bash", "-c", f"grep -rIl {KEY} . ; find . -name annotations.yaml"],
        cwd=root, capture_output=True, text=True)
    return out.stdout.strip()


def _project(tmp_path, *, plugin=False):
    project = (tmp_path / "project").resolve()
    case = project / DATASET / "case-001"
    case.mkdir(parents=True)
    (case / "input.yaml").write_text(yaml.safe_dump({"prompt": "hello"}))
    (case / "annotations.yaml").write_text(f"expected: {KEY}\n")
    (project / "skills" / "demo" / "SKILL.md").write_text("# demo\n")
    raw = {
        "name": "iso",
        "execution": {"skill": "demo", "arguments": "{{ input.prompt }}"},
        "dataset": {"path": DATASET, "schema": "x"},
        "outputs": [{"path": "out", "schema": "o"}],
        "judges": [{"name": "j", "check": "return (True, 'ok')\n"}],
    }
    if plugin:
        raw["runner"] = {"type": "claude-code", "plugin_dirs": ["."]}
    cfg_path = project / "eval.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return project, cfg_path, EvalConfig.from_yaml(cfg_path)


class TestSkillUploads:
    def test_root_holding_the_dataset_is_staged_without_it(self, tmp_path):
        project, _cfg_path, config = _project(tmp_path)
        stage = tmp_path / "stage"
        stage.mkdir()

        roots = run_mod._dataset_free_skill_roots(
            config, [project / "skills"], stage)

        assert roots != [project / "skills"]
        assert (roots[0] / "demo" / "SKILL.md").is_file()
        assert bash_finds_key(roots[0]) == ""
        # The original is untouched.
        assert (project / DATASET / "case-001" / "annotations.yaml").is_file()

    def test_link_to_the_dataset_inside_a_root_is_not_uploaded(self, tmp_path):
        project, _cfg_path, config = _project(tmp_path)
        other = tmp_path / "plugin" / "skills"
        other.mkdir(parents=True)
        (other / "gold").symlink_to(project / DATASET)
        stage = tmp_path / "stage"
        stage.mkdir()

        roots = run_mod._dataset_free_skill_roots(config, [other], stage)

        assert not (roots[0] / "gold").exists()
        assert bash_finds_key(roots[0]) == ""

    def test_clean_root_is_passed_through(self, tmp_path):
        _p, _c, config = _project(tmp_path)
        clean = tmp_path / "plugin" / "skills"
        clean.mkdir(parents=True)
        (clean / "SKILL.md").write_text("# s\n")
        stage = tmp_path / "stage"
        stage.mkdir()
        assert run_mod._dataset_free_skill_roots(config, [clean], stage) == [clean]

    @pytest.mark.parametrize("sub", [DATASET, f"{DATASET}/case-001"])
    def test_root_that_is_or_lies_inside_the_dataset_is_refused(
            self, tmp_path, sub):
        project, _cfg_path, config = _project(tmp_path)
        stage = tmp_path / "stage"
        stage.mkdir()
        with pytest.raises(ValueError, match="answer keys"):
            run_mod._dataset_free_skill_roots(config, [project / sub], stage)


class TestExplicitMounts:
    def _mount(self, source):
        return {"type": "bind", "source": str(source), "target": "/data",
                "read_only": True}

    @pytest.mark.parametrize("sub", [
        ".", "skills", DATASET, f"{DATASET}/case-001"])
    def test_mount_holding_or_inside_the_dataset_is_refused(self, tmp_path, sub):
        project, _cfg_path, config = _project(tmp_path)
        with pytest.raises(ValueError, match="answer keys"):
            run_mod._refuse_dataset_mounts(
                config, [self._mount(project / sub)])

    def test_mount_elsewhere_is_allowed(self, tmp_path):
        _p, _c, config = _project(tmp_path)
        data = tmp_path / "data"
        data.mkdir()
        run_mod._refuse_dataset_mounts(config, [self._mount(data)])

    def test_link_into_the_dataset_is_followed(self, tmp_path):
        project, _cfg_path, config = _project(tmp_path)
        data = tmp_path / "data"
        data.mkdir()
        mount = tmp_path / "mount"
        mount.symlink_to(project / DATASET)
        with pytest.raises(ValueError, match="answer keys"):
            run_mod._refuse_dataset_mounts(config, [self._mount(mount)])


class TestStaleAndReusedPackages:
    def test_regeneration_discards_what_an_earlier_run_staged(self, tmp_path):
        project, cfg_path, config = _project(tmp_path)
        out = tmp_path / "tasks"
        gen.generate_tasks(config, cfg_path, out, image="img:1")
        stale = out / "case-001" / "environment" / "gold"
        stale.mkdir()
        (stale / "annotations.yaml").write_text(f"expected: {KEY}\n")
        assert bash_finds_key(out / "case-001") != ""

        gen.generate_tasks(config, cfg_path, out, image="img:1")

        assert not stale.exists()
        assert bash_finds_key(out / "case-001") == ""
        assert (out / "case-001" / "environment" / "input.yaml").is_file()

    def test_tasks_dir_that_is_the_dataset_is_refused(self, tmp_path):
        project, cfg_path, config = _project(tmp_path)
        with pytest.raises(ValueError, match="dataset case"):
            gen.generate_tasks(config, cfg_path, project / DATASET, image="img:1")
        # Nothing was deleted.
        assert (project / DATASET / "case-001" / "annotations.yaml").is_file()

    def test_reuse_refuses_a_package_holding_a_dataset_copy(self, tmp_path):
        project, cfg_path, config = _project(tmp_path)
        out = tmp_path / "tasks"
        gen.generate_tasks(config, cfg_path, out, image="img:1")
        run_mod._validate_task_package_reuse(out, config)  # clean: accepted

        staged = out / "case-001" / "environment" / "demo" / "evals"
        staged.mkdir(parents=True)
        (staged / "annotations.yaml").write_text(f"expected: {KEY}\n")

        with pytest.raises(ValueError, match="--regenerate"):
            run_mod._validate_task_package_reuse(out, config)

    def test_reuse_check_ignores_the_case_inputs_a_package_carries(self, tmp_path):
        project, cfg_path, config = _project(tmp_path)
        out = tmp_path / "tasks"
        gen.generate_tasks(config, cfg_path, out, image="img:1")
        assert (out / "case-001" / "environment" / "input.yaml").is_file()
        run_mod._validate_task_package_reuse(out, config)


class TestLaunch:
    """``run_eval_on_harbor`` end to end, with the harbor child faked."""

    def _run(self, tmp_path, monkeypatch, config_path, **kwargs):
        captured = {}

        class FakeProcess:
            returncode = 17

            def wait(self):
                # The staged skill root is still on disk while harbor runs.
                command = captured["command"]
                if "--skill" in command:
                    root = Path(command[command.index("--skill") + 1])
                    captured["skill_listing"] = bash_finds_key(root)
                    captured["skill_has_skill_md"] = any(root.rglob("SKILL.md"))
                    captured["skill_root"] = root
                return 17

            def send_signal(self, signum):
                pass

        real_popen = subprocess.Popen

        def fake_popen(command, **kw):
            if command[0] != "harbor":   # the Bash hunt below is a real process
                return real_popen(command, **kw)
            captured["command"] = command
            captured["env"] = kw["env"]
            return FakeProcess()

        monkeypatch.setattr(run_mod.subprocess, "Popen", fake_popen)
        result = run_mod.run_eval_on_harbor(
            config_path, image=None, model="m", output_dir=tmp_path / "out",
            tasks_dir=tmp_path / "tasks", jobs_dir=tmp_path / "jobs",
            harbor_bin="harbor", **kwargs)
        return result, captured

    def test_plugin_skill_upload_has_no_dataset_and_is_cleaned_up(
            self, tmp_path, monkeypatch):
        # The plugin is the project: its skills/ holds the dataset.
        project, cfg_path, config = _project(tmp_path, plugin=True)
        manifest = project / ".claude-plugin"
        manifest.mkdir()
        (manifest / "plugin.json").write_text(json.dumps({"name": "demo"}))
        gen.generate_tasks(config, cfg_path, tmp_path / "tasks", image="img:1")

        result, captured = self._run(tmp_path, monkeypatch, cfg_path)

        assert result == 17
        assert captured["skill_has_skill_md"] is True
        assert captured["skill_listing"] == ""
        assert not captured["skill_root"].exists()

    def test_project_mount_gets_json_mask_metadata(self, tmp_path, monkeypatch):
        project, cfg_path, config = _project(tmp_path)
        gen.generate_tasks(config, cfg_path, tmp_path / "tasks", image="img:1")
        monkeypatch.setenv("AGENT_EVAL_PODMAN_PROJECT_DIR", str(project))
        # An operator-supplied value never survives.
        monkeypatch.setenv("AGENT_EVAL_PODMAN_PROJECT_HIDE", "[]")

        _result, captured = self._run(tmp_path, monkeypatch, cfg_path)

        assert json.loads(captured["env"]["AGENT_EVAL_PODMAN_PROJECT_HIDE"]) == [
            DATASET]

    def test_project_mount_outside_the_dataset_gets_an_empty_list(
            self, tmp_path, monkeypatch):
        project, cfg_path, config = _project(tmp_path)
        gen.generate_tasks(config, cfg_path, tmp_path / "tasks", image="img:1")
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.setenv("AGENT_EVAL_PODMAN_PROJECT_DIR", str(other))
        monkeypatch.delenv("AGENT_EVAL_PODMAN_PROJECT_HIDE", raising=False)

        _result, captured = self._run(tmp_path, monkeypatch, cfg_path)

        assert captured["env"]["AGENT_EVAL_PODMAN_PROJECT_HIDE"] == "[]"

    def test_no_project_mount_leaves_no_metadata(self, tmp_path, monkeypatch):
        project, cfg_path, config = _project(tmp_path)
        gen.generate_tasks(config, cfg_path, tmp_path / "tasks", image="img:1")
        monkeypatch.delenv("AGENT_EVAL_PODMAN_PROJECT_DIR", raising=False)
        monkeypatch.setenv("AGENT_EVAL_PODMAN_PROJECT_HIDE", '["x"]')

        _result, captured = self._run(tmp_path, monkeypatch, cfg_path)

        assert "AGENT_EVAL_PODMAN_PROJECT_HIDE" not in captured["env"]

    def test_mount_over_the_dataset_stops_the_run(self, tmp_path, monkeypatch):
        project, cfg_path, config = _project(tmp_path)
        gen.generate_tasks(config, cfg_path, tmp_path / "tasks", image="img:1")
        with pytest.raises(ValueError, match="answer keys"):
            self._run(
                tmp_path, monkeypatch, cfg_path,
                env_import_path=run_mod._ENV_IMPORT_PATHS["podman"],
                mounts=[{"type": "bind", "source": str(project / DATASET),
                         "target": "/data", "read_only": True}])

    def test_reused_package_with_an_answer_key_stops_the_run(
            self, tmp_path, monkeypatch):
        project, cfg_path, config = _project(tmp_path)
        gen.generate_tasks(config, cfg_path, tmp_path / "tasks", image="img:1")
        gold = tmp_path / "tasks" / "case-001" / "environment" / "gold"
        gold.mkdir()
        (gold / "annotations.yaml").write_text(f"expected: {KEY}\n")
        with pytest.raises(ValueError, match="--regenerate"):
            self._run(tmp_path, monkeypatch, cfg_path)


class TestProvisioningRobustness:
    def test_cyclic_symlink_does_not_abort_staging(self, tmp_path):
        # Path.resolve() raises RuntimeError on a link cycle (Python 3.11/3.12).
        project, cfg_path, _config = _project(tmp_path)
        shared = project / "shared"
        shared.mkdir()
        (shared / "a").symlink_to(shared / "b")
        (shared / "b").symlink_to(shared / "a")
        (shared / "keep.txt").write_text("ok\n")
        raw = yaml.safe_load(cfg_path.read_text())
        raw["dataset"]["workspace"] = {"files": [
            {"dest": "shared", "source": "shared"}]}
        cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
        config = EvalConfig.from_yaml(cfg_path)

        for protected in ([], dataset_protected_paths(config)):
            ws = tmp_path / f"ws{len(protected)}"
            ws.mkdir()
            materialize_shared_files(ws, config, protected_paths=protected)
            assert (ws / "shared" / "keep.txt").is_file()
            assert not (ws / "shared" / "a").exists()

    def test_fingerprints_flag_copies_not_case_inputs(self, tmp_path):
        project, _cfg_path, config = _project(tmp_path)
        prints = dataset_fingerprints(dataset_protected_paths(config))
        tree = tmp_path / "tree"
        (tree / "x").mkdir(parents=True)
        (tree / "x" / "annotations.yaml").write_text(f"expected: {KEY}\n")
        (tree / "x" / "annotations.yaml.bak").write_text(f"expected: {KEY}\n")
        (tree / "x" / "input.yaml").write_bytes(
            (project / DATASET / "case-001" / "input.yaml").read_bytes())
        (tree / "x" / "other.yaml").write_text("different\n")

        found = find_dataset_copies(tree, prints)

        assert [p.name for p in found] == ["annotations.yaml"]

    def test_fingerprints_leave_out_the_excluded_tree(self, tmp_path):
        project, _cfg_path, config = _project(tmp_path)
        # A tree inside the dataset (a broad root) is not its own answer key.
        inner = project / DATASET / "case-001" / "pkg"
        inner.mkdir()
        (inner / "notes.txt").write_text("package content\n")
        prints = dataset_fingerprints(
            dataset_protected_paths(config), exclude=[inner])
        assert not any(name == "notes.txt" for name, _ in prints)
        assert any(name == "annotations.yaml" for name, _ in prints)
