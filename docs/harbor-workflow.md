# Harbor Workflow

Run evals on [Harbor](https://github.com/laude-institute/harbor) (containerized
execution: Podman locally, Kubernetes or OpenShift) while keeping the harness's
judging, MLflow backbone, and authoring skills.

## Setup (once)

Build the base image — OS + agent CLI + harness (judge engine, reward bridge):

```bash
podman build --platform linux/amd64 -f deploy/Containerfile \
  -t <registry>/agent-eval-harness:latest .
podman push <registry>/agent-eval-harness:latest
```

If project resources are too large for the per-case `environment/` upload
(e.g. large `.context/` directories), build a project-specific image:

```bash
cd <project-repo>
podman build --platform linux/amd64 -f deploy/Containerfile \
  --build-arg BASE_IMAGE=<registry>/agent-eval-harness:latest \
  -t <registry>/<project>-eval:latest .
podman push <registry>/<project>-eval:latest
```

For Podman (local dev), project resources can be bind-mounted instead:
`AGENT_EVAL_PODMAN_PROJECT_DIR=.`

Large read-only datasets should be mounted rather than copied into every task:

```bash
--mount /absolute/host/data:/data:ro
```

For OpenShift credentials and project resource options, see
`deploy/harbor/README.md`.

## Generate dataset + task packages

```bash
/eval-dataset --harbor --image <registry>/<project>-eval:latest
```

This produces:

```
eval/dataset/cases/           ← test cases (input.yaml, answers.yaml, ...)
eval/harbor-tasks/            ← task packages (one per case)
  case-001-.../
    task.toml                 ← runtime image, timeouts
    instruction.md            ← skill invocation command
    tests/test.sh             ← verifier: judge → reward.json bridge
    tests/eval.yaml           ← bundled judge config
    environment/              ← uploaded to workspace by Harbor
      input.yaml, batch.yaml, hooks/, .claude/settings.json
```

## Run

### Credentials (.env file)

Create a `.env` file in the project root with cluster-specific config.
`run.py` loads it automatically via `_load_dotenv()`:

```
AGENT_EVAL_K8S_NAMESPACE=<namespace>
AGENT_EVAL_K8S_CREDENTIALS_SECRET=<secret-name>
```

The credentials Secret should contain API keys or gateway config
(e.g. `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` for a LiteLLM
gateway). See `deploy/harbor/README.md` for credential options.

### Using eval-run (recommended)

`eval-run --runner harbor` wraps the full flow — reuses pre-generated
task packages (or generates them if `--image` is provided), calls
`harbor run`, parses results, generates report, checks regressions:

```bash
# Kubernetes (default)
/eval-run --runner harbor --model <model> -n 10

# Podman (local)
/eval-run --runner harbor --env podman --model <model>
```

Codex is selected by `runner.type: codex`. The wrapper automatically forwards
every configured plugin's complete `skills/` root (including sibling/dependent
skills) and translates `runner.effort` into Harbor's Codex agent option:

Skill roots are forwarded for every Harbor agent, not just Codex — `--skill`
becomes the agent's `skills_dir`, which the stock claude-code agent also reads.
A plugin that exports no skills (commands only) is skipped with a warning for
other agents, but fails fast for Codex, which can only consume a plugin's skills.

Omitting `effort:` does not mean the trial runs without one: Harbor's Codex agent
defaults `reasoning_effort` to `high`. Run metadata records that effective value,
so a matrix cell with no `effort:` is not treated as a different condition from
an explicit `effort: high`.

```yaml
runner:
  type: codex
  effort: xhigh
  plugin_dirs: [plugins/ci]
models:
  skill: gpt-5.6-luna
```

```bash
python -m agent_eval.harbor.run ... \
  --env podman --model gpt-5.6-luna \
  --n-concurrent 1 --cpus 2 --memory-mb 4096 \
  --mount "$HOME/git/historical-payload-data:/historical-payload-data:ro"
```

`--mount` is implemented by the local Podman environment. Kubernetes runs need
the equivalent data in a PVC, image, or other cluster-visible volume; a laptop
host path cannot be mounted into an OpenShift pod. When external Podman mounts
are present, the adapter disables SELinux container labeling instead of
recursively relabeling the host dataset; the requested read-only mode remains
enforced by Podman. Disabling labels removes SELinux separation for the entire
trial container, so task code can read host content accessible to the mapped
UID; `:ro` prevents writes only. Prefer a dedicated, pre-labeled data directory
and never expose a broad home or checkout directory to an untrusted task.

Provider environment variables, including API keys, are forwarded by name:
their values are supplied through Podman's process environment rather than
embedded in `podman run` or `podman exec` arguments. This prevents ordinary
process listings from printing credentials. The Harbor wrapper likewise passes
agent env values indirectly through its child environment, not `harbor run`
argv. Same-UID host processes can still inspect environment values; this is not
a credential-isolation boundary.

The `--env` flag selects the execution environment (`kubernetes` by
default, also accepts `podman`, `openshift`, `k8s`). The `-n` flag
sets parallelism (concurrent pods/containers).

Produces the same `runs/<id>/` layout as a local run (summary.yaml,
report.html, per-case artifacts).

### Using harbor run directly

Run individual cases or the full task set with Harbor's CLI:

```bash
# Podman (local)
PYTHONPATH="$(pwd)" \
harbor run -p eval/harbor-tasks \
  --agent codex -m gpt-5.6-luna \
  --agent-kwarg reasoning_effort=xhigh \
  --skill /absolute/path/to/plugins/ci/skills \
  --mounts '[{"type":"bind","source":"/absolute/host/data","target":"/data","read_only":true}]' \
  --environment-import-path agent_eval.harbor.podman:PodmanEnvironment \
  -n 1 -o eval/harbor-jobs

# Kubernetes / OpenShift
PYTHONPATH="$(pwd)" \
harbor run -p eval/harbor-tasks \
  --agent claude-code -m <model> \
  --environment-import-path agent_eval.harbor.kubernetes:KubernetesEnvironment \
  -n 5 -o eval/harbor-jobs
```

Run a single case by pointing at its directory:

```bash
harbor run -p eval/harbor-tasks/case-001-... --agent claude-code -m <model> ...
```

Results land in `eval/harbor-jobs/<timestamp>/` with per-trial
transcripts (`agent/claude-code.txt`, `agent/trajectory.json`),
verifier output (`verifier/reward.json`, `verifier/judges.json`),
and collected artifacts.

## Review + MLflow

Results from `eval-run --runner harbor` are in the same format as local
runs — these work unchanged:

```bash
/eval-review --run-id <id>
/eval-mlflow --run-id <id>
```

## What goes where

| Content | How it gets into the pod |
|---|---|
| Runtime (OS, Claude Code + Codex CLIs, harness) | Container image pull (`docker_image` in task.toml) |
| Project resources (skills, scripts, .context) | Image layer, bind-mount (Podman), or ConfigMap (K8s) |
| Per-case files (input.yaml, hooks) | Harbor `environment/` upload via exec |
| Instruction (skill command) | Harbor reads `instruction.md` as agent prompt |
| Verifier (judges) | Harbor uploads `tests/` for verifier step |

## Environment variables

### Podman

| Variable | Description |
|---|---|
| `AGENT_EVAL_PODMAN_PROJECT_DIR` | Bind-mount project resources (no project image needed) |
| `AGENT_EVAL_PODMAN_GCP_CREDENTIALS_FILE` | GCP service-account key (read-only mount) |
| `AGENT_EVAL_PODMAN_KEEP_RUN` | `1` to keep container after trial for debugging |

### Kubernetes / OpenShift

| Variable | Description |
|---|---|
| `AGENT_EVAL_K8S_NAMESPACE` | Target namespace |
| `AGENT_EVAL_K8S_CREDENTIALS_SECRET` | Secret with API keys (injected via envFrom) |
| `AGENT_EVAL_K8S_GCP_CREDENTIALS_SECRET` | Secret with GCP SA key (file mount) |
| `AGENT_EVAL_K8S_SERVICE_ACCOUNT` | Pod ServiceAccount (Workload Identity) |
| `AGENT_EVAL_K8S_PROJECT_CONFIGMAP` | ConfigMap with project resources (< 1 MB) |
| `AGENT_EVAL_K8S_INSTALL_PACKAGES` | `1` to run agent install (default: skip for pre-built images) |
| `AGENT_EVAL_K8S_KEEP_RUN` | `1` to keep pod after trial for debugging |
| `AGENT_EVAL_K8S_CPU` / `AGENT_EVAL_K8S_MEMORY` | Resource requests (default: 1 / 2Gi) |

## Debugging

Keep the container/pod alive after a trial:

```bash
AGENT_EVAL_PODMAN_KEEP_RUN=1 ...    # podman logs <name>; podman exec -it <name> /bin/sh
AGENT_EVAL_K8S_KEEP_RUN=1 ...       # oc logs <pod>; oc exec -it <pod> -- /bin/sh
```

Agent logs stream to pod stdout (visible via `oc logs -f <pod>`).

Harbor also captures transcripts in the job directory:
`agent/claude-code.txt` (stream-json), `agent/trajectory.json` (ATIF),
`agent/sessions/projects/**/*.jsonl` (Claude Code's persisted session
transcripts), `verifier/reward.json`, `verifier/judges.json`. OpenRouter
generation IDs are collected from both the stream-json and the session
transcripts, because some Claude Code versions omit `requestId` from
stream-json.
