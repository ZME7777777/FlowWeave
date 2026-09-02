from __future__ import annotations

import json
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

from flowweave.cli import main


class FakeResponse:
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._payload

    def close(self) -> None:
        return None


def configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    monkeypatch.setenv(main.CONFIG_ENV, str(path))
    assert main.main(["config", "init", "--base-url", "https://example.test/flowweave"]) == 0
    return path


def test_config_init_and_show(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = configure(monkeypatch, tmp_path)
    capsys.readouterr()

    assert main.main(["config", "show"]) == 0

    shown = json.loads(capsys.readouterr().out)
    assert shown == {"base_url": "https://example.test/flowweave", "config_path": str(path)}
    assert path.stat().st_mode & 0o777 == 0o600


def test_api_resolves_prefixed_base_url_and_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(monkeypatch, tmp_path)
    capsys.readouterr()
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> FakeResponse:
        captured["url"] = request.full_url
        captured["method"] = request.method
        captured["body"] = request.data
        captured["headers"] = dict(request.header_items())
        captured["timeout"] = timeout
        return FakeResponse(201, {"id": "flow-1"})

    monkeypatch.setattr(main, "urlopen", fake_urlopen)

    assert main.main(["api", "post", "/flows", "--data", '{"name":"Flow"}', "-q", "page=1"]) == 0

    assert captured["url"] == "https://example.test/flowweave/api/v1/flows?page=1"
    assert captured["method"] == "POST"
    assert json.loads(captured["body"]) == {"name": "Flow"}
    assert captured["headers"]["Content-type"] == "application/json"
    assert json.loads(capsys.readouterr().out) == {"id": "flow-1"}


def test_openapi_paths_sorts_routes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(monkeypatch, tmp_path)
    capsys.readouterr()

    def fake_urlopen(request: Any, timeout: float) -> FakeResponse:
        assert request.full_url == "https://example.test/flowweave/openapi.json"
        return FakeResponse(
            200,
            {"paths": {"/api/v1/flows": {"post": {}, "get": {}}, "/health": {"get": {}}}},
        )

    monkeypatch.setattr(main, "urlopen", fake_urlopen)

    assert main.main(["openapi", "--paths"]) == 0

    assert json.loads(capsys.readouterr().out) == [
        {"method": "GET", "path": "/api/v1/flows"},
        {"method": "POST", "path": "/api/v1/flows"},
        {"method": "GET", "path": "/health"},
    ]


def test_resource_delete_and_dry_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure(monkeypatch, tmp_path)
    capsys.readouterr()

    assert main.main(["resource", "flows", "delete", "flow-1", "--dry-run"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "method": "DELETE",
        "payload": None,
        "url": "https://example.test/flowweave/api/v1/flows/flow-1",
    }


def test_upload_dry_run_builds_prefixed_multipart_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure(monkeypatch, tmp_path)
    capsys.readouterr()
    attachment = tmp_path / "brief.txt"
    attachment.write_text("brief", encoding="utf-8")

    assert (
        main.main(
            [
                "upload",
                "post",
                "/agent-workspaces/workspace-1/attachments",
                "--form",
                "label=brief",
                "--file",
                f"file={attachment}",
                "--dry-run",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["method"] == "POST"
    assert (
        payload["url"]
        == "https://example.test/flowweave/api/v1/agent-workspaces/workspace-1/attachments"
    )
    assert payload["payload"] == {
        "fields": {"label": "brief"},
        "files": [{"field": "file", "path": str(attachment)}],
    }


def test_websocket_dry_run_preserves_prefix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure(monkeypatch, tmp_path)
    capsys.readouterr()

    assert (
        main.main(
            [
                "ws",
                "/agent-workspaces/workspace-1/runtime/stream",
                "--message-json",
                '{"type":"ping"}',
                "--max-messages",
                "1",
                "--dry-run",
            ]
        )
        == 0
    )

    assert json.loads(capsys.readouterr().out) == {
        "max_messages": 1,
        "message": '{"type": "ping"}',
        "url": "wss://example.test/flowweave/api/v1/agent-workspaces/workspace-1/runtime/stream",
    }


def test_http_error_has_structured_platform_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure(monkeypatch, tmp_path)

    def fake_urlopen(request: Any, timeout: float) -> FakeResponse:
        body = BytesIO(json.dumps({"error": {"code": "INVALID_COMMAND"}}).encode("utf-8"))
        raise HTTPError(request.full_url, 422, "unprocessable", Message(), body)

    monkeypatch.setattr(main, "urlopen", fake_urlopen)

    with pytest.raises(SystemExit) as exited:
        main.main(["api", "get", "/flows"])

    assert exited.value.code == 2
