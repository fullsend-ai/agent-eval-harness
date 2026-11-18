"""Tests for the Stop guard hook (scripts/eval_stop_guard.py).

The hook is stdlib-only and detects a live eval executor from the
``execute.pid`` file that execute.py writes, so these tests use a real
subprocess as the "live executor" rather than mocking process liveness.
"""
import ast
import importlib.util
import io
import os
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOK_PATH = REPO / "scripts" / "eval_stop_guard.py"
EXECUTE_PATH = REPO / "skills" / "eval-run" / "scripts" / "execute.py"


def _load_hook():
    spec = importlib.util.spec_from_file_location("eval_stop_guard", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


guard = _load_hook()


@pytest.fixture
def live_proc():
    """A real, still-running child process to stand in for a live executor."""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        yield p
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


def _make_run(runs_dir, name, pid, create_time, with_result=False):
    d = runs_dir / name
    d.mkdir(parents=True)
    (d / "execute.pid").write_text(
        json.dumps({"pid": pid, "create_time": create_time})
    )
    if with_result:
        (d / "run_result.json").write_text("{}")
    return d


def test_live_executor_detected(tmp_path, monkeypatch, live_proc):
    runs = tmp_path / "eval" / "runs"
    ct = guard._process_create_time(live_proc.pid)
    run_dir = _make_run(runs, "run-1", live_proc.pid, ct)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(runs))

    live = guard._live_eval_runs(str(tmp_path))
    assert str(run_dir) in live


def test_finished_run_not_live(tmp_path, monkeypatch, live_proc):
    # Process is alive, but run_result.json exists -> already done.
    runs = tmp_path / "eval" / "runs"
    ct = guard._process_create_time(live_proc.pid)
    _make_run(runs, "run-1", live_proc.pid, ct, with_result=True)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(runs))

    assert guard._live_eval_runs(str(tmp_path)) == []


def test_dead_pid_not_live(tmp_path, monkeypatch):
    # Spawn then reap a child so its pid is reliably gone.
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    runs = tmp_path / "eval" / "runs"
    _make_run(runs, "run-1", p.pid, None)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(runs))

    assert guard._live_eval_runs(str(tmp_path)) == []


def test_pid_reuse_guarded(tmp_path, monkeypatch, live_proc):
    # pid is alive, but the recorded create_time doesn't match -> treat as a
    # reused pid belonging to an unrelated process, not this run.
    runs = tmp_path / "eval" / "runs"
    _make_run(runs, "run-1", live_proc.pid, "definitely-not-the-real-marker")
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(runs))

    # Only assert the guard fires where create_time is actually resolvable.
    if guard._process_create_time(live_proc.pid) is not None:
        assert guard._live_eval_runs(str(tmp_path)) == []


def test_default_runs_dir(tmp_path, monkeypatch, live_proc):
    # No AGENT_EVAL_RUNS_DIR -> falls back to <cwd>/eval/runs.
    monkeypatch.delenv("AGENT_EVAL_RUNS_DIR", raising=False)
    runs = tmp_path / "eval" / "runs"
    ct = guard._process_create_time(live_proc.pid)
    run_dir = _make_run(runs, "run-1", live_proc.pid, ct)

    assert str(run_dir) in guard._live_eval_runs(str(tmp_path))


def test_main_blocks_while_live(tmp_path, monkeypatch, capsys, live_proc):
    monkeypatch.setattr(guard.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    runs = tmp_path / "eval" / "runs"
    ct = guard._process_create_time(live_proc.pid)
    _make_run(runs, "run-1", live_proc.pid, ct)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(runs))
    monkeypatch.setattr(
        sys, "stdin",
        io.StringIO(json.dumps({"session_id": "s1", "cwd": str(tmp_path)})),
    )

    rc = guard.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out)["decision"] == "block"


def test_main_allows_when_no_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(guard.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(tmp_path / "eval" / "runs"))
    monkeypatch.setattr(
        sys, "stdin",
        io.StringIO(json.dumps({"session_id": "s1", "cwd": str(tmp_path)})),
    )

    rc = guard.main()
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_main_disabled_env(tmp_path, monkeypatch, capsys, live_proc):
    monkeypatch.setenv("AGENT_EVAL_STOP_GUARD", "0")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    runs = tmp_path / "eval" / "runs"
    ct = guard._process_create_time(live_proc.pid)
    _make_run(runs, "run-1", live_proc.pid, ct)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(runs))
    monkeypatch.setattr(
        sys, "stdin",
        io.StringIO(json.dumps({"session_id": "s1", "cwd": str(tmp_path)})),
    )

    rc = guard.main()
    assert rc == 0
    assert capsys.readouterr().out == ""  # allowed despite a live run


def test_main_escape_hatch(tmp_path, monkeypatch, capsys, live_proc):
    monkeypatch.setattr(guard.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("AGENT_EVAL_STOP_GUARD_MAX_MIN", "60")
    runs = tmp_path / "eval" / "runs"
    ct = guard._process_create_time(live_proc.pid)
    _make_run(runs, "run-1", live_proc.pid, ct)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(runs))

    # Pre-seed the first-block timestamp well past the ceiling, against the
    # same run set the hook will see (a differing set resets the timer).
    state_path = guard._state_path("s1", str(tmp_path))
    assert state_path is not None
    runs_key = sorted(guard._live_eval_runs(str(tmp_path)))
    assert runs_key, "fixture must present a live run"
    Path(state_path).write_text(
        json.dumps({"first_block": time.time() - 7200, "runs": runs_key})
    )

    monkeypatch.setattr(
        sys, "stdin",
        io.StringIO(json.dumps({"session_id": "s1", "cwd": str(tmp_path)})),
    )
    rc = guard.main()
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == ""  # stop allowed
    assert "escape hatch" in captured.err


def test_stale_pidfile_ignored(tmp_path, monkeypatch, live_proc):
    """A pidfile older than the age bound is abandoned even if the pid lives.

    Backstops the platforms where no create_time marker is obtainable and the
    pid-reuse check is therefore inert.
    """
    runs = tmp_path / "eval" / "runs"
    ct = guard._process_create_time(live_proc.pid)
    run_dir = _make_run(runs, "run-1", live_proc.pid, ct)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(runs))
    assert guard._live_eval_runs(str(tmp_path))  # fresh -> live

    old = time.time() - (guard.MAX_PIDFILE_AGE_S + 3600)
    os.utime(run_dir / "execute.pid", (old, old))
    assert guard._live_eval_runs(str(tmp_path)) == []


def test_pidfile_found_at_nested_depths(tmp_path, monkeypatch, live_proc):
    """Bounded-depth globs must still cover the real layout
    (<runs>/<eval-name>/<run-id>/execute.pid) and shallower variants."""
    runs = tmp_path / "eval" / "runs"
    ct = guard._process_create_time(live_proc.pid)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(runs))
    expected = set()
    for name in ("run-1", "my-eval/run-2", "a/b/run-3"):
        expected.add(str(_make_run(runs, name, live_proc.pid, ct)))

    assert set(guard._live_eval_runs(str(tmp_path))) == expected


def test_no_recursive_glob_walk(tmp_path, monkeypatch, live_proc):
    """The scan must not walk deep artifact trees under a run dir."""
    runs = tmp_path / "eval" / "runs"
    ct = guard._process_create_time(live_proc.pid)
    run_dir = _make_run(runs, "my-eval/run-1", live_proc.pid, ct)
    # A decoy far below the bounded depth: a recursive walk would surface it.
    deep = run_dir / "cases" / "case-1" / "subagents" / "x" / "y"
    deep.mkdir(parents=True)
    (deep / "execute.pid").write_text(json.dumps({"pid": live_proc.pid,
                                                  "create_time": ct}))
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(runs))

    assert guard._live_eval_runs(str(tmp_path)) == [str(run_dir)]


def test_escape_hatch_timer_resets_on_new_run(tmp_path, monkeypatch):
    """A chained eval must not inherit the previous run's first_block."""
    state = tmp_path / "state.json"
    old = time.time() - 7200
    assert guard._load_timer(str(state), old, ["/runs/run-1"]) == (old, False)
    # Same run set -> timer preserved.
    assert guard._load_timer(str(state), time.time(), ["/runs/run-1"]) == (old, False)
    # Different run set -> timer restarts.
    now = time.time()
    assert guard._load_timer(str(state), now, ["/runs/run-2"]) == (now, False)


def test_load_timer_fails_open_when_unpersistable(tmp_path):
    """No session identity / unwritable state dir must yield the sentinel, not a
    fresh `now` — every later Stop would otherwise see elapsed ~= 0 and block
    forever."""
    assert guard._load_timer(None, time.time(), []) is guard._PERSIST_FAILED
    unwritable = tmp_path / "nonexistent-dir" / "state.json"
    assert guard._load_timer(str(unwritable), time.time(), []) is guard._PERSIST_FAILED


@pytest.mark.parametrize("bad_pid", [0, -1, -12345, True, False, "123", 1.5, None])
def test_invalid_pids_rejected(tmp_path, monkeypatch, bad_pid):
    """os.kill() overloads non-positive pids into broadcasts that always
    'succeed', and bool is an int subclass — either would wedge the session."""
    runs = tmp_path / "eval" / "runs"
    _make_run(runs, "run-1", bad_pid, None)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(runs))

    assert guard._valid_pid(bad_pid) is False
    assert guard._live_eval_runs(str(tmp_path)) == []


def test_kill_broadcast_pids_would_report_alive():
    """Pins *why* _valid_pid exists: these pids pass a naive liveness probe."""
    assert guard._pid_alive(0) is False       # 0 = caller's process group
    assert guard._pid_alive(-1) is False      # -1 = every permitted process
    assert guard._pid_alive(True) is False    # bool -> pid 1 (init), always up


def test_windows_guard_disabled(monkeypatch):
    """os.kill(pid, 0) on Windows calls TerminateProcess — it would kill the
    executor it is probing, causing the very failure this hook prevents."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("AGENT_EVAL_STOP_GUARD", raising=False)
    assert guard._guard_enabled() is False
    # Not even an explicit opt-in may enable it there.
    monkeypatch.setenv("AGENT_EVAL_STOP_GUARD", "1")
    assert guard._guard_enabled() is False


def test_glob_metachars_in_path(tmp_path, monkeypatch, live_proc):
    """A runs root containing glob metacharacters (CI paths like
    'linux[py311]') must not silently match nothing."""
    runs = tmp_path / "linux[py311]" / "eval" / "runs"
    ct = guard._process_create_time(live_proc.pid)
    run_dir = _make_run(runs, "my-eval/run-1", live_proc.pid, ct)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(runs))

    assert guard._live_eval_runs(str(tmp_path)) == [str(run_dir)]


def test_stale_result_in_reused_dir_still_live(tmp_path, monkeypatch, live_proc):
    """A run_result.json left by a previous attempt in a reused run dir must
    not mark the new executor finished."""
    runs = tmp_path / "eval" / "runs"
    ct = guard._process_create_time(live_proc.pid)
    run_dir = _make_run(runs, "run-1", live_proc.pid, ct)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(runs))

    result = run_dir / "run_result.json"
    result.write_text("{}")
    old = time.time() - 3600           # result predates the pidfile
    os.utime(result, (old, old))
    assert guard._live_eval_runs(str(tmp_path)) == [str(run_dir)]

    # A result written *after* the pidfile is this run's own -> finished.
    now = time.time() + 10
    os.utime(result, (now, now))
    assert guard._live_eval_runs(str(tmp_path)) == []


def test_escape_hatch_does_not_rearm(tmp_path, monkeypatch, capsys, live_proc):
    """Once the ceiling is hit, later Stops must allow immediately rather than
    opening a fresh full-length window on the same wedged run."""
    monkeypatch.setattr(guard.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("AGENT_EVAL_STOP_GUARD_MAX_MIN", "60")
    runs = tmp_path / "eval" / "runs"
    ct = guard._process_create_time(live_proc.pid)
    _make_run(runs, "run-1", live_proc.pid, ct)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(runs))

    state_path = guard._state_path("s1", str(tmp_path))
    runs_key = sorted(guard._live_eval_runs(str(tmp_path)))
    guard._write_timer(state_path, {"first_block": time.time() - 7200,
                                    "runs": runs_key})

    def _stop():
        monkeypatch.setattr(sys, "stdin", io.StringIO(
            json.dumps({"session_id": "s1", "cwd": str(tmp_path)})))
        rc = guard.main()
        return rc, capsys.readouterr()

    rc, first = _stop()
    assert rc == 0 and first.out == "" and "escape hatch" in first.err

    # The wedged run is still live; the next Stop must NOT block again.
    rc, second = _stop()
    assert rc == 0
    assert second.out == "", "guard re-armed a fresh window after escaping"
    assert "already tripped" in second.err


def test_pidfile_write_precedes_heavy_imports():
    """The early write must stay ahead of the agent_eval imports.

    The window this closes is the race the guard exists for: the orchestrator
    can end its turn immediately after launching execute.py, and the hook is
    blind until execute.pid lands. Module import dominates startup, so letting
    the write drift below those imports would silently reopen the hole.
    """
    tree = ast.parse(EXECUTE_PATH.read_text())
    early_call = min(
        (n.lineno for n in ast.walk(tree)
         if isinstance(n, ast.Call)
         and isinstance(n.func, ast.Name)
         and n.func.id == "_early_pidfile"),
        default=None,
    )
    heavy_import = min(
        (n.lineno for n in ast.walk(tree)
         if isinstance(n, ast.ImportFrom)
         and (n.module or "").startswith("agent_eval.")
         and n.module != "agent_eval._bootstrap"),
        default=None,
    )
    assert early_call is not None, "_early_pidfile() call disappeared"
    assert heavy_import is not None
    assert early_call < heavy_import, (
        f"_early_pidfile() at line {early_call} runs after the agent_eval "
        f"import at line {heavy_import}; the Stop guard is blind for the "
        f"duration of those imports."
    )


@pytest.mark.parametrize("argv,expected", [
    (["--output", "RUNDIR", "--config", "x.yaml"], True),
    (["--config", "x.yaml", "--output=RUNDIR"], True),
    (["--run-dir", "RUNDIR", "--config", "x.yaml"], True),
    (["--config", "x.yaml", "--run-dir=RUNDIR"], True),
    (["--config", "x.yaml"], False),          # no --output -> no write
    (["--output"], False),                    # dangling flag -> no crash
])
def test_early_pidfile_argv_scrape(tmp_path, monkeypatch, argv, expected):
    """--output is scraped by hand, so cover both spellings and the misses."""
    import execute

    monkeypatch.setattr(execute, "_PIDFILE_WRITTEN", False)
    target = tmp_path / "rundir"
    argv = [a.replace("RUNDIR", str(target)) for a in argv]

    execute._early_pidfile(argv)
    assert (target / "execute.pid").exists() is expected
    if expected:
        assert json.loads((target / "execute.pid").read_text())["pid"] == os.getpid()


def _func_ast(path, name):
    """AST dump of a function body minus its docstring (format-insensitive)."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = list(node.body)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(getattr(body[0], "value", None), ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
            return "\n".join(ast.dump(n) for n in body)
    return None


def test_process_create_time_matches_execute_py():
    """The hook and execute.py must derive the create-time marker identically,
    or the pid-reuse guard silently rejects every live run."""
    hook_code = _func_ast(HOOK_PATH, "_process_create_time")
    exec_code = _func_ast(EXECUTE_PATH, "_process_create_time")
    assert hook_code is not None and exec_code is not None
    assert hook_code == exec_code
