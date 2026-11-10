"""Tests for agent_eval._bootstrap venv activation.

Regression coverage for the duplicate-run bug: _bootstrap must NEVER os.execv
when it is reached via `python -m`, `python -c`, `python -`, the REPL, or
importlib exec_module — only for a genuine top-level script entry under an
ABI-mismatched interpreter. The default action is sys.path patching, which is
re-entrant and side-effect-free.
"""

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_eval import _bootstrap

_RUNNING = f"{sys.version_info.major}.{sys.version_info.minor}"


@pytest.fixture
def clean_sentinel(monkeypatch):
    monkeypatch.delenv(_bootstrap._SENTINEL, raising=False)
    yield


def _set_main_spec(monkeypatch, spec):
    main = sys.modules["__main__"]
    monkeypatch.setattr(main, "__spec__", spec, raising=False)


# ---------------------------------------------------------------------------
# _venv_pyver — guards the '.../lib/pythonX.Y/site-packages' parse
# ---------------------------------------------------------------------------

def test_venv_pyver_extracts_version():
    site = "/x/.eval-venv/lib/python3.11/site-packages"
    assert _bootstrap._venv_pyver([site]) == "3.11"


def test_venv_pyver_none_on_nonstandard_layout():
    assert _bootstrap._venv_pyver(["/x/.eval-venv/weird/site-packages"]) is None


# ---------------------------------------------------------------------------
# _is_true_script_entry — the execv discriminator
# ---------------------------------------------------------------------------

def test_script_entry_true_for_plain_script(monkeypatch):
    _set_main_spec(monkeypatch, None)
    monkeypatch.setattr(sys, "argv", ["/some/script.py", "--flag"])
    assert _bootstrap._is_true_script_entry() is True


def test_script_entry_false_for_dash_c(monkeypatch):
    _set_main_spec(monkeypatch, None)
    monkeypatch.setattr(sys, "argv", ["-c"])
    assert _bootstrap._is_true_script_entry() is False


def test_script_entry_false_for_stdin_repl_empty(monkeypatch):
    # `python -` (stdin), REPL/embedded (argv[0] == ''), and an empty argv have
    # no real script file to re-exec, so they must not take the execv path.
    _set_main_spec(monkeypatch, None)
    for argv in (["-"], [""], []):
        monkeypatch.setattr(sys, "argv", argv)
        assert _bootstrap._is_true_script_entry() is False, argv


def test_script_entry_false_for_dash_m(monkeypatch):
    # `python -m pkg.mod` leaves a non-None ModuleSpec on __main__.
    import importlib.machinery
    _set_main_spec(monkeypatch, importlib.machinery.ModuleSpec("pkg.mod", None))
    monkeypatch.setattr(sys, "argv", ["/abs/path/mod.py"])
    assert _bootstrap._is_true_script_entry() is False


# ---------------------------------------------------------------------------
# _activate — never execv on -m / -c; execv only on script + ABI mismatch
# ---------------------------------------------------------------------------

def _arm(monkeypatch, venv_ver):
    """Make _activate see a venv whose site-packages report python<venv_ver>."""
    site = f"/fake/.eval-venv/lib/python{venv_ver}/site-packages"
    monkeypatch.setattr(_bootstrap, "_venv_python_and_site",
                        lambda root: ("/fake/.eval-venv/bin/python3", [site]))
    execv_calls, patch_calls = [], []
    monkeypatch.setattr(os, "execv", lambda *a: execv_calls.append(a))
    monkeypatch.setattr(_bootstrap, "_patch_syspath", lambda s: patch_calls.append(s))
    return execv_calls, patch_calls


def test_activate_abi_match_patches_no_execv(clean_sentinel, monkeypatch):
    execv_calls, patch_calls = _arm(monkeypatch, _RUNNING)
    _set_main_spec(monkeypatch, None)
    monkeypatch.setattr(sys, "argv", ["/some/script.py"])
    _bootstrap._activate()
    assert execv_calls == []
    assert patch_calls and os.environ.get(_bootstrap._SENTINEL) == "1"


def test_activate_abi_mismatch_script_execs(clean_sentinel, monkeypatch):
    execv_calls, patch_calls = _arm(monkeypatch, "2.0")  # != running
    _set_main_spec(monkeypatch, None)
    monkeypatch.setattr(sys, "argv", ["/some/script.py"])
    _bootstrap._activate()
    assert len(execv_calls) == 1
    assert execv_calls[0][0] == "/fake/.eval-venv/bin/python3"


def test_activate_abi_mismatch_dash_m_does_not_exec(clean_sentinel, monkeypatch):
    import importlib.machinery
    execv_calls, patch_calls = _arm(monkeypatch, "2.0")  # mismatch, but -m
    _set_main_spec(monkeypatch, importlib.machinery.ModuleSpec("agent_eval.harbor.run", None))
    monkeypatch.setattr(sys, "argv", ["/abs/run.py", "--config", "x"])
    _bootstrap._activate()
    assert execv_calls == []           # the duplicate-run bug must not reappear
    assert patch_calls                  # falls back to sys.path patch


def test_activate_sentinel_short_circuits(clean_sentinel, monkeypatch):
    monkeypatch.setenv(_bootstrap._SENTINEL, "1")
    called = []
    monkeypatch.setattr(_bootstrap, "_venv_python_and_site",
                        lambda root: called.append(1) or (None, []))
    _bootstrap._activate()
    assert called == []  # returned before touching anything


def test_activate_no_venv_is_noop(clean_sentinel, monkeypatch):
    monkeypatch.setattr(_bootstrap, "_venv_python_and_site", lambda root: (None, []))
    execv_calls = []
    monkeypatch.setattr(os, "execv", lambda *a: execv_calls.append(a))
    _bootstrap._activate()
    assert execv_calls == []
    assert os.environ.get(_bootstrap._SENTINEL) == "1"


# ---------------------------------------------------------------------------
# Integration: a fresh process with a *fake ABI-mismatched* .eval-venv, so the
# mismatch branch is exercised regardless of the test runner's interpreter
# (no skip, never vacuous). The `-m` case faithfully reproduces how the harbor
# runner is launched; the true-script case is a positive control proving the
# mismatch branch is actually reachable in this harness.
# ---------------------------------------------------------------------------

# Spy installed before any agent_eval import: records an execv attempt to a
# marker file instead of replacing the process.
_EXECV_SPY = textwrap.dedent("""
    import os
    def _spy(*a, **k):
        with open(os.environ["EXECV_MARKER"], "w") as f:
            f.write("EXECV")
        raise SystemExit("execv-attempted")
    os.execv = _spy
""")


def _make_fake_plugin(tmp_path, venv_pyver="0.0"):
    """Throwaway plugin root: a copy of _bootstrap plus a fake .eval-venv whose
    python<venv_pyver> never matches the runner -> always the ABI-mismatch path."""
    root = tmp_path / "plugin"
    pkg = root / "agent_eval"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")  # no _bootstrap import (mirrors real __init__)
    shutil.copy(_bootstrap.__file__, pkg / "_bootstrap.py")
    venv = root / ".eval-venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python3").write_text("")  # only needs to be a regular file
    (venv / "lib" / f"python{venv_pyver}" / "site-packages").mkdir(parents=True)
    return root


def _run_child(root, code, module=None):
    marker = root / "execv.marker"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root)          # isolate: only the fake agent_eval
    env["EXECV_MARKER"] = str(marker)
    env.pop("_AGENT_EVAL_BOOTSTRAP_DONE", None)
    if module:
        (root / f"{module}.py").write_text(code)
        cmd = [sys.executable, "-m", module]
    else:
        script = root / "child_script.py"
        script.write_text(code)
        cmd = [sys.executable, str(script)]
    proc = subprocess.run(cmd, cwd=str(root), env=env,
                          capture_output=True, text=True, timeout=120)
    return proc, marker.exists()


def test_dash_m_under_abi_mismatch_does_not_exec(tmp_path):
    # `python -m childmod` -> __main__.__spec__ non-None (like the harbor runner).
    # The deferred exec_module import of _bootstrap must NOT execv even though the
    # fake venv is ABI-mismatched.
    root = _make_fake_plugin(tmp_path)
    (root / "loaded.py").write_text("import agent_eval._bootstrap\nVALUE = 42\n")
    code = _EXECV_SPY + textwrap.dedent("""
        import sys, os, importlib.util
        import agent_eval
        assert "agent_eval._bootstrap" not in sys.modules, "bootstrap leaked early"
        p = os.path.join(os.path.dirname(__file__), "loaded.py")
        spec = importlib.util.spec_from_file_location("loaded_mod", p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        print("OK", m.VALUE)
    """)
    proc, execd = _run_child(root, code, module="childmod")
    assert not execd, f"execv fired on the -m path:\n{proc.stdout}\n{proc.stderr}"
    assert proc.returncode == 0 and "OK 42" in proc.stdout, \
        f"child failed:\n{proc.stdout}\n{proc.stderr}"


def test_true_script_under_abi_mismatch_execs(tmp_path):
    # Positive control: a real top-level script under the SAME fake ABI-mismatch
    # venv DOES take the execv path — so the -m test above is not passing vacuously.
    root = _make_fake_plugin(tmp_path)
    code = _EXECV_SPY + "import agent_eval._bootstrap\nprint('NO EXEC')\n"
    proc, execd = _run_child(root, code)  # plain `python child_script.py`
    assert execd, f"expected execv on true-script ABI mismatch:\n{proc.stdout}\n{proc.stderr}"


def test_bootstrap_symlink_realpath(tmp_path):
    # Setup designed layout where agent_eval is imported via a symlink
    root = _make_fake_plugin(tmp_path, _RUNNING)
    skills_dir = root / "skills" / "eval-analyze" / "scripts"
    skills_dir.mkdir(parents=True)
    symlink_path = skills_dir / "agent_eval"
    os.symlink(root / "agent_eval", symlink_path, target_is_directory=True)
    
    site = str(root / ".eval-venv" / "lib" / f"python{_RUNNING}" / "site-packages")
    code = textwrap.dedent(f"""
        import sys
        import os
        # Insert scripts dir at front so we import the symlink, not the real package
        sys.path.insert(0, os.path.dirname(__file__))
        import agent_eval._bootstrap as b

        # Loaded through the symlink, not the real package dir.
        assert os.path.join("skills", "eval-analyze", "scripts") in b.__file__, b.__file__

        # Only realpath resolution reaches <root>/.eval-venv from the symlinked
        # package dir, so its site-packages landing on sys.path is the whole
        # discriminator. Compare the EXACT path: a substring test for
        # '.eval-venv' also matches the *runner's* own venv when the suite is run
        # with .eval-venv/bin/python3, and would pass under abspath too.
        want = {site!r}
        print("VENV_ON_PATH=" + str(any(
            os.path.realpath(p) == os.path.realpath(want) for p in sys.path)))
    """)
    script = skills_dir / "test_symlink.py"
    script.write_text(code)
    
    env = dict(os.environ)
    env.pop("_AGENT_EVAL_BOOTSTRAP_DONE", None)
    env.pop("PYTHONPATH", None)  # Ensure it doesn't bypass the symlink
    
    proc = subprocess.run([sys.executable, str(script)], cwd=str(skills_dir), env=env,
                          capture_output=True, text=True, timeout=120)
    
    assert proc.returncode == 0, f"child failed:\n{proc.stdout}\n{proc.stderr}"
    assert "VENV_ON_PATH=True" in proc.stdout, (
        f"venv site-packages not on the child's sys.path — plugin_root was resolved "
        f"through the symlink instead of to the real tree\nstdout: {proc.stdout}")


# ---------------------------------------------------------------------------
# AGENT_EVAL_HARNESS_ROOT: a staged COPY of the package has no venv beside it
# (the runner's plugin staging materializes scripts/agent_eval; a rollout's
# restored plumbing is a copy too). The launching process names the real
# checkout; _activate falls back to that venv — and only falls back.
# ---------------------------------------------------------------------------

def test_activate_falls_back_to_the_named_harness_root(clean_sentinel, monkeypatch):
    seen = []

    def lookup(root):
        seen.append(root)
        return (("/named/.eval-venv/bin/python3", [f"/named/.eval-venv/lib/python{_RUNNING}/site-packages"])
                if root == "/named" else (None, []))

    monkeypatch.setattr(_bootstrap, "_venv_python_and_site", lookup)
    patched = []
    monkeypatch.setattr(_bootstrap, "_patch_syspath", lambda dirs: patched.append(dirs))
    monkeypatch.setenv(_bootstrap.HARNESS_ROOT_ENV, "/named")
    _bootstrap._activate()
    assert seen[1:] == ["/named"]                      # the realpath root first, then the named one
    assert patched == [[f"/named/.eval-venv/lib/python{_RUNNING}/site-packages"]]


def test_a_venv_beside_the_real_package_wins_over_the_named_root(clean_sentinel, monkeypatch):
    """Fallback, never precedence: nothing that resolved before the variable
    existed may resolve differently once it is set."""
    seen = []
    monkeypatch.setattr(_bootstrap, "_venv_python_and_site",
                        lambda root: (seen.append(root), ("/real/.eval-venv/bin/python3",
                                      [f"/real/.eval-venv/lib/python{_RUNNING}/site-packages"]))[1])
    patched = []
    monkeypatch.setattr(_bootstrap, "_patch_syspath", lambda dirs: patched.append(dirs))
    monkeypatch.setenv(_bootstrap.HARNESS_ROOT_ENV, "/named")
    _bootstrap._activate()
    assert len(seen) == 1 and "/named" not in seen
    assert patched == [[f"/real/.eval-venv/lib/python{_RUNNING}/site-packages"]]


def test_harness_root_is_the_package_parent():
    assert _bootstrap.harness_root() == os.path.dirname(os.path.dirname(os.path.realpath(_bootstrap.__file__)))


def test_staged_copy_activates_the_named_harness_venv(tmp_path):
    """End to end in a child process: agent_eval is a COPY under a skill's
    scripts/ (no venv anywhere near it). Without the variable the venv is not
    found — the staged-copy failure; with it, the real checkout's venv lands
    on sys.path exactly as it does for the real package."""
    root = _make_fake_plugin(tmp_path, _RUNNING)
    staged = tmp_path / "workspace" / "skills" / "demo" / "scripts"
    staged.mkdir(parents=True)
    shutil.copytree(root / "agent_eval", staged / "agent_eval")   # the copy, as staging produces it
    site = str(root / ".eval-venv" / "lib" / f"python{_RUNNING}" / "site-packages")
    script = staged / "run.py"
    script.write_text(textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, os.path.dirname(__file__))
        import agent_eval._bootstrap as b
        assert os.path.join("workspace", "skills") in b.__file__, b.__file__   # the copy, not the real package
        want = {site!r}
        print("VENV_ON_PATH=" + str(any(os.path.realpath(p) == os.path.realpath(want) for p in sys.path)))
    """))

    def run(named):
        env = dict(os.environ)
        env.pop("_AGENT_EVAL_BOOTSTRAP_DONE", None)
        env.pop("PYTHONPATH", None)
        env.pop(_bootstrap.HARNESS_ROOT_ENV, None)
        if named:
            env[_bootstrap.HARNESS_ROOT_ENV] = str(root)
        proc = subprocess.run([sys.executable, str(script)], cwd=str(staged), env=env,
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, f"child failed:\n{proc.stdout}\n{proc.stderr}"
        return "VENV_ON_PATH=True" in proc.stdout

    assert run(named=False) is False
    assert run(named=True) is True


def test_ensure_harness_root_treats_blank_as_unset():
    """`export AGENT_EVAL_HARNESS_ROOT=` from a ${VAR:-} template must not switch
    the fallback off: _activate ignores a blank value, so the producer must too."""
    assert _bootstrap.ensure_harness_root({})[_bootstrap.HARNESS_ROOT_ENV] == _bootstrap.harness_root()
    assert _bootstrap.ensure_harness_root({_bootstrap.HARNESS_ROOT_ENV: ""})[_bootstrap.HARNESS_ROOT_ENV] == _bootstrap.harness_root()
    assert _bootstrap.ensure_harness_root({_bootstrap.HARNESS_ROOT_ENV: "/x"})[_bootstrap.HARNESS_ROOT_ENV] == "/x"


def test_staged_copy_under_abi_mismatch_execs_into_the_named_venv(tmp_path):
    """The developer-box case the feature exists for: system python running a
    staged-copy script whose only venv is the named checkout's, built for a
    different interpreter. The re-exec must target THAT venv's python."""
    root = _make_fake_plugin(tmp_path)                       # python0.0: always a mismatch
    staged = tmp_path / "workspace" / "skills" / "demo" / "scripts"
    staged.mkdir(parents=True)
    shutil.copytree(root / "agent_eval", staged / "agent_eval")
    spy = textwrap.dedent("""
        import os
        def _spy(*a, **k):
            with open(os.environ["EXECV_MARKER"], "w") as f:
                f.write(a[0])
            raise SystemExit("execv-attempted")
        os.execv = _spy
    """)
    script = staged / "run.py"
    script.write_text(spy + "import os, sys\nsys.path.insert(0, os.path.dirname(__file__))\n"
                      "import agent_eval._bootstrap\nprint('NO EXEC')\n")
    marker = tmp_path / "execv.marker"
    env = dict(os.environ)
    env.pop("_AGENT_EVAL_BOOTSTRAP_DONE", None)
    env.pop("PYTHONPATH", None)
    env["EXECV_MARKER"] = str(marker)
    env[_bootstrap.HARNESS_ROOT_ENV] = str(root)
    proc = subprocess.run([sys.executable, str(script)], cwd=str(staged), env=env,
                          capture_output=True, text=True, timeout=120)
    assert marker.exists(), f"no execv from the staged copy:\n{proc.stdout}\n{proc.stderr}"
    assert marker.read_text() == str(root / ".eval-venv" / "bin" / "python3")


def test_staged_copy_activates_under_the_environment_a_runner_builds(tmp_path, monkeypatch):
    """Not a hand-made env: the one CliRunner._build_env produces, with the
    parent's bootstrap sentinel set as it always is in a harness process. The
    runner must strip the sentinel and name the checkout, or the staged copy's
    _activate returns before it ever reads the fallback."""
    from agent_eval.agent.cli_runner import CliRunner
    root = _make_fake_plugin(tmp_path, _RUNNING)
    staged = tmp_path / "workspace" / "skills" / "demo" / "scripts"
    staged.mkdir(parents=True)
    shutil.copytree(root / "agent_eval", staged / "agent_eval")
    site = str(root / ".eval-venv" / "lib" / f"python{_RUNNING}" / "site-packages")
    script = staged / "run.py"
    script.write_text(textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, os.path.dirname(__file__))
        import agent_eval._bootstrap
        want = {site!r}
        print("VENV_ON_PATH=" + str(any(os.path.realpath(p) == os.path.realpath(want) for p in sys.path)))
    """))
    monkeypatch.setenv("_AGENT_EVAL_BOOTSTRAP_DONE", "1")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    # The runner names ITS checkout (the real harness); for this test the
    # "checkout" is the fake plugin root, passed as an explicit value.
    monkeypatch.setenv(_bootstrap.HARNESS_ROOT_ENV, str(root))
    env = CliRunner("echo hi")._build_env()
    assert "_AGENT_EVAL_BOOTSTRAP_DONE" not in env and env[_bootstrap.HARNESS_ROOT_ENV] == str(root)
    proc = subprocess.run([sys.executable, str(script)], cwd=str(staged), env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"child failed:\n{proc.stdout}\n{proc.stderr}"
    assert "VENV_ON_PATH=True" in proc.stdout, proc.stdout
