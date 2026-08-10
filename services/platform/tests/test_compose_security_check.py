from __future__ import annotations

import runpy
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

_CHECKER = runpy.run_path(str(Path(__file__).parents[1] / "scripts" / "compose_security_check.py"))
check_document = _CHECKER["check_document"]


_API_KEY = "compose-api-key-with-at-least-32-characters"
_WORKER_KEY = "compose-worker-key-with-at-least-32-characters"


def _document() -> dict[str, Any]:
    client_environment = {
        "DOCKER_CONTROLLER_MODE": "remote",
        "DOCKER_CONTROLLER_URL": "http://sandbox-controller:8090",
        "SANDBOX_RUNTIME_NETWORK_MODE": "egress",
    }
    return {
        "services": {
            "sandbox-controller": {
                "user": "10001:10001",
                "read_only": True,
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "pids_limit": 256,
                "tmpfs": ["/tmp:rw,noexec,nosuid,nodev,size=64m"],
                "group_add": ["0"],
                "networks": {"docker-control": None},
                "volumes": [
                    {
                        "type": "bind",
                        "source": "/var/run/docker.sock",
                        "target": "/var/run/docker.sock",
                    }
                ],
                "environment": {
                    "DOCKER_CONTROLLER_MODE": "local",
                    "SANDBOX_RUNTIME_NETWORK_MODE": "egress",
                    "DOCKER_CONTROLLER_API_KEY": _API_KEY,
                    "DOCKER_CONTROLLER_WORKER_API_KEY": _WORKER_KEY,
                },
            },
            "api": {
                "user": "10001:10001",
                "ports": [{"host_ip": "127.0.0.1", "published": "8080", "target": 8080}],
                "networks": {"default": None, "docker-control": None},
                "environment": {
                    **client_environment,
                    "DOCKER_CONTROLLER_API_KEY": _API_KEY,
                },
            },
            "worker": {
                "user": "10001:10001",
                "labels": {
                    "flowweave.runtime-client": "true",
                    "flowweave.runtime-client-role": "worker",
                },
                "networks": {"default": None, "docker-control": None},
                "environment": {
                    **client_environment,
                    "DOCKER_CONTROLLER_API_KEY": _WORKER_KEY,
                },
            },
        },
        "networks": {"default": {}, "docker-control": {"internal": True}},
    }


def test_compose_security_check_accepts_hardened_control_plane() -> None:
    check_document(_document())


def _leak_socket(document: dict[str, Any]) -> None:
    document["services"]["worker"]["volumes"] = [
        {
            "type": "bind",
            "source": "/var/run/docker.sock",
            "target": "/var/run/docker.sock",
        }
    ]


def _attach_untrusted_service(document: dict[str, Any]) -> None:
    document["services"]["runtime"] = {"networks": {"docker-control": None}}


def _copy_runtime_client_identity(document: dict[str, Any]) -> None:
    document["services"]["api"]["labels"] = {
        "flowweave.runtime-client": "true",
        "flowweave.runtime-client-role": "worker",
    }


def _make_controller_privileged(document: dict[str, Any]) -> None:
    document["services"]["sandbox-controller"]["privileged"] = True


def _run_worker_as_root(document: dict[str, Any]) -> None:
    document["services"]["worker"]["user"] = "0"


def _use_local_worker_control(document: dict[str, Any]) -> None:
    document["services"]["worker"]["environment"]["DOCKER_CONTROLLER_MODE"] = "local"


def _reuse_api_key_for_worker(document: dict[str, Any]) -> None:
    document["services"]["worker"]["environment"]["DOCKER_CONTROLLER_API_KEY"] = _API_KEY
    document["services"]["sandbox-controller"]["environment"][
        "DOCKER_CONTROLLER_WORKER_API_KEY"
    ] = _API_KEY


def _leak_database_credentials(document: dict[str, Any]) -> None:
    document["services"]["sandbox-controller"]["environment"]["DATABASE_URL"] = (
        "postgresql://example.invalid/flowweave"
    )


def _use_symbolic_socket_group(document: dict[str, Any]) -> None:
    document["services"]["sandbox-controller"]["group_add"] = ["docker"]


def _drift_runtime_network_mode(document: dict[str, Any]) -> None:
    document["services"]["worker"]["environment"]["SANDBOX_RUNTIME_NETWORK_MODE"] = "isolated"


def _add_shared_dependency_network(document: dict[str, Any]) -> None:
    document["networks"]["dependency-build"] = {}
    document["services"]["worker"]["networks"]["dependency-build"] = None


def _publish_api_on_all_interfaces(document: dict[str, Any]) -> None:
    document["services"]["api"]["ports"][0]["host_ip"] = "0.0.0.0"


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (_leak_socket, "only sandbox-controller may mount Docker Socket"),
        (_attach_untrusted_service, "only sandbox-controller, api, and worker"),
        (_copy_runtime_client_identity, "only worker may carry Runtime client labels"),
        (_make_controller_privileged, "must not run privileged"),
        (_run_worker_as_root, "worker must run explicitly as uid/gid 10001"),
        (_use_local_worker_control, "worker must use remote Docker control mode"),
        (_reuse_api_key_for_worker, "different strong controller keys"),
        (_leak_database_credentials, "controller contains business credentials"),
        (_use_symbolic_socket_group, "numeric Docker Socket GID"),
        (_drift_runtime_network_mode, "Runtime network mode must match"),
        (_add_shared_dependency_network, "must not use static shared Compose networks"),
        (_publish_api_on_all_interfaces, "published ports must bind only to loopback"),
    ),
)
def test_compose_security_check_rejects_boundary_regressions(
    mutate: Callable[[dict[str, Any]], None], message: str
) -> None:
    document = deepcopy(_document())
    mutate(document)

    with pytest.raises(SystemExit, match=message):
        check_document(document)
