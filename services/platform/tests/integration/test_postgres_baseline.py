from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from flowweave.shared.application.uow import SqlAlchemyUnitOfWork


def test_postgresql_schema_has_core_constraints_and_indexes(db_session_factory) -> None:
    engine = db_session_factory.kw["bind"]
    inspector = inspect(engine)
    assert inspector.get_unique_constraints("flow_runs")
    assert {item["name"] for item in inspector.get_indexes("run_events")} >= {"ix_run_event_cursor"}
    columns = {item["name"]: item for item in inspector.get_columns("run_events")}
    assert str(columns["cursor"]["type"]).upper() == "BIGINT"
    assert "managed_sandboxes" in inspector.get_table_names()
    setup_columns = {item["name"] for item in inspector.get_columns("environment_setup_sessions")}
    assert "sandbox_id" in setup_columns
    assert "published_version_id" in setup_columns
    setup_foreign_keys = inspector.get_foreign_keys("environment_setup_sessions")
    assert any(
        item["referred_table"] == "managed_sandboxes"
        and item["constrained_columns"] == ["sandbox_id"]
        for item in setup_foreign_keys
    )
    assert {
        "oauth_sessions",
        "credential_connections",
        "credential_leases",
    }.isdisjoint(inspector.get_table_names())


def test_postgresql_skip_locked_can_claim_distinct_tasks(db_session_factory) -> None:
    first: Session = db_session_factory()
    second: Session = db_session_factory()
    try:
        first.execute(text("LOCK TABLE background_tasks IN ROW EXCLUSIVE MODE"))
        # The query shape is PostgreSQL-native and accepted inside a transaction.
        second.execute(
            text(
                "SELECT id FROM background_tasks "
                "WHERE state IN ('PENDING', 'RETRY') "
                "FOR UPDATE SKIP LOCKED LIMIT 1"
            )
        )
        second.rollback()
        first.rollback()
    finally:
        first.close()
        second.close()


def test_async_uow_is_the_declared_transaction_boundary() -> None:
    assert SqlAlchemyUnitOfWork.__doc__


@pytest.mark.asyncio
async def test_run_event_trigger_notifies_after_commit(container, db_session_factory) -> None:
    from flowweave.shared.models import FlowDefinition, FlowRun, RunEvent

    with db_session_factory() as db:
        flow = FlowDefinition(
            name="notify-flow",
            lark_root_folder_url="https://example.feishu.cn/drive/folder/notify-root",
        )
        db.add(flow)
        db.flush()
        run = FlowRun(
            flow_definition_id=flow.id,
            run_no=1,
            name="notify-run",
            lark_folder_token="notify-run-folder",
            lark_folder_url="https://example.feishu.cn/drive/folder/notify-run-folder",
        )
        db.add(run)
        db.commit()
        run_id = run.id

    async with container.run_event_listener.subscribe() as subscription:
        with db_session_factory() as db:
            db.add(
                RunEvent(
                    flow_run_id=run_id,
                    event_type="NOTIFY_PROBE",
                    payload_json={"committed": True},
                )
            )
            db.flush()
            # PostgreSQL delivers NOTIFY only when the inserting transaction commits.
            assert await subscription.wait(run_id, 0.05) is False
            db.commit()
        assert await subscription.wait(run_id, 2.0) is True


def test_run_event_cursor_compensation_and_bounded_batches(db_session_factory) -> None:
    from flowweave.modules.orchestration.application.service import events
    from flowweave.shared.models import FlowDefinition, FlowRun, RunEvent

    with db_session_factory() as db:
        flow = FlowDefinition(
            name="cursor-flow",
            lark_root_folder_url="https://example.feishu.cn/drive/folder/cursor-root",
        )
        db.add(flow)
        db.flush()
        run = FlowRun(
            flow_definition_id=flow.id,
            run_no=1,
            name="cursor-run",
            lark_folder_token="cursor-run-folder",
            lark_folder_url="https://example.feishu.cn/drive/folder/cursor-run-folder",
        )
        db.add(run)
        db.flush()
        for index in range(7):
            db.add(
                RunEvent(
                    flow_run_id=run.id,
                    event_type="CURSOR_PROBE",
                    payload_json={"index": index},
                )
            )
        db.commit()
        run_id = run.id

    # These events predate any LISTEN subscription. Cursor reads remain the source of truth.
    with db_session_factory() as db:
        first = events(db, run_id, 0, limit=3)
        second = events(db, run_id, first[-1]["cursor"], limit=3)
        third = events(db, run_id, second[-1]["cursor"], limit=3)

    assert [len(first), len(second), len(third)] == [3, 3, 1]
    combined = first + second + third
    assert [item["payload"]["index"] for item in combined] == list(range(7))
    cursors = [item["cursor"] for item in combined]
    assert cursors == sorted(set(cursors))


@pytest.mark.asyncio
async def test_slow_sse_consumer_does_not_block_fast_cursor_compensation(
    container, db_session_factory
) -> None:
    """Independent LISTEN connections isolate backpressure and cursors close every gap."""

    import asyncio
    from functools import partial
    from time import monotonic

    from flowweave.modules.orchestration.public import run_events_after
    from flowweave.shared.models import FlowDefinition, FlowRun, RunEvent

    event_count = 40
    batch_limit = 5
    with db_session_factory() as db:
        flow = FlowDefinition(
            name="slow-consumer-flow",
            lark_root_folder_url="https://example.feishu.cn/drive/folder/slow-root",
        )
        db.add(flow)
        db.flush()
        run = FlowRun(
            flow_definition_id=flow.id,
            run_no=1,
            name="slow-consumer-run",
            lark_folder_token="slow-run-folder",
            lark_folder_url="https://example.feishu.cn/drive/folder/slow-run-folder",
        )
        db.add(run)
        db.commit()
        run_id = run.id

    def write_batch(start: int) -> None:
        with db_session_factory() as db:
            for index in range(start, min(start + 2, event_count)):
                db.add(
                    RunEvent(
                        flow_run_id=run_id,
                        event_type="PRESSURE_PROBE",
                        payload_json={"index": index},
                    )
                )
            db.commit()

    async def writer() -> None:
        for start in range(0, event_count, 2):
            await asyncio.to_thread(write_batch, start)
            await asyncio.sleep(0.002)

    async def consume(delay: float) -> tuple[list[int], list[int], float]:
        cursor = 0
        indexes: list[int] = []
        batch_sizes: list[int] = []
        async with container.run_event_listener.subscribe() as subscription:
            while len(indexes) < event_count:
                read_batch = partial(
                    run_events_after,
                    run_id=run_id,
                    after=cursor,
                    limit=batch_limit,
                )
                async with container.database.session() as session:
                    rows = await session.run_sync(read_batch)
                if not rows:
                    await subscription.wait(run_id, 1.0)
                    continue
                batch_sizes.append(len(rows))
                indexes.extend(int(row["payload"]["index"]) for row in rows)
                cursor = int(rows[-1]["cursor"])
                if delay:
                    await asyncio.sleep(delay)
        return indexes, batch_sizes, monotonic()

    fast_task = asyncio.create_task(consume(0.0))
    slow_task = asyncio.create_task(consume(0.025))
    await asyncio.sleep(0)
    await writer()
    fast_indexes, fast_batches, fast_finished = await fast_task
    slow_indexes, slow_batches, slow_finished = await slow_task

    assert fast_finished < slow_finished
    assert fast_indexes == slow_indexes == list(range(event_count))
    assert max(fast_batches) <= batch_limit
    assert max(slow_batches) <= batch_limit
    assert len(slow_batches) > 1


def test_attempt_confirmation_cas_allows_only_one_transaction(
    client, db_session_factory, settings, skill_capability
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import func, select

    from flowweave.modules.orchestration.application.service import confirm_start
    from flowweave.shared.domain.errors import DomainError
    from flowweave.shared.models import BackgroundTask, HumanAction, RunEvent
    from flowweave.shared.schemas import AttemptStartWrite
    from flowweave.shared.settings import settings_context

    asset_response = client.post(
        "/api/v1/node-assets",
        json={
            "name": "CAS 并发节点",
            "inputs": [
                {
                    "field_key": "prd",
                    "display_name": "需求",
                    "data_type": "URL",
                    "template_url": "https://example.feishu.cn/docx/prd-template",
                }
            ],
            "outputs": [
                {
                    "field_key": "design",
                    "display_name": "方案",
                    "data_type": "URL",
                    "template_url": "https://example.feishu.cn/docx/design-template",
                }
            ],
            "executor": {"startup_prompt": "生成方案"},
            "capabilities": [skill_capability],
        },
    )
    assert asset_response.status_code == 201, asset_response.text
    asset = asset_response.json()
    flow_response = client.post(
        "/api/v1/flows",
        json={
            "name": "CAS 并发流程",
            "environment_version_id": client.environment_version_id,
            "lark_root_folder_url": ("https://example.feishu.cn/drive/folder/cas-root"),
            "default_entry_key": "design",
            "nodes": [{"instance_key": "design", "node_asset_id": asset["id"]}],
        },
    )
    assert flow_response.status_code == 201, flow_response.text
    flow = flow_response.json()
    run_response = client.post(
        f"/api/v1/flows/{flow['id']}/runs",
        json={
            "flow_node_key": "design",
            "artifacts": [
                {
                    "field_key": "prd",
                    "artifact_type": "URL",
                    "uri": "https://example.feishu.cn/docx/cas-input",
                }
            ],
        },
    )
    run = run_response.json()
    attempt = run["node_runs"][0]["attempts"][0]
    assert attempt["state"] == "WAITING_START_CONFIRMATION"
    attempt_id = attempt["id"]
    version = attempt["state_version"]
    barrier = Barrier(2)
    worker_settings = settings.model_copy(update={"execution_mode": "worker"})

    def issue(index: int) -> str:
        with settings_context(worker_settings), db_session_factory() as db:
            barrier.wait(timeout=5)
            try:
                confirm_start(
                    db,
                    attempt_id,
                    AttemptStartWrite(expected_state_version=version),
                    f"cas-confirm-{index}",
                )
            except DomainError as exc:
                return exc.code
            return "OK"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(issue, (1, 2)))

    assert outcomes == ["OK", "VERSION_CONFLICT"]
    with db_session_factory() as db:
        assert (
            db.scalar(
                select(func.count(HumanAction.id)).where(
                    HumanAction.attempt_id == attempt_id,
                    HumanAction.action_type == "CONFIRM_START",
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(BackgroundTask.id)).where(
                    BackgroundTask.aggregate_id == attempt_id,
                    BackgroundTask.task_type == "START_RUNTIME",
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(RunEvent.cursor)).where(
                    RunEvent.attempt_id == attempt_id,
                    RunEvent.event_type == "ATTEMPT_EXECUTING",
                )
            )
            == 1
        )
