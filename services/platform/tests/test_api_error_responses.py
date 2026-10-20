from fastapi.testclient import TestClient
from sqlalchemy.exc import DataError

from flowweave.bootstrap.api import create_app


def _assert_error_shape(response, code: str, status_code: int) -> None:
    assert response.status_code == status_code
    assert response.headers["X-Request-ID"]
    body = response.json()["error"]
    assert body["code"] == code
    assert isinstance(body["message"], str)
    assert isinstance(body["details"], dict)
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_unknown_route_returns_structured_error(client) -> None:
    _assert_error_shape(client.get("/api/v1/not-a-route"), "ROUTE_NOT_FOUND", 404)


def test_unhandled_exception_returns_safe_structured_error(settings) -> None:
    app = create_app(settings)

    @app.get("/test/unhandled-error")
    async def unhandled_error() -> None:
        raise RuntimeError("sensitive internal detail")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/test/unhandled-error")

    _assert_error_shape(response, "INTERNAL_SERVER_ERROR", 500)
    assert "sensitive internal detail" not in response.text


def test_database_data_error_returns_structured_error(settings) -> None:
    app = create_app(settings)

    @app.get("/test/data-error")
    async def data_error() -> None:
        raise DataError("INSERT", {}, ValueError("value exceeds database column length"))

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/test/data-error")

    _assert_error_shape(response, "DATA_INVALID", 422)
