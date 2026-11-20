"""Podman BaseEnvironment for Harbor.

A compose-free Harbor environment that runs each trial in a single Podman
container via ``podman run``/``exec``/``cp``. Like the Kubernetes env it is a
generic exec/copy surface — Harbor drives the agent, oracle, and verifier,
preserving the agent zoo and ``reward.json`` contract.

Usage::

    harbor run -p <task> --agent claude-code -m <model> \\
      --environment-import-path agent_eval.harbor.podman:PodmanEnvironment

Only Linux containers are supported. The image is taken from
``[environment].docker_image`` (preferred) or built from ``environment/Dockerfile``.
"""

import asyncio
import re
import shlex
from pathlib import Path, PurePosixPath

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import (
    EnvironmentCapabilities,
    EnvironmentResourceCapabilities,
)
from harbor.models.task.config import TaskOS

import os
import sys

_PODMAN = os.environ.get("PODMAN_BINARY", "podman")

# Env vars forwarded into the Podman trial container. Includes provider config
# AND API keys — the container runs on the host machine (no security boundary).
# On K8s, API keys come from a Secret (AGENT_EVAL_K8S_CREDENTIALS_SECRET).
_FORWARD_ENV = (
    # Provider config
    "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION",
    "GOOGLE_CLOUD_PROJECT", "ANTHROPIC_MODEL", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "AWS_REGION",
    "OPENAI_BASE_URL", "OPENAI_MODEL",
    # API keys
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK",
    "OPENAI_API_KEY",
    # `openrouter:/` judges grade in-container through their dedicated client
    # (spec 014); its default key variable travels like the others.
    "OPENROUTER_API_KEY",
)

_HARBOR_LOG_TARGETS = {"/logs/verifier", "/logs/agent", "/logs/artifacts"}


def _plan_exclusions() -> set:
    """Host keys not to forward while an OpenRouter plan is active
    (``AGENT_EVAL_PODMAN_PLAN_EXCLUDE``, set by ``harbor/run.py`` — spec 014):
    the plan's env block reaches the agent through ``--agent-env``, and no host
    Vertex/Bedrock setting or Anthropic credential may enter the container.
    Empty (nothing excluded) without a plan."""
    raw = os.environ.get("AGENT_EVAL_PODMAN_PLAN_EXCLUDE", "")
    return {k.strip() for k in raw.split(",") if k.strip()}


def _plan_forward_names() -> tuple:
    """Extra host variables to forward while a plan is active
    (``AGENT_EVAL_PODMAN_PLAN_FORWARD``): the inference key under a custom
    ``api_key_env`` name, for the in-container ``openrouter:/`` judge."""
    raw = os.environ.get("AGENT_EVAL_PODMAN_PLAN_FORWARD", "")
    return tuple(k.strip() for k in raw.split(",") if k.strip())

# Where a mounted GCP credentials file lands inside the container.
_CONTAINER_CREDS = "/var/creds/creds.json"


def _container_name(session_id: str) -> str:
    """Derive a valid container name from the trial session id."""
    name = session_id.lower()
    name = re.sub(r"[^a-z0-9_.-]", "-", name)
    return f"aeh-{name}"[:120]


def _external_bind_args(mounts: list[dict]) -> list[str]:
    """Translate user mounts to Podman args, excluding copy-based log dirs."""
    args: list[str] = []
    for mount in mounts:
        target = mount.get("target", "")
        if target in _HARBOR_LOG_TARGETS:
            continue
        if mount.get("type") != "bind":
            raise ValueError(
                "PodmanEnvironment supports external bind mounts only; "
                f"got {mount.get('type')!r} for {target!r}")
        raw_source = mount.get("source")
        if not isinstance(raw_source, str) or not raw_source.strip():
            raise ValueError(f"Podman mount source is required for {target!r}")
        if not isinstance(target, str) or not target.strip():
            raise ValueError("Podman mount target is required")
        source = Path(raw_source).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"Podman mount source not found: {source}")
        if not Path(target).is_absolute():
            raise ValueError(f"Podman mount target must be absolute: {target}")
        if source == Path("/"):
            raise ValueError("Refusing to mount the host filesystem root")
        # normpath, not resolve(): the target is a container path, and
        # resolving it against the host filesystem would follow unrelated
        # host symlinks. Lexical normalization catches /data/../ spellings.
        if os.path.normpath(target) == "/":
            raise ValueError("Refusing to mount over the container filesystem root")
        # Harbor's ServiceVolumeConfig types read_only as
        # NotRequired[Literal[True]] (Compose semantics: omission means
        # writable; False is unrepresentable). The harness's safe read-only
        # default lives in the producer — run.py's _parse_bind_mount stamps
        # read_only: True unless :rw was explicitly requested.
        mode = "ro" if mount.get("read_only") else "rw"
        args += ["-v", f"{source}:{target}:{mode}"]
    return args


_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _environment_args(environment: dict[str, str]) -> list[str]:
    """Return value-free Podman environment arguments.

    Podman resolves ``-e NAME`` from its own process environment. Passing
    ``-e NAME=value`` would expose credentials in process listings while a
    trial is running. Keys must be valid POSIX names: Podman treats a
    trailing ``*`` in a value-free ``-e`` as a host-environment prefix glob
    (``-e '*'`` forwards the entire host environment into the container).
    """
    args: list[str] = []
    for key in environment:
        if not _ENV_KEY.fullmatch(key):
            raise ValueError(
                f"Refusing to forward invalid environment variable name {key!r}: "
                "Podman expands value-free names ending in '*' as a "
                "host-environment prefix glob")
        args += ["-e", key]
    return args


def _project_hide_args(project_mount: str) -> list[str]:
    """``--mount`` args masking ``AGENT_EVAL_PODMAN_PROJECT_HIDE`` under the project mount.

    The variable holds ``os.pathsep``-separated paths relative to the project
    directory (set by ``harbor.run``). Each is overlaid with an empty tmpfs.
    """
    args: list[str] = []
    for rel in filter(None, os.environ.get("AGENT_EVAL_PODMAN_PROJECT_HIDE", "").split(os.pathsep)):
        path = PurePosixPath(rel)
        if path.is_absolute() or ".." in path.parts or path.parts in ((), (".",)):
            raise ValueError(f"Refusing to mask invalid project path {rel!r}")
        target = PurePosixPath(project_mount, *path.parts)
        if "," in str(target):
            raise ValueError(f"Cannot mask project path containing a comma: {rel!r}")
        args += ["--mount", f"type=tmpfs,destination={target},ro=true"]
    return args


class PodmanEnvironment(BaseEnvironment):
    """Single-container Harbor environment backed by the Podman CLI."""

    def __init__(self, *args, keep_containers: bool | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._container = _container_name(self.session_id)
        # Keep the container after the trial for `podman logs`/`podman exec` debugging.
        if keep_containers is None:
            keep_containers = os.environ.get("AGENT_EVAL_PODMAN_KEEP_RUN") == "1"
        self._keep_containers = keep_containers
        self._started = False

    # --- identity / capabilities -------------------------------------------

    @staticmethod
    def type() -> str:
        return "podman"

    @property
    def capabilities(self) -> EnvironmentCapabilities:
        return EnvironmentCapabilities(
            gpus=False,
            tpus=False,
            disable_internet=True,   # podman run --network none
            network_allowlist=False,
            windows=False,
            mounted=False,           # we copy in/out, no host bind for logs
            docker_compose=False,
        )

    @classmethod
    def resource_capabilities(cls) -> EnvironmentResourceCapabilities:
        # podman run --cpus / --memory apply hard ceilings.
        return EnvironmentResourceCapabilities(
            cpu_limit=True, cpu_request=False,
            memory_limit=True, memory_request=False,
        )

    @classmethod
    def preflight(cls) -> None:
        import shutil
        if not shutil.which(_PODMAN):
            raise SystemExit(
                f"Podman ('{_PODMAN}') is not installed or not on PATH. "
                "Install Podman (and `podman machine start` on macOS) and retry."
            )

    def _validate_definition(self) -> None:
        if self.task_env_config.docker_image:
            return
        dockerfile = self.environment_dir / "Dockerfile"
        if not dockerfile.is_file():
            raise FileNotFoundError(
                f"Podman environment needs [environment].docker_image or "
                f"{dockerfile} — neither found."
            )

    # --- podman command helper ---------------------------------------------

    async def _podman(self, args: list[str], timeout_sec: int | None = None,
                      input_bytes: bytes | None = None,
                      environment: dict[str, str] | None = None) -> ExecResult:
        subprocess_environment = None
        if environment:
            subprocess_environment = os.environ.copy()
            subprocess_environment.update(environment)
        proc = await asyncio.create_subprocess_exec(
            _PODMAN, *args,
            stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=subprocess_environment,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=input_bytes), timeout=timeout_sec)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ExecResult(stdout="", stderr=f"podman timed out after {timeout_sec}s",
                              return_code=124)
        return ExecResult(
            stdout=(stdout or b"").decode(errors="replace"),
            stderr=(stderr or b"").decode(errors="replace"),
            return_code=proc.returncode,
        )

    # --- lifecycle ----------------------------------------------------------

    async def start(self, force_build: bool) -> None:
        if self.os == TaskOS.WINDOWS:
            raise RuntimeError("PodmanEnvironment supports Linux containers only.")

        image = self.task_env_config.docker_image
        if not image or force_build:
            dockerfile = self.environment_dir / "Dockerfile"
            if dockerfile.is_file():
                image = f"aeh-{_container_name(self.session_id)}:latest"
                build = await self._podman(
                    ["build", "-t", image, str(self.environment_dir)], timeout_sec=1800)
                if build.return_code != 0:
                    raise RuntimeError(f"podman build failed:\n{build.stderr}")
        if not image:
            raise RuntimeError("No image to run (set [environment].docker_image)")

        # Remove any stale container with the same name.
        await self._podman(["rm", "-f", self._container])

        run_args = ["run", "-d", "--name", self._container, "--entrypoint", "sleep"]
        if self._effective_cpus:
            run_args += ["--cpus", str(self._effective_cpus)]
        if self._effective_memory_mb:
            run_args += ["--memory", f"{self._effective_memory_mb}m"]
        if self._network_disabled:
            run_args += ["--network", "none"]

        # Harbor always supplies three internal log mounts. This environment
        # intentionally keeps those copy-based (capabilities.mounted=False),
        # while honoring any additional user bind mounts such as a large,
        # read-only dataset that should not be copied per trial.
        external_mount_args = _external_bind_args(self._mounts)
        if external_mount_args:
            # Host data under $HOME commonly carries user_home_t. A container
            # can stat such a bind mount but SELinux denies directory traversal.
            # Disabling labels for this local trial avoids mutating the user's
            # dataset labels recursively via :z/:Z. The requested ro/rw mode is
            # still enforced by Podman.
            print(f"WARNING: disabling SELinux label confinement for container "
                  f"{self._container} (external bind mounts present)",
                  file=sys.stderr)
            run_args += ["--security-opt", "label=disable"]
            run_args += external_mount_args

        # Forward provider configuration and credentials needed by agent CLIs
        # (minus what an active OpenRouter plan told us to keep out).
        exclude = _plan_exclusions()
        forwarded = {k: os.environ[k] for k in _FORWARD_ENV + _plan_forward_names()
                     if os.environ.get(k) and k not in exclude}

        # Credentials: only via an explicitly provided file, read-only mounted —
        # never the host's personal ADC. AGENT_EVAL_PODMAN_GCP_CREDENTIALS_FILE should point at a
        # service-account key (or a Workload-Identity credential config). Skipped
        # under a plan: a Vertex credential inside an OpenRouter run is noise.
        creds_file = None if exclude else os.environ.get("AGENT_EVAL_PODMAN_GCP_CREDENTIALS_FILE")
        if creds_file and Path(creds_file).is_file():
            run_args += ["-v", f"{Path(creds_file).resolve()}:{_CONTAINER_CREDS}:ro"]
            forwarded["GOOGLE_APPLICATION_CREDENTIALS"] = _CONTAINER_CREDS

        # Project resources from a host directory (bind-mount, read-only).
        # Podman equivalent of AGENT_EVAL_K8S_PROJECT_CONFIGMAP — with this, no
        # project-specific image is needed.
        project_dir = os.environ.get("AGENT_EVAL_PODMAN_PROJECT_DIR")
        if project_dir and Path(project_dir).is_dir():
            project_mount = os.environ.get("AGENT_EVAL_PODMAN_PROJECT_MOUNT", "/opt/project")
            run_args += ["-v", f"{Path(project_dir).resolve()}:{project_mount}:ro"]
            forwarded["AGENT_EVAL_PROJECT_DIR"] = project_mount
            # The eval dataset (annotations, reference answers) inside the
            # project dir is masked with an empty tmpfs so the agent under test
            # cannot read it, through Bash or otherwise.
            run_args += _project_hide_args(project_mount)

        merged_env = {**forwarded, **(self._persistent_env or {})}
        run_args += _environment_args(merged_env)
        self._persistent_env = merged_env

        run_args += [image, "infinity"]

        result = await self._podman(
            run_args, timeout_sec=300, environment=merged_env)
        if result.return_code != 0:
            raise RuntimeError(f"podman run failed:\n{result.stderr}")
        self._started = True

        # Upload task environment/ for prebuilt-image tasks (base helper).
        await self._upload_environment_dir_after_start()

    async def stop(self, delete: bool) -> None:
        if not self._started:
            return
        if self._keep_containers:
            # Leave it running for `podman logs`/`podman exec` debugging.
            self.logger.info(
                "Keeping container %s (AGENT_EVAL_PODMAN_KEEP_RUN). Inspect with: "
                "podman logs %s | podman exec -it %s /bin/sh",
                self._container, self._container, self._container)
            return
        await self._podman(["stop", "-t", "5", self._container], timeout_sec=60)
        if delete:
            await self._podman(["rm", "-f", self._container], timeout_sec=60)

    # --- exec ---------------------------------------------------------------

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        user = self._resolve_user(user)
        env = self._merge_env(env)

        args = ["exec"]
        effective_cwd = cwd or self.task_env_config.workdir
        if effective_cwd:
            args += ["-w", effective_cwd]
        if env:
            args += _environment_args(env)
        if user is not None:
            args += ["-u", str(user)]
        args.append(self._container)
        # Run through a shell so pipelines / redirections in `command` work,
        # matching BaseInstalledAgent._exec which prefixes `set -o pipefail`.
        args += ["/bin/sh", "-c", command]

        return await self._podman(
            args, timeout_sec=timeout_sec, environment=env)

    # --- file transfer (podman cp) -----------------------------------------

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        parent = str(Path(target_path).parent)
        if parent and parent != ".":
            await self.exec(f"mkdir -p {shlex.quote(parent)}", user="root")
        await self._cp(str(source_path), f"{self._container}:{target_path}")

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        # `podman cp <dir>/. <c>:<target>` copies contents into target.
        src = str(source_dir).rstrip("/")
        await self.exec(f"mkdir -p {shlex.quote(target_dir)}", user="root")
        await self._cp(f"{src}/.", f"{self._container}:{target_dir}")

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        Path(target_path).parent.mkdir(parents=True, exist_ok=True)
        await self._cp(f"{self._container}:{source_path}", str(target_path))

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        Path(target_dir).mkdir(parents=True, exist_ok=True)
        await self._cp(f"{self._container}:{source_dir.rstrip('/')}/.", str(target_dir))

    async def _cp(self, src: str, dst: str) -> None:
        result = await self._podman(["cp", src, dst], timeout_sec=300)
        if result.return_code != 0:
            raise RuntimeError(f"podman cp {src} -> {dst} failed:\n{result.stderr}")
