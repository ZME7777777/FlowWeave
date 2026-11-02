from sqlalchemy import select

from flowweave.modules.orchestration.application import service
from flowweave.modules.runs.infrastructure.models import (
    FlowRun,
    FlowRunSchedule,
    FlowRunScheduleOccurrence,
    NodeAttempt,
    NodeRun,
)
from flowweave.modules.tasks.infrastructure.models import BackgroundTask
from flowweave.shared.artifact_store import artifact_store_context
from flowweave.shared.domain.enums import AttemptState, FlowRunState, NodeRunState
from flowweave.shared.settings import settings_context


def test_deleting_schedule_queues_existing_record_cleanup_and_final_removal(
    db_session_factory,
    container,
) -> None:
    with (
        settings_context(container.settings),
        artifact_store_context(container.artifact_store),
        db_session_factory() as db,
    ):
        parent = FlowRun(
            flow_definition_id="flow-1",
            run_no=1,
            name="父 FlowRun",
            run_mode="MANUAL",
            state=FlowRunState.ACTIVE,
        )
        schedule = FlowRunSchedule(
            flow_definition_id="flow-1",
            environment_version_id="environment-1",
            source_flow_run_id="template-record-1",
            name="每小时检查",
            run_mode="AUTOMATIC",
            start_node_key="first",
            interval_minutes=1,
            cron_expression="0 * * * *",
        )
        db.add_all([parent, schedule])
        db.flush()
        run = FlowRun(
            flow_definition_id="flow-1",
            run_no=1,
            name="每小时检查 · 2026-10-24 12:00",
            run_mode="AUTOMATIC",
            parent_flow_run_id=parent.id,
            schedule_id=schedule.id,
            state=FlowRunState.DRAFT,
        )
        db.add(run)
        db.flush()
        occurrence = FlowRunScheduleOccurrence(
            schedule_id=schedule.id,
            config_version=1,
            trigger_kind="MANUAL",
            state="STARTED",
            flow_run_id=run.id,
        )
        node_run = NodeRun(
            flow_run_id=run.id,
            flow_node_snapshot_key="first",
            sequence_no=1,
            name="节点一",
            state=NodeRunState.CANCELLED,
        )
        db.add_all([occurrence, node_run])
        db.flush()
        attempt = NodeAttempt(
            node_run_id=node_run.id,
            attempt_no=1,
            snapshot_id="snapshot-1",
            state=AttemptState.CANCELLED,
            runtime_phase="CANCELLED",
        )
        db.add(attempt)
        db.commit()

        service.delete_flow_run_schedule(db, schedule.id)

        assert db.get(FlowRunSchedule, schedule.id).status == "PAUSED"
        assert (
            db.scalar(
                select(BackgroundTask).where(
                    BackgroundTask.task_type == "DELETE_AUTOMATIC_RUN_RECORD",
                    BackgroundTask.aggregate_id == run.id,
                )
            )
            is not None
        )
        assert (
            db.scalar(
                select(BackgroundTask).where(
                    BackgroundTask.task_type == "DELETE_FLOW_RUN_SCHEDULE",
                    BackgroundTask.aggregate_id == schedule.id,
                )
            )
            is not None
        )

        service.process_delete_automatic_run_record(db, run.id, parent.id)
        service.process_delete_flow_run_schedule(db, schedule.id)

        assert db.get(FlowRunSchedule, schedule.id) is None
        assert db.get(FlowRunScheduleOccurrence, occurrence.id) is None
        assert db.get(FlowRun, run.id) is None
        assert db.get(NodeRun, node_run.id) is None
        assert db.get(NodeAttempt, attempt.id) is None
        assert db.scalar(select(FlowRun).where(FlowRun.id == parent.id)) is not None
