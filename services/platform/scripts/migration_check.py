"""Run the PostgreSQL migration round trip in an isolated temporary database."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql


@contextmanager
def source_database_url() -> Iterator[str]:
    configured = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    if configured:
        yield configured
        return

    from testcontainers.postgres import PostgresContainer

    with PostgresContainer(
        image=(
            "postgres:16.9-alpine3.21"
            "@sha256:36e8aabaa6fa6037537cff64011fa45a200fe2ba202141b9aca48cff3df7ad42"
        ),
        username="flowweave",
        password="flowweave_migration",
        dbname="flowweave",
        driver="psycopg",
    ) as postgres:
        yield postgres.get_connection_url()


def check(source_url: str) -> None:
    connection_url = source_url.replace("postgresql+psycopg://", "postgresql://", 1)
    parameters = psycopg.conninfo.conninfo_to_dict(connection_url)
    source_database = parameters.pop("dbname", "flowweave")
    database = f"{source_database}_migration_{uuid4().hex[:10]}"
    admin_parameters = {**parameters, "dbname": "postgres"}

    with psycopg.connect(**admin_parameters, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))

    target_url = source_url.rsplit("/", 1)[0] + f"/{database}"
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.attributes["database_url"] = target_url
    try:
        command.upgrade(config, "head")
        command.downgrade(config, "0005_execution")
        command.upgrade(config, "head")
    finally:
        with psycopg.connect(**admin_parameters, autocommit=True) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database,),
            )
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))


def main() -> None:
    with source_database_url() as source_url:
        check(source_url)


if __name__ == "__main__":
    main()
