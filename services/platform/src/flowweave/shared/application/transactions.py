from __future__ import annotations

from collections.abc import Callable
from typing import cast

from sqlalchemy.orm import Session

_UOW_OWNED = "flowweave_uow_owned"
_ROLLBACK_ACTIONS = "flowweave_rollback_actions"
_COMMIT_ACTIONS = "flowweave_commit_actions"


def mark_uow_owned(session: Session) -> None:
    """Mark a compatibility sync session as owned by an outer async UoW."""

    session.info[_UOW_OWNED] = True


def register_rollback_action(session: Session, action: Callable[[], None]) -> None:
    """Register an idempotent external compensation for the current UoW."""

    _actions(session, _ROLLBACK_ACTIONS).append(action)


def register_commit_action(session: Session, action: Callable[[], None]) -> None:
    """Register external work that must run only after a successful DB commit."""

    _actions(session, _COMMIT_ACTIONS).append(action)


def _actions(session: Session, key: str) -> list[Callable[[], None]]:
    raw = session.info.setdefault(key, [])
    if not isinstance(raw, list):
        raise RuntimeError("Invalid transaction action registry")
    return cast(list[Callable[[], None]], raw)


def _take_actions(session: Session, key: str) -> list[Callable[[], None]]:
    raw = session.info.pop(key, [])
    if not isinstance(raw, list):
        return []
    return cast(list[Callable[[], None]], raw)


def clear_transaction_actions(session: Session) -> None:
    session.info.pop(_ROLLBACK_ACTIONS, None)
    session.info.pop(_COMMIT_ACTIONS, None)


def run_commit_actions(session: Session) -> None:
    """Run post-commit external work after the DB transaction has ended."""

    actions = _take_actions(session, _COMMIT_ACTIONS)
    session.info.pop(_ROLLBACK_ACTIONS, None)
    _run_actions(actions, reverse=False)


def run_rollback_actions(session: Session) -> None:
    """Run compensations after the database transaction has rolled back."""

    actions = _take_actions(session, _ROLLBACK_ACTIONS)
    session.info.pop(_COMMIT_ACTIONS, None)
    _run_actions(actions, reverse=True)


def _run_actions(actions: list[Callable[[], None]], *, reverse: bool) -> None:
    ordered = reversed(actions) if reverse else iter(actions)
    first_error: Exception | None = None
    for action in ordered:
        try:
            action()
        except Exception as exc:
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


def finish(session: Session) -> None:
    """Flush into an outer UoW, or commit and settle actions when standalone."""

    if session.info.get(_UOW_OWNED) is True:
        session.flush()
        return
    try:
        session.commit()
    except BaseException:
        session.rollback()
        run_rollback_actions(session)
        raise
    run_commit_actions(session)
