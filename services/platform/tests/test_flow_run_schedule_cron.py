from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from flowweave.modules.orchestration.application.service import _next_cron_at
from flowweave.shared.schemas import FlowRunScheduleWrite


def test_schedule_write_only_accepts_a_record_master_and_cron() -> None:
    payload = FlowRunScheduleWrite(
        name=" hourly check ",
        source_flow_run_id="a" * 36,
        cron_expression=" 0   *  * * * ",
    )

    assert payload.name == "hourly check"
    assert payload.cron_expression == "0 * * * *"
    with pytest.raises(ValidationError):
        FlowRunScheduleWrite(
            name="old",
            source_flow_run_id="a" * 36,
            cron_expression="0 * * * *",
            interval_minutes=60,
        )  # type: ignore[call-arg]


def test_schedule_cron_advances_to_the_next_utc_slot() -> None:
    after = datetime(2026, 9, 6, 10, 25, 30, tzinfo=UTC)

    assert _next_cron_at("0 */2 * * *", after) == datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    assert _next_cron_at("*/15 * * * *", after) == datetime(2026, 9, 6, 10, 30, tzinfo=UTC)


def test_schedule_cron_rejects_non_standard_or_impossible_fields() -> None:
    after = datetime(2026, 9, 6, tzinfo=UTC)

    with pytest.raises(ValueError, match="five"):
        _next_cron_at("every hour", after)
    with pytest.raises(ValueError, match="outside"):
        _next_cron_at("61 * * * *", after)
