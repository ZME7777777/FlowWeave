from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from flowweave.shared.application.artifact_store import ArtifactStorePort
from flowweave.shared.infrastructure.artifact_store import LocalArtifactStore, S3ArtifactStore


class MissingObject(Exception):
    def __init__(self) -> None:
        self.response = {"Error": {"Code": "NoSuchKey"}}
        super().__init__("NoSuchKey")


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, **kwargs: Any) -> object:
        self.objects[(str(kwargs["Bucket"]), str(kwargs["Key"]))] = bytes(kwargs["Body"])
        return {}

    def copy_object(self, **kwargs: Any) -> object:
        source = kwargs["CopySource"]
        assert isinstance(source, dict)
        source_key = (str(source["Bucket"]), str(source["Key"]))
        if source_key not in self.objects:
            raise MissingObject
        self.objects[(str(kwargs["Bucket"]), str(kwargs["Key"]))] = self.objects[source_key]
        return {}

    def delete_object(self, **kwargs: Any) -> object:
        self.objects.pop((str(kwargs["Bucket"]), str(kwargs["Key"])), None)
        return {}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        key = (str(kwargs["Bucket"]), str(kwargs["Key"]))
        if key not in self.objects:
            raise MissingObject
        return {"Body": io.BytesIO(self.objects[key])}

    def head_object(self, **kwargs: Any) -> object:
        if (str(kwargs["Bucket"]), str(kwargs["Key"])) not in self.objects:
            raise MissingObject
        return {}


@pytest.fixture(params=["local", "s3"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> ArtifactStorePort:
    if request.param == "local":
        return LocalArtifactStore(tmp_path / "objects")
    return S3ArtifactStore(FakeS3(), "flowweave-test", "contract-prefix")


def test_store_temporary_finalize_read_and_idempotency(store: ArtifactStorePort) -> None:
    temporary = store.put_temporary("imports/session", b"validated-content")
    assert temporary.startswith("_temporary/imports/session/")
    assert store.exists(temporary)
    assert store.read(temporary) == b"validated-content"

    final = store.finalize(temporary, "capabilities/ab/final-object")
    assert final == "capabilities/ab/final-object"
    assert not store.exists(temporary)
    assert store.read(final) == b"validated-content"

    # A retry after an uncertain response is safe once the final object exists.
    assert store.finalize(temporary, final) == final
    store.delete(final)
    store.delete(final)
    assert not store.exists(final)


def test_store_put_is_atomic_and_missing_finalize_fails(store: ArtifactStorePort) -> None:
    assert store.put("artifacts/run-1/version-1", b"artifact") == "artifacts/run-1/version-1"
    assert store.read("artifacts/run-1/version-1") == b"artifact"
    with pytest.raises(FileNotFoundError):
        store.finalize("_temporary/missing/object", "artifacts/missing")


@pytest.mark.parametrize(
    "key",
    ["", "../escape", "safe/../../escape", "/absolute", "safe/./object", r"safe\..\escape"],
)
def test_store_rejects_unsafe_keys(store: ArtifactStorePort, key: str) -> None:
    with pytest.raises(ValueError):
        store.put(key, b"unsafe")
