from flowweave.shared.domain.enums import AttemptState
from flowweave.shared.domain.errors import illegal

TRANSITIONS: dict[tuple[str, str], str] = {
    (AttemptState.WAITING_INPUT, "READY"): AttemptState.START_GATES,
    (AttemptState.START_GATES, "GATES_PASS"): AttemptState.WAITING_START_CONFIRMATION,
    (AttemptState.START_GATES, "GATES_BLOCK"): AttemptState.START_BLOCKED,
    (AttemptState.WAITING_START_CONFIRMATION, "CONFIRM_START"): AttemptState.EXECUTING,
    (AttemptState.EXECUTING, "HUMAN_REQUIRED"): AttemptState.WAITING_HUMAN,
    (AttemptState.WAITING_HUMAN, "HUMAN_INPUT"): AttemptState.EXECUTING,
    (AttemptState.EXECUTING, "OUTPUT_READY"): AttemptState.END_GATES,
    (AttemptState.END_GATES, "GATES_PASS"): AttemptState.WAITING_ACCEPTANCE,
    (AttemptState.END_GATES, "GATES_BLOCK"): AttemptState.END_BLOCKED,
    (AttemptState.WAITING_ACCEPTANCE, "ACCEPT"): AttemptState.ACCEPTED,
    (AttemptState.WAITING_ACCEPTANCE, "REJECT"): AttemptState.REJECTED,
}


def transition(current: str, event: str) -> str:
    if event == "CANCEL" and current not in {
        AttemptState.ACCEPTED,
        AttemptState.REJECTED,
        AttemptState.CANCELLED,
    }:
        return AttemptState.CANCELLED
    target = TRANSITIONS.get((current, event))
    if not target:
        raise illegal("Attempt does not allow this command", current_state=current, event=event)
    return target
