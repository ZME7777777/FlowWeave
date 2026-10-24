"""Real isolated PostgreSQL faults through production pools and application locks."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4


def require(ok, message):
    if not ok:
        raise AssertionError(message)


def check():
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    from flowweave.bootstrap.settings import Settings
    from flowweave.modules.environments.application.service import lock_referenceable_version
    from flowweave.modules.environments.infrastructure.models import (
        EnvironmentVersion,
        TerminalEnvironment,
    )
    from flowweave.shared.errors import DomainError
    from flowweave.shared.http import run_blocking, run_blocking_control
    from flowweave.shared.infrastructure.database import Database

    url = os.environ["TEST_DATABASE_URL"]
    require(
        url == "postgresql+psycopg://incident@127.0.0.1:5432/incident",
        "Only disposable loopback database is allowed",
    )
    import psycopg

    with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://")) as connection:
        require(
            connection.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
            ).fetchone()[0]
            == 0,
            "Refusing to migrate a nonempty database",
        )
    os.environ["DATABASE_URL"] = url
    migration = subprocess.run(
        ["alembic", "upgrade", "head"], capture_output=True, text=True, timeout=60
    )
    require(migration.returncode == 0, "Disposable database full migration failed")
    settings = Settings(
        database_url=url,
        pool_size=2,
        pool_max_overflow=0,
        blocking_pool_size=2,
        database_pool_timeout_seconds=0.2,
        blocking_pool_timeout_seconds=0.2,
        history_read_pool_size=1,
        statement_timeout_ms=2000,
    )
    database = Database(settings, poll_pool_size=1)
    report = {"migration": "PASS"}
    try:
        environment_id = str(uuid4())
        version_id = str(uuid4())
        with database.blocking_sessions() as db:
            db.add(
                TerminalEnvironment(
                    id=environment_id, name="isolated-incident", base_image="isolated"
                )
            )
            db.add(
                EnvironmentVersion(
                    id=version_id,
                    environment_id=environment_id,
                    version_no=1,
                    state="READY",
                    image_digest="sha256:" + "1" * 64,
                    runtime_protocol_profile="OH_FW_V2",
                )
            )
            db.commit()
        held = [database.blocking_engine.connect() for _ in range(2)]
        try:
            try:
                with database.blocking_engine.connect():
                    pass
                raise AssertionError("QueuePool exhaustion did not fail")
            except PoolTimeout:
                report["queue_pool_exhaustion"] = "PASS"
            with database.control_engine.connect() as connection:
                require(connection.scalar(text("SELECT 1")) == 1, "Control pool was not isolated")
            with database.history_engine.connect() as connection:
                require(connection.scalar(text("SELECT 1")) == 1, "History pool was not isolated")
        finally:
            for connection in held:
                connection.close()
        with database.blocking_sessions() as holder:
            require(
                lock_referenceable_version(holder, version_id) is not None,
                "Cannot acquire actual environment-version lock",
            )
            with ThreadPoolExecutor(max_workers=1) as pool:

                def contender():
                    with database.blocking_sessions() as db:
                        db.execute(text("SET LOCAL statement_timeout=300"))
                        return lock_referenceable_version(db, version_id)

                future = pool.submit(contender)
                observed = False
                deadline = time.monotonic() + 0.25
                while time.monotonic() < deadline:
                    with database.control_engine.connect() as connection:
                        observed = bool(
                            connection.scalar(
                                text(
                                    "SELECT count(*) FROM pg_stat_activity "
                                    "WHERE datname=current_database() "
                                    "AND cardinality(pg_blocking_pids(pid))>0"
                                )
                            )
                        )
                    if observed:
                        break
                    time.sleep(0.01)
                require(observed, "No real PostgreSQL row-lock wait observed")
                try:
                    future.result(timeout=3)
                    raise AssertionError("Blocked application lock did not time out")
                except OperationalError as exc:
                    require(
                        getattr(exc.orig, "sqlstate", None) == "57014",
                        "Wrong PostgreSQL timeout SQLSTATE",
                    )
            holder.rollback()
        with database.blocking_sessions() as db:
            require(
                lock_referenceable_version(db, version_id) is not None,
                "Application lock did not recover",
            )
            db.rollback()
        report["application_row_lock_timeout"] = {
            "result": "PASS",
            "sqlstate": "57014",
            "recovery": "PASS",
        }

        async def lanes():
            from flowweave.modules.tasks.application.service import claim
            from flowweave.modules.users.application.security import tenant_bypass

            held_async = [await database.engine.connect() for _ in range(2)]
            try:
                async with database.session() as session:
                    try:
                        with tenant_bypass():
                            await session.run_sync(
                                lambda db: claim(
                                    db, "isolated-incident", lease_seconds=30, commit=False
                                )
                            )
                        raise AssertionError(
                            "Worker claim did not encounter the exhausted async pool"
                        )
                    except PoolTimeout:
                        report["worker_claim_pool_exhaustion"] = "PASS"
            finally:
                for connection in held_async:
                    await connection.close()
            async with database.session() as session:
                with tenant_bypass():
                    require(
                        await session.run_sync(
                            lambda db: claim(
                                db, "isolated-incident", lease_seconds=30, commit=False
                            )
                        )
                        is None,
                        "Isolated task queue unexpectedly contained work",
                    )
                await session.rollback()
            report["worker_claim_recovery"] = "PASS"
            with (
                ThreadPoolExecutor(max_workers=2) as executor,
                ThreadPoolExecutor(max_workers=1) as control,
            ):
                container = SimpleNamespace(
                    settings=settings,
                    database=database,
                    blocking_executor=executor,
                    blocking_io_slots=asyncio.Semaphore(2),
                    blocking_capacity=2,
                    blocking_control_executor=control,
                    blocking_control_slots=asyncio.Semaphore(1),
                )
                with database.control_sessions() as holder:
                    require(
                        lock_referenceable_version(holder, version_id) is not None,
                        "Cannot set lock fixture",
                    )
                    pending = [
                        asyncio.create_task(
                            run_blocking(
                                container, lambda db: lock_referenceable_version(db, version_id)
                            )
                        )
                        for _ in range(2)
                    ]
                    try:
                        deadline = time.monotonic() + 1
                        while time.monotonic() < deadline:
                            # Polling uses the production independent poll pool.
                            with database.poll_engine.connect() as connection:
                                blocked = connection.scalar(
                                    text(
                                        "SELECT count(*) FROM pg_stat_activity "
                                        "WHERE datname=current_database() "
                                        "AND cardinality(pg_blocking_pids(pid))>0"
                                    )
                                )
                            if blocked == 2:
                                break
                            await asyncio.sleep(0.01)
                        require(
                            blocked == 2,
                            "Read workers did not both block on the actual environment lock",
                        )
                        try:
                            await run_blocking(container, lambda db: db.scalar(text("SELECT 1")))
                            raise AssertionError("Saturated read lane did not reject")
                        except DomainError as exc:
                            require(
                                exc.status == 503 and exc.code == "RUNTIME_READ_SATURATED",
                                "Unexpected saturation result",
                            )
                            response = {"status": exc.status, "code": exc.code}
                    finally:
                        holder.rollback()
                    await asyncio.gather(*pending)
                require(
                    await run_blocking_control(container, lambda db: db.scalar(text("SELECT 1")))
                    == 1,
                    "Control lane failed after lock release",
                )
                require(
                    await run_blocking(container, lambda db: db.scalar(text("SELECT 1"))) == 1,
                    "Read lane did not recover",
                )
                await database.ping()
                return {
                    "result": "PASS",
                    **response,
                    "recovery": "PASS",
                    "ordinary_async_query": "PASS",
                }

        report["lock_driven_read_saturation"] = asyncio.run(lanes())
        import psycopg

        try:
            with psycopg.connect(
                host="incident-" + uuid4().hex + ".invalid",
                dbname="incident",
                user="incident",
                connect_timeout=1,
            ):
                pass
            raise AssertionError("Invalid isolated DNS name resolved")
        except psycopg.OperationalError as exc:
            require(
                any(
                    x in str(exc).lower()
                    for x in ["resolve", "resolution", "translate host", "name or service"]
                ),
                "Expected actual resolver failure",
            )
            report["dns_failure"] = {
                "result": "PASS",
                "type": type(exc).__name__,
                "sqlstate": exc.sqlstate,
            }
        require(
            all(values["checked_out"] == 0 for values in database.pool_metrics().values()),
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
        "Explicit disposable Docker fault-injection opt-in required",
    )
    logging.disable(logging.CRITICAL)
    print(json.dumps(check()), flush=True)
