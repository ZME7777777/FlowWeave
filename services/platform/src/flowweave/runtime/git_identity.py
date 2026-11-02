"""Fixed Git configuration operation executed by OpenHands' native Bash API."""

GIT_IDENTITY_SCRIPT = r'''
import fcntl
import hashlib
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

def read_defaults():
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

def repository_config(repo, filename):
    if (repo / ".git").is_symlink():
        return None
    resolved = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-path", filename],
        capture_output=True, text=True, timeout=3)
    if resolved.returncode:
        raise ValueError("Cannot inspect repository configuration")
    path = Path(resolved.stdout.strip())
    if (not path.is_absolute() or not path.is_relative_to(root) or not path.exists()
            or path.resolve(strict=True) != path):
        return None
    plain(path)
    return path

def clear_repository_identity(path, legacy_only):
    legacy = [git("--file", str(path), "--no-includes", "--get-all", key, missing=True)
              for key in ("user.name", "user.email")]
    if not any(legacy) or (legacy_only and legacy != ["openhands", "openhands@all-hands.dev"]):
        return
    lock_path = Path(str(path) + ".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                         stat.S_IMODE(path.stat().st_mode))
    try:
        with os.fdopen(descriptor, "wb") as locked:
            original = path.read_bytes()
            locked.write(original)
        current = [git("--file", str(lock_path), "--no-includes", "--get-all", key, missing=True)
                   for key in ("user.name", "user.email")]
        if current != legacy:
            raise ValueError("Repository configuration changed concurrently")
        for key, value in zip(("user.name", "user.email"), legacy):
            if value:
                git("--file", str(lock_path), "--unset-all", key)
        if path.read_bytes() != original:
            raise ValueError("Repository configuration changed concurrently")
        os.replace(lock_path, path)
    finally:
        lock_path.unlink(missing_ok=True)

def reconcile_legacy_identity():
    if git("--file", str(config), "--get", "flowweave.legacyIdentityRepair", missing=True) == "1":
        return
    seen = set()
    for directory, children, files in os.walk(root, followlinks=False):
        repo = Path(directory)
        children[:] = [name for name in children
                       if name not in {".git", "node_modules", ".venv", "venv", ".cache",
                                       ".npm", "__pycache__", "target", "dist", "build"}
                       and not (repo / name).is_symlink()]
        if not ((repo / ".git").is_dir() or (repo / ".git").is_file()):
            continue
        for filename in ("config", "config.worktree"):
            path = repository_config(repo, filename)
            if path is None or path in seen:
                continue
            seen.add(path)
            clear_repository_identity(path, legacy_only=True)
    git("--file", str(config), "flowweave.legacyIdentityRepair", "1")

def read():
    # A root that is itself a repository has a real local override. Report it
    # directly rather than substituting the managed defaults or login profile.
    if (root / ".git").exists() and not (root / ".git").is_symlink():
        if repository_config(root, "config") is None:
            raise ValueError("Unsafe root repository configuration")
        author, email = [git("--get", key, missing=True) for key in ("user.name", "user.email")]
        complete = bool(author and email)
        return {"author": author if complete else "", "email": email if complete else "",
                "configured": complete}
    return read_defaults()

def display():
    print(json.dumps(read()))

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
                if read_defaults()["configured"]:
                    reconcile_legacy_identity()
                display()
                sys.exit(0)
            stored = [git("--file", str(config), "--no-includes", "--get", key, missing=True)
                      for key in ("user.name", "user.email")]
            digest = git("--file", str(config), "--get", "flowweave.profileIdentityDigest",
                         missing=True)
            expected_digest = hashlib.sha256(json.dumps(stored).encode()).hexdigest()
            if ((digest and digest != expected_digest)
                    or (not digest and stored != request["profile"])):
                # Editing the directory file externally makes it user-owned.
                # A subsequent message must not replace that choice with LDAP.
                git("--file", str(config), "flowweave.identitySource", "manual")
                if read_defaults()["configured"]:
                    reconcile_legacy_identity()
                display()
                sys.exit(0)
            current = read_defaults()
            if (current["configured"]
                    and [current["author"], current["email"]] == request["profile"]):
                reconcile_legacy_identity()
                display()
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
            if automatic:
                pair = json.dumps([request["author"], request["email"]]).encode()
                digest = hashlib.sha256(pair).hexdigest()
                git("--file", temporary, "flowweave.profileIdentityDigest", digest)
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
            result = read_defaults()
            if result != dict(author=request["author"], email=request["email"], configured=True):
                raise ValueError("Git configuration was not confirmed")
            reconcile_legacy_identity()
            if not automatic and (root / ".git").exists():
                # Saving this directory explicitly replaces its own override;
                # custom identities in descendant repositories remain intact.
                for filename in ("config", "config.worktree"):
                    path = repository_config(root, filename)
                    if path is not None:
                        clear_repository_identity(path, legacy_only=False)
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
        display()
else:
    lock = root / ".flowweave-git-settings.lock"
    plain(lock)
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r") as handle:
        info = os.fstat(handle.fileno())
        if info.st_nlink != 1 or not stat.S_ISREG(info.st_mode):
            raise ValueError("Invalid user configuration lock")
        fcntl.flock(handle, fcntl.LOCK_EX)
        display()
'''
