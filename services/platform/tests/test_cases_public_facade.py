"""The Case router reaches services only through the public facade; keep them in sync."""

import ast
from pathlib import Path

from flowweave.modules.cases import public


def test_router_only_uses_names_exported_by_the_case_facade() -> None:
    router = Path(public.__file__).parent / "presentation" / "router.py"
    used = {
        node.attr
        for node in ast.walk(ast.parse(router.read_text()))
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "service"
    }
    missing = sorted(name for name in used if not hasattr(public, name))
    assert not missing, f"Case router uses names missing from cases.public: {missing}"
    assert {"request_case_start", "rerun_case_stage"} <= used
