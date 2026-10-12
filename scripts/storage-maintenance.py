#!/usr/bin/env python3
"""Server-local, bounded release retention. Logs contain counts, never paths."""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import time

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
    return bool(tags) and all(
        tag.partition(":")[0] in REPOS and "rollback" in tag.partition(":")[2]
        for tag in tags
    )


def select_images(images, referenced, current, keep=3):
    protected = set(referenced) | set(current)
    for repo in REPOS:
        rollback = sorted(
            [
                i
                for i in images
                if any(
                    t.startswith(repo + ":") and "rollback" in t
                    for t in i.get("RepoTags") or []
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
        {
            c["Image"]
            for c in json.loads(release.run(["docker", "inspect", *containers]))
        }
        if containers
        else set()
    )
    current = {
        json.loads(release.run(["docker", "image", "inspect", repo + ":remote-amd64"]))[
            0
        ]["Id"]
        for repo in REPOS
    }
    ids = set()
    for repo in REPOS:
        ids.update(
            release.run(
                ["docker", "image", "ls", "--no-trunc", "--format", "{{.ID}}", repo]
            ).split()
        )
    images = (
        json.loads(release.run(["docker", "image", "inspect", *sorted(ids)]))
        if ids
        else []
    )
    return select_images(images, referenced, current)


def maintain(config, apply=False):
    root = Path(config["root"])
    build = release.checked_root(root, config["build"])
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
        protected, used = release.docker_protection(build)
        contexts = release.select_cleanup(
            build, protected, used, keep=3, now=time.time()
        )
        images = image_plan()
        if len(contexts) > 100 or len(images) > 100:
            raise ValueError("Bounded plan exceeded")
        identities = {p: p.stat() for p in contexts}
        result = {
            "status": "apply" if apply else "dry-run",
            "candidate_contexts": len(contexts),
            "candidate_images": len(images),
            "removed_contexts": 0,
            "removed_images": 0,
        }
        print(json.dumps(result), flush=True)
        if apply:
            for path in contexts:
                if active_mutation():
                    raise ValueError("New mutation activity")
                protected, used = release.docker_protection(build)
                latest = release.select_cleanup(
                    build, protected, used, keep=3, now=time.time()
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
                shutil.rmtree(path)
                result["removed_contexts"] += 1
            for image_id, tags in images.items():
                for index, tag in enumerate(tags):
                    if active_mutation() or image_plan().get(image_id) != tags[index:]:
                        raise ValueError("Image protection or tags changed")
                    # Inspect the exact tag, not just its old image, immediately
                    # before non-force removal. Docker protects container use.
                    if (
                        json.loads(release.run(["docker", "image", "inspect", tag]))[0][
                            "Id"
                        ]
                        != image_id
                    ):
                        raise ValueError("Tag was reassigned")
                    release.run(["docker", "image", "rm", tag])
                result["removed_images"] += 1
        result["free_bytes"] = shutil.disk_usage(build).free
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
