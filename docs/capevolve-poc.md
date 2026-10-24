# cap-evolve integration

This connects an existing agent-eval-harness **skill evaluation** to the
cap-evolve search engine. The harness owns measurement: each rollout goes
through the existing workspace, execution, collection, judge, and
`compose_reward()` paths. cap-evolve owns splits, trials, acceptance gating,
search memory, and the final sealed-test run.

cap-evolve is an optional dependency. Onboarding a new benchmark needs no code
in either repo and no edit to the benchmark itself: run `agent-eval-capevolve`
from the benchmark's checkout and it performs cap-evolve's `intake` phase
itself, then hands the generated project to cap-evolve:

```bash
git clone https://github.com/fullsend-ai/rfe-creator && cd rfe-creator
agent-eval-capevolve --max-usd 50 --dry-run      # intake + scaffold + check + estimate
agent-eval-capevolve --max-usd 50                # launch detached
agent-eval-capevolve --status                    # any later session
agent-eval-capevolve --apply                     # winner onto a capevolve/<run> branch
```

The only seam is the generated `.capevolve/project/adapters/adapter.py`, which
cap-evolve imports by path like any other adapter.

## Intake

Intake mines the benchmark's own `eval.yaml` and resolves every input
cap-evolve's intake contract marks NEEDED:

| Input | Resolved from |
|---|---|
| tasks dataset | `dataset.path` case dirs (`dataset_source: adapter`) |
| target agent | `execution.skill`/`mode`, `runner.type`, `models.skill` |
| capability artifact | `optimize.candidate`, or else the entry skill plus every skill it invokes as `/<name>` or `Skill(skill: "<name>")`, transitively — searched wherever the runner loads skills from: `.claude/skills`, then each `runner.plugin_dirs` skill root |
| scorer + scoring source | the eval config's judges, composed by `compose_reward()` over `reward:` — or else every judge weighted equally with `gate: false` |
| trajectories path | none: cap-evolve's per-rollout JSON. The harness run records mix every split, so handing them to the proposer would leak the sealed test set |

When `eval.yaml` lacks `optimize:` or `reward:`, intake writes them to
`.capevolve/eval.optimize.yaml`, an `extends: ../eval.yaml` overlay, so the
benchmark checkout stays untouched (rfe-creator's `eval.yaml` is generated and
must not be hand-edited). An existing overlay is reused as-is, so edit it to
narrow the candidate or weight the judges; `--reintake` regenerates it.

Blocks declared in the eval config always win. When `eval.yaml` declares both,
intake proposes nothing, uses it directly and deletes any overlay left by an
earlier run. When it declares one, the overlay is rewritten to supply only the
other one, keeping your edits to it.

Intake also resolves the **runtime resources**: the project paths each rollout
workspace carries as real copies so plugin-relative skill scripts resolve
inside Claude's workspace boundary. That is the harness's default project links
(`scripts`, `CLAUDE.md`, `.context`, `skills`), plus every top-level directory the
candidate skills, `.claude/settings.json`, or an included resource reference as
a code path (`"types/…"`, `ROOT / "types"`, `../types/`), to a fixpoint. The
dataset, the run records, every eval config and profile, and `test`/`tests`
are never included, nor is any name the harness writes into the workspace itself
(`hooks/` holds its tool-interception hooks, as in a plain `/eval-run`). For
rfe-creator this resolves to `scripts, CLAUDE.md, types`.

An input that cannot be resolved (no eval config, an `execution.skill` with no
skill directory, no judges, no cases, an unsupported runner) is never guessed.
Intake writes `BLOCKED: <input> — why — how` lines to `PROJECT.md`, removes any
stale `capevolve.yaml`, and the CLI exits 1 with `{"blocked": [...]}`.

`.capevolve/project/` is cap-evolve's own `templates/project` tree (found
next to `--skills-dir`/`$CAPEVOLVE_SKILLS_DIR` or the installed package), with
these files generated over it:

- `adapters/adapter.py` + `aeh_bridge.py` + `substrate.json` + `candidate.json`: the seam
- `seed_capability/`: one directory per candidate skill (a multi-skill `skill-package` root)
- `capevolve.yaml`: the search spec, including an absolute `optimizer_instructions_file` (cap-evolve
  issue #252) and an empty `runner_repo_path`. The benchmark checkout holds gold answers and judge
  prompts, so the proposer is never pointed at it.
- `optimizer/INSTRUCTIONS.md`: cap-evolve's template with every `{{…}}` placeholder intact, plus a
  benchmark-facts section (no benchmark paths)
- `PROJECT.md`: resolved NEEDED inputs and every defaulted RECOMMENDED input with its honesty cost
- `intake.json`: read by `cap-evolve run` into the run's spend ledger (intake makes no LLM calls)

## Architecture

The harness side is split in two, matching cap-evolve's own adapter contract
("splits, trials, gating, pass^k, memory and the run dir are provided by core
and must NOT be reimplemented here" — the one mistake a rebuilt split/gate/
reward-scalarizer on the harness side would be):

- **`agent_eval/optimize/`** — engine-agnostic. Knows nothing about
  cap-evolve or any other search engine.
  - `intake.py` — mines a benchmark checkout: the skill chain, the default
    reward, the runtime resources, and the `extends:` overlay (see Intake).
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
  keys, runner/plugin restrictions), the intake phase's `.capevolve/project/`
  scaffolding,
  `check`/`estimate`/launch/status/apply dispatch, and the `agent-eval-capevolve`
  CLI entry point. `agent_eval/engines/_bridge_template.py` is the stdlib-only
  `CapabilityAdapter` implementation this module copies (not imports) into
  each generated project as `adapters/aeh_bridge.py`.

## Supported scope

- `--runner local` (default) or `--runner harbor` (see Harbor below),
  `execution.mode: case` or `batch`, and `execution.skill`. `--runner evalhub`
  is refused at argparse (no `--cases`: it runs the whole dataset per
  invocation).
- Batch evals are run once per selected case: the harness builds a one-case
  `batch.yaml` for each cap-evolve rollout, preserving the target skill's batch
  invocation arguments while keeping cap-evolve's task-level splits and gates.
- `--max-usd` is mandatory: cap-evolve has no stop command, so a budget cap
  is the only graceful halt. cap-evolve checks its total budget between
  evaluations, so it is a best-effort campaign stop, not a strict
  provider-side billing ceiling.
- An optional pilot (`--pilot-case-ids TRAIN VAL TEST`) pins a three-case
  split, at most `_MAX_PILOT_ITERATIONS` (2) iterations and one trial.
- One or more skill packages from one skill root, configured through
  `optimize.candidate` and `optimize.apply_to`: the project's `.claude/skills`
  or a `runner.plugin_dirs` skill root (e.g. `skills`), or one skill directory
  inside it. No benchmark layout is assumed. rfe-creator (`.claude/skills`, root
  `eval.yaml`) and epic-creator (plugin `skills/`, `eval/epic-decompose.yaml`)
  both onboard unchanged.
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
- **Harbor injects `--skill` from the host.** Harbor uploads each `--skill`
  directory into the trial (`/harbor/skills/<name>`) and the claude-code agent
  registers it under `$CLAUDE_CONFIG_DIR/skills/`, so a candidate reaches the
  container per rollout with no image rebuild (`harbor/trial/trial.py`,
  `harbor/agents/installed/claude_code.py`).

## Harbor

```bash
podman build -f deploy/Containerfile -t localhost/agent-eval-harness:latest .
agent-eval-capevolve --max-usd 50 --dry-run --runner harbor --env podman \
  --image localhost/agent-eval-harness:latest
```

Each rollout reuses the harness's own Harbor path unchanged:

1. **Candidate as a plugin.** A private plugin root holds every skill of the
   candidate's skill source (`.claude/skills` or the plugin's skill roots) with
   the candidate overlaid. A per-rollout `extends:` overlay of the eval config
   makes it the only `runner.plugin_dirs` entry (`!replace`), which
   `agent_eval.harbor.run` forwards as `--skill`. Harbor uploads it into the
   trial, so there is no image rebuild per candidate.
2. **Runtime resources as workspace files.** The same overlay lists intake's
   runtime resources as `dataset.workspace.files` `{dest, source}` entries, which
   the task generator copies into the package's `environment/` (uploaded to
   `/workspace`). `hooks` is a reserved workspace name (tool interception owns
   it), and `.claude/` subdirectories other than skills are not shipped.
3. **A private, scrubbed package.** `skills/eval-dataset/scripts/harbor.py
   --cases <id> --agent-timeout <execution.timeout>` generates one package into a
   private dir; `answers.yaml` is removed from its `environment/`. A
   pre-generated package refuses `--cases`, which is why each rollout generates
   its own.
4. **Judged once, in-container.** `python -m agent_eval.harbor.run --env <env>`
   runs it. The verifier judges inside the container and `run.py` maps the job
   into the usual `run_result.json`/`summary.yaml` (the Harbor job lands under
   the run dir's `harbor-jobs/`). The rollout reads that `summary.yaml` and
   calls the same `compose_reward()`. Re-running the reward bridge would
   double LLM-judge spend. A trial or infra error, or an errored judge
   (`value: None` from a judge without an `if:`), is missing data.

Intake blocks the Harbor substrate, rather than guessing, when `--image` is
missing, `harbor` isn't importable by the harness interpreter (Python ≥ 3.12),
`podman` isn't on `PATH` (for `--env podman`), or `models.skill` is unset.
`--env kubernetes|openshift` uses the same mechanism (skills and resources
travel with the trial, no ConfigMap) but is unverified for optimization.

## Configure `eval.yaml` (optional)

Intake proposes both blocks when they are missing. To pin them yourself, in the
eval config or in the `.capevolve/eval.optimize.yaml` overlay, keep the
existing dataset, runner, judges, and execution settings and add an explicit
reward composition and this measurement/artifact block:

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
candidate package. `apply_to` names the skill root the candidate lives in (or
one skill directory inside it), and must be somewhere the runner actually loads
skills from: `.claude/skills`, or a `runner.plugin_dirs` skill root. Anything
else is refused, because a candidate placed there would never be loaded and the
search would measure nothing. Project skills are staged into each rollout
workspace. Plugin skills run from a private copy of the plugin, which a
per-rollout `extends:` overlay makes the runner's only plugin
(`plugin_dirs: !replace`), so the real plugin is never mutated. `--apply` writes
the winner back to the `source` paths. This requires the `claude-code` runner.
Search settings — splits, gate parameters,
trials, iteration limits, and budgets — are rejected inside `optimize:`; pass
those on the CLI instead.

## Install and dry-run

Install the pinned optional engine dependency — cap-evolve **v0.2.0** (commit
`520ecbe8b`; its `core/pyproject.toml` still reads `0.1.0`, the tag did not bump
it) — and the cap-evolve skills tree from the same version.
The core package alone does not include optimizer/phase skills or the project
templates.

```bash
python3 -m pip install -e '.[optimize]'
export CAPEVOLVE_SKILLS_DIR=/path/to/cap-evolve/skills
cd /path/to/benchmark && agent-eval-capevolve --max-usd 20 --dry-run
```

`--config` defaults to `./eval.yaml`. The command runs intake, creates
`.capevolve/project/`, runs `cap-evolve check`, and prints
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
agent-eval-capevolve --max-usd 50 --max-iterations 10 --num-trials 3 \
  --optimizer claude-code --optimizer-model claude-sonnet-4-6
```

Bare `--max-usd` (with none of `--dry-run`/`--status`/`--apply`) launches —
`--run` is accepted as an explicit no-op alias for scripts that want to say
so. The optimizer defaults to `claude-code`; for a no-model plumbing check,
use `--optimizer mock`, which validates the adapter and rollout path but is
not expected to improve the skill. Each rollout gets its own harness run ID and isolated
workspace with gold removed before the target runs; harness judges read the
original dataset, and only composed reward plus redacted outcome labels reach
cap-evolve.

For a smoke pilot, specify exactly three dataset cases in train/val/test order
and a per-invocation runner limit, for example:

```bash
agent-eval-capevolve --max-usd 60 --max-iterations 2 --num-trials 1 \
  --pilot-case-ids case-001 case-026 case-002 --runner-budget-usd 15 \
  --optimizer opencode --optimizer-model pricetag-hosted/rits/zai-org/glm-5-3
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

## Outputs

A finished campaign leaves two sets of output.

**cap-evolve's campaign record** — `.capevolve/run_<ts>/`:

| File | What it holds |
|---|---|
| `final.json` | the winner (`best_id`), sealed test reward ± stderr, test delta, per-task test rewards |
| `report.md` | baseline vs best val, held-out test, val→test gap (the selection optimism) |
| `dashboard.html` | a self-contained static dashboard — open it in a browser |
| `baseline.json`, `splits.json`, `state.json` | baseline scores, the frozen split, budget and spend |
| `events.jsonl`, `eval_index.jsonl` | every step, evaluation and gate decision |
| `history.jsonl`, `rejected.jsonl` | accepted / rejected candidates (written once there is one) |
| `candidates/<id>/` | each candidate's skills, plus the proposer's `DIAGNOSIS.json` and `PROCESS.md` |
| `JOURNAL.md`, `INSIGHTS.md`, `META_INSIGHTS.md` | the optimizer's cross-iteration memory |

Dashboards: `dashboard.html` is always written; the live web dashboard is the
separate `capevolve-dashboard` package (`pip install -e <cap-evolve>/dashboard/backend`),
and `cap-evolve watch .capevolve/run_<ts>` streams one line per gate decision.
With an `mlflow:` block in the eval config, the generated `capevolve.yaml` adds an
MLflow observer (needs cap-evolve's `capevolve_telemetry` package).

**One harness run dir per rollout** — `$AGENT_EVAL_RUNS_DIR/<eval>/opt-<case>-<candidate>-<seed>-<id>/`
with the usual `run_result.json`, `summary.yaml`, `report.html`, `cases/`,
transcripts and subagent logs, plus a `condition.json` naming the engine,
substrate, candidate, case and seed. The harness's own tools read them:

| Tool | Over rollout run dirs |
|---|---|
| `/eval-compare` | works — discovers runs by `summary.yaml` + `run_result.json` |
| `/eval-mlflow` | works — `log_results.py --run-id <opt-…>` |
| `/eval-review` | works — per run dir |
| `report.html` | written per rollout (local by `report.py`, Harbor by `agent_eval.harbor.run`) |
| `/eval-anova` | **not yet** — its `condition.json` schema is `{condition_id, levels}`, so it would treat each rollout as its own condition; don't run it over `opt-*` runs |

## From `/eval-optimize`

`/eval-optimize --engine capevolve [--dry-run | --max-usd N | --status | --apply]`
delegates to `agent-eval-capevolve` with the equivalent flags, adding what only
a conversation can do: resolving `blocked` inputs with you, telling you which
defaults intake used (it uses them as-is; edit the overlay only if you want
to), a spend confirmation before launching, and narrating
`--status`/`--apply` results in prose instead of printing raw JSON. Omit `--engine` for the existing
interactive diagnose/edit/verify loop.
