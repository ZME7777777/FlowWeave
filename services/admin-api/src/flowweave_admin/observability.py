from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from typing import Any, cast

import httpx

from flowweave_admin.settings import Settings

_METRIC_LINE = re.compile(r"^(flowweave_[a-zA-Z0-9_:]+)(\{[^}]*\})?\s+([-+0-9.eE]+)$")
_INTERESTING_METRICS = {
    "flowweave_http_requests_total",
    "flowweave_http_request_duration_seconds_count",
    "flowweave_database_pool_connections",
    "flowweave_runtime_relay_hubs",
    "flowweave_runtime_relay_subscribers",
    "flowweave_runtime_relay_hub_capacity",
    "flowweave_terminal_attachments",
    "flowweave_terminal_sessions",
}


def _rows(
    connection: Any, statement: str, parameters: tuple[object, ...] = ()
) -> list[dict[str, Any]]:
    result = connection.execute(cast(Any, statement), parameters)
    rows = cast(list[dict[str, Any]], result.fetchall())
    return [dict(row) for row in rows]


def overview(connection: Any) -> dict[str, Any]:
    runtime_states = _rows(
        connection,
        """
        SELECT runtime_kind, status, count(*)::int AS count
        FROM (
          SELECT 'FLOW_RUN'::text AS runtime_kind, status FROM flow_run_runtimes
          UNION ALL
          SELECT 'AGENT_WORKSPACE'::text AS runtime_kind, status FROM agent_workspace_runtimes
        ) AS runtimes
        GROUP BY runtime_kind, status
        ORDER BY runtime_kind, status
        """,
    )
    tasks = _rows(
        connection,
        """
        SELECT state, count(*)::int AS count,
               min(created_at) FILTER (WHERE state IN ('PENDING', 'RETRY', 'RUNNING')) AS oldest_at
        FROM background_tasks
        GROUP BY state
        ORDER BY state
        """,
    )
    database = _rows(
        connection,
        """
        SELECT state, count(*)::int AS count
        FROM pg_stat_activity
        WHERE datname = current_database()
        GROUP BY state
        ORDER BY state
        """,
    )
    return {"runtime_states": runtime_states, "tasks": tasks, "database_connections": database}


def runtimes(connection: Any, *, limit: int) -> list[dict[str, Any]]:
    return _rows(
        connection,
        """
        WITH all_runtimes AS (
          SELECT id, 'FLOW_RUN'::text AS runtime_kind, flow_run_id AS owner_id,
                 node_attempt_id, status, active_generation, row_version,
                 runtime_image_digest, updated_at, replacement_error_code AS failure_code,
                 replacement_error_summary AS failure_summary
          FROM flow_run_runtimes
          UNION ALL
          SELECT id, 'AGENT_WORKSPACE'::text AS runtime_kind, workspace_id AS owner_id,
                 NULL::text AS node_attempt_id, status, active_generation, row_version,
                 runtime_image_digest, updated_at, failure_code, failure_summary
          FROM agent_workspace_runtimes
        ), all_generations AS (
          SELECT runtime_session_id, generation, managed_runtime_id, state, ready_at, failure_code,
                 failure_summary
          FROM runtime_generations
          UNION ALL
          SELECT runtime_session_id, generation, managed_runtime_id, state, ready_at, failure_code,
                 failure_summary
          FROM agent_workspace_runtime_generations
        ), bindings AS (
          SELECT runtime_session_id, count(*)::int AS conversation_count,
                 count(*) FILTER (WHERE lifecycle = 'ACTIVE')::int AS active_conversation_count,
                 max(last_connected_at) AS last_connected_at
          FROM agent_conversation_bindings
          GROUP BY runtime_session_id
        )
        SELECT runtime.id AS runtime_session_id, runtime.runtime_kind, runtime.owner_id,
               runtime.node_attempt_id, runtime.status, runtime.active_generation,
               runtime.row_version, runtime.runtime_image_digest, runtime.updated_at,
               runtime.failure_code, runtime.failure_summary, generation.state AS generation_state,
               generation.ready_at, sandbox.id AS managed_sandbox_id,
               sandbox.backend_resource_id AS container_id,
               sandbox.backend_resource_name AS container_name,
               sandbox.desired_state, sandbox.observed_state, sandbox.last_activity_at,
               sandbox.idle_expires_at, sandbox.hard_expires_at, sandbox.last_error_code,
               sandbox.last_error_detail,
               coalesce(bindings.conversation_count, 0) AS conversation_count,
               coalesce(bindings.active_conversation_count, 0) AS active_conversation_count,
               bindings.last_connected_at,
               flow_definition.name AS flow_definition_name,
               flow_run.name AS flow_run_name, flow_run.run_no AS flow_run_no,
               flow_run.state AS flow_run_state,
               node_run.name AS node_run_name, node_run.sequence_no AS node_run_sequence_no,
               node_attempt.attempt_no AS node_attempt_no,
               node_attempt.state AS node_attempt_state,
               workspace.display_name AS workspace_display_name,
               workspace.scope_key AS workspace_scope_key
        FROM all_runtimes AS runtime
        LEFT JOIN flow_runs AS flow_run
          ON runtime.runtime_kind = 'FLOW_RUN' AND flow_run.id = runtime.owner_id
        LEFT JOIN flow_definitions AS flow_definition
          ON flow_definition.id = flow_run.flow_definition_id
        LEFT JOIN node_attempts AS node_attempt
          ON node_attempt.id = runtime.node_attempt_id
        LEFT JOIN node_runs AS node_run ON node_run.id = node_attempt.node_run_id
        LEFT JOIN agent_workspaces AS workspace
          ON runtime.runtime_kind = 'AGENT_WORKSPACE' AND workspace.id = runtime.owner_id
        LEFT JOIN all_generations AS generation
          ON generation.runtime_session_id = runtime.id
         AND generation.generation = runtime.active_generation
        LEFT JOIN managed_sandboxes AS sandbox ON sandbox.id = generation.managed_runtime_id
        LEFT JOIN bindings ON bindings.runtime_session_id = runtime.id
        ORDER BY runtime.updated_at DESC
        LIMIT %s
        """,
        (limit,),
    )


def runtime_detail(connection: Any, *, runtime_session_id: str) -> dict[str, Any] | None:
    runtime_rows = _rows(
        connection,
        """
        WITH all_runtimes AS (
          SELECT id, 'FLOW_RUN'::text AS runtime_kind, flow_run_id AS owner_id,
                 node_attempt_id, status, active_generation, row_version,
                 runtime_image_digest, created_at, updated_at,
                 replacement_error_code AS failure_code,
                 replacement_error_summary AS failure_summary
          FROM flow_run_runtimes
          UNION ALL
          SELECT id, 'AGENT_WORKSPACE'::text AS runtime_kind, workspace_id AS owner_id,
                 NULL::text AS node_attempt_id, status, active_generation, row_version,
                 runtime_image_digest, created_at, updated_at, failure_code, failure_summary
          FROM agent_workspace_runtimes
        ), all_generations AS (
          SELECT runtime_session_id, generation, managed_runtime_id, state, ready_at,
                 started_at, stopped_at, created_at, updated_at, failure_code, failure_summary
          FROM runtime_generations
          UNION ALL
          SELECT runtime_session_id, generation, managed_runtime_id, state, ready_at,
                 started_at, stopped_at, created_at, updated_at, failure_code, failure_summary
          FROM agent_workspace_runtime_generations
        )
        SELECT runtime.*, generation.managed_runtime_id AS managed_sandbox_id,
               generation.state AS generation_state, generation.ready_at,
               sandbox.backend_resource_id AS container_id,
               sandbox.backend_resource_name AS container_name,
               sandbox.desired_state, sandbox.observed_state, sandbox.last_activity_at,
               sandbox.idle_expires_at, sandbox.hard_expires_at, sandbox.last_error_code,
               sandbox.last_error_detail,
               flow_definition.name AS flow_definition_name,
               flow_run.name AS flow_run_name, flow_run.run_no AS flow_run_no,
               flow_run.state AS flow_run_state,
               node_run.name AS node_run_name, node_run.sequence_no AS node_run_sequence_no,
               node_attempt.attempt_no AS node_attempt_no,
               node_attempt.state AS node_attempt_state,
               workspace.display_name AS workspace_display_name,
               workspace.scope_key AS workspace_scope_key
        FROM all_runtimes AS runtime
        LEFT JOIN flow_runs AS flow_run
          ON runtime.runtime_kind = 'FLOW_RUN' AND flow_run.id = runtime.owner_id
        LEFT JOIN flow_definitions AS flow_definition
          ON flow_definition.id = flow_run.flow_definition_id
        LEFT JOIN node_attempts AS node_attempt
          ON node_attempt.id = runtime.node_attempt_id
        LEFT JOIN node_runs AS node_run ON node_run.id = node_attempt.node_run_id
        LEFT JOIN agent_workspaces AS workspace
          ON runtime.runtime_kind = 'AGENT_WORKSPACE' AND workspace.id = runtime.owner_id
        LEFT JOIN all_generations AS generation
          ON generation.runtime_session_id = runtime.id
         AND generation.generation = runtime.active_generation
        LEFT JOIN managed_sandboxes AS sandbox ON sandbox.id = generation.managed_runtime_id
        WHERE runtime.id = %s
        """,
        (runtime_session_id,),
    )
    if not runtime_rows:
        return None
    runtime = runtime_rows[0]
    generations = _rows(
        connection,
        """
        SELECT generation, state, managed_runtime_id, started_at, ready_at, stopped_at,
               created_at, updated_at, failure_code, failure_summary
        FROM (
          SELECT runtime_session_id, generation, state, managed_runtime_id, started_at,
                 ready_at, stopped_at, created_at, updated_at, failure_code, failure_summary
          FROM runtime_generations
          UNION ALL
          SELECT runtime_session_id, generation, state, managed_runtime_id, started_at,
                 ready_at, stopped_at, created_at, updated_at, failure_code, failure_summary
          FROM agent_workspace_runtime_generations
        ) AS all_generations
        WHERE runtime_session_id = %s
        ORDER BY generation DESC
        LIMIT 20
        """,
        (runtime_session_id,),
    )
    conversation_summary = _rows(
        connection,
        """
        SELECT lifecycle, count(*)::int AS count, max(updated_at) AS last_updated_at,
               max(last_connected_at) AS last_connected_at
        FROM agent_conversation_bindings
        WHERE runtime_session_id = %s
        GROUP BY lifecycle
        ORDER BY lifecycle
        """,
        (runtime_session_id,),
    )
    operations = _rows(
        connection,
        """
        SELECT id AS operation_id, action, status, runtime_kind, owner_id,
               expected_generation, expected_session_row_version, actor_username,
               reason, request_id, created_at
        FROM admin_runtime_operations
        WHERE runtime_session_id = %s
        ORDER BY created_at DESC, id DESC
        LIMIT 20
        """,
        (runtime_session_id,),
    )
    return {
        "runtime": runtime,
        "generations": generations,
        "conversation_summary": conversation_summary,
        "operations": operations,
    }


def conversations(connection: Any, *, limit: int) -> list[dict[str, Any]]:
    return _rows(
        connection,
        """
        SELECT binding.id AS binding_id, binding.owner_user_id, users.username,
               binding.host_kind, binding.host_id, binding.flow_run_id, binding.node_run_id,
               binding.node_attempt_id, binding.runtime_session_id,
               binding.openhands_conversation_id, binding.display_title, binding.lifecycle,
               binding.model_name, binding.created_at, binding.updated_at,
               binding.last_connected_at, binding.unread
        FROM agent_conversation_bindings AS binding
        LEFT JOIN users ON users.id = binding.owner_user_id
        ORDER BY binding.updated_at DESC
        LIMIT %s
        """,
        (limit,),
    )


def runtime_operations(connection: Any, *, limit: int) -> list[dict[str, Any]]:
    return _rows(
        connection,
        """
        WITH all_runtimes AS (
          SELECT id, 'FLOW_RUN'::text AS runtime_kind, flow_run_id AS owner_id, status,
                 active_generation, row_version, replacement_generation,
                 replacement_started_at, replacement_error_code, replacement_error_summary
          FROM flow_run_runtimes
          UNION ALL
          SELECT id, 'AGENT_WORKSPACE'::text AS runtime_kind, workspace_id AS owner_id, status,
                 active_generation, row_version, NULL::integer AS replacement_generation,
                 NULL::timestamptz AS replacement_started_at,
                 failure_code AS replacement_error_code,
                 failure_summary AS replacement_error_summary
          FROM agent_workspace_runtimes
        ), all_generations AS (
          SELECT runtime_session_id, generation, state, ready_at, failure_code, failure_summary
          FROM runtime_generations
          UNION ALL
          SELECT runtime_session_id, generation, state, ready_at, failure_code, failure_summary
          FROM agent_workspace_runtime_generations
        )
        SELECT operation.id AS operation_id, operation.action, operation.status,
               operation.actor_user_id, operation.actor_username, operation.runtime_kind,
               operation.owner_id, operation.flow_run_id, operation.runtime_session_id,
               operation.expected_generation, operation.expected_session_row_version,
               operation.reason, operation.request_id, operation.created_at,
               runtime.status AS current_runtime_status,
               runtime.active_generation AS current_generation,
               runtime.replacement_generation, runtime.replacement_started_at,
               runtime.row_version AS current_session_row_version,
               runtime.replacement_error_code, runtime.replacement_error_summary,
               replacement.state AS replacement_generation_state,
               replacement.ready_at AS replacement_ready_at,
               replacement.failure_code AS replacement_failure_code,
               replacement.failure_summary AS replacement_failure_summary
        FROM admin_runtime_operations AS operation
        LEFT JOIN all_runtimes AS runtime
          ON runtime.id = operation.runtime_session_id
         AND runtime.runtime_kind = operation.runtime_kind
        LEFT JOIN all_generations AS replacement
          ON replacement.runtime_session_id = operation.runtime_session_id
         AND replacement.generation = operation.expected_generation + 1
        ORDER BY operation.created_at DESC, operation.id DESC
        LIMIT %s
        """,
        (limit,),
    )


def enrich_runtime_operation_status(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for entry in entries:
        entry["replacement_status"] = _replacement_status(entry)
    return entries


def _replacement_status(entry: dict[str, Any]) -> str:
    generation_state = entry.get("replacement_generation_state")
    if generation_state == "FAILED" or entry.get("replacement_failure_code"):
        return "FAILED"
    if entry.get("replacement_error_code"):
        return "FAILED"
    if (
        entry.get("current_generation") is not None
        and entry["current_generation"] > entry["expected_generation"]
        and generation_state == "READY"
    ):
        return "RECOVERED"
    if generation_state in {"PROVISIONING", "READY", "DRAINING"}:
        return "RECOVERING"
    if entry.get("replacement_generation") is not None or entry.get("replacement_started_at"):
        return "RECOVERING"
    return "SUBMITTED"


def admin_operations(
    connection: Any, *, limit: int, action: str | None, actor: str | None, since_hours: int
) -> list[dict[str, Any]]:
    return _rows(
        connection,
        """
        WITH operations AS (
          SELECT operation.id, operation.action, 'RUNTIME'::text AS target_kind,
                 operation.runtime_session_id AS target_id,
                 operation.runtime_kind || ':' || operation.owner_id AS target_detail,
                 operation.actor_user_id, operation.actor_username, operation.reason,
                 operation.request_id, operation.created_at,
                 CASE
                   WHEN COALESCE(
                     flow_runtime.replacement_error_code, workspace_runtime.failure_code
                   ) IS NOT NULL THEN 'FAILED'
                   WHEN COALESCE(
                     flow_generation.state, workspace_generation.state
                   ) = 'FAILED' THEN 'FAILED'
                   WHEN COALESCE(
                     flow_runtime.active_generation, workspace_runtime.active_generation
                   ) > operation.expected_generation
                   AND COALESCE(
                     flow_generation.state, workspace_generation.state
                   ) = 'READY' THEN 'RECOVERED'
                   WHEN COALESCE(
                     flow_generation.state, workspace_generation.state
                   ) IN ('PROVISIONING', 'READY', 'DRAINING')
                   OR flow_runtime.replacement_generation IS NOT NULL THEN 'RECOVERING'
                   ELSE 'SUBMITTED'
                 END AS status,
                 NULL::timestamptz AS silenced_until
          FROM admin_runtime_operations AS operation
          LEFT JOIN flow_run_runtimes AS flow_runtime
            ON operation.runtime_kind = 'FLOW_RUN'
           AND flow_runtime.id = operation.runtime_session_id
          LEFT JOIN agent_workspace_runtimes AS workspace_runtime
            ON operation.runtime_kind = 'AGENT_WORKSPACE'
           AND workspace_runtime.id = operation.runtime_session_id
          LEFT JOIN runtime_generations AS flow_generation
            ON operation.runtime_kind = 'FLOW_RUN'
           AND flow_generation.runtime_session_id = operation.runtime_session_id
           AND flow_generation.generation = operation.expected_generation + 1
          LEFT JOIN agent_workspace_runtime_generations AS workspace_generation
            ON operation.runtime_kind = 'AGENT_WORKSPACE'
           AND workspace_generation.runtime_session_id = operation.runtime_session_id
           AND workspace_generation.generation = operation.expected_generation + 1
          UNION ALL
          SELECT action.id, action.action, 'ALERT'::text AS target_kind,
                 action.alert_key AS target_id, NULL::text AS target_detail,
                 action.actor_user_id, action.actor_username, action.reason,
                 action.request_id, action.created_at, 'RECORDED'::text AS status,
                 action.silenced_until
          FROM admin_alert_actions AS action
        )
        SELECT * FROM operations
        WHERE (%s::text IS NULL OR action = %s)
          AND (%s::text IS NULL OR actor_username ILIKE ('%%' || %s || '%%'))
          AND created_at >= now() - (%s * interval '1 hour')
        ORDER BY created_at DESC, id DESC
        LIMIT %s
        """,
        (action, action, actor, actor, since_hours, limit),
    )


def alert_states(connection: Any, *, keys: list[str]) -> list[dict[str, Any]]:
    if not keys:
        return []
    return _rows(
        connection,
        """
        SELECT alert_key, acknowledged_at, acknowledged_by_username, silenced_until, reason
        FROM admin_alert_states
        WHERE alert_key = ANY(%s)
        """,
        (keys,),
    )


def metric_history(
    connection: Any, *, scope: str, subject: str, metric: str, hours: int
) -> list[dict[str, Any]]:
    return _rows(
        connection,
        """
        SELECT date_trunc('minute', observed_at) AS observed_at, avg(value) AS value
        FROM admin_metric_samples
        WHERE scope = %s
          AND subject = %s
          AND metric = %s
          AND observed_at >= now() - (%s * interval '1 hour')
        GROUP BY date_trunc('minute', observed_at)
        ORDER BY observed_at
        """,
        (scope, subject, metric, hours),
    )


def _parse_metrics(body: str) -> dict[str, list[dict[str, str]]]:
    result: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    for line in body.splitlines():
        match = _METRIC_LINE.match(line)
        if match is None or match.group(1) not in _INTERESTING_METRICS:
            continue
        result[match.group(1)].append({"labels": match.group(2) or "", "value": match.group(3)})
    return dict(result)


async def service_metrics(settings: Settings) -> dict[str, Any]:
    targets = {
        "api": settings.platform_api_url,
        "stream-api": settings.platform_stream_api_url,
        "runtime-provider": settings.runtime_provider_url,
    }

    async def read(name: str, base_url: str) -> tuple[str, dict[str, Any]]:
        try:
            async with httpx.AsyncClient(
                timeout=settings.admin_api_request_timeout_seconds
            ) as client:
                health, metrics = await asyncio.gather(
                    client.get(f"{base_url.rstrip('/')}/health"),
                    client.get(f"{base_url.rstrip('/')}/metrics"),
                )
            return name, {
                "health": "UP" if health.is_success else "DOWN",
                "metrics": _parse_metrics(metrics.text) if metrics.is_success else {},
            }
        except httpx.HTTPError:
            return name, {"health": "UNREACHABLE", "metrics": {}}

    pairs = await asyncio.gather(*(read(name, target) for name, target in targets.items()))
    return dict(pairs)


async def runtime_observations(settings: Settings) -> dict[str, Any]:
    if not settings.admin_runtime_observer_key:
        return {"available": False, "services": [], "managed_resources": []}
    try:
        async with httpx.AsyncClient(timeout=settings.admin_api_request_timeout_seconds) as client:
            response = await client.get(
                f"{settings.runtime_provider_url.rstrip('/')}/v1/admin/observability",
                headers={"Authorization": f"Bearer {settings.admin_runtime_observer_key}"},
            )
        if not response.is_success:
            return {"available": False, "services": [], "managed_resources": []}
        body = response.json()
        if isinstance(body, dict):
            return body
        return {"available": False, "services": [], "managed_resources": []}
    except (httpx.HTTPError, ValueError):
        return {"available": False, "services": [], "managed_resources": []}
