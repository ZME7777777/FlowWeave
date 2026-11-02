"""Transactional source identity, tenant isolation and frozen coordinator routing."""

from concurrent.futures import ThreadPoolExecutor
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select, text
from sqlalchemy.exc import ProgrammingError
from test_case_flow_tools import flow_case  # noqa: F401
from test_case_native_tools import native_case  # noqa: F401

from flowweave.modules.cases.application import event_inbox
from flowweave.modules.cases.infrastructure.models import (
    CaseAgentTask,
    CaseControlRound,
    CaseExecutionEvent,
    CaseFlowRun,
)
from flowweave.modules.orchestration.application import service as orchestration
from flowweave.modules.runs.infrastructure.models import (
    ArtifactVersion,
    CandidateOutputSet,
    NodeAttempt,
    NodeRun,
    RunEvent,
)
from flowweave.modules.users.application.security import tenant_user
from flowweave.shared.schemas import ArtifactWrite, RejectWrite


@pytest.fixture
def event_case(flow_case):  # noqa: F811
    g = flow_case
    g.entry.state = "STARTED"
    g.db.commit()
    g.node = NodeRun(flow_run_id=g.execution.id, flow_node_snapshot_key="first", sequence_no=1)
    g.db.add(g.node)
    g.db.flush()
    g.attempt = NodeAttempt(
        node_run_id=g.node.id,
        attempt_no=1,
        snapshot_id=g.execution.active_snapshot_id,
        state="WAITING_ACCEPTANCE",
    )
    g.db.add(g.attempt)
    g.db.commit()
    return g


def emit(g, event_type="RUNTIME_COMPLETION_PROJECTED", *, run_id=None, payload=None):
    orchestration._event(
        g.db,
        run_id or g.execution.id,
        event_type,
        payload or {},
        g.node.id,
        g.attempt.id,
    )
    return g.db.scalar(select(RunEvent).order_by(RunEvent.cursor.desc()).limit(1))


def inbox(g):
    return list(
        g.db.scalars(select(CaseExecutionEvent).order_by(CaseExecutionEvent.run_event_cursor))
    )


def test_producer_commit_and_rollback_are_atomic(event_case, db_session_factory):
    g = event_case
    event = emit(g)
    cursor = event.cursor
    assert len(inbox(g)) == 1
    # A separate connection cannot observe either uncommitted source or inbox.
    with tenant_user(g.owner), db_session_factory() as reader:
        assert reader.get(RunEvent, cursor) is None
        assert reader.scalar(select(func.count()).select_from(CaseExecutionEvent)) == 0
    g.db.rollback()
    assert g.db.get(RunEvent, cursor) is None and inbox(g) == []
    event = emit(g)
    cursor = event.cursor
    g.db.commit()
    with tenant_user(g.owner), db_session_factory() as reader:
        assert reader.get(RunEvent, cursor) is not None
        saved = reader.scalar(select(CaseExecutionEvent))
        assert saved.run_event_cursor == cursor
        assert saved.binding_id == g.binding.id and saved.round_id == g.entry.id
        assert saved.case_row_version == g.item.row_version
        assert saved.routing_state == "PENDING"


def test_distinct_formal_cursors_are_not_collapsed_by_type_or_attempt(event_case):
    g = event_case
    first = emit(g, "RUNTIME_FAILED")
    second = emit(g, "RUNTIME_FAILED")
    g.db.commit()
    event_inbox.record_case_execution_event(g.db, first)
    event_inbox.record_case_execution_event(g.db, second)
    g.db.commit()
    assert [entry.run_event_cursor for entry in inbox(g)] == [first.cursor, second.cursor]
    # Legacy operation records use these same primary keys, not type + attempt.
    tasks = list(g.db.scalars(select(CaseAgentTask)))
    assert {task.task_key for task in tasks} == {f"event:{first.cursor}", f"event:{second.cursor}"}


def test_concurrent_replay_keeps_one_frozen_recipient(event_case, db_session_factory):
    g = event_case
    event = RunEvent(
        flow_run_id=g.execution.id,
        node_run_id=g.node.id,
        attempt_id=g.attempt.id,
        event_type="RUNTIME_FAILED",
    )
    g.db.add(event)
    g.db.commit()
    cursor = event.cursor

    def replay(_):
        with tenant_user(g.owner), db_session_factory() as db:
            event_inbox.record_case_execution_event(db, db.get(RunEvent, cursor))
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(replay, range(2)))
    saved = inbox(g)
    assert len(saved) == 1
    original = saved[0]
    next_round = CaseControlRound(
        case_id=g.item.id,
        workspace_id=g.entry.workspace_id,
        work_directory_id=g.entry.work_directory_id,
        binding_id=str(uuid4()),
        idempotency_key="new-round",
        state="STARTED",
        initial_prompt="new objective review",
    )
    g.db.add(next_round)
    g.db.flush()
    g.item.current_control_round_id = next_round.id
    g.item.control_conversation_binding_id = next_round.binding_id
    g.item.row_version += 1
    g.db.commit()
    event_inbox.record_case_execution_event(g.db, event)
    g.db.commit()
    g.db.refresh(original)
    assert original.round_id == g.entry.id and original.binding_id == g.binding.id
    assert original.case_row_version < g.item.row_version
    assert len(inbox(g)) == 1


@pytest.mark.parametrize(
    ("state", "round_state", "routing", "reason"),
    [
        ("ACTIVE", "STARTED", "PENDING", None),
        ("PAUSED", "STARTED", "HELD", "CASE_PAUSED"),
        ("COMPLETED", "STARTED", "AUDIT_ONLY", "CASE_TERMINAL"),
        ("ARCHIVED", "STARTED", "AUDIT_ONLY", "CASE_TERMINAL"),
        ("DRAFT", "STARTED", "AUDIT_ONLY", "CASE_NOT_ACTIVE"),
        ("ACTIVE", "PENDING", "AUDIT_ONLY", "COORDINATOR_UNAVAILABLE"),
        ("ACTIVE", "FAILED", "AUDIT_ONLY", "COORDINATOR_UNAVAILABLE"),
    ],
)
def test_occurrence_routing_is_explicit(event_case, state, round_state, routing, reason):
    g = event_case
    g.item.state, g.entry.state = state, round_state
    emit(g)
    g.db.commit()
    saved = inbox(g)[0]
    assert saved.routing_state == routing and saved.routing_reason == reason
    assert saved.case_state == state


def test_missing_or_mismatched_coordinator_is_audit_only(event_case):
    g = event_case
    g.item.control_conversation_binding_id = None
    emit(g)
    g.item.current_control_round_id = None
    emit(g)
    g.db.commit()
    assert all(entry.routing_state == "AUDIT_ONLY" for entry in inbox(g))
    assert inbox(g)[1].round_id is None and inbox(g)[1].binding_id is None


def test_templates_links_historical_executions_and_noise_do_not_enqueue(event_case):
    g = event_case
    g.db.add(CaseFlowRun(case_id=g.other.id, flow_run_id=g.execution.id))
    g.db.commit()
    emit(g)
    assert {entry.case_id for entry in inbox(g)} == {g.item.id}
    for event_type in (
        "TOKEN_DELTA",
        "HEARTBEAT",
        "ATTEMPT_EXECUTING",
        "STEPWISE_NODE_DRAFT_SAVED",
        "AUTOMATIC_RUN_DRAFT_UPDATED",
    ):
        emit(g, event_type)
    emit(g, run_id=g.template.id)
    g.stage.current_execution_flow_run_id = None
    emit(g)
    g.db.commit()
    assert len(inbox(g)) == 1


def test_formal_output_and_acceptance_remain_distinct_facts(event_case):
    g = event_case
    artifact = orchestration._register_artifact(
        g.db,
        g.execution.id,
        orchestration.prepare_artifact(
            ArtifactWrite(
                field_key="report", artifact_type="URL", uri="https://example.test/report"
            )
        ),
        attempt_id=g.attempt.id,
        source="RUNTIME",
        runtime_completion_event_id="native-completion-17",
    )
    candidate = CandidateOutputSet(
        attempt_id=g.attempt.id,
        completion_event_id="native-completion-17",
        artifact_ids_json=[artifact.id],
    )
    g.db.add(candidate)
    g.db.flush()
    g.attempt.current_candidate_output_set_id = candidate.id
    emit(g, payload={"completion_event_id": "native-completion-17"})
    emit(g, "NODE_RUN_COMPLETED")
    g.db.commit()
    registered, finished, accepted = inbox(g)
    assert [entry.fact_type for entry in inbox(g)] == [
        "ARTIFACT_REGISTERED",
        "NODE_EXECUTION_FINISHED",
        "NODE_ACCEPTED",
    ]
    assert registered.artifact_version_id == artifact.id
    assert registered.native_completion_event_id == "native-completion-17"
    assert finished.candidate_output_set_id == candidate.id
    assert finished.native_completion_event_id == "native-completion-17"
    assert accepted.native_completion_event_id is None
    assert accepted.candidate_output_set_id == candidate.id
    assert all(entry.node_run_id == g.node.id for entry in inbox(g))
    assert all(entry.node_key == "first" for entry in inbox(g))
    # Capturing a fact cannot accept the Attempt or start any downstream work.
    assert g.attempt.state == "WAITING_ACCEPTANCE" and g.node.accepted_attempt_id is None


def test_inbox_failure_rolls_back_source_and_artifact(event_case, monkeypatch):
    g = event_case
    original = g.db.scalar

    def fail(statement, *args, **kwargs):
        if getattr(getattr(statement, "table", None), "name", None) == "case_execution_events":
            raise RuntimeError("inbox write failed")
        return original(statement, *args, **kwargs)

    monkeypatch.setattr(g.db, "scalar", fail)
    with pytest.raises(RuntimeError, match="inbox write failed"):
        orchestration._register_artifact(
            g.db,
            g.execution.id,
            orchestration.prepare_artifact(
                ArtifactWrite(
                    field_key="report", artifact_type="URL", uri="https://example.test/result"
                )
            ),
            attempt_id=g.attempt.id,
        )
    g.db.rollback()
    monkeypatch.setattr(g.db, "scalar", original)
    assert inbox(g) == []
    assert g.db.scalar(select(func.count()).select_from(RunEvent)) == 0
    assert g.db.scalar(select(func.count()).select_from(ArtifactVersion)) == 0


def test_cross_run_node_lineage_and_input_artifacts_are_excluded(event_case):
    g = event_case
    source = RunEvent(
        flow_run_id=g.execution.id,
        node_run_id=g.node.id,
        attempt_id=g.attempt.id,
        event_type="RUNTIME_FAILED",
    )
    g.db.add(source)
    g.db.flush()
    g.node.flow_run_id = g.template.id
    g.db.flush()
    event_inbox.record_case_execution_event(g.db, source)
    assert inbox(g) == []
    g.node.flow_run_id = g.execution.id
    orchestration._register_artifact(
        g.db,
        g.execution.id,
        orchestration.prepare_artifact(
            ArtifactWrite(field_key="link", artifact_type="URL", uri="https://example.test/input")
        ),
    )
    g.db.commit()
    assert inbox(g) == []


def test_terminal_fact_is_captured_before_legacy_stage_advancement(event_case):
    g = event_case
    # Preserve current pre-reconciliation scope even if legacy cancellation pauses the Case.
    g.execution.state = "CANCELLED"
    emit(g, "FLOW_RUN_CANCELLED")
    g.db.commit()
    saved = inbox(g)[0]
    assert saved.case_state == "ACTIVE" and saved.routing_state == "PENDING"
    assert g.item.state == "PAUSED"
    # Delivery must recheck this new state (CASE-05B), never trust PENDING alone.


def test_rejection_has_formal_source_identity(event_case, monkeypatch):
    g = event_case
    monkeypatch.setattr(orchestration, "_dispatch_readiness", lambda *args: None)
    monkeypatch.setattr(
        orchestration, "ensure_node_attempt_workspace", lambda *args, **kwargs: "test"
    )
    monkeypatch.setattr(orchestration, "attempt_detail", lambda db, attempt_id: {"id": attempt_id})
    orchestration.reject_attempt(
        g.db,
        g.attempt.id,
        RejectWrite(expected_state_version=g.attempt.state_version, reason="needs evidence"),
        "reject-with-event",
    )
    saved = inbox(g)[0]
    source = g.db.get(RunEvent, saved.run_event_cursor)
    assert saved.fact_type == "NODE_REJECTED" and saved.node_attempt_id == g.attempt.id
    assert source.payload_json["replacement_attempt_id"] != g.attempt.id
    assert source.payload_json["action_id"]
    assert g.attempt.state == "REJECTED"


def test_rls_migration_reversal_and_case_cascade(event_case, db_session_factory):
    g = event_case
    emit(g)
    g.db.commit()
    with tenant_user(str(uuid4())), db_session_factory() as db:
        assert db.scalar(select(CaseExecutionEvent)) is None
    source = Path(__file__).parents[1] / "migrations/versions/0186_case_execution_events.py"
    spec = spec_from_file_location("case_event_inbox_migration", source)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    role = "case_inbox_rls_" + uuid4().hex
    with g.db.bind.connect() as connection:
        connection.execute(text(f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER'))
        connection.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
        connection.execute(text(f'GRANT SELECT, UPDATE ON case_execution_events TO "{role}"'))
        connection.execute(text(f'SET LOCAL ROLE "{role}"'))
        connection.execute(text("SELECT set_config('flowweave.bypass', 'off', true)"))
        connection.execute(text("SELECT set_config('flowweave.user_id', 'other', true)"))
        assert connection.scalar(text("SELECT count(*) FROM case_execution_events")) == 0
        connection.execute(
            text("SELECT set_config('flowweave.user_id', :owner, true)"), {"owner": g.owner}
        )
        assert connection.scalar(text("SELECT count(*) FROM case_execution_events")) == 1
        with pytest.raises(ProgrammingError) as rejected, connection.begin_nested():
            connection.execute(text("UPDATE case_execution_events SET owner_user_id = 'other'"))
        assert rejected.value.orig.sqlstate == "42501"
        connection.rollback()
    # Migration DDL is rolled back after testing both directions.
    g.db.rollback()
    with g.db.bind.connect() as connection:
        with connection.begin():
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                migration.upgrade()
                assert connection.scalar(text("SELECT count(*) FROM case_execution_events")) == 0
                assert connection.scalar(
                    text(
                        "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class "
                        "WHERE relname = 'case_execution_events'"
                    )
                )
            connection.rollback()
    g.db.expire_all()
    assert len(inbox(g)) == 1
    g.db.delete(g.item)
    g.db.commit()
    assert inbox(g) == []
    assert g.db.get(RunEvent, inbox_source := g.db.scalar(select(RunEvent.cursor))) is not None
    assert inbox_source is not None


def test_unrecorded_old_source_cannot_wake_new_coordinator(event_case):
    g = event_case
    event = RunEvent(
        flow_run_id=g.execution.id,
        event_type="RUNTIME_FAILED",
    )
    g.db.add(event)
    g.db.commit()
    entry = CaseControlRound(
        case_id=g.item.id,
        workspace_id=g.entry.workspace_id,
        work_directory_id=g.entry.work_directory_id,
        binding_id=str(uuid4()),
        idempotency_key="after-source",
        state="STARTED",
        initial_prompt="new round",
    )
    g.db.add(entry)
    g.db.flush()
    g.item.current_control_round_id = entry.id
    g.item.control_conversation_binding_id = entry.binding_id
    g.db.commit()
    event_inbox.record_case_execution_event(g.db, event)
    g.db.commit()
    saved = inbox(g)[0]
    assert saved.routing_state == "AUDIT_ONLY"
    assert saved.routing_reason == "COORDINATOR_NEWER_THAN_SOURCE"


def test_failure_does_not_invent_causality_from_previous_candidate(event_case):
    g = event_case
    candidate = CandidateOutputSet(attempt_id=g.attempt.id, completion_event_id="old-native-event")
    g.db.add(candidate)
    g.db.flush()
    g.attempt.current_candidate_output_set_id = candidate.id
    emit(g, "RUNTIME_FAILED")
    g.db.commit()
    saved = inbox(g)[0]
    assert saved.candidate_output_set_id == candidate.id
    assert saved.native_completion_event_id is None


def test_inactive_stage_has_no_delivery_authority_and_old_record_survives_removal(event_case):
    g = event_case
    g.stage.state = "COMPLETED"
    emit(g)
    g.db.commit()
    saved = inbox(g)[0]
    assert saved.routing_state == "AUDIT_ONLY" and saved.routing_reason == "STAGE_NOT_ACTIVE"
    stage_id, round_id = saved.stage_id, saved.round_id
    g.db.delete(g.stage)
    g.db.commit()
    g.db.refresh(saved)
    assert saved.stage_id == stage_id and saved.round_id == round_id


def test_cross_owner_current_execution_cannot_route_a_foreign_event(event_case, db_session_factory):
    from flowweave.modules.cases.infrastructure.models import Case, CaseStage

    g = event_case
    event = emit(g)
    g.stage.current_execution_flow_run_id = None
    g.db.commit()
    with tenant_user(str(uuid4())), db_session_factory() as db:
        foreign = Case(name="private", state="ACTIVE")
        db.add(foreign)
        db.flush()
        db.add(
            CaseStage(
                case_id=foreign.id,
                stage_key="foreign",
                name="private",
                position=0,
                current_execution_flow_run_id=g.execution.id,
                state="ACTIVE",
            )
        )
        db.commit()
        # RunEvent may be visible through a shared Run, but its original owner
        # still cannot confer delivery authority on this user's Case.
        event_inbox.record_case_execution_event(db, event)
        db.commit()
        assert db.scalar(select(CaseExecutionEvent)) is None
    assert len(inbox(g)) == 1
