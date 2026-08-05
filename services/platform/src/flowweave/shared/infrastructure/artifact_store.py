from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast
from uuid import uuid4

from flowweave.bootstrap.settings import Settings
from flowweave.shared.application.artifact_store import ArtifactStorePort


def _safe_key(value: str) -> str:
    normalized = value.replace("\\", "/")
    raw_parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or normalized.endswith("/")
        or any(part in {"", ".", ".."} for part in raw_parts)
    ):
        raise ValueError("Artifact storage key is unsafe")
    path = PurePosixPath(normalized)
    if path.is_absolute():
        raise ValueError("Artifact storage key is unsafe")
    return path.as_posix()


class LocalArtifactStore:
    """Filesystem object store with path confinement and atomic finalize."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        target = (self.root / _safe_key(key)).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("Artifact storage key escapes configured root")
        return target

    def put_temporary(self, namespace: str, content: bytes) -> str:
        key = f"_temporary/{_safe_key(namespace)}/{uuid4().hex}"
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return key

    def finalize(self, temporary_key: str, final_key: str) -> str:
        source = self._path(temporary_key)
        target = self._path(final_key)
        if not source.is_file():
            if target.is_file():
                return _safe_key(final_key)
            raise FileNotFoundError(temporary_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
        self._remove_empty_parents(source.parent)
        return _safe_key(final_key)

    def put(self, key: str, content: bytes) -> str:
        temporary = self.put_temporary("direct", content)
        try:
            return self.finalize(temporary, key)
        except Exception:
            self.delete(temporary)
            raise

    def read(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        target = self._path(key)
        target.unlink(missing_ok=True)
        self._remove_empty_parents(target.parent)

    def _remove_empty_parents(self, current: Path) -> None:
        while current != self.root and current.is_relative_to(self.root):
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent


class S3Body(Protocol):
    def read(self) -> bytes: ...


class S3Client(Protocol):
    def put_object(self, **kwargs: Any) -> object: ...

    def copy_object(self, **kwargs: Any) -> object: ...

    def delete_object(self, **kwargs: Any) -> object: ...

    def get_object(self, **kwargs: Any) -> dict[str, Any]: ...

    def head_object(self, **kwargs: Any) -> object: ...


class S3ArtifactStore:
    """S3 object store. Finalize copies first and only then removes the temporary key."""

    def __init__(self, client: S3Client, bucket: str, prefix: str = "") -> None:
        if not bucket:
            raise ValueError("S3 artifact bucket is required")
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def _key(self, key: str) -> str:
        safe = _safe_key(key)
        return f"{self.prefix}/{safe}" if self.prefix else safe

    def put_temporary(self, namespace: str, content: bytes) -> str:
        key = f"_temporary/{_safe_key(namespace)}/{uuid4().hex}"
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=content)
        return key

    def finalize(self, temporary_key: str, final_key: str) -> str:
        final = _safe_key(final_key)
        if not self.exists(temporary_key):
            if self.exists(final):
                return final
            raise FileNotFoundError(temporary_key)
        self.client.copy_object(
            Bucket=self.bucket,
            Key=self._key(final),
            CopySource={"Bucket": self.bucket, "Key": self._key(temporary_key)},
        )
        self.delete(temporary_key)
        return final

    def put(self, key: str, content: bytes) -> str:
        temporary = self.put_temporary("direct", content)
        try:
            return self.finalize(temporary, key)
        except Exception:
            self.delete(temporary)
            raise

    def read(self, key: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        body = cast(S3Body, response["Body"])
        return body.read()

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except Exception as exc:
            response = cast(dict[str, Any], getattr(exc, "response", {}))
            error = cast(dict[str, Any], response.get("Error", {}))
            if str(error.get("Code", "")) in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key(key))


def build_artifact_store(settings: Settings) -> ArtifactStorePort:
    if settings.artifact_backend == "local":
        return LocalArtifactStore(settings.artifact_root)
    if settings.artifact_backend != "s3":
        raise ValueError(f"Unsupported artifact backend: {settings.artifact_backend}")
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=settings.artifact_s3_endpoint_url or None,
        region_name=settings.artifact_s3_region,
        aws_access_key_id=settings.artifact_s3_access_key or None,
        aws_secret_access_key=settings.artifact_s3_secret_key or None,
    )
    return S3ArtifactStore(
        cast(S3Client, client),
        settings.artifact_s3_bucket,
        settings.artifact_s3_prefix,
    )
