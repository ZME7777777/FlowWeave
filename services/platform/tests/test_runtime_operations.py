from pathlib import Path, PurePosixPath
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


def test_flow_run_terminal_materializes_its_record_directory_before_attach(
    monkeypatch,
) -> None:
    flow_run_id = "12345678-1234-4234-9234-123456789abc"
    monkeypatch.setattr(
        runtime_operations,
        "active_flow_run_runtime_connection",
        lambda _db, *, flow_run_id: SimpleNamespace(
            flow_run_id=flow_run_id,
            resource_name="runtime-resource",
            managed_runtime_id="managed-runtime",
        ),
    )
    created_records: list[str] = []
    monkeypatch.setattr(
        runtime_operations,
        "ensure_flow_run_record_workspace",
        lambda _db, record_id: (
            created_records.append(record_id),
            PurePosixPath("/runtime/workspace/project", record_id),
        )[1],
    )

    assert flow_run_terminal_details(object(), flow_run_id) == (
        "runtime-resource",
        "managed-runtime",
        f"/runtime/workspace/project/{flow_run_id}",
    )
    assert created_records == [flow_run_id]
