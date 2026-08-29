"""Deterministic OpenAI-compatible model used by the real OpenHands smoke test."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _write(self, value: object) -> None:
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._write({"data": [{"id": "fake-model", "object": "model"}]})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length) or b"{}")
        if not request.get("tools"):
            self._write(
                {
                    "id": "chatcmpl-condensation-smoke",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "fake-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "OpenHands smoke conversation summary.",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }
            )
            return
        tool_names = {
            str(tool.get("function", {}).get("name"))
            for tool in request.get("tools", [])
            if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
        }
        has_tool_result = any(
            message.get("role") == "tool" for message in request.get("messages", [])
        )
        if has_tool_result:
            name = "finish"
            arguments = {"message": "smoke complete"}
            call_id = "finish-call-1"
        elif "task" in tool_names:
            name = "task"
            arguments = {
                "description": "verify native task",
                "prompt": "Run the deterministic smoke command and report completion.",
                "subagent_type": "smoke-reviewer",
            }
            call_id = "task-call-1"
        elif "ask_oracle" in tool_names:
            name = "ask_oracle"
            arguments = {
                "question": "Is the governed Oracle profile reachable?",
                "context": "Return the deterministic smoke response.",
            }
            call_id = "oracle-call-1"
        else:
            name = "terminal"
            arguments = {"command": "printf smoke"}
            call_id = "terminal-call-1"
        self._write(
            {
                "id": "chatcmpl-smoke",
                "object": "chat.completion",
                "created": 1,
                "model": "fake-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": call_id,
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(arguments),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        )

    def log_message(self, _format: str, *args: object) -> None:
        del args


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 18080), Handler).serve_forever()
