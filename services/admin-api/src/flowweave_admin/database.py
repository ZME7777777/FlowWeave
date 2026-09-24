from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import psycopg
from psycopg.rows import dict_row

from flowweave_admin.settings import Settings


@contextmanager
def connect(settings: Settings) -> Iterator[Any]:
    with psycopg.connect(
        settings.normalized_database_url,
        row_factory=cast(Any, dict_row),
        autocommit=True,
        options="-c default_transaction_read_only=on -c statement_timeout=3000",
    ) as connection:
        yield cast(Any, connection)
