"""Declarative key-budget queries and a bounded numeric expression language."""

from __future__ import annotations

import ast
import math
import operator
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from flowweave.shared.secret_redaction import is_sensitive_key

_PATH = re.compile(
    r"[A-Za-z_][A-Za-z_0-9]*(?:(?:\.(?:[A-Za-z_][A-Za-z_0-9]*|\*))|(?:\[(?:\d+|[A-Za-z_][A-Za-z_0-9]*=[^\]\s]+)\]))*"
)
_SEGMENT = re.compile(r"([A-Za-z_][A-Za-z_0-9]*|\*)|\[([^\]]+)\]")
_OPERATORS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_FUNCTIONS = {"sum", "min", "max", "count"}


@dataclass(frozen=True)
class MetricExpression:
    label: str
    currency: bool
    percentage: bool
    tree: ast.Expression
    paths: dict[str, str]


def parse_expressions(source: str) -> list[MetricExpression]:
    if not source.strip() or len(source) > 2000:
        raise ValueError("余额字段不能为空且最多 2000 个字符")
    parts = source.split(";")
    if len(parts) > 10 or any(not part.strip() for part in parts):
        raise ValueError("请用分号分隔最多 10 个余额表达式")
    expressions: list[MetricExpression] = []
    for part in parts:
        label, separator, expression = part.strip().partition(":")
        expression = expression.strip() if separator else label
        label = label.strip() if separator else "余额"
        if not label or len(label) > 60:
            raise ValueError("余额标签长度须为 1 至 60 个字符")
        currency = expression.startswith("$")
        percentage = expression.endswith("%")
        expression = expression.removeprefix("$").removesuffix("%").strip()
        paths: dict[str, str] = {}

        def replace_path(
            match: re.Match[str], expression: str = expression, paths: dict[str, str] = paths
        ) -> str:
            path = match.group()
            if path in _FUNCTIONS and expression[match.end() :].lstrip().startswith("("):
                return path
            key = f"field_{len(paths)}"
            paths[key] = path
            return key

        translated = _PATH.sub(replace_path, expression)
        try:
            tree = ast.parse(translated, mode="eval")
        except (SyntaxError, RecursionError) as exc:
            raise ValueError("余额表达式格式无效") from exc
        nodes = list(ast.walk(tree))
        allowed = (
            ast.Expression,
            ast.BinOp,
            ast.UnaryOp,
            ast.Constant,
            ast.Name,
            ast.Load,
            ast.Call,
            ast.Add,
            ast.Sub,
            ast.Mult,
            ast.Div,
            ast.UAdd,
            ast.USub,
        )
        if len(nodes) > 160 or any(not isinstance(node, allowed) for node in nodes):
            raise ValueError("余额表达式仅支持字段、四则运算和 sum/min/max/count")
        for node in nodes:
            if isinstance(node, ast.Call) and (
                not isinstance(node.func, ast.Name)
                or node.func.id not in _FUNCTIONS
                or len(node.args) != 1
                or node.keywords
            ):
                raise ValueError("汇总函数仅接受一个字段或数组")
            if isinstance(node, ast.Constant) and (
                isinstance(node.value, bool) or not isinstance(node.value, int | float)
            ):
                raise ValueError("余额表达式只接受数字常量")
        expressions.append(MetricExpression(label, currency, percentage, tree, paths))
    return expressions


def _path_values(payload: object, path: str) -> object:
    values: list[Any] = [payload]
    multiple = False
    for match in _SEGMENT.finditer(path):
        field, selector = match.groups()
        next_values: list[Any] = []
        for value in values:
            if field == "*" and isinstance(value, list):
                next_values.extend(cast(list[Any], value))
                multiple = True
            elif field and isinstance(value, dict) and field in value:
                next_values.append(value[field])
            elif selector and selector.isdigit() and isinstance(value, list):
                index = int(selector)
                items = cast(list[Any], value)
                if index < len(items):
                    next_values.append(items[index])
            elif selector and "=" in selector and isinstance(value, list):
                key, expected = selector.split("=", 1)
                next_values.extend(
                    cast(dict[str, Any], item)
                    for item in cast(list[Any], value)
                    if isinstance(item, dict)
                    and str(cast(dict[str, Any], item).get(key)) == expected
                )
                multiple = True
        values = next_values
        if len(values) > 10000:
            raise ValueError("余额数组过大")
    if multiple:
        return values
    if len(values) != 1:
        raise ValueError("余额字段不存在")
    return values[0]


def _numeric(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ValueError("余额字段不是数字")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("余额字段不是有限数字")
    return number


def _evaluate(node: ast.AST, payload: object, paths: dict[str, str]) -> object:
    if isinstance(node, ast.Constant):
        return _numeric(node.value)
    if isinstance(node, ast.Name) and node.id in paths:
        return _path_values(payload, paths[node.id])
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
        left = _numeric(_evaluate(node.left, payload, paths))
        right = _numeric(_evaluate(node.right, payload, paths))
        return _numeric(_OPERATORS[type(node.op)](left, right))
    if isinstance(node, ast.UnaryOp):
        value = _numeric(_evaluate(node.operand, payload, paths))
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        value = _evaluate(node.args[0], payload, paths)
        if not isinstance(value, list):
            raise ValueError("汇总函数需要数组")
        items = cast(list[object], value)
        if len(items) > 10000:
            raise ValueError("余额数组过大")
        if node.func.id == "count":
            return len(items)
        numbers = [_numeric(item) for item in items]
        # An empty match cannot be presented as a known zero balance.
        if not numbers:
            raise ValueError("未匹配到余额字段")
        functions = {"sum": sum, "min": min, "max": max}
        return functions[node.func.id](numbers)
    raise ValueError("余额表达式无效")


def evaluate_metrics(source: str, payload: object) -> list[dict[str, object]]:
    metrics: list[dict[str, object]] = []
    for expression in parse_expressions(source):
        try:
            value = _numeric(_evaluate(expression.tree.body, payload, expression.paths))
            if expression.percentage:
                value = _numeric(value * 100)
        except (ValueError, ArithmeticError, RecursionError):
            value = None
        metrics.append(
            {
                "label": expression.label,
                "value": value,
                "currency": expression.currency,
                "percentage": expression.percentage,
            }
        )
    return metrics


class ProviderUsageQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2000)
    method: Literal["GET", "POST"] = "GET"
    expression: str = Field(min_length=1, max_length=2000)
    headers: dict[str, str] = Field(default_factory=dict, max_length=20)

    @model_validator(mode="after")
    def validate_query(self) -> ProviderUsageQuery:
        self.url = self.url.strip()
        parsed = urlsplit(self.url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or "{" in parsed.netloc
            or "}" in parsed.netloc
        ):
            raise ValueError("查询地址须为完整的 HTTP 或 HTTPS 地址")
        # Validate ports and templates before any request can use a credential.
        _ = parsed.port
        if "{" in self.url.replace("{key}", "") or "}" in self.url.replace("{key}", ""):
            raise ValueError("查询地址仅支持 {key} 占位符")
        for name, value in parse_qsl(parsed.query):
            if is_sensitive_key(name) and value and "{key}" not in value:
                raise ValueError("查询地址中的密钥须使用 {key} 占位符")
        for name, value in self.headers.items():
            if (
                not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name)
                or name.lower()
                in {"host", "content-length", "connection", "transfer-encoding", "cookie"}
                or len(value) > 2000
                or "\r" in value
                or "\n" in value
            ):
                raise ValueError("查询请求头无效")
            if is_sensitive_key(name) and "{key}" not in value:
                raise ValueError("认证请求头须使用 {key} 占位符")
            if "{" in value.replace("{key}", "") or "}" in value.replace("{key}", ""):
                raise ValueError("查询请求头仅支持 {key} 占位符")
        parse_expressions(self.expression)
        return self
