"""The eval dataset (annotations, reference answers) stays out of sandboxed agents.

Covers the Harbor task package and the fullsend upload: whatever the agent can
reach in them — including through Bash (``grep -r``, ``cat``, ``find``) — holds
no dataset content. Local runners are not covered: they have no filesystem
isolation, so a permission rule alone cannot protect the answer key there.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from agent_eval.agent.fullsend import FullsendRunner, materialize_symlinks
from agent_eval.config import EvalConfig
from agent_eval.harbor import tasks as gen
from agent_eval.harbor.run import _podman_project_hide_paths
from agent_eval.workspace_provisioning import materialize_shared_files

KEY = "ANSWER-KEY-7f3a"


@pytest.fixture(autouse=True)
def _project_cwd(tmp_path, monkeypatch):
    """The project root is the CWD; shared-file sources resolve against it."""
    (tmp_path / "project").mkdir()
    monkeypatch.chdir(tmp_path / "project")


def bash_finds_key(root: Path) -> str:
    """What an agent running Bash in *root* sees when it hunts for the answer key."""
    out = subprocess.run(
        ["bash", "-c", f"grep -rIl --binary-files=without-match {KEY} . ; "
                       f"find . -name 'annotations.yaml' -o -name 'reference.md'"],
        cwd=root, capture_output=True, text=True)
    return out.stdout.strip()


def _project(tmp_path, *, shared=None, dataset="skills/demo/evals/cases"):
    project = tmp_path / "project"
    cases = project / dataset
    case = cases / "case-001"
    case.mkdir(parents=True)
    (case / "input.yaml").write_text(yaml.safe_dump({"prompt": "hello"}))
    (case / "answers.yaml").write_text("guidance: ask nicely\n")
    (case / "annotations.yaml").write_text(f"expected: {KEY}\n")
    (case / "reference.md").write_text(f"# {KEY}\n")
    (project / "skills" / "demo" / "SKILL.md").write_text("# demo skill\n")
    raw = {
        "name": "iso",
        "execution": {"skill": "demo", "arguments": "{{ input.prompt }}"},
        "dataset": {"path": dataset, "schema": "x"},
        "outputs": [{"path": "out", "schema": "o"}],
        "judges": [{"name": "j", "check": "return (True, 'ok')\n"}],
    }
    if shared:
        raw["dataset"]["workspace"] = {"files": shared}
    cfg_path = project / "eval.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return project, cfg_path, EvalConfig.from_yaml(cfg_path)


class TestHarbor:
    def test_environment_holds_no_answer_key(self, tmp_path):
        project, cfg_path, config = _project(tmp_path)
        out = tmp_path / "tasks"
        gen.generate_tasks(config, cfg_path, out, image="img:1")
        env_dir = out / "case-001" / "environment"
        assert (env_dir / "input.yaml").is_file()
        assert bash_finds_key(env_dir) == ""

    @pytest.mark.parametrize("source", [
        "skills/demo/evals/cases/case-001/reference.md",  # a dataset file
        "skills/demo/evals/cases",                          # the dataset itself
    ])
    def test_shared_file_from_dataset_is_skipped(self, tmp_path, source):
        project, cfg_path, config = _project(
            tmp_path, shared=[{"dest": "ref", "source": source}])
        out = tmp_path / "tasks"
        gen.generate_tasks(config, cfg_path, out, image="img:1")
        env_dir = out / "case-001" / "environment"
        assert not (env_dir / "ref").exists()
        assert bash_finds_key(env_dir) == ""

    def test_shared_directory_containing_dataset_is_copied_without_it(
            self, tmp_path):
        project, cfg_path, config = _project(
            tmp_path, shared=[{"dest": "demo", "source": "skills/demo"}])
        out = tmp_path / "tasks"
        gen.generate_tasks(config, cfg_path, out, image="img:1")
        env_dir = out / "case-001" / "environment"
        assert (env_dir / "demo" / "SKILL.md").is_file()
        assert not (env_dir / "demo" / "evals" / "cases").exists()
        assert bash_finds_key(env_dir) == ""

    def test_materialize_without_protection_is_unchanged(self, tmp_path):
        # Local/EvalHub callers pass no protected paths and keep their behavior.
        project, cfg_path, config = _project(
            tmp_path, shared=[{"dest": "demo", "source": "skills/demo"}])
        target = tmp_path / "ws"
        target.mkdir()
        materialize_shared_files(target, config)
        assert (target / "demo" / "evals" / "cases" / "case-001"
                / "annotations.yaml").is_file()

    def test_podman_project_mount_masks_dataset(self, tmp_path):
        project, _cfg_path, config = _project(tmp_path)
        env = {"AGENT_EVAL_PODMAN_PROJECT_DIR": str(project)}
        assert _podman_project_hide_paths(config, env) == [
            "skills/demo/evals/cases"]

    def test_podman_project_mount_outside_dataset_hides_nothing(self, tmp_path):
        project, _cfg_path, config = _project(tmp_path)
        other = tmp_path / "other"
        other.mkdir()
        assert _podman_project_hide_paths(
            config, {"AGENT_EVAL_PODMAN_PROJECT_DIR": str(other)}) == []
        assert _podman_project_hide_paths(config, {}) == []

    def test_podman_project_mount_that_is_the_dataset_is_refused(self, tmp_path):
        project, _cfg_path, config = _project(tmp_path)
        dataset = project / "skills" / "demo" / "evals" / "cases"
        with pytest.raises(ValueError, match="answer keys"):
            _podman_project_hide_paths(
                config, {"AGENT_EVAL_PODMAN_PROJECT_DIR": str(dataset)})


class TestPodmanMask:
    @pytest.fixture(autouse=True)
    def _harbor(self):
        pytest.importorskip("harbor.environments.base")

    def test_mask_args(self, monkeypatch):
        from agent_eval.harbor.podman import _project_hide_args
        monkeypatch.setenv("AGENT_EVAL_PODMAN_PROJECT_HIDE",
                           json.dumps(["evals/cases", "x/y"]))
        assert _project_hide_args("/opt/project") == [
            "--mount",
            "type=tmpfs,destination=/opt/project/evals/cases,ro=true,notmpcopyup",
            "--mount",
            "type=tmpfs,destination=/opt/project/x/y,ro=true,notmpcopyup",
        ]

    def test_mask_path_with_a_colon_is_one_path(self, monkeypatch):
        # os.pathsep-joined metadata used to split this into two masks.
        from agent_eval.harbor.podman import _project_hide_args
        monkeypatch.setenv("AGENT_EVAL_PODMAN_PROJECT_HIDE",
                           json.dumps(["evals:v2/cases"]))
        assert _project_hide_args("/opt/project") == [
            "--mount",
            "type=tmpfs,destination=/opt/project/evals:v2/cases,ro=true,notmpcopyup",
        ]

    @pytest.mark.parametrize("bad", [
        '["/abs"]', '["../up"]', '["a/../b"]', '["."]', '["a,b"]', '[""]',
        "not-json", '{"a": 1}', "[1]", "evals/cases",
    ])
    def test_mask_rejects_bad_metadata(self, monkeypatch, bad):
        from agent_eval.harbor.podman import _project_hide_args
        monkeypatch.setenv("AGENT_EVAL_PODMAN_PROJECT_HIDE", bad)
        with pytest.raises(ValueError):
            _project_hide_args("/opt/project")

    @pytest.mark.parametrize("value", [None, "", "  "])
    def test_missing_metadata_refuses_the_mount(self, monkeypatch, value):
        from agent_eval.harbor.podman import _project_hide_args
        if value is None:
            monkeypatch.delenv("AGENT_EVAL_PODMAN_PROJECT_HIDE", raising=False)
        else:
            monkeypatch.setenv("AGENT_EVAL_PODMAN_PROJECT_HIDE", value)
        monkeypatch.delenv("AGENT_EVAL_PODMAN_PROJECT_UNSAFE_ALLOW", raising=False)
        with pytest.raises(ValueError, match="answer keys"):
            _project_hide_args("/opt/project")

    def test_unsafe_opt_in_is_explicit(self, monkeypatch):
        from agent_eval.harbor.podman import _project_hide_args
        monkeypatch.delenv("AGENT_EVAL_PODMAN_PROJECT_HIDE", raising=False)
        monkeypatch.setenv("AGENT_EVAL_PODMAN_PROJECT_UNSAFE_ALLOW", "yes")
        with pytest.raises(ValueError):
            _project_hide_args("/opt/project")
        monkeypatch.setenv("AGENT_EVAL_PODMAN_PROJECT_UNSAFE_ALLOW", "1")
        assert _project_hide_args("/opt/project") == []

    @pytest.mark.skipif(shutil.which("podman") is None, reason="podman not installed")
    def test_actual_mount_hides_the_dataset_from_bash(self, tmp_path, monkeypatch):
        """A real ``podman run``: the masked directory is empty, Bash included.

        The project is bind-mounted read-only and the dataset overlaid with the
        tmpfs the environment builds; ``tmpcopyup`` would have copied the
        underlying files into it.
        """
        from agent_eval.harbor.podman import _project_hide_args
        image = os.environ.get(
            "AGENT_EVAL_TEST_PODMAN_IMAGE", "docker.io/library/busybox")
        if subprocess.run(["podman", "image", "exists", image]).returncode != 0:
            pytest.skip(f"image {image} is not available locally")
        project = tmp_path / "project"
        case = project / "evals" / "cases" / "case-001"
        case.mkdir(parents=True)
        (case / "annotations.yaml").write_text(f"expected: {KEY}\n")
        (project / "SKILL.md").write_text("# skill\n")
        monkeypatch.setenv(
            "AGENT_EVAL_PODMAN_PROJECT_HIDE", json.dumps(["evals/cases"]))
        mask = _project_hide_args("/opt/project")
        out = subprocess.run(
            ["podman", "run", "--rm", "-v", f"{project}:/opt/project:ro", *mask,
             image, "sh", "-c",
             f"grep -rl {KEY} /opt/project; "
             "find /opt/project -name annotations.yaml; "
             "ls -A /opt/project/evals/cases; cat /opt/project/SKILL.md"],
            capture_output=True, text=True)
        assert KEY not in out.stdout and "annotations.yaml" not in out.stdout
        assert "# skill" in out.stdout


class TestFullsend:
    def _ws(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        return ws

    def test_linked_tree_holding_dataset_is_copied_without_it(self, tmp_path):
        project, _cfg, config = _project(tmp_path)
        ws = self._ws(tmp_path)
        (ws / "skills").symlink_to(project / "skills")
        protected = [config.resolve_path(config.dataset.path)]

        materialize_symlinks(ws, [project.resolve()], protected)

        assert (ws / "skills" / "demo" / "SKILL.md").is_file()
        assert not (ws / "skills" / "demo" / "evals" / "cases").exists()
        assert bash_finds_key(ws) == ""

    def test_link_to_dataset_is_dropped(self, tmp_path):
        project, _cfg, config = _project(tmp_path)
        ws = self._ws(tmp_path)
        (ws / "cases").symlink_to(project / "skills/demo/evals/cases")
        (ws / "one").symlink_to(
            project / "skills/demo/evals/cases/case-001/annotations.yaml")
        protected = [config.resolve_path(config.dataset.path)]

        materialize_symlinks(ws, [project.resolve()], protected)

        assert not (ws / "cases").exists() and not (ws / "cases").is_symlink()
        assert not (ws / "one").exists() and not (ws / "one").is_symlink()
        assert bash_finds_key(ws) == ""

    def test_nested_link_to_dataset_is_skipped(self, tmp_path):
        project, _cfg, config = _project(tmp_path)
        (project / "skills" / "demo" / "gold").symlink_to(
            project / "skills/demo/evals/cases/case-001")
        ws = self._ws(tmp_path)
        (ws / "skills").symlink_to(project / "skills")
        protected = [config.resolve_path(config.dataset.path)]

        materialize_symlinks(ws, [project.resolve()], protected)

        assert not (ws / "skills" / "demo" / "gold").exists()
        assert bash_finds_key(ws) == ""

    def test_without_protection_links_copy_everything(self, tmp_path):
        project, _cfg, _config = _project(tmp_path)
        ws = self._ws(tmp_path)
        (ws / "skills").symlink_to(project / "skills")
        materialize_symlinks(ws, [project.resolve()])
        assert "annotations.yaml" in bash_finds_key(ws)

    def test_runner_protects_the_dataset(self, tmp_path):
        project, cfg_path, _config = _project(tmp_path)
        raw = yaml.safe_load(cfg_path.read_text())
        raw["runner"] = {"type": "fullsend", "agent": "a"}
        cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
        config = EvalConfig.from_yaml(cfg_path)
        runner = FullsendRunner.from_config(config)
        assert runner._protected_paths == [
            (project / "skills/demo/evals/cases").resolve()]
