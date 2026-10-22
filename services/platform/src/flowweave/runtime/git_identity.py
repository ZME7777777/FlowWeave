"""Fixed Git configuration operation executed by OpenHands' native Bash API."""

GIT_IDENTITY_SCRIPT = r'''
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

request = json.loads(sys.argv[1])
root = Path(request["root"])
if not (root.is_absolute() and root.resolve(strict=True) == root and root.is_dir()):
    raise ValueError("Invalid user project root")
config = root / ".flowweave-gitconfig"
key = "includeIf.gitdir:" + str(root) + "/.path"

def plain(path):
    if path.is_symlink():
        raise ValueError("Unsafe Git configuration")
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Unsafe Git configuration")

def git(*args, missing=False):
    result = subprocess.run(["git", "-C", str(root), "config", *args],
                            capture_output=True, text=True, timeout=3)
    if result.returncode and not (missing and result.returncode == 1):
        raise ValueError("Git configuration failed")
    return result.stdout.rstrip("\n")

def read():
    plain(config)
    if not config.exists():
        return {"author": "", "email": "", "configured": False}
    includes = git("--global", "--get-all", key, missing=True).splitlines()
    if str(config) not in includes:
        return {"author": "", "email": "", "configured": False}
    author = git("--file", str(config), "--no-includes", "--get", "user.name", missing=True)
    email = git("--file", str(config), "--no-includes", "--get", "user.email", missing=True)
    confirmed = False
    if author and email:
        # A disposable bare repository exercises Git's real gitdir matching
        # and configuration precedence without initializing the user's root.
        with tempfile.TemporaryDirectory(prefix=".flowweave-git-check-", dir=root) as probe:
            subprocess.run(["git", "-c", "init.templateDir=", "init", "--bare", "-q", probe],
                           check=True, capture_output=True, timeout=3)
            actual = []
            for field in ("user.name", "user.email"):
                value = subprocess.run(["git", "-C", probe, "config", "--get", field],
                                       check=True, capture_output=True, text=True, timeout=3)
                actual.append(value.stdout.rstrip("\n"))
            confirmed = actual == [author, email]
    return {"author": author if confirmed else "",
            "email": email if confirmed else "", "configured": confirmed}

plain(config)
if "author" in request or "profile" in request:
    # A per-user lock protects the pair; Git independently locks the shared
    # global config when registering a directory condition.
    lock = root / ".flowweave-git-settings.lock"
    plain(lock)
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        info = os.fstat(handle.fileno())
        if info.st_nlink != 1 or not stat.S_ISREG(info.st_mode):
            raise ValueError("Invalid user configuration lock")
        fcntl.flock(handle, fcntl.LOCK_EX)
        plain(config)
        automatic = "profile" in request
        if automatic and config.exists():
            source = git("--file", str(config), "--get", "flowweave.identitySource", missing=True)
            if source != "profile":
                print(json.dumps(read()))
                sys.exit(0)
            current = read()
            if (current["configured"]
                    and [current["author"], current["email"]] == request["profile"]):
                print(json.dumps(current))
                sys.exit(0)
        if automatic:
            request["author"], request["email"] = request["profile"]
        previous = config.read_bytes() if config.exists() else None
        descriptor, temporary = tempfile.mkstemp(prefix=".flowweave-gitconfig-", dir=root)
        os.close(descriptor)
        try:
            git("--file", temporary, "user.name", request["author"])
            git("--file", temporary, "user.email", request["email"])
            git("--file", temporary, "flowweave.identitySource",
                "profile" if automatic else "manual")
            os.replace(temporary, config)
            registry_lock = Path.home() / ".flowweave-git-registry.lock"
            plain(registry_lock)
            registry_fd = os.open(registry_lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(registry_fd, "w") as registry:
                info = os.fstat(registry.fileno())
                if info.st_nlink != 1 or not stat.S_ISREG(info.st_mode):
                    raise ValueError("Invalid Git registry lock")
                fcntl.flock(registry, fcntl.LOCK_EX)
                if str(config) not in git("--global", "--get-all", key, missing=True).splitlines():
                    git("--global", "--add", key, str(config))
            result = read()
            if result != dict(author=request["author"], email=request["email"], configured=True):
                raise ValueError("Git configuration was not confirmed")
        except BaseException:
            if previous is None:
                config.unlink(missing_ok=True)
            else:
                Path(temporary).write_bytes(previous)
                os.chmod(temporary, 0o600)
                os.replace(temporary, config)
            raise
        finally:
            Path(temporary).unlink(missing_ok=True)
        print(json.dumps(result))
else:
    lock = root / ".flowweave-git-settings.lock"
    plain(lock)
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r") as handle:
        info = os.fstat(handle.fileno())
        if info.st_nlink != 1 or not stat.S_ISREG(info.st_mode):
            raise ValueError("Invalid user configuration lock")
        fcntl.flock(handle, fcntl.LOCK_SH)
        print(json.dumps(read()))
'''
