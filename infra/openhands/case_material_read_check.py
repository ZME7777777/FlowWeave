"""Local acceptance: a distinct Runtime reads Case artifacts over native MCP/HTTP.

Configuration is read only from stdin; credentials and content are never printed.
No host workspace or source container mount is required.
"""

import base64
import hashlib
import importlib.metadata
import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen


def check(config: dict) -> dict:
    lock = json.loads(Path("/runtime/openhands-source.lock.json").read_text())
    provenance = json.loads(
        Path("/runtime/openhands-source-provenance.json").read_text()
    )
    assert lock["source_commit"] == config["source_commit"]
    assert lock["archive_sha256"] == config["archive_sha256"]
    assert provenance["source_archive_sha256"] == config["archive_sha256"]
    versions = {
        name: importlib.metadata.version(name)
        for name in (
            "openhands-agent-server",
            "openhands-sdk",
            "openhands-tools",
            "openhands-workspace",
        )
    }
    assert set(versions.values()) == {"1.54.0"}
    headers = {"Authorization": "Bearer " + config["token"]}
    with urlopen(
        Request(config["content_url"], headers=headers), timeout=15
    ) as response:
        content = response.read()
    assert len(content) == config["byte_size"]
    assert hashlib.sha256(content).hexdigest() == config["content_hash"]
    chunks, offset = [], 0
    while True:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "read_case_material",
                "arguments": {
                    "operation": "read",
                    "artifact_version_id": config["artifact_id"],
                    "offset": offset,
                },
            },
        }
        request = Request(
            config["mcp_url"],
            data=json.dumps(payload).encode(),
            headers={**headers, "Content-Type": "application/json"},
        )
        with urlopen(request, timeout=15) as response:
            result = json.load(response)["result"]
        assert not result["isError"]
        chunk = json.loads(result["content"][0]["text"])
        chunks.append(base64.b64decode(chunk["content"]))
        offset = chunk["next_offset"]
        if offset is None:
            break
    assert b"".join(chunks) == content
    return {
        "status": "passed",
        "source_commit": lock["source_commit"],
        "package_versions": versions,
        "byte_size": len(content),
        "checks": ["separate_container_standard_url", "native_material_chunks", "hash"],
    }


if __name__ == "__main__":
    try:
        report = check(json.load(sys.stdin))
    except Exception as exc:
        # Network exceptions may contain private endpoints. Print only the type.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(report))
