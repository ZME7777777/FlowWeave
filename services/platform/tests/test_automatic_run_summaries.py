from types import SimpleNamespace

import pytest

from flowweave.modules.orchestration.application import service as orchestration_service


@pytest.fixture(autouse=True)
def database():
    """Query-construction regression checks do not require PostgreSQL."""

    yield


def test_nested_automatic_record_lists_build_the_delete_exclusion_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both rails must use RunEvent's cursor primary key, not a nonexistent id."""

    statements: list[object] = []

    class Db:
        def scalars(self, statement: object) -> tuple[()]:
            statements.append(statement)
            return ()

    parent = SimpleNamespace(id="parent-1")
    monkeypatch.setattr(orchestration_service, "_run", lambda *_args: parent)

    assert orchestration_service.list_nested_automatic_runs(Db(), parent.id) == []
    assert orchestration_service.list_nested_automatic_run_summaries(Db(), parent.id) == []
    assert len(statements) == 2
