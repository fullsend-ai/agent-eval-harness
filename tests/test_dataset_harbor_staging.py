"""Dataset (answer-key) protection across the Harbor launch surface (#345).

Beyond the task package itself: ``--skill`` uploads, explicit ``--mount``
sources, reused or regenerated packages, and the isolation metadata handed to
the podman environment. Every check that matters is a Bash-style hunt
(``grep -r``/``find``/``cat``) over what the container would receive.
"""

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

from agent_eval.config import EvalConfig
from agent_eval.harbor import run as run_mod
from agent_eval.harbor import tasks as gen
from agent_eval.workspace_provisioning import (
    STAGING_VERSION,
    dataset_protected_paths,
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

    def test_generated_package_records_the_dataset_filtering(self, tmp_path):
        _project_dir, cfg_path, config = _project(tmp_path)
        out = tmp_path / "tasks"
        gen.generate_tasks(config, cfg_path, out, image="img:1")
        task = tomllib.loads((out / "case-001" / "task.toml").read_text())
        assert task["metadata"]["dataset_filter"] == STAGING_VERSION
        run_mod._validate_task_package_reuse(out, config)  # accepted

    def test_multi_step_package_records_the_dataset_filtering(self, tmp_path):
        _project_dir, cfg_path, _config = _project(tmp_path)
        raw = yaml.safe_load(cfg_path.read_text())
        raw["execution"] = {"steps": [
            {"id": "a", "skill": "demo", "arguments": "{{ input.prompt }}"},
            {"id": "b", "skill": "demo", "arguments": "again"},
        ]}
        cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
        config = EvalConfig.from_yaml(cfg_path)
        out = tmp_path / "tasks"
        gen.generate_tasks(config, cfg_path, out, image="img:1")
        task = tomllib.loads((out / "case-001" / "task.toml").read_text())
        assert task["metadata"]["dataset_filter"] == STAGING_VERSION
        run_mod._validate_task_package_reuse(out, config)

    @pytest.mark.parametrize("value", [None, 0, STAGING_VERSION - 1, True, "1"])
    def test_reuse_refuses_a_package_without_the_current_record(
            self, tmp_path, value):
        # A package from before the filtering carries no record; whatever it
        # holds, it is refused and has to be regenerated. Its content is never
        # inspected, so a renamed answer key cannot slip through either.
        _project_dir, cfg_path, config = _project(tmp_path)
        out = tmp_path / "tasks"
        gen.generate_tasks(config, cfg_path, out, image="img:1")
        toml_path = out / "case-001" / "task.toml"
        lines = [ln for ln in toml_path.read_text().splitlines()
                 if not ln.startswith("dataset_filter")]
        if value is not None:
            lines.insert(
                lines.index("[environment]"),
                f"dataset_filter = {json.dumps(value)}")
        toml_path.write_text("\n".join(lines) + "\n")

        with pytest.raises(ValueError, match="--regenerate"):
            run_mod._validate_task_package_reuse(out, config)

    def test_regeneration_makes_an_old_package_reusable(self, tmp_path):
        _project_dir, cfg_path, config = _project(tmp_path)
        out = tmp_path / "tasks"
        gen.generate_tasks(config, cfg_path, out, image="img:1")
        toml_path = out / "case-001" / "task.toml"
        toml_path.write_text("\n".join(
            ln for ln in toml_path.read_text().splitlines()
            if not ln.startswith("dataset_filter")) + "\n")
        with pytest.raises(ValueError, match="--regenerate"):
            run_mod._validate_task_package_reuse(out, config)

        gen.generate_tasks(config, cfg_path, out, image="img:1")

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
            # The Bash hunt below is a real process; harbor may be spelled as
            # a resolved path when it is installed next to the interpreter.
            if Path(command[0]).name != "harbor":
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

    def test_reused_package_from_before_the_filtering_stops_the_run(
            self, tmp_path, monkeypatch):
        project, cfg_path, config = _project(tmp_path)
        gen.generate_tasks(config, cfg_path, tmp_path / "tasks", image="img:1")
        toml_path = tmp_path / "tasks" / "case-001" / "task.toml"
        toml_path.write_text("\n".join(
            ln for ln in toml_path.read_text().splitlines()
            if not ln.startswith("dataset_filter")) + "\n")
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



def _commit_everything(project: Path) -> None:
    """Commit the project, annotations included, as a real repository would."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for argv in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "cases"]):
        subprocess.run(["git", "-C", str(project), *argv], check=True,
                       capture_output=True, env=env)


def git_recovers_key(view: Path) -> bool:
    """Can the committed annotations be read back through git inside *view*?

    The commands an agent in the container would run, with ``safe.directory``
    set the way a read-only mount owned by another user needs.
    """
    path = f"{DATASET}/case-001/annotations.yaml"
    git = ["git", "-c", "safe.directory=*", "-C", str(view)]
    for argv in (["show", f"HEAD:{path}"], ["grep", "-I", KEY, "HEAD"],
                 ["log", "-p", "--all"], ["cat-file", "-p", f"HEAD:{path}"]):
        out = subprocess.run([*git, *argv], capture_output=True, text=True)
        if out.returncode == 0 and KEY in out.stdout:
            return True
    return False


def container_view(project: Path, hidden: list[str], dest: Path) -> Path:
    """The project as the container sees it: each hidden path an empty tmpfs."""
    shutil.copytree(project, dest, symlinks=True)
    for rel in hidden:
        masked = dest / rel
        shutil.rmtree(masked)
        masked.mkdir()
    return dest


class TestGitHistory:
    """Committed annotations must not come back through ``git`` (#345)."""

    def test_the_committed_key_is_recoverable_without_a_mask(self, tmp_path):
        project, _cfg_path, _config = _project(tmp_path)
        _commit_everything(project)
        # Guards the tests below: with only the dataset directory hidden, the
        # key is still one ``git show`` away.
        view = container_view(project, [DATASET], tmp_path / "view")
        assert bash_finds_key(view) == ""
        assert git_recovers_key(view)

    def test_podman_project_mount_hides_the_git_metadata(self, tmp_path):
        project, _cfg_path, config = _project(tmp_path)
        _commit_everything(project)

        hidden = run_mod._podman_project_hide_paths(
            config, {"AGENT_EVAL_PODMAN_PROJECT_DIR": str(project)})

        assert DATASET in hidden and ".git" in hidden
        view = container_view(project, hidden, tmp_path / "view")
        assert bash_finds_key(view) == ""
        assert not git_recovers_key(view)

    def test_nested_repositories_are_hidden_too(self, tmp_path):
        project, _cfg_path, config = _project(tmp_path)
        _commit_everything(project)
        nested = project / "vendor" / "lib"
        nested.mkdir(parents=True)
        (nested / "f.txt").write_text("x\n")
        _commit_everything(nested)
        # A repository inside the dataset is masked with the dataset itself.
        inner = project / DATASET / "case-001" / "sub"
        inner.mkdir()
        (inner / "f.txt").write_text("x\n")
        _commit_everything(inner)

        hidden = run_mod._podman_project_hide_paths(
            config, {"AGENT_EVAL_PODMAN_PROJECT_DIR": str(project)})

        assert sorted(hidden) == sorted([DATASET, ".git", "vendor/lib/.git"])

    def test_gitdir_file_pointing_into_the_project_is_followed(self, tmp_path):
        project, _cfg_path, config = _project(tmp_path)
        _commit_everything(project)
        # A submodule-style checkout: its ``.git`` is a file naming a metadata
        # directory elsewhere in the project (here outside any ``.git``).
        meta = project / "meta" / "sub.git"
        shutil.copytree(project / ".git", meta)
        sub = project / "sub"
        sub.mkdir()
        (sub / ".git").write_text(f"gitdir: {meta}\n")

        hidden = run_mod._podman_project_hide_paths(
            config, {"AGENT_EVAL_PODMAN_PROJECT_DIR": str(project)})

        assert "meta/sub.git" in hidden
        assert "sub/.git" not in hidden   # a file: nothing to overlay

    def test_gitdir_outside_the_project_is_not_in_the_container(self, tmp_path):
        project, _cfg_path, config = _project(tmp_path)
        outside = tmp_path / "elsewhere.git"
        outside.mkdir()
        sub = project / "sub"
        sub.mkdir()
        (sub / ".git").write_text(f"gitdir: {outside}\n")

        hidden = run_mod._podman_project_hide_paths(
            config, {"AGENT_EVAL_PODMAN_PROJECT_DIR": str(project)})

        assert hidden == [DATASET]

    def test_project_without_the_dataset_needs_no_git_mask(self, tmp_path):
        _project_dir, _cfg_path, config = _project(tmp_path)
        other = tmp_path / "other"
        (other / ".git").mkdir(parents=True)

        assert run_mod._podman_project_hide_paths(
            config, {"AGENT_EVAL_PODMAN_PROJECT_DIR": str(other)}) == []

    def test_skill_root_copy_leaves_the_repository_history_out(self, tmp_path):
        project, _cfg_path, config = _project(tmp_path)
        _commit_everything(project)
        stage = tmp_path / "stage"
        stage.mkdir()

        roots = run_mod._dataset_free_skill_roots(config, [project], stage)

        assert roots != [project]
        assert (roots[0] / "skills" / "demo" / "SKILL.md").is_file()
        assert not (roots[0] / ".git").exists()
        assert bash_finds_key(roots[0]) == ""

    def test_shared_directory_holding_the_dataset_is_copied_without_git(
            self, tmp_path):
        project, cfg_path, _config = _project(tmp_path)
        _commit_everything(project)
        raw = yaml.safe_load(cfg_path.read_text())
        raw["dataset"]["workspace"] = {"files": [
            {"dest": "proj", "source": "."}]}
        cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
        config = EvalConfig.from_yaml(cfg_path)
        ws = tmp_path / "ws"
        ws.mkdir()

        materialize_shared_files(
            ws, config, protected_paths=dataset_protected_paths(config))

        assert (ws / "proj" / "skills" / "demo" / "SKILL.md").is_file()
        assert not (ws / "proj" / ".git").exists()
        assert bash_finds_key(ws) == ""

    def test_shared_directory_without_the_dataset_keeps_its_git(self, tmp_path):
        project, cfg_path, _config = _project(tmp_path)
        plugin = project / "plugin"
        plugin.mkdir()
        (plugin / "SKILL.md").write_text("# p\n")
        _commit_everything(plugin)
        raw = yaml.safe_load(cfg_path.read_text())
        raw["dataset"]["workspace"] = {"files": [
            {"dest": "plugin", "source": "plugin"}]}
        cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
        config = EvalConfig.from_yaml(cfg_path)
        ws = tmp_path / "ws"
        ws.mkdir()

        materialize_shared_files(
            ws, config, protected_paths=dataset_protected_paths(config))

        assert (ws / "plugin" / ".git").is_dir()
