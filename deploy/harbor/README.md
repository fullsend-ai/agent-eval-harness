# Harbor integration

Run agent-eval-harness evals on the [Harbor](https://github.com/laude-institute/harbor)
execution substrate (containers: Podman locally, Kubernetes or OpenShift) while keeping
the harness's judging, MLflow backbone, and authoring skills. Harbor is the engine;
agent-eval-harness is the judgment + authoring layer on top.

The interop surface is the **Harbor task package + `reward.json`**. Nothing here forks
Harbor — our environments plug in via `--environment-import-path` and task packages
are self-contained (Harbor's stock agents run them directly).

## Components

| Piece | Path | Role |
|---|---|---|
| Task generation (library) | `agent_eval/harbor/tasks.py` | `eval.yaml` + dataset → per-case Harbor task packages |
| Task generation (CLI) | `skills/eval-dataset/scripts/harbor.py` | thin `/eval-dataset` wrapper around `tasks.generate_tasks` |
| Judge → reward bridge | `agent_eval/harbor/reward.py` | runs the judge engine as the Harbor verifier → `reward.json` |
| Podman environment | `agent_eval/harbor/podman.py` | native local env via the `podman` CLI (`--environment-import-path`) |
| Kubernetes environment | `agent_eval/harbor/kubernetes.py` | OpenShift env via the Kubernetes Python client (in-cluster ready) |
| (no custom agent) | — | Tasks are self-contained; use Harbor's stock agents (`--agent claude-code`, `--agent opencode`, etc.) |
| Results parser | `agent_eval/harbor/results.py` | Harbor job dir → per-case results for MLflow/report |
| Run orchestration | `agent_eval/harbor/run.py` | `/eval-run --runner harbor`: generate/reuse tasks → run → map → report |
| Base image | `deploy/Containerfile` | generic runtime: OS + agent CLIs + harness (trial pods) |
| EvalHub provider | `deploy/evalhub/Containerfile` | FROM base + harbor + k8s client + eval-hub-sdk (orchestrator pod) |

## Container images

**Base image** (`deploy/Containerfile`): generic agent-eval runtime for trial pods.
Contains UBI9 + python + node + claude-code + codex + agent-eval-harness (judge engine,
reward bridge, interception). No project code — project resources come from a
ConfigMap (K8s), bind-mount (Podman), or a thin `FROM base` Containerfile in
the project's own repo.

```bash
podman build -f deploy/Containerfile -t quay.io/rhoai/agent-eval-harness:latest .
```

**EvalHub provider** (`deploy/evalhub/Containerfile`): `FROM base` + harbor +
kubernetes client + eval-hub-sdk. The orchestrator pod that creates trial pods.

## Orchestrated run (eval-run)

The simplest way to run on Harbor is through the eval-run skill:

```bash
# Kubernetes (default)
/eval-run --runner harbor --model <model> -n 10

# Podman (local)
/eval-run --runner harbor --env podman --model <model>
```

Cluster-specific config is read from a `.env` file in the project root:

```
AGENT_EVAL_K8S_NAMESPACE=<namespace>
AGENT_EVAL_K8S_CREDENTIALS_SECRET=<secret-name>
```

See `docs/harbor-workflow.md` for the full workflow.

## Local run (Podman)

```bash
# 1. Build the base image (once)
podman build -f deploy/Containerfile -t localhost/agent-eval-harness:latest .

# 2. Generate per-case Harbor tasks from an eval.yaml
python3 skills/eval-dataset/scripts/harbor.py \
    --config <eval.yaml> --out harbor-tasks --image localhost/agent-eval-harness:latest \
    --arguments '{prompt}' --skill <skill> \
    [--judge-model <model>]

# 3. Run on Harbor — stock agent, our Podman env
PYTHONPATH="$(pwd)" harbor run -p harbor-tasks/<case> --agent claude-code -m <model> \
    --environment-import-path agent_eval.harbor.podman:PodmanEnvironment \
    -n 1 -o harbor-jobs
```

Pass the plugin's whole skill root so orchestrator skills can invoke their
sibling dependencies, and pass large datasets as a bind mount. `--skill` is not
Codex-specific — Harbor hands it to any agent as `skills_dir`, and the stock
claude-code agent copies it into `$CLAUDE_CONFIG_DIR/skills/`:

```bash
PYTHONPATH="$(pwd)" harbor run -p harbor-tasks/<case> \
    --agent codex -m gpt-5.6-luna \
    --agent-kwarg reasoning_effort=xhigh \
    --skill /path/to/project/plugins/ci/skills \
    --mounts '[{"type":"bind","source":"/path/to/history","target":"/historical-payload-data","read_only":true}]' \
    --environment-import-path agent_eval.harbor.podman:PodmanEnvironment \
    -n 1 -o harbor-jobs
```

The higher-level `agent_eval.harbor.run` wrapper derives `--skill` and the
Codex reasoning effort from `eval.yaml`; its repeatable `--mount` flag generates
the Harbor mount JSON. Host bind mounts apply to Podman only. For external bind
mounts the local adapter uses `--security-opt label=disable`, avoiding a
recursive SELinux relabel of the host data while preserving Podman's `:ro` mode.
This disables SELinux separation for the whole trial container: task code can
read any host content exposed to the mapped UID, and `:ro` prevents writes only.
Prefer a dedicated, pre-labeled data directory; do not mount a broad home or
checkout directory into an untrusted task.

Notes:
- `PYTHONPATH` must include this repo so Harbor can import the environment
  plug-ins (unnecessary if agent-eval-harness is pip-installed).
- **Auth** — provider config and API keys (`ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`, `AWS_ACCESS_KEY_ID`, etc.) are forwarded from the host env
  into the container automatically. Podman receives only variable names in its
  command arguments; values travel through the child process environment so
  normal process listings do not print credentials. Same-UID processes can
  still inspect those environment values; this local environment is not a
  credential-isolation boundary. For Vertex AI
  (which needs a credentials file), set
  `AGENT_EVAL_PODMAN_GCP_CREDENTIALS_FILE=/path/to/sa-key.json` (mounted
  read-only). See the OpenShift section for Secret / Workload Identity equivalents.
- Per-case grading writes `reward.json` (boolean judges gate; numeric LLM judges average).
  Pairwise + regression thresholds stay suite-level above Harbor.
- `dataset.workspace.files` shared `{dest, source}` entries are resolved from the local
  checkout at task-generation time and copied into each task's `environment/` (which
  Harbor uploads into the workspace), so the file travels inside the package — no
  ConfigMap, bind-mount, or project image needed for it.

## Debugging a run

By default the environment deletes its container/pod after each trial. To keep it
alive for inspection:

```bash
AGENT_EVAL_PODMAN_KEEP_RUN=1 ...   # local: leaves the container running
                        #   podman logs <name> ; podman exec -it <name> /bin/sh
AGENT_EVAL_K8S_KEEP_RUN=1 ...      # OpenShift: leaves the pod running
                        #   oc logs <pod> -n <ns> ; oc exec -it <pod> -n <ns> -- /bin/sh
```

The kept container/pod name is logged at the end of the run. Clean up afterwards
with `podman rm -f <name>` or `oc delete pod -l app.kubernetes.io/managed-by=agent-eval-harness -n <ns>`.
Note: each trial uses a unique name, so kept resources accumulate — remember to prune.

Even without keeping the pod, Harbor captures the agent transcript
(`agent/claude-code.txt` or `agent/codex.txt`, plus `agent/trajectory.json`), subagent transcripts, and the
verifier output (`verifier/{reward.json,judges.json,test-stdout.txt}`) into the job dir
before deletion.

## OpenShift

The Kubernetes `BaseEnvironment` (sibling of the Podman env) runs the same task images
as pods under the restricted-v2 SCC (non-root, arbitrary assigned UID; the image must be
group-0 writable). Run it with `--environment-import-path
agent_eval.harbor.kubernetes:KubernetesEnvironment` and `AGENT_EVAL_K8S_NAMESPACE=<ns>`.

It uses the **Kubernetes Python client** (not the `oc` CLI): `load_incluster_config()`
when running inside a pod (the EvalHub provider — uses the pod's ServiceAccount), falling
back to your local kubeconfig. Install with `/eval-setup --harbor` or
`pip install agent-eval-harness[harbor]`. `exec` uses the API's websocket stream;
file copy is tar+base64 over `exec`. The Podman env, by contrast, stays on the
`podman` CLI (local-dev only, the binary is always present).

### Credentials (from the cluster, never copied from the host)

**A. GCP / Vertex AI** — service-account key in a Secret (mounted as a file):
```python
from agent_eval.harbor.k8s_resources import create_creds_secret
create_creds_secret("/path/to/sa-key.json", "vertex-creds", "<ns>")
# then: AGENT_EVAL_K8S_GCP_CREDENTIALS_SECRET=vertex-creds
```

**B. GCP / Vertex AI** — Workload Identity (no stored key, preferred):
```bash
AGENT_EVAL_K8S_SERVICE_ACCOUNT=<sa>        # pod runs as this SA, federated to GCP
```

**C. API keys / Bedrock** — env vars from a Secret (injected via envFrom):
```python
from agent_eval.harbor.k8s_resources import create_env_secret
create_env_secret({"ANTHROPIC_API_KEY": "sk-..."}, "model-keys", "<ns>")
# then: AGENT_EVAL_K8S_CREDENTIALS_SECRET=model-keys
```

### Project resources (no project-specific image needed)

Mount project resources (skills, scripts, .context, CLAUDE.md) from a ConfigMap
instead of baking them into a project-specific image. The generic base image +
a ConfigMap covers any project:

```python
from agent_eval.harbor.k8s_resources import create_project_configmap
create_project_configmap("/path/to/project", "my-project", "<ns>")
# then: AGENT_EVAL_K8S_PROJECT_CONFIGMAP=my-project harbor run -p <task> --agent claude-code ...
```

The mount is read-only (defaultMode 0755 so scripts are executable). The agent
copies what it needs into `/workspace` at runtime.

Locally with Podman, the equivalent is a bind-mount from a host directory:
```bash
AGENT_EVAL_PODMAN_PROJECT_DIR=/path/to/project /eval-run --runner harbor ...
```

`/eval-run --runner harbor` masks the eval dataset and the project's Git metadata inside the
mount. A direct `harbor run` with `AGENT_EVAL_PODMAN_PROJECT_DIR` is refused unless you set
`AGENT_EVAL_PODMAN_PROJECT_UNSAFE_ALLOW=1`, which is only for a directory that holds no
dataset or Git history of it.

All K8s resources are created programmatically via `agent_eval.harbor.k8s_resources`
(uses the Kubernetes Python client — works from a laptop via kubeconfig and in-cluster
via ServiceAccount). Resources are labeled `app.kubernetes.io/managed-by: agent-eval-harness`
for easy cleanup:

```python
from agent_eval.harbor.k8s_resources import cleanup
cleanup("<ns>")  # deletes all ConfigMaps + Secrets created by agent-eval-harness
```

Resource/scheduling knobs: `AGENT_EVAL_K8S_CPU`/`AGENT_EVAL_K8S_MEMORY` (defaults 1 / 2Gi),
`AGENT_EVAL_K8S_KEEP_RUN=1` (debugging).
