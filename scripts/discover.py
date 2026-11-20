#!/usr/bin/env python3
"""Discover eval configs and print results as JSON.

Shared helper used by all eval skill SKILL.md files when --config
is not provided. Scans the project for eval.yaml files and prints
discovery results for the LLM to parse and act on.

Usage:
    python3 scripts/discover.py [--project-root <path>]
    python3 scripts/discover.py --config <eval.yaml> --run-id <id>
    python3 scripts/discover.py --runs-dir --config <eval.yaml>

Output (JSON):
    {
        "configs": [{"path": "...", "eval_name": "...", "is_root": true}],
        "layout": "root|nested|flat|mixed|none",
        "count": 1
    }

With --config and --run-id it prints instead, as one plain line, the run
directory every eval-run script (execute, collect, score, report) resolves
for that run — $AGENT_EVAL_RUNS_DIR/<eval_name>/<run-id> — so a CI wrapper
reads results from exactly where the harness writes them. The path is
relative when $AGENT_EVAL_RUNS_DIR is (resolved against the cwd).

With --runs-dir and --config it prints, the same way, the directory that
holds all of that config's runs — $AGENT_EVAL_RUNS_DIR/<eval_name> — where
eval-anova writes its matrix runs and anova.json and eval-compare reads them.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv
from agent_eval.config import EvalConfig, discover_configs, infer_layout
from agent_eval.run_location import (RunLocationError, config_runs_dir,
                                     resolve_run_dir)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project-root", default=".",
                        help="Project root directory (default: cwd)")
    parser.add_argument("--config", default=None,
                        help="With --run-id: print the run directory for this config; "
                             "with --runs-dir: print the directory holding its runs")
    parser.add_argument("--run-id", default=None,
                        help="With --config: print the run directory for this run id")
    parser.add_argument("--runs-dir", action="store_true",
                        help="With --config: print the directory holding all of the "
                             "config's runs ($AGENT_EVAL_RUNS_DIR/<eval_name>)")
    args = parser.parse_args()

    if args.runs_dir:
        if not args.config:
            parser.error("--runs-dir requires --config")
        if args.run_id:
            parser.error("--runs-dir and --run-id are mutually exclusive")
        config = EvalConfig.from_yaml(args.config)
        try:
            print(config_runs_dir(config))
        except RunLocationError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if args.config or args.run_id:
        if not (args.config and args.run_id):
            parser.error("--config and --run-id must be given together")
        config = EvalConfig.from_yaml(args.config)
        try:
            run_dir, _ = resolve_run_dir(config, args.run_id)
        except RunLocationError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        print(run_dir)
        return

    project_root = Path(args.project_root).resolve()
    configs = discover_configs(project_root)
    layout = infer_layout(configs)

    result = {
        "configs": [
            {
                "path": str(c.path),
                "eval_name": c.eval_name,
                "is_root": c.is_root,
            }
            for c in configs
        ],
        "layout": layout,
        "count": len(configs),
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
