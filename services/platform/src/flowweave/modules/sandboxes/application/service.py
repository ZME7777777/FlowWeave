from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from flowweave.modules.sandboxes.infrastructure.docker import (
    DockerObservation,
    DockerSandboxProvider,
    backend_name,
)
from flowweave.modules.sandboxes.infrastructure.models import ManagedSandbox
from flowweave.modules.sandboxes.application.runtime_allocation import (
    resolve_runtime_secret,
    runtime_allocation_for_flow_run,
)
from flowweave.runtime.workspace import cleanup_runtime_memory
from flowweave.shared.application.transactions import register_rollback_action
from flowweave.shared.database import uid
from flowweave.shared.errors import DomainError, not_found
from flowweave.shared.infrastructure.docker_control import ephemeral_lease_is_expired
from flowweave.shared.settings import get_settings


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    inspected: int = 0
    deleted: int = 0
    expired: int = 0
    orphans_deleted: int = 0
    errors: int = 0


@dataclass(frozen=True, slots=True)
class RuntimeSandboxAllocation:
    id: str
    resource_name: str
    base_url: str


@dataclass(frozen=True, slots=True)
class _ReconcileOutcome:
    kind: str
    observation: DockerObservation | None = None
    error: DomainError | None = None


def _error(resource: ManagedSandbox, exc: DomainError) -> None:
    resource.observed_state = "ERROR"
    resource.last_error_code = exc.code
    resource.last_error_detail = exc.message
    resource.cleanup_attempts += 1
    resource.next_reconcile_at = datetime.now(UTC) + timedelta(
        seconds=min(2 ** min(resource.cleanup_attempts, 10), 3600)
    )


def _control_engine(db: Session) -> Engine:
    """Return an Engine so control-plane writes never reuse the caller transaction."""

    bind = db.get_bind()
    return bind.engine if isinstance(bind, Connection) else bind


def _renew_runtime_lease(resource: ManagedSandbox, *, now: datetime) -> None:
    if resource.hard_expires_at <= now:
        raise DomainError(
            "SANDBOX_HARD_EXPIRED",
            "The Runtime sandbox reached its absolute lifetime limit",
            409,
            {"sandbox_id": resource.id},
        )
    if bool((resource.spec_json or {}).get("bound")):
        # Once an Agent has durable state, its owner lifecycle is authoritative.
        # A human may legitimately leave the Runtime waiting longer than the
        # idle lease. The absolute hard limit still guarantees eventual cleanup
        # if every owner-driven deletion path fails.
        resource.last_activity_at = now
        resource.idle_expires_at = None
        return
    settings = get_settings()
    resource.last_activity_at = now
    resource.idle_expires_at = min(
        resource.hard_expires_at,
        now + timedelta(seconds=settings.sandbox_runtime_idle_ttl_seconds),
    )


def create_setup_sandbox(
    db: Session,
    *,
    owner_id: str,
    environment_id: str,
    image: str,
    base_image_reference: str,
    base_image_digest: str,
    base_version_id: str | None,
    base_version_no: int | None,
    hard_expires_at: datetime,
) -> ManagedSandbox:
    provider = DockerSandboxProvider(get_settings())
    # Reject a disabled backend before a rollback compensation is registered.
    # Otherwise the compensation itself could mask the configuration error by
    # trying to contact Docker even though creation was never attempted.
    provider.require_enabled()
    resource_id = uid()
    created_at = datetime.now(UTC)
    resource = ManagedSandbox(
        id=resource_id,
        kind="ENVIRONMENT_SETUP",
        owner_type="SETUP_SESSION",
        owner_id=owner_id,
        backend="docker",
        backend_resource_name=backend_name(
            resource_id, owner_type="SETUP_SESSION", owner_id=owner_id
        ),
        image_reference=image,
        spec_json={
            "environment_id": environment_id,
            "base_version_id": base_version_id,
            "base_version_no": base_version_no,
            "base_image_reference": base_image_reference,
            "base_image_digest": base_image_digest,
        },
        hard_expires_at=hard_expires_at,
        observed_state="CREATING",
        # Do not let maintenance race a legitimate slow docker run. If this
        # process dies, the durable row becomes eligible after the same bounded
        # startup window and reconciliation completes cleanup or creation.
        next_reconcile_at=created_at
        + timedelta(seconds=get_settings().terminal_environment_start_timeout_seconds),
    )
    with Session(bind=_control_engine(db), expire_on_commit=False) as control_db:
        control_db.add(resource)
        control_db.commit()
        control_db.expunge(resource)

    # The business transaction can still fail after Docker succeeds. Persist a
    # monotonic delete request; reconciliation performs the ownership-checked
    # physical cleanup.
    register_rollback_action(
        db, lambda resource_id=resource.id: request_delete_durable(db, resource_id)
    )
    try:
        observation = provider.ensure_running(resource)
    except DomainError as exc:
        with Session(bind=_control_engine(db), expire_on_commit=False) as control_db:
            current = control_db.get(ManagedSandbox, resource.id)
            if current is not None:
                _error(current, exc)
                current.desired_state = "DELETED"
                current.next_reconcile_at = datetime.now(UTC)
                control_db.commit()
        raise
    with Session(bind=_control_engine(db), expire_on_commit=False) as control_db:
        current = control_db.get(ManagedSandbox, resource.id)
        if current is None:
            raise RuntimeError("Setup sandbox ledger row disappeared during creation")
        current.backend_resource_id = observation.resource_identifier
        current.observed_state = observation.state
        current.last_activity_at = datetime.now(UTC)
        current.next_reconcile_at = datetime.now(UTC) + timedelta(
            seconds=get_settings().sandbox_reconcile_seconds
        )
        current.last_error_code = None
        current.last_error_detail = None
        control_db.commit()
        control_db.expunge(current)
        resource = current
    return resource


def create_runtime_sandbox(
    db: Session,
    *,
    flow_run_id: str | None = None,
    owner_type: str,
    owner_id: str,
    image: str,
    environment_id: str,
    environment_version_id: str,
    environment_version_no: int,
    workspace_relative: str,
    memory_enabled: bool = False,
    memory_working_dir_relative: str = "",
) -> RuntimeSandboxAllocation:
    provider = DockerSandboxProvider(get_settings())
    provider.require_enabled()
    flow_run_allocation = (
        runtime_allocation_for_flow_run(db, flow_run_id) if flow_run_id else None
    )
    if owner_type in {"ATTEMPT", "CONVERSATION"} and flow_run_allocation is None:
        raise DomainError(
            "FLOW_RUN_RUNTIME_ALLOCATION_REQUIRED",
            "FlowRun Agent Runtime creation requires external storage allocation",
            409,
            {"owner_type": owner_type, "owner_id": owner_id},
        )
    runtime_secret_key = (
        resolve_runtime_secret(db, flow_run_allocation.id)
        if flow_run_allocation is not None
        else None
    )
    engine = _control_engine(db)
    # Runtime provisioning intentionally owns a short independent transaction.
    # The ledger row is committed before Docker is touched, so a process crash
    # always leaves enough durable state for the reconciler to recover.
    with engine.connect() as connection:
        lock_key = f"AGENT_RUNTIME:{owner_type}:{owner_id}"
        lock_id = connection.scalar(select(func.hashtextextended(lock_key, 0)))
        if lock_id is None:
            raise RuntimeError("Could not derive the Runtime sandbox allocation lock")
        connection.scalar(select(func.pg_advisory_lock(lock_id)))
        connection.commit()
        try:
            with Session(bind=connection, expire_on_commit=False) as control_db:
                resource = control_db.scalar(
                    select(ManagedSandbox)
                    .where(
                        ManagedSandbox.kind == "AGENT_RUNTIME",
                        ManagedSandbox.owner_type == owner_type,
                        ManagedSandbox.owner_id == owner_id,
                        ManagedSandbox.desired_state == "RUNNING",
                    )
                    .with_for_update()
                )
                if resource is None:
                    generation = (
                        int(
                            control_db.scalar(
                                select(func.coalesce(func.max(ManagedSandbox.generation), 0)).where(
                                    ManagedSandbox.kind == "AGENT_RUNTIME",
                                    ManagedSandbox.owner_type == owner_type,
                                    ManagedSandbox.owner_id == owner_id,
                                )
                            )
                            or 0
                        )
                        + 1
                    )
                    resource_id = uid()
                    created_at = datetime.now(UTC)
                    resource = ManagedSandbox(
                        id=resource_id,
                        kind="AGENT_RUNTIME",
                        owner_type=owner_type,
                        owner_id=owner_id,
                        backend="docker",
                        backend_resource_name=backend_name(
                            resource_id, owner_type=owner_type, owner_id=owner_id
                        ),
                        generation=generation,
                        image_reference=image,
                        runtime_allocation_id=(
                            flow_run_allocation.id if flow_run_allocation is not None else None
                        ),
                        spec_json={
                            "port": 8000,
                            "bound": False,
                            "workspace_relative": workspace_relative,
                            "environment_id": environment_id,
                            "environment_version_id": environment_version_id,
                            "environment_version_no": environment_version_no,
                            "memory_enabled": memory_enabled,
                            "memory_working_dir_relative": (
                                memory_working_dir_relative if memory_enabled else None
                            ),
                            "flow_run_id": flow_run_id,
                            "runtime_allocation_id": (
                                flow_run_allocation.id
                                if flow_run_allocation is not None
                                else None
                            ),
                            "runtime_allocation_relative": (
                                flow_run_allocation.relative_root
                                if flow_run_allocation is not None
                                else None
                            ),
                            "runtime_secret_reference_id": (
                                flow_run_allocation.secret_reference_id
                                if flow_run_allocation is not None
                                else None
                            ),
                        },
                        idle_expires_at=created_at
                        + timedelta(seconds=get_settings().sandbox_runtime_idle_ttl_seconds),
                        hard_expires_at=created_at
                        + timedelta(seconds=get_settings().sandbox_runtime_hard_ttl_seconds),
                        observed_state="CREATING",
                    )
                    control_db.add(resource)
                elif (
                    resource.image_reference != image
                    or resource.runtime_allocation_id
                    != (flow_run_allocation.id if flow_run_allocation is not None else None)
                    or str((resource.spec_json or {}).get("workspace_relative") or "")
                    != workspace_relative
                    or str((resource.spec_json or {}).get("environment_id") or "") != environment_id
                    or str((resource.spec_json or {}).get("environment_version_id") or "")
                    != environment_version_id
                    or int((resource.spec_json or {}).get("environment_version_no") or 0)
                    != environment_version_no
                    or bool((resource.spec_json or {}).get("memory_enabled")) != memory_enabled
                    or str((resource.spec_json or {}).get("memory_working_dir_relative") or "")
                    != (memory_working_dir_relative if memory_enabled else "")
                ):
                    raise DomainError(
                        "SANDBOX_SPEC_CONFLICT",
                        "An active Runtime sandbox has a different immutable specification",
                        409,
                        {"sandbox_id": resource.id},
                    )
                control_db.commit()

                try:
                    observation = provider.ensure_running(
                        resource, runtime_secret_key=runtime_secret_key
                    )
                except DomainError as exc:
                    _error(resource, exc)
                    resource.desired_state = "DELETED"
                    resource.next_reconcile_at = datetime.now(UTC)
                    control_db.commit()
                    raise

                resource.backend_resource_id = observation.resource_identifier
                resource.observed_state = observation.state
                _renew_runtime_lease(resource, now=datetime.now(UTC))
                resource.last_error_code = None
                resource.last_error_detail = None
                control_db.commit()
                allocation = RuntimeSandboxAllocation(
                    id=resource.id,
                    resource_name=resource.backend_resource_name,
                    base_url=f"http://{resource.backend_resource_name}:8000",
                )
        finally:
            if connection.in_transaction():
                connection.rollback()
            connection.scalar(select(func.pg_advisory_unlock(lock_id)))
            connection.commit()
    return allocation


def mark_runtime_bound(db: Session, sandbox_id: str | None) -> None:
    """Durably mark a Runtime as carrying Agent state before business CAS."""

    if not sandbox_id:
        return
    with Session(bind=_control_engine(db), expire_on_commit=False) as control_db:
        resource = control_db.get(ManagedSandbox, sandbox_id)
        if resource is None or resource.kind != "AGENT_RUNTIME":
            raise not_found("managed_sandbox", sandbox_id)
        if resource.desired_state != "RUNNING":
            raise DomainError(
                "SANDBOX_NOT_ACTIVE",
                "The Runtime sandbox is no longer active",
                409,
                {"sandbox_id": sandbox_id},
            )
        resource.spec_json = {**(resource.spec_json or {}), "bound": True}
        _renew_runtime_lease(resource, now=datetime.now(UTC))
        control_db.commit()


def touch_runtime(db: Session, sandbox_id: str | None) -> None:
    """Renew the idle lease in a transaction independent from Runtime delivery."""

    if not sandbox_id:
        return
    with Session(bind=_control_engine(db), expire_on_commit=False) as control_db:
        resource = control_db.get(ManagedSandbox, sandbox_id)
        if resource is None or resource.kind != "AGENT_RUNTIME":
            raise not_found("managed_sandbox", sandbox_id)
        if resource.desired_state != "RUNNING":
            raise DomainError(
                "SANDBOX_NOT_ACTIVE",
                "The Runtime sandbox is no longer active",
                409,
                {"sandbox_id": sandbox_id},
            )
        _renew_runtime_lease(resource, now=datetime.now(UTC))
        control_db.commit()


def request_delete_durable(db: Session, sandbox_id: str | None) -> None:
    """Persist deletion independently from a caller that may still roll back."""

    if not sandbox_id:
        return
    with Session(bind=_control_engine(db), expire_on_commit=False) as control_db:
        resource = control_db.get(ManagedSandbox, sandbox_id)
        if resource is None:
            return
        resource.desired_state = "DELETED"
        resource.next_reconcile_at = datetime.now(UTC)
        control_db.commit()


def delete_sandbox_now(db: Session, sandbox_id: str) -> None:
    resource = db.scalar(
        select(ManagedSandbox).where(ManagedSandbox.id == sandbox_id).with_for_update()
    )
    if resource is None:
        raise not_found("managed_sandbox", sandbox_id)
    resource.desired_state = "DELETED"
    resource.observed_state = "DELETING"
    provider = DockerSandboxProvider(get_settings())
    try:
        provider.delete(resource)
        if (
            resource.kind == "AGENT_RUNTIME"
            and bool((resource.spec_json or {}).get("memory_enabled"))
            and resource.owner_type in {"ATTEMPT", "CONVERSATION"}
        ):
            cleanup_runtime_memory(owner_type=resource.owner_type, owner_id=resource.owner_id)
    except DomainError as exc:
        _error(resource, exc)
        raise
    # The deletion intent remains durable until the external resource is gone.
    # Once Docker confirms cleanup, absence of the ledger row is authoritative.
    db.delete(resource)


def request_delete(db: Session, sandbox_id: str) -> None:
    resource = db.get(ManagedSandbox, sandbox_id)
    if resource is None:
        return
    resource.desired_state = "DELETED"
    resource.next_reconcile_at = datetime.now(UTC)


def owner_has_live_sandbox(db: Session, *, owner_type: str, owner_id: str) -> bool:
    """Return whether an owner still has a non-deleted managed resource."""

    return (
        db.scalar(
            select(ManagedSandbox.id).where(
                ManagedSandbox.owner_type == owner_type,
                ManagedSandbox.owner_id == owner_id,
            )
        )
        is not None
    )


def runtime_memory_cleanup_pending(db: Session, *, owner_type: str, owner_id: str) -> bool:
    """Return whether durable Runtime deletion owns this Memory cleanup.

    Runtime allocation commits its ledger before Docker I/O. If provisioning
    then fails, that independent transaction marks the resource for deletion.
    Callers must retain the mount source until the reconciler has proved the
    container is gone; reading through a separate Session makes that decision
    independent from a caller transaction that may be rolled back.
    """

    with Session(bind=_control_engine(db), expire_on_commit=False) as control_db:
        resources = control_db.scalars(
            select(ManagedSandbox).where(
                ManagedSandbox.kind == "AGENT_RUNTIME",
                ManagedSandbox.owner_type == owner_type,
                ManagedSandbox.owner_id == owner_id,
                ManagedSandbox.desired_state == "DELETED",
            )
        )
        return any(bool((item.spec_json or {}).get("memory_enabled")) for item in resources)


def cleanup_unclaimed_runtime_memory(db: Session, *, owner_type: str, owner_id: str) -> bool:
    """Clean Memory only when no durable Runtime deletion owns the mount source."""

    if runtime_memory_cleanup_pending(db, owner_type=owner_type, owner_id=owner_id):
        return False
    cleanup_runtime_memory(owner_type=owner_type, owner_id=owner_id)
    return True


def image_has_live_sandbox(db: Session, *, reference: str, digest: str) -> bool:
    """Return whether a non-deleted sandbox still depends on an image."""

    return (
        db.scalar(
            select(ManagedSandbox.id).where(
                ManagedSandbox.image_reference.in_([reference, digest]),
            )
        )
        is not None
    )


def environment_has_live_sandbox(db: Session, *, environment_id: str) -> bool:
    """Return whether an environment still owns a non-deleted sandbox."""

    return (
        db.scalar(
            select(ManagedSandbox.id).where(
                ManagedSandbox.spec_json["environment_id"].as_string() == environment_id,
            )
        )
        is not None
    )


def delete_environment_credentials(environment_id: str) -> None:
    """Delete one owned environment credential volume through the Docker provider."""

    DockerSandboxProvider(get_settings()).delete_environment_credentials(environment_id)


def _sandbox_spec_signature(resource: ManagedSandbox) -> tuple[object, ...]:
    """Fields whose change invalidates an in-flight Docker observation."""

    return (
        resource.kind,
        resource.backend,
        resource.backend_resource_name,
        resource.generation,
        resource.image_reference,
        resource.runtime_allocation_id,
        str((resource.spec_json or {}).get("runtime_allocation_relative") or ""),
        str((resource.spec_json or {}).get("workspace_relative") or ""),
    )


def _owner_is_active(
    db: Session, resource: ManagedSandbox, *, now: datetime, binding_grace_seconds: int
) -> bool:
    """Return whether the durable owner still authorizes this resource to run.

    The owner reference is checked as well as its state. This closes the crash
    window between durably binding a newly-created Runtime sandbox and writing
    that sandbox ID back through the business aggregate's compare-and-swap.
    A short grace period covers the intentional two-phase Runtime allocation:
    the ledger and container are durable before the aggregate CAS stores the
    sandbox ID. Unknown owner types fail closed after the same grace period.
    """

    if resource.owner_type == "SETUP_SESSION":
        from flowweave.modules.environments.public import setup_sandbox_owner_is_active

        return setup_sandbox_owner_is_active(
            db,
            resource.owner_id,
            resource.id,
            created_at=resource.created_at,
            now=now,
            binding_grace_seconds=binding_grace_seconds,
        )
    if resource.owner_type == "ATTEMPT":
        from flowweave.modules.orchestration.public import attempt_sandbox_owner_is_active

        return attempt_sandbox_owner_is_active(
            db,
            resource.owner_id,
            resource.id,
            created_at=resource.created_at,
            now_at=now,
            binding_grace_seconds=binding_grace_seconds,
        )
    if resource.owner_type == "CONVERSATION":
        from flowweave.modules.conversations.public import conversation_sandbox_owner_is_active

        return conversation_sandbox_owner_is_active(
            db,
            resource.owner_id,
            resource.id,
            created_at=resource.created_at,
            now_at=now,
            binding_grace_seconds=binding_grace_seconds,
        )
    if resource.owner_type == "CAPABILITY_VALIDATION":
        from flowweave.modules.catalog.public import capability_validation_owner_is_active

        return capability_validation_owner_is_active(db, resource.owner_id)
    if resource.owner_type == "MCP_OAUTH_AUTHORIZATION":
        from flowweave.modules.catalog.public import (
            mcp_oauth_authorization_owner_is_active,
        )

        return mcp_oauth_authorization_owner_is_active(db, resource.owner_id)
    return resource.created_at + timedelta(seconds=binding_grace_seconds) > now


def _claim_reconcile_batch(
    connection: Connection,
    *,
    now: datetime,
    batch_size: int,
    interval_seconds: int,
    binding_grace_seconds: int,
) -> tuple[list[ManagedSandbox], int]:
    """Claim due rows in one short transaction and return detached snapshots."""

    expired = 0
    with Session(bind=connection, expire_on_commit=False) as control_db:
        resources = list(
            control_db.scalars(
                select(ManagedSandbox)
                .where(
                    ManagedSandbox.next_reconcile_at <= now,
                )
                .order_by(ManagedSandbox.next_reconcile_at, ManagedSandbox.created_at)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        )
        for resource in resources:
            owner_active = _owner_is_active(
                control_db,
                resource,
                now=now,
                binding_grace_seconds=binding_grace_seconds,
            )
            owner_inactive = not owner_active
            bound_runtime = resource.kind == "AGENT_RUNTIME" and bool(
                (resource.spec_json or {}).get("bound")
            )
            idle_expired = (
                resource.kind == "AGENT_RUNTIME"
                and not bound_runtime
                and resource.idle_expires_at is not None
                and resource.idle_expires_at <= now
            )
            # The hard expiry is an absolute safety boundary. Owner activity may
            # suppress the renewable idle lease, but must never turn a managed
            # container into an unbounded host resource.
            hard_expired = resource.hard_expires_at <= now
            if (hard_expired or idle_expired or owner_inactive) and (
                resource.desired_state != "DELETED"
            ):
                resource.desired_state = "DELETED"
                expired += 1
            # This is a crash-safe claim, not a long-lived lease. A process
            # failure makes the row eligible again after one normal interval.
            resource.next_reconcile_at = now + timedelta(seconds=interval_seconds)
        control_db.commit()
        control_db.expunge_all()
    return resources, expired


def _perform_reconcile(
    provider: DockerSandboxProvider,
    resource: ManagedSandbox,
    *,
    runtime_secret_key: str | None,
) -> _ReconcileOutcome:
    """Perform Docker I/O using only a detached, immutable ledger snapshot."""

    try:
        observation = provider.inspect(resource.backend_resource_name)
        if resource.desired_state == "DELETED":
            if observation is not None and observation.resource_id != resource.id:
                return _ReconcileOutcome("CONFLICT", observation)
            # Deletion covers every deterministic auxiliary resource, not only
            # the container. A Runtime container may disappear before its
            # per-sandbox network, so always execute the idempotent resource
            # deletion path even when container inspect returns None.
            provider.delete(resource)
            if (
                resource.kind == "AGENT_RUNTIME"
                and bool((resource.spec_json or {}).get("memory_enabled"))
                and resource.owner_type in {"ATTEMPT", "CONVERSATION"}
            ):
                cleanup_runtime_memory(owner_type=resource.owner_type, owner_id=resource.owner_id)
            return _ReconcileOutcome("DELETED")
        if observation is not None and observation.resource_id != resource.id:
            return _ReconcileOutcome("CONFLICT", observation)
        if (
            observation is None
            and resource.kind == "AGENT_RUNTIME"
            and bool((resource.spec_json or {}).get("bound"))
        ):
            return _ReconcileOutcome("RUNTIME_LOST")
        observation = provider.ensure_running(
            resource, runtime_secret_key=runtime_secret_key
        )
        return _ReconcileOutcome("RUNNING", observation)
    except DomainError as exc:
        return _ReconcileOutcome("ERROR", error=exc)


def _apply_reconcile_outcome(
    connection: Connection,
    snapshot: ManagedSandbox,
    outcome: _ReconcileOutcome,
    *,
    now: datetime,
    interval_seconds: int,
) -> tuple[int, int]:
    """Conditionally persist an observation without overwriting newer intent."""

    deleted = errors = 0
    with Session(bind=connection, expire_on_commit=False) as control_db:
        current = control_db.scalar(
            select(ManagedSandbox).where(ManagedSandbox.id == snapshot.id).with_for_update()
        )
        if current is None:
            control_db.commit()
            return deleted, errors

        if _sandbox_spec_signature(current) != _sandbox_spec_signature(snapshot):
            # The Docker result belongs to an older immutable specification.
            # Never project it onto the newer row.
            current.next_reconcile_at = now
            control_db.commit()
            return deleted, errors

        if outcome.kind == "DELETED":
            if current.desired_state == "DELETED":
                control_db.delete(current)
                deleted = 1
            else:
                # Resurrection is not a supported transition, but fail safely
                # if a future caller introduces it after physical deletion.
                current.observed_state = "PENDING"
                current.backend_resource_id = ""
                current.next_reconcile_at = now
            control_db.commit()
            return deleted, errors

        # A concurrent delete request is monotonic and always wins over an
        # older RUNNING/error observation. Reconciliation will remove the
        # resource on the next pass.
        if current.desired_state == "DELETED" and snapshot.desired_state != "DELETED":
            current.next_reconcile_at = now
            control_db.commit()
            return deleted, errors

        if outcome.kind == "RUNNING" and outcome.observation is not None:
            current.observed_state = outcome.observation.state
            current.backend_resource_id = outcome.observation.resource_identifier
            current.cleanup_attempts = 0
            current.last_error_code = None
            current.last_error_detail = None
            current.next_reconcile_at = now + timedelta(seconds=interval_seconds)
        elif outcome.kind == "CONFLICT":
            current.observed_state = "ERROR"
            current.last_error_code = "SANDBOX_RESOURCE_CONFLICT"
            current.last_error_detail = "Docker labels do not match the sandbox ledger"
            current.next_reconcile_at = now + timedelta(seconds=interval_seconds)
            errors = 1
        elif outcome.kind == "RUNTIME_LOST":
            current.observed_state = "ERROR"
            current.last_error_code = "SANDBOX_RUNTIME_LOST"
            current.last_error_detail = (
                "The bound Agent Runtime resource no longer exists and cannot be "
                "recreated without losing its conversation state"
            )
            current.next_reconcile_at = now + timedelta(seconds=interval_seconds)
            errors = 1
        elif outcome.error is not None:
            _error(current, outcome.error)
            errors = 1
        control_db.commit()
    return deleted, errors


def _reconcile_orphans(
    connection: Connection,
    provider: DockerSandboxProvider,
    *,
    now: datetime,
    grace_seconds: int,
) -> tuple[int, int]:
    """List Docker first, check ledger membership briefly, then delete outside SQL."""

    try:
        observations = provider.list_managed()
    except DomainError:
        return 0, 1
    resource_ids = {item.resource_id for item in observations if item.resource_id}
    known_ids: set[str] = set()
    if resource_ids:
        with Session(bind=connection) as control_db:
            known_ids.update(
                control_db.scalars(
                    select(ManagedSandbox.id).where(ManagedSandbox.id.in_(resource_ids))
                )
            )
            control_db.commit()

    deleted = errors = 0
    for observation in observations:
        if not observation.resource_id or observation.resource_id in known_ids:
            continue
        is_ephemeral = observation.labels.get("flowweave.lifecycle") == "ephemeral"
        eligible = (
            ephemeral_lease_is_expired(observation.labels, now=now)
            if is_ephemeral
            else DockerSandboxProvider.orphan_is_stale(observation, grace_seconds)
        )
        if not eligible:
            continue
        # A managed ledger row is normally committed before Docker is touched,
        # but re-check immediately before deletion so an operator repair or a
        # concurrent recovery cannot turn an earlier orphan observation into a
        # destructive stale decision. Docker ownership labels are verified once
        # more by delete_orphan after this database check.
        with Session(bind=connection) as control_db:
            now_known = control_db.get(ManagedSandbox, observation.resource_id) is not None
            control_db.commit()
        if now_known:
            continue
        try:
            provider.delete_orphan(observation)
            deleted += 1
        except DomainError:
            errors += 1
    return deleted, errors


def reconcile_managed_sandboxes(db: Session) -> ReconcileReport:
    """Converge the ledger without holding a SQL transaction during Docker I/O."""

    settings = get_settings()
    provider = DockerSandboxProvider(settings)
    if not provider.control_enabled():
        return ReconcileReport()
    engine = _control_engine(db)
    lock_key = f"SANDBOX_RECONCILE:{settings.sandbox_manager_scope}"
    with engine.connect() as connection:
        lock_id = connection.scalar(select(func.hashtextextended(lock_key, 0)))
        if lock_id is None:
            raise RuntimeError("Could not derive the sandbox reconciliation lock")
        lock_acquired = connection.scalar(select(func.pg_try_advisory_lock(lock_id)))
        connection.commit()
        if lock_acquired is not True:
            return ReconcileReport()
        try:
            now = datetime.now(UTC)
            resources, expired = _claim_reconcile_batch(
                connection,
                now=now,
                batch_size=settings.sandbox_reconcile_batch_size,
                interval_seconds=settings.sandbox_reconcile_seconds,
                binding_grace_seconds=(
                    settings.terminal_environment_start_timeout_seconds
                    + settings.sandbox_orphan_grace_seconds
                ),
            )
            deleted = errors = 0
            for snapshot in resources:
                runtime_secret_key = None
                try:
                    if snapshot.runtime_allocation_id is not None:
                        with Session(bind=connection) as secret_db:
                            runtime_secret_key = resolve_runtime_secret(
                                secret_db, snapshot.runtime_allocation_id
                            )
                            secret_db.commit()
                    outcome = _perform_reconcile(
                        provider,
                        snapshot,
                        runtime_secret_key=runtime_secret_key,
                    )
                except DomainError as exc:
                    outcome = _ReconcileOutcome("ERROR", error=exc)
                item_deleted, item_errors = _apply_reconcile_outcome(
                    connection,
                    snapshot,
                    outcome,
                    now=now,
                    interval_seconds=settings.sandbox_reconcile_seconds,
                )
                deleted += item_deleted
                errors += item_errors
            orphans_deleted, orphan_errors = _reconcile_orphans(
                connection,
                provider,
                now=now,
                grace_seconds=settings.sandbox_orphan_grace_seconds,
            )
            errors += orphan_errors
            return ReconcileReport(len(resources), deleted, expired, orphans_deleted, errors)
        finally:
            if connection.in_transaction():
                connection.rollback()
            connection.scalar(select(func.pg_advisory_unlock(lock_id)))
            connection.commit()


def sandbox_dict(item: ManagedSandbox) -> dict[str, Any]:
    return {
        "id": item.id,
        "kind": item.kind,
        "owner_type": item.owner_type,
        "owner_id": item.owner_id,
        "backend": item.backend,
        "backend_resource_name": item.backend_resource_name,
        "desired_state": item.desired_state,
        "observed_state": item.observed_state,
        "image_reference": item.image_reference,
        "hard_expires_at": item.hard_expires_at.isoformat(),
        "last_error_code": item.last_error_code,
        "last_error_detail": item.last_error_detail,
    }


def sandbox_snapshot(db: Session, sandbox_id: str | None) -> dict[str, Any] | None:
    """Return a stable cross-module view of one managed sandbox."""

    if not sandbox_id:
        return None
    item = db.get(ManagedSandbox, sandbox_id)
    if item is None:
        return None
    return {
        **sandbox_dict(item),
        "generation": item.generation,
        "spec": dict(item.spec_json or {}),
        "created_at": item.created_at,
        "next_reconcile_at": item.next_reconcile_at,
    }


def latest_runtime_sandbox_snapshot(
    db: Session, *, owner_type: str, owner_id: str
) -> dict[str, Any] | None:
    """Return the newest Agent Runtime sandbox owned by an aggregate."""

    item = db.scalar(
        select(ManagedSandbox)
        .where(
            ManagedSandbox.kind == "AGENT_RUNTIME",
            ManagedSandbox.owner_type == owner_type,
            ManagedSandbox.owner_id == owner_id,
        )
        .order_by(ManagedSandbox.generation.desc())
        .limit(1)
    )
    return sandbox_snapshot(db, item.id) if item is not None else None
