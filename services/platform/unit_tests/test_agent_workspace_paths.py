from pathlib import Path

import pytest

from flowweave.modules.agent_workspaces.application.workspace import _host_path
from flowweave.shared.errors import DomainError


@pytest.mark.parametrize(
    ("relative", "allow_automation"),
    [
        ("reports/daily.html", False),
        (".tmp/preview.html", False),
        ("reports/.tmp/chart.svg", False),
        (".flowweave/automations/task/reports/daily.html", True),
        (".flowweave/automations/task/.tmp/chart.svg", True),
    ],
)
def test_workspace_preview_reads_tmp_and_authorized_automation_files(
    tmp_path: Path, relative: str, allow_automation: bool
):
    report = tmp_path / relative
    report.parent.mkdir(parents=True)
    report.write_text("report")

    result = _host_path(
        tmp_path,
        "/scope",
        f"/scope/{relative}",
        require_file=True,
        allow_automation_workspace=allow_automation,
    )

    assert result.read_text() == "report"


@pytest.mark.parametrize(
    ("relative", "allow_automation"),
    [
        (".tmp/.private/secret", False),
        (".openhands/secret", True),
        (".flowweave/private", True),
        (".flowweave/automations/task/report.html", False),
        (".flowweave/automations/task/.private/secret", True),
        ("../outside", False),
    ],
)
def test_tmp_exception_preserves_hidden_path_and_traversal_rejection(
    tmp_path: Path, relative: str, allow_automation: bool
):
    with pytest.raises(DomainError, match="文件路径不在工作区范围内"):
        _host_path(
            tmp_path,
            "/scope",
            f"/scope/{relative}",
            require_file=True,
            allow_automation_workspace=allow_automation,
        )


def test_tmp_preview_rejects_symlinks_and_parent_directory_escape(tmp_path: Path):
    project = tmp_path / "project"
    temporary = project / ".tmp"
    temporary.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    (temporary / "report.html").symlink_to(outside)
    (temporary / "outside").symlink_to(tmp_path, target_is_directory=True)

    for relative in (".tmp/report.html", ".tmp/outside/outside.txt"):
        with pytest.raises(DomainError, match="文件路径不在工作区范围内"):
            _host_path(project, "/scope", f"/scope/{relative}", require_file=True)
