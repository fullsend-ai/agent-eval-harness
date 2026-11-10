"""Tests for staging plugin dirs inside the case workspace.

Passing ``--plugin-dir <path>`` puts that path verbatim in the session's
system context (the stream-json init event registers the plugin under it).
Bash is not path-gated in isolated workspaces, so a path pointing outside the
workspace lets a case agent follow it out of its throwaway workspace and into
the real project. ``stage_plugin_dir`` copies the plugin's discoverable
content into the workspace so the runner can pass THAT path instead.

The last section covers what artifact collection must ignore in a case
workspace: the staged plugin copy and the harness's own run files.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval.agent.claude_code import ClaudeCodeRunner, stage_plugin_dir
from agent_eval.config import EvalConfig, OutputConfig


def make_plugin(root, name="demo-plugin", manifest=..., skills=("my-skill",)):
    """Create a minimal plugin: manifest + skills/<name>/SKILL.md."""
    plugin = root / name
    (plugin / ".claude-plugin").mkdir(parents=True)
    if manifest is ...:
        manifest = {"name": name}
    (plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest))
    for skill in skills:
        skill_dir = plugin / "skills" / skill
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"# {skill}\n")
    return plugin


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class TestStagePluginDir:
    """Unit tests for the staging helper."""

    def test_copies_manifest_and_skill_root(self, tmp_path):
        plugin = make_plugin(tmp_path / "plugins")
        ws = tmp_path / "ws"
        ws.mkdir()
        staged = stage_plugin_dir(plugin, ws)
        assert staged == ws / ".staged-plugins" / "demo-plugin"
        assert (staged / ".claude-plugin" / "plugin.json").is_file()
        # The staged tree must remain discoverable: a SKILL.md under the
        # staged skill root is what Claude Code's plugin loader looks for.
        assert (staged / "skills" / "my-skill" / "SKILL.md").is_file()

    def test_copies_optional_dirs(self, tmp_path):
        plugin = make_plugin(tmp_path / "plugins")
        (plugin / "commands").mkdir()
        (plugin / "commands" / "go.md").write_text("cmd")
        (plugin / "agents").mkdir()
        (plugin / "agents" / "helper.md").write_text("agent")
        (plugin / "hooks").mkdir()
        (plugin / "hooks" / "hooks.json").write_text("{}")
        ws = tmp_path / "ws"
        ws.mkdir()
        staged = stage_plugin_dir(plugin, ws)
        assert (staged / "commands" / "go.md").is_file()
        assert (staged / "agents" / "helper.md").is_file()
        assert (staged / "hooks" / "hooks.json").is_file()

    def test_copies_plugin_mcp_json(self, tmp_path):
        plugin = make_plugin(tmp_path / "plugins")
        (plugin / ".mcp.json").write_text('{"mcpServers": {"ship-status": {}}}')
        ws = tmp_path / "ws"
        ws.mkdir()
        staged = stage_plugin_dir(plugin, ws)
        assert (staged / ".mcp.json").is_file()
        assert "ship-status" in (staged / ".mcp.json").read_text()

    def test_escaping_mcp_json_symlink_is_refused(self, tmp_path):
        plugin = make_plugin(tmp_path / "plugins")
        secret = tmp_path / "outside-mcp.json"
        secret.write_text('{"secret": true}')
        (plugin / ".mcp.json").symlink_to(secret)
        ws = tmp_path / "ws"
        ws.mkdir()
        staged = stage_plugin_dir(plugin, ws)
        assert not (staged / ".mcp.json").exists()

    def test_skips_git_node_modules_and_unlisted_files(self, tmp_path):
        plugin = make_plugin(tmp_path / "plugins")
        # Bulk inside a copied tree is pruned by ignore patterns...
        (plugin / "skills" / ".git").mkdir()
        (plugin / "skills" / ".git" / "config").write_text("x")
        (plugin / "skills" / "node_modules" / "pkg").mkdir(parents=True)
        (plugin / "skills" / "node_modules" / "pkg" / "i.js").write_text("x")
        # ...and content outside the discovery set is never copied at all.
        (plugin / ".git").mkdir()
        (plugin / ".git" / "config").write_text("x")
        (plugin / "README.md").write_text("readme")
        ws = tmp_path / "ws"
        ws.mkdir()
        staged = stage_plugin_dir(plugin, ws)
        assert not (staged / "skills" / ".git").exists()
        assert not (staged / "skills" / "node_modules").exists()
        assert not (staged / ".git").exists()
        assert not (staged / "README.md").exists()

    def test_manifest_declared_skill_root(self, tmp_path):
        plugin = make_plugin(
            tmp_path / "plugins",
            manifest={"name": "p", "skills": "custom-skills"},
            skills=(),
        )
        alpha = plugin / "custom-skills" / "alpha"
        alpha.mkdir(parents=True)
        (alpha / "SKILL.md").write_text("# alpha\n")
        ws = tmp_path / "ws"
        ws.mkdir()
        staged = stage_plugin_dir(plugin, ws)
        assert (staged / "custom-skills" / "alpha" / "SKILL.md").is_file()

    def test_idempotent_per_workspace(self, tmp_path):
        plugin = make_plugin(tmp_path / "plugins")
        ws = tmp_path / "ws"
        ws.mkdir()
        staged = stage_plugin_dir(plugin, ws)
        sentinel = staged / "sentinel.txt"
        sentinel.write_text("kept")
        again = stage_plugin_dir(plugin, ws)
        assert again == staged
        assert sentinel.read_text() == "kept", "second call must not recopy"

    def test_internal_symlinks_materialized_external_rejected(self, tmp_path):
        """Symlinks resolving inside the plugin are materialized (the staged
        copy must be self-contained); symlinks escaping the plugin are
        REFUSED — symlinks=False would otherwise copy the host target into
        the workspace where the agent can read it (CWE-59 -> CWE-200)."""
        plugin = make_plugin(tmp_path / "plugins")
        real = plugin / "skills" / "my-skill" / "real.md"
        real.write_text("content")
        (plugin / "skills" / "my-skill" / "link.md").symlink_to(real)
        secret = tmp_path / "outside-secret.txt"
        secret.write_text("hostcreds")
        (plugin / "skills" / "my-skill" / "leak.md").symlink_to(secret)
        ws = tmp_path / "ws"
        ws.mkdir()
        dest = stage_plugin_dir(plugin, ws)
        staged_skill = dest / "skills" / "my-skill"
        assert (staged_skill / "link.md").is_file()
        assert not (staged_skill / "link.md").is_symlink()
        assert not (staged_skill / "leak.md").exists(), (
            "a symlink escaping the plugin must not be staged")

    def test_escaping_symlinked_copy_root_is_refused(self, tmp_path):
        """copytree(symlinks=False) follows a source dir that is ITSELF a
        symlink, and the ignore callback only sees entries inside walked
        directories — an escaping link at the copy root (scripts ->
        external dir) would otherwise materialize the external tree
        wholesale (CWE-59 -> CWE-200)."""
        plugin = make_plugin(tmp_path / "plugins")
        external = tmp_path / "outside"
        external.mkdir()
        (external / "secret.txt").write_text("hostcreds")
        (plugin / "scripts").symlink_to(external)
        internal = plugin / "tools"
        internal.mkdir()
        (internal / "helper.py").write_text("print('ok')\n")
        (plugin / "hooks").symlink_to(internal)
        ws = tmp_path / "ws"
        ws.mkdir()
        dest = stage_plugin_dir(plugin, ws)
        assert not (dest / "scripts").exists(), (
            "a copy root symlinked outside the plugin must not be staged")
        assert "hostcreds" not in "".join(
            p.read_text() for p in dest.rglob("*") if p.is_file())
        assert (dest / "hooks" / "helper.py").is_file(), (
            "a copy root symlinked WITHIN the plugin is materialized")

    def test_copy_reads_from_checked_canonical_path(self, tmp_path, monkeypatch):
        """The containment check resolves each copy root; the copy must read
        from that SAME canonical path. Copying from the symlink would
        re-follow it at copy time — a check/use race (CWE-367) where a
        concurrent writer swaps the link between resolve() and copytree()."""
        import shutil as shutil_mod

        import agent_eval.agent.claude_code as claude_code_mod

        plugin = make_plugin(tmp_path / "plugins")
        internal = plugin / "tools"
        internal.mkdir()
        (internal / "helper.py").write_text("print('ok')\n")
        (plugin / "scripts").symlink_to(internal)
        ws = tmp_path / "ws"
        ws.mkdir()
        seen = []

        # Proxy only the module-under-test's shutil reference: copytree
        # recurses into itself positionally, so patching the global would
        # intercept (and break) its internal recursive calls too.
        class ShutilProxy:
            def __getattr__(self, name):
                return getattr(shutil_mod, name)

            @staticmethod
            def copytree(src, dst, **kwargs):
                seen.append(Path(src))
                return shutil_mod.copytree(src, dst, **kwargs)

        monkeypatch.setattr(claude_code_mod, "shutil", ShutilProxy())
        dest = stage_plugin_dir(plugin, ws)
        assert (dest / "scripts" / "helper.py").is_file()
        assert seen, "copytree was not invoked"
        for src in seen:
            assert not src.is_symlink(), (
                f"copy must read the checked canonical path, not a symlink: {src}")

    def test_symlink_loop_is_skipped_not_fatal(self, tmp_path):
        """A self-referential link raises RuntimeError from resolve() on
        Python 3.11/3.12 and OSError(ELOOP) later — either way staging must
        skip it, not crash the case."""
        plugin = make_plugin(tmp_path / "plugins")
        loop = plugin / "skills" / "my-skill" / "loop"
        loop.symlink_to(loop)
        ws = tmp_path / "ws"
        ws.mkdir()
        dest = stage_plugin_dir(plugin, ws)
        assert (dest / "skills" / "my-skill" / "SKILL.md").is_file()
        assert not (dest / "skills" / "my-skill" / "loop").exists()

    def test_plugin_without_skills_is_tolerated(self, tmp_path):
        # A Claude plugin may ship only commands/agents/hooks; staging must
        # not fail a configuration the unstaged path would have accepted.
        plugin = make_plugin(tmp_path / "plugins", skills=())
        (plugin / "commands").mkdir()
        (plugin / "commands" / "go.md").write_text("cmd")
        ws = tmp_path / "ws"
        ws.mkdir()
        staged = stage_plugin_dir(plugin, ws)
        assert (staged / ".claude-plugin" / "plugin.json").is_file()
        assert (staged / "commands" / "go.md").is_file()


    def test_declared_missing_skill_root_propagates(self, tmp_path):
        """A manifest that DECLARES skills must not be silently tolerated when
        the declaration is broken — staging it would only resurface later as
        an undiscoverable slash command."""
        plugin = make_plugin(tmp_path / "plugins")
        (plugin / ".claude-plugin" / "plugin.json").write_text(
            '{"name": "p", "skills": "does-not-exist"}')
        ws = tmp_path / "ws"
        ws.mkdir()
        with pytest.raises(FileNotFoundError):
            stage_plugin_dir(plugin, ws)

    def test_malformed_manifest_propagates(self, tmp_path):
        plugin = make_plugin(tmp_path / "plugins")
        (plugin / ".claude-plugin" / "plugin.json").write_text("{not json")
        ws = tmp_path / "ws"
        ws.mkdir()
        with pytest.raises(ValueError):
            stage_plugin_dir(plugin, ws)


class TestClaudeCodeRunnerStaging:
    """Integration through ClaudeCodeRunner.execute with a stub `claude` on
    PATH that dumps its argv into the workspace (its cwd)."""

    JSON_OK = ('{"type":"result","subtype":"success","is_error":false,'
               '"num_turns":3,"total_cost_usd":0.1,"result":"done"}\n')

    def _stub_claude(self, tmp_path, monkeypatch):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        stub = bindir / "claude"
        stub.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$@\" > argv.txt\n"
            "cat > /dev/null\n"
            f"cat <<'JSON_EOF'\n{self.JSON_OK}JSON_EOF\n"
        )
        stub.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    def _argv(self, ws):
        return (ws / "argv.txt").read_text().splitlines()

    def _plugin_dir_args(self, argv):
        return [argv[i + 1] for i, a in enumerate(argv) if a == "--plugin-dir"]

    def test_staged_path_replaces_real_path(self, tmp_path, monkeypatch):
        self._stub_claude(tmp_path, monkeypatch)
        plugin = make_plugin(tmp_path / "plugins")
        ws = tmp_path / "ws"
        ws.mkdir()
        runner = ClaudeCodeRunner(plugin_dirs=[str(plugin)])
        result = runner.execute(
            target="epic-decompose", args="", workspace=ws,
            model="m", timeout_s=60)
        assert result.exit_code == 0, result.stderr
        argv = self._argv(ws)
        [staged] = self._plugin_dir_args(argv)
        assert staged.startswith(str(ws)), (
            "--plugin-dir must point inside the workspace")
        assert str(plugin) not in "\n".join(argv), (
            "the real plugin path must never enter the session")
        assert (Path(staged) / "skills" / "my-skill" / "SKILL.md").is_file()

    def test_staging_is_the_default_behavior(self, tmp_path, monkeypatch):
        """No opt-in flag: isolation is the harness's contract, so any plugin
        living outside the workspace is always staged."""
        self._stub_claude(tmp_path, monkeypatch)
        plugin = make_plugin(tmp_path / "plugins")
        ws = tmp_path / "ws"
        ws.mkdir()
        runner = ClaudeCodeRunner(plugin_dirs=[str(plugin)])
        result = runner.execute(
            target="epic-decompose", args="", workspace=ws,
            model="m", timeout_s=60)
        assert result.exit_code == 0, result.stderr
        [staged] = self._plugin_dir_args(self._argv(ws))
        assert staged.startswith(str(ws))
        assert (ws / ".staged-plugins").exists()

    def test_plugin_inside_workspace_passes_through(self, tmp_path, monkeypatch):
        """A plugin already inside the workspace discloses nothing outside the
        sandbox and is passed through unchanged — this is also what makes
        workspace_mode: repo safe (the workspace IS the project there, and
        staging would write .staged-plugins/ into the user's repo)."""
        self._stub_claude(tmp_path, monkeypatch)
        ws = tmp_path / "ws"
        ws.mkdir()
        plugin = make_plugin(ws / "vendored")
        runner = ClaudeCodeRunner(plugin_dirs=[str(plugin)])
        result = runner.execute(
            target="epic-decompose", args="", workspace=ws,
            model="m", timeout_s=60)
        assert result.exit_code == 0, result.stderr
        assert self._plugin_dir_args(self._argv(ws)) == [str(plugin.resolve())]
        assert not (ws / ".staged-plugins").exists()

    def test_plugin_root_scripts_are_staged(self, tmp_path, monkeypatch):
        """Skills commonly run ${CLAUDE_PLUGIN_ROOT}/scripts/... at runtime; a
        staged copy without scripts/ would break them."""
        self._stub_claude(tmp_path, monkeypatch)
        plugin = make_plugin(tmp_path / "plugins")
        (plugin / "scripts").mkdir()
        (plugin / "scripts" / "helper.py").write_text("print('hi')\n")
        ws = tmp_path / "ws"
        ws.mkdir()
        runner = ClaudeCodeRunner(plugin_dirs=[str(plugin)])
        result = runner.execute(
            target="epic-decompose", args="", workspace=ws,
            model="m", timeout_s=60)
        assert result.exit_code == 0, result.stderr
        [staged] = self._plugin_dir_args(self._argv(ws))
        assert (Path(staged) / "scripts" / "helper.py").is_file()

    def test_plugin_mcp_json_is_staged(self, tmp_path, monkeypatch):
        """Claude Code loads plugin MCP servers from .mcp.json at the plugin
        root. Isolated evals must stage that file or the skill never sees
        those tools."""
        self._stub_claude(tmp_path, monkeypatch)
        plugin = make_plugin(tmp_path / "plugins")
        (plugin / ".mcp.json").write_text('{"mcpServers": {"ship-status": {}}}')
        ws = tmp_path / "ws"
        ws.mkdir()
        runner = ClaudeCodeRunner(plugin_dirs=[str(plugin)])
        result = runner.execute(
            target="epic-decompose", args="", workspace=ws,
            model="m", timeout_s=60)
        assert result.exit_code == 0, result.stderr
        [staged] = self._plugin_dir_args(self._argv(ws))
        assert (Path(staged) / ".mcp.json").is_file()

    def test_duplicate_plugin_basenames_fail_loud(self, tmp_path, monkeypatch):
        self._stub_claude(tmp_path, monkeypatch)
        first = make_plugin(tmp_path / "a", name="pkg")
        second = make_plugin(tmp_path / "b", name="pkg")
        ws = tmp_path / "ws"
        ws.mkdir()
        runner = ClaudeCodeRunner(plugin_dirs=[str(first), str(second)])
        result = runner.execute(
            target="epic-decompose", args="", workspace=ws,
            model="m", timeout_s=60)
        assert result.exit_code == -1
        assert "same directory name" in result.stderr

    def test_repo_mode_skips_staging_entirely(self, tmp_path, monkeypatch):
        """workspace_mode: repo runs in the user's real repository — there is
        no isolation boundary for staging to defend, and staging an external
        plugin there would write .staged-plugins/ into the repo, polluting it
        and reading back as a spurious repo modification."""
        self._stub_claude(tmp_path, monkeypatch)
        plugin = make_plugin(tmp_path / "plugins")
        ws = tmp_path / "repo"
        ws.mkdir()
        runner = ClaudeCodeRunner(
            plugin_dirs=[str(plugin)], workspace_mode="repo")
        result = runner.execute(
            target="epic-decompose", args="", workspace=ws,
            model="m", timeout_s=60)
        assert result.exit_code == 0, result.stderr
        assert self._plugin_dir_args(self._argv(ws)) == [str(plugin.resolve())]
        assert not (ws / ".staged-plugins").exists(), (
            "repo mode must never write .staged-plugins/ into the user's repo")


class TestCollectIgnoresStagedPlugins:
    """collect.py diffs the case workspace against its initial commit to find
    agent-modified files; the staged plugin copy lands after that commit and
    must not be reported (it would flood judge context with plugin content).

    The same walk drops the harness's own run files — case-mode
    ``output/stdout.log``/``stderr.log``, ``run_result.json`` and the fullsend
    ``.fullsend-runs/`` tree — by exact path, leaving every other ``output/``
    file as agent evidence."""

    def _load_collect(self):
        import importlib.util
        path = (Path(__file__).parent.parent
                / "skills" / "eval-run" / "scripts" / "collect.py")
        spec = importlib.util.spec_from_file_location("eval_run_collect", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_staged_plugins_excluded_from_modified_files(self, tmp_path):
        from types import SimpleNamespace
        collect = self._load_collect()
        case = tmp_path / "case-001"
        case.mkdir()
        (case / "input.yaml").write_text("prompt: x\n")
        env = {**os.environ,
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                    ["git", "commit", "-q", "-m", "initial"]):
            subprocess.run(cmd, cwd=case, env=env, check=True)
        staged = case / ".staged-plugins" / "demo" / "skills" / "my-skill"
        staged.mkdir(parents=True)
        (staged / "SKILL.md").write_text("---\nname: my-skill\n---\n")
        (case / "artifacts").mkdir()
        (case / "artifacts" / "out.md").write_text("real agent output\n")

        modified = collect._collect_modified_files(
            case, SimpleNamespace(outputs=[]))
        rels = [rel for rel, _ in modified]
        assert "artifacts/out.md" in rels
        assert not any(r.startswith(".staged-plugins") for r in rels), (
            "staged plugin content must never surface as agent-modified files")

    def test_run_internals_excluded_and_agent_output_kept(self, tmp_path):
        """Per-case workspaces are diffed against their initial commit, so the
        ``output/stdout.log``/``stderr.log`` streams the harness writes there
        (and the fullsend ``.fullsend-runs/`` tree) would otherwise land in
        ``_modified/``. The exclusion is exact — an undeclared
        ``output/report.md``, a ``project/output/stdout.log`` and an
        ``output/stdout.log.bak`` stay evidence — and the skill's own root
        streams and the run's canonical trace are separate files."""
        sentinel = "HARNESS INTERNAL SENTINEL"
        collect = self._load_collect()
        workspace = tmp_path / "workspace"
        case = workspace / "cases" / "case-001"
        _write(case / "output" / "epic" / "report.md", "# Epic\n")
        _write(case / "output" / "stdout.log", "initial stream\n")
        _write(case / ".fullsend-runs" / "fs-1" / "output.jsonl", "{}\n")
        env = {**os.environ,
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                    ["git", "commit", "-q", "-m", "initial"]):
            subprocess.run(cmd, cwd=case, env=env, check=True)

        # Internals: tracked-and-modified plus untracked.
        _write(case / "output" / "stdout.log", sentinel * 100)
        _write(case / ".fullsend-runs" / "fs-1" / "output.jsonl",
               sentinel * 100)
        _write(case / "output" / "stderr.log", sentinel * 100)
        _write(case / ".fullsend-runs" / "fs-2" / "downloads" / "x.bin",
               sentinel)
        _write(case / "run_result.json", sentinel * 100)
        # A skill's own root streams are workspace evidence, not internals.
        _write(case / "stdout.log", "skill's own stdout\n")
        _write(case / "stderr.log", "skill's own stderr\n")
        # Real skill output, including names the exclusion must not widen to.
        _write(case / "output" / "report.md", "agent artifact\n")
        _write(case / "output" / "stdout.log.bak", "similar name\n")
        _write(case / "project" / "output" / "stdout.log", "nested output\n")
        _write(case / "batch.yaml-notes.md", "similar root name\n")
        _write(case / "batch.yaml" / "notes.md", "dir named like a root file\n")
        _write(case / ".fullsend-runs-notes.md", "similar dir name\n")
        _write(case / "output" / "epic" / "report.md", "# Epic v2\n")
        _write(case / "src" / "app.py", "VALUE = 2\n")

        runs_dir = tmp_path / "runs" / "run-1"
        collected = runs_dir / "cases" / "case-001"
        # Canonical trace written by execute.py into the run, not the workspace.
        _write(collected / "stdout.log", json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "canonical trace"}]},
        }) + "\n")
        _write(collected / "stderr.log", "canonical run stderr\n")

        config = EvalConfig(name="internal-log-test")
        config.outputs = [OutputConfig(path="output/epic")]
        collect._collect_per_case(workspace, runs_dir, config)

        # Declared output is collected from its own walk, declaration intact.
        assert (collected / "output" / "epic" / "report.md").read_text() == (
            "# Epic v2\n")
        # Genuine changes are collected, including uncomfortably similar names.
        for rel in ("output/report.md", "output/stdout.log.bak",
                    "project/output/stdout.log", "src/app.py",
                    "batch.yaml-notes.md", "batch.yaml/notes.md",
                    ".fullsend-runs-notes.md", "stdout.log", "stderr.log"):
            assert (collected / "_modified" / rel).is_file(), rel
        assert (collected / "_modified" / "src" / "app.py").read_text() == (
            "VALUE = 2\n")
        # The skill's root streams are evidence and separate from the run trace.
        assert (collected / "_modified" / "stdout.log").read_text() == (
            "skill's own stdout\n")
        assert (collected / "_modified" / "stderr.log").read_text() == (
            "skill's own stderr\n")
        assert "canonical trace" in (collected / "stdout.log").read_text()
        assert (collected / "stderr.log").read_text() == "canonical run stderr\n"
        # Only the harness's exact streams, root files and top-level dirs are out.
        assert not (collected / "_modified" / "output" / "stdout.log").exists()
        assert not (collected / "_modified" / "output" / "stderr.log").exists()
        assert not (collected / "_modified" / "run_result.json").exists()
        assert not (collected / "_modified" / ".fullsend-runs").exists()
        assert sentinel not in (runs_dir / "collection.json").read_text()
        assert not [path for path in collected.rglob("*")
                    if path.is_file() and sentinel in path.read_text(
                        errors="replace")]

        # The canonical trace is untouched and still becomes events.json.
        assert (collected / "stdout.log").read_text().strip().startswith("{")
        events = json.loads((collected / "events.json").read_text())
        assert events and "canonical trace" in json.dumps(events)
