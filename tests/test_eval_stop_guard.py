"""Tests for the Stop guard hook (scripts/eval_stop_guard.py).

The hook is stdlib-only and detects a live eval executor from the
``execute.<pid>.pid`` file that execute.py writes (or the legacy shared
``execute.pid``, which ``_make_run`` writes), so these tests use a real
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
    monkeypatch.setattr(execute, "_PIDFILE_PATH", None)
    target = tmp_path / "rundir"
    argv = [a.replace("RUNDIR", str(target)) for a in argv]

    execute._early_pidfile(argv)
    pidfile = target / f"execute.{os.getpid()}.pid"
    assert pidfile.exists() is expected
    if expected:
        assert json.loads(pidfile.read_text())["pid"] == os.getpid()


# ── Overlapping executors in one reused run dir (#337) ─────────────────────
# Current executors lock the run dir so a second one is refused; the markers
# below stand in for overlapping older (pre-lock) executors the guard still sees.

@pytest.fixture
def live_proc_b():
    """A second live process, standing in for an overlapping executor."""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        yield p
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


def _start_executor(execute, monkeypatch, run_dir, pid):
    """Run execute.py's real pidfile write as the executor with ``pid``.

    Each executor is its own process with its own module globals; this resets
    them and fakes ``os.getpid`` so one test process can play several
    executors. Returns the pidfile path, as the stand-in for that executor's
    ``_PIDFILE_PATH``.
    """
    monkeypatch.setattr(execute, "_PIDFILE_WRITTEN", False)
    monkeypatch.setattr(execute, "_PIDFILE_PATH", None)
    monkeypatch.setattr(execute.os, "getpid", lambda: pid)
    execute._write_pidfile(run_dir)
    path = execute._PIDFILE_PATH
    assert path is not None and path.exists()
    return path


def _entries(run_dir):
    """Run-dir contents, minus the run-directory lock file that stays behind."""
    return sorted(p.name for p in run_dir.iterdir() if p.name != ".execute.lock")


def _finish_executor(execute, monkeypatch, run_dir, pidfile):
    """What an executor does on completion: write its run-level result, then
    release its own pidfile."""
    (run_dir / "run_result.json").write_text("{}")
    monkeypatch.setattr(execute, "_PIDFILE_PATH", pidfile)
    execute._release_pidfile()


@pytest.fixture
def running_executor(tmp_path, monkeypatch, live_proc):
    """Executor A, live, owning a run dir."""
    import execute

    runs = tmp_path / "eval" / "runs"
    run_dir = runs / "my-eval" / "run-1"
    run_dir.mkdir(parents=True)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(runs))
    a = _start_executor(execute, monkeypatch, run_dir, live_proc.pid)
    assert guard._live_eval_runs(str(tmp_path)) == [str(run_dir)]
    return execute, run_dir, a


def test_second_executor_rejected_in_active_run_dir(
        tmp_path, monkeypatch, capsys, running_executor, live_proc_b):
    """B targets a run dir A is still running in: B is refused, writes no
    marker, and A stays protected by the Stop guard throughout."""
    execute, run_dir, a = running_executor
    a_before = a.read_text()

    with pytest.raises(SystemExit) as exc:
        _start_executor(execute, monkeypatch, run_dir, live_proc_b.pid)
    assert exc.value.code not in (0, None)
    err = capsys.readouterr().err
    assert str(run_dir) in err and "already in use" in err
    assert not (run_dir / f"execute.{live_proc_b.pid}.pid").exists()
    assert execute._PIDFILE_PATH is None  # nothing for B's atexit to release
    assert _entries(run_dir) == [a.name]
    assert a.read_text() == a_before
    assert guard._live_eval_runs(str(tmp_path)) == [str(run_dir)]

    # B's exit path (release) must not touch A's marker either.
    execute._release_pidfile()
    assert a.exists()
    assert guard._live_eval_runs(str(tmp_path)) == [str(run_dir)]

    # A finishes normally: only then does the guard stand down.
    _finish_executor(execute, monkeypatch, run_dir, a)
    assert guard._live_eval_runs(str(tmp_path)) == []


def test_run_dir_reusable_after_first_executor_finishes(
        tmp_path, monkeypatch, running_executor, live_proc, live_proc_b):
    """Rejection is only for a *live* executor: once A has released its marker
    (result written), a new executor may use the dir."""
    execute, run_dir, a = running_executor

    _finish_executor(execute, monkeypatch, run_dir, a)
    b = _start_executor(execute, monkeypatch, run_dir, live_proc_b.pid)
    assert b.exists()
    assert guard._live_eval_runs(str(tmp_path)) == [str(run_dir)]


def test_killed_executor_does_not_block_new_executor(
        tmp_path, monkeypatch, running_executor, live_proc, live_proc_b):
    """A killed executor leaves its marker behind; that dead pid must neither
    block a retry in the same dir nor keep the run live."""
    execute, run_dir, a = running_executor

    live_proc.kill()
    live_proc.wait(timeout=5)
    # The stand-in executor shares this test process, so drop its lock the way
    # the kernel would on a real SIGKILL (see test_lock_dropped_when_holder_is_killed).
    os.close(execute._RUN_DIR_LOCKS.pop(a))
    assert a.exists()
    assert guard._live_eval_runs(str(tmp_path)) == []

    b = _start_executor(execute, monkeypatch, run_dir, live_proc_b.pid)
    assert guard._live_eval_runs(str(tmp_path)) == [str(run_dir)]
    live_proc_b.kill()
    live_proc_b.wait(timeout=5)
    assert guard._live_eval_runs(str(tmp_path)) == []
    assert b.exists()


def test_reused_pid_marker_does_not_block_new_executor(
        tmp_path, monkeypatch, live_proc, live_proc_b):
    """A stale marker whose pid now belongs to an unrelated process (creation
    marker differs) is not a live executor."""
    import execute

    if guard._process_create_time(live_proc.pid) is None:
        pytest.skip("no process creation marker on this platform")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / f"execute.{live_proc.pid}.pid").write_text(
        json.dumps({"pid": live_proc.pid, "create_time": "bogus"}))

    b = _start_executor(execute, monkeypatch, run_dir, live_proc_b.pid)
    assert b.exists()


def test_new_executor_rejected_by_live_legacy_marker(
        tmp_path, monkeypatch, capsys, live_proc, live_proc_b):
    """A running older executor (legacy shared execute.pid) also owns the dir:
    the new executor is refused and the legacy marker is left untouched."""
    import execute

    runs = tmp_path / "eval" / "runs"
    ct = guard._process_create_time(live_proc.pid)
    run_dir = _make_run(runs, "run-1", live_proc.pid, ct)
    legacy = run_dir / "execute.pid"
    before = legacy.read_text()
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(runs))

    with pytest.raises(SystemExit):
        _start_executor(execute, monkeypatch, run_dir, live_proc_b.pid)
    assert "already in use" in capsys.readouterr().err
    assert legacy.read_text() == before
    assert _entries(run_dir) == ["execute.pid"]
    assert guard._live_eval_runs(str(tmp_path)) == [str(run_dir)]


def test_legacy_marker_with_newer_result_does_not_block(
        tmp_path, monkeypatch, live_proc, live_proc_b):
    """Same finished-run rule as the guard for the legacy marker: a result at
    least as new as it means that executor is done."""
    import execute

    runs = tmp_path / "eval" / "runs"
    ct = guard._process_create_time(live_proc.pid)
    run_dir = _make_run(runs, "run-1", live_proc.pid, ct, with_result=True)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(runs))

    b = _start_executor(execute, monkeypatch, run_dir, live_proc_b.pid)
    assert b.exists()


def test_executor_paused_before_publishing_still_excludes_peer(
        tmp_path, monkeypatch, live_proc):
    """A has taken the run dir but is paused between writing its temp marker and
    publishing it. A real second executor started in that window must be
    refused: ownership comes from A's lock, not from published markers, so A is
    never admitted alongside a B that got in first."""
    import execute

    run_dir = tmp_path / "eval" / "runs" / "my-eval" / "run-1"
    run_dir.mkdir(parents=True)
    monkeypatch.setenv("AGENT_EVAL_RUNS_DIR", str(tmp_path / "eval" / "runs"))
    real_replace = os.replace
    b_result = {}

    def replace_after_b_starts(src, dst):
        assert not (run_dir / f"execute.{live_proc.pid}.pid").exists()
        b_result["proc"] = subprocess.run(
            [sys.executable, str(EXECUTE_PATH), "--output", str(run_dir)],
            capture_output=True, text=True, timeout=60, cwd=tmp_path,
        )
        return real_replace(src, dst)

    monkeypatch.setattr(execute.os, "replace", replace_after_b_starts)
    a = _start_executor(execute, monkeypatch, run_dir, live_proc.pid)

    proc = b_result["proc"]
    assert proc.returncode != 0 and "already in use" in proc.stderr
    assert _entries(run_dir) == [a.name]  # B published nothing
    assert guard._live_eval_runs(str(tmp_path)) == [str(run_dir)]


def test_lock_dropped_when_holder_is_killed(tmp_path, monkeypatch, live_proc_b):
    """The run-dir lock belongs to a live process: a second executor is refused
    while a real executor holds it, and admitted once that process is SIGKILLed
    (the kernel drops the lock; no cleanup code runs)."""
    import execute

    scripts = str(EXECUTE_PATH.parent)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time; sys.path.insert(0, %r); import execute; "
         "from pathlib import Path; execute._write_pidfile(Path(%r)); "
         "print('ready', flush=True); time.sleep(120)" % (scripts, str(run_dir))],
        stdout=subprocess.PIPE, text=True, cwd=tmp_path,
    )
    try:
        assert holder.stdout.readline().strip() == "ready"
        with pytest.raises(SystemExit):
            _start_executor(execute, monkeypatch, run_dir, live_proc_b.pid)
        holder.kill()
        holder.wait(timeout=5)
        b = _start_executor(execute, monkeypatch, run_dir, live_proc_b.pid)
        assert b.exists()
    finally:
        holder.kill()
        holder.wait(timeout=5)
        holder.stdout.close()


def _assert_refused_unowned(execute, monkeypatch, capsys, run_dir, pid):
    """Starting an executor in ``run_dir`` exits non-zero, explains that
    ownership could not be established, and publishes no marker."""
    with pytest.raises(SystemExit) as exc:
        _start_executor(execute, monkeypatch, run_dir, pid)
    assert exc.value.code not in (0, None)
    assert "exclusive ownership" in capsys.readouterr().err
    assert not list(run_dir.glob("execute.*.pid"))
    assert not list(run_dir.glob(".execute.*.tmp"))
    assert execute._PIDFILE_PATH is None


def test_refuses_to_run_when_no_locking_primitive(
        tmp_path, monkeypatch, capsys):
    """No ``fcntl``/``msvcrt``: ownership cannot be established, so the
    executor refuses to run rather than falling back to the racy marker scan."""
    import execute

    monkeypatch.setattr(execute, "fcntl", None)
    monkeypatch.setattr(execute, "msvcrt", None)
    _assert_refused_unowned(execute, monkeypatch, capsys, tmp_path, os.getpid())


def test_refuses_to_run_when_filesystem_refuses_lock(
        tmp_path, monkeypatch, capsys):
    """``flock`` failing with anything but contention (e.g. ENOLCK on a
    filesystem without lock support) fails closed."""
    import errno
    import execute

    def no_locks(fd, op):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(execute.fcntl, "flock", no_locks)
    _assert_refused_unowned(execute, monkeypatch, capsys, tmp_path, os.getpid())


def test_refuses_to_run_when_lock_path_is_a_symlink(
        tmp_path, monkeypatch, capsys):
    """A symlink planted at the lock name is never followed; startup refuses."""
    import execute

    target = tmp_path / "elsewhere"
    target.write_text("keep")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / ".execute.lock").symlink_to(target)
    _assert_refused_unowned(execute, monkeypatch, capsys, run_dir, os.getpid())
    assert target.read_text() == "keep"


def test_refuses_to_run_when_lock_path_is_a_fifo(
        tmp_path, monkeypatch, capsys):
    """A FIFO at the lock name neither hangs startup nor is accepted."""
    import signal
    import execute

    if not hasattr(os, "mkfifo") or not hasattr(signal, "SIGALRM"):
        pytest.skip("needs mkfifo and SIGALRM")
    os.mkfifo(tmp_path / ".execute.lock")
    signal.signal(signal.SIGALRM, lambda *a: pytest.fail("startup hung on FIFO"))
    signal.alarm(10)
    try:
        _assert_refused_unowned(execute, monkeypatch, capsys, tmp_path,
                                os.getpid())
    finally:
        signal.alarm(0)


def test_refuses_to_run_when_lock_file_not_openable(
        tmp_path, monkeypatch, capsys):
    """A lock path that cannot be opened (here, a directory) fails closed."""
    import execute

    (tmp_path / ".execute.lock").mkdir()
    _assert_refused_unowned(execute, monkeypatch, capsys, tmp_path, os.getpid())


def test_simultaneous_start_without_lock_support_admits_nobody_extra(
        tmp_path, monkeypatch, capsys, live_proc, live_proc_b):
    """The race the old fallback allowed: A and B start together with locking
    unavailable. Neither is admitted, so there is never a pair sharing the dir."""
    import execute

    monkeypatch.setattr(execute, "fcntl", None)
    monkeypatch.setattr(execute, "msvcrt", None)
    with pytest.raises(SystemExit):
        _start_executor(execute, monkeypatch, tmp_path, live_proc.pid)
    with pytest.raises(SystemExit):
        _start_executor(execute, monkeypatch, tmp_path, live_proc_b.pid)
    assert not list(tmp_path.glob("execute.*.pid"))


def test_marker_publication_failure_aborts_and_keeps_dir_owned(
        tmp_path, monkeypatch, capsys, live_proc, live_proc_b):
    """If the marker cannot be published (here, a directory sits at the marker
    name), startup aborts with the underlying error instead of returning to
    ``main()`` — which would retry with the lock released in between and, on a
    second failure, run with neither lock nor marker."""
    import execute

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / f"execute.{live_proc.pid}.pid").mkdir()

    with pytest.raises(SystemExit) as exc:
        _start_executor(execute, monkeypatch, run_dir, live_proc.pid)
    assert exc.value.code not in (0, None)
    err = capsys.readouterr().err
    assert "cannot publish" in err and "Refusing to run" in err
    assert execute._PIDFILE_PATH is None
    assert not list(run_dir.glob(".execute.*.tmp"))
    # The aborted executor left no marker and its lock goes with it, so the
    # directory is free for the next (single) executor.
    (run_dir / f"execute.{live_proc.pid}.pid").rmdir()
    b = _start_executor(execute, monkeypatch, run_dir, live_proc_b.pid)
    assert b.exists()


def test_publication_failure_process_exits_nonzero(tmp_path):
    """End to end: a real executor whose marker path is blocked exits non-zero
    instead of continuing without a marker or a lock."""
    run_dir = tmp_path / "eval" / "runs" / "e" / "r"
    run_dir.mkdir(parents=True)
    # Any marker name this process could pick is its own pid, unknown up front;
    # make every temp/marker write fail by removing write permission instead.
    run_dir.chmod(0o500)
    try:
        if os.access(run_dir, os.W_OK):
            pytest.skip("directory permissions not enforced (running as root)")
        proc = subprocess.run(
            [sys.executable, str(EXECUTE_PATH), "--output", str(run_dir)],
            capture_output=True, text=True, timeout=60, cwd=tmp_path,
        )
    finally:
        run_dir.chmod(0o700)
    assert proc.returncode != 0
    assert "Refusing to run" in proc.stderr


def test_second_executor_process_exits_nonzero_before_touching_run_dir(
        tmp_path, live_proc):
    """End to end: a real second ``execute.py`` process aimed at a run dir with
    a live executor exits non-zero with an explanation, and leaves the dir's
    contents exactly as the first executor had them."""
    run_dir = tmp_path / "eval" / "runs" / "my-eval" / "run-1"
    run_dir.mkdir(parents=True)
    ct = guard._process_create_time(live_proc.pid)
    marker = run_dir / f"execute.{live_proc.pid}.pid"
    marker.write_text(json.dumps({"pid": live_proc.pid, "create_time": ct}))
    before = marker.read_text()

    proc = subprocess.run(
        [sys.executable, str(EXECUTE_PATH), "--output", str(run_dir)],
        capture_output=True, text=True, timeout=60, cwd=tmp_path,
    )
    assert proc.returncode != 0
    assert "already in use" in proc.stderr
    assert _entries(run_dir) == [marker.name]
    assert marker.read_text() == before


def test_release_pidfile_idempotent(tmp_path, monkeypatch):
    """Released explicitly, and again by atexit."""
    import execute

    pidfile = _start_executor(execute, monkeypatch, tmp_path, os.getpid())
    execute._release_pidfile()
    assert not pidfile.exists()
    execute._release_pidfile()  # atexit's second call: no error


# ── Review hardening (#338) ────────────────────────────────────────────────

def test_peer_probe_never_signals_on_windows(
        tmp_path, monkeypatch, live_proc, live_proc_b):
    """On win32 ``os.kill(pid, 0)`` is TerminateProcess: the peer scan must not
    call it, so a second executor can never kill the first."""
    import execute

    ct = guard._process_create_time(live_proc.pid)
    (tmp_path / f"execute.{live_proc.pid}.pid").write_text(
        json.dumps({"pid": live_proc.pid, "create_time": ct}))

    def boom(*a, **k):
        raise AssertionError("os.kill must not be called on win32")

    # Scoped so the fixtures' teardown (which signals the children) runs
    # unpatched.
    with monkeypatch.context() as m:
        m.setattr(execute.sys, "platform", "win32")
        m.setattr(execute.os, "kill", boom)
        assert execute._live_peer_executor(tmp_path, live_proc_b.pid) is None
    assert live_proc.poll() is None


def test_marker_held_until_exit_not_result_write(
        tmp_path, monkeypatch, live_proc, live_proc_b):
    """Finalization after ``run_result.json`` is written (reconcile, strict
    rewrite, after_all) still owns the run dir: a second executor is rejected
    until the process exits and atexit releases the marker."""
    import types
    import execute

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    a = _start_executor(execute, monkeypatch, run_dir, live_proc.pid)
    seen = {}

    def finish(run_meta):
        seen["peer"] = execute._live_peer_executor(run_dir, live_proc_b.pid)
        seen["marker"] = a.exists()
        return run_meta

    def write_result(path, meta):
        Path(path).write_text(json.dumps(meta))
        return meta

    monkeypatch.setattr(execute, "write_run_result", write_result)
    monkeypatch.setattr(execute, "_finish_provider_run", finish)
    monkeypatch.setattr(execute, "_finalize_strict",
                        lambda path, meta, code: (meta, code))
    monkeypatch.setattr(execute, "_with_runner_provenance",
                        lambda params, runner, texts: {})
    result = types.SimpleNamespace(
        stdout="", stderr="", exit_code=0, duration_s=1.0, token_usage=None,
        cost_usd=None, per_model_usage=None, num_turns=None,
        per_model_turns=None, permission_denials=None, resolved_model=None,
        models_used=None, message_ids=None, generation_ids=None,
        cost_source=None, error_class=None, budget=None)
    args = types.SimpleNamespace(workspace=str(tmp_path / "ws"))
    runner = types.SimpleNamespace(name="x")
    execute._save_result(result, args, run_dir, runner, "m")

    assert seen["marker"] is True
    assert seen["peer"] is not None and seen["peer"][0] == live_proc.pid
    assert a.exists()


def test_fifo_marker_does_not_hang_startup(tmp_path, monkeypatch):
    """A FIFO planted at a marker name is skipped, not blocked on."""
    import signal
    import execute

    if not hasattr(os, "mkfifo") or not hasattr(signal, "SIGALRM"):
        pytest.skip("needs mkfifo and SIGALRM")
    os.mkfifo(tmp_path / "execute.4242.pid")
    signal.signal(signal.SIGALRM, lambda *a: pytest.fail("startup hung on FIFO"))
    signal.alarm(10)
    try:
        assert execute._live_peer_executor(tmp_path, os.getpid()) is None
    finally:
        signal.alarm(0)


def test_oversized_marker_is_not_read_whole(tmp_path, monkeypatch, live_proc):
    """The read is capped, so a huge planted marker cannot be a live peer."""
    import execute

    (tmp_path / f"execute.{live_proc.pid}.pid").write_text(
        '{"pid": %d, "pad": "%s"}' % (live_proc.pid, "x" * 100_000))
    assert execute._live_peer_executor(tmp_path, os.getpid()) is None


def test_marker_name_must_match_recorded_pid(tmp_path, live_proc):
    """``execute.999.pid`` claiming another live pid is not an executor marker."""
    import execute

    ct = guard._process_create_time(live_proc.pid)
    (tmp_path / "execute.999.pid").write_text(
        json.dumps({"pid": live_proc.pid, "create_time": ct}))
    assert execute._live_peer_executor(tmp_path, os.getpid()) is None


def test_marker_without_create_time_is_not_authoritative(tmp_path, live_proc):
    """Where a creation marker is obtainable, a marker lacking one (e.g. a
    planted ``{"pid": 1}``) cannot block an executor start."""
    import execute

    if execute._process_create_time(live_proc.pid) is None:
        pytest.skip("no process creation marker on this platform")
    (tmp_path / f"execute.{live_proc.pid}.pid").write_text(
        json.dumps({"pid": live_proc.pid}))
    assert execute._live_peer_executor(tmp_path, os.getpid()) is None


def test_marker_published_atomically(tmp_path, monkeypatch):
    """The marker appears via ``os.replace`` of a complete temp file, and no
    temp file is left behind."""
    import execute

    replaced = []
    real_replace = os.replace

    def spy(src, dst):
        replaced.append((Path(src).read_text(), Path(dst).name))
        return real_replace(src, dst)

    monkeypatch.setattr(execute.os, "replace", spy)
    pidfile = _start_executor(execute, monkeypatch, tmp_path, os.getpid())
    assert len(replaced) == 1
    body, dst = replaced[0]
    assert dst == pidfile.name and json.loads(body)["pid"] == os.getpid()
    assert _entries(tmp_path) == [pidfile.name]


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


# ── Review follow-ups (#338) ───────────────────────────────────────────────

def test_early_pidfile_duplicate_output_flag_last_wins(tmp_path, monkeypatch):
    """argparse keeps the last --output; the early scrape must lock that one,
    not the first, or the guard watches a directory results never reach."""
    import execute

    monkeypatch.setattr(execute, "_PIDFILE_WRITTEN", False)
    monkeypatch.setattr(execute, "_PIDFILE_PATH", None)
    a, b = tmp_path / "a", tmp_path / "b"
    execute._early_pidfile(["--output", str(a), "--run-dir=" + str(b)])
    assert not a.exists()
    assert (b / f"execute.{os.getpid()}.pid").exists()


def test_write_pidfile_refuses_a_different_directory_once_locked(
        tmp_path, monkeypatch, capsys):
    """After the lock is taken on one directory, a later call for another one
    (a disagreeing argument) must not carry on into it."""
    import execute

    first, other = tmp_path / "first", tmp_path / "other"
    first.mkdir()
    other.mkdir()
    _start_executor(execute, monkeypatch, first, os.getpid())
    execute._write_pidfile(first)  # same directory: idempotent
    with pytest.raises(SystemExit) as exc:
        execute._write_pidfile(other)
    assert exc.value.code not in (0, None)
    assert "already owns" in capsys.readouterr().err
    assert list(other.iterdir()) == []
    execute._release_pidfile()


def test_duplicate_output_flag_process_does_not_split_directories(tmp_path):
    """End to end: with ``--output A --output B`` the first directory is never
    touched and the second one is the one that gets locked."""
    a, b = tmp_path / "eval" / "runs" / "e" / "a", tmp_path / "eval" / "runs" / "e" / "b"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    subprocess.run(
        [sys.executable, str(EXECUTE_PATH), "--output", str(a), "--output", str(b)],
        capture_output=True, text=True, timeout=60, cwd=tmp_path,
    )
    assert list(a.iterdir()) == []
    assert (b / ".execute.lock").exists()


def test_new_executor_reaps_dead_markers_only(
        tmp_path, monkeypatch, live_proc, live_proc_b):
    """Markers left by SIGKILLed executors are removed once a new executor owns
    the directory; a live one, a legacy ``execute.pid`` and malformed or
    mismatched files are not ours to delete."""
    import execute

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=10)
    stale = tmp_path / f"execute.{dead.pid}.pid"
    stale.write_text(json.dumps({"pid": dead.pid, "create_time": "1"}))
    reused = tmp_path / f"execute.{live_proc.pid}.pid"  # live pid, other identity
    reused.write_text(json.dumps({"pid": live_proc.pid, "create_time": "bogus"}))
    legacy = tmp_path / "execute.pid"
    legacy.write_text(json.dumps({"pid": dead.pid, "create_time": "1"}))
    garbage = tmp_path / "execute.4242.pid"
    garbage.write_text("not json")
    mismatch = tmp_path / "execute.4243.pid"
    mismatch.write_text(json.dumps({"pid": dead.pid, "create_time": "1"}))

    b = _start_executor(execute, monkeypatch, tmp_path, live_proc_b.pid)
    assert not stale.exists()
    if guard._process_create_time(live_proc.pid) is not None:
        assert not reused.exists()
    assert legacy.exists() and garbage.exists() and mismatch.exists()
    assert b.exists()


def test_reaping_never_touches_a_live_peers_marker(
        tmp_path, monkeypatch, running_executor, live_proc_b):
    """A rejected executor reaps nothing that belongs to the live one."""
    execute, run_dir, a = running_executor
    with pytest.raises(SystemExit):
        _start_executor(execute, monkeypatch, run_dir, live_proc_b.pid)
    assert a.exists()
