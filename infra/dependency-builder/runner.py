from __future__ import annotations

import base64
import io
import json
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, cast

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@/-]{0,127}$")
VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?$")
CLI_ALLOWLIST = {"lark-cli": "1.0.84", "uv": "0.7.8"}
MAX_BUNDLE_BYTES = 100 * 1024 * 1024


def checked_group(value: object, ecosystem: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{ecosystem} dependencies must be an object")
    result: dict[str, str] = {}
    for raw_name, raw_version in cast(dict[object, object], value).items():
        name, version = str(raw_name), str(raw_version)
        if not NAME.fullmatch(name) or ".." in name or name.startswith(("/", ".")):
            raise ValueError(f"invalid {ecosystem} dependency name")
        if not VERSION.fullmatch(version):
            raise ValueError(f"{ecosystem} dependency must use an exact version")
        result[name] = version
    return result


def run(command: list[str]) -> None:
    completed = subprocess.run(
        command,
        cwd="/work",
        env={
            "HOME": "/tmp",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "npm_config_cache": "/tmp/npm-cache",
        },
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout)[-2000:]
        raise RuntimeError(f"dependency resolver failed: {detail}")


def add_tree(archive: zipfile.ZipFile, root: Path) -> int:
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        total += path.stat().st_size
        if total > MAX_BUNDLE_BYTES:
            raise ValueError("dependency bundle exceeds 100 MiB")
        info = zipfile.ZipInfo(path.relative_to(root).as_posix())
        info.external_attr = (stat.S_IFREG | 0o444) << 16
        archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
    return total


def main() -> None:
    payload = json.load(sys.stdin)
    raw_dependencies = payload.get("dependencies")
    if payload.get("schema_version") != 1 or not isinstance(raw_dependencies, dict):
        raise ValueError("invalid dependency manifest")
    dependencies = cast(dict[str, object], raw_dependencies)
    if set(dependencies) - {"python", "node", "cli"}:
        raise ValueError("unsupported dependency ecosystem")
    python = checked_group(dependencies.get("python"), "python")
    node = checked_group(dependencies.get("node"), "node")
    cli = checked_group(dependencies.get("cli"), "cli")
    if any(CLI_ALLOWLIST.get(name) != version for name, version in cli.items()):
        raise ValueError("CLI dependency is not provided by the runtime allowlist")

    root = Path("/work/bundle")
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    if python:
        target = root / "python"
        target.mkdir()
        run([
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-input",
            "--no-cache-dir",
            "--only-binary=:all:",
            "--no-compile",
            "--target",
            str(target),
            *[f"{name}=={version}" for name, version in sorted(python.items())],
        ])
    if node:
        target = root / "node"
        target.mkdir()
        (target / "package.json").write_text(
            json.dumps({"private": True, "dependencies": node}, sort_keys=True),
            encoding="utf-8",
        )
        run([
            "npm",
            "install",
            "--ignore-scripts",
            "--omit=dev",
            "--package-lock=true",
            "--audit=false",
            "--fund=false",
            "--prefix",
            str(target),
        ])

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "python_version": "3.13",
        "node_version": "22",
        "dependencies": {"python": python, "node": node, "cli": cli},
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", allowZip64=False) as archive:
        expanded_bytes = add_tree(archive, root)
    json.dump(
        {
            "content_base64": base64.b64encode(output.getvalue()).decode("ascii"),
            "manifest": {**manifest, "expanded_bytes": expanded_bytes},
        },
        sys.stdout,
        separators=(",", ":"),
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
