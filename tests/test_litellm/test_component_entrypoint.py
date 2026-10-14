"""Unit tests for `docker/component_entrypoint.sh` and its wiring into the
componentized `gateway` / `backend` images."""

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPONENT_ENTRYPOINT = REPO_ROOT / "docker" / "component_entrypoint.sh"
PROD_ENTRYPOINT = REPO_ROOT / "docker" / "prod_entrypoint.sh"
GATEWAY_DOCKERFILE = REPO_ROOT / "gateway" / "Dockerfile"
BACKEND_DOCKERFILE = REPO_ROOT / "backend" / "Dockerfile"

IMAGE_ENTRYPOINT_PATH = "/app/docker/component_entrypoint.sh"

TRUTHY_USE_DDTRACE = ("true", "True", "TRUE", "tRuE")
FALSY_USE_DDTRACE = (None, "", "false", "False", "1", "yes", "on", "truex")

PYTHONPATH_SENTINEL = "/lit-entrypoint-sentinel:/app"

_STUB_TEMPLATE = """#!/bin/sh
{{
  echo "exec={name}"
  echo "args=$*"
  echo "DD_TRACE_OPENAI_ENABLED=${{DD_TRACE_OPENAI_ENABLED-<unset>}}"
  echo "PYTHONPATH=${{PYTHONPATH-<unset>}}"
}} >> "$RECORD"
"""

_ENTRYPOINT_RE = re.compile(r"^ENTRYPOINT\s+(\[.*\])\s*$", re.MULTILINE)
_CMD_RE = re.compile(r"^CMD\s+(\[.*\])\s*$", re.MULTILINE)


def _write_stubs(bin_dir: Path, names: tuple[str, ...]) -> None:
    for name in names:
        stub = bin_dir / name
        stub.write_text(_STUB_TEMPLATE.format(name=name))
        stub.chmod(0o755)


def _run_entrypoint(
    script: Path,
    argv: tuple[str, ...],
    use_ddtrace: str | None,
    tmp_path: Path,
) -> tuple[str, ...]:
    """Run `script` with stubbed executables on PATH and return the recorded lines."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    _write_stubs(bin_dir, ("ddtrace-run", "uvicorn", "python", "litellm"))
    record = tmp_path / "record.txt"

    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "RECORD": str(record),
        "PYTHONPATH": PYTHONPATH_SENTINEL,
    }
    env.pop("USE_DDTRACE", None)
    env.pop("DD_TRACE_OPENAI_ENABLED", None)
    if use_ddtrace is not None:
        env["USE_DDTRACE"] = use_ddtrace

    result = subprocess.run(
        ["sh", str(script), *argv],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout} stderr={result.stderr}"
    return tuple(record.read_text().splitlines()) if record.exists() else ()


def _entrypoint_argv(dockerfile: Path) -> tuple[str, ...]:
    matches = _ENTRYPOINT_RE.findall(dockerfile.read_text())
    assert matches, f"no exec-form ENTRYPOINT found in {dockerfile}"
    parsed = json.loads(matches[-1])
    return tuple(str(part) for part in parsed)


def _cmd_argv(dockerfile: Path) -> tuple[str, ...]:
    matches = _CMD_RE.findall(dockerfile.read_text())
    assert matches, f"no exec-form CMD found in {dockerfile}"
    return tuple(str(part) for part in json.loads(matches[-1]))


def test_ddtrace_enabled_wraps_the_command_and_disables_the_openai_integration(
    tmp_path: Path,
) -> None:
    """`USE_DDTRACE=true` must prefix the command with `ddtrace-run`, turn the openai
    integration off, and leave PYTHONPATH alone.

    `ddtrace-run` installs its instrumentation by PREPENDING a bootstrap directory to
    PYTHONPATH, and the images set PYTHONPATH=/app so the app package is importable. A
    wrapper that assigned PYTHONPATH instead of inheriting it would either drop the
    bootstrap (silently disabling tracing) or drop /app (breaking the import), so the
    recorded value is asserted verbatim.

    PYTHONPATH_SENTINEL deliberately differs from the images' own /app: with /app as the
    fixture value, a wrapper that overwrote PYTHONPATH with /app would still satisfy this
    assertion and the check would prove nothing.

    The openai integration is disabled because under `ddtrace-run` the bootstrap patches
    it before any litellm code runs, so litellm's in-process `patch_all(..., openai=False)`
    can no longer suppress it; leaving it on double-reports every LLM call.
    """
    recorded = _run_entrypoint(
        COMPONENT_ENTRYPOINT,
        ("uvicorn", "gateway.main:app", "--workers", "2", "--port", "4000"),
        use_ddtrace="true",
        tmp_path=tmp_path,
    )

    assert recorded == (
        "exec=ddtrace-run",
        "args=uvicorn gateway.main:app --workers 2 --port 4000",
        "DD_TRACE_OPENAI_ENABLED=False",
        f"PYTHONPATH={PYTHONPATH_SENTINEL}",
    )


def test_ddtrace_disabled_execs_the_command_directly(tmp_path: Path) -> None:
    recorded = _run_entrypoint(
        COMPONENT_ENTRYPOINT,
        ("uvicorn", "backend.main:app", "--port", "4001"),
        use_ddtrace=None,
        tmp_path=tmp_path,
    )

    assert recorded == (
        "exec=uvicorn",
        "args=backend.main:app --port 4001",
        "DD_TRACE_OPENAI_ENABLED=<unset>",
        f"PYTHONPATH={PYTHONPATH_SENTINEL}",
    )


@pytest.mark.parametrize(
    "use_ddtrace, traced",
    [*((v, True) for v in TRUTHY_USE_DDTRACE), *((v, False) for v in FALSY_USE_DDTRACE)],
)
def test_gating_matches_the_monolithic_entrypoint_and_get_secret_bool(
    use_ddtrace: str | None, traced: bool, tmp_path: Path
) -> None:
    """Both entrypoints must accept exactly the spellings `get_secret_bool` accepts.

    `ProxyStartupEvent._init_dd_tracer` reads `USE_DDTRACE` through `get_secret_bool`, which
    matches `true` case-insensitively. If the shell gate were stricter, `USE_DDTRACE=True` would
    give in-process LLM spans without `ddtrace-run` HTTP spans, a half-enabled state.
    """
    component = _run_entrypoint(
        COMPONENT_ENTRYPOINT,
        ("uvicorn", "gateway.main:app"),
        use_ddtrace=use_ddtrace,
        tmp_path=tmp_path / "component",
    )
    monolith = _run_entrypoint(
        PROD_ENTRYPOINT,
        ("--port", "4000"),
        use_ddtrace=use_ddtrace,
        tmp_path=tmp_path / "monolith",
    )

    expected_exec = "exec=ddtrace-run" if traced else "exec=uvicorn"
    expected_openai = "DD_TRACE_OPENAI_ENABLED=False" if traced else "DD_TRACE_OPENAI_ENABLED=<unset>"
    assert component[0] == expected_exec
    assert component[2] == expected_openai
    assert monolith[0] == ("exec=ddtrace-run" if traced else "exec=litellm")
    assert monolith[2] == expected_openai
    assert monolith[1] == ("args=litellm --port 4000" if traced else "args=--port 4000")


def test_wipes_the_prometheus_multiproc_dir_before_uvicorn_forks(tmp_path: Path) -> None:
    """A restarted container inherits the emptyDir of its predecessor, whose worker pids it may reuse, so the
    stale .db files must be gone before any worker opens the one carrying its own pid."""
    multiproc_dir = tmp_path / "multiproc"
    multiproc_dir.mkdir()
    (multiproc_dir / "gauge_livesum_7.db").write_bytes(b"stale")
    (multiproc_dir / "counter_7.db").write_bytes(b"stale")
    (multiproc_dir / "keep.txt").write_text("not a sample")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stubs(bin_dir, ("uvicorn",))
    record = tmp_path / "record.txt"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "RECORD": str(record),
        "PROMETHEUS_MULTIPROC_DIR": str(multiproc_dir),
    }
    env.pop("USE_DDTRACE", None)
    result = subprocess.run(
        ["sh", str(COMPONENT_ENTRYPOINT), "uvicorn", "gateway.main:app"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"stdout={result.stdout} stderr={result.stderr}"
    assert sorted(p.name for p in multiproc_dir.iterdir()) == ["keep.txt"]
    assert record.read_text().splitlines()[0] == "exec=uvicorn"


def test_creates_a_missing_prometheus_multiproc_dir(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stubs(bin_dir, ("uvicorn",))
    missing = tmp_path / "multiproc"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "RECORD": str(tmp_path / "record.txt"),
        "PROMETHEUS_MULTIPROC_DIR": str(missing),
    }
    env.pop("USE_DDTRACE", None)
    result = subprocess.run(
        ["sh", str(COMPONENT_ENTRYPOINT), "uvicorn", "gateway.main:app"], env=env, capture_output=True, text=True
    )

    assert result.returncode == 0, f"stdout={result.stdout} stderr={result.stderr}"
    assert missing.is_dir()


def test_entrypoint_script_is_executable() -> None:
    mode = COMPONENT_ENTRYPOINT.stat().st_mode
    assert mode & stat.S_IXUSR, "entrypoint must be committed executable to run as the image ENTRYPOINT"
    assert mode & stat.S_IXOTH, "entrypoint must be executable by the unprivileged `nonroot` user"


def test_entrypoint_script_has_no_carriage_returns() -> None:
    assert b"\r" not in COMPONENT_ENTRYPOINT.read_bytes()


@pytest.mark.parametrize(
    "dockerfile, launcher",
    [
        (GATEWAY_DOCKERFILE, "python -m gateway.launch"),
        (BACKEND_DOCKERFILE, "uvicorn backend.main:app"),
    ],
)
def test_component_images_launch_uvicorn_through_the_entrypoint(dockerfile: Path, launcher: str) -> None:
    entrypoint = " ".join(_entrypoint_argv(dockerfile))

    assert IMAGE_ENTRYPOINT_PATH in entrypoint, f"{dockerfile} bypasses the ddtrace-aware entrypoint"
    assert launcher in entrypoint
    assert entrypoint.index(IMAGE_ENTRYPOINT_PATH) < entrypoint.index(launcher), (
        f"{dockerfile} must invoke uvicorn through the entrypoint, not the other way around"
    )


@pytest.mark.parametrize(
    "use_ddtrace, num_workers, expected_exec, expected_args",
    [
        (None, "4", "exec=python", "args=-m gateway.launch --workers 4 --host 0.0.0.0 --port 4000"),
        (None, None, "exec=python", "args=-m gateway.launch --workers 1 --host 0.0.0.0 --port 4000"),
        ("true", "4", "exec=ddtrace-run", "args=python -m gateway.launch --workers 4 --host 0.0.0.0 --port 4000"),
    ],
)
def test_gateway_image_execs_the_supervisor_with_its_worker_count(
    use_ddtrace: str | None, num_workers: str | None, expected_exec: str, expected_args: str, tmp_path: Path
) -> None:
    """Run the gateway image's ENTRYPOINT + CMD and record what the container execs.

    The Dockerfile's `/app/...` script path is resolved to the checked-in script and `python`
    is stubbed on PATH, so the assertion is on the argv `gateway.launch` receives, not on the
    Dockerfile text.
    """
    entrypoint = tuple(
        part.replace(IMAGE_ENTRYPOINT_PATH, str(COMPONENT_ENTRYPOINT)) for part in _entrypoint_argv(GATEWAY_DOCKERFILE)
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    _write_stubs(bin_dir, ("ddtrace-run", "python", "uvicorn"))
    record = tmp_path / "record.txt"
    overrides = {"USE_DDTRACE": use_ddtrace, "NUM_WORKERS": num_workers}
    env = {
        **{k: v for k, v in os.environ.items() if k not in ("DD_TRACE_OPENAI_ENABLED", *overrides)},
        **{k: v for k, v in overrides.items() if v is not None},
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "RECORD": str(record),
        "PYTHONPATH": PYTHONPATH_SENTINEL,
    }

    result = subprocess.run(
        [*entrypoint, *_cmd_argv(GATEWAY_DOCKERFILE)], env=env, capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, f"stdout={result.stdout} stderr={result.stderr}"
    assert tuple(record.read_text().splitlines())[:2] == (expected_exec, expected_args)


@pytest.mark.parametrize("dockerfile", [GATEWAY_DOCKERFILE, BACKEND_DOCKERFILE])
def test_component_images_make_the_entrypoint_executable(dockerfile: Path) -> None:
    body = dockerfile.read_text()
    assert "chmod +x docker/component_entrypoint.sh" in body


def test_gateway_keeps_its_worker_count_and_backend_keeps_a_single_process() -> None:
    gateway = " ".join(_entrypoint_argv(GATEWAY_DOCKERFILE)) + " " + " ".join(_cmd_argv(GATEWAY_DOCKERFILE))
    backend = " ".join(_entrypoint_argv(BACKEND_DOCKERFILE)) + " " + " ".join(_cmd_argv(BACKEND_DOCKERFILE))

    assert "--workers" in gateway and "NUM_WORKERS" in gateway
    assert "--workers" not in backend and "NUM_WORKERS" not in backend
