# Deployment Architecture

agent-eval-harness is an evaluation harness that handles execution (local +
EvalHub), judgment (LLM judges, pairwise, regression), and authoring
(`/eval-analyze`, `/eval-dataset`).
[Harbor](https://github.com/laude-institute/harbor) is an optional execution layer for containerized runs, adding sandboxed isolation and the agent zoo (claude-code, opencode, codex, etc.).

```
┌────────────────────────────────────────────────────────────────────────┐
│  Entry points                                                          │
│                                                                        │
│  /eval-run                 harbor run          EvalHub / TrustyAI      │
│  agent-eval run            (CLI, CI)         (platform, control plane) │
│  (skill, interactive)                                                  │
└──────────┬───────────────────────┬───────── ─────────────┬─────────────┘
           │                       │                       │
           ▼                       ▼                       ▼
┌────────────────────┐ ┌────────────────────┐ ┌──────────────────────────┐
│ Local execution    │ │ Harbor execution   │ │ EvalHub server           │
│                    │ │                    │ │ (job orchestrator)       │
│ ClaudeCodeRunner   │ │ run.py → harbor    │ │                          │
│ or CLIRunner       │ │ → trial pods       │ │ Creates K8s Jobs         │
│ (subprocess)       │ │ → parse results    │ │ for each benchmark       │
│                    │ │ → report           │ │                          │
│ → summary.yaml     │ │ → summary.yaml     │ │                          │
│ → report.html      │ │ → report.html      │ │                          │
└────────────────────┘ └────────┬───────────┘ └─────────────┬────────────┘
                                │                           │
                                │                           │
                       ┌────────┴────────┐                  │
                       ▼                 ▼                  ▼
┌──────────────────────────┐ ┌──────────────────────────────────────────┐
│  Local containers        │ │  Kubernetes / OpenShift                  │
│  (Podman)                │ │                                          │
│                          │ │  ┌──────────────┐  ┌──────────────────┐  │
│  ┌──────────────────┐    │ │  │ Harbor trial │  │ EvalHub Job pod  │  │
│  │  Harbor trial    │    │ │  │ pod (N)      │  │ (agent-eval-hub) │  │
│  │  container       │    │ │  │              │  │                  │  │
│  │                  │    │ │  │ Base image   │  │ Adapter          │  │
│  │  Base image      │    │ │  │ + project    │  │ IN-PROCESS       │  │
│  │  + project       │    │ │  │  (ConfigMap) │  │ ClaudeCodeRunner │  │
│  │   (bind-mount)   │    │ │  │              │  │ (no sub-pods,    │  │
│  │                  │    │ │  │ Agent runs   │  │  no Harbor)      │  │
│  │  Agent runs      │    │ │  │ Verifier     │  │                  │  │
│  │  Verifier        │    │ │  │ → reward.json│  │ + sidecar        │  │
│  │  → reward.json   │    │ │  │              │  │  (→ server)      │  │
│  └──────────────────┘    │ │  └──────────────┘  └──────────────────┘  │
│                          │ │        ▲                    ▲            │
│  created by Harbor       │ │  Harbor via            EvalHub server    │
│  (via PodmanEnvironment) │ │  KubernetesEnv        (Job lifecycle)    │
└──────────────────────────┘ └──────────────────────────────────────────┘
```

## Container images

| Image | Containerfile | Contents | Used by |
|---|---|---|---|
| **agent-eval-harness** | `deploy/Containerfile` | UBI9 + python + node + Claude Code + Codex + agent-eval-harness (judge engine, reward bridge, interception). No project code. | Harbor trial pods, EvalHub Job pods (base) |
| **agent-eval-hub** | `deploy/evalhub/Containerfile` | FROM agent-eval-harness + eval-hub-sdk + boto3 + mlflow. No Harbor/kubernetes deps (adapter runs in-process). | EvalHub provider pod |

No project-specific images needed. Project resources are delivered to trial pods via:
- **Kubernetes:** ConfigMap volume (`AGENT_EVAL_K8S_PROJECT_CONFIGMAP`)
- **Podman:** host bind-mount (`AGENT_EVAL_PODMAN_PROJECT_DIR`)
- **Image layer:** `FROM agent-eval-harness` + `COPY project/` (Containerfile in the project's repo)

## eval.yaml is portable

`eval.yaml` describes **what** to evaluate (agent type, dataset, judges, thresholds) —
not **where** or **how** to run it. The same `eval.yaml` works unchanged across:

| Path | Invocation |
|---|---|
| Local | `/eval-run --model opus` or `agent-eval run --config eval.yaml` |
| Harbor (Podman) | `harbor run -p <tasks> --agent claude-code -m opus --environment-import-path agent_eval.harbor.podman:PodmanEnvironment` |
| Harbor (K8s) | `harbor run -p <tasks> --agent claude-code -m opus --environment-import-path agent_eval.harbor.kubernetes:KubernetesEnvironment` |
| EvalHub | Platform-triggered (adapter runs in-process inside the Job pod) |

The execution substrate is a CLI flag or env var, never in the eval config.

The exceptions are `runner.type: cursor` and `runner.type: fullsend`, which are
**local-only**: the base image ships no `cursor-agent` CLI, so Harbor has no cursor
agent and EvalHub rejects a cursor config at load; fullsend has no Harbor or
EvalHub equivalent. Use `claude-code` or `codex` for Harbor/EvalHub.

## Task packages (Harbor path)

`/eval-dataset` generates self-contained Harbor task packages (via `agent_eval.harbor.tasks`):

```
<case-id>/
  task.toml                     # image ref + timeouts
  instruction.md                # resolved skill command + input context
  tests/
    test.sh                     # verifier: runs reward.py → reward.json
    eval.yaml                   # bundled judges config
  environment/                  # auto-uploaded to workspace by Harbor
    input.yaml                  # case input
    tool_handlers.yaml          # resolved tool interception handlers
    hooks/tools.py              # interceptor script
    .claude/settings.json       # PreToolUse hooks (Claude Code)
```

Tasks are self-contained — any Harbor agent runs them directly. No custom agent
wrapper; Harbor's stock agents (claude-code, opencode, codex, etc.) work as-is.

## What each layer owns

| Layer | Owns | Does NOT own |
|---|---|---|
| **eval.yaml** | Agent type, dataset, judges, thresholds, models, MLflow config | Runner, environment, image, credentials |
| **Task packages** | Per-case instruction, inputs, tool interception, verifier | Agent installation, environment lifecycle |
| **agent-eval-harness** | Execution (local + EvalHub), task generation, judgment, reporting, regression | Container substrate, agent zoo |
| **Harbor** | Containerized trial orchestration, agent zoo, concurrency, trajectory | Judgment, reporting, regression detection |
| **Environments** (podman.py, kubernetes.py) | Container/pod lifecycle, exec, file transfer, credentials | Agent behavior, grading |
| **EvalHub** | Job governance, scheduling, MLflow persistence, OCI export | Execution, judgment |

## Subdirectories

- [`harbor/`](harbor/README.md) — Harbor integration details (environments, credentials,
  ConfigMaps, debugging)
- [`evalhub/`](evalhub/README.md) — EvalHub provider (adapter, manifests, smoke tests)
