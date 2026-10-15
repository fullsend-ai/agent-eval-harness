# 0009: cap-evolve Integration

## Status

Implemented. See `docs/capevolve-poc.md` for the user-facing reference.

## Problem

`/eval-optimize` works well for quick, interactive skill fixes — Claude
diagnoses failures from judge rationale and transcripts, makes targeted
SKILL.md edits, and verifies. But it has three structural gaps:

1. **No statistical validation** — improvements might be noise from
   stochastic LLM judges; the acceptance check is just "did failures pass?"
2. **No overfitting protection** — all cases are used every iteration, no
   holdout
3. **Claude Code lock-in** — the optimizer IS Claude Code (Skill/Agent/Edit
   tools); no other coding agent can drive it

[cap-evolve](https://github.com/skillberry-ai/cap-evolve) solves all three
with paired significance tests, train/val/test splits with a sealed test set,
cross-iteration memory, and agent-agnostic design — any coding agent CLI can
propose edits. But it has no judge engine of its own: it relies on a
user-provided `adapter.score()` returning a single scalar reward.

## Scope

Connect them: cap-evolve drives the optimization loop (splits, trials, gating,
memory, the sealed test run); agent-eval-harness does the evaluation (the
existing workspace/execution/collection/judge/`compose_reward()` pipeline).
`/eval-optimize` without `--engine capevolve` is unchanged. Local runner only
— Harbor, EvalHub, plugin-resolved skills (`runner.plugin_dirs`), and
prompt-mode/multi-step evaluations are out of scope for this integration.

## Design

### Architecture: an engine-agnostic layer + one coupled file

```
cap-evolve (external, optional dependency, pinned to a commit)
  owns: splits, trials, acceptance gating, cross-iteration memory,
  the sealed test run, the run directory

agent_eval/optimize/        — engine-agnostic. No cap-evolve imports.
  artifact.py                 resolve/validate optimize: candidate+apply_to+
                               feedback+on_error; snapshot; overlay ("make it live")
  rollout.py                  run ONE case: workspace -> execute -> collect ->
                               judge -> compose_reward() (the only reward path)
  export.py                   place a rollout into the standard run-dir layout
  redact.py                   gold-answer scrubbing; outcome-only feedback

agent_eval/engines/         — search-engine-specific integrations
  capevolve.py                 the ONE file coupled to cap-evolve: additional
                                validation (search-key rejection, runner/plugin
                                restrictions), scaffolding, check/estimate/
                                launch/status/apply dispatch, the
                                agent-eval-capevolve CLI entry point
  _bridge_template.py          stdlib-only CapabilityAdapter implementation,
                                COPIED (not imported) into each generated
                                project as adapters/aeh_bridge.py
```

Candidate resolution, running one rollout, exporting results, and redacting
gold are concepts any search engine needs, not just cap-evolve. Keeping
cap-evolve's coupling to a single file is what lets `/eval-optimize`'s
existing interactive loop (or a future different engine) reuse the same
engine-agnostic primitives later.

### The seam: how cap-evolve calls back into the harness

cap-evolve imports a Python file the harness generated and calls methods on
it — no wire, no API, no plugin registry. Its loader
(`cap_evolve/check.py:69-88`) does `importlib.util.spec_from_file_location`
on `<project>/adapters/adapter.py`, requires a class literally named
`Adapter`, and constructs it with zero arguments. Two consequences:

- **No injection point → configuration comes off disk.** Each generated
  project gets `adapters/substrate.json` (narrow: `runner`, `env`, `image`,
  `harness_python` — the execution substrate; a file, not env vars, because a
  `--resume` days later in a new shell would otherwise inherit whatever
  happens to be exported, silently changing substrate mid-campaign) and
  `adapters/candidate.json` (the task/candidate manifest
  `_bridge_template.py` reads).
- **Re-instantiated per phase process** — cap-evolve loads the adapter fresh
  in every subprocess (check, baseline, each algorithm step, finalize).
  Nothing is shared between them except files.

`_bridge_template.py` is copied, not imported, into each project: this avoids
a `sys.path.insert` back into the harness's install location, so a generated
project keeps working even if the harness that scaffolded it later moves —
relevant because a campaign can run for hours unattended. It is deliberately
stdlib-only and never imports `agent_eval._bootstrap`: cap-evolve is a
console script, so the bootstrap's execv-on-ABI-mismatch guard would misfire
inside cap-evolve's own interpreter. Every harness operation it performs
spawns a fresh subprocess (`python -m agent_eval.optimize.rollout ...`) that
activates the real harness venv itself.

`run_target()` does the judging (workspace → execute → collect → judge);
`score()` is a pure in-memory read of what `run_target()` already computed —
one worker-subprocess call per rollout, and judging benefits from
cap-evolve's `run_target` thread pool instead of always running serially.

### Hard rules (verified against cap-evolve's real source, not its docs)

- `compose_reward()` (`agent_eval/harbor/reward.py`) is the only reward
  path — the same function Harbor's reward bridge and `/eval-anova` consume.
  Nothing in this integration computes or overrides a reward itself.
- `score()` must never raise and must never return an unmarked zero —
  cap-evolve does not catch exceptions from `adapter.score()` anywhere, and
  treats any `Score` without `raw={"errored": True}` as a real reward
  averaged into the gate (cap-evolve's own `harness.py` cites a production
  incident caused by exactly this).
- `run_target()` must be thread-safe (cap-evolve pools it across
  `CAPEVOLVE_WORKERS` threads against one shared `Adapter` instance, default
  `1`); `score()` is always called serially and need not be.
- Never import `agent_eval._bootstrap` in cap-evolve's own process; pop the
  `_AGENT_EVAL_BOOTSTRAP_DONE` sentinel last in every child env (mirrors
  `orchestrate.py::_child_env`); never `os.chdir`.
- `capevolve.yaml` is generated, never hand-edited — `optimize:` in
  `eval.yaml` rejects any key cap-evolve's own search config owns
  (`num_trials`, `gate_*`, `split_*`, `max_usd`, `protected_paths`,
  `observers`, etc.). `protected_paths: default` is always generated (a real
  cap-evolve sentinel — omitting it leaves dataset/judge files fully
  editable by the optimizer); `observers: [{backend: mlflow, ...}]` is
  generated when the project's `eval.yaml` has an `mlflow:` block.
- cap-evolve has no native `status`/`apply`/detach/background mode —
  `cap-evolve run` blocks synchronously end-to-end. `engines/capevolve.py`
  builds that layer on the harness side: a detached subprocess
  (`start_new_session=True`) plus a pidfile-style manifest using the same
  pid+creation-time liveness technique as
  `skills/eval-run/scripts/execute.py`/`scripts/eval_stop_guard.py`, reading
  cap-evolve's own on-disk `state.json`/`events.jsonl`/`history.jsonl`/
  `rejected.jsonl`/`splits.json`/`final.json` for progress and the winning
  candidate — nothing cap-evolve already tracks is reimplemented.

### eval.yaml — the only new block

```yaml
reward:
  formula: weighted
  weights:
    quality: 1.0

optimize:
  candidate:
    - {dest: SKILL.md, source: skills/my-skill/SKILL.md}
    - {dest: references/, source: skills/my-skill/references/}
  apply_to: .claude/skills/my-skill
  feedback: redacted      # judge rationale reaches the proposer; gold answers do not
  on_error: noise         # infra failure = missing data, never reward 0
```

A `reward:` block becomes required — the default composition gates on
booleans, so a boolean-heavy eval composes to ≈0 everywhere with no gradient
to climb. Keys the block rejects with a redirect, because `capevolve.yaml`
owns them: `num_trials`, `max_iterations`, `gate_*`, `split_*`, `stall`,
`max_usd`, `store`, `protected_paths`, `observers`, `optimizer_*`,
`target_model`. If `optimize:`/`reward:` are absent entirely (not just
invalid), `--dry-run` returns `{"needs_setup": true, ...}` instead of
erroring, so `/eval-optimize --engine capevolve` can run a setup conversation
and write the block in.

### User journey

```
/eval-setup --optimize                                 # once: installs cap-evolve + its skills tree
/eval-optimize --engine capevolve --dry-run             # plan; a setup conversation if optimize:/reward: are missing
/eval-optimize --engine capevolve --max-usd 420          # confirms spend, launches detached, exits
cap-evolve watch .capevolve/run_<ts>                     # optional, the user's own terminal — real upstream command
/eval-optimize --engine capevolve --status               # any later session; narrates progress, not raw JSON
/eval-optimize --engine capevolve --apply                # branch + apply the winner + optimistic-bias caveat
```

The run is detached: a real campaign is ~5h of work against a ~55 min idle
SIGTERM in an interactive session. The Python layer
(`agent_eval/engines/capevolve.py`) stays headless and returns structured
data; the skill (`skills/eval-optimize/SKILL.md`) owns everything that needs
a conversation — the setup dialogue, the spend confirmation, and narrating
`--status`/`--apply` in prose instead of printing raw JSON. `--apply` requires
`final.json` (the campaign ran to completion; a stalled/killed run is
inspected via `--status`, never auto-applied), copies the winner onto a new
git branch, and leaves the user's current branch/working tree untouched.

## Examples

Scaffold and check readiness without spending anything:

```bash
python3 -m pip install -e '.[optimize]'
agent-eval-capevolve --config eval.yaml --max-usd 20 --dry-run \
  --skills-dir /path/to/cap-evolve/skills
```

Launch a three-case pilot (the live-run scope this integration supports):

```bash
agent-eval-capevolve --config eval.yaml --max-usd 60 \
  --max-iterations 2 --num-trials 1 \
  --pilot-case-ids case-001 case-026 case-002 \
  --runner-budget-usd 15 --judge-budget-usd 2 \
  --optimizer opencode --optimizer-model pricetag-hosted/rits/zai-org/glm-5-3 \
  --skills-dir /path/to/cap-evolve/skills
```

Check progress from any later session, and apply the winner once finished:

```bash
agent-eval-capevolve --status
agent-eval-capevolve --apply
```

Full command/flag reference and output shapes: `docs/capevolve-poc.md`.

## Alternatives Considered

**Rebuilding splits/gate/memory harness-side** — a harness-owned train/val/
test split, a Wilcoxon/t-test gate with p-values and alpha, and a
`LEDGER.json` memory schema. Rejected: none of that exists in real cap-evolve
(verified: no p-value/alpha/Wilcoxon logic anywhere in its source; the real
gate is `mean(per-task delta) > k_se * SE`), and cap-evolve's own adapter
contract says plainly that splits, trials, gating, memory, and the run dir
are provided by core and must not be reimplemented. This implementation only
ever implements the three required adapter methods (`tasks`, `run_target`,
`score`).

**A monolithic cap-evolve-specific package** — every file under
`agent_eval/optimize/` coupled to cap-evolve, with no engine-agnostic layer
and no `agent_eval/engines/`. Rejected: it would block `/eval-optimize`'s
existing interactive loop, or any future different search engine, from
reusing the candidate/rollout/export/redact primitives — see Design above.

**Pushing the setup conversation, spend confirmation, and status narration
into the Python CLI** (interactive prompts on a real TTY). Rejected: the CLI
stays headless and scriptable, returning structured data; only the
Claude-driven skill can hold a conversation, so that layer belongs there.

## Migration

No breaking change to existing usage: `/eval-optimize` without `--engine
capevolve` is untouched, and `optimize:`/`reward:` are new optional
`eval.yaml` keys. `cap-evolve-core` is an optional extra
(`pip install -e '.[optimize]'`), deliberately excluded from the `[all]`
extra (it's a 0.x, commit-pinned package, not on PyPI).

## Open Questions

**`apply_to` vs. `optimize.candidate[].source` for plugin-resolved skills.**
For a plugin-resolved skill (`runner.plugin_dirs`), the live-during-execution
location and the real project location coincide, so a single path could serve
both roles. This integration rejects `runner.plugin_dirs` (plugin support is
out of scope) and uses `apply_to` purely as the `.claude/skills/<name>`
workspace-staging destination, which is why `--apply` needs
`candidate[].source` tracked separately to know the real apply destination.
Revisit if plugin support is ever added.

**CI smoke test.** `tests/test_capevolve_poc.py` has a test that builds a
Task/Rollout/Score and runs real `cap_evolve.check` against a fixture, but
it's `skipif`-guarded on `cap_evolve` being importable, and
`.github/workflows/tests.yml` only installs `.[all,test]` — `optimize` is
deliberately excluded from `[all]`, so the smoke test never actually runs in
CI today.

**Gate-resolution figure in `--dry-run`.** Showing a concrete resolvable-delta
number (e.g. "at 10 val cases this gate resolves Δ ≳ 0.07") needs an assumed
per-task reward variance that doesn't exist before any case has run;
`--dry-run` surfaces the mechanically known facts (split sizes, gate mode,
`k_se`) and the qualitative point (smaller real gains come back indecisive,
not accept) without fabricating a number.

## Future Extensions

- **Harbor substrate** — `agent_eval.harbor.run`/`agent_eval.harbor.reward`
  already exist and are what a containerized `run_target()` should shell out
  to directly, rather than a new bespoke module; judging already happens
  in-container there, so `score()` would read `reward.json` instead of a
  harness summary.
- **`engines/builtin.py`** — refactor today's interactive `/eval-optimize`
  loop into the same engine shape, so it and cap-evolve share the
  `agent_eval/optimize/` primitives (`artifact`, `rollout`, `export`,
  `redact`) instead of only cap-evolve using them.
- **Cost-estimate calibration** — `observed_cost_per_rollout` currently
  averages this eval's own past run costs when any exist; could be extended
  to weight recent runs more heavily or account for candidate size changes.
