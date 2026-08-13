from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import zipfile
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from secrets import token_urlsafe
from typing import Any, cast

import yaml
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from flowweave.modules.catalog.application.capability_repository import (
    list_versions,
    publish_dependency_build,
    publish_import,
    resolve_version,
)
from flowweave.modules.tasks.public import enqueue
from flowweave.shared.application.transactions import (
    finish,
    register_commit_action,
    register_rollback_action,
)
from flowweave.shared.artifact_store import get_artifact_store
from flowweave.shared.dependency_builder import get_dependency_builder
from flowweave.shared.domain.agent_definition import (
    normalize_agent_definition_document,
)
from flowweave.shared.domain.runtime_policy import (
    normalize_agent_profile_document,
    normalize_context_policy_document,
    normalize_critic_policy_document,
    normalize_memory_policy_document,
)
from flowweave.shared.domain.tool_policy import (
    DEFAULT_TOOL_POLICY_KEY,
    normalize_tool_policy_document,
)
from flowweave.shared.errors import DomainError
from flowweave.shared.models import (
    CapabilityCollection,
    CapabilityCollectionItem,
    CapabilityImport,
    CapabilityVersion,
    NodeAsset,
    NodeCapabilityRef,
)
from flowweave.shared.schemas import CapabilityValidateWrite
from flowweave.shared.settings import get_settings

SENSITIVE = {"api_key", "apikey", "token", "secret", "password", "authorization"}
ZIP_MAX_COMPRESSED_BYTES = 25 * 1024 * 1024
ZIP_MAX_EXPANDED_BYTES = 100 * 1024 * 1024
ZIP_MAX_FILE_BYTES = 25 * 1024 * 1024
# Keep a generous raw-entry ceiling so harmless macOS metadata does not make a
# valid package fail, while still bounding the work needed to inspect an
# archive. The stricter limit applies after ignored metadata is removed.
ZIP_MAX_RAW_ENTRIES = 5000
ZIP_MAX_FILES = 1000
ZIP_MAX_DEPTH = 8
CONFIG_MAX_BYTES = 1024 * 1024
CONFIG_MAX_ALIASES = 20
CONFIG_MAX_DEPTH = 20
CONFIG_MAX_NODES = 10_000
MCP_SCRIPT_MAX_FILES = 20
MCP_SCRIPT_MAX_FILE_BYTES = 1024 * 1024
MCP_SCRIPT_MAX_TOTAL_BYTES = 10 * 1024 * 1024
MCP_SCRIPT_ALLOWED_SUFFIXES = {
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".sh",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".txt",
}
HOOK_SCRIPT_MAX_FILES = MCP_SCRIPT_MAX_FILES
HOOK_SCRIPT_MAX_FILE_BYTES = MCP_SCRIPT_MAX_FILE_BYTES
HOOK_SCRIPT_MAX_TOTAL_BYTES = MCP_SCRIPT_MAX_TOTAL_BYTES
HOOK_SCRIPT_ALLOWED_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".sh"}
NESTED_ARCHIVE_SUFFIXES = {
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".7z",
    ".rar",
    ".jar",
    ".whl",
}
SKILL_ALLOWED_SUFFIXES = {
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".html",
    ".xml",
    ".css",
    ".csv",
    ".lock",
    ".sh",
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
}
SKILL_ALLOWED_EXTENSIONLESS = {"license", "notice"}
DEPENDENCY_ECOSYSTEMS = {"python", "node", "cli"}
PLATFORM_CLI_VERSIONS = {
    "lark-cli": "1.0.84",
    "uv": "0.7.8",
}
DEPENDENCY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@/-]{0,127}$")
EXACT_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?$")
MCP_SERVER_KEYS = {
    "url",
    "transport",
    "type",
    "command",
    "args",
    "env",
    "cwd",
    "description",
    "icon",
    "timeout",
    "sse_read_timeout",
    "keep_alive",
    "headers",
    "auth",
    "enabled",
}
MCP_TRANSPORTS = {"stdio", "http", "streamable-http", "sse"}
HOOK_EVENTS = {
    "PreToolUse": "pre_tool_use",
    "PostToolUse": "post_tool_use",
    "UserPromptSubmit": "user_prompt_submit",
    "SessionStart": "session_start",
    "SessionEnd": "session_end",
    "Stop": "stop",
}
HOOK_EVENT_KEYS = frozenset(HOOK_EVENTS.values())
HOOK_TYPES = {"command", "script", "prompt", "agent"}
HOOK_DEFINITION_KEYS = {
    "type",
    "name",
    "command",
    "script",
    "prompt",
    "system_prompt",
    "tools",
    "timeout",
    "max_iterations",
    "async",
}


def _ignored_archive_entry(path: PurePosixPath) -> bool:
    return "__MACOSX" in path.parts or path.name == ".DS_Store" or path.name.startswith("._")


def _reject(message: str, **details: Any) -> DomainError:
    return DomainError("IMPORT_REJECTED", message, 422, details)


def _reject_sensitive(value: object, path: str = "$") -> None:
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        for key, child in mapping.items():
            if str(key).lower() in SENSITIVE:
                raise _reject("Capability config contains a secret field", path=f"{path}.{key}")
            _reject_sensitive(child, f"{path}.{key}")
    elif isinstance(value, list):
        sequence = cast(list[object], value)
        for index, child in enumerate(sequence):
            _reject_sensitive(child, f"{path}[{index}]")


def _validate_config_structure(value: object) -> None:
    count = 0

    def walk(current: object, depth: int, ancestors: frozenset[int]) -> None:
        nonlocal count
        count += 1
        if count > CONFIG_MAX_NODES:
            raise _reject("Config expands to too many values")
        if depth > CONFIG_MAX_DEPTH:
            raise _reject("Config nesting exceeds limit")
        if isinstance(current, dict):
            mapping = cast(dict[object, object], current)
            identity = id(mapping)
            if identity in ancestors:
                raise _reject("Recursive YAML aliases are not allowed")
            nested_ancestors = ancestors | {identity}
            for key, child in mapping.items():
                walk(key, depth + 1, nested_ancestors)
                walk(child, depth + 1, nested_ancestors)
        elif isinstance(current, list):
            sequence = cast(list[object], current)
            identity = id(sequence)
            if identity in ancestors:
                raise _reject("Recursive YAML aliases are not allowed")
            nested_ancestors = ancestors | {identity}
            for child in sequence:
                walk(child, depth + 1, nested_ancestors)

    walk(value, 0, frozenset())


def _safe_yaml_load(text: str) -> object:
    try:
        aliases = sum(isinstance(token, yaml.tokens.AliasToken) for token in yaml.scan(text))
    except yaml.YAMLError as exc:
        raise _reject("Invalid JSON or YAML") from exc
    if aliases > CONFIG_MAX_ALIASES:
        raise _reject("YAML alias count exceeds limit")
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _reject("Invalid JSON or YAML") from exc
    _validate_config_structure(value)
    return value


def _frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        value = _safe_yaml_load(parts[1])
    except DomainError as exc:
        raise _reject("SKILL.md frontmatter is invalid") from exc
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _ensure_unique_capability_keys(capabilities: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for capability in capabilities:
        key = str(capability.get("capability_key") or "")
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    if duplicates:
        raise _reject(
            "Capability names must be unique within one import",
            capability_keys=sorted(duplicates),
        )


def _dependencies(value: object) -> dict[str, dict[str, str]]:
    """Validate declarative dependencies without accepting executable text."""

    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise _reject("Skill dependencies must be an object")
    raw = cast(dict[object, object], value)
    unknown = sorted(str(key) for key in raw if str(key) not in DEPENDENCY_ECOSYSTEMS)
    if unknown:
        raise _reject("Unsupported dependency ecosystem", ecosystems=unknown)
    result: dict[str, dict[str, str]] = {}
    total = 0
    for ecosystem in ("python", "node", "cli"):
        values = raw.get(ecosystem)
        if values is None:
            continue
        if not isinstance(values, dict):
            raise _reject("Dependency group must be an object", ecosystem=ecosystem)
        entries: dict[str, str] = {}
        for raw_name, raw_version in cast(dict[object, object], values).items():
            name = str(raw_name).strip()
            version = str(raw_version).strip()
            if not DEPENDENCY_NAME.fullmatch(name) or ".." in name or name.startswith(("/", ".")):
                raise _reject("Dependency name is invalid", ecosystem=ecosystem, name=name)
            if not EXACT_VERSION.fullmatch(version):
                raise _reject(
                    "Dependency version must be exactly pinned",
                    ecosystem=ecosystem,
                    name=name,
                )
            if ecosystem == "cli" and PLATFORM_CLI_VERSIONS.get(name) != version:
                raise _reject(
                    "CLI is not in the platform allowlist",
                    name=name,
                    allowed=PLATFORM_CLI_VERSIONS,
                )
            entries[name] = version
            total += 1
        if entries:
            result[ecosystem] = entries
    if total > 50:
        raise _reject("Skill declares too many dependencies", max_dependencies=50)
    return result


def _validate_skill(content: bytes, fallback_name: str) -> dict[str, Any]:
    if len(content) > ZIP_MAX_COMPRESSED_BYTES:
        raise _reject("ZIP exceeds 25 MiB", max_bytes=ZIP_MAX_COMPRESSED_BYTES)
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            files = archive.infolist()
            if len(files) > ZIP_MAX_RAW_ENTRIES:
                raise _reject(
                    f"ZIP contains {len(files)} raw entries; maximum is {ZIP_MAX_RAW_ENTRIES}",
                    actual_entries=len(files),
                    max_entries=ZIP_MAX_RAW_ENTRIES,
                )
            names: list[str] = []
            total = 0
            effective_entries = 0
            ignored_entries = 0
            for item in files:
                name = item.filename.replace("\\", "/")
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts:
                    raise _reject("Unsafe ZIP path")
                if (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise _reject("Symbolic links are not allowed")
                if item.file_size > ZIP_MAX_FILE_BYTES:
                    raise _reject(
                        "ZIP file exceeds 25 MiB per-file limit",
                        filename=name,
                        max_bytes=ZIP_MAX_FILE_BYTES,
                    )
                total += item.file_size
                if total > ZIP_MAX_EXPANDED_BYTES:
                    raise _reject("Expanded ZIP exceeds 100 MiB", max_bytes=ZIP_MAX_EXPANDED_BYTES)
                if _ignored_archive_entry(path):
                    ignored_entries += 1
                    continue
                effective_entries += 1
                if effective_entries > ZIP_MAX_FILES:
                    raise _reject(
                        f"ZIP contains {effective_entries} effective entries; maximum is "
                        f"{ZIP_MAX_FILES}",
                        actual_entries=effective_entries,
                        max_entries=ZIP_MAX_FILES,
                        ignored_entries=ignored_entries,
                    )
                if len(path.parts) > ZIP_MAX_DEPTH:
                    raise _reject("ZIP path nesting exceeds limit", filename=name)
                if not item.is_dir():
                    suffix = path.suffix.lower()
                    if suffix in NESTED_ARCHIVE_SUFFIXES:
                        raise _reject("Nested archives are not allowed", filename=name)
                    if (
                        suffix not in SKILL_ALLOWED_SUFFIXES
                        and path.name.lower() not in SKILL_ALLOWED_EXTENSIONLESS
                    ):
                        raise _reject("ZIP file extension is not allowed", filename=name)
                    names.append(name)
            skill_files = sorted(x for x in names if x == "SKILL.md" or x.endswith("/SKILL.md"))
            if not skill_files:
                raise _reject("SKILL.md is required")
            capabilities: list[dict[str, Any]] = []
            for skill_file in skill_files:
                try:
                    text = archive.read(skill_file).decode("utf-8")
                except (KeyError, UnicodeDecodeError) as exc:
                    raise _reject("SKILL.md must be UTF-8") from exc
                metadata = _frontmatter(text)
                parent = PurePosixPath(skill_file).parent.name
                key = str(metadata.get("name") or parent or fallback_name or "skill").strip()
                if not key or len(key) > 200:
                    raise _reject("Skill name is invalid", skill_file=skill_file)
                capabilities.append(
                    {
                        "capability_key": key,
                        "normalized_config": {
                            "entry": skill_file,
                            "description": str(metadata.get("description") or "")[:2000],
                            "version": str(metadata.get("version") or ""),
                            "dependencies": _dependencies(metadata.get("dependencies")),
                            "dependency_build_state": (
                                "PENDING" if metadata.get("dependencies") else "NOT_REQUIRED"
                            ),
                        },
                    }
                )
            _ensure_unique_capability_keys(capabilities)
            return {
                "capabilities": capabilities,
                "skill_files": skill_files,
                "files": names[:50],
                "file_count": len(names),
                "raw_entry_count": len(files),
                "effective_entry_count": effective_entries,
                "ignored_entry_count": ignored_entries,
            }
    except zipfile.BadZipFile as exc:
        raise _reject("Invalid ZIP file") from exc


def _validate_plugin(content: bytes, fallback_name: str) -> dict[str, Any]:
    """Validate one OpenHands Plugin directory without executing its content."""

    if len(content) > ZIP_MAX_COMPRESSED_BYTES:
        raise _reject("ZIP exceeds 25 MiB", max_bytes=ZIP_MAX_COMPRESSED_BYTES)
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > ZIP_MAX_RAW_ENTRIES:
                raise _reject(
                    f"ZIP contains {len(entries)} raw entries; maximum is {ZIP_MAX_RAW_ENTRIES}",
                    actual_entries=len(entries),
                    max_entries=ZIP_MAX_RAW_ENTRIES,
                )
            files: dict[str, bytes] = {}
            total = 0
            for item in entries:
                path = PurePosixPath(item.filename.replace("\\", "/"))
                if path.is_absolute() or ".." in path.parts:
                    raise _reject("Unsafe ZIP path", filename=item.filename)
                mode_type = (item.external_attr >> 16) & 0o170000
                if not item.is_dir() and mode_type not in {0, 0o100000}:
                    raise _reject("Plugin ZIP contains a non-regular file", filename=item.filename)
                if item.is_dir() or _ignored_archive_entry(path):
                    continue
                if len(files) >= ZIP_MAX_FILES:
                    raise _reject(
                        f"ZIP contains more than {ZIP_MAX_FILES} effective entries",
                        max_entries=ZIP_MAX_FILES,
                    )
                if len(path.parts) > ZIP_MAX_DEPTH:
                    raise _reject("ZIP path nesting exceeds limit", filename=item.filename)
                if item.file_size > ZIP_MAX_FILE_BYTES:
                    raise _reject(
                        "ZIP file exceeds 25 MiB per-file limit",
                        filename=item.filename,
                        max_bytes=ZIP_MAX_FILE_BYTES,
                    )
                if path.suffix.lower() in NESTED_ARCHIVE_SUFFIXES:
                    raise _reject("Nested archives are not allowed", filename=item.filename)
                if (
                    path.suffix.lower() not in SKILL_ALLOWED_SUFFIXES
                    and path.name.lower() not in SKILL_ALLOWED_EXTENSIONLESS
                ):
                    raise _reject("ZIP file extension is not allowed", filename=item.filename)
                data = archive.read(item)
                total += len(data)
                if total > ZIP_MAX_EXPANDED_BYTES:
                    raise _reject("Expanded ZIP exceeds 100 MiB", max_bytes=ZIP_MAX_EXPANDED_BYTES)
                normalized_path = path.as_posix()
                if normalized_path in files:
                    raise _reject("ZIP contains duplicate paths", filename=normalized_path)
                files[normalized_path] = data

            manifests = sorted(
                path
                for path in files
                if path.endswith("/.plugin/plugin.json")
                or path.endswith("/.claude-plugin/plugin.json")
                or path in {".plugin/plugin.json", ".claude-plugin/plugin.json"}
            )
            if len(manifests) != 1:
                raise _reject(
                    "Plugin ZIP must contain exactly one .plugin/plugin.json or "
                    ".claude-plugin/plugin.json",
                    manifest_count=len(manifests),
                )
            manifest_path = PurePosixPath(manifests[0])
            plugin_prefix = manifest_path.parent.parent
            prefix = "." if plugin_prefix == PurePosixPath(".") else plugin_prefix.as_posix()
            relative_files: dict[str, bytes] = {}
            for path, data in files.items():
                source = PurePosixPath(path)
                try:
                    relative = source.relative_to(plugin_prefix)
                except ValueError as exc:
                    raise _reject(
                        "Plugin ZIP contains files outside its Plugin root", filename=path
                    ) from exc
                relative_files[relative.as_posix()] = data
            try:
                manifest_object = json.loads(files[manifests[0]].decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _reject("Plugin manifest must be valid UTF-8 JSON") from exc
            if not isinstance(manifest_object, dict):
                raise _reject("Plugin manifest must be an object")
            manifest = cast(dict[str, Any], manifest_object)
            _validate_config_structure(manifest)
            _reject_sensitive(manifest)
            name = str(manifest.get("name") or fallback_name).strip()
            version = str(manifest.get("version") or "1.0.0").strip()
            if not name or len(name) > 200 or not version or len(version) > 100:
                raise _reject("Plugin name or version is invalid")

            skill_files = sorted(
                path
                for path in relative_files
                if path.startswith("skills/") and path.endswith("/SKILL.md")
            )
            agent_files = sorted(
                path
                for path in relative_files
                if path.startswith("agents/") and path.endswith(".md")
            )
            if agent_files:
                raise _reject(
                    "OpenHands 1.40.0 loads Plugin agents but does not merge them into the "
                    "running Agent; publish Agent Definitions separately",
                    filenames=agent_files,
                )
            command_files = sorted(
                path
                for path in relative_files
                if path.startswith("commands/") and path.endswith(".md")
            )

            skill_names: list[str] = []
            for skill_file in skill_files:
                try:
                    skill_text = relative_files[skill_file].decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise _reject("Plugin Skill files must be UTF-8", filename=skill_file) from exc
                metadata = _frontmatter(skill_text)
                _reject_sensitive(metadata, f"$.{skill_file}.frontmatter")
                skill_name = str(
                    metadata.get("name") or PurePosixPath(skill_file).parent.name
                ).strip()
                if not skill_name or len(skill_name) > 200:
                    raise _reject("Plugin Skill name is invalid", filename=skill_file)
                skill_names.append(skill_name)
            if len(skill_names) != len(set(skill_names)):
                raise _reject("Plugin Skill names must be unique")

            command_names: list[str] = []
            for command_file in command_files:
                try:
                    command_text = relative_files[command_file].decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise _reject(
                        "Plugin Command files must be UTF-8", filename=command_file
                    ) from exc
                metadata = _frontmatter(command_text)
                _reject_sensitive(metadata, f"$.{command_file}.frontmatter")
                command_name = str(metadata.get("name") or PurePosixPath(command_file).stem).strip()
                if not command_name or len(command_name) > 200:
                    raise _reject("Plugin Command name is invalid", filename=command_file)
                command_names.append(command_name)
            if len(command_names) != len(set(command_names)):
                raise _reject("Plugin Command names must be unique")

            mcp_names: list[str] = []
            if ".mcp.json" in relative_files:
                try:
                    mcp_object = json.loads(relative_files[".mcp.json"].decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise _reject("Plugin .mcp.json must be valid UTF-8 JSON") from exc
                if not isinstance(mcp_object, dict):
                    raise _reject("Plugin .mcp.json root must be an object")
                mcp_document = cast(dict[str, Any], mcp_object)
                _validate_config_structure(mcp_document)
                _reject_sensitive(mcp_document, "$.mcp")
                mcp_names = [
                    str(item["capability_key"]) for item in _mcp_capabilities(mcp_document)
                ]

            hook_events: list[str] = []
            if "hooks/hooks.json" in relative_files:
                try:
                    hook_object = json.loads(relative_files["hooks/hooks.json"].decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise _reject("Plugin hooks/hooks.json must be valid UTF-8 JSON") from exc
                if not isinstance(hook_object, dict):
                    raise _reject("Plugin hooks/hooks.json root must be an object")
                hook_document = cast(dict[str, Any], hook_object)
                _validate_config_structure(hook_document)
                _reject_sensitive(hook_document, "$.hooks")
                _hook_name, _hook_description, hook_config = _normalize_hook_config(hook_document)
                hook_events = sorted(hook_config)

            if not any((skill_names, command_names, mcp_names, hook_events)):
                raise _reject("Plugin must contribute at least one Skill, Command, MCP, or Hook")
            contributions = {
                "skills": skill_names,
                "mcp_servers": mcp_names,
                "hook_events": hook_events,
                "commands": command_names,
            }
            file_hashes = {
                path: hashlib.sha256(data).hexdigest()
                for path, data in sorted(relative_files.items())
            }
            normalized = {
                "entry": prefix,
                "manifest": manifest,
                "description": str(manifest.get("description") or "")[:2000],
                "version": version,
                "contributions": contributions,
                "file_hashes": file_hashes,
                "package_format": "openhands-plugin-v1",
            }
            return {
                "capabilities": [{"capability_key": name, "normalized_config": normalized}],
                "plugin_manifest": manifest,
                "contributions": contributions,
                "files": list(file_hashes)[:50],
                "file_count": len(file_hashes),
            }
    except zipfile.BadZipFile as exc:
        raise _reject("Invalid ZIP file") from exc


def validate_plugin_archive(content: bytes, fallback_name: str) -> dict[str, Any]:
    """Public catalog validator shared by upload and isolated Git resolution."""

    return _validate_plugin(content, fallback_name)


def _string_mapping(value: object, *, server: str, field: str) -> dict[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(child, str)
        for key, child in cast(dict[object, object], value).items()
    ):
        raise _reject(f"MCP {field} must be a string map", server=server, field=field)
    return cast(dict[str, str], value)


def _normalize_mcp_server(name: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _reject("MCP server must be an object", server=name)
    server = {str(key): child for key, child in cast(dict[object, object], value).items()}
    unknown = sorted(set(server) - MCP_SERVER_KEYS)
    if unknown:
        raise _reject("MCP server contains unsupported fields", server=name, fields=unknown)

    typed_transport = server.pop("type", None)
    if typed_transport is not None and "transport" in server:
        raise _reject("MCP server cannot declare both type and transport", server=name)
    transport_value = server.get("transport", typed_transport)
    if transport_value == "shttp":
        transport_value = "http"
    if transport_value is None:
        transport_value = "stdio" if server.get("command") else "streamable-http"
    if not isinstance(transport_value, str) or transport_value not in MCP_TRANSPORTS:
        raise _reject(
            "MCP transport is not supported",
            server=name,
            transport=transport_value,
            supported_transports=sorted(MCP_TRANSPORTS),
        )
    server["transport"] = transport_value

    command = server.get("command")
    url = server.get("url")
    if transport_value == "stdio":
        if not isinstance(command, str) or not command.strip():
            raise _reject("stdio MCP server requires command", server=name)
        if url is not None:
            raise _reject("stdio MCP server cannot declare url", server=name)
        server["command"] = command.strip()
    else:
        if not isinstance(url, str) or not url.strip():
            raise _reject("Remote MCP server requires url", server=name)
        if command is not None:
            raise _reject("Remote MCP server cannot declare command", server=name)
        server["url"] = url.strip()

    args = server.get("args")
    if args is not None and (
        not isinstance(args, list)
        or any(not isinstance(argument, str) for argument in cast(list[object], args))
    ):
        raise _reject("MCP args must be a string list", server=name, field="args")
    for field in ("env", "headers"):
        if field in server:
            server[field] = _string_mapping(server[field], server=name, field=field)
    for field in ("cwd", "description", "icon"):
        if field in server and not isinstance(server[field], str):
            raise _reject("MCP field must be a string", server=name, field=field)
    for field in ("timeout", "sse_read_timeout"):
        value = server.get(field)
        if value is not None and (
            not isinstance(value, int | float) or isinstance(value, bool) or value <= 0
        ):
            raise _reject("MCP timeout must be positive", server=name, field=field)
    for field in ("keep_alive", "enabled"):
        if field in server and not isinstance(server[field], bool):
            raise _reject("MCP field must be boolean", server=name, field=field)
    if "auth" in server:
        if not isinstance(server["auth"], dict):
            raise _reject("MCP auth must be an object", server=name, field="auth")
        auth = cast(dict[object, object], server["auth"])
        if "state" in auth:
            raise _reject(
                "MCP OAuth state must use a governed Secret Reference",
                server=name,
                field="auth.state",
            )
    return server


def _mcp_capabilities(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    roots = [key for key in ("mcpServers", "servers") if key in parsed]
    if len(roots) != 1:
        raise _reject("MCP config must contain exactly one mcpServers or servers root")
    raw_servers = parsed[roots[0]]
    if not isinstance(raw_servers, dict) or not raw_servers:
        raise _reject("MCP config must contain at least one named server")
    capabilities: list[dict[str, Any]] = []
    for raw_name, raw_server in cast(dict[object, object], raw_servers).items():
        name = str(raw_name).strip()
        if not name or len(name) > 200:
            raise _reject("MCP server name is invalid", server=str(raw_name))
        capabilities.append(
            {
                "capability_key": name,
                "normalized_config": _normalize_mcp_server(name, raw_server),
            }
        )
    return capabilities


def _mcp_bundle(
    config_content: bytes,
    capabilities: list[dict[str, Any]],
    payload: CapabilityValidateWrite,
) -> tuple[bytes, list[dict[str, Any]]]:
    if not payload.mcp_scripts:
        return config_content, capabilities
    if payload.capability_type != "MCP":
        raise _reject("Only MCP capabilities can include scripts")
    if len(payload.mcp_scripts) > MCP_SCRIPT_MAX_FILES:
        raise _reject(
            "MCP script count exceeds limit",
            actual_files=len(payload.mcp_scripts),
            max_files=MCP_SCRIPT_MAX_FILES,
        )

    capability_by_name = {str(item["capability_key"]): item for item in capabilities}
    server_positions = {
        str(item["capability_key"]): position for position, item in enumerate(capabilities)
    }
    decoded: list[tuple[str, str, bytes]] = []
    seen: set[tuple[str, str]] = set()
    total_bytes = 0
    for script in payload.mcp_scripts:
        server = script.server.strip()
        capability = capability_by_name.get(server)
        if capability is None:
            raise _reject("MCP script references an unknown server", server=server)
        normalized = cast(dict[str, Any], capability["normalized_config"])
        if normalized.get("transport") != "stdio":
            raise _reject("Only stdio MCP servers can include scripts", server=server)

        filename = script.filename.strip()
        path = PurePosixPath(filename.replace("\\", "/"))
        if (
            path.is_absolute()
            or len(path.parts) != 1
            or path.name in {"", ".", ".."}
            or path.suffix.lower() not in MCP_SCRIPT_ALLOWED_SUFFIXES
        ):
            raise _reject(
                "MCP script filename or extension is not supported",
                server=server,
                filename=filename,
                allowed_extensions=sorted(MCP_SCRIPT_ALLOWED_SUFFIXES),
            )
        identity = (server, path.name)
        if identity in seen:
            raise _reject(
                "MCP script filenames must be unique within one server",
                server=server,
                filename=path.name,
            )
        seen.add(identity)
        try:
            content = base64.b64decode(script.content_base64, validate=True)
        except (ValueError, TypeError) as exc:
            raise _reject(
                "MCP script content is not valid base64",
                server=server,
                filename=path.name,
            ) from exc
        if len(content) > MCP_SCRIPT_MAX_FILE_BYTES:
            raise _reject(
                "MCP script exceeds 1 MiB",
                server=server,
                filename=path.name,
                max_bytes=MCP_SCRIPT_MAX_FILE_BYTES,
            )
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _reject(
                "MCP scripts must be UTF-8 text files",
                server=server,
                filename=path.name,
            ) from exc
        total_bytes += len(content)
        if total_bytes > MCP_SCRIPT_MAX_TOTAL_BYTES:
            raise _reject(
                "MCP scripts exceed 10 MiB in total",
                max_bytes=MCP_SCRIPT_MAX_TOTAL_BYTES,
            )
        decoded.append((server, path.name, content))

    scripts_by_server: dict[str, list[str]] = {}
    hashes_by_server: dict[str, dict[str, str]] = {}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("config.json", config_content)
        for server, filename, content in decoded:
            position = server_positions[server]
            archive_path = f"scripts/{position}/{filename}"
            info = zipfile.ZipInfo(archive_path)
            info.external_attr = (0o755 if Path(filename).suffix == ".sh" else 0o644) << 16
            archive.writestr(info, content)
            scripts_by_server.setdefault(server, []).append(filename)
            hashes_by_server.setdefault(server, {})[filename] = hashlib.sha256(content).hexdigest()

    for server, filenames in scripts_by_server.items():
        capability = capability_by_name[server]
        normalized = cast(dict[str, Any], capability["normalized_config"])
        position = server_positions[server]
        normalized["script_files"] = sorted(filenames)
        normalized["script_hashes"] = hashes_by_server[server]
        normalized["script_archive_prefix"] = f"scripts/{position}"
        normalized["package_format"] = "mcp-bundle-v1"
    return output.getvalue(), capabilities


def _normalize_hook_config(parsed: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    name = str(parsed.get("name") or "hook-policy").strip()
    description = str(parsed.get("description") or "").strip()[:2000]
    if not name or len(name) > 200:
        raise _reject("Hook name is invalid")

    raw_config: object = parsed.get("hooks", parsed)
    if raw_config is parsed:
        raw_config = {
            key: value for key, value in parsed.items() if key not in {"name", "description"}
        }
    if not isinstance(raw_config, dict) or not raw_config:
        raise _reject("Hook config must contain at least one lifecycle event")

    normalized: dict[str, Any] = {}
    for raw_event, raw_matchers in cast(dict[object, object], raw_config).items():
        event_name = str(raw_event)
        event = HOOK_EVENTS.get(event_name, event_name)
        if event not in HOOK_EVENT_KEYS:
            raise _reject(
                "Hook event is not supported by OpenHands",
                event=event_name,
                supported_events=sorted(HOOK_EVENT_KEYS),
            )
        if event in normalized:
            raise _reject("Hook event is declared more than once", event=event)
        if not isinstance(raw_matchers, list) or not raw_matchers:
            raise _reject("Hook event must contain at least one matcher", event=event)

        matchers: list[dict[str, Any]] = []
        for raw_matcher in cast(list[object], raw_matchers):
            if not isinstance(raw_matcher, dict):
                raise _reject("Hook matcher must be an object", event=event)
            matcher = {
                str(key): value for key, value in cast(dict[object, object], raw_matcher).items()
            }
            unknown_matcher = sorted(set(matcher) - {"matcher", "hooks"})
            if unknown_matcher:
                raise _reject(
                    "Hook matcher contains unsupported fields",
                    event=event,
                    fields=unknown_matcher,
                )
            raw_hooks = matcher.get("hooks")
            if not isinstance(raw_hooks, list) or not raw_hooks:
                raise _reject("Hook matcher must contain at least one hook", event=event)

            hooks: list[dict[str, Any]] = []
            for raw_hook in cast(list[object], raw_hooks):
                if not isinstance(raw_hook, dict):
                    raise _reject("Hook definition must be an object", event=event)
                hook = {
                    str(key): value for key, value in cast(dict[object, object], raw_hook).items()
                }
                unknown = sorted(set(hook) - HOOK_DEFINITION_KEYS)
                if unknown:
                    raise _reject(
                        "Hook definition contains unsupported fields",
                        event=event,
                        fields=unknown,
                    )
                hook_type = str(hook.get("type") or "command")
                if hook_type not in HOOK_TYPES:
                    raise _reject("Hook type is not supported", event=event, hook_type=hook_type)
                if hook_type == "command" and not str(hook.get("command") or "").strip():
                    raise _reject("Command Hook requires command", event=event)
                if hook_type == "script":
                    script_name = str(hook.get("script") or "").strip()
                    script_path = PurePosixPath(script_name.replace("\\", "/"))
                    if (
                        script_path.is_absolute()
                        or len(script_path.parts) != 1
                        or script_path.name in {"", ".", ".."}
                        or script_path.suffix.lower() not in HOOK_SCRIPT_ALLOWED_SUFFIXES
                    ):
                        raise _reject(
                            "Script Hook requires a supported uploaded script",
                            event=event,
                            filename=script_name,
                            allowed_extensions=sorted(HOOK_SCRIPT_ALLOWED_SUFFIXES),
                        )
                    if hook.get("command"):
                        raise _reject("Script Hook cannot declare command", event=event)
                    hook["script"] = script_path.name
                if hook_type == "prompt" and not str(hook.get("prompt") or "").strip():
                    raise _reject("Prompt Hook requires prompt", event=event)
                if hook_type == "agent" and hook.get("command"):
                    raise _reject("Agent Hook cannot declare command", event=event)
                if hook_type == "agent" and hook.get("async"):
                    raise _reject("Agent Hook cannot run asynchronously", event=event)
                if event in {"pre_tool_use", "user_prompt_submit", "stop"} and hook.get("async"):
                    raise _reject("Blocking lifecycle Hooks cannot run asynchronously", event=event)

                timeout = hook.get("timeout", 60)
                iterations = hook.get("max_iterations", 3)
                if (
                    not isinstance(timeout, int)
                    or isinstance(timeout, bool)
                    or not 1 <= timeout <= 300
                ):
                    raise _reject("Hook timeout must be between 1 and 300 seconds", event=event)
                if (
                    not isinstance(iterations, int)
                    or isinstance(iterations, bool)
                    or not 1 <= iterations <= 20
                ):
                    raise _reject("Hook max_iterations must be between 1 and 20", event=event)
                raw_tools: object = hook.get("tools", [])
                if not isinstance(raw_tools, list) or any(
                    not isinstance(tool, str) for tool in cast(list[object], raw_tools)
                ):
                    raise _reject("Hook tools must be a string list", event=event)

                normalized_hook = {**hook, "type": hook_type, "timeout": timeout}
                if hook_type == "agent":
                    normalized_hook["max_iterations"] = iterations
                hooks.append(normalized_hook)
            matchers.append(
                {
                    "matcher": str(matcher.get("matcher") or "*"),
                    "hooks": hooks,
                }
            )
        normalized[event] = matchers
    return name, description, normalized


def _hook_capabilities(parsed: dict[str, Any], fallback_name: str) -> list[dict[str, Any]]:
    name, description, normalized = _normalize_hook_config(parsed)
    if name == "hook-policy" and fallback_name:
        name = fallback_name
    return [
        {
            "capability_key": name,
            "normalized_config": {**normalized, "description": description},
        }
    ]


def _hook_bundle(
    config_content: bytes,
    capabilities: list[dict[str, Any]],
    payload: CapabilityValidateWrite,
) -> tuple[bytes, list[dict[str, Any]]]:
    if payload.capability_type != "HOOK":
        raise _reject("Only Hook capabilities can include Hook scripts")
    if len(payload.hook_scripts) > HOOK_SCRIPT_MAX_FILES:
        raise _reject(
            "Hook script count exceeds limit",
            actual_files=len(payload.hook_scripts),
            max_files=HOOK_SCRIPT_MAX_FILES,
        )
    if len(capabilities) != 1:
        raise _reject("A Hook script bundle must contain exactly one Hook policy")

    decoded: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    total_bytes = 0
    for script in payload.hook_scripts:
        filename = script.filename.strip()
        path = PurePosixPath(filename.replace("\\", "/"))
        if (
            path.is_absolute()
            or len(path.parts) != 1
            or path.name in {"", ".", ".."}
            or path.suffix.lower() not in HOOK_SCRIPT_ALLOWED_SUFFIXES
        ):
            raise _reject(
                "Hook script filename or extension is not supported",
                filename=filename,
                allowed_extensions=sorted(HOOK_SCRIPT_ALLOWED_SUFFIXES),
            )
        if path.name in seen:
            raise _reject("Hook script filenames must be unique", filename=path.name)
        seen.add(path.name)
        try:
            content = base64.b64decode(script.content_base64, validate=True)
        except (ValueError, TypeError) as exc:
            raise _reject("Hook script content is not valid base64", filename=path.name) from exc
        if len(content) > HOOK_SCRIPT_MAX_FILE_BYTES:
            raise _reject(
                "Hook script exceeds 1 MiB",
                filename=path.name,
                max_bytes=HOOK_SCRIPT_MAX_FILE_BYTES,
            )
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _reject("Hook scripts must be UTF-8 text files", filename=path.name) from exc
        total_bytes += len(content)
        if total_bytes > HOOK_SCRIPT_MAX_TOTAL_BYTES:
            raise _reject(
                "Hook scripts exceed 10 MiB in total", max_bytes=HOOK_SCRIPT_MAX_TOTAL_BYTES
            )
        decoded.append((path.name, content))

    normalized = cast(dict[str, Any], capabilities[0]["normalized_config"])
    referenced: set[str] = set()
    for event in HOOK_EVENT_KEYS:
        for matcher in cast(list[dict[str, Any]], normalized.get(event, [])):
            for hook in cast(list[dict[str, Any]], matcher.get("hooks", [])):
                if hook.get("type") == "script":
                    referenced.add(str(hook.get("script") or ""))
    missing = sorted(referenced - seen)
    if missing:
        raise _reject("Hook action references an unuploaded script", filenames=missing)
    unused = sorted(seen - referenced)
    if unused:
        raise _reject("Uploaded Hook scripts must be bound to an action", filenames=unused)
    if not decoded:
        return config_content, capabilities

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("config.json", config_content)
        for filename, content in decoded:
            info = zipfile.ZipInfo(f"scripts/0/{filename}")
            info.external_attr = (
                0o755
                if Path(filename).suffix.lower() in {".sh", ".py", ".js", ".mjs", ".cjs"}
                else 0o644
            ) << 16
            archive.writestr(info, content)
    normalized["script_files"] = sorted(seen)
    normalized["script_hashes"] = {
        filename: hashlib.sha256(content).hexdigest() for filename, content in decoded
    }
    normalized["script_archive_prefix"] = "scripts/0"
    normalized["package_format"] = "hook-bundle-v1"
    return output.getvalue(), capabilities


def _decode_and_validate(payload: CapabilityValidateWrite) -> tuple[bytes, dict[str, Any]]:
    try:
        content = base64.b64decode(payload.content_base64, validate=True)
    except (ValueError, TypeError) as exc:
        raise _reject("Invalid base64 content") from exc
    filename = Path(payload.filename).name
    if filename != payload.filename or not filename:
        raise _reject("Invalid filename")
    if payload.capability_type in {"SKILL", "PLUGIN"}:
        if not filename.lower().endswith(".zip"):
            raise _reject(f"{payload.capability_type.title()} must be a ZIP")
        if payload.mcp_scripts or payload.hook_scripts:
            raise _reject("ZIP capabilities cannot include detached scripts")
        validator = (
            _validate_skill if payload.capability_type == "SKILL" else validate_plugin_archive
        )
        return content, validator(content, Path(filename).stem)
    suffix = Path(filename).suffix.lower()
    if suffix != ".json":
        raise _reject(f"{payload.capability_type} config must be JSON")
    if len(content) > CONFIG_MAX_BYTES:
        raise _reject("Config exceeds 1 MiB")
    try:
        text = content.decode("utf-8")
        parsed_object = cast(object, json.loads(text))
        _validate_config_structure(parsed_object)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise _reject("Invalid JSON") from exc
    if not isinstance(parsed_object, dict):
        raise _reject("Config root must be an object")
    parsed = cast(dict[str, Any], parsed_object)
    _reject_sensitive(parsed)
    if payload.capability_type == "MCP":
        capabilities = _mcp_capabilities(parsed)
    elif payload.capability_type == "HOOK":
        capabilities = _hook_capabilities(parsed, Path(filename).stem)
    elif payload.capability_type == "TOOL_POLICY":
        try:
            capability_key, normalized = normalize_tool_policy_document(
                parsed, fallback_key=Path(filename).stem
            )
        except ValueError as exc:
            raise _reject(str(exc)) from exc
        capabilities = [{"capability_key": capability_key, "normalized_config": normalized}]
    elif payload.capability_type == "AGENT_DEFINITION":
        try:
            capability_key, normalized = normalize_agent_definition_document(
                parsed, fallback_key=Path(filename).stem
            )
        except ValueError as exc:
            raise _reject(str(exc)) from exc
        capabilities = [{"capability_key": capability_key, "normalized_config": normalized}]
    elif payload.capability_type in {
        "CONTEXT_POLICY",
        "MEMORY_POLICY",
        "CRITIC_POLICY",
        "AGENT_PROFILE",
    }:
        normalizer = {
            "CONTEXT_POLICY": normalize_context_policy_document,
            "MEMORY_POLICY": normalize_memory_policy_document,
            "CRITIC_POLICY": normalize_critic_policy_document,
            "AGENT_PROFILE": normalize_agent_profile_document,
        }[payload.capability_type]
        try:
            capability_key, normalized = normalizer(parsed, fallback_key=Path(filename).stem)
        except ValueError as exc:
            raise _reject(str(exc)) from exc
        capabilities = [{"capability_key": capability_key, "normalized_config": normalized}]
    else:
        raise _reject("Capability type is no longer supported")
    if payload.capability_type == "MCP":
        content, capabilities = _mcp_bundle(content, capabilities, payload)
    elif payload.capability_type == "HOOK":
        content, capabilities = _hook_bundle(content, capabilities, payload)
    elif payload.mcp_scripts:
        raise _reject("Only MCP capabilities can include scripts")
    if payload.capability_type != "MCP" and payload.mcp_scripts:
        raise _reject("Only MCP capabilities can include scripts")
    if payload.capability_type != "HOOK" and payload.hook_scripts:
        raise _reject("Only Hook capabilities can include Hook scripts")
    _ensure_unique_capability_keys(capabilities)
    return content, {
        "capabilities": capabilities,
        "config": parsed,
        "script_count": len(payload.mcp_scripts) + len(payload.hook_scripts),
    }


@dataclass(frozen=True, slots=True)
class PreparedCapabilityImport:
    token: str
    token_digest: str
    capability_type: str
    filename: str
    content: bytes
    content_hash: str
    preview: dict[str, Any]
    expires_at: datetime
    storage_key: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilityCommitPlan:
    import_id: str
    source_key: str
    final_key: str
    expired: bool


@dataclass(frozen=True, slots=True)
class PreparedSkillUpdate:
    capability_id: str
    expected_digest: str
    prepared: PreparedCapabilityImport


def prepare_validation(payload: CapabilityValidateWrite) -> PreparedCapabilityImport:
    """Validate and freeze an import before object-store or database work."""

    content, preview = _decode_and_validate(payload)
    token = token_urlsafe(32)
    return PreparedCapabilityImport(
        token=token,
        token_digest=hashlib.sha256(token.encode()).hexdigest(),
        capability_type=payload.capability_type,
        filename=payload.filename,
        content=content,
        content_hash=hashlib.sha256(content).hexdigest(),
        preview=preview,
        expires_at=datetime.now(UTC)
        + timedelta(seconds=get_settings().capability_import_ttl_seconds),
    )


def store_validation_source(prepared: PreparedCapabilityImport) -> PreparedCapabilityImport:
    """Write the validated source while no database transaction is active."""

    from dataclasses import replace

    try:
        key = get_artifact_store().put_temporary("capability-imports", prepared.content)
    except OSError as exc:
        raise DomainError(
            "CAPABILITY_STORAGE_UNAVAILABLE",
            "Capability storage is temporarily unavailable",
            503,
        ) from exc
    return replace(prepared, storage_key=key)


def discard_validation_source(prepared: PreparedCapabilityImport) -> None:
    if prepared.storage_key is not None:
        get_artifact_store().delete(prepared.storage_key)


def register_validation(db: Session, prepared: PreparedCapabilityImport) -> dict[str, Any]:
    """Persist a pre-written source reference in one short transaction."""

    if prepared.storage_key is None:
        raise RuntimeError("Capability import source was not stored")
    source_key = prepared.storage_key
    register_rollback_action(db, lambda: get_artifact_store().delete(source_key))
    item = CapabilityImport(
        token_digest=prepared.token_digest,
        capability_type=prepared.capability_type,
        filename=prepared.filename,
        content_hash=prepared.content_hash,
        storage_key=source_key,
        byte_size=len(prepared.content),
        preview_json=prepared.preview,
        expires_at=prepared.expires_at,
    )
    db.add(item)
    db.flush()
    enqueue(
        db,
        task_type="CLEANUP_CAPABILITY_IMPORT",
        aggregate_type="CAPABILITY_IMPORT",
        aggregate_id=item.id,
        idempotency_key=f"cleanup-capability-import:{item.id}",
        available_at=item.expires_at,
    )
    finish(db)
    return {
        "import_token": prepared.token,
        "content_hash": prepared.content_hash,
        "preview": prepared.preview,
        "warnings": [],
        "errors": [],
        "expires_at": prepared.expires_at.isoformat(),
    }


def cleanup_expired_import(db: Session, import_id: str) -> None:
    stmt = select(CapabilityImport).where(CapabilityImport.id == import_id)
    if db.bind and db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    item = db.scalar(stmt)
    if not item or item.state == "COMMITTED":
        return
    current_time = datetime.now(UTC)
    if item.state == "VALIDATED" and _utc(item.expires_at) > current_time:
        return
    item.state = "EXPIRED"
    source_key = item.storage_key
    register_commit_action(db, lambda: get_artifact_store().delete(source_key))
    finish(db)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def prepare_commit(db: Session, token: str) -> CapabilityCommitPlan:
    """Freeze an import source in a short transaction before finalize I/O."""

    token_digest = hashlib.sha256(token.encode()).hexdigest()
    stmt = select(CapabilityImport).where(CapabilityImport.token_digest == token_digest)
    if db.bind and db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    item = db.scalar(stmt)
    if not item or item.state != "VALIDATED":
        raise _reject("Import token is invalid, expired, or already consumed")
    if item.capability_type not in {
        "SKILL",
        "PLUGIN",
        "MCP",
        "HOOK",
        "TOOL_POLICY",
        "AGENT_DEFINITION",
        "CONTEXT_POLICY",
        "MEMORY_POLICY",
        "CRITIC_POLICY",
        "AGENT_PROFILE",
    }:
        raise _reject("Capability type is no longer supported")
    expired = _utc(item.expires_at) <= datetime.now(UTC)
    if expired:
        item.state = "EXPIRED"
        source_key = item.storage_key
        register_commit_action(db, lambda: get_artifact_store().delete(source_key))
    final_key = f"capability-imports/{item.content_hash[:2]}/{item.content_hash}-{item.id}"
    plan = CapabilityCommitPlan(item.id, item.storage_key, final_key, expired)
    finish(db)
    return plan


def finalize_commit_source(plan: CapabilityCommitPlan) -> str:
    """Finalize an import object while no database transaction is active."""

    if plan.expired:
        raise _reject("Import token is invalid, expired, or already consumed")
    try:
        return get_artifact_store().finalize(plan.source_key, plan.final_key)
    except FileNotFoundError as exc:
        raise _reject("Imported content is unavailable") from exc
    except OSError as exc:
        raise DomainError(
            "CAPABILITY_STORAGE_UNAVAILABLE",
            "Capability storage is temporarily unavailable",
            503,
        ) from exc


def confirm_commit(db: Session, plan: CapabilityCommitPlan, final_key: str) -> dict[str, Any]:
    """CAS-confirm the finalized object reference and consume the one-time token."""

    consumed_at = datetime.now(UTC)
    claimed_id = db.scalar(
        update(CapabilityImport)
        .where(
            CapabilityImport.id == plan.import_id,
            CapabilityImport.state == "VALIDATED",
            CapabilityImport.storage_key == plan.source_key,
            CapabilityImport.expires_at > consumed_at,
        )
        .values(state="COMMITTED", storage_key=final_key, consumed_at=consumed_at)
        .returning(CapabilityImport.id)
        .execution_options(synchronize_session=False)
    )
    if claimed_id is None:
        db.expire_all()
        current = db.get(CapabilityImport, plan.import_id)
        winner_owns_object = (
            current is not None
            and current.state == "COMMITTED"
            and current.storage_key == final_key
        )
        if not winner_owns_object:
            register_rollback_action(db, lambda: get_artifact_store().delete(final_key))
        raise _reject("Import token is invalid, expired, or already consumed")
    # If the DB commit fails, the finalized object is not referenced and can be reclaimed.
    register_rollback_action(db, lambda: get_artifact_store().delete(final_key))
    db.expire_all()
    item = db.get(CapabilityImport, plan.import_id)
    if item is None:
        raise _reject("Import token is invalid, expired, or already consumed")
    published = publish_import(db, item)
    for position, entry in enumerate(item.preview_json.get("capabilities", [])):
        normalized = cast(dict[str, Any], entry.get("normalized_config", {}))
        dependencies = normalized.get("dependencies")
        if isinstance(dependencies, dict) and dependencies:
            enqueue(
                db,
                task_type="BUILD_CAPABILITY_DEPENDENCIES",
                aggregate_type="CAPABILITY_IMPORT",
                aggregate_id=item.id,
                idempotency_key=f"build-capability-dependencies:{item.id}:{position}",
                payload={"position": position},
            )
    finish(db)
    capabilities = [
        {
            "capability_id": capability.version.id,
            "capability_type": capability.package.capability_type,
            "capability_key": capability.package.capability_key,
            "normalized_config": capability.runtime_config(),
        }
        for capability in published
    ]
    return {
        "id": item.id,
        "capability_type": item.capability_type,
        "filename": item.filename,
        "content_hash": item.content_hash,
        "storage_key": final_key,
        "byte_size": item.byte_size,
        "preview": item.preview_json,
        "capabilities": capabilities,
        "committed_at": consumed_at.isoformat(),
    }


def list_capabilities(db: Session) -> list[dict[str, Any]]:
    """List immutable repository versions; imports are source audit only."""

    return list_versions(db)


def read_skill_source(db: Session, capability_id: str) -> dict[str, Any]:
    published = resolve_version(db, capability_id)
    if published.package.capability_type != "SKILL":
        raise DomainError("CAPABILITY_NOT_EDITABLE", "Only Skill source can be edited", 422)
    normalized = published.version.normalized_config_json or {}
    skill_file = str(normalized.get("entry") or "")
    try:
        package = get_artifact_store().read(published.blob.storage_key)
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            content = archive.read(skill_file).decode("utf-8")
    except (FileNotFoundError, KeyError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise DomainError(
            "CAPABILITY_SOURCE_UNAVAILABLE", "Skill source is unavailable", 422
        ) from exc
    return {
        "id": capability_id,
        "capability_key": published.package.capability_key,
        "filename": published.version.source_filename,
        "entry": skill_file,
        "content": content,
    }


def prepare_skill_update(db: Session, capability_id: str, content: str) -> PreparedSkillUpdate:
    published = resolve_version(db, capability_id)
    if published.package.capability_type != "SKILL":
        raise DomainError("CAPABILITY_NOT_EDITABLE", "Only Skill source can be edited", 422)
    normalized = published.version.normalized_config_json or {}
    skill_file = str(normalized.get("entry") or "")
    try:
        source = get_artifact_store().read(published.blob.storage_key)
        output = io.BytesIO()
        replaced = False
        with (
            zipfile.ZipFile(io.BytesIO(source)) as archive,
            zipfile.ZipFile(output, "w") as revised,
        ):
            for info in archive.infolist():
                data = archive.read(info.filename) if not info.is_dir() else b""
                if info.filename.replace("\\", "/") == skill_file:
                    if data.decode("utf-8") == content:
                        raise DomainError(
                            "CAPABILITY_SOURCE_UNCHANGED",
                            "Capability source has not changed",
                            422,
                        )
                    data = content.encode("utf-8")
                    replaced = True
                revised.writestr(info, data)
        if not replaced:
            raise KeyError(skill_file)
    except (FileNotFoundError, KeyError, OSError, zipfile.BadZipFile) as exc:
        raise DomainError(
            "CAPABILITY_SOURCE_UNAVAILABLE", "Skill source is unavailable", 422
        ) from exc
    payload = CapabilityValidateWrite(
        capability_type="SKILL",
        filename=published.version.source_filename,
        content_base64=base64.b64encode(output.getvalue()).decode("ascii"),
    )
    prepared = prepare_validation(payload)
    revised_entries = [
        candidate
        for candidate in cast(list[dict[str, Any]], prepared.preview.get("capabilities", []))
        if cast(dict[str, Any], candidate.get("normalized_config", {})).get("entry") == skill_file
    ]
    if len(revised_entries) != 1:
        raise DomainError(
            "CAPABILITY_SOURCE_INVALID",
            "Edited capability is not present in the validated package",
            422,
        )
    revised_key = str(revised_entries[0].get("capability_key") or "")
    original_key = published.package.capability_key
    if revised_key != original_key:
        raise DomainError(
            "CAPABILITY_IDENTITY_CHANGED",
            "Editing a capability cannot change its name",
            422,
            {"expected": original_key, "actual": revised_key},
        )
    preview = deepcopy(prepared.preview)
    preview["capabilities"] = revised_entries
    return PreparedSkillUpdate(
        capability_id=capability_id,
        expected_digest=published.version.digest,
        prepared=replace(prepared, preview=preview),
    )


def store_skill_update_source(update_plan: PreparedSkillUpdate) -> PreparedSkillUpdate:
    return replace(update_plan, prepared=store_validation_source(update_plan.prepared))


def finalize_skill_update_source(update_plan: PreparedSkillUpdate) -> str:
    temporary_key = update_plan.prepared.storage_key
    if temporary_key is None:
        raise RuntimeError("Capability update source was not stored")
    final_key = (
        f"capability-imports/{update_plan.prepared.content_hash[:2]}/"
        f"{update_plan.prepared.content_hash}-{update_plan.capability_id}-"
        f"{update_plan.prepared.token_digest[:12]}"
    )
    try:
        return get_artifact_store().finalize(temporary_key, final_key)
    except FileNotFoundError as exc:
        raise DomainError(
            "CAPABILITY_SOURCE_UNAVAILABLE", "Capability source is unavailable", 422
        ) from exc
    except OSError as exc:
        raise DomainError(
            "CAPABILITY_STORAGE_UNAVAILABLE",
            "Capability storage is temporarily unavailable",
            503,
        ) from exc


def confirm_skill_update(
    db: Session, update_plan: PreparedSkillUpdate, final_key: str
) -> dict[str, Any]:
    """Publish a new immutable Skill version without rebinding consumers."""

    register_rollback_action(db, lambda: get_artifact_store().delete(final_key))
    baseline = db.scalar(
        select(CapabilityVersion)
        .where(CapabilityVersion.id == update_plan.capability_id)
        .with_for_update()
    )
    if (
        baseline is None
        or baseline.state != "PUBLISHED"
        or baseline.digest != update_plan.expected_digest
    ):
        raise DomainError(
            "CAPABILITY_SOURCE_CONFLICT",
            "Capability source changed while it was being edited",
            409,
        )
    now = datetime.now(UTC)
    imported = CapabilityImport(
        token_digest=update_plan.prepared.token_digest,
        capability_type="SKILL",
        filename=update_plan.prepared.filename,
        content_hash=update_plan.prepared.content_hash,
        storage_key=final_key,
        byte_size=len(update_plan.prepared.content),
        preview_json=update_plan.prepared.preview,
        state="COMMITTED",
        expires_at=update_plan.prepared.expires_at,
        consumed_at=now,
    )
    db.add(imported)
    db.flush()
    published = publish_import(db, imported)
    if len(published) != 1:
        raise DomainError("CAPABILITY_SOURCE_INVALID", "Capability source is invalid", 422)
    revised = published[0]
    if revised.package.id != baseline.package_id:
        raise DomainError(
            "CAPABILITY_IDENTITY_CHANGED",
            "Editing a capability cannot change its identity",
            422,
        )
    normalized = revised.version.normalized_config_json or {}
    if normalized.get("dependencies"):
        enqueue(
            db,
            task_type="BUILD_CAPABILITY_DEPENDENCIES",
            aggregate_type="CAPABILITY_IMPORT",
            aggregate_id=imported.id,
            idempotency_key=f"build-capability-dependencies:{imported.id}:0",
            payload={"position": 0},
        )
    revised_id = revised.version.id
    finish(db)
    return next(item for item in list_versions(db) if item["id"] == revised_id)


def process_dependency_build(db: Session, import_id: str, position: int) -> None:
    """Build a pinned dependency bundle without holding a database transaction."""

    source_version = db.scalar(
        select(CapabilityVersion).where(
            CapabilityVersion.source_import_id == import_id,
            CapabilityVersion.source_position == position,
        )
    )
    if source_version is None or source_version.state != "PUBLISHED":
        raise RuntimeError("Capability version is unavailable")
    source = resolve_version(db, source_version.id)
    normalized = dict(source.version.normalized_config_json or {})
    raw_dependencies = normalized.get("dependencies")
    dependencies = (
        cast(dict[str, dict[str, str]], raw_dependencies)
        if isinstance(raw_dependencies, dict)
        else {}
    )
    if not dependencies:
        return
    if normalized.get("dependency_build_state") == "READY":
        return
    source_digest = source.version.digest

    # The builder can access package registries, so close the database transaction first.
    # Its container receives only this validated manifest and never application credentials.
    db.rollback()
    bundle = get_dependency_builder().build(dependencies)
    digest = hashlib.sha256(bundle.content).hexdigest()
    storage_key = f"capability-dependencies/{digest[:2]}/{digest}.zip"
    try:
        get_artifact_store().put(storage_key, bundle.content)
        current_version = db.get(CapabilityVersion, source.version.id)
        if (
            current_version is None
            or current_version.state != "PUBLISHED"
            or current_version.digest != source_digest
        ):
            raise RuntimeError("Capability version disappeared during dependency build")
        current = resolve_version(db, current_version.id)
        current_normalized = dict(current.version.normalized_config_json or {})
        if current_normalized.get("dependencies") != dependencies:
            raise RuntimeError("Capability dependencies changed during build")
        current_normalized.update(
            {
                "dependency_build_state": "READY",
                "dependency_storage_key": storage_key,
                "dependency_content_hash": digest,
                "dependency_manifest": bundle.manifest,
            }
        )
        current_normalized.pop("dependency_build_error", None)
        _derived, created = publish_dependency_build(db, current, current_normalized)
        if created:
            register_rollback_action(db, lambda: get_artifact_store().delete(storage_key))
        finish(db)
    except BaseException:
        if not db.in_transaction():
            get_artifact_store().delete(storage_key)
        raise


def _references(db: Session, capability_version_id: str) -> list[dict[str, str]]:
    rows = db.execute(
        select(NodeCapabilityRef.node_asset_id, NodeAsset.name)
        .join(NodeAsset, NodeAsset.id == NodeCapabilityRef.node_asset_id)
        .where(
            NodeAsset.deleted_at.is_(None),
            NodeCapabilityRef.capability_version_id == capability_version_id,
        )
        .order_by(NodeAsset.name, NodeAsset.id)
    ).all()
    return [{"id": node_id, "name": node_name} for node_id, node_name in rows]


def _collection_references(db: Session, capability_version_id: str) -> list[dict[str, str]]:
    rows = db.execute(
        select(CapabilityCollection.id, CapabilityCollection.name)
        .join(
            CapabilityCollectionItem,
            CapabilityCollectionItem.collection_id == CapabilityCollection.id,
        )
        .where(CapabilityCollectionItem.capability_version_id == capability_version_id)
        .order_by(CapabilityCollection.name, CapabilityCollection.id)
    ).all()
    return [{"id": collection_id, "name": name} for collection_id, name in rows]


def delete_capabilities(db: Session, capability_ids: list[str]) -> dict[str, Any]:
    unique_ids = list(dict.fromkeys(capability_ids))
    blocked: list[dict[str, Any]] = []
    deletable: list[CapabilityVersion] = []
    for capability_id in unique_ids:
        published = resolve_version(db, capability_id)
        if (
            published.package.capability_type == "TOOL_POLICY"
            and published.package.capability_key == DEFAULT_TOOL_POLICY_KEY
        ):
            blocked.append(
                {
                    "id": capability_id,
                    "name": published.package.capability_key,
                    "relation": "BUILTIN_CAPABILITY",
                    "nodes": [],
                }
            )
            continue
        nodes = _references(db, capability_id)
        collections = _collection_references(db, capability_id)
        if nodes or collections:
            reference: dict[str, Any] = {
                "id": capability_id,
                "name": published.package.capability_key,
                "relation": "NODE_CAPABILITY" if nodes else "CAPABILITY_COLLECTION",
                "nodes": nodes,
            }
            # Preserve the existing node-only response shape while exposing
            # collection references when they actually block deletion.
            if collections:
                reference["collections"] = collections
            blocked.append(reference)
        else:
            deletable.append(published.version)

    for version in deletable:
        version.state = "RETIRED"
    finish(db)
    return {
        "deleted_ids": [version.id for version in deletable],
        "blocked": blocked,
    }
