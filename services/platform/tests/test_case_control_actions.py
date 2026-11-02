from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from flowweave.modules.cases.application import service
from flowweave.modules.cases.infrastructure.models import (
    Case,
    CaseControlPlan,
    CaseControlRound,
    CaseDecisionProposal,
    CaseFeedbackDelivery,
    CaseFlowRun,
    CaseStage,
)
from flowweave.modules.runs.infrastructure.models import FlowRun, NodeRun
from flowweave.modules.users.application.security import tenant_user
from flowweave.shared.errors import DomainError
from flowweave.shared.schemas import (
    CaseControlPlanWrite,
    CaseDecisionProposalWrite,
    CaseStageExecutionStartWrite,
    CaseStageLaunchWrite,
)


@pytest.fixture(autouse=True)
def database():
    """Exercise persistent control actions without booting Runtime services."""


@pytest.fixture
def control_db(monkeypatch) -> Iterator[Session]:
    engine = create_engine("sqlite://")
    event.listen(
        engine,
        "connect",
        lambda connection, _: connection.create_function("set_config", 3, lambda _k, v, _l: v),
    )
    for model in (
        Case,
        CaseStage,
        CaseControlPlan,
        CaseControlRound,
        CaseDecisionProposal,
        CaseFlowRun,
        CaseFeedbackDelivery,
        FlowRun,
        NodeRun,
    ):
        model.__table__.create(engine)
    monkeypatch.setattr(service, "write_case_files", lambda *args: None)
    with tenant_user("owner"), Session(engine) as db:
        db.add_all(
            [
                Case(id="case", name="case", state="ACTIVE"),
                CaseStage(
                    id="stage",
                    case_id="case",
                    stage_key="one",
                    name="one",
                    position=0,
                    state="ACTIVE",
                    execution_mode="CONTINUOUS",
                    flow_run_id="template",
                    current_execution_flow_run_id="execution",
                ),
                FlowRun(
                    id="execution",
                    flow_definition_id="flow",
                    run_no=1,
                    name="execution",
                    state="DRAFT",
                    run_mode="AUTOMATIC",
                ),
                CaseFlowRun(case_id="case", flow_run_id="execution"),
            ]
        )
        db.commit()
        yield db
    engine.dispose()


def make_plan(db, *, action="START_EXECUTION", key="start"):
    return service.create_case_control_plan(
        db,
        "case",
        CaseControlPlanWrite(
            stage_id="stage",
            action=action,
            idempotency_key=key,
            reason="test action",
        ),
    )


def failed_start(db):
    proposal = CaseDecisionProposal(
        case_id="case",
        flow_run_id="execution",
        proposal_key="stage-start:stage:execution",
        action="START_STAGE_EXECUTION",
        state="FAILED",
        reason="first failure",
        payload_json={"stage_id": "stage", "execution_mode": "CONTINUOUS"},
        result_json={"code": "PROVIDER_UNAVAILABLE"},
    )
    db.add(proposal)
    db.commit()
    return proposal


def test_failed_stage_decision_cannot_make_plan_successful(control_db, monkeypatch):
    db = control_db
    previous = failed_start(db)
    calls = []
    monkeypatch.setattr(service.orchestration, "start_automatic_run", lambda *a: calls.append(a))
    plan = make_plan(db)
    with pytest.raises(DomainError) as error:
        service.execute_case_control_plan(db, "case", plan["id"])
    assert error.value.code == "PROVIDER_UNAVAILABLE"
    db.expire_all()
    saved = db.get(CaseControlPlan, plan["id"])
    assert saved.state == "FAILED"
    assert saved.result_json["decision_id"] == previous.id
    assert saved.error_code == "PROVIDER_UNAVAILABLE"
    assert calls == []
    with pytest.raises(DomainError):
        service.execute_case_control_plan(db, "case", plan["id"])
    assert calls == []


def test_explicit_retry_creates_linked_decision_and_starts_once(control_db, monkeypatch):
    db = control_db
    calls = []

    def start(db, run_id, payload, key):
        calls.append(key)
        if len(calls) == 1:
            raise DomainError("PROVIDER_UNAVAILABLE", "temporary failure", 503)
        db.get(FlowRun, run_id).state = "ACTIVE"
        return {"id": run_id, "state": "ACTIVE"}

    monkeypatch.setattr(service.orchestration, "start_automatic_run", start)
    original = make_plan(db)
    with pytest.raises(DomainError):
        service.execute_case_control_plan(db, "case", original["id"])
    old_decision = db.get(CaseControlPlan, original["id"]).result_json["decision_id"]
    retry = service.retry_case_control_plan(db, "case", original["id"])
    result = service.execute_case_control_plan(db, "case", retry["id"])
    assert result["state"] == "SUCCEEDED"
    assert result["result"]["decision"]["state"] == "EXECUTED"
    assert result["result"]["decision"]["payload"]["retry_of_proposal_id"] == old_decision
    assert db.get(CaseDecisionProposal, old_decision).state == "FAILED"
    assert db.get(CaseControlPlan, original["id"]).state == "FAILED"
    assert len(calls) == 2
    assert service.execute_case_control_plan(db, "case", retry["id"])["state"] == "SUCCEEDED"
    assert (
        service.start_case_stage_execution(
            db,
            "case",
            "stage",
            CaseStageExecutionStartWrite(),
        )["id"]
        == result["result"]["decision"]["id"]
    )
    assert len(calls) == 2
    assert len(list(db.scalars(select(CaseDecisionProposal)))) == 2


@pytest.mark.parametrize("mode", ["CONTINUOUS", "STEPWISE"])
def test_retry_refuses_partial_execution_instead_of_duplicate_start(control_db, monkeypatch, mode):
    db = control_db
    previous = failed_start(db)
    plan = make_plan(db)
    row = db.get(CaseControlPlan, plan["id"])
    row.state = "FAILED"
    row.error_code = "PROVIDER_UNAVAILABLE"
    row.result_json = {"code": row.error_code, "decision_id": previous.id}
    db.get(CaseStage, "stage").execution_mode = mode
    run = db.get(FlowRun, "execution")
    if mode == "CONTINUOUS":
        run.state = "ACTIVE"
    else:
        run.run_mode = "MANUAL"
        db.add(
            NodeRun(
                id="started-node",
                flow_run_id=run.id,
                flow_node_snapshot_key="first",
                sequence_no=1,
                state="ACTIVE",
                created_from="STEPWISE_DRAFT_START",
            )
        )
    db.commit()
    retry = service.retry_case_control_plan(db, "case", plan["id"])
    calls = []
    monkeypatch.setattr(service.orchestration, "start_automatic_run", lambda *a: calls.append(a))
    monkeypatch.setattr(
        service.orchestration, "start_stepwise_node_draft", lambda *a: calls.append(a)
    )
    with pytest.raises(DomainError) as error:
        service.execute_case_control_plan(db, "case", retry["id"])
    assert error.value.code == "CASE_START_RECONCILIATION_REQUIRED"
    assert db.get(CaseControlPlan, retry["id"]).state == "FAILED"
    assert calls == []


@pytest.mark.parametrize("state", ["PROPOSED", "SUPERSEDED"])
def test_unfinished_stage_decision_is_not_reported_as_success(control_db, state):
    db = control_db
    proposal = failed_start(db)
    proposal.state = state
    db.commit()
    plan = make_plan(db)
    with pytest.raises(DomainError):
        service.execute_case_control_plan(db, "case", plan["id"])
    assert db.get(CaseControlPlan, plan["id"]).state == "FAILED"


@pytest.mark.parametrize(
    "entry",
    [
        "launch",
        "start",
        "create-plan",
        "execute-plan",
        "retry-plan",
        "create-decision",
        "execute-decision",
        "advance",
    ],
)
def test_paused_case_blocks_work_at_every_control_entry(control_db, monkeypatch, entry):
    db = control_db
    plan = make_plan(db)
    proposal = CaseDecisionProposal(
        case_id="case",
        flow_run_id="execution",
        proposal_key="continue",
        action="COPY_AUTOMATIC_RUN",
        state="PROPOSED",
        reason="continue",
        payload_json={},
    )
    db.add(proposal)
    db.get(Case, "case").state = "PAUSED"
    db.commit()
    calls = []
    for name in ("copy_automatic_run_draft", "start_automatic_run", "start_stepwise_node_draft"):
        monkeypatch.setattr(service.orchestration, name, lambda *a: calls.append(a))
    if entry == "advance":
        db.get(CaseStage, "stage").execution_mode = "STEPWISE"
        monkeypatch.setattr(service.orchestration, "run_detail", lambda *a, **k: calls.append(a))
        service.advance_case_stepwise_execution(db, "execution")
    else:
        with pytest.raises(DomainError) as error:
            if entry == "launch":
                service.launch_case_stage(db, "case", "stage", CaseStageLaunchWrite())
            elif entry == "start":
                service.start_case_stage_execution(
                    db, "case", "stage", CaseStageExecutionStartWrite()
                )
            elif entry == "create-plan":
                make_plan(db, key="paused-start")
            elif entry == "execute-plan":
                service.execute_case_control_plan(db, "case", plan["id"])
            elif entry == "retry-plan":
                db.get(CaseControlPlan, plan["id"]).state = "FAILED"
                db.commit()
                service.retry_case_control_plan(db, "case", plan["id"])
            elif entry == "create-decision":
                service.create_case_decision_proposal(
                    db,
                    "case",
                    CaseDecisionProposalWrite(
                        flow_run_id="execution",
                        proposal_key="paused-copy",
                        action="COPY_AUTOMATIC_RUN",
                        reason="copy",
                    ),
                )
            else:
                service.execute_case_decision_proposal(db, "case", proposal.id)
        assert error.value.code == "CASE_PAUSED"
    assert calls == []


def test_paused_case_can_stop_current_execution_and_replay_result(control_db, monkeypatch):
    db = control_db
    db.get(Case, "case").state = "PAUSED"
    db.commit()
    calls = []

    def stop(db, run_id, key):
        calls.append(run_id)
        return {"id": run_id, "state": "CANCELLED"}

    monkeypatch.setattr(service.orchestration, "cancel_run", stop)
    plan = make_plan(db, action="STOP_EXECUTION", key="stop")
    assert service.execute_case_control_plan(db, "case", plan["id"])["state"] == "SUCCEEDED"
    assert service.execute_case_control_plan(db, "case", plan["id"])["state"] == "SUCCEEDED"
    assert calls == ["execution"]


def test_retry_creation_reuses_inflight_retry_and_retries_form_a_failure_chain(
    control_db, monkeypatch
):
    db = control_db
    calls = []

    def fail(db, run_id, payload, key):
        calls.append(key)
        raise DomainError("START_REJECTED", "still blocked", 422)

    monkeypatch.setattr(service.orchestration, "start_automatic_run", fail)
    original = make_plan(db)
    with pytest.raises(DomainError):
        service.execute_case_control_plan(db, "case", original["id"])
    retry = service.retry_case_control_plan(db, "case", original["id"])
    assert service.retry_case_control_plan(db, "case", original["id"])["id"] == retry["id"]
    with pytest.raises(DomainError):
        service.execute_case_control_plan(db, "case", retry["id"])
    second = service.retry_case_control_plan(db, "case", retry["id"])
    with pytest.raises(DomainError):
        service.execute_case_control_plan(db, "case", second["id"])
    decisions = list(
        db.scalars(select(CaseDecisionProposal).order_by(CaseDecisionProposal.created_at))
    )
    assert len(decisions) == 3
    assert decisions[1].payload_json["retry_of_proposal_id"] == decisions[0].id
    assert decisions[2].payload_json["retry_of_proposal_id"] == decisions[1].id
    assert all(decision.state == "FAILED" for decision in decisions)
    assert len(calls) == 3


def test_legacy_failed_plan_without_decision_reference_can_be_retried(control_db, monkeypatch):
    db = control_db
    previous = failed_start(db)
    original = make_plan(db)
    failed = db.get(CaseControlPlan, original["id"])
    failed.state = "FAILED"
    failed.result_json = {"code": "PROVIDER_UNAVAILABLE"}
    db.commit()
    calls = []
    monkeypatch.setattr(
        service.orchestration,
        "start_automatic_run",
        lambda *a: calls.append(a) or {"id": "execution", "state": "ACTIVE"},
    )
    retry = service.retry_case_control_plan(db, "case", failed.id)
    result = service.execute_case_control_plan(db, "case", retry["id"])
    assert result["result"]["decision"]["payload"]["retry_of_proposal_id"] == previous.id
    assert len(calls) == 1


@pytest.mark.parametrize("entry", ["plan", "suggestion"])
def test_paused_case_binding_cannot_bypass_action_guard(control_db, monkeypatch, entry):
    from flowweave.shared.schemas import CaseControlSuggestionWrite

    db = control_db
    case = db.get(Case, "case")
    case.state = "PAUSED"
    db.commit()
    monkeypatch.setattr(
        service,
        "resolve_case_control_binding_scope",
        lambda *a: {
            "case": case,
            "stage": db.get(CaseStage, "stage"),
        },
    )
    with pytest.raises(DomainError) as error:
        if entry == "plan":
            service.create_case_control_plan_for_binding(
                db,
                "binding",
                CaseControlPlanWrite(
                    stage_id="stage",
                    action="START_EXECUTION",
                    idempotency_key="binding-start",
                    reason="start",
                ),
            )
        else:
            service.ingest_case_control_suggestion(
                db,
                "binding",
                CaseControlSuggestionWrite(
                    suggestion_key="start",
                    action="START_EXECUTION",
                    reason="start",
                ),
            )
    assert error.value.code == "CASE_PAUSED"
    assert list(db.scalars(select(CaseControlPlan))) == []


def test_nested_unexecuted_decision_response_is_rejected_defensively(control_db, monkeypatch):
    db = control_db
    monkeypatch.setattr(
        service,
        "start_case_stage_execution",
        lambda *a, **k: {
            "id": "not-executed",
            "state": "PROPOSED",
        },
    )
    plan = make_plan(db)
    with pytest.raises(DomainError) as error:
        service.execute_case_control_plan(db, "case", plan["id"])
    assert error.value.code == "CASE_STAGE_START_NOT_EXECUTED"
    assert db.get(CaseControlPlan, plan["id"]).state == "FAILED"


def test_terminal_case_can_replay_successful_plan_without_dispatch(control_db, monkeypatch):
    db = control_db
    plan = make_plan(db)
    row = db.get(CaseControlPlan, plan["id"])
    row.state = "SUCCEEDED"
    row.result_json = {"decision": {"state": "EXECUTED"}}
    db.get(Case, "case").state = "COMPLETED"
    db.commit()
    calls = []
    monkeypatch.setattr(service.orchestration, "start_automatic_run", lambda *a: calls.append(a))
    assert service.execute_case_control_plan(db, "case", plan["id"])["state"] == "SUCCEEDED"
    assert calls == []


def test_declared_but_unsupported_decision_fails_explicitly(control_db):
    db = control_db
    proposal = CaseDecisionProposal(
        case_id="case",
        flow_run_id="execution",
        proposal_key="unsupported",
        action="REVISE_UNSTARTED_RUNTIME",
        state="PROPOSED",
        reason="not implemented",
    )
    db.add(proposal)
    db.commit()
    with pytest.raises(DomainError) as error:
        service.execute_case_decision_proposal(db, "case", proposal.id)
    assert error.value.code == "ILLEGAL_STATE_TRANSITION"
    assert proposal.state == "FAILED"
    assert proposal.result_json["code"] == "ILLEGAL_STATE_TRANSITION"
