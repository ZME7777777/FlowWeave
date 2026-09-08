from __future__ import annotations

import os
from types import SimpleNamespace

from flowweave.bootstrap import runtime_provider
from flowweave.modules.environments.infrastructure import docker


class _Process:
    def __init__(self) -> None:
        self.terminated = False

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float) -> None:
        return None

    def kill(self) -> None:
        self.terminated = True


def test_persistent_terminal_ttl_waits_for_attachment_then_reaps(monkeypatch) -> None:
    now = 0.0
    destroyed: list[tuple[str, str]] = []
    protected: list[dict[str, set[str]]] = []

    def monotonic() -> float:
        return now

    def open_terminal(*_args: object, **_kwargs: object) -> tuple[int, _Process]:
        master, slave = os.pipe()
        os.close(slave)
        return master, _Process()

    def destroy_terminal_session(container_id: str, session_name: str) -> None:
        destroyed.append((container_id, session_name))

    def reap_managed_terminal_sessions(**kwargs: object) -> int:
        protected.append(kwargs["protected_sessions"])
        return 0

    monkeypatch.setattr(runtime_provider.time, "monotonic", monotonic)
    monkeypatch.setattr(runtime_provider.environments_docker, "open_terminal", open_terminal)
    monkeypatch.setattr(
        runtime_provider.environments_docker,
        "destroy_terminal_session",
        destroy_terminal_session,
    )
    monkeypatch.setattr(
        runtime_provider.environments_docker,
        "reap_managed_terminal_sessions",
        reap_managed_terminal_sessions,
    )
    manager = runtime_provider._TerminalManager(idle_seconds=10, hard_ttl_seconds=20)

    terminal_id = manager.start("container-a", "flowweave-terminal-a", 24, 80)
    now = 30.0
    manager.get(terminal_id)
    manager.reap()
    assert destroyed == []
    assert protected == [{"container-a": {"flowweave-terminal-a"}}]

    manager.close(terminal_id)
    manager.reap()
    assert destroyed == [("container-a", "flowweave-terminal-a")]


def test_persistent_terminal_scan_is_scoped_and_excludes_attached_sessions(monkeypatch) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], *, timeout: int) -> str:
        commands.append(command)
        if command[1:3] == ["ps", "--quiet"]:
            return "owned-runtime\n"
        assert command[1:3] == ["exec", "owned-runtime"]
        assert command[-1] == "flowweave-active"
        return "1"

    monkeypatch.setattr(docker, "require_backend", lambda: None)
    monkeypatch.setattr(
        docker,
        "get_settings",
        lambda: SimpleNamespace(docker_binary="docker", sandbox_manager_scope="scope-a"),
    )
    monkeypatch.setattr(docker, "_run", run)

    assert (
        docker.reap_managed_terminal_sessions(
            idle_seconds=1800,
            hard_ttl_seconds=28_800,
            protected_sessions={"owned-runtime": {"flowweave-active"}},
        )
        == 1
    )
    assert "label=flowweave.manager-scope=scope-a" in commands[0]
    assert "label=flowweave.kind=agent-runtime" in commands[0]
