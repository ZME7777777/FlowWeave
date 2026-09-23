import pytest

from flowweave.bootstrap.api import _rate_limit_policy
from flowweave.bootstrap.settings import Settings
from flowweave.shared.observability import Metrics, RateLimiter


@pytest.fixture(autouse=True)
def database() -> None:
    pass


def test_rate_limit_policy_separates_reads_actions_and_messages() -> None:
    settings = Settings(
        rate_limit_read_requests_per_minute=600,
        rate_limit_user_requests_per_minute=120,
        rate_limit_conversation_messages_per_minute=20,
    )

    read = _rate_limit_policy("GET", "/api/v1/node-assets", "user-1", settings)
    action = _rate_limit_policy("POST", "/api/v1/node-assets", "user-1", settings)
    message = _rate_limit_policy(
        "POST",
        "/api/v1/agent-workspaces/workspace-1/conversations/conversation-1/messages",
        "user-1",
        settings,
    )

    assert (read.scope, read.subject, read.limit, read.error_code) == (
        "read",
        "user-1",
        600,
        "READ_RATE_LIMITED",
    )
    assert (action.scope, action.subject, action.limit, action.error_code) == (
        "user",
        "user-1",
        120,
        "RATE_LIMITED",
    )
    assert (message.scope, message.subject, message.limit, message.error_code) == (
        "conversation",
        "user-1:conversation-1",
        20,
        "CONVERSATION_RATE_LIMITED",
    )


def test_rate_limit_policy_recognizes_flow_run_messages() -> None:
    settings = Settings(rate_limit_conversation_messages_per_minute=7)

    policy = _rate_limit_policy(
        "POST",
        "/api/v1/flow-runs/run-1/node-attempts/attempt-1/agent-sessions/session-1/messages/rerun",
        "user-1",
        settings,
    )

    assert policy.scope == "conversation"
    assert policy.subject == "user-1:session-1"
    assert policy.limit == 7


@pytest.mark.asyncio
async def test_rate_limit_buckets_do_not_consume_each_other() -> None:
    limiter = RateLimiter(Settings(), Metrics())

    assert (await limiter.allow("read", "user-1", limit=1, window_seconds=60)).allowed
    assert not (await limiter.allow("read", "user-1", limit=1, window_seconds=60)).allowed

    assert (await limiter.allow("user", "user-1", limit=1, window_seconds=60)).allowed
    assert (
        await limiter.allow(
            "conversation", "user-1:conversation-1", limit=1, window_seconds=60
        )
    ).allowed
