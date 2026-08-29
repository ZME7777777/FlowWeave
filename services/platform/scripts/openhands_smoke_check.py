"""Deterministic end-to-end smoke checks for the pinned OpenHands image.

The check runs an Agent Server and a local OpenAI-compatible fake model on an
isolated Docker network. No model request or project data leaves the host.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

IMAGE = "flowweave-openhands-runtime:1"
SESSION_KEY = "flowweave-smoke-key"
SECRET_KEY = "flowweave-smoke-persistent-secret"
EXPECTED_VERSION = "1.44.0"
REPOSITORY = Path(__file__).resolve().parents[3]
FAKE_MODEL = REPOSITORY / "services/platform/scripts/openhands_fake_llm.py"


def _run(*args: str, capture: bool = False) -> str:
    completed = subprocess.run(args, check=True, text=True, capture_output=capture)
    return completed.stdout.strip() if capture else ""


def _request(
    base_url: str,
    method: str,
    path: str,
    payload: object | None = None,
    *,
    timeout: float = 30,
) -> tuple[int, object]:
    request = urllib.request.Request(
        base_url + path,
        data=None if payload is None else json.dumps(payload).encode(),
        method=method,
        headers={
            "X-Session-API-Key": SESSION_KEY,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return exc.code, json.loads(body) if body else {}


def _wait_for(
    base_url: str, conversation_id: str, expected: set[str], *, timeout: float = 30
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        code, value = _request(base_url, "GET", f"/api/conversations/{conversation_id}")
        if code == 200 and isinstance(value, dict):
            last = value
            if str(value.get("execution_status")) in expected:
                return value
        time.sleep(0.2)
    raise AssertionError(
        f"conversation {conversation_id} did not reach {sorted(expected)}; last={last}"
    )


def _events(base_url: str, conversation_id: str) -> list[dict[str, object]]:
    code, value = _request(
        base_url,
        "GET",
        f"/api/conversations/{conversation_id}/events/search?limit=100&sort_order=TIMESTAMP",
    )
    assert code == 200 and isinstance(value, dict), (code, value)
    items = value.get("items", [])
    assert isinstance(items, list), value
    return [item for item in items if isinstance(item, dict)]


def _llm(base_url: str, usage_id: str) -> dict[str, object]:
    return {
        "model": "openai/fake-model",
        "base_url": base_url,
        "api_key": "smoke",
        "usage_id": usage_id,
        "num_retries": 0,
        "timeout": 20,
    }


def _confirmation_smoke(base_url: str, fake_model_url: str, suffix: str) -> str:
    code, created = _request(
        base_url,
        "POST",
        "/api/conversations",
        {
            "workspace": {
                "kind": "LocalWorkspace",
                "working_dir": f"/runtime/workspace/project/confirmation-{suffix}",
            },
            "max_iterations": 4,
            "agent": {
                "kind": "Agent",
                "llm": _llm(fake_model_url, "confirmation-smoke"),
                "tools": [{"name": "terminal", "params": {}}],
                "condenser": {"kind": "NoOpCondenser"},
                "agent_context": {
                    "skills": [],
                    "system_message_suffix": "Use the terminal once, then finish.",
                },
            },
            "confirmation_policy": {"kind": "AlwaysConfirm"},
            "initial_message": {
                "role": "user",
                "content": [{"type": "text", "text": "Run the smoke check."}],
                "run": True,
            },
        },
    )
    assert code == 201 and isinstance(created, dict), (code, created)
    conversation_id = str(created["id"])
    _wait_for(base_url, conversation_id, {"waiting_for_confirmation"})

    pending = [
        item
        for item in _events(base_url, conversation_id)
        if item.get("kind") == "ActionEvent"
        and isinstance(item.get("action"), dict)
        and item["action"].get("kind") == "TerminalAction"
    ]
    assert len(pending) == 1, pending
    code, response = _request(
        base_url,
        "POST",
        f"/api/conversations/{conversation_id}/events/respond_to_confirmation",
        {"accept": True, "reason": "deterministic smoke approval"},
    )
    assert code == 200 and isinstance(response, dict) and response.get("success") is True, (
        code,
        response,
    )
    _wait_for(base_url, conversation_id, {"finished"})
    events = _events(base_url, conversation_id)
    actions = [
        item["action"].get("kind")
        for item in events
        if item.get("kind") == "ActionEvent" and isinstance(item.get("action"), dict)
    ]
    observations = [
        item["observation"].get("kind")
        for item in events
        if item.get("kind") == "ObservationEvent" and isinstance(item.get("observation"), dict)
    ]
    assert actions == ["TerminalAction", "FinishAction"], actions
    assert observations == ["TerminalObservation", "FinishObservation"], observations
    return conversation_id


def _condenser_smoke(base_url: str, fake_model_url: str, suffix: str) -> str:
    model = _llm(fake_model_url, "condenser-smoke")
    code, created = _request(
        base_url,
        "POST",
        "/api/conversations",
        {
            "workspace": {
                "kind": "LocalWorkspace",
                "working_dir": f"/runtime/workspace/project/condenser-{suffix}",
            },
            "max_iterations": 2,
            "agent": {
                "kind": "Agent",
                "llm": model,
                "tools": [],
                "condenser": {
                    "kind": "LLMSummarizingCondenser",
                    "llm": model,
                    "max_size": 6,
                    "keep_first": 1,
                },
            },
            "confirmation_policy": {"kind": "NeverConfirm"},
            "initial_message": {
                "role": "user",
                "content": [{"type": "text", "text": "history initial"}],
                "run": False,
            },
        },
    )
    assert code == 201 and isinstance(created, dict), (code, created)
    conversation_id = str(created["id"])
    for index in range(10):
        code, response = _request(
            base_url,
            "POST",
            f"/api/conversations/{conversation_id}/events",
            {
                "role": "user",
                "content": [{"type": "text", "text": f"history {index}"}],
                "run": False,
            },
        )
        assert code == 200 and isinstance(response, dict) and response.get("success") is True, (
            code,
            response,
        )

    code, response = _request(
        base_url,
        "POST",
        f"/api/conversations/{conversation_id}/condense",
        {},
        timeout=60,
    )
    assert code == 200 and isinstance(response, dict) and response.get("success") is True, (
        code,
        response,
    )
    deadline = time.monotonic() + 30
    condensations: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        condensations = [
            item
            for item in _events(base_url, conversation_id)
            if item.get("kind") in {"CondensationRequest", "Condensation"}
        ]
        if condensations and condensations[-1].get("kind") == "Condensation":
            break
        time.sleep(0.2)
    assert [item.get("kind") for item in condensations][-2:] == [
        "CondensationRequest",
        "Condensation",
    ], condensations
    completed = condensations[-1]
    assert completed.get("summary") == "OpenHands smoke conversation summary.", completed
    forgotten = completed.get("forgotten_event_ids")
    assert isinstance(forgotten, list) and forgotten, completed
    return conversation_id


def _native_task_smoke(base_url: str, fake_model_url: str, suffix: str) -> str:
    code, created = _request(
        base_url,
        "POST",
        "/api/conversations",
        {
            "workspace": {
                "kind": "LocalWorkspace",
                "working_dir": f"/runtime/workspace/project/native-task-{suffix}",
            },
            "max_iterations": 4,
            "agent": {
                "kind": "Agent",
                "llm": _llm(fake_model_url, "native-task-parent-smoke"),
                "tools": [{"name": "task_tool_set", "params": {}}],
                "condenser": {"kind": "NoOpCondenser"},
            },
            "agent_definitions": [
                {
                    "name": "smoke-reviewer",
                    "description": "Deterministic Task Tool smoke agent",
                    "model": "inherit",
                    "tools": ["terminal"],
                    "skills": [],
                    "system_prompt": "Run the requested smoke command and finish.",
                    "when_to_use_examples": [],
                    "permission_mode": "never_confirm",
                    "max_iteration_per_run": 3,
                    "max_budget_per_run": 1.0,
                    "condenser": {"kind": "NoOpCondenser"},
                    "metadata": {},
                }
            ],
            "confirmation_policy": {"kind": "NeverConfirm"},
            "initial_message": {
                "role": "user",
                "content": [{"type": "text", "text": "Delegate the smoke check."}],
                "run": True,
            },
        },
        timeout=60,
    )
    assert code == 201 and isinstance(created, dict), (code, created)
    conversation_id = str(created["id"])
    finished = _wait_for(base_url, conversation_id, {"finished"}, timeout=60)
    events = _events(base_url, conversation_id)
    actions = [
        item
        for item in events
        if item.get("kind") == "ActionEvent"
        and isinstance(item.get("action"), dict)
        and item["action"].get("kind") == "TaskAction"
    ]
    observations = [
        item
        for item in events
        if item.get("kind") == "ObservationEvent"
        and isinstance(item.get("observation"), dict)
        and item["observation"].get("kind") == "TaskObservation"
    ]
    assert len(actions) == 1, actions
    assert len(observations) == 1, observations
    action = actions[0]
    observation = observations[0]
    assert action.get("id") and action.get("tool_call_id"), action
    assert observation.get("action_id") == action["id"], observation
    assert observation.get("tool_call_id") == action["tool_call_id"], observation
    detail = observation["observation"]
    assert detail.get("status") == "completed" and detail.get("task_id"), detail
    assert detail.get("subagent") == "smoke-reviewer", detail
    stats = finished.get("stats")
    assert isinstance(stats, dict), finished
    usage_to_metrics = stats.get("usage_to_metrics")
    assert isinstance(usage_to_metrics, dict), stats
    task_metrics_key = f"task:{detail['task_id']}"
    assert task_metrics_key in usage_to_metrics, usage_to_metrics
    # The server publishes the child aggregate inside the parent Conversation;
    # There is no separate child Task stats endpoint in the pinned 1.44.0 API.
    task_metrics = usage_to_metrics[task_metrics_key]
    assert isinstance(task_metrics, dict), task_metrics
    token_usage = task_metrics.get("accumulated_token_usage")
    assert isinstance(token_usage, dict), task_metrics
    assert int(token_usage.get("prompt_tokens", 0)) > 0, task_metrics
    return conversation_id


def _oracle_smoke(base_url: str, fake_model_url: str, suffix: str) -> str:
    code, saved = _request(
        base_url,
        "POST",
        "/api/profiles/oracle",
        {
            "llm": _llm(fake_model_url, "flowweave-oracle-smoke-binding"),
            "include_secrets": True,
        },
    )
    assert code == 201 and isinstance(saved, dict) and saved.get("name") == "oracle", (
        code,
        saved,
    )
    code, created = _request(
        base_url,
        "POST",
        "/api/conversations",
        {
            "workspace": {
                "kind": "LocalWorkspace",
                "working_dir": f"/runtime/workspace/project/oracle-{suffix}",
            },
            "max_iterations": 4,
            "agent": {
                "kind": "Agent",
                "llm": _llm(fake_model_url, "oracle-parent-smoke"),
                "tools": [{"name": "ask_oracle", "params": {}}],
                "condenser": {"kind": "NoOpCondenser"},
            },
            "confirmation_policy": {"kind": "NeverConfirm"},
            "initial_message": {
                "role": "user",
                "content": [{"type": "text", "text": "Consult the Oracle once."}],
                "run": True,
            },
        },
        timeout=60,
    )
    assert code == 201 and isinstance(created, dict), (code, created)
    conversation_id = str(created["id"])
    finished = _wait_for(base_url, conversation_id, {"finished"}, timeout=60)
    observations = [
        item["observation"]
        for item in _events(base_url, conversation_id)
        if item.get("kind") == "ObservationEvent"
        and isinstance(item.get("observation"), dict)
        and item["observation"].get("kind") == "AskOracleObservation"
    ]
    assert len(observations) == 1, observations
    observation = observations[0]
    assert observation.get("is_error") is False, observation
    content = observation.get("content")
    assert isinstance(content, list) and any(
        isinstance(item, dict)
        and item.get("type") == "text"
        and "OpenHands smoke conversation summary." in str(item.get("text") or "")
        for item in content
    ), observation
    stats = finished.get("stats")
    assert isinstance(stats, dict), finished
    usage_to_metrics = stats.get("usage_to_metrics")
    assert isinstance(usage_to_metrics, dict), stats
    oracle_metrics = usage_to_metrics.get("oracle:oracle")
    assert isinstance(oracle_metrics, dict), usage_to_metrics
    token_usage = oracle_metrics.get("accumulated_token_usage")
    assert isinstance(token_usage, dict), oracle_metrics
    assert int(token_usage.get("prompt_tokens", 0)) > 0, oracle_metrics
    return conversation_id


def _cleanup(server: str, fake: str, network: str) -> None:
    subprocess.run(
        ["docker", "stop", server, fake],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["docker", "rm", server, fake],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["docker", "network", "rm", network],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _start_server(server: str, network: str, state_root: Path) -> str:
    _run(
        "docker",
        "run",
        "-d",
        "--name",
        server,
        "--network",
        network,
        "-p",
        "127.0.0.1::8000",
        "-e",
        f"SESSION_API_KEY={SESSION_KEY}",
        "-e",
        f"OH_SESSION_API_KEYS_0={SESSION_KEY}",
        "-e",
        f"OH_SECRET_KEY={SECRET_KEY}",
        "-e",
        "OH_WORKSPACE_PATH=/runtime/workspace/project",
        "-e",
        "OH_CONVERSATIONS_PATH=/runtime/state/conversations",
        "-e",
        "OH_BASH_EVENTS_DIR=/runtime/state/bash-events",
        "-e",
        "OH_PERSISTENCE_DIR=/runtime/state/persistence",
        "-e",
        "HOME=/home/flowweave",
        "--mount",
        f"type=bind,src={state_root / 'workspace'},dst=/runtime/workspace/project",
        "--mount",
        f"type=bind,src={state_root / 'conversations'},dst=/runtime/state/conversations",
        "--mount",
        f"type=bind,src={state_root / 'bash-events'},dst=/runtime/state/bash-events",
        "--mount",
        f"type=bind,src={state_root / 'persistence'},dst=/runtime/state/persistence",
        "--mount",
        f"type=bind,src={state_root / 'home'},dst=/home/flowweave",
        "--mount",
        (
            f"type=bind,src={state_root / 'persistence/profiles'},"
            "dst=/home/flowweave/.openhands/profiles"
        ),
        IMAGE,
    )
    port_line = _run("docker", "port", server, "8000/tcp", capture=True).splitlines()[0]
    base_url = f"http://127.0.0.1:{port_line.rsplit(':', 1)[1]}"
    deadline = time.monotonic() + 60
    while True:
        try:
            code, _ = _request(base_url, "GET", "/openapi.json", timeout=2)
            if code == 200:
                return base_url
        except (OSError, TimeoutError, urllib.error.URLError):
            pass
        if time.monotonic() >= deadline:
            logs = _run("docker", "logs", server, capture=True)
            raise AssertionError(f"OpenHands Agent Server did not become ready:\n{logs[-4000:]}")
        time.sleep(0.5)


def _event_ids(base_url: str, conversation_id: str) -> tuple[str, ...]:
    result = tuple(str(item["id"]) for item in _events(base_url, conversation_id) if item.get("id"))
    assert result, conversation_id
    return result


def _assert_persisted_conversations(state_root: Path, conversation_ids: tuple[str, ...]) -> None:
    for conversation_id in conversation_ids:
        conversation_dir = state_root / "conversations" / conversation_id.replace("-", "")
        for filename in ("meta.json", "base_state.json"):
            path = conversation_dir / filename
            assert path.is_file() and path.stat().st_size > 0, path


def _catalog_ids(base_url: str) -> set[str]:
    code, value = _request(base_url, "GET", "/api/conversations/search?limit=100")
    assert code == 200 and isinstance(value, dict), (code, value)
    items = value.get("items")
    assert isinstance(items, list), value
    return {str(item["id"]) for item in items if isinstance(item, dict) and item.get("id")}


def main() -> None:
    suffix = uuid4().hex[:10]
    network = f"flowweave-oh-smoke-{suffix}"
    fake = f"flowweave-fake-llm-{suffix}"
    server = f"flowweave-oh-server-{suffix}"
    with tempfile.TemporaryDirectory(prefix="flowweave-openhands-smoke-") as state_dir:
        state_root = Path(state_dir)
        for name in ("workspace", "conversations", "bash-events", "persistence", "home"):
            (state_root / name).mkdir()
        (state_root / "persistence/profiles").mkdir()
        (state_root / "home/.openhands").mkdir()
        try:
            _run("docker", "network", "create", network)
            _run(
                "docker",
                "run",
                "-d",
                "--name",
                fake,
                "--network",
                network,
                "--entrypoint",
                "/runtime/.venv/bin/python",
                "-v",
                f"{FAKE_MODEL}:/smoke/fake.py:ro",
                IMAGE,
                "/smoke/fake.py",
            )
            base_url = _start_server(server, network, state_root)
            versions = _run(
                "docker",
                "exec",
                server,
                "/runtime/.venv/bin/python",
                "-c",
                (
                    "from importlib.metadata import version; "
                    "names=('openhands-agent-server','openhands-sdk','openhands-tools',"
                    "'openhands-workspace'); print(','.join(version(n) for n in names))"
                ),
                capture=True,
            )
            assert versions.split(",") == [EXPECTED_VERSION] * 4, versions
            fake_model_url = f"http://{fake}:18080/v1"
            confirmation_id = _confirmation_smoke(base_url, fake_model_url, suffix)
            condenser_id = _condenser_smoke(base_url, fake_model_url, suffix)
            native_task_id = _native_task_smoke(base_url, fake_model_url, suffix)
            oracle_id = _oracle_smoke(base_url, fake_model_url, suffix)
            conversation_ids = (confirmation_id, condenser_id, native_task_id, oracle_id)
            original_event_ids = {item: _event_ids(base_url, item) for item in conversation_ids}
            _assert_persisted_conversations(state_root, conversation_ids)

            code, response = _request(
                base_url,
                "POST",
                "/api/conversations/prepare-for-sandbox-pause",
                {},
            )
            assert code in {200, 204}, (code, response)
            _run("docker", "rm", "-f", server)
            base_url = _start_server(server, network, state_root)
            assert set(conversation_ids) <= _catalog_ids(base_url)
            reloaded_event_ids = {item: _event_ids(base_url, item) for item in conversation_ids}
            assert reloaded_event_ids == original_event_ids, (
                original_event_ids,
                reloaded_event_ids,
            )

            print(
                json.dumps(
                    {
                        "status": "ok",
                        "openhands_version": EXPECTED_VERSION,
                        "confirmation_conversation_id": confirmation_id,
                        "condenser_conversation_id": condenser_id,
                        "native_task_conversation_id": native_task_id,
                        "oracle_conversation_id": oracle_id,
                        "original_id_reload": True,
                    },
                    sort_keys=True,
                )
            )
        finally:
            _cleanup(server, fake, network)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"OpenHands smoke failed: {exc}", file=sys.stderr)
        raise
