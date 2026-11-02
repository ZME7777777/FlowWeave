"""Convert existing UTC cron expressions to China time without moving their slots.

Revision ID: 0158_schedule_china_timezone
Revises: 0157_background_task_aggregate_type_length
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "0158_schedule_china_timezone"
down_revision = "0157_background_task_aggregate_type_length"
branch_labels = None
depends_on = None


def _values(field: str, lower: int, upper: int) -> set[int]:
    # Freeze the five-field grammar here rather than import future application code.
    values: set[int] = set()
    for item in field.split(","):
        base, separator, step_text = item.partition("/")
        if separator and (not step_text.isdigit() or int(step_text) < 1):
            raise ValueError("invalid cron step")
        step = int(step_text) if separator else 1
        if base == "*":
            start, end = lower, upper
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            start, end = int(start_text), int(end_text)
        else:
            start = end = int(base)
        if start < lower or end > upper or start > end:
            raise ValueError("cron value outside field bounds")
        values.update(range(start, end + 1, step))
    if upper == 7 and 7 in values:
        values.remove(7)
        values.add(0)
    return values


def _field(values: set[int], lower: int, upper: int) -> str:
    if values == set(range(lower, upper + 1)):
        return "*"
    return ",".join(str(value) for value in sorted(values))


def _shift_expression(expression: str, offset: int) -> str:
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("cron must have five fields")
    _values(fields[0], 0, 59)
    hours = _values(fields[1], 0, 23)
    days = _values(fields[2], 1, 31)
    months = _values(fields[3], 1, 12)
    weekdays = _values(fields[4], 0, 7)
    carries = {(hour + offset) // 24 for hour in hours}
    calendar_restricted = len(days) != 31 or len(months) != 12
    weekday_restricted = len(weekdays) != 7
    # A single cron cannot generally express a month-end shift, or a weekly
    # rule whose hours move to different dates. Fail transactionally instead
    # of silently changing those schedules or partially migrating the tables.
    if carries != {0} and (calendar_restricted or (weekday_restricted and len(carries) != 1)):
        raise ValueError("calendar rollover cannot be preserved in a single five-field cron")
    if weekday_restricted and carries != {0}:
        carry = next(iter(carries))
        fields[4] = _field({(day + carry) % 7 for day in weekdays}, 0, 6)
    shifted_hours = {(hour + offset) % 24 for hour in hours}
    if shifted_hours != hours:
        fields[1] = _field(shifted_hours, 0, 23)
    result = " ".join(fields)
    if len(result) > 120:
        raise ValueError("converted cron exceeds the 120-character field limit")
    return result


def _migrate_schedules(connection: Connection, offset: int) -> None:
    for table_name in ("flow_run_schedules", "agent_conversation_schedules"):
        table = sa.table(
            table_name,
            sa.column("id", sa.String()),
            sa.column("cron_expression", sa.String()),
            sa.column("row_version", sa.Integer()),
            sa.column("updated_at", sa.DateTime(timezone=True)),
        )
        rows = connection.execute(
            sa.select(table.c.id, table.c.cron_expression)
            .where(table.c.cron_expression.is_not(None))
            .order_by(table.c.id)
        ).all()
        for schedule_id, expression in rows:
            try:
                converted = _shift_expression(expression, offset)
            except ValueError as error:
                raise RuntimeError(
                    f"Cannot convert {table_name} schedule {schedule_id}: {error}"
                ) from error
            connection.execute(
                table.update()
                .where(table.c.id == schedule_id)
                .values(
                    cron_expression=converted,
                    row_version=table.c.row_version + 1,
                    updated_at=sa.func.now(),
                )
            )
    # next_run_at is already an absolute UTC instant. Keep it, including overdue
    # slots; occurrence identity/history and paused state must remain unchanged.


def _run(offset: int) -> None:
    connection = op.get_bind()
    previous_bypass = connection.scalar(sa.text("SELECT current_setting('flowweave.bypass', true)"))
    connection.execute(sa.text("SELECT set_config('flowweave.bypass', 'on', true)"))
    # Serialize with scans and edits across all users for the migration transaction.
    connection.execute(
        sa.text(
            "LOCK TABLE flow_run_schedules, agent_conversation_schedules "
            "IN SHARE ROW EXCLUSIVE MODE"
        )
    )
    _migrate_schedules(connection, offset)
    connection.execute(
        sa.text("SELECT set_config('flowweave.bypass', :value, true)"),
        {"value": previous_bypass or ""},
    )


def upgrade() -> None:
    _run(8)


def downgrade() -> None:
    _run(-8)
