import asyncio
from types import SimpleNamespace

import httpx
import pytest

from flowweave_admin import control


@pytest.mark.parametrize("operation", ["list", "stop"])
def test_admin_request_uses_dedicated_key_and_scope(monkeypatch, operation):
    requests = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            requests.append((url, kwargs))
            return httpx.Response(
                200, json={"services": [{"pid": 91}]} if operation == "list" else {"stopped": True}
            )

    monkeypatch.setattr(control.httpx, "AsyncClient", Client)
    settings = SimpleNamespace(
        sandbox_manager_scope="owned-scope",
        admin_runtime_observer_key="observe-only",
        admin_runtime_controller_key="control-only",
        runtime_provider_url="http://provider",
        admin_api_request_timeout_seconds=5,
    )
    args = dict(resource_name="owned-name", resource_id="owned-id")
    result = asyncio.run(
        control.list_runtime_services(settings, **args)
        if operation == "list"
        else control.stop_runtime_service(
            settings, **args, pid=91, started_ticks="42", process_group_id=91
        )
    )
    assert result
    url, request = requests[0]
    assert url.endswith("/services/" + operation)
    assert request["json"]["manager_scope"] == "owned-scope"
    assert request["headers"]["Authorization"] == "Bearer " + (
        "observe-only" if operation == "list" else "control-only"
    )
    if operation == "stop":
        assert request["json"]["started_ticks"] == "42"


def test_admin_stop_without_control_key_fails_closed():
    with pytest.raises(control.AdminControlError) as error:
        asyncio.run(
            control.stop_runtime_service(
                SimpleNamespace(admin_runtime_controller_key=""),
                resource_name="x",
                resource_id="y",
                pid=91,
                started_ticks="42",
                process_group_id=None,
            )
        )
    assert error.value.status == 503
