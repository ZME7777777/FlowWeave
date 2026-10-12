#!/usr/bin/env python3
"""Commit-bound release contexts and health-gated, bounded release retention.

Connection facts stay in the ignored local deployment config. This tool never
recreates a service, changes server Compose/env, or removes a data volume.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import time

CONFIRM = "DELETE_FLOWWEAVE_RELEASE_CONTEXTS"
MARKER = ".flowweave-release.json"
UNKNOWN_PROVENANCE = "!unprovenanced-image"
PLATFORM_FILES = (
    "services/platform/Dockerfile",
    "services/platform/pyproject.toml",
    "services/platform/uv.lock",
    "services/platform/alembic.ini",
    "services/platform/migrations",
    "services/platform/src",
    "infra/openhands/contract_check.py",
    "infra/openhands/openhands-source-provenance.json",
)
WEB_FILES = (
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "apps/web/Dockerfile",
    "apps/web/package.json",
    "apps/web/nginx.conf",
    "apps/web/src",
    "apps/web/index.html",
    "apps/web/vite.config.ts",
    "apps/web/tsconfig.json",
    "apps/web/tsconfig.app.json",
    "apps/web/tsconfig.node.json",
)


def run(command: list[str]) -> str:
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def source_paths(commit: str, scope: str) -> list[str]:
    paths = list(PLATFORM_FILES if scope == "platform" else WEB_FILES)
    paths.append(".dockerignore")
    tracked = set(run(["git", "ls-tree", "-r", "--name-only", commit]).splitlines())
    if scope == "web" and any(p.startswith("apps/web/public/") for p in tracked):
        paths.append("apps/web/public")
    for path in paths:
        if path not in tracked and not any(p.startswith(path + "/") for p in tracked):
            raise ValueError("Commit lacks a required build input")
    return paths


def release_id(commit: str, scope: str) -> str:
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise ValueError("Release requires a full commit SHA")
    if scope not in ("web", "platform"):
        raise ValueError("Only web and platform release contexts are supported")
    return f"release-{commit}-{scope}"


def read_marker(directory: Path) -> dict | None:
    marker = directory / MARKER
    if directory.is_symlink() or not directory.is_dir() or marker.is_symlink():
        return None
    try:
        data = json.loads(marker.read_text())
        if not isinstance(data, dict):
            return None
        if (
            data.get("schema") != 1
            or release_id(data["commit"], data["scope"]) != directory.name
        ):
            return None
        if data.get("state") not in ("building", "succeeded", "failed"):
            return None
        if not isinstance(data.get("created_at"), (int, float)) or not math.isfinite(
            data["created_at"]
        ):
            return None
        if data["state"] == "succeeded" and (
            not isinstance(data.get("finished_at"), (int, float))
            or not math.isfinite(data["finished_at"])
        ):
            return None
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_marker(directory: Path, data: dict) -> None:
    temporary = directory / (MARKER + ".new")
    with temporary.open("x") as stream:
        json.dump(data, stream, sort_keys=True)
    os.chmod(temporary, 0o600)
    temporary.replace(directory / MARKER)


def select_cleanup(
    build: Path, protected: set[str], used_paths: list[Path], *, keep: int, now: float
) -> list[Path]:
    """Unknown/legacy assets and active references are always protected."""
    if keep < 1:
        raise ValueError("At least one rollback context must be retained")
    rows = [(p, read_marker(p)) for p in build.iterdir()]
    rows = [(p, m) for p, m in rows if m is not None]
    retained = set()
    for scope in ("platform", "web"):
        successful = sorted(
            [
                (p, m)
                for p, m in rows
                if m["scope"] == scope and m["state"] == "succeeded"
            ],
            key=lambda row: row[1].get("finished_at", row[1]["created_at"]),
            reverse=True,
        )
        retained.update(p for p, _ in successful[:keep])
    result = []
    for path, marker in rows:
        if path in retained or marker["commit"] in protected:
            continue
        if UNKNOWN_PROVENANCE in protected and marker["state"] != "succeeded":
            continue
        if any(
            path == used or path in used.parents or used in path.parents
            for used in used_paths
        ):
            continue
        if marker["state"] != "succeeded" and now - marker["created_at"] < 7 * 86400:
            continue
        result.append(path)
    return sorted(result)


def docker_protection() -> tuple[set[str], list[Path]]:
    ids = run(["docker", "ps", "-aq"]).split()
    containers = json.loads(run(["docker", "inspect", *ids])) if ids else []
    images = {c["Image"] for c in containers}
    for repo in ("flowweave-platform", "flowweave-web"):
        current = json.loads(
            run(["docker", "image", "inspect", repo + ":remote-amd64"])
        )[0]
        images.add(current["Id"])
        ids = sorted(
            set(
                run(
                    ["docker", "image", "ls", "--no-trunc", "--format", "{{.ID}}", repo]
                ).split()
            )
        )
        values = json.loads(run(["docker", "image", "inspect", *ids])) if ids else []
        rollback = sorted(
            [
                i
                for i in values
                if any("rollback" in tag for tag in i.get("RepoTags", []))
            ],
            key=lambda i: i["Created"],
            reverse=True,
        )[:3]
        images.update(i["Id"] for i in rollback)
    values = json.loads(run(["docker", "image", "inspect", *sorted(images)]))
    protected = {
        (i.get("Config", {}).get("Labels") or {}).get(
            "org.opencontainers.image.revision", ""
        )
        for i in values
    }
    protected.discard("")
    # Legacy images may lack revision labels. They cannot be linked safely to
    # incomplete contexts, which therefore remain protected. Successful
    # governed contexts passed finish's immutable image/revision health gate;
    # their images necessarily have the revision label and are protected above.
    if any(
        not (i.get("Config", {}).get("Labels") or {}).get(
            "org.opencontainers.image.revision"
        )
        for i in values
        if any(
            t.startswith(("flowweave-platform:", "flowweave-web:"))
            for t in i.get("RepoTags", [])
        )
    ):
        protected.add(UNKNOWN_PROVENANCE)
    paths = [
        Path(m["Source"])
        for c in containers
        for m in c.get("Mounts", [])
        if m.get("Source")
    ]
    for pid in Path("/proc").iterdir():
        if not pid.name.isdigit():
            continue
        try:
            for link in [pid / "cwd", *(pid / "fd").iterdir()]:
                try:
                    value = os.readlink(link)
                    if value.startswith("/"):
                        paths.append(Path(value))
                except OSError:
                    pass
        except OSError:
            pass
    return protected, paths


def checked_root(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("Invalid configured relative path")
    candidate = root / relative
    if root.is_symlink() or candidate.resolve() != candidate or not candidate.is_dir():
        raise ValueError(
            "Deployment directory must be a real directory without symlink parents"
        )
    return candidate


def check_release_services(config: dict, commit: str, scope: str) -> None:
    services = ("web",) if scope == "web" else ("api", "stream-api", "worker")
    root = Path(config["FLOWWEAVE_REMOTE_ROOT"])
    for service in services:
        prefix = (
            "FLOWWEAVE_REMOTE_STREAM"
            if service == "stream-api"
            and config.get("FLOWWEAVE_REMOTE_STREAM_COMPOSE_FILE")
            else "FLOWWEAVE_REMOTE"
        )
        compose = [
            "docker",
            "compose",
            "--env-file",
            str(root / config[prefix + "_ENV_FILE"]),
            "-f",
            str(root / config[prefix + "_COMPOSE_FILE"]),
        ]
        ids = run([*compose, "ps", "-q", service]).split()
        if not ids:
            raise ValueError("Release service is absent")
        for container in json.loads(run(["docker", "inspect", *ids])):
            state = container["State"]
            if (
                not state["Running"]
                or state.get("Health", {}).get("Status", "healthy") != "healthy"
            ):
                raise ValueError("Release service is not healthy")
            image = json.loads(run(["docker", "image", "inspect", container["Image"]]))[
                0
            ]
            if (image.get("Config", {}).get("Labels") or {}).get(
                "org.opencontainers.image.revision"
            ) != commit:
                raise ValueError(
                    "Release services do not run the same requested commit"
                )
            if image["Architecture"] != "amd64" or image["Os"] != "linux":
                raise ValueError("Release image platform is not linux/amd64")
            if service in ("api", "stream-api"):
                for route in ("/health", "/health/ready"):
                    run(
                        [
                            "docker",
                            "exec",
                            container["Id"],
                            "curl",
                            "-fsS",
                            "http://127.0.0.1:8080" + route,
                        ]
                    )
            elif service == "web":
                run(
                    [
                        "docker",
                        "exec",
                        container["Id"],
                        "wget",
                        "-q",
                        "-O",
                        "/dev/null",
                        "http://127.0.0.1/",
                    ]
                )


def remote_main(payload: dict) -> None:
    config = payload["config"]
    root = Path(config["FLOWWEAVE_REMOTE_ROOT"])
    build = checked_root(root, config["FLOWWEAVE_REMOTE_BUILD_ROOT"])
    directory = build / release_id(payload["commit"], payload["scope"])
    if payload["action"] == "audit":
        protected, used = docker_protection()
        candidates = select_cleanup(build, protected, used, keep=3, now=time.time())
        print(
            json.dumps(
                {
                    "candidate_release_ids": [p.name for p in candidates],
                    "keep_per_scope": 3,
                }
            )
        )
        return
    # Each operation shares one lock. In-progress contexts are also protected
    # by their marker age and live mount/process references between operations.
    lock_path = build / ".flowweave-release.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        action = payload["action"]
        if action == "prepare":
            if directory.exists() or directory.is_symlink():
                raise ValueError("Release context already exists; never overwrite it")
            directory.mkdir(mode=0o700)
            archive = sys.stdin.buffer.read()
            data = {
                "schema": 1,
                "commit": payload["commit"],
                "scope": payload["scope"],
                "state": "building",
                "created_at": time.time(),
                "archive_sha256": hashlib.sha256(archive).hexdigest(),
            }
            try:
                with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as stream:
                    # Git archive is the only input. Reject links/devices and
                    # traversal explicitly, including on older host Python.
                    for entry in stream.getmembers():
                        if (
                            entry.name.startswith("/")
                            or ".." in Path(entry.name).parts
                            or not (entry.isfile() or entry.isdir())
                        ):
                            raise ValueError(
                                "Archive contains a non-regular build input"
                            )
                    if hasattr(tarfile, "data_filter"):
                        stream.extractall(directory, filter="data")
                    else:
                        # Older host Python has no extraction filter. The
                        # complete archive above is restricted to regular
                        # files/directories, with absolute/traversal/link
                        # entries rejected before this private directory write.
                        stream.extractall(directory)
                write_marker(directory, data)
            except Exception:
                data["state"] = "failed"
                write_marker(directory, data)
                raise
            print(
                json.dumps(
                    {
                        "release_id": directory.name,
                        "archive_sha256": data["archive_sha256"],
                        "archive_bytes": len(archive),
                    }
                )
            )
            return
        if action == "finish":
            check_release_services(config, payload["commit"], payload["scope"])
            data = read_marker(directory)
            if data is None:
                raise ValueError("Release context is not governed by this tool")
            data.update(state="succeeded", finished_at=time.time())
            write_marker(directory, data)
        protected, used = docker_protection()
        candidates = select_cleanup(build, protected, used, keep=3, now=time.time())
        reviewed = {p: p.stat() for p in candidates}
        print(
            json.dumps(
                {
                    "candidate_release_ids": [p.name for p in candidates],
                    "keep_per_scope": 3,
                }
            ),
            flush=True,
        )
        if payload["apply"]:
            if len(candidates) > 100:
                raise ValueError("Deletion plan exceeds 100 release contexts")
            # Re-read every reference before each destructive operation.
            for path in candidates:
                protected, used = docker_protection()
                current = select_cleanup(
                    build, protected, used, keep=3, now=time.time()
                )
                st = path.stat()
                before = reviewed[path]
                if path not in current or (st.st_dev, st.st_ino, st.st_mtime_ns) != (
                    before.st_dev,
                    before.st_ino,
                    before.st_mtime_ns,
                ):
                    raise ValueError("Release context changed after dry-run")
                if not shutil.rmtree.avoids_symlink_attacks:
                    raise ValueError("Host lacks safe directory removal support")
                shutil.rmtree(path)
                print(json.dumps({"removed_release_id": path.name}), flush=True)


def load_config(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("Private config must be a regular owner-only file")
    sidecar = path.with_suffix(".entrypoints.env")
    if sidecar.exists() and (sidecar.is_symlink() or sidecar.stat().st_mode & 0o077):
        raise ValueError("Private sidecar must be a regular owner-only file")
    program = (
        'set -a; source "$1"; if [[ -f "$2" ]]; then source "$2"; fi; python3 -c '
        "'import os,json; print(json.dumps({k:v for k,v in os.environ.items() if k.startswith(\"FLOWWEAVE_REMOTE_\")}))'"
    )
    return json.loads(
        run(["bash", "-c", program, "release-config", str(path), str(sidecar)])
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--scope", choices=("platform", "web"), required=True)
    parser.add_argument(
        "--action", choices=("prepare", "finish", "audit", "cleanup"), default="audit"
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if args.apply and args.confirm != CONFIRM:
        parser.error("--apply requires --confirm " + CONFIRM)
    if args.apply and args.action not in ("finish", "cleanup"):
        parser.error("--apply is only valid with finish or cleanup")
    if args.action == "cleanup" and not args.apply:
        parser.error("Use audit for a read-only plan, or cleanup --apply --confirm")
    commit = run(["git", "rev-parse", "--verify", args.commit + "^{commit}"]).strip()
    release_id(commit, args.scope)
    config = load_config(args.config)
    # Preflight internally restates/verifies the private target; keep its raw
    # output in memory, never put connection details in chat or build logs.
    run(
        [
            "scripts/verify-remote-deploy.sh",
            "--config",
            str(args.config),
            "--commit",
            commit,
            "--scope",
            args.scope,
        ]
    )
    print("remote_preflight=READY", flush=True)
    payload = {
        "config": config,
        "commit": commit,
        "scope": args.scope,
        "action": args.action,
        "apply": args.apply,
    }
    encoded_source = base64.b64encode(Path(__file__).read_bytes()).decode()
    encoded_payload = base64.b64encode(json.dumps(payload).encode()).decode()
    code = (
        "import base64,json; namespace={'__name__':'flowweave_release_remote'}; "
        "exec(compile(base64.b64decode("
        + repr(encoded_source)
        + "),'<release-tool>','exec'),namespace); "
        "namespace['remote_main'](json.loads(base64.b64decode("
        + repr(encoded_payload)
        + ")))"
    )
    target = config["FLOWWEAVE_REMOTE_USER"] + "@" + config["FLOWWEAVE_REMOTE_HOST"]
    ssh = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        target,
        "python3 -c " + shlex.quote(code),
    ]
    archive = None
    if args.action == "prepare":
        archive = subprocess.Popen(
            [
                "git",
                "archive",
                "--format=tar.gz",
                commit,
                "--",
                *source_paths(commit, args.scope),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    try:
        result = subprocess.run(
            ssh,
            stdin=archive.stdout if archive else subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
        if archive and archive.stdout:
            archive.stdout.close()
        if archive and archive.wait() != 0:
            raise ValueError("Commit-bound archive creation failed")
        if result.returncode:
            raise ValueError(
                "Remote release operation failed; private diagnostics withheld"
            )
        print(result.stdout, end="")
    finally:
        if archive and archive.poll() is None:
            archive.terminate()
            archive.wait()
    if args.action == "finish" and args.apply:
        # The existing image tool audits and protects all container references,
        # current tags and the newest three rollback images. No builder prune.
        tool = [
            "scripts/maintain-remote-docker-retention.sh",
            "--config",
            str(args.config),
        ]
        audit = run(tool)
        for line in audit.splitlines():
            if line.startswith(("candidate_image_ids=", "CANDIDATE ")):
                print(line)
        applied = run(
            [*tool, "--apply", "--confirm", "DELETE_UNREFERENCED_FLOWWEAVE_IMAGES"]
        )
        for line in applied.splitlines():
            if line.startswith(("REMOVED ", "candidate_image_ids=")):
                print(line)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError):
        print(
            "Release operation failed. Connection/configuration details are withheld.",
            file=sys.stderr,
        )
        sys.exit(1)
