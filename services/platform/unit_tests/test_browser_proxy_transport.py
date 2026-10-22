import base64
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from flowweave.shared.errors import DomainError
from flowweave.shared.infrastructure import browser_proxy


@pytest.fixture
def transport(tmp_path: Path):
    starts = tmp_path / "starts"
    executable = tmp_path / "docker"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os,sys\n"
        f'with open({str(starts)!r},"a") as out: out.write(sys.argv[3]+"\\n")\n'
        'assert sys.argv[1:3]==["exec","-i"]\n'
        "os.execv(sys.executable,[sys.executable,*sys.argv[5:]])\n"
    )
    executable.chmod(0o700)
    barrier = threading.Barrier(4)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/target")
                self.end_headers()
                return
            if self.path.startswith("/parallel/"):
                barrier.wait(timeout=5)
            data = (
                b"x" * (browser_proxy.MAX_CONTENT_BYTES + 1)
                if self.path == "/large"
                else self.path.encode()
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Set-Cookie", "upstream-cookie=must-not-leak")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    pool = browser_proxy.BrowserFetchPool(str(executable), max_connections=4)
    yield pool, server.server_port, starts
    pool.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def fetch(pool, port, path="/", *, container="container-1", query=""):
    return pool.fetch(container, scheme="http", port=port, path=path, query=query)


def body(response):
    return base64.b64decode(response["content_base64"])


def test_http_reads_reuse_process_without_reusing_content_or_target(transport):
    pool, port, starts = transport
    assert body(fetch(pool, port, "/first", query="x=1&x=2")) == b"/first?x=1&x=2"
    assert body(fetch(pool, port, "/changed")) == b"/changed"
    assert starts.read_text().splitlines() == ["container-1"]
    assert body(fetch(pool, port, "/new-generation", container="container-2")) == b"/new-generation"
    assert starts.read_text().splitlines() == ["container-1", "container-2"]
    result = fetch(pool, port, "/redirect")
    assert result["status"] == 302
    assert result["headers"]["Location"] == "/target"
    assert "Set-Cookie" not in result["headers"]


def test_parallel_http_reads_are_bounded_and_cannot_mix_responses(transport):
    pool, port, starts = transport
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(lambda index: body(fetch(pool, port, f"/parallel/{index}")), range(4))
        )
    assert results == [f"/parallel/{index}".encode() for index in range(4)]
    assert len(starts.read_text().splitlines()) == 4
    for index in range(8):
        assert body(fetch(pool, port, f"/after/{index}")) == f"/after/{index}".encode()
    assert len(starts.read_text().splitlines()) == 4


def test_dead_idle_expired_connections_are_reaped_and_shutdown_is_final(transport):
    pool, port, starts = transport
    fetch(pool, port)
    process = pool._connections[0].process
    process.kill()
    process.wait(timeout=2)
    assert body(fetch(pool, port, "/recovered")) == b"/recovered"
    assert len(starts.read_text().splitlines()) == 2
    pool._connections[0].last_used -= 20
    pool.reap()
    assert not pool._connections
    fetch(pool, port)
    pool._connections[0].created_at -= 130
    pool.reap()
    assert not pool._connections
    pool.close()
    with pytest.raises(DomainError):
        fetch(pool, port)


def test_oversize_and_invalid_targets_fail_closed_without_poisoning_next_read(transport):
    pool, port, _ = transport
    with pytest.raises(DomainError) as too_large:
        fetch(pool, port, "/large")
    assert too_large.value.code == "AGENT_BROWSER_RESPONSE_TOO_LARGE"
    for path in ["//example.test/", "/a/../b", "/a?redirect=other", "/a#fragment"]:
        with pytest.raises(DomainError):
            fetch(pool, port, path)
    assert body(fetch(pool, port, "/still-readable")) == b"/still-readable"


def test_connection_limit_evicts_only_idle_immutable_container_connections(transport):
    pool, port, starts = transport
    pool.max_connections = 1
    assert body(fetch(pool, port, "/a")) == b"/a"
    old = pool._connections[0].process
    assert body(fetch(pool, port, "/b", container="container-2")) == b"/b"
    assert old.poll() is not None
    assert len(pool._connections) == 1
    assert starts.read_text().splitlines() == ["container-1", "container-2"]


def test_transport_script_never_uses_environment_http_proxy(transport, monkeypatch):
    pool, port, _ = transport
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    assert body(fetch(pool, port, "/direct-loopback")) == b"/direct-loopback"


def test_provider_checks_ownership_on_each_read_even_with_reused_transport(monkeypatch):
    from fastapi.testclient import TestClient

    from flowweave.bootstrap import runtime_provider
    from flowweave.bootstrap.settings import Settings
    from flowweave.shared.infrastructure.docker_control import DockerOwnershipError

    scopes = []
    reads = []

    class Pool:
        def __init__(self, _binary):
            pass

        def fetch(self, container_id, **payload):
            reads.append((container_id, payload))
            return {"status": 200, "headers": {}, "content_base64": "b2s="}

    def inspect(*args, **kwargs):
        scopes.append((args, kwargs))
        if len(scopes) == 3:
            raise DockerOwnershipError("ownership changed")
        return "immutable-container"

    monkeypatch.setattr(runtime_provider, "BrowserFetchPool", Pool)
    monkeypatch.setattr(runtime_provider, "inspect_owned_container", inspect)
    settings = Settings(
        _env_file=None,
        docker_controller_api_key="a" * 32,
        docker_controller_worker_api_key="w" * 32,
    )
    client = TestClient(runtime_provider.create_app(settings))
    payload = {
        "manager_scope": settings.sandbox_manager_scope,
        "resource_name": "fw-sbx-browser-runtime",
        "resource_id": "00000000-0000-0000-0000-000000000001",
        "scheme": "http",
        "port": 5186,
        "path": "/",
        "query": "",
    }
    headers = {"Authorization": "Bearer " + "a" * 32}
    for path in ["/", "/src/main.js"]:
        result = client.post("/v1/browser/fetch", json={**payload, "path": path}, headers=headers)
        assert result.status_code == 200, result.text
    assert client.post("/v1/browser/fetch", json=payload, headers=headers).status_code == 409
    assert len(reads) == 2 and all(row[0] == "immutable-container" for row in reads)
    assert all(row[1]["expected_kind"] == "agent-runtime" for row in scopes)
    assert (
        client.post(
            "/v1/browser/fetch", json=payload, headers={"Authorization": "Bearer " + "w" * 32}
        ).status_code
        == 403
    )
    assert len(scopes) == 3
    client.close()
