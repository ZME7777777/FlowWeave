from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class LarkResource:
    token: str
    url: str
    object_token: str | None = None


class LarkDrivePort(Protocol):
    def create_wiki_node(
        self, *, access_token: str, parent_url: str, name: str
    ) -> LarkResource: ...

    def delete_wiki_node(self, *, access_token: str, node_url: str) -> None: ...

    def create_folder(
        self, *, access_token: str, parent_token: str, parent_url: str, name: str
    ) -> LarkResource: ...

    def copy_docx(
        self, *, access_token: str, source_token: str, folder_token: str, name: str
    ) -> LarkResource: ...

    def create_docx(
        self, *, access_token: str, folder_token: str, folder_url: str, name: str
    ) -> LarkResource: ...

    def delete(self, *, access_token: str, token: str, resource_type: str) -> None: ...
