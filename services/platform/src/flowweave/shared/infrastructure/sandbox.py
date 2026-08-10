from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import quickjs

from flowweave.bootstrap.settings import Settings
from flowweave.shared.application.sandbox import (
    SandboxExecution,
    SandboxLanguage,
    SandboxPort,
)
from flowweave.shared.infrastructure.docker_control import (
    DockerControlError,
    EphemeralDockerLease,
    remove_owned_container,
)
from flowweave.shared.infrastructure.docker_controller import (
    DockerControllerClient,
    DockerControllerError,
    controller_is_remote,
)

_MAX_CODE_BYTES = 32_768
_MAX_LOG_BYTES = 4_000


def _invalid_code(code: str) -> SandboxExecution | None:
    if not code or len(code.encode()) > _MAX_CODE_BYTES:
        return SandboxExecution("ERROR", error="Gate code is empty or too large")
    return None


def _decode_output(stdout: str, stderr: str, returncode: int) -> SandboxExecution:
    try:
        value = cast(object, json.loads(stdout))
    except json.JSONDecodeError:
        return SandboxExecution(
            "ERROR",
            error="Sandbox returned invalid JSON",
            log=(stderr or stdout)[:_MAX_LOG_BYTES],
        )
    if returncode == 0:
        return SandboxExecution("COMPLETED", result=value, log=stderr[:_MAX_LOG_BYTES])
    if isinstance(value, dict):
        error = str(cast(dict[str, object], value).get("runner_error") or "Sandbox failed")
    else:
        error = "Sandbox failed"
    return SandboxExecution("ERROR", error=error, log=stderr[:_MAX_LOG_BYTES])


def _limit_child() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
        resource.setrlimit(resource.RLIMIT_AS, (128 * 1024 * 1024, 128 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16))
    except (ImportError, OSError, ValueError):
        pass


class ProcessSandbox:
    """Restricted local adapter for tests and development; not a security boundary."""

    def execute(
        self,
        language: SandboxLanguage,
        code: str,
        context: dict[str, Any],
        timeout_seconds: int,
    ) -> SandboxExecution:
        invalid = _invalid_code(code)
        if invalid:
            return invalid
        if language == "PYTHON":
            return self._python(code, context, timeout_seconds)
        return self._javascript(code, context, timeout_seconds)

    def _python(self, code: str, context: dict[str, Any], timeout_seconds: int) -> SandboxExecution:
        runner = (
            Path(__file__).parents[2] / "modules" / "gates" / "application" / "_python_runner.py"
        )
        try:
            completed = subprocess.run(
                [sys.executable, "-I", "-S", str(runner)],
                input=json.dumps({"code": code, "context": context}, ensure_ascii=False),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                env={"PATH": os.defpath, "PYTHONIOENCODING": "utf-8"},
                preexec_fn=_limit_child if os.name == "posix" else None,
            )
        except subprocess.TimeoutExpired:
            return SandboxExecution("TIMEOUT", error="Python gate timed out")
        return _decode_output(completed.stdout, completed.stderr, completed.returncode)

    def _javascript(
        self, code: str, context: dict[str, Any], timeout_seconds: int
    ) -> SandboxExecution:
        runtime = quickjs.Context()
        runtime.set_memory_limit(16 * 1024 * 1024)
        runtime.set_max_stack_size(512 * 1024)
        runtime.set_time_limit(float(timeout_seconds))
        context_json = json.dumps(context, ensure_ascii=False).replace("</", "<\\/")
        source = (
            "JSON.stringify((function(context){'use strict';"
            + code
            + "})(JSON.parse("
            + json.dumps(context_json)
            + ")))"
        )
        try:
            raw = runtime.eval(source)
            if not isinstance(raw, str):
                return SandboxExecution("ERROR", error="JavaScript gate returned invalid JSON")
            return SandboxExecution("COMPLETED", result=cast(object, json.loads(raw)))
        except Exception as exc:
            message = str(exc)
            if "interrupted" in message.lower():
                return SandboxExecution("TIMEOUT", error="JavaScript gate timed out", log=message)
            return SandboxExecution(
                "ERROR", error="JavaScript gate failed", log=message[:_MAX_LOG_BYTES]
            )


class DockerSandbox:
    """Runs each script in a new, resource-constrained Docker container."""

    def __init__(
        self,
        python_image: str,
        javascript_image: str,
        *,
        docker_binary: str = "docker",
        manager_scope: str,
        cleanup_grace_seconds: int = 300,
        storage_size: str = "4g",
    ) -> None:
        self.images: dict[SandboxLanguage, str] = {
            "PYTHON": python_image,
            "JAVASCRIPT": javascript_image,
        }
        self.docker_binary = docker_binary
        self.manager_scope = manager_scope
        self.cleanup_grace_seconds = cleanup_grace_seconds
        self.storage_size = storage_size

    def execute(
        self,
        language: SandboxLanguage,
        code: str,
        context: dict[str, Any],
        timeout_seconds: int,
    ) -> SandboxExecution:
        invalid = _invalid_code(code)
        if invalid:
            return invalid
        lease = EphemeralDockerLease.create(
            kind="gate",
            owner_type="GATE_EXECUTION",
            manager_scope=self.manager_scope,
            ttl_seconds=timeout_seconds + self.cleanup_grace_seconds,
        )
        command = [
            self.docker_binary,
            "run",
            "--rm",
            "--interactive",
            "--name",
            lease.resource_name,
            *lease.label_args(),
            "--log-driver",
            "local",
            "--log-opt",
            "max-size=4m",
            "--log-opt",
            "max-file=2",
            "--storage-opt",
            f"size={self.storage_size}",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "64",
            "--memory",
            "128m",
            "--cpus",
            "1",
            "--user",
            "65534:65534",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m",
            self.images[language],
        ]
        payload = json.dumps({"code": code, "context": context}, ensure_ascii=False)
        try:
            completed = subprocess.run(
                command,
                input=payload,
                capture_output=True,
                text=True,
                timeout=timeout_seconds + 1,
                check=False,
                env={"PATH": os.defpath},
            )
        except subprocess.TimeoutExpired:
            try:
                remove_owned_container(
                    self.docker_binary,
                    lease.resource_name,
                    lease.resource_id,
                    expected_manager_scope=lease.manager_scope,
                    timeout=5,
                )
            except DockerControlError:
                # The durable expiry labels let maintenance retry cleanup. Never
                # bypass ownership verification merely because immediate cleanup failed.
                pass
            return SandboxExecution("TIMEOUT", error=f"{language.title()} gate timed out")
        except OSError as exc:
            return SandboxExecution("ERROR", error="Docker sandbox is unavailable", log=str(exc))
        return _decode_output(completed.stdout, completed.stderr, completed.returncode)


class RemoteDockerSandbox:
    """Executes fixed gate operations through the authenticated controller."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def execute(
        self,
        language: SandboxLanguage,
        code: str,
        context: dict[str, Any],
        timeout_seconds: int,
    ) -> SandboxExecution:
        invalid = _invalid_code(code)
        if invalid:
            return invalid
        try:
            raw = DockerControllerClient(self.settings).post(
                "/v1/gates/execute",
                {
                    "language": language,
                    "code": code,
                    "context": context,
                    "timeout_seconds": timeout_seconds,
                },
                timeout=timeout_seconds + 10,
            )
        except DockerControllerError as exc:
            return SandboxExecution(
                "ERROR", error="Docker sandbox controller is unavailable", log=str(exc)
            )
        status = str(raw.get("status") or "ERROR")
        if status not in {"COMPLETED", "ERROR", "TIMEOUT"}:
            status = "ERROR"
        return SandboxExecution(
            cast(Any, status),
            result=raw.get("result"),
            error=str(raw.get("error") or "") or None,
            log=str(raw.get("log") or "")[:_MAX_LOG_BYTES],
        )


def build_sandbox(settings: Settings) -> SandboxPort:
    if settings.sandbox_backend == "process":
        return ProcessSandbox()
    if settings.sandbox_backend == "docker":
        if controller_is_remote(settings):
            return RemoteDockerSandbox(settings)
        return DockerSandbox(
            settings.sandbox_image_python,
            settings.sandbox_image_javascript,
            docker_binary=settings.docker_binary,
            manager_scope=settings.sandbox_manager_scope,
            cleanup_grace_seconds=settings.sandbox_orphan_grace_seconds,
            storage_size=settings.sandbox_storage_size,
        )
    raise ValueError(f"Unsupported sandbox backend: {settings.sandbox_backend}")
