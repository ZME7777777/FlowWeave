"""Opt-in real TCP proof using an independently installed, frozen native server."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr

from flowweave.bootstrap.settings import Settings
from flowweave.runtime.base import RuntimeHandle
from flowweave.runtime.child_contract import V3_ARCHIVE_SHA256, V3_SOURCE_COMMIT
from flowweave.runtime.child_creation import NativeChildLaunchRequest, verify_child_configuration
from flowweave.runtime.child_governance import (
    NativeChildPreparationOverrides,
    NativeChildStaticSecret,
)
from flowweave.runtime.child_governance_contract import V4_SOURCE_COMMIT
from flowweave.runtime.openhands import OpenHandsRuntime
from flowweave.shared.errors import DomainError


@pytest.mark.parametrize("profile", ["OH_FW_V3", "OH_FW_V4"])
def test_child_creation_adapter_against_frozen_native_tcp(monkeypatch, profile):
    python = os.environ.get(
        "FLOWWEAVE_NATIVE_CHILD_V3_PYTHON"
        if profile == "OH_FW_V3"
        else "FLOWWEAVE_NATIVE_CHILD_PYTHON"
    )
    if not python:
        pytest.skip("Set FLOWWEAVE_NATIVE_CHILD_PYTHON to a frozen four-package environment")
    repo = Path(__file__).resolve().parents[3]
    root = repo / ".tmp/fr721/native"
    root.mkdir(parents=True, exist_ok=True)
    state = Path(tempfile.mkdtemp(dir=root))
    state.chmod(0o700)
    for directory in ("home", "tmp", "cache"):
        (state / directory).mkdir()
    env = {name: os.environ[name] for name in ("PATH", "LANG", "SYSTEMROOT") if name in os.environ}
    env.update(
        HOME=str(state / "home"),
        TMPDIR=str(state / "tmp"),
        XDG_CACHE_HOME=str(state / "cache"),
        LITELLM_LOCAL_MODEL_COST_MAP="True",
    )
    expected_source = V3_SOURCE_COMMIT if profile == "OH_FW_V3" else V4_SOURCE_COMMIT
    lock = json.loads((repo / "infra/openhands/source.lock.json").read_text())
    if profile == "OH_FW_V3":
        lock.update(
            source_commit=V3_SOURCE_COMMIT,
            fork_commit=V3_SOURCE_COMMIT,
            archive_sha256=V3_ARCHIVE_SHA256,
        )
    lock_path = state / "source.lock.json"
    lock_path.write_text(json.dumps(lock))
    env["FLOWWEAVE_NATIVE_CHILD_LOCK"] = str(lock_path)
    script = Path(__file__).parent / "fixtures/native_child_server.py"
    runtime = OpenHandsRuntime(Settings(workspace_root=state))
    with (state / "server.log").open("w") as log:
        process = subprocess.Popen(
            [str(Path(python).absolute()), str(script), str(state), str(repo)],
            cwd=state,
            env=env,
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=log,
            text=True,
        )
        try:
            ready_file = state / "ready.json"
            deadline = time.monotonic() + 50
            while not ready_file.exists():
                assert process.poll() is None, (
                    f"Native fixture failed; inspect {state / 'server.log'}"
                )
                assert time.monotonic() < deadline, "Native fixture startup timeout"
                time.sleep(0.05)
            ready = json.loads(ready_file.read_text())
            assert ready["source_commit"] == expected_source
            assert ready["verified_python_files"] >= 500
            monkeypatch.setattr(runtime, "_base_url_for_handle", lambda _: ready["host"])
            monkeypatch.setattr(runtime, "_session_key_for_handle", lambda _: ready["runtime_key"])
            handle = RuntimeHandle(
                "agent-workspace:native",
                ready["parent"],
                protocol_profile=profile,
                workspace_root=ready["workspace"],
            )
            request = NativeChildLaunchRequest(
                task="review child 🌳", title="child", token=SecretStr(ready["token"])
            )
            creation = UUID(ready["creation"])
            invocation = runtime.verify_child_launcher(handle, request)
            assert invocation.policy.parent_conversation_id == UUID(handle.conversation_id)
            assert not hasattr(invocation, "creation_id")
            assert runtime.read_child_preparation(handle, uuid4()) is None
            if profile == "OH_FW_V4":
                resolved = runtime.resolve_child_launcher(handle, request)
                assert resolved.invocation == invocation
                assert resolved.identity.creation_id == creation
                overrides = NativeChildPreparationOverrides(
                    governance_id=uuid4(),
                    replace_secrets={
                        "QA_CHILD": NativeChildStaticSecret(value=SecretStr("child-only"))
                    },
                    remove_secrets=frozenset({"QA_PARENT"}),
                )
                prepared = runtime.prepare_governed_child_conversation(
                    handle, creation, request, overrides
                )
                assert prepared.governance_id == overrides.governance_id
                assert (
                    runtime.prepare_governed_child_conversation(
                        handle, creation, request, overrides
                    )
                    == prepared
                )
                assert prepared.receipt.identity == resolved.identity
            else:
                prepared = runtime.prepare_child_conversation(handle, creation, request)
                assert runtime.prepare_child_conversation(handle, creation, request) == prepared
            assert prepared.phase == "prepared"
            assert runtime.read_child_preparation(handle, creation) == prepared
            child = replace(handle, conversation_id=str(prepared.receipt.identity.conversation_id))
            configuration = runtime.child_conversation_configuration(child)
            assert configuration.agent["tools"] == []  # launcher stripped natively
            with httpx.Client(
                base_url=ready["host"],
                headers={"X-Session-API-Key": ready["runtime_key"]},
                timeout=10,
                trust_env=False,
            ) as client:
                parent_info = client.get(
                    f"/api/conversations/{handle.conversation_id}",
                    params={"include_skills": "true"},
                )
                parent_info.raise_for_status()
                expected_agent = parent_info.json()["agent"]
                expected_agent["tools"] = []  # formal governed child inheritance
                verify_child_configuration(
                    configuration,
                    expected=prepared.receipt.identity,
                    working_directory=ready["workspace"],
                    expected_agent=expected_agent,
                )
                child_root = f"/api/conversations/{child.conversation_id}"
                if profile == "OH_FW_V4":
                    registry = client.get(child_root).json()["secret_registry"]["secret_sources"]
                    assert "QA_CHILD" in registry and "QA_PARENT" not in registry
                assert client.post(child_root + "/run").status_code == 409
                before = client.get(child_root + "/events/search").json()["items"]
                assert not [e for e in before if e["kind"] == "MessageEvent"]
                assert not (Path(ready["workspace"]) / ".git").exists()
                assert (
                    runtime.release_child_conversation(handle, creation, request).phase == "started"
                )
                deadline = time.monotonic() + 10
                while client.get(child_root).json()["execution_status"] != "finished":
                    assert time.monotonic() < deadline, "Native child did not finish"
                    time.sleep(0.05)
                assert (
                    runtime.release_child_conversation(handle, creation, request).phase == "started"
                )
                after = client.get(child_root + "/events/search").json()["items"]
                assert (
                    len([e for e in after if e["kind"] == "MessageEvent" and e["source"] == "user"])
                    == 1
                )
                assert (
                    len(
                        [e for e in after if e["kind"] == "MessageEvent" and e["source"] == "agent"]
                    )
                    == 1
                )
            with pytest.raises(DomainError) as denied:
                runtime.verify_child_launcher(
                    handle, request.model_copy(update={"token": SecretStr("forged")})
                )
            assert denied.value.status == 403
        finally:
            asyncio.run(runtime.aclose())
            if process.stdin:
                try:
                    process.stdin.write("stop\n")
                    process.stdin.close()
                except BrokenPipeError:
                    pass
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=10)
            assert process.returncode == 0, f"Native fixture failed; inspect {state / 'server.log'}"
