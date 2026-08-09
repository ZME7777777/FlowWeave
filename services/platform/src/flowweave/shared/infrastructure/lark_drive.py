from __future__ import annotations

from typing import cast
from urllib.parse import quote, urlparse
from uuid import uuid4

import httpx

from flowweave.bootstrap.settings import Settings
from flowweave.shared.application.lark_drive import LarkDrivePort, LarkResource
from flowweave.shared.errors import DomainError


class MockLarkDrive:
    def create_wiki_node(self, *, access_token: str, parent_url: str, name: str) -> LarkResource:
        node_token = f"mock-wiki-{uuid4()}"
        object_token = f"mock-docx-{uuid4()}"
        host = urlparse(parent_url).netloc or "example.feishu.cn"
        return LarkResource(node_token, f"https://{host}/wiki/{node_token}", object_token)

    def delete_wiki_node(self, *, access_token: str, node_url: str) -> None:
        return None

    def create_folder(
        self, *, access_token: str, parent_token: str, parent_url: str, name: str
    ) -> LarkResource:
        token = f"mock-folder-{uuid4()}"
        host = urlparse(parent_url).netloc or "example.feishu.cn"
        return LarkResource(token, f"https://{host}/drive/folder/{token}")

    def copy_docx(
        self, *, access_token: str, source_token: str, folder_token: str, name: str
    ) -> LarkResource:
        token = f"mock-docx-{uuid4()}"
        return LarkResource(token, f"https://example.feishu.cn/docx/{token}")

    def create_docx(
        self, *, access_token: str, folder_token: str, folder_url: str, name: str
    ) -> LarkResource:
        token = f"mock-docx-{uuid4()}"
        host = urlparse(folder_url).netloc or "example.feishu.cn"
        return LarkResource(token, f"https://{host}/docx/{token}")

    def delete(self, *, access_token: str, token: str, resource_type: str) -> None:
        return None


class HttpLarkDrive:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.lark_api_base_url.rstrip("/")

    def _request(
        self, method: str, path: str, access_token: str, *, json: dict[str, str] | None = None
    ) -> dict[str, object]:
        try:
            response = httpx.request(
                method,
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {access_token}"},
                json=json,
                timeout=30,
            )
            response.raise_for_status()
            raw_payload: object = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise DomainError(
                "LARK_DRIVE_UNAVAILABLE",
                "Lark Drive rejected the document operation",
                502,
            ) from exc
        payload = cast(dict[str, object], raw_payload) if isinstance(raw_payload, dict) else {}
        raw_code = payload.get("code", 0)
        code = raw_code if isinstance(raw_code, int) else -1
        if code != 0:
            raise DomainError(
                "LARK_DRIVE_REJECTED",
                str(payload.get("msg") or "Lark Drive rejected the document operation"),
                422,
            )
        data = payload.get("data")
        return cast(dict[str, object], data) if isinstance(data, dict) else {}

    @staticmethod
    def _wiki_token(url: str) -> str:
        path = urlparse(url).path
        if "/wiki/" not in path:
            raise DomainError("LARK_WIKI_URL_INVALID", "Lark Wiki node URL is invalid", 422)
        token = path.split("/wiki/", 1)[1].split("/", 1)[0]
        if not token:
            raise DomainError("LARK_WIKI_URL_INVALID", "Lark Wiki node token is missing", 422)
        return token

    def _wiki_node(self, *, access_token: str, node_token: str) -> dict[str, object]:
        data = self._request(
            "GET",
            f"/open-apis/wiki/v2/spaces/get_node?token={quote(node_token)}",
            access_token,
        )
        raw_node = data.get("node")
        return cast(dict[str, object], raw_node) if isinstance(raw_node, dict) else data

    def create_wiki_node(self, *, access_token: str, parent_url: str, name: str) -> LarkResource:
        parent_token = self._wiki_token(parent_url)
        parent = self._wiki_node(access_token=access_token, node_token=parent_token)
        space_id = str(parent.get("space_id") or "")
        if not space_id:
            raise DomainError("LARK_PROTOCOL_ERROR", "Lark Wiki space id is missing", 502)
        data = self._request(
            "POST",
            f"/open-apis/wiki/v2/spaces/{quote(space_id)}/nodes",
            access_token,
            json={
                "obj_type": "docx",
                "parent_node_token": parent_token,
                "node_type": "origin",
                "title": name,
            },
        )
        raw_node = data.get("node")
        node = cast(dict[str, object], raw_node) if isinstance(raw_node, dict) else data
        node_token = str(node.get("node_token") or "")
        object_token = str(node.get("obj_token") or "")
        if not node_token or not object_token:
            raise DomainError("LARK_PROTOCOL_ERROR", "Created Lark Wiki node is incomplete", 502)
        host = urlparse(parent_url).netloc or "open.feishu.cn"
        return LarkResource(node_token, f"https://{host}/wiki/{node_token}", object_token)

    def delete_wiki_node(self, *, access_token: str, node_url: str) -> None:
        node_token = self._wiki_token(node_url)
        node = self._wiki_node(access_token=access_token, node_token=node_token)
        space_id = str(node.get("space_id") or "")
        if not space_id:
            raise DomainError("LARK_PROTOCOL_ERROR", "Lark Wiki space id is missing", 502)
        self._request(
            "DELETE",
            f"/open-apis/wiki/v2/spaces/{quote(space_id)}/nodes/{quote(node_token)}",
            access_token,
        )

    def create_folder(
        self, *, access_token: str, parent_token: str, parent_url: str, name: str
    ) -> LarkResource:
        data = self._request(
            "POST",
            "/open-apis/drive/v1/files/create_folder",
            access_token,
            json={"name": name, "folder_token": parent_token},
        )
        token = str(data.get("token") or "")
        if not token:
            raise DomainError("LARK_PROTOCOL_ERROR", "Lark folder token is missing", 502)
        host = urlparse(parent_url).netloc
        return LarkResource(token, f"https://{host}/drive/folder/{token}")

    def copy_docx(
        self, *, access_token: str, source_token: str, folder_token: str, name: str
    ) -> LarkResource:
        data = self._request(
            "POST",
            f"/open-apis/drive/v1/files/{source_token}/copy",
            access_token,
            json={"name": name, "type": "docx", "folder_token": folder_token},
        )
        raw_file = data.get("file")
        file_data = cast(dict[str, object], raw_file) if isinstance(raw_file, dict) else data
        token = str(file_data.get("token") or "")
        url = str(file_data.get("url") or "")
        if not token:
            raise DomainError("LARK_PROTOCOL_ERROR", "Copied document token is missing", 502)
        return LarkResource(token, url or f"https://open.feishu.cn/docx/{token}")

    def create_docx(
        self, *, access_token: str, folder_token: str, folder_url: str, name: str
    ) -> LarkResource:
        data = self._request(
            "POST",
            "/open-apis/docx/v1/documents",
            access_token,
            json={"title": name, "folder_token": folder_token},
        )
        raw_document = data.get("document")
        document = cast(dict[str, object], raw_document) if isinstance(raw_document, dict) else data
        token = str(document.get("document_id") or document.get("token") or "")
        if not token:
            raise DomainError("LARK_PROTOCOL_ERROR", "Created document token is missing", 502)
        host = urlparse(folder_url).netloc or "open.feishu.cn"
        return LarkResource(token, f"https://{host}/docx/{token}")

    def delete(self, *, access_token: str, token: str, resource_type: str) -> None:
        self._request(
            "DELETE",
            f"/open-apis/drive/v1/files/{token}?type={resource_type}",
            access_token,
        )


def build_lark_drive(settings: Settings) -> LarkDrivePort:
    return MockLarkDrive() if settings.runtime_adapter == "mock" else HttpLarkDrive(settings)
