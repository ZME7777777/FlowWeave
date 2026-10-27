from types import SimpleNamespace

from flowweave_admin_metrics import sampler


def test_cleanup_respects_age_managed_runtime_and_lock(monkeypatch):
    calls = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, query, args):
            calls.append(query)
            return SimpleNamespace(fetchone=lambda: {"pg_try_advisory_lock": True})

    monkeypatch.setattr(sampler.psycopg, "connect", lambda *args, **kwargs: Connection())
    monkeypatch.setattr(
        sampler,
        "_observations",
        lambda _: {
            "managed_resources": [
                {
                    "kind": "agent-runtime",
                    "state": "RUNNING",
                    "resource_id": "owned-id",
                    "resource_name": "owned-name",
                },
                {"kind": "environment", "resource_id": "excluded", "resource_name": "excluded"},
            ]
        },
    )
    monkeypatch.setattr(
        sampler,
        "_runtime_services",
        lambda _, resource: [
            {"age_seconds": 86399, "pid": 1},
            {"age_seconds": 86400, "pid": 91},
            {"pid": 2},
        ],
    )
    stopped = []
    monkeypatch.setattr(
        sampler,
        "_stop_runtime_service",
        lambda _, resource, service: stopped.append((resource, service)) or True,
    )
    count = sampler.cleanup_expired_temporary_services(
        SimpleNamespace(
            normalized_database_url="isolated", admin_temporary_service_max_age_seconds=86400
        )
    )
    assert count == 1 and stopped[0][1]["pid"] == 91 and stopped[0][0]["resource_id"] == "owned-id"
    assert "pg_advisory_unlock" in calls[-1]


def test_collect_once_writes_through_psycopg_cursor_and_releases_lock(monkeypatch):
    rows = []
    calls = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def executemany(self, query, values):
            assert "INSERT INTO admin_metric_samples" in query
            rows.extend(values)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def cursor(self):
            return Cursor()

        def execute(self, query, args):
            calls.append(query)
            return SimpleNamespace(fetchone=lambda: {"pg_try_advisory_lock": True})

    monkeypatch.setattr(sampler.psycopg, "connect", lambda *args, **kwargs: Connection())
    monkeypatch.setattr(
        sampler,
        "_observations",
        lambda _: {
            "services": [{"service": "api", "usage": {"cpu_usage_percent": 2.5}}],
        },
    )
    assert sampler.collect_once(
        SimpleNamespace(
            normalized_database_url="isolated",
            admin_metrics_retention_days=14,
        )
    )
    assert len(rows) == 1
    assert rows[0][1:] == ("SERVICE", "api", "cpu_usage_percent", 2.5)
    assert any("DELETE FROM admin_metric_samples" in query for query in calls)
    assert "pg_advisory_unlock" in calls[-1]


def test_cleanup_targets_only_running_agent_runtimes():
    resources = sampler._managed_runtime_resources(
        {
            "managed_resources": [
                {
                    "kind": "agent-runtime",
                    "state": state,
                    "resource_id": state,
                    "resource_name": state,
                }
                for state in ("RUNNING", "EXITED", "CREATED", "UNKNOWN")
            ]
        }
    )
    assert resources == [{"resource_id": "RUNNING", "resource_name": "RUNNING"}]
