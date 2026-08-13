"""Fetch and verify the immutable OpenHands source input for the runtime image."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

REQUIRED_PACKAGES = (
    "openhands-agent-server",
    "openhands-sdk",
    "openhands-tools",
    "openhands-workspace",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_lock(path: Path) -> dict[str, Any]:
    lock = json.loads(path.read_text(encoding="utf-8"))
    if lock.get("schema_version") != 1:
        raise RuntimeError("unsupported OpenHands source lock schema")
    for field in (
        "source_kind",
        "repository",
        "archive_url",
        "archive_sha256",
        "upstream_base_commit",
        "source_commit",
        "package_version",
    ):
        if not isinstance(lock.get(field), str) or not lock[field]:
            raise RuntimeError(f"missing source lock field: {field}")
    archive_digest = lock["archive_sha256"]
    if len(archive_digest) != 64 or any(
        character not in "0123456789abcdef" for character in archive_digest
    ):
        raise RuntimeError("invalid immutable source identity: archive_sha256")
    for field in ("upstream_base_commit", "source_commit"):
        commit = lock[field]
        if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
            raise RuntimeError(f"invalid immutable source identity: {field}")
    if lock["archive_url"].split("?", 1)[0].endswith(("/main.tar.gz", "/master.tar.gz")):
        raise RuntimeError("floating OpenHands archive URL is forbidden")
    if lock.get("packages") != list(REQUIRED_PACKAGES):
        raise RuntimeError("source lock must enumerate the four governed packages")
    fork_commit = lock.get("fork_commit")
    if fork_commit is not None and (
        not isinstance(fork_commit, str)
        or len(fork_commit) != 40
        or any(character not in "0123456789abcdef" for character in fork_commit)
    ):
        raise RuntimeError("invalid fork commit")
    if lock["source_kind"] == "flowweave_fork" and fork_commit != lock["source_commit"]:
        raise RuntimeError("a FlowWeave fork lock must identify its exact fork commit")
    if lock["source_kind"] != "flowweave_fork" and fork_commit is not None:
        raise RuntimeError("non-fork bootstrap input cannot claim a fork commit")
    return lock


def _safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    roots: set[str] = set()
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise RuntimeError(f"unsafe archive member: {member.name}")
        if member.issym() or member.islnk() or member.isdev():
            raise RuntimeError(f"unsupported archive member type: {member.name}")
        roots.add(path.parts[0])
    if len(roots) != 1:
        raise RuntimeError("OpenHands archive must have exactly one root directory")
    return members


def fetch_source(
    lock_path: Path, destination: Path, provenance_path: Path, overlays: list[Path]
) -> None:
    lock = _load_lock(lock_path)
    if destination.exists():
        raise RuntimeError(f"source destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="flowweave-openhands-source-") as temporary:
        archive_path = Path(temporary) / "source.tar.gz"
        with urllib.request.urlopen(lock["archive_url"], timeout=120) as response:  # noqa: S310
            with archive_path.open("wb") as stream:
                shutil.copyfileobj(response, stream)
        actual_digest = _sha256(archive_path)
        if actual_digest != lock["archive_sha256"]:
            raise RuntimeError(
                "OpenHands source archive digest mismatch: "
                f"expected {lock['archive_sha256']}, got {actual_digest}"
            )
        extract_root = Path(temporary) / "extract"
        extract_root.mkdir()
        with tarfile.open(archive_path, mode="r:gz") as archive:
            members = _safe_members(archive)
            archive.extractall(extract_root, members=members, filter="data")
        roots = list(extract_root.iterdir())
        source_root = roots[0]
        for package in REQUIRED_PACKAGES:
            pyproject = source_root / package / "pyproject.toml"
            if not pyproject.is_file():
                raise RuntimeError(f"source archive is missing {package}/pyproject.toml")
            project = pyproject.read_text(encoding="utf-8")
            if f'version = "{lock["package_version"]}"' not in project:
                raise RuntimeError(f"unexpected package version in {package}")
        shutil.move(str(source_root), destination)

    overlay_digests = {}
    for overlay in overlays:
        if not overlay.is_file():
            raise RuntimeError(f"missing governed source overlay: {overlay}")
        overlay_digests[overlay.name] = _sha256(overlay)
    provenance = {
        "schema_version": 1,
        "build_input": lock,
        "source_archive_sha256": lock["archive_sha256"],
        "source_root": str(destination),
        "overlays": overlay_digests,
    }
    provenance_path.write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--overlay", action="append", type=Path, default=[])
    args = parser.parse_args()
    fetch_source(args.lock, args.destination, args.provenance, args.overlay)


if __name__ == "__main__":
    main()
