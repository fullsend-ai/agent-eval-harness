"""Tests for the Harbor Podman environment adapter."""

import asyncio
from types import SimpleNamespace

import pytest

# The Harbor framework is an optional dependency. Probe the exact submodule:
# an unrelated top-level ``harbor`` module must not make collection fail.
pytest.importorskip("harbor.environments.base")

from agent_eval.harbor.podman import (
    PodmanEnvironment,
    _environment_args,
    _external_bind_args,
)


def test_external_bind_args_ignores_harbor_log_mounts(tmp_path):
    args = _external_bind_args([
        {"type": "bind", "source": str(tmp_path), "target": "/logs/agent"},
        {"type": "bind", "source": str(tmp_path),
         "target": "/historical-payload-data", "read_only": True},
    ])
    assert args == [
        "-v", f"{tmp_path.resolve()}:/historical-payload-data:ro"]


def test_external_bind_args_supports_writable_mount(tmp_path):
    # Harbor's ServiceVolumeConfig cannot express read_only: False — absence
    # of the key is Compose's "writable" (see test_mount_pipeline_* below for
    # the composed CLI-to-podman contract).
    assert _external_bind_args([
        {"type": "bind", "source": str(tmp_path), "target": "/data"},
    ]) == ["-v", f"{tmp_path.resolve()}:/data:rw"]


def test_external_bind_args_honors_read_only(tmp_path):
    assert _external_bind_args([
        {"type": "bind", "source": str(tmp_path), "target": "/data",
         "read_only": True},
    ]) == ["-v", f"{tmp_path.resolve()}:/data:ro"]


def test_mount_pipeline_preserves_rw_and_ro_end_to_end(tmp_path):
    # Composition regression test: the dict emitted by run.py's CLI parser
    # must round-trip through _external_bind_args with the requested mode.
    from agent_eval.harbor.run import _parse_bind_mount
    rw = _parse_bind_mount(f"{tmp_path}:/data:rw")
    ro = _parse_bind_mount(f"{tmp_path}:/data:ro")
    default = _parse_bind_mount(f"{tmp_path}:/data")
    assert _external_bind_args([rw]) == ["-v", f"{tmp_path.resolve()}:/data:rw"]
    assert _external_bind_args([ro]) == ["-v", f"{tmp_path.resolve()}:/data:ro"]
    assert _external_bind_args([default]) == [
        "-v", f"{tmp_path.resolve()}:/data:ro"]


@pytest.mark.parametrize("mount,error", [
    ({"type": "volume", "source": "/x", "target": "/data"},
     "bind mounts only"),
    ({"type": "bind", "source": "", "target": "/data"},
     "source is required"),
    ({"type": "bind", "source": "/tmp", "target": ""},
     "target is required"),
    ({"type": "bind", "source": "/tmp", "target": "relative"},
     "target must be absolute"),
    ({"type": "bind", "source": "/", "target": "/data"},
     "host filesystem root"),
    ({"type": "bind", "source": "/tmp", "target": "/"},
     "container filesystem root"),
])
def test_external_bind_args_rejects_invalid_mounts(mount, error):
    with pytest.raises(ValueError, match=error):
        _external_bind_args([mount])


def test_external_bind_args_rejects_missing_source(tmp_path):
    with pytest.raises(FileNotFoundError, match="source not found"):
        _external_bind_args([{
            "type": "bind", "source": str(tmp_path / "missing"),
            "target": "/data",
        }])


def test_environment_args_do_not_expose_values():
    args = _environment_args({
        "OPENAI_API_KEY": "secret-value",
        "EVAL_SNAPSHOT_DIR": "/historical-payload-data",
    })

    assert args == ["-e", "OPENAI_API_KEY", "-e", "EVAL_SNAPSHOT_DIR"]
    assert "secret-value" not in " ".join(args)


@pytest.mark.parametrize("key", ["*", "AEH_*", "BAD-KEY", "1BAD", "A=B", ""])
def test_environment_args_reject_invalid_names(key):
    # Podman expands a value-free `-e NAME*` as a host-environment prefix
    # glob (`-e '*'` forwards the entire host environment), so non-POSIX
    # names must be rejected at the sink.
    with pytest.raises(ValueError, match="invalid environment variable name"):
        _environment_args({key: "x"})


def _startable_environment(tmp_path, mounts):
    return SimpleNamespace(
        os=None,
        task_env_config=SimpleNamespace(docker_image="example/eval:latest"),
        environment_dir=tmp_path,
        _container="aeh-test",
        _effective_cpus=2,
        _effective_memory_mb=512,
        _network_disabled=True,
        _mounts=mounts,
        _persistent_env={"TASK_FLAG": "enabled"},
        _started=False,
    )


def test_start_wires_mount_resources_and_value_free_env(
        tmp_path, monkeypatch):
    env = _startable_environment(tmp_path, [{
        "type": "bind", "source": str(tmp_path), "target": "/data",
        "read_only": True,
    }])
    calls = []
    uploaded = []

    async def fake_podman(args, timeout_sec=None, input_bytes=None,
                          environment=None):
        calls.append((args, timeout_sec, environment))
        return SimpleNamespace(return_code=0, stderr="")

    async def fake_upload():
        uploaded.append(True)

    env._podman = fake_podman
    env._upload_environment_dir_after_start = fake_upload
    monkeypatch.setenv("OPENAI_API_KEY", "secret-value")

    asyncio.run(PodmanEnvironment.start(env, force_build=False))

    run_args, timeout, child_env = calls[-1]
    assert timeout == 300
    assert ["--cpus", "2"] == run_args[
        run_args.index("--cpus"):run_args.index("--cpus") + 2]
    assert ["--memory", "512m"] == run_args[
        run_args.index("--memory"):run_args.index("--memory") + 2]
    assert ["--network", "none"] == run_args[
        run_args.index("--network"):run_args.index("--network") + 2]
    assert ["--security-opt", "label=disable"] == run_args[
        run_args.index("--security-opt"):run_args.index("--security-opt") + 2]
    assert f"{tmp_path.resolve()}:/data:ro" in run_args
    assert ["-e", "OPENAI_API_KEY"] == run_args[
        run_args.index("OPENAI_API_KEY") - 1:run_args.index("OPENAI_API_KEY") + 1]
    assert "secret-value" not in " ".join(run_args)
    assert child_env["OPENAI_API_KEY"] == "secret-value"
    assert child_env["TASK_FLAG"] == "enabled"
    assert uploaded == [True]
    assert env._started is True


def test_start_does_not_disable_labels_for_internal_log_mounts(tmp_path):
    env = _startable_environment(tmp_path, [{
        "type": "bind", "source": str(tmp_path), "target": "/logs/agent",
    }])
    calls = []

    async def fake_podman(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(return_code=0, stderr="")

    async def fake_upload():
        return None

    env._podman = fake_podman
    env._upload_environment_dir_after_start = fake_upload
    asyncio.run(PodmanEnvironment.start(env, force_build=False))

    assert "--security-opt" not in calls[-1]


def test_exec_passes_scoped_environment_to_podman_process():
    env = SimpleNamespace(
        _started=True,
        _container="aeh-test",
        task_env_config=SimpleNamespace(workdir="/workspace"),
        _resolve_user=lambda user: user,
        _merge_env=lambda extra: {"CARRIER": "secret", **(extra or {})},
    )
    calls = []

    async def fake_podman(args, timeout_sec=None, environment=None):
        calls.append((args, timeout_sec, environment))
        return SimpleNamespace(return_code=0, stdout="", stderr="")

    env._podman = fake_podman
    asyncio.run(PodmanEnvironment.exec(
        env, "env", env={"VISIBLE": "yes"}, timeout_sec=7))

    args, timeout, child_env = calls[0]
    assert timeout == 7
    assert child_env == {"CARRIER": "secret", "VISIBLE": "yes"}
    assert "secret" not in " ".join(args)
    assert ["-e", "CARRIER", "-e", "VISIBLE"] == args[3:7]


def test_podman_forward_excludes_under_plan(tmp_path, monkeypatch):
    """With the plan's exclusion list set by harbor/run.py, no host Vertex
    setting or Anthropic credential is forwarded and the GCP credential mount
    is skipped; an allowed key (the verifier's OPENROUTER_API_KEY) still is."""
    env = _startable_environment(tmp_path, [])
    calls = []

    async def fake_podman(args, timeout_sec=None, input_bytes=None, environment=None):
        calls.append((args, environment))
        return SimpleNamespace(return_code=0, stderr="")

    async def fake_upload():
        return None

    env._podman = fake_podman
    env._upload_environment_dir_after_start = fake_upload
    creds = tmp_path / "sa.json"
    creds.write_text("{}")
    for k, v in {"CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_VERTEX_PROJECT_ID": "p", "CLOUD_ML_REGION": "r",
                 "ANTHROPIC_API_KEY": "sk-ant", "ANTHROPIC_AUTH_TOKEN": "tok", "ANTHROPIC_BASE_URL": "https://x",
                 "OPENROUTER_API_KEY": "sk-or", "AGENT_EVAL_PODMAN_GCP_CREDENTIALS_FILE": str(creds),
                 "AGENT_EVAL_PODMAN_PLAN_EXCLUDE": "CLAUDE_CODE_USE_VERTEX,ANTHROPIC_VERTEX_PROJECT_ID,"
                 "CLOUD_ML_REGION,ANTHROPIC_API_KEY,ANTHROPIC_AUTH_TOKEN,ANTHROPIC_BASE_URL"}.items():
        monkeypatch.setenv(k, v)
    asyncio.run(PodmanEnvironment.start(env, force_build=False))
    run_args, child_env = calls[-1]
    forwarded = {run_args[i + 1] for i, a in enumerate(run_args) if a == "-e"}
    assert "OPENROUTER_API_KEY" in forwarded and "TASK_FLAG" in forwarded
    assert "MY_OR_KEY" not in forwarded                                    # not on the static list
    assert not forwarded & {"CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION",
                            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                            "GOOGLE_APPLICATION_CREDENTIALS"}
    assert not any(str(creds) in a for a in run_args)
    # a custom api_key_env is forwarded by name when run.py asks for it
    monkeypatch.setenv("MY_OR_KEY", "sk-or-custom")
    monkeypatch.setenv("AGENT_EVAL_PODMAN_PLAN_FORWARD", "MY_OR_KEY")
    asyncio.run(PodmanEnvironment.start(env, force_build=False))
    run_args, child_env = calls[-1]
    assert "MY_OR_KEY" in run_args and child_env["MY_OR_KEY"] == "sk-or-custom"
    monkeypatch.delenv("AGENT_EVAL_PODMAN_PLAN_EXCLUDE")
    monkeypatch.delenv("AGENT_EVAL_PODMAN_PLAN_FORWARD")
    asyncio.run(PodmanEnvironment.start(env, force_build=False))
    run_args, _ = calls[-1]
    assert "CLAUDE_CODE_USE_VERTEX" in run_args and "GOOGLE_APPLICATION_CREDENTIALS" in run_args



def test_start_masks_dataset_inside_project_mount(tmp_path, monkeypatch):
    # The project dir is bind-mounted read-only; the dataset in it (answer keys)
    # is overlaid with an empty tmpfs so the agent cannot read it, even via Bash.
    project = tmp_path / "project"
    project.mkdir()
    env = _startable_environment(tmp_path, [])
    calls = []

    async def fake_podman(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(return_code=0, stderr="")

    async def fake_upload():
        return None

    env._podman = fake_podman
    env._upload_environment_dir_after_start = fake_upload
    monkeypatch.setenv("AGENT_EVAL_PODMAN_PROJECT_DIR", str(project))
    monkeypatch.setenv("AGENT_EVAL_PODMAN_PROJECT_HIDE", "evals/cases")
    asyncio.run(PodmanEnvironment.start(env, force_build=False))

    run_args = calls[-1]
    assert f"{project.resolve()}:/opt/project:ro" in run_args
    mount = run_args.index("--mount")
    assert run_args[mount + 1] == (
        "type=tmpfs,destination=/opt/project/evals/cases,ro=true")
    assert run_args.index(f"{project.resolve()}:/opt/project:ro") < mount
