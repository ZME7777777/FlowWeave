"""Stable public facade for Case control-plane services."""

from flowweave.modules.cases.application.service import (
    attach_case_flow_run,
    bind_case_stage_flow_run,
    create_case,
    create_case_control_conversation,
    create_case_decision_proposal,
    execute_case_decision_proposal,
    get_case,
    launch_case_stage,
    list_case_stage_templates,
    list_cases,
    project_case_observations,
    project_flow_run_event,
    project_flow_run_observations,
    set_case_stage_state,
)

__all__ = (
    "attach_case_flow_run",
    "bind_case_stage_flow_run",
    "create_case",
    "create_case_control_conversation",
    "create_case_decision_proposal",
    "execute_case_decision_proposal",
    "get_case",
    "launch_case_stage",
    "list_case_stage_templates",
    "list_cases",
    "project_case_observations",
    "project_flow_run_event",
    "project_flow_run_observations",
    "set_case_stage_state",
)
