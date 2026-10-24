#!/usr/bin/env python3
"""Entry point for /eval-optimize --engine capevolve.

Runs agent_eval.engines.capevolve (the agent-eval-capevolve CLI) from any
benchmark checkout: the cwd is the benchmark, while agent_eval resolves through
this directory's agent_eval symlink, so no install or PYTHONPATH is needed.
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

from agent_eval.engines.capevolve import main

if __name__ == "__main__":
    raise SystemExit(main())
