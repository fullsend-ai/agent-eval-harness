# Running on Harbor (containers)

Run the same `eval.yaml` unchanged in containers. [Harbor](https://github.com/laude-institute/harbor)
is the execution substrate (Podman locally, Kubernetes/OpenShift on a cluster) and the
agent zoo; agent-eval-harness stays the authoring + judgment layer on top. The execution
substrate is a CLI flag, never part of the eval config.

!!! abstract "What Harbor buys you"
    Sandboxed, isolated per-case containers, cluster-scale concurrency, and Harbor's stock
    agents (`claude-code`, `opencode`, `codex`, …) — while keeping your judges, thresholds,
    MLflow logging, and the same `runs/<id>/` report layout as a local run.

## The eval flow on Kubernetes

Each test case runs in its **own trial pod**, in parallel. The harness drives every pod
through the same exec back-and-forth — launch, upload, run, verify, collect — over the
Kubernetes API. Step through it below, or press **Simulate**:

<div class="harbor-diagram" aria-label="Interactive diagram of the Harbor eval flow on Kubernetes: an orchestrator driving parallel trial pods through launch, setup, run, test, collect, and score."></div>

!!! note "This is the `--env kubernetes` / `openshift` path"
    Podman (`--env podman`) follows the same lifecycle with one local container per case
    instead of a pod. Pods are created from a prebuilt image via `KubernetesEnvironment`;
    all file transfer and command execution happen as tar + base64 streamed over the
    Kubernetes exec websocket.

## How it fits together

Harbor runs **self-contained task packages**. `/eval-dataset` emits one package per case;
each carries everything Harbor needs, so no custom agent wrapper is required.

```mermaid
flowchart LR
    A[eval.yaml + dataset] -->|/eval-dataset --harbor| B[task packages]
    B -->|/eval-run --runner harbor| C{--env}
    C -->|podman| D[local container]
    C -->|kubernetes / openshift| E[trial pods]
    D --> F[verifier → reward.json]
    E --> F
    F -->|parse + map| G["runs/&lt;id&gt;/<br/>summary.yaml · report.html"]
```

A task package looks like this:

```text
eval/harbor-tasks/case-001-.../
├── task.toml                 # runtime image ref + timeouts
├── instruction.md            # resolved skill command + input context (agent prompt)
├── tests/
│   ├── test.sh               # verifier: runs the reward bridge → reward.json
│   └── eval.yaml             # bundled judge config
└── environment/              # auto-uploaded to /workspace by Harbor
    ├── input.yaml            # case input
    ├── tool_handlers.yaml    # resolved tool interception handlers
    ├── hooks/tools.py        # interceptor script
    └── .claude/settings.json # PreToolUse hooks (Claude Code)
```

!!! note "Per-case vs suite-level judging"
    Per-case judging happens **in-container** — the reward bridge runs as the Harbor
    verifier and writes `reward.json` (boolean judges gate; numeric LLM judges average).
    Pairwise comparison and [regression thresholds](../concepts/thresholds.md) stay
    **suite-level** on top of Harbor, applied when results are mapped back.

## Quick start (orchestrated run)

`/eval-run --runner harbor` wraps the whole flow: reuse (or generate) task packages,
call `harbor run`, parse the job dir, generate the report, and check regressions.

=== "Kubernetes / OpenShift (default)"

    ```bash
    /eval-run --runner harbor --model <model> -n 10
    ```

=== "Podman (local)"

    ```bash
    /eval-run --runner harbor --env podman --model <model>
    ```

| Flag | Effect |
| --- | --- |
| `--runner harbor` | Use the Harbor execution path instead of local subprocess |
| `--env <name>` | Execution environment: `kubernetes` (default), `podman`, `openshift`, `k8s` |
| `--model <name>` | Model passed to the Harbor agent (`harbor run -m`) |
| `-n <n>` | Parallelism — concurrent trial pods/containers |
| `--mount <source:target>` (optional `:ro` or `:rw` suffix) | Repeatable Podman bind mount; read-only by default |
| `--cpus <n>` / `--memory-mb <MiB>` | Hard per-environment resource limits |
| `--no-llm-judges` | Generate and run deterministic judges only; requires `--image` and an empty tasks directory or `--regenerate` |

The output is the same `eval/runs/<id>/` layout as a local run (`summary.yaml`,
`report.html`, per-case artifacts), so `/eval-review` and `/eval-mlflow` work unchanged.

!!! tip "eval.yaml portability"
    `eval.yaml` describes **what** to evaluate (agent, dataset, judges, thresholds) — not
    **where** it runs. The same file works locally, on Podman, on Kubernetes, and on
    [EvalHub](evalhub.md). See [Backends](../concepts/backends.md). Exception:
    `runner.type: cursor` is local-only — Harbor has no cursor agent.

!!! warning "Mounts are local-Podman only"
    Host bind mounts are implemented by the bundled Podman environment. Supplying
    `--mount` with Kubernetes/OpenShift or an unrelated custom environment is rejected
    so mounted data is never silently absent from an eval.

## Generating task packages

Task packages are generated once from your `eval.yaml` and dataset:

```bash
/eval-dataset --harbor --image <registry>/<project>-eval:latest
```

`/eval-run --runner harbor` reuses pre-generated packages when `--tasks-dir` already
contains them, and only regenerates when you pass `--image` (one-shot convenience) or
`--regenerate`.

## Podman (local)

The Podman environment runs each trial in a single container via the `podman` CLI. Build
the base image once, then run:

```bash
# 1. Build the base image (once)
podman build -f deploy/Containerfile -t localhost/agent-eval-harness:latest .

# 2. Run — stock Harbor agent, our Podman environment
PYTHONPATH="$(pwd)" harbor run -p eval/harbor-tasks \
    --agent claude-code -m <model> \
    --environment-import-path agent_eval.harbor.podman:PodmanEnvironment \
    -n 1 -o eval/harbor-jobs
```

The harness wrapper can run the same task packages with the native Harbor Codex agent
and a read-only data mount:

```bash
python -m agent_eval.harbor.run \
  --config eval.yaml --model gpt-5.6-luna --agent codex --env podman \
  --tasks-dir eval/harbor-tasks --jobs-dir eval/harbor-jobs \
  --output eval/runs/codex-luna \
  --mount "$HOME/git/historical-payload-data:/historical-payload-data:ro" \
  --cpus 2 --memory-mb 4096
```

!!! warning "Credentials are forwarded from the host"
    The container runs on your machine (no security boundary), so provider config **and**
    API keys are copied from the host env into the container automatically —
    `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`,
    `AWS_ACCESS_KEY_ID`, and friends. Nothing to configure. Vertex AI needs a credentials
    file, so set `AGENT_EVAL_PODMAN_GCP_CREDENTIALS_FILE=/path/to/sa-key.json` (mounted
    read-only).

    The Harbor subprocess receives requested agent variables through its environment;
    plaintext secret values are not placed in its command line. They still share the
    host user's `/proc` visibility, and the local container deliberately runs with the
    invoking UID, so this is resource isolation rather than credential isolation.

    Under an `openrouter:/` skill model the plan's env block **narrows** this forwarding:
    the Anthropic credentials (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
    `ANTHROPIC_BASE_URL`), the Vertex variables (`CLAUDE_CODE_USE_VERTEX`,
    `ANTHROPIC_VERTEX_PROJECT_ID`, `CLOUD_ML_REGION`, `GOOGLE_CLOUD_PROJECT`) and the
    Bedrock switches (`CLAUDE_CODE_USE_BEDROCK`, `AWS_REGION`, `AWS_BEARER_TOKEN_BEDROCK`)
    are not forwarded, and the GCP credentials file is not mounted. The AWS access-key
    variables (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`) and the
    `OPENAI_*` variables are still forwarded when set — unset them on the host for
    isolation. Export `OPENROUTER_API_KEY` on the host (and `OPENROUTER_MANAGEMENT_KEY` at
    `enforcement: key-guardrail`): the inference key reaches the agent as
    `ANTHROPIC_AUTH_TOKEN` through value-free `--agent-env` carriers, and
    `OPENROUTER_API_KEY` itself enters the container only when an in-container
    `openrouter:/` judge needs it. See
    [Model providers → Credentials by backend](../concepts/providers.md#credentials-by-backend).

!!! warning "SELinux and external mounts"
    The Podman adapter disables SELinux labeling when external bind mounts are present
    because relabeling a large shared dataset can be destructive. Use only a dedicated,
    pre-labeled data directory on SELinux-enforcing hosts; do not mount broad home or
    system directories.

Project resources (skills, scripts, `.context`, `CLAUDE.md`) can be **bind-mounted** from
a host directory instead of baked into an image:

```bash
AGENT_EVAL_PODMAN_PROJECT_DIR=. harbor run -p eval/harbor-tasks --agent claude-code ...
```

| Variable | Description |
| --- | --- |
| `AGENT_EVAL_PODMAN_PROJECT_DIR` | Bind-mount project resources (no project image needed) |
| `AGENT_EVAL_PODMAN_GCP_CREDENTIALS_FILE` | GCP service-account key (read-only mount) |
| `AGENT_EVAL_PODMAN_KEEP_RUN` | `1` keeps the container after the trial for debugging |

!!! note "PYTHONPATH"
    `PYTHONPATH` must include this repo so Harbor can import the environment plug-in —
    unnecessary if agent-eval-harness is pip-installed.

## Kubernetes / OpenShift

The Kubernetes `BaseEnvironment` runs the same task images as pods, using the **Kubernetes
Python client** (not the `oc` CLI). It calls `load_incluster_config()` when running inside
a pod (using the pod's ServiceAccount) and falls back to your local kubeconfig otherwise.
Install the extras with `/eval-setup --harbor` or `pip install agent-eval-harness[harbor]`.

```bash
PYTHONPATH="$(pwd)" harbor run -p eval/harbor-tasks \
    --agent claude-code -m <model> \
    --environment-import-path agent_eval.harbor.kubernetes:KubernetesEnvironment \
    -n 5 -o eval/harbor-jobs
```

!!! warning "restricted-v2 SCC — the image must be group-0 writable"
    Pods run non-root under the `restricted-v2` SCC with an arbitrary assigned UID. The
    task image must be **group-0 writable** or the agent can't write to its workspace.
    Also set `AGENT_EVAL_K8S_INSTALL_PACKAGES` only if you need the agent install step —
    it defaults to skipping it, assuming a **prebuilt image**.

### Credentials (from the cluster, never copied from the host)

Unlike Podman, nothing is forwarded from your host. Credentials come from the cluster:

=== "API keys / Bedrock (Secret)"

    Env vars injected via `envFrom`:

    ```python
    from agent_eval.harbor.k8s_resources import create_env_secret
    create_env_secret({"ANTHROPIC_API_KEY": "sk-..."}, "model-keys", "<ns>")
    # then: AGENT_EVAL_K8S_CREDENTIALS_SECRET=model-keys
    ```

=== "OpenRouter (Secret key → secretKeyRef)"

    When `models.skill` is `openrouter:/…` the credentials Secret holds the OpenRouter
    key; the harness maps it to `ANTHROPIC_AUTH_TOKEN` with an explicit `secretKeyRef`
    (winning over `envFrom`) and writes the plan's non-secret env into the pod spec:

    ```python
    from agent_eval.harbor.k8s_resources import create_env_secret
    create_env_secret({"OPENROUTER_API_KEY": "sk-or-..."}, "model-keys", "<ns>")
    # then: AGENT_EVAL_K8S_CREDENTIALS_SECRET=model-keys; at `audit` also export OPENROUTER_API_KEY on the host
    ```

    At `enforcement: key-guardrail` the harness creates the per-run Secret
    `agent-eval-<run_id>-openrouter` before `harbor run` and deletes it with the key —
    the harness's identity needs `create`, `get` and `delete` on `secrets` in the
    namespace, and the host needs `OPENROUTER_MANAGEMENT_KEY` (the credentials Secret is
    then needed only for an in-container `openrouter:/` judge).

    Under a plan the trial container carries **no Anthropic credentials**: the plan's
    `ANTHROPIC_API_KEY=""` and blanked Vertex entries are explicit pod env and win over
    the credentials Secret (on Podman the same variables are stripped), so an
    in-container LLM judge must be `openrouter:/` or `openai:/` (or deterministic) — a
    Claude judge next to an OpenRouter agent works only on Local and EvalHub. The
    per-backend credential matrix is in
    [Model providers → Credentials by backend](../concepts/providers.md#credentials-by-backend);
    the Kubernetes specifics are in the [OpenRouter guide](openrouter.md#backends).

=== "Vertex AI (Secret file)"

    Service-account key mounted as a file:

    ```python
    from agent_eval.harbor.k8s_resources import create_creds_secret
    create_creds_secret("/path/to/sa-key.json", "vertex-creds", "<ns>")
    # then: AGENT_EVAL_K8S_GCP_CREDENTIALS_SECRET=vertex-creds
    ```

    Also put `CLAUDE_CODE_USE_VERTEX=1`, `ANTHROPIC_VERTEX_PROJECT_ID` and
    `CLOUD_ML_REGION` in the credentials Secret (`AGENT_EVAL_K8S_CREDENTIALS_SECRET`,
    `envFrom`) — nothing but `ANTHROPIC_MODEL` / `ANTHROPIC_BASE_URL` is inherited from
    your shell on Kubernetes, so without them Claude Code (and an in-container Anthropic
    judge) falls back to the direct API with no key.

=== "Vertex AI (Workload Identity)"

    No stored key — the pod runs as an SA federated to GCP (preferred):

    ```bash
    AGENT_EVAL_K8S_SERVICE_ACCOUNT=<sa>
    ```

    The same three Vertex variables (`CLAUDE_CODE_USE_VERTEX=1`,
    `ANTHROPIC_VERTEX_PROJECT_ID`, `CLOUD_ML_REGION`) still go in the credentials
    Secret (`AGENT_EVAL_K8S_CREDENTIALS_SECRET`); the service account only replaces the
    key file.

### Project resources via ConfigMap

Deliver project resources from a **ConfigMap** rather than baking a project-specific image —
the generic base image plus a ConfigMap covers any project:

```python
from agent_eval.harbor.k8s_resources import create_project_configmap
create_project_configmap("/path/to/project", "my-project", "<ns>")
# then: AGENT_EVAL_K8S_PROJECT_CONFIGMAP=my-project
```

The mount is read-only (`defaultMode 0755` so scripts stay executable); the agent copies
what it needs into `/workspace` at runtime.

!!! tip "Large project resources"
    A ConfigMap must stay under ~1 MB. If your `.context/` is too large, build a
    project-specific image instead: `FROM <base>` + `COPY project/` (see
    [Container images](../reference/container-images.md)).

### Environment variables

| Variable | Description |
| --- | --- |
| `AGENT_EVAL_K8S_NAMESPACE` | Target namespace |
| `AGENT_EVAL_K8S_CREDENTIALS_SECRET` | Secret with API keys (injected via `envFrom`; under an OpenRouter plan also `OPENROUTER_API_KEY`, mapped to `ANTHROPIC_AUTH_TOKEN` via `secretKeyRef`) |
| `AGENT_EVAL_K8S_GCP_CREDENTIALS_SECRET` | Secret with GCP SA key (file mount); the Vertex variables themselves go in the credentials Secret |
| `AGENT_EVAL_K8S_SERVICE_ACCOUNT` | Pod ServiceAccount (Workload Identity) |
| `AGENT_EVAL_K8S_PROJECT_CONFIGMAP` | ConfigMap with project resources (< 1 MB) |
| `AGENT_EVAL_K8S_INSTALL_PACKAGES` | `1` to run the agent install (default: skip for prebuilt images) |
| `AGENT_EVAL_K8S_KEEP_RUN` | `1` keeps the pod after the trial for debugging |
| `AGENT_EVAL_K8S_CPU` / `AGENT_EVAL_K8S_MEMORY` | Resource requests (default: `1` / `2Gi`) |

Cluster config can also live in a `.env` file at the project root — `/eval-run --runner
harbor` loads it automatically:

```bash title=".env"
AGENT_EVAL_K8S_NAMESPACE=<namespace>
AGENT_EVAL_K8S_CREDENTIALS_SECRET=<secret-name>
```

### Cleanup

All K8s resources are labeled `app.kubernetes.io/managed-by: agent-eval-harness`:

```python
from agent_eval.harbor.k8s_resources import cleanup
cleanup("<ns>")  # deletes all ConfigMaps + Secrets created by agent-eval-harness
```

## Debugging a run

By default the environment deletes its container/pod after each trial. Keep it alive for
inspection:

=== "Podman"

    ```bash
    AGENT_EVAL_PODMAN_KEEP_RUN=1 ...
    # podman logs <name> ; podman exec -it <name> /bin/sh
    ```

=== "OpenShift"

    ```bash
    AGENT_EVAL_K8S_KEEP_RUN=1 ...
    # oc logs <pod> -n <ns> ; oc exec -it <pod> -n <ns> -- /bin/sh
    ```

The kept container/pod name is logged at the end of the run.

!!! warning "Kept resources accumulate"
    Each trial uses a unique name, so kept containers/pods pile up. Prune afterwards with
    `podman rm -f <name>` or
    `oc delete pod -l app.kubernetes.io/managed-by=agent-eval-harness -n <ns>`.

Even without keeping the container, Harbor captures the transcripts and verifier output
into the job dir before deletion:

| File | Contents |
| --- | --- |
| `agent/claude-code.txt` | Agent transcript (stream-json) |
| `agent/trajectory.json` | ATIF trajectory |
| `verifier/reward.json` | Per-case scalar reward |
| `verifier/judges.json` | Per-judge results |
| `verifier/test-stdout.txt` | Verifier stdout |

## Suite-level regression check

After mapping the Harbor job back into the run dir, `/eval-run --runner harbor` runs the
**same** regression detection as a local run — comparing per-judge means and pass rates
against your [`thresholds`](../reference/config/thresholds.md). If any threshold is
violated, the command **exits non-zero** (useful in [CI](ci.md)):

```text
REGRESSIONS: 1 detected
  [completeness] min_mean: 3.5 -> 3.1
```

Harbor infra errors (transient pod/exec failures with no reward) and trial errors (pods
that never became Ready) are surfaced separately — infra errors are excluded from judge
means rather than scored `0`.

## Where to go next

<div class="grid cards" markdown>

-   :material-layers: **Backends & execution paths**

    ---

    How local, Harbor, and EvalHub relate — and why one `eval.yaml` covers all three.

    [:octicons-arrow-right-24: Backends](../concepts/backends.md)

-   :material-server-network: **Run on the platform**

    ---

    In-process, platform-triggered runs via the EvalHub adapter.

    [:octicons-arrow-right-24: EvalHub](evalhub.md)

-   :material-package-variant: **Container images**

    ---

    The base and provider images, and how project resources get delivered.

    [:octicons-arrow-right-24: Container images](../reference/container-images.md)

</div>
