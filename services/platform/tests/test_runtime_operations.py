from pathlib import Path

from flowweave.modules.sandboxes.application.runtime_operations import _host_project_mount_path


def test_host_project_mount_path_uses_only_canonical_flow_run_allocation(tmp_path: Path) -> None:
    relative = ".flow-run-runtimes/" + "a" * 32 + "/12345678-1234-4234-9234-123456789abc"
    workspace_root = tmp_path / "workspaces"

    assert _host_project_mount_path(
        {"runtime_allocation_relative": relative}, workspace_root
    ) == str(workspace_root / relative / "workspace/project")
    assert _host_project_mount_path(
        {"runtime_allocation_relative": "../../etc"},
        workspace_root,
    ) is None
    assert _host_project_mount_path(
        {"runtime_allocation_relative": relative}, Path("relative-workspaces")
    ) is None
