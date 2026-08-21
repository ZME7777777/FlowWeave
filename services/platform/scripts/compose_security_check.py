"""Fail when the rendered local Compose sandbox trust boundary drifts."""

from __future__ import annotations

import json
import sys
from typing import Any, cast


def fail(message: str) -> None:
    raise SystemExit(f"compose security check failed: {message}")


def mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(f"{name} must be an object")
    return cast(dict[str, Any], value)


def sequence(value: object) -> list[Any]:
    return cast(list[Any], value) if isinstance(value, list) else []


def environment(service: dict[str, Any]) -> dict[str, str]:
    raw = mapping(service.get("environment", {}), "service environment")
    return {str(key): str(value) for key, value in raw.items()}


def network_names(service: dict[str, Any]) -> set[str]:
    raw = service.get("networks", {})
    if isinstance(raw, dict):
        return {str(name) for name in raw}
    if isinstance(raw, list):
        return {str(name) for name in raw}
    return set()


def socket_mounts(service: dict[str, Any]) -> list[dict[str, Any]]:
    mounts: list[dict[str, Any]] = []
    for value in sequence(service.get("volumes")):
        if not isinstance(value, dict):
            continue
        mount = cast(dict[str, Any], value)
        source = str(mount.get("source") or "")
        target = str(mount.get("target") or "")
        if source == "/var/run/docker.sock" or target == "/var/run/docker.sock":
            mounts.append(mount)
    return mounts


def published_ports(service: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        cast(dict[str, Any], item)
        for item in sequence(service.get("ports"))
        if isinstance(item, dict)
    ]


def check_document(document: dict[str, Any]) -> None:
    services = mapping(document.get("services"), "services")
    required = {"runtime-provider", "api", "worker"}
    if missing := required - services.keys():
        fail(f"missing services: {', '.join(sorted(missing))}")

    controller = mapping(services["runtime-provider"], "runtime-provider")
    api = mapping(services["api"], "api")
    worker = mapping(services["worker"], "worker")
    worker_labels = mapping(worker.get("labels", {}), "worker labels")

    for name, raw_service in services.items():
        service = mapping(raw_service, str(name))
        for port in published_ports(service):
            if str(port.get("host_ip") or "") not in {"127.0.0.1", "::1"}:
                fail(f"{name} published ports must bind only to loopback")

    holders = {
        name: socket_mounts(mapping(service, name))
        for name, service in services.items()
        if socket_mounts(mapping(service, name))
    }
    if set(holders) != {"runtime-provider"} or len(holders["runtime-provider"]) != 1:
        fail("only runtime-provider may mount Docker Socket exactly once")
    socket_mount = holders["runtime-provider"][0]
    if socket_mount.get("type") != "bind":
        fail("Docker Socket must be an explicit bind mount")

    networks = mapping(document.get("networks"), "networks")
    docker_control = mapping(networks.get("docker-control"), "docker-control network")
    if docker_control.get("internal") is not True:
        fail("docker-control network must be internal")
    if network_names(controller) != {"docker-control"}:
        fail("runtime-provider must attach only to docker-control")
    if controller.get("ports") or controller.get("expose"):
        fail("runtime-provider must not publish or expose ports")
    if "docker-control" not in network_names(api) or "docker-control" not in network_names(worker):
        fail("api and worker must reach the controller only through docker-control")
    forbidden_static_sandbox_networks = {
        name for name in networks if "dependency" in str(name) or "sandbox" in str(name)
    }
    if forbidden_static_sandbox_networks:
        fail("disposable sandboxes must not use static shared Compose networks")
    allowed_control_clients = {"runtime-provider", "api", "worker"}
    attached_to_control = {
        str(name)
        for name, service in services.items()
        if "docker-control" in network_names(mapping(service, str(name)))
    }
    if attached_to_control != allowed_control_clients:
        fail("only runtime-provider, api, and worker may attach to docker-control")
    for name, client in (("api", api), ("worker", worker)):
        if str(client.get("user")) != "10001:10001":
            fail(f"{name} must run explicitly as uid/gid 10001")
    if worker_labels.get("flowweave.runtime-client") != "true":
        fail("worker must be explicitly marked as a Runtime network client")
    if worker_labels.get("flowweave.runtime-client-role") != "worker":
        fail("worker Runtime client role label is missing")
    for name, raw_service in services.items():
        if str(name) == "worker":
            continue
        service = mapping(raw_service, str(name))
        labels = mapping(service.get("labels", {}), f"{name} labels")
        if "flowweave.runtime-client" in labels or "flowweave.runtime-client-role" in labels:
            fail("only worker may carry Runtime client labels")

    if str(controller.get("user")) != "10001:10001":
        fail("runtime-provider must run explicitly as uid/gid 10001")
    if controller.get("privileged") is True:
        fail("runtime-provider must not run privileged")
    if controller.get("pid") == "host" or controller.get("ipc") == "host":
        fail("runtime-provider must not join host namespaces")
    if controller.get("devices"):
        fail("runtime-provider must not receive host devices")
    group_add = {str(item) for item in sequence(controller.get("group_add"))}
    if len(group_add) != 1 or not next(iter(group_add), "").isdigit():
        fail("runtime-provider must receive exactly one numeric Docker Socket GID")
    if controller.get("read_only") is not True:
        fail("runtime-provider root filesystem must be read-only")
    if "ALL" not in {str(item) for item in sequence(controller.get("cap_drop"))}:
        fail("runtime-provider must drop all Linux capabilities")
    security_opt = {str(item) for item in sequence(controller.get("security_opt"))}
    if "no-new-privileges:true" not in security_opt:
        fail("runtime-provider must enable no-new-privileges")
    if not isinstance(controller.get("pids_limit"), int) or controller["pids_limit"] > 256:
        fail("runtime-provider must have a PID limit of at most 256")
    if not any(str(item).startswith("/tmp") for item in sequence(controller.get("tmpfs"))):
        fail("runtime-provider must use a bounded writable /tmp tmpfs")

    controller_env = environment(controller)
    api_env = environment(api)
    worker_env = environment(worker)
    if controller_env.get("DOCKER_CONTROLLER_MODE") != "local":
        fail("runtime-provider must use local Docker control mode")
    runtime_network_mode = controller_env.get("SANDBOX_RUNTIME_NETWORK_MODE", "")
    if runtime_network_mode not in {"isolated", "egress"}:
        fail("runtime-provider Runtime network mode must be isolated or egress")
    for name, client_env in (("api", api_env), ("worker", worker_env)):
        if client_env.get("DOCKER_CONTROLLER_MODE") != "remote":
            fail(f"{name} must use remote Docker control mode")
        if client_env.get("DOCKER_CONTROLLER_URL") != "http://runtime-provider:8090":
            fail(f"{name} must use the internal runtime-provider URL")
        if client_env.get("SANDBOX_RUNTIME_NETWORK_MODE") != runtime_network_mode:
            fail(f"{name} Runtime network mode must match runtime-provider")
    api_key = api_env.get("DOCKER_CONTROLLER_API_KEY", "")
    worker_key = worker_env.get("DOCKER_CONTROLLER_API_KEY", "")
    if len(api_key) < 32 or len(worker_key) < 32 or api_key == worker_key:
        fail("api and worker must receive different strong controller keys")
    if controller_env.get("DOCKER_CONTROLLER_API_KEY") != api_key:
        fail("controller API key does not match the API principal")
    if controller_env.get("DOCKER_CONTROLLER_WORKER_API_KEY") != worker_key:
        fail("controller Worker key does not match the Worker principal")
    if (
        "DOCKER_CONTROLLER_WORKER_API_KEY" in api_env
        or "DOCKER_CONTROLLER_WORKER_API_KEY" in worker_env
    ):
        fail("controller dual-key configuration must not leak to clients")

    forbidden_controller_secrets = {
        "DATABASE_URL",
        "CREDENTIALS_MASTER_KEY",
        "CREDENTIAL_INTERNAL_API_KEY",
        "LARK_OAUTH_CLIENT_SECRET",
    }
    if leaked := forbidden_controller_secrets & controller_env.keys():
        fail(f"controller contains business credentials: {', '.join(sorted(leaked))}")


def main() -> None:
    try:
        document = mapping(json.load(sys.stdin), "Compose document")
    except json.JSONDecodeError as exc:
        fail(f"invalid rendered JSON: {exc}")
    check_document(document)
    print("compose security check passed")


if __name__ == "__main__":
    main()
