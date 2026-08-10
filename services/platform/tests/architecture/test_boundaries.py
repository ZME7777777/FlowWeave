from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).parents[2] / "src" / "flowweave"
REPOSITORY = Path(__file__).parents[4]
FORBIDDEN_DOMAIN_ROOTS = {"fastapi", "pydantic", "sqlalchemy", "httpx"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_domain_code_is_framework_free() -> None:
    violations: list[str] = []
    domain_files = list(SOURCE.glob("modules/*/domain/**/*.py")) + list(
        (SOURCE / "shared" / "domain").glob("**/*.py")
    )
    for path in domain_files:
        for imported in _imports(path):
            if imported.split(".")[0] in FORBIDDEN_DOMAIN_ROOTS:
                violations.append(f"{path.relative_to(SOURCE)} -> {imported}")
            if imported.startswith("flowweave.shared.models"):
                violations.append(f"{path.relative_to(SOURCE)} -> {imported}")
    assert not violations, "Domain framework imports:\n" + "\n".join(violations)


def test_bootstrap_is_factory_only() -> None:
    api_source = (SOURCE / "bootstrap" / "api.py").read_text()
    assert "app = create_app()" not in api_source
    database_source = (SOURCE / "shared" / "database.py").read_text()
    assert "create_engine(Settings()" not in database_source
    assert "SessionLocal" not in database_source


def test_external_container_images_are_immutable() -> None:
    violations: list[str] = []
    dockerfiles = (
        REPOSITORY / "services" / "platform" / "Dockerfile",
        REPOSITORY / "apps" / "web" / "Dockerfile",
        REPOSITORY / "infra" / "dependency-builder" / "Dockerfile",
        REPOSITORY / "infra" / "openhands" / "Dockerfile",
        REPOSITORY / "infra" / "sandbox" / "python" / "Dockerfile",
        REPOSITORY / "infra" / "sandbox" / "javascript" / "Dockerfile",
    )
    for path in dockerfiles:
        aliases: set[str] = set()
        for line_number, line in enumerate(path.read_text().splitlines(), 1):
            tokens = line.strip().split()
            if not tokens or tokens[0].upper() != "FROM":
                continue
            reference = next((token for token in tokens[1:] if not token.startswith("--")), "")
            if reference not in aliases and "@sha256:" not in reference:
                violations.append(f"{path.relative_to(REPOSITORY)}:{line_number} -> {reference}")
            upper_tokens = [token.upper() for token in tokens]
            if "AS" in upper_tokens:
                alias_index = upper_tokens.index("AS") + 1
                if alias_index < len(tokens):
                    aliases.add(tokens[alias_index])

    compose = (REPOSITORY / "infra" / "compose.yaml").read_text()
    for reference in (
        "alpine:3.22",
        "postgres:16.9-alpine3.21",
    ):
        if f"image: {reference}@sha256:" not in compose:
            violations.append(f"infra/compose.yaml -> {reference}")

    postgres_digest = (
        "postgres:16.9-alpine3.21"
        "@sha256:36e8aabaa6fa6037537cff64011fa45a200fe2ba202141b9aca48cff3df7ad42"
    )
    for relative in (
        "services/platform/tests/conftest.py",
        "services/platform/scripts/migration_check.py",
    ):
        if postgres_digest not in (REPOSITORY / relative).read_text().replace(
            '"\n            "', ""
        ):
            violations.append(f"{relative} -> postgres test image")

    assert not violations, "Mutable external container images:\n" + "\n".join(violations)


def test_bootstrap_entrypoints_import_in_clean_processes() -> None:
    """Catch import-order bugs hidden by pytest's already-populated module cache."""

    package_root = SOURCE.parents[1]
    environment = {
        **os.environ,
        "PYTHONPATH": str(package_root),
    }
    for module in (
        "flowweave.bootstrap.api",
        "flowweave.bootstrap.worker",
        "flowweave.bootstrap.sandbox_controller",
    ):
        completed = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=environment,
        )
        assert completed.returncode == 0, (
            f"clean import failed for {module}: {(completed.stderr or completed.stdout)[-4000:]}"
        )


def test_production_package_contains_no_sqlite_compatibility() -> None:
    violations = []
    for path in SOURCE.rglob("*.py"):
        source = path.read_text().lower()
        if "sqlite" in source:
            violations.append(str(path.relative_to(SOURCE)))
    assert not violations, "SQLite compatibility remains: " + ", ".join(violations)


def test_modules_expose_public_facades() -> None:
    modules = [
        path
        for path in (SOURCE / "modules").iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    ]
    missing = [path.name for path in modules if not (path / "public.py").is_file()]
    assert not missing, f"Missing public.py facades: {missing}"


def test_api_presentation_uses_async_uow_only() -> None:
    violations: list[str] = []
    for path in SOURCE.glob("modules/*/presentation/**/*.py"):
        source = path.read_text()
        if "sync_sessions" in source:
            violations.append(f"{path.relative_to(SOURCE)} uses sync_sessions")
        if "flowweave.shared.http" in source and "run_sync" not in source:
            violations.append(f"{path.relative_to(SOURCE)} bypasses async run_sync")
    http_source = (SOURCE / "shared" / "http.py").read_text()
    assert "AsyncSession" in http_source
    assert "container.database.uow()" in http_source
    assert "sync_sessions" not in http_source
    runs_router = (SOURCE / "modules" / "runs" / "presentation" / "router.py").read_text()
    assert "container.database.session()" in runs_router
    assert not violations, "Synchronous API transaction paths:\n" + "\n".join(violations)


def test_cross_module_dependencies_use_public_facades() -> None:
    violations: list[str] = []
    modules_root = SOURCE / "modules"
    for path in modules_root.glob("*/**/*.py"):
        relative = path.relative_to(modules_root)
        owner = relative.parts[0]
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            prefix = "flowweave.modules."
            if not node.module.startswith(prefix):
                continue
            parts = node.module[len(prefix) :].split(".")
            target = parts[0]
            if target == owner:
                continue
            imports_public_module = len(parts) == 1 and any(
                alias.name == "public" for alias in node.names
            )
            imports_from_public = len(parts) == 2 and parts[1] == "public"
            if not (imports_public_module or imports_from_public):
                violations.append(f"{path.relative_to(SOURCE)} -> {node.module}")
    assert not violations, "Cross-module internal imports:\n" + "\n".join(violations)


def test_orm_mappings_are_owned_by_module_infrastructure() -> None:
    violations: list[str] = []
    allowed = {path.resolve() for path in SOURCE.glob("modules/*/infrastructure/models.py")}
    for path in SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            declares_mapping = any(
                isinstance(base, ast.Name)
                and base.id == "Base"
                or isinstance(base, ast.Attribute)
                and base.attr == "Base"
                for base in node.bases
            )
            if declares_mapping and path.resolve() not in allowed:
                violations.append(f"{path.relative_to(SOURCE)}:{node.name}")

    shared_models = ast.parse(
        (SOURCE / "shared" / "models.py").read_text(),
        filename=str(SOURCE / "shared" / "models.py"),
    )
    assert not any(isinstance(node, ast.ClassDef) for node in shared_models.body)
    assert not violations, "ORM mappings outside module infrastructure:\n" + "\n".join(violations)
