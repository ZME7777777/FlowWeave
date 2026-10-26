#!/usr/bin/env python3
"""Server-local, bounded release retention. Logs contain counts and hashes, never paths."""

from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import http.client
import importlib.util
import json
import os
import re
import shutil
import socket
import sys
import time
import urllib.parse
from pathlib import Path

sys.dont_write_bytecode = True

CONFIRM = "DELETE_GOVERNED_FLOWWEAVE_STORAGE"
REPOS = ("flowweave-platform", "flowweave-web")
SPEC = importlib.util.spec_from_file_location(
    "release", Path(__file__).with_name("remote-release.py")
)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def active_mutation(proc=Path("/proc")):
    for pid in proc.iterdir():
        if not pid.name.isdigit():
            continue
        try:
            args = (pid / "cmdline").read_bytes().decode(errors="replace").split("\0")
        except FileNotFoundError:
            continue
        # Permission or other I/O errors fail closed, rather than hiding activity.
        name = Path(args[0]).name if args else ""
        if name in ("docker", "buildctl", "buildctl-daemonless.sh") and any(
            arg in ("build", "bake") for arg in args[1:]
        ):
            return True
        if name in ("uv", "pip", "pip3", "npm", "pnpm", "yarn") and any(
            arg in ("install", "sync", "add", "ci") for arg in args[1:]
        ):
            return True
    return False


def governed_tags(tags):
    return bool(tags) and all(tag.partition(":")[0] in REPOS for tag in tags)


def select_images(images, referenced, current, keep=1):
    protected = set(referenced) | set(current)
    for repo in REPOS:
        rollback = sorted(
            [
                i
                for i in images
                if i["Id"] not in current
                and any(
                    t.startswith(repo + ":") and "rollback" in t for t in i.get("RepoTags") or []
                )
            ],
            key=lambda i: i["Created"],
            reverse=True,
        )
        protected.update(i["Id"] for i in rollback[:keep])
    return {
        i["Id"]: sorted(i["RepoTags"])
        for i in images
        if i["Id"] not in protected and governed_tags(i.get("RepoTags"))
    }


def image_plan():
    containers = release.run(["docker", "ps", "-aq"]).split()
    referenced = (
        {c["Image"] for c in json.loads(release.run(["docker", "inspect", *containers]))}
        if containers
        else set()
    )
    current = {
        json.loads(release.run(["docker", "image", "inspect", repo + ":remote-amd64"]))[0]["Id"]
        for repo in REPOS
    }
    ids = set()
    for repo in REPOS:
        ids.update(
            release.run(
                ["docker", "image", "ls", "--no-trunc", "--format", "{{.ID}}", repo]
            ).split()
        )
    images = json.loads(release.run(["docker", "image", "inspect", *sorted(ids)])) if ids else []
    return select_images(images, referenced, current)


class DockerConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/var/run/docker.sock")


def docker_request(method, path):
    connection = DockerConnection("localhost", timeout=600)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        data = json.loads(response.read())
        if response.status != 200:
            raise ValueError("Docker cache request failed")
        return data
    finally:
        connection.close()


def select_build_cache(rows, now):
    result = []
    for row in rows:
        if row.get("InUse") or row.get("Shared"):
            continue
        value = row.get("LastUsedAt") or row.get("CreatedAt")
        if not value:
            continue
        # Docker emits RFC3339 nanoseconds; Python 3.9 only accepts microseconds.
        value = re.sub(r"(\.\d{6})\d+", r"\1", value)
        touched = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if touched.tzinfo is None:
            raise ValueError("Docker cache timestamp lacks timezone")
        if now - touched.timestamp() >= release.RETENTION_SECONDS:
            result.append(row)
    return result


def cache_plan():
    return select_build_cache(
        docker_request("GET", "/system/df").get("BuildCache") or [], time.time()
    )


def prune_build_cache():
    # Engine-native dangling cache GC only. Never images, containers or volumes.
    filters = urllib.parse.urlencode({"filters": json.dumps({"until": ["24h"]})})
    return docker_request("POST", "/build/prune?" + filters)


def legacy_plan(build, images, used):
    roots = [build] + ([images] if images is not None and images != build else [])
    historical = build / "releases"
    if historical.is_dir() and not historical.is_symlink():
        roots.append(historical)
    return sorted(
        {
            p
            for root in roots
            for p in release.select_legacy_build_assets(
                root, used, now=time.time(), bare_commits=root == historical
            )
        }
    )


def deletion_record(kind, identity):
    print(json.dumps({"event": "removed", "kind": kind, "identity": identity}), flush=True)


def maintain(config, apply=False):
    root = Path(config["root"])
    build = release.checked_root(root, config["build"])
    image_files = release.checked_root(root, config["images"]) if config.get("images") else None
    descriptor = os.open(
        build / ".flowweave-release.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "skipped-lock"}
        if active_mutation():
            return {"status": "skipped-active-build-or-install"}
        _, used = release.docker_protection(build)
        contexts = release.select_cleanup(build, used, now=time.time())
        legacy = legacy_plan(build, image_files, used)
        images = image_plan()
        cache = cache_plan() if config.get("prune_build_cache") else []
        if len(contexts) + len(legacy) > 100 or len(images) > 100:
            raise ValueError("Bounded plan exceeded")
        identities = {p: p.stat() for p in [*contexts, *legacy]}
        free_before = shutil.disk_usage(build).free
        result = {
            "status": "apply" if apply else "dry-run",
            "candidate_contexts": len(contexts),
            "candidate_images": len(images),
            "candidate_legacy_assets": len(legacy),
            "candidate_build_cache": len(cache),
            "candidate_build_cache_bytes": sum(r.get("Size", 0) for r in cache),
            "removed_contexts": 0,
            "removed_images": 0,
            "removed_legacy_assets": 0,
            "removed_build_cache": 0,
            "build_cache_reclaimed_bytes": 0,
        }
        print(json.dumps(result), flush=True)
        if apply:
            for path in [*contexts, *legacy]:
                if active_mutation():
                    raise ValueError("New mutation activity")
                _, used = release.docker_protection(build)
                latest = (
                    release.select_cleanup(build, used, now=time.time())
                    if path in contexts
                    else legacy_plan(build, image_files, used)
                )
                st, before = path.stat(), identities[path]
                if path not in latest or (st.st_dev, st.st_ino, st.st_mtime_ns) != (
                    before.st_dev,
                    before.st_ino,
                    before.st_mtime_ns,
                ):
                    raise ValueError("Context identity or reference changed")
                if not shutil.rmtree.avoids_symlink_attacks:
                    raise ValueError("Safe directory removal unavailable")
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                key = "removed_contexts" if path in contexts else "removed_legacy_assets"
                result[key] += 1
                deletion_record(key, hashlib.sha256(path.name.encode()).hexdigest())
            for image_id, tags in images.items():
                for index, tag in enumerate(tags):
                    if active_mutation() or image_plan().get(image_id) != tags[index:]:
                        raise ValueError("Image protection or tags changed")
                    # Inspect the exact tag, not just its old image, immediately
                    # before non-force removal. Docker protects container use.
                    if (
                        json.loads(release.run(["docker", "image", "inspect", tag]))[0]["Id"]
                        != image_id
                    ):
                        raise ValueError("Tag was reassigned")
                    release.run(["docker", "image", "rm", tag])
                result["removed_images"] += 1
                deletion_record("image", image_id)
            if config.get("prune_build_cache") and cache:
                if active_mutation():
                    raise ValueError("New mutation activity before cache cleanup")
                report = prune_build_cache()
                result["removed_build_cache"] = len(report.get("CachesDeleted") or [])
                result["build_cache_reclaimed_bytes"] = report.get("SpaceReclaimed", 0)
        result["free_bytes"] = shutil.disk_usage(build).free
        result["free_bytes_change"] = result["free_bytes"] - free_before
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args()
    if args.apply and args.confirm != CONFIRM:
        parser.error("Explicit governed-storage confirmation is required")
    if args.config.is_symlink() or args.config.stat().st_mode & 0o077:
        raise ValueError("Config must be owner-only and regular")
    print(
        json.dumps(maintain(json.loads(args.config.read_text()), args.apply)),
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Subprocess exceptions/tracebacks can contain private host paths.
        print(
            json.dumps({"status": "failed", "error_type": type(error).__name__}),
            file=sys.stderr,
        )
        sys.exit(1)
