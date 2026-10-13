"""A local Anthropic Messages endpoint for tests (agent/test/support/fakeAnthropic.ts).

Claude Code is pointed at it as a gateway (``ANTHROPIC_BASE_URL`` and a token), so no
test calls Anthropic. ``reply(body)`` scripts each ``/v1/messages`` answer: a text,
or one tool_use. Streaming requests get the SSE event sequence; others JSON.
"""

from __future__ import annotations

import itertools
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Reply = dict[str, Any]  # {"text": str} or {"tool_use": {"name": str, "input": dict}}

_ids = itertools.count(1)


def _sse(event: str, data: Any) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


@dataclass
class FakeAnthropic:
    reply: Callable[[dict[str, Any]], Reply]
    requests: list[dict[str, Any]] = field(default_factory=list)
    server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def messages(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r.get("_path") == "/v1/messages"]

    def start(self) -> FakeAnthropic:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                pass

            def _json(self, body: Any) -> None:
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self._json({"data": [], "has_more": False, "first_id": None, "last_id": None})

            def do_POST(self) -> None:
                path = self.path.split("?")[0]
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                try:
                    body = json.loads(raw) if raw else {}
                except ValueError:
                    body = {}
                fake.requests.append({**body, "_path": path})
                if path == "/v1/messages/count_tokens":
                    self._json({"input_tokens": 10})
                    return
                answer = fake.reply(body)
                if "text" in answer:
                    block: dict[str, Any] = {"type": "text", "text": answer["text"]}
                    stop = "end_turn"
                else:
                    use = answer["tool_use"]
                    block = {
                        "type": "tool_use",
                        "id": f"toolu_{next(_ids):06d}",
                        "name": use["name"],
                        "input": use["input"],
                    }
                    stop = "tool_use"
                usage = {"input_tokens": 10, "output_tokens": 5}
                model = body.get("model", "claude-fake")
                if not body.get("stream"):
                    self._json(
                        {
                            "id": f"msg_{next(_ids):06d}",
                            "type": "message",
                            "role": "assistant",
                            "model": model,
                            "content": [block],
                            "stop_reason": stop,
                            "stop_sequence": None,
                            "usage": usage,
                        }
                    )
                    return
                start = (
                    {"type": "text", "text": ""}
                    if block["type"] == "text"
                    else {**block, "input": {}}
                )
                delta = (
                    {"type": "text_delta", "text": block["text"]}
                    if block["type"] == "text"
                    else {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
                )
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("cache-control", "no-cache")
                self.end_headers()
                message = {
                    "id": f"msg_{next(_ids):06d}",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {**usage, "output_tokens": 1},
                }
                for chunk in (
                    _sse("message_start", {"type": "message_start", "message": message}),
                    _sse(
                        "content_block_start",
                        {"type": "content_block_start", "index": 0, "content_block": start},
                    ),
                    _sse(
                        "content_block_delta",
                        {"type": "content_block_delta", "index": 0, "delta": delta},
                    ),
                    _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    _sse(
                        "message_delta",
                        {
                            "type": "message_delta",
                            "delta": {"stop_reason": stop, "stop_sequence": None},
                            "usage": {"output_tokens": 5},
                        },
                    ),
                    _sse("message_stop", {"type": "message_stop"}),
                ):
                    self.wfile.write(chunk)
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def close(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
