from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import stat
import sys
import zipfile
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

# The resolver uses stdout as a strict machine-readable JSON channel. OpenHands
# prints an import-time banner unless this is set before importing the SDK.
os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

from openhands.sdk.marketplace.registration import MarketplaceRegistration  # noqa: E402
from openhands.sdk.marketplace.registry import MarketplaceRegistry  # noqa: E402
from openhands.sdk.plugin import Plugin, fetch_plugin_with_resolution  # noqa: E402

COMMIT = re.compile(r"^[0-9a-f]{40}$")
REPO_PATH = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")
MAX_FILES = 1000
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_BYTES = 25 * 1024 * 1024
MAX_DEPTH = 8
SOURCE_SEGMENT = re.compile(r"^[A-Za-z0-9_.~-]{1,128}$")


def _validate_remote_source(
    source: str, commit: str, repo_path: str | None, allowed_hosts: set[str]
) -> tuple[str, str, str | None]:
    if source.startswith("github:"):
        source = f"https://github.com/{source.removeprefix('github:')}"
    parsed = urlsplit(source)
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or host not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or bool(parsed.query)
        or bool(parsed.fragment)
        or not COMMIT.fullmatch(commit)
    ):
        raise ValueError("remote Plugin source is not immutable and credential-free")
    segments = parsed.path.strip("/").split("/")
    if (
        len(segments) < 2
        or len(segments) > 16
        or any(
            segment in {"", ".", ".."} or not SOURCE_SEGMENT.fullmatch(segment)
            for segment in segments
        )
    ):
        raise ValueError("invalid Plugin repository path")
    if repo_path is not None and (
        not REPO_PATH.fullmatch(repo_path)
        or any(part in {"", ".", ".."} for part in repo_path.split("/"))
    ):
        raise ValueError("invalid Plugin repository subpath")
    return f"https://{host}/{'/'.join(segments)}", commit, repo_path


def validate_input(
    payload: object,
) -> tuple[str, str, str | None, str | None, set[str]]:
    if not isinstance(payload, dict):
        raise ValueError("request must be an object")
    value = cast(dict[str, object], payload)
    schema_version = value.get("schema_version")
    if schema_version not in {1, 2}:
        raise ValueError("unsupported resolver request schema")
    source, commit = (
        str(value.get("source") or ""),
        str(value.get("commit") or "").lower(),
    )
    kind = str(value.get("source_kind") or "GIT")
    plugin_name = str(value.get("plugin_name") or "") or None
    raw_hosts = value.get("allowed_hosts")
    if not isinstance(raw_hosts, list) or not raw_hosts:
        raise ValueError("allowed hosts are required")
    allowed_hosts = {str(host).lower().rstrip(".") for host in cast(list[object], raw_hosts)}
    raw_repo_path = value.get("repo_path")
    repo_path = str(raw_repo_path) if raw_repo_path is not None else None
    source, commit, repo_path = _validate_remote_source(source, commit, repo_path, allowed_hosts)
    if kind not in {"GIT", "MARKETPLACE"}:
        raise ValueError("unsupported Plugin source kind")
    if (schema_version == 1 and kind != "GIT") or (schema_version == 2 and kind != "MARKETPLACE"):
        raise ValueError("resolver schema does not match Plugin source kind")
    if kind == "MARKETPLACE" and (
        plugin_name is None
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", plugin_name) is None
    ):
        raise ValueError("invalid Marketplace Plugin name")
    if kind == "GIT" and plugin_name is not None:
        raise ValueError("Git Plugin request cannot select a Marketplace entry")
    return source, commit, repo_path, plugin_name, allowed_hosts


def canonical_zip(root: Path) -> tuple[bytes, dict[str, str], int]:
    files: list[tuple[str, bytes, int]] = []
    total = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        if path.is_symlink():
            raise ValueError("Plugin contains a symbolic link")
        if not path.is_file():
            continue
        if len(relative.parts) > MAX_DEPTH:
            raise ValueError("Plugin path depth exceeds limit")
        content = path.read_bytes()
        if len(content) > MAX_FILE_BYTES:
            raise ValueError("Plugin file exceeds size limit")
        total += len(content)
        if total > MAX_EXPANDED_BYTES or len(files) >= MAX_FILES:
            raise ValueError("Plugin package exceeds expansion limits")
        mode = 0o555 if path.stat().st_mode & 0o111 else 0o444
        files.append((relative.as_posix(), content, mode))
    output = io.BytesIO()
    hashes: dict[str, str] = {}
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=False
    ) as archive:
        for name, content, mode in files:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED)
            hashes[name] = hashlib.sha256(content).hexdigest()
    bundle = output.getvalue()
    if not bundle or len(bundle) > MAX_ARCHIVE_BYTES:
        raise ValueError("Plugin ZIP exceeds compressed size limit")
    return bundle, hashes, total


def main() -> None:
    source, commit, repo_path, plugin_name, allowed_hosts = validate_input(json.load(sys.stdin))
    # The source was allowlisted by the control plane. Prevent Git from
    # following an HTTPS redirect to another host, consulting credential
    # helpers, prompting, or invoking any local/ext/SSH transport.
    git_config = {
        "http.followRedirects": "false",
        "credential.helper": "",
        "protocol.file.allow": "never",
        "protocol.ext.allow": "never",
        "protocol.ssh.allow": "never",
        "protocol.git.allow": "never",
    }
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
    os.environ["GIT_CONFIG_COUNT"] = str(len(git_config))
    for index, (key, value) in enumerate(git_config.items()):
        os.environ[f"GIT_CONFIG_KEY_{index}"] = key
        os.environ[f"GIT_CONFIG_VALUE_{index}"] = value
    marketplace_report: dict[str, object] | None = None
    if plugin_name is None:
        path, resolved_commit = fetch_plugin_with_resolution(
            source=source,
            cache_dir=Path("/work/cache"),
            ref=commit,
            update=False,
            repo_path=repo_path,
        )
        resolved_source, resolved_repo_path = source, repo_path
    else:
        registration = MarketplaceRegistration(
            name="governed", source=source, ref=commit, repo_path=repo_path
        )
        fetched = MarketplaceRegistry([registration]).get_marketplace_with_resolution("governed")
        if (fetched.resolved_ref or "").lower() != commit:
            raise ValueError("Marketplace did not resolve to the requested commit")
        entry = fetched.marketplace.get_plugin(plugin_name)
        if entry is None:
            raise ValueError("Marketplace Plugin entry was not found")
        entry_source, entry_ref, entry_repo_path = fetched.marketplace.resolve_plugin_source(entry)
        entry_path = Path(entry_source)
        try:
            relative = entry_path.resolve().relative_to(fetched.path.resolve())
        except (ValueError, OSError):
            if entry_ref is None:
                raise ValueError(
                    "external Marketplace Plugin source must resolve a Git ref"
                ) from None
            external_source = entry_source
            if external_source.startswith("github:"):
                external_source = f"https://github.com/{external_source.removeprefix('github:')}"
            path, resolved_ref = fetch_plugin_with_resolution(
                source=external_source,
                cache_dir=Path("/work/plugin-cache"),
                ref=entry_ref,
                update=False,
                repo_path=entry_repo_path,
            )
            resolved_commit = (resolved_ref or "").lower()
            resolved_source, _, resolved_repo_path = _validate_remote_source(
                external_source, resolved_commit, entry_repo_path, allowed_hosts
            )
        else:
            path = entry_path
            resolved_commit = commit
            resolved_source = source
            relative_path = relative.as_posix() if relative.parts else None
            resolved_repo_path = (
                "/".join(part for part in (repo_path, relative_path) if part) or None
            )
            _validate_remote_source(
                resolved_source, resolved_commit, resolved_repo_path, allowed_hosts
            )
        marketplace_report = {
            "marketplace_source": source,
            "marketplace_commit": commit,
            "marketplace_repo_path": repo_path,
            "marketplace_name": fetched.marketplace.name,
            "plugin_name": plugin_name,
        }
    if not COMMIT.fullmatch((resolved_commit or "").lower()):
        raise ValueError("Plugin source did not resolve to a complete commit")
    bundle, hashes, expanded_bytes = canonical_zip(path)
    plugin = Plugin.load(path)
    report: dict[str, Any] = {
        "schema_version": 1,
        "openhands_version": "1.42.0",
        "name": plugin.name,
        "file_count": len(hashes),
        "expanded_bytes": expanded_bytes,
        "file_hashes": hashes,
        "source_kind": "MARKETPLACE" if plugin_name else "GIT",
    }
    if marketplace_report is not None:
        report["marketplace"] = marketplace_report
    json.dump(
        {
            "content_base64": base64.b64encode(bundle).decode("ascii"),
            "resolved_source": resolved_source,
            "resolved_commit": resolved_commit,
            "resolved_repo_path": resolved_repo_path,
            "report": report,
        },
        sys.stdout,
        separators=(",", ":"),
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
