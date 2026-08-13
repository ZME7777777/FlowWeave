from flowweave.shared.domain.enums import AttemptState
from flowweave.shared.domain.errors import illegal

TRANSITIONS = {
    AttemptState.WAITING_INPUT: {AttemptState.START_GATES},
    AttemptState.START_GATES: {AttemptState.WAITING_START_CONFIRMATION, AttemptState.START_BLOCKED},
    AttemptState.START_BLOCKED: {AttemptState.START_GATES, AttemptState.CANCELLED},
    AttemptState.WAITING_START_CONFIRMATION: {AttemptState.EXECUTING, AttemptState.CANCELLED},
    AttemptState.EXECUTING: {
        AttemptState.WAITING_HUMAN,
        AttemptState.WAITING_CONFIRMATION,
        AttemptState.END_GATES,
        AttemptState.CANCELLED,
    },
    AttemptState.WAITING_HUMAN: {AttemptState.EXECUTING, AttemptState.CANCELLED},
    AttemptState.WAITING_CONFIRMATION: {AttemptState.EXECUTING, AttemptState.CANCELLED},
    AttemptState.END_GATES: {AttemptState.WAITING_ACCEPTANCE, AttemptState.END_BLOCKED},
    AttemptState.END_BLOCKED: {
        AttemptState.END_GATES,
        AttemptState.REJECTED,
        AttemptState.CANCELLED,
    },
    AttemptState.WAITING_ACCEPTANCE: {
        AttemptState.ACCEPTED,
        AttemptState.REJECTED,
        AttemptState.CANCELLED,
    },
}


def transition(current: str, target: str) -> str:
    if AttemptState(target) not in TRANSITIONS.get(AttemptState(current), set()):
        raise illegal(
            "attempt state does not allow this command", current_state=current, target_state=target
        )
    return target
