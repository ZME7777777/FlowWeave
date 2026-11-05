"""Fixed Git operations executed through OpenHands' native Bash API."""

WORKSPACE_GIT_SCRIPT = r'''
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

request = json.loads(sys.argv[1])
def fail(code):
    print(json.dumps({"error": code}))
    sys.exit(0)

root = Path(request["root"]).resolve(strict=True)
repository = Path(request["repository"])
if repository.is_symlink() or repository.resolve(strict=True) != repository or not repository.is_relative_to(root):
    fail("PATH_INVALID")
environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
def git(*arguments):
    return subprocess.run(
        ["git", "-C", str(repository), "-c", "core.hooksPath=/dev/null",
         "-c", "submodule.recurse=false", *arguments],
        capture_output=True, text=True, timeout=10, env=environment,
    )
def value(*arguments):
    result = git(*arguments)
    return result.stdout.strip() if result.returncode == 0 else ""

try:
    if value("rev-parse", "--show-toplevel") != str(repository):
        fail("PATH_INVALID")
    action = request["action"]
    if action == "sync":
        branch = value("symbolic-ref", "--quiet", "--short", "HEAD")
        remote = value("config", "branch." + branch + ".remote") or "origin"
        if remote == "." or not value("remote", "get-url", remote):
            fail("REMOTE_UNAVAILABLE")
        arguments = ["fetch", "--no-tags", "--no-recurse-submodules", "--", remote]
        merge = value("config", "branch." + branch + ".merge")
        upstream = value("rev-parse", "--symbolic-full-name", "@{upstream}")
        if merge.startswith("refs/heads/") and upstream.startswith("refs/remotes/"):
            arguments.append("+" + merge + ":" + upstream)
        if git(*arguments).returncode:
            fail("REMOTE_UNAVAILABLE")
    elif action == "switch":
        ref = request["ref"]
        if not ref.startswith(("refs/heads/", "refs/remotes/")) or git("check-ref-format", ref).returncode:
            fail("BRANCH_INVALID")
        if not value("show-ref", "--verify", "--hash", ref):
            fail("BRANCH_MISSING")
        git_dir = Path(value("rev-parse", "--absolute-git-dir"))
        lock_fd = os.open(git_dir / "flowweave-branch.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(lock_fd, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fail("BUSY")
            current_ref = value("symbolic-ref", "--quiet", "HEAD")
            if current_ref == ref:
                print(json.dumps({"ok": True}))
                sys.exit(0)
            if value("rev-parse", "HEAD") != request["expected_head"] or current_ref != request["expected_ref"]:
                fail("STALE")
            if ref.startswith("refs/heads/"):
                arguments = ["switch", "--no-guess", "--no-overwrite-ignore", "--", ref[len("refs/heads/"):]]
            else:
                remote_branch = ref[len("refs/remotes/"):]
                name = remote_branch.partition("/")[2]
                if not name or value("symbolic-ref", "--quiet", ref):
                    fail("BRANCH_INVALID")
                if value("show-ref", "--verify", "--hash", "refs/heads/" + name):
                    fail("LOCAL_EXISTS")
                arguments = ["switch", "--no-overwrite-ignore", "--track", "-c", name, "--", remote_branch]
            if git(*arguments).returncode:
                fail("SWITCH_CONFLICT")
    else:
        fail("ACTION_INVALID")
    print(json.dumps({"ok": True}))
except subprocess.TimeoutExpired:
    fail("TIMEOUT")
except (OSError, ValueError):
    fail("UNAVAILABLE")
'''
