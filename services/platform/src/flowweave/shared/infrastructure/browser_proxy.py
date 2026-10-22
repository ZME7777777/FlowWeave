"""Bounded, short-lived stdio connections for Runtime-loopback HTTP reads.

No listener, Runtime file or Agent execution protocol is installed. Each child
can only issue the same loopback GETs as the existing browser fetch endpoint.
"""

from __future__ import annotations

import base64
import json
import os
import select
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, cast

from flowweave.shared.errors import DomainError

MAX_CONTENT_BYTES = 2 * 1024 * 1024
_MAX_WIRE_BYTES = 2_800_000
_IDLE_SECONDS = 15.0
_LIFETIME_SECONDS = 120.0

# A fixed, read-only HTTP transport. Request data is never executable code and
# never contains a host, credential, arbitrary command or filesystem path.
_FETCH_SCRIPT = r"""
import base64
import json
import select
import sys
import time
import urllib.error
import urllib.request

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
deadline = time.monotonic() + 120
while time.monotonic() < deadline:
    if not select.select([sys.stdin], [], [], min(15, deadline-time.monotonic()))[0]:
        break
    line = sys.stdin.readline(32_769)
    if not line or len(line) > 32_768:
        break
    try:
        data = json.loads(line)
        scheme, port, path, query = (data[key] for key in ('scheme', 'port', 'path', 'query'))
        if scheme not in ('http', 'https') or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError()
        if (not isinstance(path, str) or not path.startswith('/') or len(path) > 2000
                or '//' in path or '\\' in path or '?' in path or '#' in path
                or any(part in ('.', '..') for part in path.split('/'))):
            raise ValueError()
        if not isinstance(query, str) or len(query) > 4000:
            raise ValueError()
        target = f'{scheme}://127.0.0.1:{port}{path}' + ('?' + query if query else '')
        request = urllib.request.Request(target, headers={'Accept-Encoding': 'identity'})
        try:
            response = opener.open(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            content = response.read(2_097_153)
            if len(content) > 2_097_152:
                result = {'error': 'too_large'}
            else:
                result = {'status': response.status,
                    'headers': {k: v for k, v in response.headers.items()
                        if k.lower() in ('content-type', 'cache-control', 'location')},
                    'content_base64': base64.b64encode(content).decode()}
    except Exception:
        result = {'error': 'unavailable'}
    print(json.dumps(result), flush=True)
"""


def _unavailable() -> DomainError:
    return DomainError(
        "AGENT_BROWSER_TARGET_UNAVAILABLE",
        "The local service is unavailable in this Agent Runtime",
        502,
    )


@dataclass(eq=False)
class _Connection:
    container_id: str
    process: subprocess.Popen[bytes]
    created_at: float
    last_used: float
    busy: bool = False

    def close(self) -> None:
        if self.process.stdin:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
        if self.process.stdout:
            self.process.stdout.close()

    def fetch(self, payload: dict[str, Any]) -> dict[str, Any]:
        assert self.process.stdin is not None and self.process.stdout is not None
        data = memoryview(json.dumps(payload).encode() + b"\n")
        while data:
            written = self.process.stdin.write(data)
            if not written:
                raise _unavailable()
            data = data[written:]
        self.process.stdin.flush()
        content = bytearray()
        deadline = time.monotonic() + 12
        descriptor = self.process.stdout.fileno()
        while len(content) <= _MAX_WIRE_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([descriptor], [], [], remaining)[0]:
                raise _unavailable()
            chunk = os.read(descriptor, min(65_536, _MAX_WIRE_BYTES + 1 - len(content)))
            if not chunk:
                raise _unavailable()
            content.extend(chunk)
            if content.endswith(b"\n"):
                raw: object = json.loads(content)
                if not isinstance(raw, dict):
                    raise ValueError("Invalid transport response")
                result = cast(dict[str, Any], raw)
                if result.get("error") == "too_large":
                    raise DomainError(
                        "AGENT_BROWSER_RESPONSE_TOO_LARGE",
                        "The local service response is too large",
                        413,
                    )
                if result.get("error"):
                    raise _unavailable()
                if not 100 <= int(result["status"]) <= 599 or not isinstance(
                    result["headers"], dict
                ):
                    raise ValueError("Invalid transport response")
                decoded = base64.b64decode(result["content_base64"], validate=True)
                if len(decoded) > MAX_CONTENT_BYTES:
                    raise _unavailable()
                return result
        raise _unavailable()


class BrowserFetchPool:
    """Reuse only connections to an already ownership-checked immutable ID.

    The caller MUST inspect ownership on every request, before entering this
    pool. It never caches a resource name, generation, permission or response.
    """

    def __init__(self, docker_binary: str, *, max_connections: int = 8) -> None:
        self.docker_binary = docker_binary
        self.max_connections = max_connections
        self._condition = threading.Condition()
        self._connections: list[_Connection] = []
        self._closed = False

    def _expired(self, connection: _Connection) -> bool:
        now = time.monotonic()
        return (
            connection.process.poll() is not None
            or now - connection.last_used >= _IDLE_SECONDS
            or now - connection.created_at >= _LIFETIME_SECONDS
        )

    def _borrow(self, container_id: str) -> _Connection:
        deadline = time.monotonic() + 12
        with self._condition:
            while not self._closed:
                self.reap()
                for connection in self._connections:
                    if connection.container_id == container_id and not connection.busy:
                        connection.busy = True
                        return connection
                if len(self._connections) >= self.max_connections:
                    idle = [item for item in self._connections if not item.busy]
                    if idle:
                        oldest = min(idle, key=lambda item: item.last_used)
                        self._connections.remove(oldest)
                        oldest.close()
                if len(self._connections) < self.max_connections:
                    process = subprocess.Popen(
                        [
                            self.docker_binary,
                            "exec",
                            "-i",
                            container_id,
                            "/runtime/.venv/bin/python",
                            "-u",
                            "-c",
                            _FETCH_SCRIPT,
                        ],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        bufsize=0,
                    )
                    now = time.monotonic()
                    connection = _Connection(container_id, process, now, now, busy=True)
                    self._connections.append(connection)
                    return connection
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
        raise _unavailable()

    def fetch(
        self, container_id: str, *, scheme: str, port: int, path: str, query: str
    ) -> dict[str, Any]:
        try:
            connection = self._borrow(container_id)
        except OSError:
            raise _unavailable() from None
        healthy = False
        try:
            result = connection.fetch(dict(scheme=scheme, port=port, path=path, query=query))
            healthy = True
            return result
        except (OSError, ValueError, KeyError, TypeError):
            raise _unavailable() from None
        finally:
            with self._condition:
                connection.busy = False
                connection.last_used = time.monotonic()
                if not healthy and connection in self._connections:
                    self._connections.remove(connection)
                    connection.close()
                self._condition.notify_all()

    def reap(self) -> None:
        with self._condition:
            for connection in list(self._connections):
                if not connection.busy and self._expired(connection):
                    self._connections.remove(connection)
                    connection.close()
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            for connection in self._connections:
                connection.close()
            self._connections.clear()
            self._condition.notify_all()
