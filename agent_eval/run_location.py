"""Where a run lives: the one resolver every eval-run script shares.

A run directory is ``$AGENT_EVAL_RUNS_DIR/<eval_name>/<run_id>`` by default,
where ``eval_name`` is ``EvalConfig.eval_name()`` (skill, then ``name``, then
the config path). It is computed *here*, from ``--config`` + ``--run-id``, and
nowhere else: execute.py, collect.py, score.py and discover.py all call
:func:`resolve_run_dir`, so the orchestrator never derives or copies a path
from one command to the next (issue #309: it used to build ``--output`` from
the config ``name`` while the scorer looked under the skill).

``--run-dir`` names the directory outright and overrides the default. Its
basename is the run id, so a ``--run-id`` that disagrees with it is a
conflict, and conflicts fail rather than guess.

Stdlib only, and no ``agent_eval.config`` import: callers pass the config.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


DEFAULT_RUNS_DIR = "eval/runs"

# The run-id rule workspace.py enforces; an explicit --run-dir basename is the
# run id, so it must satisfy it up front rather than fail mid-run.
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]+")


class RunLocationError(ValueError):
    """The run directory cannot be resolved unambiguously."""


def runs_root() -> Path:
    """``$AGENT_EVAL_RUNS_DIR``, or ``eval/runs`` when unset."""
    return Path(os.environ.get("AGENT_EVAL_RUNS_DIR", DEFAULT_RUNS_DIR))


def _check_component(value: object, what: str) -> str:
    """Reject anything that is not a single, plain path component."""
    if (not isinstance(value, str) or not value or value in (".", "..")
            or "/" in value or "\\" in value
            or any(ord(c) < 32 for c in value)):
        raise RunLocationError(f"invalid {what} for a run directory: {value!r}")
    return value


def default_run_dir(config, run_id: str) -> Path:
    """``$AGENT_EVAL_RUNS_DIR/<eval_name>/<run_id>`` for *config*."""
    eval_name = _check_component(config.eval_name(), "eval name")
    _check_component(run_id, "run id")
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise RunLocationError(
            f"invalid --run-id {run_id!r}: the run id must match [A-Za-z0-9._-]+")
    return runs_root() / eval_name / run_id


def resolve_run_dir(config, run_id: str | None = None,
                    run_dir: str | os.PathLike | None = None) -> tuple[Path, str]:
    """``(run_dir, run_id)`` for one invocation.

    * ``run_dir`` given: used as-is; the run id is its basename. A ``run_id``
      that differs from that basename raises :class:`RunLocationError`.
    * only ``run_id``: :func:`default_run_dir`.
    * neither: :class:`RunLocationError`.
    """
    if run_dir:
        path = Path(run_dir)
        resolved = path.resolve()
        cwd = Path.cwd().resolve()
        if resolved == cwd or resolved in cwd.parents:
            raise RunLocationError(
                f"invalid --run-dir {str(run_dir)!r} ({str(resolved)!r}): the run "
                f"directory must be below the current directory ({str(cwd)!r}), "
                f"not the directory itself or one of its ancestors")
        name = resolved.name
        _check_component(name, "run directory basename")
        if not RUN_ID_PATTERN.fullmatch(name):
            raise RunLocationError(
                f"invalid --run-dir basename {name!r}: the basename is the run id "
                f"and must match [A-Za-z0-9._-]+")
        if run_id and run_id != name:
            raise RunLocationError(
                f"--run-id {run_id!r} conflicts with --run-dir {str(run_dir)!r} "
                f"(whose basename, the run id, is {name!r})")
        return path, name
    if not run_id:
        raise RunLocationError("pass --run-id (or --run-dir)")
    return default_run_dir(config, run_id), run_id
