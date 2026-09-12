from __future__ import annotations

import asyncio
import importlib
import logging
import math
import time
from collections import Counter, defaultdict, deque
from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from threading import Lock
from typing import Any

from flowweave.bootstrap.settings import Settings

logger = logging.getLogger(__name__)
_REQUEST_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_current_metrics: ContextVar[Metrics | None] = ContextVar("flowweave_current_metrics", default=None)


def _label_value(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(values: Mapping[str, object]) -> str:
    if not values:
        return ""
    return (
        "{"
        + ",".join(f'{name}="{_label_value(value)}"' for name, value in sorted(values.items()))
        + "}"
    )


class Metrics:
    """Small, dependency-free Prometheus text registry with bounded label sets."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: Counter[tuple[str, tuple[tuple[str, str], ...]]] = Counter()
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._request_duration: defaultdict[tuple[str, str, str], list[float]] = defaultdict(
            lambda: [0.0, 0.0, *([0.0] * len(_REQUEST_BUCKETS))]
        )
        self._operation_duration: defaultdict[tuple[str, str], list[float]] = defaultdict(
            lambda: [0.0, 0.0, *([0.0] * len(_REQUEST_BUCKETS))]
        )

    def increment(self, name: str, /, **labels: object) -> None:
        key = (name, tuple(sorted((key, str(value)) for key, value in labels.items())))
        with self._lock:
            self._counters[key] += 1

    def gauge(self, name: str, value: float, /, **labels: object) -> None:
        key = (name, tuple(sorted((key, str(label)) for key, label in labels.items())))
        with self._lock:
            self._gauges[key] = value

    def observe_request(
        self, method: str, route: str, status: int, duration_seconds: float
    ) -> None:
        label_key = (method, route, str(status))
        value = max(0.0, duration_seconds)
        with self._lock:
            bucket = self._request_duration[label_key]
            bucket[0] += 1
            bucket[1] += value
            for index, boundary in enumerate(_REQUEST_BUCKETS, start=2):
                if value <= boundary:
                    bucket[index] += 1

    def observe_operation(
        self, operation: str, duration_seconds: float, *, outcome: str, items: int | None = None
    ) -> None:
        """Record a low-cardinality application operation.

        Callers must supply a fixed operation name and outcome.  Deliberately
        absent are request, workspace, conversation, user, path, and event
        identifiers: Prometheus label cardinality must remain bounded.
        """

        label_key = (operation, outcome)
        value = max(0.0, duration_seconds)
        with self._lock:
            bucket = self._operation_duration[label_key]
            bucket[0] += 1
            bucket[1] += value
            for index, boundary in enumerate(_REQUEST_BUCKETS, start=2):
                if value <= boundary:
                    bucket[index] += 1
            if items is not None:
                self._counters[
                    (
                        "flowweave_operation_items_total",
                        (("operation", operation), ("outcome", outcome)),
                    )
                ] += max(0, items)

    def render(self) -> str:
        lines = [
            "# HELP flowweave_http_requests_total Completed HTTP requests.",
            "# TYPE flowweave_http_requests_total counter",
            "# HELP flowweave_http_request_duration_seconds Request duration seconds.",
            "# TYPE flowweave_http_request_duration_seconds histogram",
            (
                "# HELP flowweave_operation_duration_seconds "
                "Duration of bounded application operations."
            ),
            "# TYPE flowweave_operation_duration_seconds histogram",
            (
                "# HELP flowweave_operation_items_total "
                "Items processed by bounded application operations."
            ),
            "# TYPE flowweave_operation_items_total counter",
            "# HELP flowweave_rate_limit_decisions_total Rate-limit decisions by bounded scope.",
            "# TYPE flowweave_rate_limit_decisions_total counter",
            "# HELP flowweave_rate_limit_backend Active rate-limit coordination backend.",
            "# TYPE flowweave_rate_limit_backend gauge",
            "# HELP flowweave_database_pool_connections Database pool connection watermarks.",
            "# TYPE flowweave_database_pool_connections gauge",
            "# HELP flowweave_runtime_relay_hubs Active Runtime relay hubs.",
            "# TYPE flowweave_runtime_relay_hubs gauge",
            "# HELP flowweave_runtime_relay_subscribers Active Runtime relay subscribers.",
            "# TYPE flowweave_runtime_relay_subscribers gauge",
            "# HELP flowweave_runtime_relay_hub_capacity Configured Runtime relay hub capacity.",
            "# TYPE flowweave_runtime_relay_hub_capacity gauge",
            "# HELP flowweave_terminal_attachments Active terminal PTY attachments.",
            "# TYPE flowweave_terminal_attachments gauge",
            "# HELP flowweave_terminal_sessions Active terminal tmux sessions.",
            "# TYPE flowweave_terminal_sessions gauge",
        ]
        with self._lock:
            counters = tuple(self._counters.items())
            gauges = tuple(self._gauges.items())
            durations = tuple(self._request_duration.items())
            operation_durations = tuple(self._operation_duration.items())
        for (name, labels), value in counters:
            lines.append(f"{name}{_labels(dict(labels))} {value}")
        for (name, labels), value in gauges:
            lines.append(f"{name}{_labels(dict(labels))} {value}")
        for (method, route, status), values in durations:
            base = {"method": method, "route": route, "status": status}
            for boundary, count in zip(_REQUEST_BUCKETS, values[2:], strict=True):
                lines.append(
                    "flowweave_http_request_duration_seconds_bucket"
                    f"{_labels({**base, 'le': boundary})} {int(count)}"
                )
            lines.append(
                "flowweave_http_request_duration_seconds_bucket"
                f"{_labels({**base, 'le': '+Inf'})} {int(values[0])}"
            )
            lines.append(
                f"flowweave_http_request_duration_seconds_count{_labels(base)} {int(values[0])}"
            )
            lines.append(
                f"flowweave_http_request_duration_seconds_sum{_labels(base)} {values[1]:.9f}"
            )
        for (operation, outcome), values in operation_durations:
            base = {"operation": operation, "outcome": outcome}
            for boundary, count in zip(_REQUEST_BUCKETS, values[2:], strict=True):
                lines.append(
                    "flowweave_operation_duration_seconds_bucket"
                    f"{_labels({**base, 'le': boundary})} {int(count)}"
                )
            lines.append(
                "flowweave_operation_duration_seconds_bucket"
                f"{_labels({**base, 'le': '+Inf'})} {int(values[0])}"
            )
            lines.append(
                f"flowweave_operation_duration_seconds_count{_labels(base)} {int(values[0])}"
            )
            lines.append(f"flowweave_operation_duration_seconds_sum{_labels(base)} {values[1]:.9f}")
        return "\n".join(lines) + "\n"


def bind_metrics(metrics: Metrics) -> Token[Metrics | None]:
    return _current_metrics.set(metrics)


def reset_metrics(token: Token[Metrics | None]) -> None:
    _current_metrics.reset(token)


def current_metrics() -> Metrics | None:
    """Return request-bound metrics, or no-op outside an instrumented process."""

    return _current_metrics.get()


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    backend: str


class RateLimiter:
    """Fixed-window rate limiter with optional Redis/Valkey process coordination."""

    def __init__(self, settings: Settings, metrics: Metrics) -> None:
        self._redis_url = settings.rate_limit_redis_url
        self._metrics = metrics
        self._redis: Any | None = None
        self._local: dict[str, deque[float]] = {}
        self._local_lock = asyncio.Lock()
        self._backend = "local"

    async def start(self) -> None:
        if not self._redis_url:
            self._metrics.gauge("flowweave_rate_limit_backend", 1, backend="local")
            return
        try:
            redis_module: Any = importlib.import_module("redis.asyncio")
            client: Any = redis_module.Redis.from_url(self._redis_url, decode_responses=True)
            await client.ping()
            self._redis = client
            self._backend = "redis"
        except Exception:
            logger.warning(
                "Redis/Valkey rate limit backend is unavailable; using process-local limits"
            )
            self._redis = None
            self._backend = "local_fallback"
        self._metrics.gauge("flowweave_rate_limit_backend", 1, backend=self._backend)

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def allow(
        self, scope: str, subject: str, *, limit: int, window_seconds: int
    ) -> RateLimitDecision:
        if self._redis is not None:
            try:
                window = math.floor(time.time() / window_seconds)
                key = f"flowweave:rate-limit:{scope}:{window}:{subject}"
                pipeline = self._redis.pipeline(transaction=True)
                pipeline.incr(key)
                pipeline.expire(key, window_seconds + 1, nx=True)
                count, _ = await pipeline.execute()
                allowed = int(count) <= limit
                self._record(scope, self._backend, allowed)
                return RateLimitDecision(allowed, self._backend)
            except Exception:
                logger.warning("Redis/Valkey rate limit request failed; using process-local limits")
                self._backend = "local_fallback"
                self._metrics.gauge("flowweave_rate_limit_backend", 1, backend=self._backend)
        now = time.monotonic()
        key = f"{scope}:{subject}"
        async with self._local_lock:
            entries = self._local.setdefault(key, deque())
            cutoff = now - window_seconds
            while entries and entries[0] <= cutoff:
                entries.popleft()
            allowed = len(entries) < limit
            if allowed:
                entries.append(now)
        self._record(scope, self._backend, allowed)
        return RateLimitDecision(allowed, self._backend)

    def _record(self, scope: str, backend: str, allowed: bool) -> None:
        self._metrics.increment(
            "flowweave_rate_limit_decisions_total",
            scope=scope,
            backend=backend,
            outcome="allowed" if allowed else "rejected",
        )
