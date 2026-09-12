from pathlib import Path

import pytest

from flowweave.modules.agent_workspaces.application import workspace as workspace_service
from flowweave.modules.agent_workspaces.application.workspace import (
    _authorized_directory,
    _decode_directory_cursor,
    _directory_cursor,
    list_directory,
)
from flowweave.shared.errors import DomainError


def test_authorized_directory_accepts_only_the_visible_scope(tmp_path: Path) -> None:
    project = tmp_path / "project"
    visible = project / "visible"
    hidden = project / "hidden"
    visible.mkdir(parents=True)
    hidden.mkdir()

    assert (
        _authorized_directory(
            project,
            "/runtime/workspace/project",
            ("/runtime/workspace/project/visible",),
            "/runtime/workspace/project/visible",
        )
        == visible
    )
    with pytest.raises(DomainError, match="当前工作区范围"):
        _authorized_directory(
            project,
            "/runtime/workspace/project",
            ("/runtime/workspace/project/visible",),
            "/runtime/workspace/project/hidden",
        )


def test_directory_cursor_is_opaque_and_rejects_invalid_values() -> None:
    cursor = _directory_cursor("directory", "src")
    assert _decode_directory_cursor(cursor) == ("directory", "src")
    with pytest.raises(DomainError, match="分页标识无效"):
        _decode_directory_cursor("not-a-cursor")


def test_directory_listing_reads_only_direct_children_and_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    (project / "alpha").mkdir(parents=True)
    (project / "alpha" / "nested.txt").write_text("not a root entry")
    (project / "beta").mkdir()
    (project / "zeta.txt").write_text("root entry")
    runtime_root = "/runtime/workspace/project"

    monkeypatch.setattr(
        "flowweave.modules.agent_workspaces.application.workspace._workspace", lambda *_: None
    )
    monkeypatch.setattr(
        "flowweave.modules.agent_workspaces.application.workspace._working_directory",
        lambda *_args, **_kwargs: (runtime_root, None),
    )
    monkeypatch.setattr(
        "flowweave.modules.agent_workspaces.application.workspace._file_scope_roots",
        lambda *_args, **_kwargs: (runtime_root,),
    )
    monkeypatch.setattr(
        "flowweave.modules.agent_workspaces.application.workspace._project_root",
        lambda *_: project,
    )
    monkeypatch.setattr(
        "flowweave.modules.agent_workspaces.application.workspace._runtime_root",
        lambda *_: runtime_root,
    )

    first = list_directory(None, "workspace", limit=2)
    assert first["parent_path"] == runtime_root
    assert [entry["path"] for entry in first["entries"]] == [
        f"{runtime_root}/alpha",
        f"{runtime_root}/beta",
    ]
    assert first["next_cursor"]

    second = list_directory(None, "workspace", cursor=first["next_cursor"], limit=2)
    assert [entry["path"] for entry in second["entries"]] == [f"{runtime_root}/zeta.txt"]
    assert second["next_cursor"] is None


def test_workspace_details_skips_full_tree_and_git_without_explicit_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = "/runtime/workspace/project"

    monkeypatch.setattr(workspace_service, "_workspace", lambda *_: None)
    monkeypatch.setattr(
        workspace_service, "_working_directory", lambda *_args, **_kwargs: (runtime_root, None)
    )
    monkeypatch.setattr(
        workspace_service,
        "_scope_details",
        lambda *_args, **_kwargs: {"kind": "ROOT", "display_name": "根工作区"},
    )
    monkeypatch.setattr(
        workspace_service, "_file_scope_roots", lambda *_args, **_kwargs: (runtime_root,)
    )
    monkeypatch.setattr(workspace_service, "_project_root", lambda *_: tmp_path)
    monkeypatch.setattr(workspace_service, "_runtime_root", lambda *_: runtime_root)
    monkeypatch.setattr(
        workspace_service.agent_sessions.conversations,
        "terminal_container_details",
        lambda *_: ("runtime", "resource", "sha256:1234567890abcdef"),
    )
    monkeypatch.setattr(
        workspace_service.agent_sessions, "ssh_remote_descriptor", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(
        workspace_service,
        "_scoped_workspace_entries",
        lambda *_args, **_kwargs: pytest.fail("unexpected recursive file scan"),
    )
    monkeypatch.setattr(
        workspace_service,
        "_scope_repositories",
        lambda *_args, **_kwargs: pytest.fail("unexpected Git repository scan"),
    )

    details = workspace_service.details(None, "workspace")

    assert details["files"] == []
    assert details["repositories"] == []
    assert details["runtime"]["container_id"] == "1234567890ab"


def test_full_workspace_index_does_not_trigger_git_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = "/runtime/workspace/project"

    monkeypatch.setattr(workspace_service, "_workspace", lambda *_: None)
    monkeypatch.setattr(
        workspace_service, "_working_directory", lambda *_args, **_kwargs: (runtime_root, None)
    )
    monkeypatch.setattr(
        workspace_service,
        "_scope_details",
        lambda *_args, **_kwargs: {"kind": "ROOT", "display_name": "根工作区"},
    )
    monkeypatch.setattr(
        workspace_service, "_file_scope_roots", lambda *_args, **_kwargs: (runtime_root,)
    )
    monkeypatch.setattr(workspace_service, "_project_root", lambda *_: tmp_path)
    monkeypatch.setattr(workspace_service, "_runtime_root", lambda *_: runtime_root)
    monkeypatch.setattr(
        workspace_service.agent_sessions.conversations,
        "terminal_container_details",
        lambda *_: ("runtime", "resource", "sha256:1234567890abcdef"),
    )
    monkeypatch.setattr(
        workspace_service.agent_sessions, "ssh_remote_descriptor", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(
        workspace_service,
        "_scoped_workspace_entries",
        lambda *_args, **_kwargs: [
            {"path": f"{runtime_root}/readme.md", "kind": "file", "size": 1}
        ],
    )
    monkeypatch.setattr(
        workspace_service,
        "_scope_repositories",
        lambda *_args, **_kwargs: pytest.fail("Git discovery belongs to the Git panel only"),
    )

    details = workspace_service.details(None, "workspace", full_index=True)

    assert details["files"] == [
        {"path": f"{runtime_root}/readme.md", "kind": "file", "size": 1}
    ]
    assert details["repositories"] == []


def test_git_repositories_discovers_metadata_only_from_explicit_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = "/runtime/workspace/project"
    repository = tmp_path / "repository"

    monkeypatch.setattr(workspace_service, "_workspace", lambda *_: None)
    monkeypatch.setattr(
        workspace_service, "_working_directory", lambda *_args, **_kwargs: (runtime_root, None)
    )
    monkeypatch.setattr(
        workspace_service, "_file_scope_roots", lambda *_args, **_kwargs: (runtime_root,)
    )
    monkeypatch.setattr(workspace_service, "_project_root", lambda *_: tmp_path)
    monkeypatch.setattr(workspace_service, "_runtime_root", lambda *_: runtime_root)
    monkeypatch.setattr(
        workspace_service,
        "_scope_repositories",
        lambda *_args, **_kwargs: [(repository, runtime_root)],
    )
    monkeypatch.setattr(
        workspace_service,
        "_repository_details",
        lambda host_path, path: {"path": path, "branch": host_path.name},
    )

    assert workspace_service.git_repositories(None, "workspace") == [
        {"path": runtime_root, "branch": "repository"}
    ]
