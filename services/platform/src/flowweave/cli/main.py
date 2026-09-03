from __future__ import annotations

import argparse
import json
import mimetypes
import os
import tempfile
import tomllib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

CONFIG_ENV = "FLOWWEAVE_CONFIG_PATH"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "flowweave" / "config.toml"
API_PREFIX = "/api/v1"

RESOURCE_PATHS = {
    "flows": "/flows",
    "runs": "/flow-runs",
    "environments": "/terminal-environments",
    "capabilities": "/capabilities",
    "node-assets": "/node-assets",
    "node-directories": "/node-directories",
    "model-providers": "/model-providers",
    "memory-sources": "/memory-sources",
    "capability-collections": "/capability-collections",
}


class CliError(Exception):
    """An expected command error which should be shown without a traceback."""


@dataclass(frozen=True)
class Config:
    base_url: str


def config_path() -> Path:
    value = os.environ.get(CONFIG_ENV)
    return Path(value).expanduser() if value else DEFAULT_CONFIG_PATH


def normalize_base_url(value: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CliError("--base-url 必须是绝对 HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CliError("--base-url 不能包含凭据、查询参数或片段")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def load_config() -> Config:
    path = config_path()
    if not path.exists():
        raise CliError(
            f"尚未配置 FlowWeave。请执行：flowweave config init --base-url <URL>（{path}）"
        )
    try:
        with path.open("rb") as file:
            values = tomllib.load(file)
        base_url = values.get("platform", {}).get("base_url")
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CliError(f"无法读取 FlowWeave 配置 {path}：{exc}") from exc
    if not isinstance(base_url, str):
        raise CliError(f"FlowWeave 配置 {path} 未定义 platform.base_url")
    return Config(base_url=normalize_base_url(base_url))


def save_config(base_url: str, *, overwrite: bool) -> Config:
    path = config_path()
    normalized = normalize_base_url(base_url)
    if path.exists() and not overwrite:
        raise CliError(f"配置已存在于 {path}；传入 --force 可覆盖")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    content = f"[platform]\nbase_url = {json.dumps(normalized)}\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix="config.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            temporary.write(content)
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except OSError as exc:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise CliError(f"无法写入 FlowWeave 配置 {path}：{exc}") from exc
    return Config(base_url=normalized)


def parse_json(value: str | None, source: str) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise CliError(f"{source} 必须是合法 JSON：{exc.msg}") from exc


def data_from_args(args: argparse.Namespace) -> Any:
    if args.data is not None and args.data_file is not None:
        raise CliError("--data 与 --data-file 只能使用其中一个")
    if args.data_file is not None:
        try:
            return parse_json(Path(args.data_file).read_text(encoding="utf-8"), "--data-file")
        except OSError as exc:
            raise CliError(f"无法读取 --data-file：{exc}") from exc
    return parse_json(args.data, "--data")


def assignments(values: list[str], option: str) -> list[tuple[str, str]]:
    assigned: list[tuple[str, str]] = []
    for value in values:
        name, separator, assigned_value = value.partition("=")
        if not separator or not name:
            raise CliError(f"{option} 必须使用 name=value 形式")
        assigned.append((name, assigned_value))
    return assigned


def multipart_body(fields: list[str], files: list[str]) -> tuple[bytes, str, dict[str, Any]]:
    boundary = f"----flowweave-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    summary_files: list[dict[str, str]] = []

    def append_text(value: str) -> None:
        chunks.append(value.encode("utf-8"))

    for name, value in assignments(fields, "--form"):
        append_text(f"--{boundary}\\r\\n")
        append_text(f'Content-Disposition: form-data; name="{name}"\\r\\n\\r\\n')
        append_text(value)
        append_text("\\r\\n")
    for name, raw_path in assignments(files, "--file"):
        path = Path(raw_path).expanduser()
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise CliError(f"无法读取 --file {path}：{exc}") from exc
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        append_text(f"--{boundary}\\r\\n")
        append_text(f'Content-Disposition: form-data; name="{name}"; filename="{path.name}"\\r\\n')
        append_text(f"Content-Type: {content_type}\\r\\n\\r\\n")
        chunks.append(content)
        append_text("\\r\\n")
        summary_files.append({"field": name, "path": str(path)})
    append_text(f"--{boundary}--\\r\\n")
    return (
        b"".join(chunks),
        f"multipart/form-data; boundary={boundary}",
        {
            "fields": dict(assignments(fields, "--form")),
            "files": summary_files,
        },
    )


def headers_from_args(values: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for value in values:
        name, separator, header_value = value.partition(":")
        if not separator or not name.strip():
            raise CliError("--header 必须使用 'Name: value' 形式")
        headers[name.strip()] = header_value.strip()
    return headers


def query_from_args(values: list[str]) -> list[tuple[str, str]]:
    return assignments(values, "--query")


def api_url(base_url: str, path: str, *, raw: bool, query: list[tuple[str, str]]) -> str:
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise CliError("PATH 必须是相对 API 路径，不能是完整 URL")
    if not parsed.path.startswith("/"):
        raise CliError("PATH 必须以 '/' 开头")
    prefix = "" if raw else API_PREFIX
    if raw or parsed.path.startswith(API_PREFIX + "/"):
        api_path = parsed.path
    else:
        api_path = prefix + parsed.path
    merged_query = parse_qsl(parsed.query, keep_blank_values=True) + query
    return urlunsplit(("", "", base_url.rstrip("/") + api_path, urlencode(merged_query), ""))


def websocket_url(base_url: str, path: str, *, query: list[tuple[str, str]]) -> str:
    url = api_url(base_url, path, raw=False, query=query)
    parsed = urlsplit(url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, ""))


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def response_body(response: Any) -> Any:
    content = response.read()
    if not content:
        return {"status": response.status}
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"status": response.status, "body": content.decode("utf-8", errors="replace")}


def request_api(
    config: Config,
    method: str,
    path: str,
    *,
    payload: Any,
    headers: dict[str, str],
    raw: bool,
    query: list[tuple[str, str]],
    timeout: float,
    dry_run: bool,
    encoded_body: bytes | None = None,
    content_type: str | None = None,
) -> Any:
    url = api_url(config.base_url, path, raw=raw, query=query)
    if dry_run:
        return {"method": method, "url": url, "payload": payload}
    if encoded_body is not None and payload is not None:
        raise CliError("CLI 内部错误：请求不能同时包含 JSON 和已编码数据")
    body = (
        encoded_body
        if encoded_body is not None
        else (None if payload is None else json.dumps(payload).encode("utf-8"))
    )
    request_headers = {"Accept": "application/json", **headers}
    if body is not None and not any(name.lower() == "content-type" for name in request_headers):
        request_headers["Content-Type"] = content_type or "application/json"
    request = Request(url, data=body, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL comes from explicit config.
            return response_body(response)
    except HTTPError as exc:
        error = response_body(exc)
        raise CliError(f"HTTP {exc.code}: {json.dumps(error, ensure_ascii=False)}") from exc
    except URLError as exc:
        raise CliError(f"无法连接 FlowWeave {url}：{exc.reason}") from exc


def add_payload_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--data", help="JSON 请求体")
    group.add_argument("--data-file", help="包含 JSON 请求体的 UTF-8 文件")


def add_request_arguments(parser: argparse.ArgumentParser) -> None:
    add_payload_arguments(parser)
    parser.add_argument(
        "-H", "--header", action="append", default=[], help="HTTP 请求头（Name: value）"
    )
    parser.add_argument(
        "-q", "--query", action="append", default=[], help="查询参数（name=value）"
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="请求超时秒数")
    parser.add_argument(
        "--dry-run", action="store_true", help="只打印请求，不发送"
    )


def add_transport_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-H", "--header", action="append", default=[], help="WebSocket HTTP 请求头")
    parser.add_argument(
        "-q", "--query", action="append", default=[], help="查询参数（name=value）"
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="连接超时秒数")
    parser.add_argument(
        "--dry-run", action="store_true", help="只打印连接信息，不建立连接"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flowweave",
        description=(
            "FlowWeave 平台 API 命令行工具。当前平台无需登录。"
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    config = subcommands.add_parser("config", help="管理本地 CLI 配置")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    init = config_commands.add_parser("init", help="设置 FlowWeave 基础 URL")
    init.add_argument(
        "--base-url", required=True, help="平台根地址，例如 https://host/flowweave"
    )
    init.add_argument("--force", action="store_true", help="覆盖已有配置")
    config_commands.add_parser("show", help="显示当前 CLI 配置")

    health = subcommands.add_parser("health", help="检查 FlowWeave 健康状态")
    health.add_argument("--ready", action="store_true", help="检查依赖数据库的就绪状态")
    health.add_argument("--timeout", type=float, default=10.0, help="请求超时秒数")

    openapi = subcommands.add_parser("openapi", help="查看在线 OpenAPI 契约")
    openapi.add_argument("--paths", action="store_true", help="仅输出方法和路径")
    openapi.add_argument("--timeout", type=float, default=30.0, help="请求超时秒数")

    api = subcommands.add_parser("api", help="调用任意 FlowWeave REST 接口")
    api.add_argument("method", choices=("get", "post", "put", "patch", "delete"))
    api.add_argument("path", help="API 路径，例如 /flows 或 /flow-runs/<id>")
    api.add_argument(
        "--raw", action="store_true", help="不自动添加 /api/v1（用于 health 或 OpenAPI）"
    )
    add_request_arguments(api)

    upload = subcommands.add_parser(
        "upload", help="向任意 FlowWeave REST 接口发送 multipart 请求"
    )
    upload.add_argument("method", choices=("post", "put", "patch"))
    upload.add_argument("path", help="API 路径，例如 /agent-workspaces/<id>/attachments")
    upload.add_argument(
        "--form", action="append", default=[], help="Multipart 文本字段（name=value）"
    )
    upload.add_argument(
        "--file", action="append", default=[], help="Multipart 文件字段（name=path）"
    )
    upload.add_argument(
        "-H", "--header", action="append", default=[], help="HTTP 请求头（Name: value）"
    )
    upload.add_argument(
        "-q", "--query", action="append", default=[], help="查询参数（name=value）"
    )
    upload.add_argument("--timeout", type=float, default=30.0, help="请求超时秒数")
    upload.add_argument(
        "--dry-run", action="store_true", help="只打印请求，不发送"
    )

    websocket = subcommands.add_parser("ws", help="连接 FlowWeave WebSocket 接口")
    websocket.add_argument("path", help="API 路径，例如 /agent-workspaces/<id>/runtime/stream")
    message = websocket.add_mutually_exclusive_group()
    message.add_argument("--message", help="连接后立即发送的文本消息")
    message.add_argument("--message-json", help="连接后立即发送的 JSON 消息")
    websocket.add_argument(
        "--max-messages",
        type=int,
        default=0,
        help="收到指定条数后停止（0 表示持续到 Ctrl-C）",
    )
    add_transport_arguments(websocket)

    resource = subcommands.add_parser(
        "resource", help="常用平台资源的 CRUD 快捷路径"
    )
    resource.add_argument("kind", choices=sorted(RESOURCE_PATHS))
    resource.add_argument("action", choices=("list", "get", "create", "update", "delete"))
    resource.add_argument("identifier", nargs="?", help="get/update/delete 所需的资源标识")
    add_request_arguments(resource)
    return parser


def openapi_paths(document: dict[str, Any]) -> list[dict[str, str]]:
    raw_paths = document.get("paths")
    if not isinstance(raw_paths, dict):
        raise CliError("OpenAPI 文档不包含 paths 对象")
    paths = cast(dict[object, object], raw_paths)
    rows: list[dict[str, str]] = []
    allowed_methods = {"get", "post", "put", "patch", "delete"}
    for raw_path, operations in paths.items():
        if not isinstance(raw_path, str) or not isinstance(operations, dict):
            continue
        for raw_method in cast(dict[object, object], operations):
            if isinstance(raw_method, str) and raw_method.lower() in allowed_methods:
                rows.append({"method": raw_method.upper(), "path": raw_path})
    return sorted(rows, key=lambda row: (row["path"], row["method"]))


def resource_request(args: argparse.Namespace) -> tuple[str, str]:
    base = RESOURCE_PATHS[args.kind]
    requires_identifier = args.action in {"get", "update", "delete"}
    if requires_identifier and not args.identifier:
        raise CliError(f"resource {args.kind} {args.action} 必须提供资源标识")
    if args.action in {"list", "create"} and args.identifier:
        raise CliError(f"resource {args.kind} {args.action} 不接受资源标识")
    methods = {
        "list": "GET",
        "get": "GET",
        "create": "POST",
        "update": "PUT",
        "delete": "DELETE",
    }
    method = methods[args.action]
    return method, base if not args.identifier else f"{base}/{args.identifier}"


def run_websocket(config: Config, args: argparse.Namespace) -> int:
    if args.max_messages < 0:
        raise CliError("--max-messages 必须大于或等于零")
    message = args.message
    if args.message_json is not None:
        message = json.dumps(parse_json(args.message_json, "--message-json"), ensure_ascii=False)
    url = websocket_url(config.base_url, args.path, query=query_from_args(args.query))
    headers = headers_from_args(args.header)
    if args.dry_run:
        print_json({"url": url, "message": message, "max_messages": args.max_messages})
        return 0
    from websockets.exceptions import WebSocketException
    from websockets.sync.client import connect

    try:
        with connect(url, additional_headers=headers, open_timeout=args.timeout) as connection:
            if message is not None:
                connection.send(message)
            received = 0
            for event in connection:
                try:
                    print_json(json.loads(event))
                except json.JSONDecodeError:
                    print(event)
                received += 1
                if args.max_messages and received >= args.max_messages:
                    break
    except (OSError, WebSocketException) as exc:
        raise CliError(f"无法打开 FlowWeave WebSocket {url}：{exc}") from exc
    return 0


def run(args: argparse.Namespace) -> int:
    if args.command == "config":
        if args.config_command == "init":
            config = save_config(args.base_url, overwrite=args.force)
            print_json({"config_path": str(config_path()), "base_url": config.base_url})
        else:
            config = load_config()
            print_json({"config_path": str(config_path()), "base_url": config.base_url})
        return 0

    config = load_config()
    if args.command == "health":
        payload = request_api(
            config,
            "GET",
            "/health/ready" if args.ready else "/health",
            payload=None,
            headers={},
            raw=True,
            query=[],
            timeout=args.timeout,
            dry_run=False,
        )
    elif args.command == "openapi":
        payload = request_api(
            config,
            "GET",
            "/openapi.json",
            payload=None,
            headers={},
            raw=True,
            query=[],
            timeout=args.timeout,
            dry_run=False,
        )
        if args.paths:
            if not isinstance(payload, dict):
                raise CliError("OpenAPI 接口返回了不符合预期的响应")
            payload = openapi_paths(cast(dict[str, Any], payload))
    elif args.command == "api":
        payload = request_api(
            config,
            args.method.upper(),
            args.path,
            payload=data_from_args(args),
            headers=headers_from_args(args.header),
            raw=args.raw,
            query=query_from_args(args.query),
            timeout=args.timeout,
            dry_run=args.dry_run,
        )
    elif args.command == "upload":
        if not args.form and not args.file:
            raise CliError("upload 至少需要一个 --form 或 --file 参数")
        body, content_type, summary = multipart_body(args.form, args.file)
        payload = request_api(
            config,
            args.method.upper(),
            args.path,
            payload=None,
            headers=headers_from_args(args.header),
            raw=False,
            query=query_from_args(args.query),
            timeout=args.timeout,
            dry_run=args.dry_run,
            encoded_body=body,
            content_type=content_type,
        )
        if args.dry_run:
            payload["payload"] = summary
    elif args.command == "ws":
        return run_websocket(config, args)
    else:
        method, path = resource_request(args)
        payload = request_api(
            config,
            method,
            path,
            payload=data_from_args(args),
            headers=headers_from_args(args.header),
            raw=False,
            query=query_from_args(args.query),
            timeout=args.timeout,
            dry_run=args.dry_run,
        )
    print_json(payload)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        return run(parser.parse_args(argv))
    except CliError as exc:
        parser.exit(2, f"flowweave: error: {exc}\\n")


if __name__ == "__main__":
    main()
