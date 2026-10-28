from uuid import uuid4

import pytest
from sqlalchemy import select

from flowweave.modules.cases.application import service
from flowweave.modules.cases.infrastructure.models import (
    Case,
    CaseControlPlan,
    CaseDecisionProposal,
    CaseFlowRun,
    CaseStage,
)
from flowweave.modules.flows.infrastructure.models import FlowDefinition
from flowweave.modules.runs.infrastructure.models import FlowRun
from flowweave.modules.users.application.security import tenant_user
from flowweave.shared.errors import DomainError


@pytest.fixture
def case_execution(client, db_session_factory):
    owner = client.get("/api/v1/auth/me").json()["id"]
    with tenant_user(owner), db_session_factory() as db:
        definition = FlowDefinition(name="case action fixture")
        case = Case(name="control fixture", state="ACTIVE")
        db.add_all([definition, case])
        db.flush()
        run = FlowRun(
            flow_definition_id=definition.id,
            run_no=1,
            name="execution",
            run_mode="AUTOMATIC",
            state="DRAFT",
        )
        db.add(run)
        db.flush()
        stage = CaseStage(
            case_id=case.id,
            stage_key="one",
            name="one",
            position=0,
            state="ACTIVE",
            execution_mode="CONTINUOUS",
            current_execution_flow_run_id=run.id,
        )
        db.add_all([stage, CaseFlowRun(case_id=case.id, flow_run_id=run.id)])
        db.commit()
        return {"owner": owner, "case": case.id, "stage": stage.id, "run": run.id}


def create_plan(client, fixture, *, action="START_EXECUTION"):
    response = client.post(
        f"/api/v1/cases/{fixture['case']}/control-plans",
        json={
            "stage_id": fixture["stage"],
            "action": action,
            "idempotency_key": f"test:{uuid4()}",
            "reason": "test control action",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_http_failure_survives_request_rollback_and_retry_is_a_new_audited_attempt(
    client,
    case_execution,
    db_session_factory,
    monkeypatch,
):
    fixture = case_execution
    calls = []

    def start(db, run_id, payload, key):
        calls.append(key)
        if len(calls) == 1:
            raise DomainError("PROVIDER_UNAVAILABLE", "temporary failure", 503)
        db.get(FlowRun, run_id).state = "ACTIVE"
        return {"id": run_id, "state": "ACTIVE"}

    monkeypatch.setattr(service.orchestration, "start_automatic_run", start)
    plan = create_plan(client, fixture)
    path = f"/api/v1/cases/{fixture['case']}/control-plans"
    failed = client.post(f"{path}/{plan['id']}/execute")
    assert failed.status_code == 503, failed.text
    assert failed.json()["error"]["code"] == "PROVIDER_UNAVAILABLE"
    with tenant_user(fixture["owner"]), db_session_factory() as db:
        stored = db.get(CaseControlPlan, plan["id"])
        assert stored.state == "FAILED"
        decision_id = stored.result_json["decision_id"]
        assert db.get(CaseDecisionProposal, decision_id).state == "FAILED"
    repeated = client.post(f"{path}/{plan['id']}/execute")
    assert repeated.status_code == 409
    assert len(calls) == 1
    retry = client.post(f"{path}/{plan['id']}/retry")
    assert retry.status_code == 201, retry.text
    assert retry.json()["retry_of_plan_id"] == plan["id"]
    result = client.post(f"{path}/{retry.json()['id']}/execute")
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "SUCCEEDED"
    assert result.json()["result"]["decision"]["payload"]["retry_of_proposal_id"] == decision_id
    assert client.post(f"{path}/{retry.json()['id']}/execute").json()["state"] == "SUCCEEDED"
    assert len(calls) == 2
    with tenant_user(fixture["owner"]), db_session_factory() as db:
        assert db.get(CaseControlPlan, plan["id"]).state == "FAILED"
        assert db.get(CaseDecisionProposal, decision_id).state == "FAILED"
        assert len(list(db.scalars(select(CaseDecisionProposal)))) == 2
        assert db.get(FlowRun, fixture["run"]).state == "ACTIVE"


@pytest.mark.parametrize("error_kind", ["domain", "unexpected"])
def test_direct_stage_start_failure_is_persisted_and_never_replayed_as_success(
    client,
    case_execution,
    db_session_factory,
    monkeypatch,
    error_kind,
):
    fixture = case_execution
    calls = []

    def start(*args):
        calls.append("start")
        if error_kind == "domain":
            raise DomainError("START_REJECTED", "start rejected", 422)
        raise RuntimeError("private failure details")

    monkeypatch.setattr(service.orchestration, "start_automatic_run", start)
    path = f"/api/v1/cases/{fixture['case']}/stages/{fixture['stage']}/execution/start"
    first = client.post(path, json={})
    assert first.status_code == (422 if error_kind == "domain" else 500)
    assert "private failure details" not in first.text
    second = client.post(path, json={})
    assert second.status_code == 409
    assert calls == ["start"]
    with tenant_user(fixture["owner"]), db_session_factory() as db:
        rows = list(db.scalars(select(CaseDecisionProposal)))
        assert len(rows) == 1 and rows[0].state == "FAILED"


def test_http_paused_case_rejects_pending_start_but_can_stop_current_run(
    client,
    case_execution,
    db_session_factory,
    monkeypatch,
):
    fixture = case_execution
    plan = create_plan(client, fixture)
    with tenant_user(fixture["owner"]), db_session_factory() as db:
        db.get(Case, fixture["case"]).state = "PAUSED"
        db.commit()
    calls = []
    monkeypatch.setattr(
        service.orchestration, "start_automatic_run", lambda *a: calls.append("start")
    )
    path = f"/api/v1/cases/{fixture['case']}/control-plans"
    blocked = client.post(f"{path}/{plan['id']}/execute")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "CASE_PAUSED"
    assert calls == []
    with tenant_user(fixture["owner"]), db_session_factory() as db:
        assert db.get(CaseControlPlan, plan["id"]).state == "PENDING"
    stop = create_plan(client, fixture, action="STOP_EXECUTION")

    def cancel(db, run_id, key):
        calls.append("stop")
        db.get(FlowRun, run_id).state = "CANCELLED"
        return {"id": run_id, "state": "CANCELLED"}

    monkeypatch.setattr(service.orchestration, "cancel_run", cancel)
    stopped = client.post(f"{path}/{stop['id']}/execute")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["state"] == "SUCCEEDED"
    assert calls == ["stop"]


def test_control_actions_are_not_visible_to_another_user(
    client,
    user_client,
    case_execution,
    db_session_factory,
):
    fixture = case_execution
    plan = create_plan(client, fixture)
    path = f"/api/v1/cases/{fixture['case']}"
    for action in ("execute", "retry"):
        response = user_client.post(f"{path}/control-plans/{plan['id']}/{action}")
        assert response.status_code == 404, response.text
    response = user_client.post(f"{path}/stages/{fixture['stage']}/execution/start", json={})
    assert response.status_code == 404, response.text
    with tenant_user(fixture["owner"]), db_session_factory() as db:
        assert db.get(CaseControlPlan, plan["id"]).state == "PENDING"
        assert list(db.scalars(select(CaseDecisionProposal))) == []


def test_postgres_concurrent_stage_starts_share_one_decision(
    case_execution,
    db_session_factory,
    monkeypatch,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from time import sleep

    from flowweave.shared.schemas import CaseStageExecutionStartWrite

    fixture = case_execution
    barrier = Barrier(2)
    calls = []

    def start(db, run_id, payload, key):
        calls.append(key)
        sleep(0.05)
        db.get(FlowRun, run_id).state = "ACTIVE"
        return {"id": run_id, "state": "ACTIVE"}

    monkeypatch.setattr(service.orchestration, "start_automatic_run", start)

    def submit():
        with tenant_user(fixture["owner"]), db_session_factory() as db:
            barrier.wait(timeout=5)
            return service.start_case_stage_execution(
                db,
                fixture["case"],
                fixture["stage"],
                CaseStageExecutionStartWrite(),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert results[0]["id"] == results[1]["id"]
    assert all(result["state"] == "EXECUTED" for result in results)
    assert len(calls) == 1
    with tenant_user(fixture["owner"]), db_session_factory() as db:
        assert len(list(db.scalars(select(CaseDecisionProposal)))) == 1
