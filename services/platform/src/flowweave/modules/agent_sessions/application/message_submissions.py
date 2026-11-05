"""Short DB cancellation fences; OpenHands still owns delivered events and pause."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.infrastructure.models import AgentMessageSubmission
from flowweave.modules.users.application.security import current_user_id
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError


def _query(binding_id: str, submission_id: str):
    return select(AgentMessageSubmission).where(
        AgentMessageSubmission.owner_user_id == current_user_id(),
        AgentMessageSubmission.binding_id == binding_id,
        AgentMessageSubmission.submission_id == submission_id,
    )


def _insert(db: Session, binding_id: str, submission_id: str, phase: str) -> bool:
    if not submission_id or len(submission_id) > 200:
        raise DomainError("AGENT_SUBMISSION_ID_INVALID", "消息提交标识无效", 422)
    return (
        db.execute(
            insert(AgentMessageSubmission)
            .values(
                id=str(uuid4()),
                owner_user_id=current_user_id(),
                binding_id=binding_id,
                submission_id=submission_id,
                phase=phase,
                pause_requested=phase == "CANCELLED",
                created_at=now(),
            )
            .on_conflict_do_nothing(index_elements=["owner_user_id", "binding_id", "submission_id"])
            .returning(AgentMessageSubmission.id)
        ).scalar_one_or_none()
        is not None
    )


def begin(db: Session, binding_id: str, submission_id: str) -> None:
    if _insert(db, binding_id, submission_id, "PREPARING"):
        return
    item = db.scalar(_query(binding_id, submission_id))
    if item is not None and item.phase == "CANCELLED":
        raise DomainError("AGENT_MESSAGE_CANCELLED", "已取消发送，消息已回到发送框", 409)
    raise DomainError("AGENT_MESSAGE_SUBMISSION_EXISTS", "此消息已提交，请勿重复发送", 409)


def cancel(db: Session, binding_id: str, submission_id: str) -> dict[str, bool]:
    # A tombstone also wins when cancellation reaches another API worker first.
    _insert(db, binding_id, submission_id, "CANCELLED")
    item = db.scalar(_query(binding_id, submission_id).with_for_update())
    assert item is not None
    if item.phase == "PREPARING":
        item.phase = "CANCELLED"
    item.pause_requested = True
    db.flush()
    return {"cancelled": item.phase == "CANCELLED"}


def discard_preparation(db: Session, binding_id: str, submission_id: str) -> None:
    # Called only after a completed worker error, never coroutine cancellation.
    db.execute(
        delete(AgentMessageSubmission).where(
            AgentMessageSubmission.owner_user_id == current_user_id(),
            AgentMessageSubmission.binding_id == binding_id,
            AgentMessageSubmission.submission_id == submission_id,
            AgentMessageSubmission.phase == "PREPARING",
        )
    )


@dataclass
class DispatchFence:
    sessions: Callable[[], Session]
    binding_id: str
    submission_id: str

    def claim(self) -> None:
        with self.sessions() as db:
            changed = db.execute(
                update(AgentMessageSubmission)
                .where(
                    AgentMessageSubmission.owner_user_id == current_user_id(),
                    AgentMessageSubmission.binding_id == self.binding_id,
                    AgentMessageSubmission.submission_id == self.submission_id,
                    AgentMessageSubmission.phase == "PREPARING",
                )
                .values(phase="DISPATCHING")
                .returning(AgentMessageSubmission.id)
            ).scalar_one_or_none()
            db.commit()
        if not changed:
            raise DomainError("AGENT_MESSAGE_CANCELLED", "已取消发送，消息已回到发送框", 409)

    def pause_requested(self) -> bool:
        with self.sessions() as db:
            item = db.scalar(_query(self.binding_id, self.submission_id))
            return bool(item and item.pause_requested)


_fence: ContextVar[DispatchFence | None] = ContextVar("message_submission_fence", default=None)


@contextmanager
def fenced(fence: DispatchFence | None) -> Iterator[None]:
    token = _fence.set(fence)
    try:
        yield
    finally:
        _fence.reset(token)


def dispatch(runtime, handle, content: str, image_urls=()):
    fence = _fence.get()
    if fence is not None:
        fence.claim()
    try:
        return runtime.send_message(handle, content, image_urls)
    finally:
        # A native append is irreversible. If cancel lost the atomic fence,
        # repeat the formal interrupt after append, even if the browser left.
        if fence is not None and fence.pause_requested():
            runtime.interrupt(handle)
