#!/usr/bin/env python3
"""Install commit-bound native systemd maintenance on the preflight target."""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

FILES = (
    "scripts/storage-maintenance.py",
    "scripts/remote-release.py",
    "deploy/systemd/flowweave-storage-maintenance.service",
    "deploy/systemd/flowweave-storage-maintenance.timer",
)
OWNED = "# Managed by FlowWeave storage maintenance installer; schema=1"


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def atomic(path, data, mode):
    temporary = path.with_name(path.name + ".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def install_locked(payload):
    if os.geteuid() != 0:
        raise ValueError("Installation requires server root")
    base = Path("/usr/local/lib/flowweave-storage-maintenance")
    settings = Path("/etc/flowweave-storage-maintenance.json")
    unit_root = Path("/etc/systemd/system")
    config = {"schema": 1, "root": payload["root"], "build": payload["build"]}
    config["images"] = payload["images"]
    config["prune_build_cache"] = payload["prune_build_cache"]
    if base.is_symlink() or settings.is_symlink():
        raise ValueError("Symlink installation target")
    if base.exists() and not (base / ".managed-schema-1").is_file():
        raise ValueError("Unknown installation directory")
    previous_settings = settings.read_bytes() if settings.exists() else None
    if previous_settings is not None:
        previous = json.loads(previous_settings)
        if any(previous.get(k) != config[k] for k in ("schema", "root", "build")):
            raise ValueError("Existing settings target differs; explicit migration required")
        if previous.get("images", config["images"]) != config["images"]:
            raise ValueError("Existing image directory differs")
        if set(previous) - set(config):
            raise ValueError("Unknown existing settings")
    units = [unit_root / Path(name).name for name in FILES if name.endswith((".service", ".timer"))]
    for path in units:
        if path.is_symlink() or path.exists() and not path.read_text().startswith(OWNED):
            raise ValueError("Unknown existing systemd unit")
    # Validate the unit syntax/calendar before changing an existing schedule.
    run(["systemd-analyze", "calendar", "*-*-* 03:30:00 Asia/Shanghai"])
    base.mkdir(mode=0o700, exist_ok=True)
    (base / ".managed-schema-1").touch(mode=0o600)
    version = base / payload["commit"]
    if version.is_symlink():
        raise ValueError("Symlink version directory")
    version.mkdir(mode=0o700, exist_ok=True)
    decoded = {name: base64.b64decode(data) for name, data in payload["files"].items()}
    for name, data in decoded.items():
        target = version / Path(name).name
        if target.exists():
            if target.is_symlink() or target.read_bytes() != data:
                raise ValueError("Immutable installed version differs")
        else:
            atomic(target, data, 0o600)
    run(
        [
            "systemd-analyze",
            "verify",
            str(version / units[0].name),
            str(version / units[1].name),
        ]
    )
    # Audit before enabling deletion. The runner shares the release lock.
    config_data = json.dumps(config, sort_keys=True).encode()
    if settings.exists() and settings.stat().st_mode & 0o077:
        raise ValueError("Existing settings have unsafe permissions")
    # Audit against the intended config without changing the live timer's settings.
    audit_code = (
        "import importlib.util,json; "
        "s=importlib.util.spec_from_file_location('maintenance',"
        + repr(str(version / "storage-maintenance.py"))
        + ");m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        + "print(json.dumps(m.maintain(json.loads("
        + repr(config_data.decode())
        + "))))"
    )
    report = run(
        [
            "/usr/bin/python3",
            "-B",
            "-c",
            audit_code,
        ]
    )
    rows = [json.loads(row) for row in report.splitlines()]
    print(json.dumps({"audit": rows[-1]}), flush=True)
    # Do not enable a job that could not finish its first protection inventory.
    if rows[-1]["status"] != "dry-run":
        raise ValueError("Audit was skipped; retry after active work")
    previous_link = os.readlink(base / "current") if (base / "current").is_symlink() else None
    if (base / "current").exists() and previous_link is None:
        raise ValueError("Unknown current entry")
    original = {p: p.read_bytes() if p.exists() else None for p in units}
    was_enabled = (
        subprocess.run(["systemctl", "is-enabled", units[1].name], capture_output=True).returncode
        == 0
    )
    was_active = (
        subprocess.run(["systemctl", "is-active", units[1].name], capture_output=True).returncode
        == 0
    )
    try:
        run(["systemctl", "stop", units[1].name]) if original[units[1]] else None
        service_active = (
            run(["systemctl", "show", units[0].name, "-p", "ActiveState"])
            if original[units[0]]
            else ""
        )
        if "ActiveState=active" in service_active or "ActiveState=activating" in service_active:
            raise ValueError("Existing maintenance service is still running")
        # Keep one rollback copy, replaced on updates, with restricted mode.
        for path, data in original.items():
            if data is not None:
                atomic(base / (path.name + ".previous"), data, 0o600)
        if previous_settings is not None:
            atomic(base / "settings.previous", previous_settings, 0o600)
        atomic(settings, config_data, 0o600)
        link = base / "current.new"
        link.symlink_to(version.name)
        link.replace(base / "current")
        for path in units:
            data = next(data for name, data in decoded.items() if Path(name).name == path.name)
            atomic(path, data, 0o644)
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "start", units[0].name])
        state = run(
            [
                "systemctl",
                "show",
                units[0].name,
                "-p",
                "Result",
                "-p",
                "ExecMainStatus",
            ]
        )
        if "Result=success" not in state or "ExecMainStatus=0" not in state:
            raise ValueError("First scheduled-service run failed")
        run(["systemctl", "enable", "--now", units[1].name])
        # Bound installer history as well; preserve current and one prior
        # immutable version. Only audited version directories are eligible.
        keep = {version.name, previous_link}
        for directory in base.iterdir():
            if directory.is_symlink() or not directory.is_dir() or directory.name in keep:
                continue
            if (
                len(directory.name) == 40
                and all(c in "0123456789abcdef" for c in directory.name)
                and {p.name for p in directory.iterdir()} == {Path(n).name for n in FILES}
            ):
                import shutil

                if shutil.rmtree.avoids_symlink_attacks:
                    shutil.rmtree(directory)
        print(
            json.dumps(
                {
                    "status": "installed",
                    "commit": payload["commit"],
                    "timer_enabled": True,
                    "first_service_run": "success",
                }
            ),
            flush=True,
        )
    except Exception:
        run(["systemctl", "disable", "--now", units[1].name])
        for path, data in original.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                atomic(path, data, 0o644)
        if previous_settings is None:
            settings.unlink(missing_ok=True)
        else:
            atomic(settings, previous_settings, 0o600)
        if previous_link is not None:
            link = base / "current.rollback"
            link.symlink_to(previous_link)
            link.replace(base / "current")
        else:
            (base / "current").unlink(missing_ok=True)
        run(["systemctl", "daemon-reload"])
        if was_enabled:
            run(["systemctl", "enable", units[1].name])
        if was_active:
            run(["systemctl", "start", units[1].name])
        raise


def install_server(payload):
    import fcntl

    descriptor = os.open(
        "/run/flowweave-maintenance-install.lock",
        os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        install_locked(payload)


def server_program(source):
    program = source.rsplit('if __name__ == "__main__":', 1)[0] + (
        "\ntry:\n install_server(json.load(sys.stdin))\nexcept Exception as error:\n"
        ' print(json.dumps({"status":"failed","error_type":type(error).__name__}), '
        "file=sys.stderr)\n"
        " sys.exit(1)\n"
    )
    compile(program, "<server-installer>", "exec")
    return program


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--prune-build-cache", action="store_true")
    args = parser.parse_args()
    commit = run(["git", "rev-parse", "--verify", args.commit + "^{commit}"]).strip()
    spec = importlib.util.spec_from_file_location(
        "release", Path(__file__).with_name("remote-release.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = module.load_config(args.config)
    run(
        [
            "scripts/verify-remote-deploy.sh",
            "--config",
            str(args.config),
            "--commit",
            commit,
            "--scope",
            "other",
        ]
    )
    payload = {
        "commit": commit,
        "root": config["FLOWWEAVE_REMOTE_ROOT"],
        "build": config["FLOWWEAVE_REMOTE_BUILD_ROOT"],
        "images": config["FLOWWEAVE_REMOTE_IMAGE_ROOT"],
        "prune_build_cache": args.prune_build_cache,
        "files": {
            name: base64.b64encode(
                subprocess.run(
                    ["git", "show", commit + ":" + name],
                    check=True,
                    capture_output=True,
                ).stdout
            ).decode()
            for name in FILES
        },
    }
    source = subprocess.run(
        ["git", "show", commit + ":scripts/install-storage-maintenance.py"],
        check=True,
        capture_output=True,
    ).stdout.decode()
    program = server_program(source)
    command = "python3 -c " + shlex.quote(
        "import base64;exec(base64.b64decode("
        + repr(base64.b64encode(program.encode()).decode())
        + "))"
    )
    result = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            config["FLOWWEAVE_REMOTE_USER"] + "@" + config["FLOWWEAVE_REMOTE_HOST"],
            command,
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    # Only emit structured statuses; even SSH failures may include private facts.
    for line in result.stdout.splitlines():
        try:
            print(json.dumps(json.loads(line)))
        except ValueError:
            pass
    if result.returncode:
        print("Installation failed; private diagnostic output withheld", file=sys.stderr)
        raise RuntimeError("Remote installation failed")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            json.dumps({"status": "failed", "error_type": type(error).__name__}),
            file=sys.stderr,
        )
        sys.exit(1)
