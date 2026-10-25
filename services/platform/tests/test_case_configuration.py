from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from flowweave.modules.cases.application import service
from flowweave.modules.cases.infrastructure.models import (
    Case,
    CaseAgentTask,
    CaseControlPlan,
    CaseDecisionProposal,
    CaseFlowRun,
    CaseStage,
)
from flowweave.modules.runs.infrastructure.models import FlowRun
from flowweave.modules.users.application.security import tenant_user
from flowweave.shared.errors import DomainError
from flowweave.shared.schemas import (
    CaseCreateWrite,
    CasePathWrite,
    CaseSessionConfigWrite,
    CaseUpdateWrite,
)


@pytest.fixture(autouse=True)
def database():
    """Exercise the control plane with SQLite, without starting a Runtime or Docker."""


@pytest.fixture
def case_db(monkeypatch, tmp_path):
    engine = create_engine("sqlite://")
    # PostgreSQL session variables are irrelevant to SQLite; ORM tenant filters remain active.
    event.listen(
        engine,
        "connect",
        lambda connection, _: connection.create_function(
            "set_config", 3, lambda _key, value, _local: value
        ),
    )
    for model in (
        Case,
        CaseStage,
        CaseAgentTask,
        CaseControlPlan,
        CaseDecisionProposal,
        CaseFlowRun,
        FlowRun,
    ):
        model.__table__.create(engine)
    monkeypatch.setattr(
        service.agent_workspaces,
        "ensure_default_agent_workspace",
        lambda db: SimpleNamespace(id="workspace"),
    )

    def create_directory(db, workspace_id, case_id, name):
        (tmp_path / ".flowweave" / "cases" / case_id).mkdir(parents=True)
        return {"id": f"directory-{case_id[:8]}"}

    monkeypatch.setattr(service.agent_workspaces, "create_case_work_directory", create_directory)
    monkeypatch.setattr(service.work_directories, "_project_root", lambda db, workspace: tmp_path)
    monkeypatch.setattr(
        service.work_directories, "get_work_directory", lambda db, workspace, id: {"id": id}
    )
    monkeypatch.setattr(service, "_write_case_files", lambda db, item: None)
    with tenant_user("owner"), Session(engine) as db:
        yield db, tmp_path
    engine.dispose()


def create_case(db):
    return service.create_case(
        db, CaseCreateWrite(name="客户迁移", description="背景说明", objective="完成迁移")
    )


def test_basics_save_independently_and_reject_stale_and_forged_files(case_db):
    db, _ = case_db
    item = create_case(db)
    assert item["stages"] == [] and item["description"] == "背景说明"
    assert item["control_work_directory_id"]
    payload = dict(
        name="新名称",
        description="新说明",
        objective="新目标",
        work_directory_id="existing",
        materials=[{"kind": "LINK", "name": "输入文档", "value": "https://example.test/input"}],
        expected_row_version=item["row_version"],
    )
    updated = service.update_case(db, item["id"], CaseUpdateWrite(**payload))
    assert updated["work_directory_id"] == "existing"
    assert updated["objective"] == "新目标" and updated["description"] == "新说明"
    with pytest.raises(DomainError, match="") as error:
        service.update_case(db, item["id"], CaseUpdateWrite(**payload))
    assert error.value.code == "VERSION_CONFLICT"
    db.rollback()
    payload["expected_row_version"] = updated["row_version"]
    payload["materials"] = [{"kind": "FILE", "name": "私有文件", "value": "../other/private.txt"}]
    with pytest.raises(DomainError) as error:
        service.update_case(db, item["id"], CaseUpdateWrite(**payload))
    assert error.value.code == "CASE_MATERIAL_INVALID"


def test_material_upload_round_trip_and_symlink_boundary(case_db):
    db, root = case_db
    item = create_case(db)
    updated = service.upload_case_material(
        db, item["id"], item["row_version"], "资料.txt", b"input data"
    )
    material = updated["materials"][0]
    assert material["value"].startswith(f".flowweave/cases/{item['id']}/materials/")
    assert service.read_case_material(db, item["id"], material["value"]) == (
        b"input data",
        "资料.txt",
        "text/plain",
    )
    assert service.get_case(db, item["id"])["materials"] == [material]
    renamed = service.update_case(
        db,
        item["id"],
        CaseUpdateWrite(
            expected_row_version=updated["row_version"],
            name=item["name"],
            materials=[{**material, "name": "上线资料.txt"}],
        ),
    )
    assert renamed["materials"][0]["value"] == material["value"]
    assert service.read_case_material(db, item["id"], material["value"]) == (
        b"input data",
        "上线资料.txt",
        "text/plain",
    )
    assert service.get_case(db, item["id"])["materials"][0]["name"] == "上线资料.txt"
    path = root / material["value"]
    path.unlink()
    outside = root / "private.txt"
    outside.write_text("private")
    path.symlink_to(outside)
    with pytest.raises(DomainError):
        service.read_case_material(db, item["id"], material["value"])
    other = create_case(db)
    with pytest.raises(DomainError):
        service.read_case_material(db, other["id"], material["value"])


def test_path_add_reorder_delete_preserves_source_runs_and_rejects_stale(case_db):
    db, _ = case_db
    item = create_case(db)
    for index in range(2):
        db.add(
            FlowRun(
                id=f"run-{index}",
                flow_definition_id="flow",
                parent_flow_run_id="master",
                run_no=index,
                name=f"运行 {index}",
                run_mode="AUTOMATIC",
                automation_plan_json={"start_node_key": "start", "readiness": {"ready": True}},
            )
        )
    db.commit()

    def save(current, cards):
        return service.save_case_path(
            db,
            current["id"],
            CasePathWrite(expected_row_version=current["row_version"], cards=cards),
        )

    added = save(item, [{"flow_run_id": "run-0"}, {"flow_run_id": "run-1"}])
    assert [stage["flow_run_id"] for stage in added["stages"]] == ["run-0", "run-1"]
    assert [stage["state"] for stage in added["stages"]] == ["ACTIVE", "PENDING"]
    moved = save(added, [{"id": stage["id"]} for stage in reversed(added["stages"])])
    assert [stage["flow_run_id"] for stage in moved["stages"]] == ["run-1", "run-0"]
    with pytest.raises(DomainError) as error:
        save(added, [])
    assert error.value.code == "VERSION_CONFLICT"
    db.rollback()
    empty = save(moved, [])
    assert empty["stages"] == []
    assert len(list(db.scalars(select(FlowRun)))) == 2
    assert len(service.list_case_stage_templates(db, item["id"])) == 2


def test_session_configuration_persists_all_selections_and_rejects_unsupported_effort(
    case_db, monkeypatch
):
    db, _ = case_db
    item = create_case(db)
    monkeypatch.setattr(
        service,
        "list_providers",
        lambda db: [
            {
                "id": "provider",
                "available_for_nodes": True,
                "models": [
                    {
                        "model_name": "model",
                        "enabled": True,
                        "supported_reasoning_efforts": ["high"],
                    }
                ],
            }
        ],
    )
    monkeypatch.setattr(
        service,
        "validate_template_configuration",
        lambda db, caps, credentials: (caps, credentials),
    )
    payload = CaseSessionConfigWrite(
        expected_row_version=item["row_version"],
        model_provider_id="provider",
        model_name="model",
        reasoning_effort="high",
        capability_version_ids=["skill-v2"],
        credential_ids=["credential"],
    )
    result = service.save_case_session_config(db, item["id"], payload)
    assert result["session_config"] == payload.model_dump(exclude={"expected_row_version"})
    assert service.get_case(db, item["id"])["session_config"] == result["session_config"]
    with pytest.raises(DomainError) as error:
        service.save_case_session_config(
            db,
            item["id"],
            payload.model_copy(
                update={"expected_row_version": result["row_version"], "reasoning_effort": "low"}
            ),
        )
    assert error.value.code == "CASE_REASONING_INVALID"


def test_case_schema_rejects_blank_names():
    with pytest.raises(ValidationError):
        CaseCreateWrite(name="   ")


@pytest.mark.parametrize(
    "value",
    [
        "https://example.test/doc",
        "feishu://docs/item",
        "ftp://example.test/input",
        "intranet/wiki/page",
        "javascript:alert(1)",
    ],
)
def test_case_material_keeps_reference_text_without_url_type_validation(case_db, value):
    db, _ = case_db
    created = service.create_case(
        db,
        CaseCreateWrite(
            name="资料地址", materials=[{"kind": "LINK", "name": "输入", "value": value}]
        ),
    )
    assert service.get_case(db, created["id"])["materials"] == [
        {"kind": "LINK", "name": "输入", "value": value}
    ]


def test_case_and_path_candidates_are_isolated_by_user(case_db):
    db, _ = case_db
    item = create_case(db)
    db.add(
        FlowRun(
            id="private-run",
            parent_flow_run_id="master",
            flow_definition_id="flow",
            run_no=1,
            name="private",
            run_mode="AUTOMATIC",
            automation_plan_json={"start_node_key": "start", "readiness": {"ready": True}},
        )
    )
    db.commit()
    with tenant_user("other"), Session(db.get_bind()) as other_db:
        with pytest.raises(DomainError) as error:
            service.get_case(other_db, item["id"])
        assert error.value.code == "RESOURCE_NOT_FOUND"
        own = create_case(other_db)
        assert service.list_case_stage_templates(other_db, own["id"]) == []


def test_case_configuration_migration_preserves_existing_rows():
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    source = Path(__file__).parents[1] / "migrations/versions/0172_case_configuration.py"
    spec = importlib.util.spec_from_file_location("case_configuration_migration", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE cases (id TEXT PRIMARY KEY, name TEXT NOT NULL)"))
        connection.execute(text("INSERT INTO cases VALUES ('old', 'Existing Case')"))
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
        row = connection.execute(text("SELECT * FROM cases")).mappings().one()
        assert row["name"] == "Existing Case" and row["description"] == ""
        assert row["materials_json"] == "[]" and row["session_config_json"] == "{}"
        assert row["work_directory_id"] is None
        with Operations.context(MigrationContext.configure(connection)):
            module.downgrade()
        assert connection.execute(text("SELECT name FROM cases")).scalar_one() == "Existing Case"
    engine.dispose()
