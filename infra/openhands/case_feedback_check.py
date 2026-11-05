"""Installed locked-image Case message append/run/reload contract probe.

Run without network, host workspace mounts or real model calls. Only this test
process replaces LLM responses; installed source remains untouched.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from read_capacity_check import request, require, server_environment, wait_status


def check() -> dict[str, object]:
    from litellm.types.utils import Choices, ModelResponse
    from litellm.types.utils import Message as LiteLLMMessage
    from openhands.sdk import LLM, Message
    from openhands.sdk.llm.llm_response import LLMResponse

    def completion(self, messages, tools=None, **kwargs):
        message = LiteLLMMessage.model_validate(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "case-feedback-finish",
                        "type": "function",
                        "function": {
                            "name": "finish",
                            "arguments": json.dumps({"message": "facts reviewed"}),
                        },
                    }
                ],
            }
        )
        return LLMResponse(
            message=Message.from_llm_chat_message(message),
            metrics=self.metrics.get_snapshot(),
            raw_response=ModelResponse(
                id="case-feedback-response",
                created=int(time.time()),
                model="test-model",
                choices=[Choices(index=0, finish_reason="tool_calls", message=message)],
            ),
        )

    async def acompletion(self, messages, tools=None, **kwargs):
        return completion(self, messages, tools, **kwargs)

    def user_ids(client, root):
        page = request(
            client,
            "GET",
            f"{root}/events/search",
            params={
                "kind": "openhands.sdk.event.llm_convertible.message.MessageEvent",
                "source": "user",
                "sort_order": "TIMESTAMP_DESC",
                "limit": 100,
            },
        ).json()
        return {item["id"] for item in page["items"]}

    with (
        tempfile.TemporaryDirectory(prefix="case05b-") as temporary,
        patch.object(LLM, "completion", completion),
        patch.object(LLM, "acompletion", acompletion),
    ):
        directory = Path(temporary)
        with server_environment(directory) as (_, client, workspace):
            created = request(
                client,
                "POST",
                "/api/conversations",
                json={
                    "agent": {
                        "llm": {"model": "gpt-4o-mini", "api_key": "no-model-calls"},
                        "tools": [],
                    },
                    "workspace": {"working_dir": str(workspace)},
                    "autotitle": False,
                },
            ).json()
            root = f"/api/conversations/{created['id']}"
            before = user_ids(client, root)
            response = request(
                client,
                "POST",
                f"{root}/events",
                json={
                    "role": "user",
                    "content": [{"type": "text", "text": "case source facts"}],
                    "run": False,
                },
            ).json()
            require(
                not response.get("id") and not response.get("event_id"),
                "Expected success-only native contract",
            )
            added = user_ids(client, root) - before
            require(len(added) == 1, "Native user event identity is not unique")
            event_id = next(iter(added))
            event = request(client, "GET", f"{root}/events/{event_id}").json()
            require(
                event["kind"] == "MessageEvent" and event["source"] == "user",
                "Wrong native event",
            )
            state = request(client, "GET", root).json()
            require(
                state["execution_status"] == "idle"
                and state["leaf_event_id"] == event_id,
                "Append unexpectedly ran",
            )
            request(client, "POST", f"{root}/run")
            wait_status(client, root, "finished")
            require(
                user_ids(client, root) - before == {event_id},
                "Run appended another user event",
            )
            # The next finished -> append transition resets to native idle.
            response = request(
                client,
                "POST",
                f"{root}/events",
                json={
                    "role": "user",
                    "content": [{"type": "text", "text": "case newer facts"}],
                    "run": False,
                },
            )
            second = user_ids(client, root) - before - {event_id}
            require(len(second) == 1, "Second append lost native identity")
            pending_id = next(iter(second))
            wait_status(client, root, "idle")
            pending = request(client, "GET", f"{root}/events/{pending_id}").json()
            request(
                client, "POST", "/api/conversations/prepare-for-sandbox-pause", json={}
            )
        # Same persisted native ID and message receipt survive service reload;
        # recovery invokes only /run and never repeats the append.
        with server_environment(directory) as (_, client, _):
            state = request(client, "GET", root).json()
            require(
                state["leaf_event_id"] == pending_id, "Reload lost native pending head"
            )
            require(
                request(client, "GET", f"{root}/events/{pending_id}").json() == pending,
                "Reload changed formal event",
            )
            request(client, "POST", f"{root}/run")
            wait_status(client, root, "finished")
            require(
                user_ids(client, root) - before == {event_id, pending_id},
                "Recovery duplicated user facts",
            )
    return {
        "append_run_false": True,
        "success_only_response": True,
        "formal_user_identity_delta": True,
        "separate_run": True,
        "finished_append_idle": True,
        "pending_receipt_reload": True,
        "duplicate_user_messages": 0,
        "external_model_calls": 0,
    }


if __name__ == "__main__":
    print(json.dumps(check(), sort_keys=True))
