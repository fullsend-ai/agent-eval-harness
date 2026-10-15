# cap-evolve integration

This connects an existing agent-eval-harness **skill evaluation** to the
cap-evolve search engine. The harness owns measurement: each rollout goes
through the existing workspace, execution, collection, judge, and
`compose_reward()` paths. cap-evolve owns splits, trials, acceptance gating,
search memory, and the final sealed-test run.

## Architecture

The harness side is split in two, matching cap-evolve's own adapter contract
("splits, trials, gating, pass^k, memory and the run dir are provided by core
and must NOT be reimplemented here" — the one mistake a rebuilt split/gate/
reward-scalarizer on the harness side would be):

- **`agent_eval/optimize/`** — engine-agnostic. Knows nothing about
  cap-evolve or any other search engine.
  - `artifact.py` — resolves/validates `optimize:`'s universal vocabulary
    (`candidate`, `apply_to`, `feedback`, `on_error`), snapshots a candidate's
    seed files, and overlays (stages) a candidate into an isolated rollout
    workspace.
  - `rollout.py` — runs ONE case end to end: workspace → execute → collect →
    judge → `compose_reward()`. A judging failure and an execution failure are
    both reported the same way (the rollout's `error` field), since from a
    search engine's point of view both mean "no real measurement."
  - `export.py` — places a rollout's artifacts into the standard
    `$AGENT_EVAL_RUNS_DIR/<eval-name>/<run-id>` layout with `condition.json`,
    so `/eval-compare`, `/eval-mlflow`, and `/eval-review` work on the result
    unchanged.
  - `redact.py` — strips `answers.yaml` from a rollout workspace and builds
    the outcome-only feedback string (pass/fail/numeric labels, never judge
    rationale) that reaches a search engine's proposer.
- **`agent_eval/engines/capevolve.py`** — the one file coupled to cap-evolve's
  API: additional cap-evolve-specific validation (rejecting search-config
  keys, runner/plugin restrictions), scaffolding a `.capevolve/project/`,
  `check`/`estimate`/launch/status/apply dispatch, and the `agent-eval-capevolve`
  CLI entry point. `agent_eval/engines/_bridge_template.py` is the stdlib-only
  `CapabilityAdapter` implementation this module copies (not imports) into
  each generated project as `adapters/aeh_bridge.py`.

## Supported scope

- Local runner, `execution.mode: case` or `batch`, and `execution.skill`.
- Batch evals are run once per selected case: the harness builds a one-case
  `batch.yaml` for each cap-evolve rollout, preserving the target skill's batch
  invocation arguments while keeping cap-evolve's task-level splits and gates.
- A live pilot is restricted to a three-case train/val/test run, at most
  `_MAX_PILOT_ITERATIONS` (2) iterations, one trial, and explicit
  per-target/per-runner-judge budgets. This bounds known harness spend;
  optimizer spend (e.g. OpenCode) remains provider-accounted. cap-evolve
  checks its total budget between
  evaluations, so `--max-usd` is a best-effort campaign stop, not a strict
  provider-side billing ceiling.
- One skill package, configured through `optimize.candidate` and `optimize.apply_to`.
- Gold answers are removed from the rollout workspace and judge rationale is
  not forwarded to the proposer (`feedback: redacted`).
- Harness rollout outputs are stored in ordinary `$AGENT_EVAL_RUNS_DIR/<eval-name>/<run-id>`
  run directories with a `condition.json` identifying each cap-evolve candidate.
- A campaign launches as a **detached** background process and returns
  immediately; `--status` and `--apply` inspect and act on it afterward (see
  below). Harbor, EvalHub, and prompt-mode/multi-step evaluations are still
  outside this scope.

## Verified against cap-evolve's real source

The points below were checked directly against the `cap-evolve-core` source
(not inferred from its docs), because getting them wrong either breaks the
adapter contract or silently corrupts the optimization run.

- **`compose_reward()` is the only reward path.** `rollout.run_case()` calls
  `agent_eval.harbor.reward.compose_reward()` — the same function Harbor's
  reward bridge and `/eval-anova` consume — and nothing else. cap-evolve never
  computes or overrides a reward itself; it only ever receives the resulting
  `[0, 1]` scalar plus a redacted feedback string, embedded directly in the
  rollout it already returned. There is intentionally no second reward engine
  anywhere in this integration.
- **`score()` must never raise, and must never return an unmarked zero.**
  cap-evolve does not catch exceptions from `adapter.score()` anywhere (unlike
  `run_target`, whose exceptions it converts into an errored `Rollout`) — an
  uncaught exception crashes the whole optimization phase. The only way to
  mark a sample as "no real measurement, exclude it" is `rollout.error` (set
  by `run_target`) or `raw={"errored": True}` on the returned `Score`; an
  unmarked `Score(reward=0.0, ...)` is averaged into the task's real mean and
  used in the gate (cap-evolve's own `harness.py` carries a comment citing a
  production incident caused by exactly this). `Bridge.score()` is a pure read
  of what `run_target()` already computed and always carries this marker on
  every non-measurement path — a hard requirement, not a style choice.
- **`run_target()` does the judging; `score()` is a pure read.** Both run in
  the same process on the live path, so `run_target()` embeds the composed
  reward and feedback directly into the `Rollout`'s `metadata`, and `score()`
  just reads them back — no second subprocess call. This also means judging
  benefits from cap-evolve's `run_target` thread pool instead of always
  running serially.
- **Concurrency**: cap-evolve pools `run_target()` calls across
  `CAPEVOLVE_WORKERS` threads against one shared `Adapter` instance (default
  `1`, i.e. sequential, unless that env var is set), but always calls `score()`
  serially afterward. `run_target` must therefore be thread-safe; `score` need
  not be. The bridge here already satisfies this by construction — every
  call spawns its own subprocess with a UUID-suffixed run id and workspace,
  and nothing is mutated on `self` after `__init__`.
- **cap-evolve has no native `status`/`apply`/detach/background mode.**
  `cap-evolve run` blocks synchronously end-to-end; its real subcommands are
  `help, init, doctor, algorithms, diff, version, splits, check, run, estimate,
  dashboard, tail, watch, replay`. `agent_eval/engines/capevolve.py` builds
  the detach/status/apply layer the harness needs on top of that blocking
  call: a background subprocess plus a pidfile-style manifest (the same
  pid+creation-time liveness technique as `skills/eval-run/scripts/execute.py`),
  and reads cap-evolve's own on-disk `run_<ts>/state.json`, `events.jsonl`,
  `history.jsonl`, `rejected.jsonl`, `splits.json`, and `final.json` for
  progress, the accepted/rejected/indecisive breakdown, and the winning
  candidate — nothing is reimplemented that cap-evolve itself already tracks.
- **`protected_paths`/`observers` are real cap-evolve `capevolve.yaml` keys.**
  `protected_paths: default` is always generated (the literal string
  `"default"` is a real sentinel expanding to cap-evolve's own
  `integrity.DEFAULT_PATTERNS` tamper seal — omitting this key entirely
  leaves dataset/judge/scorer files fully editable by the optimizer).
  `observers: [{backend: mlflow, ...}]` is generated when the project's
  `eval.yaml` has an `mlflow:` block — loaded automatically by cap-evolve's
  own baseline phase, no extra invocation needed on our side.
- **For a future Harbor phase**: `capevolve_harbor/results.py` reads
  `reward.json` + `result.json` + `agent/trajectory.json` from a Harbor job
  dir — not `judges.json`/`summary.yaml`. `capevolve_harbor/tasks.py` has no
  case filter; it regenerates a full task package per call unconditionally,
  so there's no pre-generated-package reuse to preserve across candidates.

## Configure `eval.yaml`

Keep the existing dataset, runner, judges, and execution settings. Add an
explicit reward composition and this measurement/artifact block:

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
  feedback: redacted
  on_error: noise
```

`source` paths resolve from the project root. `dest` paths are relative to the
candidate package. `apply_to` is the `.claude/skills/<name>` location inside
each isolated rollout workspace — a staging destination, not necessarily the
same path `source` points at (see the note below if your skill is resolved via
`runner.plugin_dirs`, which this integration doesn't support yet). This
currently requires the `claude-code` runner and no `runner.plugin_dirs`;
plugin staging is a follow-up. Search settings — splits, gate parameters,
trials, iteration limits, and budgets — are rejected inside `optimize:`; pass
those on the CLI instead.

If `optimize:`/`reward:` are missing entirely, `--dry-run` returns
`{"needs_setup": true, "missing": [...]}` instead of erroring — `/eval-optimize
--engine capevolve` uses this to ask you which files are the candidate and
what the reward should be, then writes the block in for you.

## Install and dry-run

Install the pinned optional engine dependency and the cap-evolve skills tree.
The core package alone does not include optimizer/phase skills.

```bash
python3 -m pip install -e '.[optimize]'
agent-eval-capevolve --config eval.yaml --max-usd 20 --dry-run \
  --skills-dir /path/to/cap-evolve/skills
```

The command creates `.capevolve/project/`, runs `cap-evolve check`, and prints
cap-evolve's estimate plus split sizes and (when this eval has prior harness
runs) an `observed_cost_per_rollout` figure calibrated from them. The
generated adapter is `.capevolve/project/adapters/adapter.py` — a ~5-line shim
cap-evolve's zero-arg `Adapter()` construction loads, importing from the
adjacent, self-contained `aeh_bridge.py` (not from the harness's install
location, so the generated project keeps working even if the harness that
scaffolded it later moves). `substrate.json` next to it carries the narrow
execution substrate (`runner`, `env`, `image`, `harness_python`); the fuller
task/candidate manifest lives in `candidate.json`.

## Launch a campaign

```bash
agent-eval-capevolve --config eval.yaml --max-usd 20 --max-iterations 1 \
  --num-trials 1 --optimizer claude-code \
  --optimizer-model claude-sonnet-4-6 \
  --skills-dir /path/to/cap-evolve/skills
```

Bare `--max-usd` (with `--config`, and none of `--dry-run`/`--status`/
`--apply`) launches — `--run` is accepted as an explicit no-op alias for
scripts that want to say so. For a no-model plumbing check, use `--optimizer
mock`; it validates the adapter and rollout path but is not expected to
improve the skill. Each rollout gets its own harness run ID and isolated
workspace with gold removed before the target runs; harness judges read the
original dataset, and only composed reward plus redacted outcome labels reach
cap-evolve.

For a live pilot, specify exactly three dataset cases in train/val/test order
and per-invocation limits, for example:

```bash
agent-eval-capevolve --config /path/to/rfe-capevolve.yaml --max-usd 60 \
  --max-iterations 2 --num-trials 1 \
  --pilot-case-ids case-001 case-026 case-002 \
  --runner-budget-usd 15 --judge-budget-usd 2 \
  --optimizer opencode --optimizer-model pricetag-hosted/rits/zai-org/glm-5-3 \
  --skills-dir /path/to/cap-evolve/skills
```

The launch is **detached** — it survives the launching terminal/session
closing, since real campaigns run for hours — and returns immediately with
the campaign's `run_dir`, `pid`, `log_file`, and a `stop_hint` (`kill <pid>`).
It does not stream progress; use `--status` for that. The run directory lands
under `.capevolve/run_<timestamp>`; candidate-level harness runs remain
inspectable via the normal harness reports and comparison tools.

## Check status

```bash
agent-eval-capevolve --status
```

Run from the project root (no `--config` needed — it reads the one active
campaign recorded under `.capevolve/project/`). Reports `not_launched`,
`running`, `finished`, or `stopped` (the recorded process is gone with no
`final.json` — check `log_file` for why), the campaign's `budget`/`spent`, the
latest iteration's outcome, and an `iterations: {accepted, rejected,
indecisive}` breakdown — counted from cap-evolve's own `history.jsonl`/
`rejected.jsonl`/`events.jsonl` (there's no single field carrying this; a line
in `history.jsonl` means accepted, a line in `rejected.jsonl` means rejected,
and indecisive candidates appear only as a `step_indecisive` event).

## Apply the winner

```bash
agent-eval-capevolve --apply
```

Only works once `--status` reports `finished` (cap-evolve has written
`final.json` — the campaign ran to completion and burned its sealed test
evaluation; a stalled or killed run is not auto-applied). Requires a clean
project working tree. Copies the winning candidate's files from
`run_<ts>/candidates/<best_id>/` onto their real project locations (the
`optimize.candidate[].source` paths) on a new branch, `capevolve/run_<ts>`,
commits them there, and leaves your current branch and working tree
untouched — review or merge the branch yourself. The result also includes a
`caveat` (the sealed test delta is the honest number; a full `/eval-run` will
look better because some cases were seen during the search) and a
`suggested_next_step` naming that confirmation run.

## From `/eval-optimize`

`/eval-optimize --engine capevolve [--dry-run | --max-usd N | --status | --apply]`
delegates to `agent-eval-capevolve` with the equivalent flags, adding what only
a conversation can do: a setup dialogue when `needs_setup` comes back, a spend
confirmation before launching, and narrating `--status`/`--apply` results in
prose instead of printing raw JSON. Omit `--engine` for the existing
interactive diagnose/edit/verify loop.
