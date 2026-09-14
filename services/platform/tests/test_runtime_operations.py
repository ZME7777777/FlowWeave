from pathlib import Path
from types import SimpleNamespace

from flowweave.modules.sandboxes.application import runtime_operations
from flowweave.modules.sandboxes.application.runtime_operations import (
    _host_project_mount_path,
    flow_run_terminal_details,
)


def test_host_project_mount_path_uses_only_canonical_flow_run_allocation(tmp_path: Path) -> None:
    relative = ".flow-run-runtimes/" + "a" * 32 + "/12345678-1234-4234-9234-123456789abc"
    workspace_root = tmp_path / "workspaces"

    assert _host_project_mount_path(
        {"runtime_allocation_relative": relative}, workspace_root
    ) == str(workspace_root / relative / "workspace/project")
    assert (
        _host_project_mount_path(
            {"runtime_allocation_relative": "../../etc"},
            workspace_root,
        )
        is None
    )
    assert (
        _host_project_mount_path(
            {"runtime_allocation_relative": relative}, Path("relative-workspaces")
        )
        is None
    )


def test_flow_run_terminal_uses_global_project_root_for_shared_runtime(monkeypatch) -> None:
    flow_run_id = "12345678-1234-4234-9234-123456789abc"
    monkeypatch.setattr(
        runtime_operations,
        "active_flow_run_runtime_connection",
        lambda _db, *, flow_run_id: SimpleNamespace(
            flow_run_id=flow_run_id,
            resource_name="runtime-resource",
            managed_runtime_id="managed-runtime",
            project_record_id=None,
        ),
    )
    assert flow_run_terminal_details(object(), flow_run_id) == (
        "runtime-resource",
        "managed-runtime",
        "/runtime/workspace/project",
    )


def test_flow_run_terminal_uses_record_root_for_record_mounted_runtime(monkeypatch) -> None:
    flow_run_id = "12345678-1234-4234-9234-123456789abc"
    monkeypatch.setattr(
        runtime_operations,
        "active_flow_run_runtime_connection",
        lambda _db, *, flow_run_id: SimpleNamespace(
            flow_run_id=flow_run_id,
            resource_name="runtime-resource",
            managed_runtime_id="managed-runtime",
            project_record_id=flow_run_id,
        ),
    )

    assert flow_run_terminal_details(object(), flow_run_id) == (
        "runtime-resource",
        "managed-runtime",
        f"/runtime/workspace/{flow_run_id}",
    )
