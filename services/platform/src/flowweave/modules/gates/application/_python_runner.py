from __future__ import annotations

import ast
import json
import sys
from typing import Any

ALLOWED_NODES = {
    ast.Module,
    ast.Assign,
    ast.Expr,
    ast.Dict,
    ast.List,
    ast.Tuple,
    ast.Set,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Subscript,
    ast.Slice,
    ast.Compare,
    ast.BoolOp,
    ast.BinOp,
    ast.UnaryOp,
    ast.IfExp,
    ast.Call,
    ast.keyword,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.comprehension,
    ast.And,
    ast.Or,
    ast.Not,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
}
SAFE_CALLS = {
    "all": all,
    "any": any,
    "bool": bool,
    "float": float,
    "int": int,
    "len": len,
    "max": max,
    "min": min,
    "sorted": sorted,
    "str": str,
    "sum": sum,
}


def validate(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if type(node) not in ALLOWED_NODES:
            raise ValueError(f"Python gate syntax is not allowed: {type(node).__name__}")
        if isinstance(node, ast.Call) and (
            not isinstance(node.func, ast.Name) or node.func.id not in SAFE_CALLS
        ):
            raise ValueError("Python gate may only call approved pure functions")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ValueError("Dunder names are not allowed")


def main() -> None:
    payload: dict[str, Any] = json.load(sys.stdin)
    code = str(payload.get("code", ""))
    if not code or len(code.encode()) > 32_768:
        raise ValueError("Python gate code is empty or too large")
    tree = ast.parse(code, mode="exec")
    validate(tree)
    scope: dict[str, Any] = {"context": payload.get("context", {})}
    exec(compile(tree, "<gate>", "exec"), {"__builtins__": SAFE_CALLS}, scope)
    if "result" not in scope:
        raise ValueError("Python gate must assign a result object")
    json.dump(scope["result"], sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        json.dump({"runner_error": str(exc)}, sys.stdout)
        raise SystemExit(2) from None
