from __future__ import annotations

import copy
import importlib.util
import io
import json
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY = Path(__file__).parents[3]
SOURCE_LOCK = REPOSITORY / "infra" / "openhands" / "source.lock.json"
FETCH_SOURCE = REPOSITORY / "infra" / "openhands" / "fetch_source.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("flowweave_openhands_fetch_source", FETCH_SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _lock() -> dict[str, object]:
    return json.loads(SOURCE_LOCK.read_text(encoding="utf-8"))


def test_source_lock_rejects_floating_archive(tmp_path: Path) -> None:
    module = _module()
    lock = copy.deepcopy(_lock())
    lock["archive_url"] = "https://github.com/OpenHands/software-agent-sdk/archive/main.tar.gz"
    lock_path = tmp_path / "source.lock.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    with pytest.raises(RuntimeError, match="floating OpenHands archive URL"):
        module._load_lock(lock_path)


def test_source_fetch_rejects_archive_digest_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()

    class Response(io.BytesIO):
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(b"not-the-locked-archive"),
    )
    destination = tmp_path / "source"

    with pytest.raises(RuntimeError, match="source archive digest mismatch"):
        module.fetch_source(
            SOURCE_LOCK,
            destination,
            tmp_path / "provenance.json",
            [],
        )

    assert not destination.exists()


def test_source_archive_rejects_path_traversal(tmp_path: Path) -> None:
    module = _module()
    archive_path = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive_path, mode="w:gz") as archive:
        payload = b"escape"
        member = tarfile.TarInfo("source/../../outside")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))

    with tarfile.open(archive_path, mode="r:gz") as archive:
        with pytest.raises(RuntimeError, match="unsafe archive member"):
            module._safe_members(archive)
