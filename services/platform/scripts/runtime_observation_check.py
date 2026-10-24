"""Verify retained environment references against real isolated PostgreSQL locks.

Run only in a disposable platform container sharing an empty test PostgreSQL
network namespace. Provider/model I/O is not part of this lock acceptance.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check() -> dict[str, object]:
    import psycopg
    from sqlalchemy import select, text
    from sqlalchemy.exc import OperationalError

    from flowweave.bootstrap.settings import Settings
    from flowweave.modules.environments.application.service import (
        delete_environment,
        delete_environment_version,
        lock_referenceable_version,
        read_referenced_version,
    )
    from flowweave.modules.sandboxes.application.runtime_sessions import (
        acquire_runtime_replacement_lease,
        activate_runtime_generation,
        active_flow_run_runtime_connection,
        assert_active_runtime_fence,
        ensure_runtime_generation,
    )
    from flowweave.modules.users.application.security import tenant_user
    from flowweave.shared.errors import DomainError
    from flowweave.shared.infrastructure.database import Database
    from flowweave.shared.models import (
        EnvironmentVersion,
        FlowDefinition,
        FlowRun,
        FlowRunRuntime,
        FlowRunRuntimeAllocation,
        FlowRunRuntimeSecretReference,
        ManagedSandbox,
        TerminalEnvironment,
    )
    from flowweave.shared.read_diagnostics import failure_code

    url = os.environ["TEST_DATABASE_URL"]
    require(
        url == "postgresql+psycopg://incident@127.0.0.1:5432/incident",
        "Only disposable loopback database is allowed",
    )
    with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://")) as db:
        require(
            db.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
            ).fetchone()[0]
            == 0,
            "Refusing to migrate a nonempty database",
        )
    os.environ["DATABASE_URL"] = url
    migration = subprocess.run(
        ["alembic", "upgrade", "head"], capture_output=True, text=True, timeout=60
    )
    require(migration.returncode == 0, "Full disposable migration failed")
    database = Database(
        Settings(
            database_url=url,
            blocking_pool_size=4,
            blocking_pool_timeout_seconds=0.5,
            statement_timeout_ms=2000,
        )
    )
    image = "sha256:" + "b" * 64
    owner = str(uuid4())
    report: dict[str, object] = {"migration": "PASS"}
    try:
        with tenant_user(owner), database.blocking_sessions() as db:
            environment = TerminalEnvironment(name="observation-probe", base_image="isolated")
            flow = FlowDefinition(name="observation-probe")
            db.add_all([environment, flow])
            db.flush()
            version = EnvironmentVersion(
                environment_id=environment.id,
                version_no=1,
                state="READY",
                image_digest=image,
                runtime_protocol_profile="OH_FW_V2",
            )
            db.add(version)
            db.flush()
            run = FlowRun(
                flow_definition_id=flow.id,
                run_no=1,
                name="observation-probe",
                parent_flow_run_id=None,
                environment_version_id=version.id,
            )
            secret = FlowRunRuntimeSecretReference(
                encrypted_secret_key=b"isolated", secret_digest=uuid4().hex * 2
            )
            db.add_all([run, secret])
            db.flush()
            allocation = FlowRunRuntimeAllocation(
                flow_run_id=run.id,
                secret_reference_id=secret.id,
                relative_root=f".flow-run-runtimes/{run.id}",
            )
            db.add(allocation)
            db.flush()
            session = FlowRunRuntime(
                flow_run_id=run.id,
                environment_version_id=version.id,
                runtime_image_digest=image,
                workspace_allocation_id=allocation.id,
                protocol_profile="OH_FW_V2",
                status="STARTING",
            )
            resource = ManagedSandbox(
                kind="AGENT_RUNTIME",
                owner_type="FLOW_RUN",
                owner_id=run.id,
                backend="docker",
                backend_resource_name="isolated-probe",
                backend_resource_id="isolated-instance",
                desired_state="RUNNING",
                observed_state="RUNNING",
                generation=1,
                image_reference=image,
                runtime_allocation_id=allocation.id,
                spec_json={},
                hard_expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            db.add_all([session, resource])
            db.flush()
            generation = ensure_runtime_generation(
                db, session=session, generation=1, managed_runtime=resource
            )
            activate_runtime_generation(
                db, session=session, generation=generation, instance_id=resource.backend_resource_id
            )
            db.commit()
            environment_id, version_id, run_id = environment.id, version.id, run.id
        payload = dict(
            flow_run_id=run_id,
            version_id=version_id,
            image_digest=image,
            protocol_profile="OH_FW_V2",
        )

        def read():
            with tenant_user(owner), database.blocking_sessions() as db:
                db.execute(text("SET LOCAL statement_timeout=400"))
                require(
                    read_referenced_version(db, **payload) is not None,
                    "Existing frozen reference failed under parent lock",
                )
                db.rollback()

        # Preserve the reproduction: creation still waits; existing observation does not.
        with tenant_user(owner), database.blocking_sessions() as holder:
            require(
                lock_referenceable_version(holder, version_id) is not None,
                "Cannot acquire reference-creation lock",
            )
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(read) for _ in range(2)]
                for future in futures:
                    future.result(timeout=2)

                def create_reference():
                    with tenant_user(owner), database.blocking_sessions() as db:
                        db.execute(text("SET LOCAL statement_timeout=200"))
                        return lock_referenceable_version(db, version_id)

                pending = pool.submit(create_reference)
                try:
                    pending.result(timeout=2)
                    raise AssertionError("Creation lock was accidentally bypassed")
                except OperationalError as error:
                    require(error.orig.sqlstate == "57014", "Wrong creation-lock error")
                    require(
                        failure_code(error) == ("DATABASE_STATEMENT_TIMEOUT", "database_statement"),
                        "Actual lock wait lost its safe diagnostic classification",
                    )
            holder.rollback()
        with tenant_user(owner), database.blocking_sessions() as db:
            require(
                lock_referenceable_version(db, version_id) is not None,
                "Reference creation did not recover after releasing the lock",
            )
            db.rollback()
        report["concurrent_retained_reads_under_creation_lock"] = "PASS"
        report["new_reference_creation_still_serializes"] = "PASS"
        report["lock_timeout_classification_and_recovery"] = "PASS"
        # A retained read must itself hold no parent/version row lock.
        with tenant_user(owner), database.blocking_sessions() as reader:
            require(read_referenced_version(reader, **payload) is not None, "Retained read failed")
            with tenant_user(owner), database.control_sessions() as db:
                db.execute(text("SET LOCAL lock_timeout=200"))
                db.scalar(
                    select(TerminalEnvironment)
                    .where(TerminalEnvironment.id == environment_id)
                    .with_for_update(nowait=True)
                )
                db.scalar(
                    select(EnvironmentVersion)
                    .where(EnvironmentVersion.id == version_id)
                    .with_for_update(nowait=True)
                )
                db.rollback()
            for remove, args, code in [
                (
                    delete_environment_version,
                    (environment_id, version_id),
                    "ENVIRONMENT_VERSION_IN_USE",
                ),
                (delete_environment, (environment_id,), "ENVIRONMENT_IN_USE"),
            ]:
                with tenant_user(owner), database.control_sessions() as db:
                    try:
                        remove(db, *args)
                        raise AssertionError(
                            "Existing FlowRun no longer protects environment deletion"
                        )
                    except DomainError as error:
                        require(error.code == code, "Wrong environment deletion guard")
                    finally:
                        db.rollback()
            reader.rollback()
        report["retained_read_holds_no_environment_row_lock"] = "PASS"
        report["version_and_parent_deletion_protected"] = "PASS"
        with tenant_user(owner), database.blocking_sessions() as db:
            for key, value in [
                ("flow_run_id", str(uuid4())),
                ("image_digest", "sha256:" + "c" * 64),
                ("protocol_profile", "OH_FW_V1"),
            ]:
                require(
                    read_referenced_version(db, **{**payload, key: value}) is None,
                    "Invalid immutable binding was accepted",
                )
            row = db.get(EnvironmentVersion, version_id)
            row.state = "RETIRED"
            db.flush()
            require(read_referenced_version(db, **payload) is None, "Retired version was accepted")
            db.rollback()
        # Top-level FlowRuns remain shared; nested execution references are private.
        with tenant_user(owner), database.blocking_sessions() as db:
            private_run = FlowRun(
                flow_definition_id=db.get(FlowRun, run_id).flow_definition_id,
                parent_flow_run_id=run_id,
                run_no=1,
                name="private execution",
                environment_version_id=version_id,
            )
            db.add(private_run)
            db.commit()
            private_run_id = private_run.id
        with tenant_user(str(uuid4())), database.blocking_sessions() as db:
            require(
                read_referenced_version(db, **{**payload, "flow_run_id": private_run_id}) is None,
                "Cross-user private binding was accepted",
            )
            require(
                read_referenced_version(db, **payload) is not None,
                "Shared top-level FlowRun became inaccessible",
            )
        report["frozen_identity_and_tenant_isolation"] = "PASS"
        with tenant_user(owner), database.blocking_sessions() as db:
            connection = active_flow_run_runtime_connection(db, flow_run_id=run_id)
            fence = asdict(connection.runtime_fence)
            require(
                assert_active_runtime_fence(db, **fence) == connection.runtime_fence,
                "Original active fence failed",
            )
            acquire_runtime_replacement_lease(
                db, flow_run_id=run_id, owner="isolated-probe", lease_seconds=30
            )
            db.commit()
        with tenant_user(owner), database.blocking_sessions() as db:
            require(
                read_referenced_version(db, **payload) is not None,
                "Existing immutable reference disappeared during replacement",
            )
            try:
                assert_active_runtime_fence(db, **fence)
                raise AssertionError("Replacement no longer fences the old writer")
            except DomainError as error:
                require(error.code == "RUNTIME_COMMAND_FENCED", "Wrong generation fence error")
        report["replacement_still_fences_old_generation"] = "PASS"
        require(
            all(pool["checked_out"] == 0 for pool in database.pool_metrics().values()),
            "Probe leaked database connections",
        )
        return report
    finally:
        asyncio.run(database.dispose())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-isolated-fault-injection", action="store_true")
    args = parser.parse_args()
    require(
        args.allow_isolated_fault_injection and Path("/.dockerenv").is_file(),
        "Explicit disposable Docker opt-in required",
    )
    print(json.dumps(check()), flush=True)
