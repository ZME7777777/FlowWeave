"""Re-export of the shared gate failure-policy rules for the gates module."""

from flowweave.shared.domain.gate_failure_policy import (
    ACTIONS,
    DEFAULT_ROUNDS,
    MAX_ROUNDS_LIMIT,
    FailurePolicy,
    OnFailAction,
    parse_on_fail,
    resolve,
    validate_on_fail,
)

__all__ = (
    "ACTIONS",
    "DEFAULT_ROUNDS",
    "MAX_ROUNDS_LIMIT",
    "FailurePolicy",
    "OnFailAction",
    "parse_on_fail",
    "resolve",
    "validate_on_fail",
)
