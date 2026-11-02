from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa

from flowweave.modules.agent_sessions.application.schedules import (
    _next_cron_at as agent_next,
)
from flowweave.modules.orchestration.application.service import _next_cron_at as flow_next


@pytest.fixture(autouse=True)
def database() -> None:
    """Only pure scheduling and an isolated in-memory migration fixture are used."""


@pytest.fixture
def migration() -> ModuleType:
    path = Path(__file__).parents[1] / "migrations/versions/0158_schedule_china_timezone.py"
    spec = importlib.util.spec_from_file_location("schedule_china_timezone", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("next_at", [agent_next, flow_next])
@pytest.mark.parametrize(
    ("expression", "after", "expected"),
    [
        ("0 2 * * *", "2026-10-22T17:59:59+00:00", "2026-10-22T18:00:00+00:00"),
        ("0 10 * * *", "2026-10-23T01:59:59+00:00", "2026-10-23T02:00:00+00:00"),
        # Exact matches advance to the next day, including when the input is local.
        ("0 2 * * *", "2026-10-23T02:00:00+08:00", "2026-10-23T18:00:00+00:00"),
        ("0 9 * * 1-5", "2026-10-23T01:00:00+00:00", "2026-10-26T01:00:00+00:00"),
        ("0 0 * * 0", "2026-10-24T15:59:59+00:00", "2026-10-24T16:00:00+00:00"),
        ("0 0 1 * *", "2026-10-31T15:59:59+00:00", "2026-10-31T16:00:00+00:00"),
        ("0 0 1 1 *", "2026-12-31T15:59:59+00:00", "2026-12-31T16:00:00+00:00"),
        ("0 9 1 * 1", "2026-10-25T12:00:00+00:00", "2026-10-26T01:00:00+00:00"),
        ("*/15 9-10 * * *", "2026-10-23T01:07:00+00:00", "2026-10-23T01:15:00+00:00"),
    ],
)
def test_cron_uses_china_calendar_and_returns_utc(next_at, expression, after, expected):
    result = next_at(expression, datetime.fromisoformat(after))
    assert result == datetime.fromisoformat(expected)
    assert result.tzinfo is UTC


@pytest.mark.parametrize(
    ("expression", "converted"),
    [
        ("0 2 * * *", "0 10 * * *"),
        ("0 18 * * *", "0 2 * * *"),
        ("0 2 * * 1-5", "0 10 * * 1-5"),
        ("0 18 * * 1-5", "0 2 * * 2,3,4,5,6"),
        ("0 18 * * 7", "0 2 * * 1"),
        ("0 */6 * * *", "0 2,8,14,20 * * *"),
        ("*/15 * * * *", "*/15 * * * *"),
        ("0 2 1 1 *", "0 10 1 1 *"),
    ],
)
def test_migrated_rules_preserve_original_utc_slots(migration, expression, converted):
    assert migration._shift_expression(expression, 8) == converted
    # Evaluating the old rule in UTC is equivalent to evaluating its unshifted
    # fields eight hours earlier under the new China-calendar parser.
    instant = datetime(2026, 10, 22, 12, tzinfo=UTC)
    for _ in range(8):
        expected = flow_next(expression, instant - timedelta(hours=8)) + timedelta(hours=8)
        assert flow_next(converted, instant) == expected
        roundtrip = migration._shift_expression(converted, -8)
        assert flow_next(roundtrip, instant) == flow_next(expression, instant)
        instant = expected


@pytest.mark.parametrize("expression", ["0 18 31 * *", "0 18 * 12 *", "0 2,18 * * 1-5"])
def test_migration_rejects_rules_that_cannot_be_shifted_exactly(migration, expression):
    with pytest.raises(ValueError, match="calendar rollover"):
        migration._shift_expression(expression, 8)


def test_migration_updates_active_and_paused_schedules_and_rolls_back_errors(migration):
    metadata = sa.MetaData()
    tables = [
        sa.Table(
            name,
            metadata,
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("owner_user_id", sa.String()),
            sa.Column("cron_expression", sa.String(120)),
            sa.Column("row_version", sa.Integer()),
            sa.Column("status", sa.String()),
            sa.Column("next_run_at", sa.DateTime()),
            sa.Column("last_run_at", sa.DateTime()),
            sa.Column("updated_at", sa.DateTime()),
        )
        for name in ("flow_run_schedules", "agent_conversation_schedules")
    ]
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)
    next_run = datetime(2026, 10, 23, 2)
    last_run = datetime(2026, 10, 22, 2)
    with engine.begin() as connection:
        for table in tables:
            connection.execute(
                table.insert(),
                [
                    dict(
                        id="active",
                        owner_user_id="user-1",
                        cron_expression="0 2 * * *",
                        status="ACTIVE",
                        row_version=3,
                        next_run_at=next_run,
                        last_run_at=last_run,
                    ),
                    dict(
                        id="paused",
                        owner_user_id="user-2",
                        cron_expression="0 18 * * *",
                        status="PAUSED",
                        row_version=7,
                        next_run_at=None,
                        last_run_at=last_run,
                    ),
                ],
            )
    with engine.begin() as connection:
        migration._migrate_schedules(connection, 8)
        for table in tables:
            rows = connection.execute(sa.select(table).order_by(table.c.id)).mappings().all()
            assert [(row["cron_expression"], row["row_version"]) for row in rows] == [
                ("0 10 * * *", 4),
                ("0 2 * * *", 8),
            ]
            assert rows[0]["next_run_at"] == next_run
            assert rows[1]["next_run_at"] is None
            assert [row["status"] for row in rows] == ["ACTIVE", "PAUSED"]
            assert [row["last_run_at"] for row in rows] == [last_run, last_run]
        migration._migrate_schedules(connection, -8)
    with engine.begin() as connection:
        connection.execute(
            tables[1]
            .update()
            .where(tables[1].c.id == "paused")
            .values(cron_expression="0 18 31 * *")
        )
    with pytest.raises(RuntimeError, match="Cannot convert"):
        with engine.begin() as connection:
            migration._migrate_schedules(connection, 8)
    with engine.connect() as connection:
        for table in tables:
            row = connection.execute(sa.select(table).where(table.c.id == "active")).one()
            assert row.cron_expression == "0 2 * * *"
            assert row.row_version == 5
    engine.dispose()
