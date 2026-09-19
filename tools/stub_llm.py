#!/usr/bin/env python3
"""An offline stand-in for a model provider.

Why this exists
---------------
What the harness actually promises a model is: a guessable tool surface, an
actionable failure when something goes wrong, and a Gate that is the only judge
of "done". Verifying those needs *a* model, not a specific one — and requiring an
API key to exercise the chat path makes it untestable offline, unreproducible
when a turn misbehaves, and impossible to run in CI.

This speaks just enough OpenAI protocol for :class:`tcad.llm.client.OpenAIClient`:

    POST /v1/chat/completions   one scripted assistant reply per request
    GET  /v1/models             a single synthetic model

It is deliberately dumb — no inspection of the conversation. The point is to
drive the *harness*, not to be clever.

Usage
-----
    python tools/stub_llm.py --script tools/sessions/demo_bracket.json --port 8123
    .venv/bin/python tools/serve.py --base-url http://127.0.0.1:8123/v1

Each request consumes the next reply. When the script runs out, the stub answers
with plain text and no tool calls, which ends the turn the way a finished model
would.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def load_script(paths: list[Path]) -> list[dict]:
    """Read one or more script files into a flat list of assistant replies."""
    replies: list[dict] = []
    for path in paths:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list):               # a bare session file (calls only)
            replies.append({"text": "", "calls": raw})
            continue
        if "replies" in raw:
            replies.extend(
                {"text": r.get("text", ""), "calls": r.get("calls", [])}
                for r in raw["replies"]
            )
            continue
        raise ValueError(f"{path}: expected a list or an object with 'replies'")
    return replies


class Scripted:
    """Thread-safe cursor over the scripted replies."""

    def __init__(self, replies: list[dict], *, loop: bool = False) -> None:
        self._replies = replies
        self._loop = loop
        self._lock = threading.Lock()
        self._index = 0
        self.served: list[str] = []

    def next(self, model: str) -> dict:
        with self._lock:
            if self._index < len(self._replies):
                reply = self._replies[self._index]
            elif self._loop and self._replies:
                reply = self._replies[self._index % len(self._replies)]
            else:
                reply = {"text": "（脚本已播完）", "calls": []}
            self._index += 1
            ordinal = self._index
        return _completion(model, reply, ordinal)


def _completion(model: str, reply: dict, ordinal: int) -> dict:
    tool_calls = []
    for i, call in enumerate(reply.get("calls") or []):
        tool_calls.append(
            {
                "id": f"call_{ordinal}_{i}",
                "type": "function",
                "function": {
                    "name": call["name"],
                    # the OpenAI wire format carries arguments as a JSON string
                    "arguments": json.dumps(call.get("args", {}), ensure_ascii=False),
                },
            }
        )
    message: dict = {"role": "assistant", "content": reply.get("text", "") or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": f"chatcmpl-stub-{ordinal}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {"prompt_tokens": 512, "completion_tokens": 128, "total_tokens": 640},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "tcad-stub-llm"
    protocol_version = "HTTP/1.1"

    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.rstrip("/")
        if path.endswith("/models"):
            self._send({
                "object": "list",
                "data": [{
                    "id": self.server.model_name,  # type: ignore[attr-defined]
                    "object": "model",
                    "created": 0,
                    "owned_by": "stub",
                }],
            })
            return
        self._send({"error": {"message": f"no route for {self.path}"}}, 404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            request = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send({"error": {"message": "bad request body"}}, 400)
            return

        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send({"error": {"message": f"no route for {self.path}"}}, 404)
            return

        model = request.get("model") or self.server.model_name  # type: ignore[attr-defined]
        reply = self.server.script.next(model)  # type: ignore[attr-defined]
        self.server.script.served.append(  # type: ignore[attr-defined]
            ",".join(tc["function"]["name"] for tc in reply["choices"][0]["message"].get("tool_calls", []))
        )
        self._send(reply)

    def log_message(self, *args) -> None:  # keep the demo output readable
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--script", action="append", required=True,
                        help="script file; repeat to concatenate sessions in order")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--model-name", default="stub-scripted")
    parser.add_argument("--loop", action="store_true", help="restart the script when exhausted")
    args = parser.parse_args()

    replies = load_script([Path(p) for p in args.script])
    if not replies:
        parser.error("the script contains no replies")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.script = Scripted(replies, loop=args.loop)   # type: ignore[attr-defined]
    server.model_name = args.model_name                 # type: ignore[attr-defined]

    print(f"stub model  ->  http://{args.host}:{args.port}/v1")
    print(f"model name  ->  {args.model_name}")
    print(f"scripted    ->  {len(replies)} replies "
          f"({sum(len(r.get('calls') or []) for r in replies)} tool calls)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
