from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from flowweave.modules.gates.application.executor import execute_gate
from flowweave.shared.application.sandbox import SandboxExecution, SandboxLanguage
from flowweave.shared.infrastructure import sandbox as sandbox_module
from flowweave.shared.infrastructure.sandbox import DockerSandbox, ProcessSandbox
from flowweave.shared.sandbox import sandbox_context


class RecordingSandbox:
    def __init__(self) -> None:
        self.calls: list[tuple[SandboxLanguage, str, dict[str, Any], int]] = []

    def execute(
        self,
        language: SandboxLanguage,
        code: str,
        context: dict[str, Any],
        timeout_seconds: int,
    ) -> SandboxExecution:
        self.calls.append((language, code, context, timeout_seconds))
        return SandboxExecution(
            "COMPLETED",
            result={
                "decision": "PASS",
                "summary": "sandbox-port",
                "reasons": [],
                "evidence": [],
                "details": {},
            },
        )


def test_gate_executor_calls_only_the_sandbox_port(db_session_factory) -> None:
    sandbox = RecordingSandbox()
    with sandbox_context(sandbox), db_session_factory() as db:
        result = execute_gate(
            db,
            "PYTHON",
            {"code": "result = {'decision': 'PASS'}"},
            {"artifacts": []},
            17,
        )

    assert result.decision == "PASS"
    assert result.summary == "sandbox-port"
    assert sandbox.calls == [("PYTHON", "result = {'decision': 'PASS'}", {"artifacts": []}, 17)]


def test_process_sandbox_preserves_python_and_javascript_protocol() -> None:
    sandbox = ProcessSandbox()
    python = sandbox.execute(
        "PYTHON",
        "result = {'decision': 'PASS', 'summary': str(len(context['items']))}",
        {"items": [1, 2]},
        2,
    )
    javascript = sandbox.execute(
        "JAVASCRIPT",
        "return {decision: context.ready ? 'PASS' : 'FAIL', summary: 'js'};",
        {"ready": True},
        2,
    )

    assert python.status == "COMPLETED"
    assert python.result == {"decision": "PASS", "summary": "2"}
    assert javascript.status == "COMPLETED"
    assert javascript.result == {"decision": "PASS", "summary": "js"}


def test_docker_sandbox_uses_one_hardened_container_per_execution(monkeypatch) -> None:
    calls: list[tuple[list[str], str | None]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs.get("input")))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"decision": "PASS", "summary": "isolated"}),
            stderr="",
        )

    monkeypatch.setattr(sandbox_module.subprocess, "run", fake_run)
    sandbox = DockerSandbox(
        "python-gate:locked",
        "javascript-gate:locked",
        manager_scope="test-scope",
    )

    first = sandbox.execute("PYTHON", "result = {}", {"value": 1}, 5)
    second = sandbox.execute("JAVASCRIPT", "return {};", {"value": 2}, 5)

    assert first.status == second.status == "COMPLETED"
    assert len(calls) == 2
    first_command, first_payload = calls[0]
    second_command, _ = calls[1]
    for command in (first_command, second_command):
        assert command[:2] == ["docker", "run"]
        assert "--rm" in command
        labels = [command[index + 1] for index, value in enumerate(command) if value == "--label"]
        assert "flowweave.managed=true" in labels
        assert "flowweave.manager-scope=test-scope" in labels
        assert "flowweave.lifecycle=ephemeral" in labels
        assert any(label.startswith("flowweave.resource-id=") for label in labels)
        assert any(label.startswith("flowweave.expires-at=") for label in labels)
        assert "flowweave.kind=gate" in labels
        assert any(label.startswith("flowweave.owner-id=") for label in labels)
        assert command[command.index("--network") + 1] == "none"
        assert "--read-only" in command
        assert command[command.index("--cap-drop") + 1] == "ALL"
        assert command[command.index("--security-opt") + 1] == "no-new-privileges"
        assert command[command.index("--pids-limit") + 1] == "64"
        assert command[command.index("--memory") + 1] == "128m"
        assert command[command.index("--cpus") + 1] == "1"
        assert command[command.index("--user") + 1] == "65534:65534"
        assert command[command.index("--storage-opt") + 1] == "size=4g"
        assert command[command.index("--tmpfs") + 1] == "/tmp:rw,noexec,nosuid,size=16m"
    assert first_command[-1] == "python-gate:locked"
    assert second_command[-1] == "javascript-gate:locked"
    assert (
        first_command[first_command.index("--name") + 1]
        != second_command[second_command.index("--name") + 1]
    )
    assert json.loads(first_payload or "{}") == {"code": "result = {}", "context": {"value": 1}}


def test_docker_sandbox_force_removes_timed_out_container(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1] == "run":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if command[1] == "inspect":
            run_command = calls[0]
            labels = {
                value.split("=", 1)[0]: value.split("=", 1)[1]
                for index, item in enumerate(run_command)
                if item == "--label"
                for value in [run_command[index + 1]]
            }
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "Id": "verified-container-id",
                        "Config": {"Labels": labels},
                    }
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(sandbox_module.subprocess, "run", fake_run)
    sandbox = DockerSandbox(
        "python-gate:locked",
        "javascript-gate:locked",
        manager_scope="test-scope",
    )
    result = sandbox.execute("PYTHON", "result = {}", {}, 1)

    assert result.status == "TIMEOUT"
    assert len(calls) == 3
    container_name = calls[0][calls[0].index("--name") + 1]
    assert calls[1] == [
        "docker",
        "inspect",
        container_name,
        "--format",
        "{{json .}}",
    ]
    assert calls[2] == ["docker", "rm", "--force", "verified-container-id"]


@pytest.mark.parametrize("language", ["PYTHON", "JAVASCRIPT"])
def test_sandbox_rejects_empty_or_oversized_code_before_launch(language: SandboxLanguage) -> None:
    sandbox = DockerSandbox(
        "python-gate:locked",
        "javascript-gate:locked",
        manager_scope="test-scope",
    )
    assert sandbox.execute(language, "", {}, 1).status == "ERROR"
    assert sandbox.execute(language, "x" * 32_769, {}, 1).status == "ERROR"
