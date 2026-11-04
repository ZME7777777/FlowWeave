"""Budget templates, numeric expressions and credential boundaries without external I/O."""

import importlib.util
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session

from flowweave.bootstrap.settings import Settings
from flowweave.modules.model_providers.application import service
from flowweave.modules.model_providers.application.service import ProviderConnectionSnapshot
from flowweave.modules.model_providers.domain.usage_query import (
    ProviderUsageQuery,
    evaluate_metrics,
    parse_expressions,
)
from flowweave.modules.model_providers.infrastructure.client import read_provider_budget
from flowweave.modules.model_providers.infrastructure.models import ModelProvider, ProviderModel
from flowweave.modules.users.application.security import tenant_user
from flowweave.shared.errors import DomainError
from flowweave.shared.schemas import ModelProviderUsagePreviewWrite, ModelProviderWrite
from flowweave.shared.settings import settings_context


def query(**changes):
    return ProviderUsageQuery.model_validate(
        {
            "url": "https://budget.example.test/key/info",
            "expression": "$info.max_budget - info.spend",
            **changes,
        }
    )


def test_expressions_cover_money_ratios_arrays_filters_and_independent_missing_values():
    metrics = evaluate_metrics(
        "剩余: $info.max_budget - info.spend; 使用率: info.spend / info.max_budget %; "
        "汇总: sum(a.*.used); 最大: max(a.*.used); 最小: min(a.*.used); "
        "活动: sum(a[status=active].used); 数量: count(a[status=active].used); "
        "首项: a[0].used; 缺失: missing.balance; 容错: 1 / 0",
        {
            "info": {"max_budget": 100, "spend": "25"},
            "a": [
                {"used": 10, "status": "active"},
                {"used": 20, "status": "inactive"},
                {"used": 30, "status": "active"},
            ],
        },
    )
    assert [metric["value"] for metric in metrics] == [75, 25, 60, 30, 10, 40, 2, 10, None, None]
    assert metrics[0] == {"label": "剩余", "value": 75, "currency": True, "percentage": False}
    assert metrics[1]["percentage"] is True
    assert (
        evaluate_metrics("(1 - credits.used / 70) %", {"credits": {"used": 35}})[0]["value"] == 50
    )


@pytest.mark.parametrize("value", [True, None, "NaN", "Infinity", {}, []])
def test_unknown_and_non_finite_values_are_never_reported_as_zero(value):
    assert evaluate_metrics("data.balance", {"data": {"balance": value}})[0]["value"] is None
    assert evaluate_metrics("sum(a.*.used)", {"a": []})[0]["value"] is None


@pytest.mark.parametrize(
    "source",
    [
        "__import__('os').system('id')",
        "[1, 2]",
        "a ** 1000000",
        "a // 2",
        "a % 2",
        "sum(a, b)",
        "1;",
        "'text'",
        "sum(a, start=2)",
        "a if b else c",
        "",
        "1;" * 12,
    ],
)
def test_expression_language_rejects_code_and_unbounded_operations(source):
    with pytest.raises(ValueError):
        parse_expressions(source)


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "file:///etc/passwd"},
        {"url": "https://user:password@example.test/info"},
        {"url": "https://{key}.example.test/info"},
        {"url": "https://example.test/info?key=plaintext"},
        {"url": "https://example.test/info/{unknown}"},
        {"url": "https://example.test:bad/info"},
        {"headers": {"Authorization": "Bearer plaintext"}},
        {"headers": {"Host": "example.test"}},
        {"headers": {"X-Value": "injected\r\nX-Other: value"}},
        {"method": "DELETE"},
    ],
)
def test_query_validation_rejects_literal_credentials_and_invalid_requests(changes):
    with pytest.raises(ValidationError):
        query(**changes)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_custom_query_uses_templates_and_returns_only_numeric_metrics(method, caplog):
    secret = "fixture/?&= key"
    requests = []

    def upstream(request):
        if request.url.path == "/health":
            return httpx.Response(200)
        requests.append(request)
        assert request.method == method
        assert parse_qs(request.url.query.decode())["access"] == [secret]
        assert request.headers["X-Api-Key"] == secret
        return httpx.Response(200, json={"info": {"max_budget": 10, "spend": 3, "key": secret}})

    snapshot = ProviderConnectionSnapshot(
        "p",
        "https://models.example.test/v1",
        {"Authorization": f"Bearer {secret}"},
        query(
            url="https://budget.example.test/info?access={key}",
            method=method,
            headers={"X-Api-Key": "{key}"},
        ),
    )
    with caplog.at_level(logging.INFO, logger="httpx"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            result = await read_provider_budget(client, snapshot)
            await client.get("https://public.example.test/health")
    assert "public.example.test/health" in caplog.text
    assert len(requests) == 1
    assert result == {
        "status": "AVAILABLE",
        "metrics": [
            {"label": "余额", "value": 7, "currency": True, "percentage": False},
        ],
    }
    assert secret not in json.dumps(result)
    assert "access=" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"location": "https://other.example.test/"}),
        httpx.Response(401, text="private-upstream-secret"),
        httpx.Response(200, text="private-upstream-secret"),
        httpx.Response(200, content=b"x" * 1_048_577),
    ],
)
async def test_failed_queries_do_not_follow_redirects_or_expose_upstream_data(response):
    requests = []

    def upstream(request):
        requests.append(request)
        return response

    snapshot = ProviderConnectionSnapshot(
        "p", "https://models.example.test", {"Authorization": "Bearer fixture"}, query()
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(upstream), follow_redirects=True
    ) as client:
        result = await read_provider_budget(client, snapshot)
    assert result["status"] == "UNAVAILABLE"
    assert "private-upstream-secret" not in json.dumps(result)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_legacy_budget_query_keeps_v1_fallback():
    paths = []

    def upstream(request):
        paths.append(request.url.path)
        return (
            httpx.Response(404)
            if len(paths) == 1
            else httpx.Response(200, json={"info": {"spend": 3, "max_budget": 10}})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        result = await read_provider_budget(
            client, ProviderConnectionSnapshot("p", "https://models.example.test/v1", {})
        )
    assert paths == ["/v1/key/info", "/key/info"]
    assert result == {"status": "AVAILABLE", "spend": 3, "max_budget": 10, "remaining": 7}


@pytest.fixture
def database(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def configure(connection, _):
        connection.create_function("set_config", 3, lambda _key, value, _local: value)

    ModelProvider.__table__.create(engine)
    ProviderModel.__table__.create(engine)
    monkeypatch.setattr(service, "_provider_references", lambda *_: [])
    monkeypatch.setattr(service, "_validate_referenced_models", lambda *_: None)
    with (
        settings_context(Settings(credentials_master_key="")),
        tenant_user("owner"),
        Session(engine) as db,
    ):
        yield db
    engine.dispose()


def write(**changes):
    return ModelProviderWrite.model_validate(
        {
            "name": "budget-provider",
            "base_url": "https://models.example.test/v1",
            "api_key": "fixture-key",
            "models": [{"model_name": "m", "enabled": True, "is_default": True}],
            **changes,
        }
    )


def test_saved_query_roundtrip_legacy_updates_and_explicit_clear(database):
    configuration = query().model_dump()
    provider = service.save_provider(database, write(usage_query=configuration))
    provider_id = provider["id"]
    assert provider["usage_query"] == configuration
    assert "fixture-key" not in json.dumps(provider)
    assert service.provider_usage_snapshot(database, provider_id).usage_query == query()
    # Older clients omit the new field. Preserve the existing query in that case.
    provider = service.save_provider(
        database, write(api_key="", row_version=provider["row_version"]), provider_id
    )
    assert provider["usage_query"] == configuration
    with pytest.raises(DomainError):
        service.save_provider(
            database, write(row_version=provider["row_version"] - 1, usage_query=None), provider_id
        )
    database.rollback()
    provider = service.save_provider(
        database, write(row_version=provider["row_version"], usage_query=None), provider_id
    )
    assert provider["usage_query"] is None


def test_preview_reuses_saved_key_without_writing_and_rejects_other_users(database):
    provider = service.save_provider(database, write())
    payload = ModelProviderUsagePreviewWrite.model_validate(
        {
            "provider_id": provider["id"],
            "base_url": "https://unsaved.example.test/v1",
            "usage_query": query().model_dump(),
        }
    )
    snapshot = service.preview_provider_usage_snapshot(database, payload)
    assert snapshot.headers["Authorization"] == "Bearer fixture-key"
    assert snapshot.base_url == "https://unsaved.example.test/v1"
    assert service.get_provider(database, provider["id"]).usage_query_json is None
    with tenant_user("other-user"), pytest.raises(DomainError):
        service.preview_provider_usage_snapshot(
            database, payload.model_copy(update={"api_key": "replacement"})
        )


def test_literal_key_is_rejected_before_config_persistence(database):
    with pytest.raises(DomainError, match="占位符"):
        service.save_provider(
            database,
            write(usage_query=query(url="https://budget.example.test/fixture-key").model_dump()),
        )


def test_usage_query_migration_preserves_existing_providers_and_supports_downgrade():
    path = Path(__file__).parents[1] / "migrations/versions/0195_model_provider_usage_query.py"
    spec = importlib.util.spec_from_file_location("usage_query_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE model_providers (id TEXT PRIMARY KEY)"))
        connection.execute(text("INSERT INTO model_providers VALUES ('old')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert (
                connection.execute(
                    text("SELECT usage_query_json FROM model_providers WHERE id='old'")
                ).scalar()
                is None
            )
            migration.downgrade()
        assert [
            column["name"] for column in inspect(connection).get_columns("model_providers")
        ] == ["id"]
        assert connection.execute(text("SELECT id FROM model_providers")).scalar() == "old"
    engine.dispose()


def test_http_entry_points_use_saved_credentials_and_unsaved_query_without_mutation(
    database, monkeypatch
):
    from flowweave.modules.model_providers.presentation import router as provider_router
    from flowweave.shared.http import get_container, get_db

    provider = service.save_provider(database, write(usage_query=query().model_dump()))
    requests = []

    def upstream(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer fixture-key"
        return httpx.Response(
            200, json={"info": {"spend": 3, "max_budget": 10}, "data": {"balance": 12}}
        )

    async def run_sync(_db, operation):
        return operation(database)

    monkeypatch.setattr(provider_router, "run_sync", run_sync)
    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app = FastAPI()
    app.include_router(provider_router.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[get_container] = lambda: SimpleNamespace(http=http)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/model-providers/preview-usage",
            json={
                "provider_id": provider["id"],
                "base_url": provider["base_url"],
                "usage_query": query(expression="$data.balance").model_dump(),
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["metrics"][0]["value"] == 12
        assert (
            service.get_provider(database, provider["id"]).usage_query_json == query().model_dump()
        )
        response = client.get(f"/api/v1/model-providers/{provider['id']}/usage")
        assert response.status_code == 200, response.text
        assert response.json()["metrics"][0]["value"] == 7
        response = client.post(
            "/api/v1/model-providers/preview-usage",
            json={
                "base_url": provider["base_url"],
                "api_key": "replacement-key",
                "usage_query": {**query().model_dump(), "method": "DELETE"},
            },
        )
        assert response.status_code == 422
    assert len(requests) == 2
