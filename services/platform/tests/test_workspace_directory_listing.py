from pathlib import Path

import pytest

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
