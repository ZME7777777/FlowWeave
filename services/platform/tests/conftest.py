from __future__ import annotations

import base64
import io
import os
import shutil
import zipfile
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, inspect
from sqlalchemy.orm import Session, sessionmaker

from flowweave.bootstrap.api import create_app
from flowweave.bootstrap.container import Container, build_container
from flowweave.bootstrap.settings import Settings
from flowweave.shared import models as _models  # noqa: F401
from flowweave.shared.database import create_sync_session_factory
from flowweave.shared.sandbox import sandbox_context


def _ensure_test_database(url: str) -> None:
    parsed = psycopg.conninfo.conninfo_to_dict(
        url.replace("postgresql+psycopg://", "postgresql://")
    )
    database = parsed.pop("dbname")
    parsed["dbname"] = "postgres"
    with psycopg.connect(**parsed, autocommit=True) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (database,)
        ).fetchone()
        if not exists:
            connection.execute(f'CREATE DATABASE "{database}"')


@pytest.fixture(scope="session")
def test_database_url() -> Iterator[str]:
    configured = os.getenv("TEST_DATABASE_URL")
    if configured:
        _ensure_test_database(configured)
        yield configured
        return

    from testcontainers.postgres import PostgresContainer

    with PostgresContainer(
        image="postgres:16.9-alpine3.21",
        username="flowweave",
        password="flowweave_test",
        dbname="flowweave_platform_test",
        driver="psycopg",
    ) as postgres:
        yield postgres.get_connection_url()


@pytest.fixture(scope="session")
def settings(test_database_url: str) -> Settings:
    return Settings(
        database_url=test_database_url,
        human_write_token="test-human-token",
        credentials_master_key="Qy0d9T_0Y4GxN31PqYqzRo6YD_s-hnbJFRb_v8xQwFc=",
        runtime_adapter="mock",
        execution_mode="inline",
        seed_demo=False,
        artifact_root=Path("./test-artifacts"),
        workspace_root=Path("./test-workspaces"),
    )


@pytest.fixture(scope="session")
def worker_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"execution_mode": "worker", "worker_id": "test-worker"})


@pytest.fixture(scope="session")
def sync_session_factory(settings: Settings) -> Iterator[sessionmaker[Session]]:
    sessions = create_sync_session_factory(settings)
    engine: Engine = sessions.kw["bind"]
    yield sessions
    engine.dispose()


@pytest.fixture(scope="session")
def container(
    settings: Settings, sync_session_factory: sessionmaker[Session]
) -> Iterator[Container]:
    value = build_container(settings, role="api")
    engine: Engine = sync_session_factory.kw["bind"]
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    command.upgrade(config, "head")
    yield value
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")


@pytest.fixture(scope="session")
def worker_container(worker_settings: Settings) -> Iterator[Container]:
    value = build_container(worker_settings, role="worker")
    yield value


@pytest.fixture
def db_session_factory(
    sync_session_factory: sessionmaker[Session],
) -> sessionmaker[Session]:
    return sync_session_factory


@pytest.fixture(autouse=True)
def database(container: Container, sync_session_factory: sessionmaker[Session]) -> Iterator[None]:
    shutil.rmtree("./test-artifacts", ignore_errors=True)
    shutil.rmtree("./test-workspaces", ignore_errors=True)
    engine: Engine = sync_session_factory.kw["bind"]
    existing = [name for name in inspect(engine).get_table_names() if name != "alembic_version"]
    if existing:
        table_names = ", ".join(f'"{name}"' for name in existing)
        with engine.begin() as connection:
            connection.exec_driver_sql(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE")
    with sandbox_context(container.sandbox):
        yield
    shutil.rmtree("./test-artifacts", ignore_errors=True)
    shutil.rmtree("./test-workspaces", ignore_errors=True)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as value:
        value.headers["Authorization"] = "Bearer test-human-token"
        yield value


@pytest.fixture
def worker_client(worker_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(worker_settings)) as value:
        value.headers["Authorization"] = "Bearer test-human-token"
        yield value


@pytest.fixture
def public_client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as value:
        yield value


def _import_test_skill(api_client: TestClient) -> dict:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("test-skill/SKILL.md", "# Test Skill\n")
    validated = api_client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "SKILL",
            "filename": "test-skill.zip",
            "content_base64": base64.b64encode(buffer.getvalue()).decode(),
        },
    )
    assert validated.status_code == 200, validated.text
    committed = api_client.post(
        "/api/v1/capability-imports",
        json={"import_token": validated.json()["import_token"]},
    )
    assert committed.status_code == 201, committed.text
    return committed.json()["capabilities"][0]


@pytest.fixture
def skill_capability(client: TestClient) -> dict:
    return _import_test_skill(client)


@pytest.fixture
def worker_skill_capability(worker_client: TestClient) -> dict:
    return _import_test_skill(worker_client)
