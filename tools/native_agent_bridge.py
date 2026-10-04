#!/usr/bin/env python3
"""Local, opt-in bridge for testing a real external model agent without an API key.

This is a test harness, not a production LLM provider. For every OpenAI chat
completion request it atomically writes `<id>.request.json`. The cooperating
model reads the actual messages/tool declarations, chooses its reply, and
atomically writes `<id>.response.json` (an assistant message with `content` and
optional OpenAI `tool_calls`). The bridge returns that choice unchanged.

No generated answer or geometry is baked in. Subsequent requests contain the
real tool/Gate results. Keep the spool private: it contains conversation data.
Bind is loopback-only. Stop this process when the test agent finishes.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
import uuid


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # stopped browser/turn; its response must not feed a later turn

    def do_GET(self):
        if self.path.rstrip("/") == "/v1/models":
            self.send_json(200, {"object": "list", "data": [{
                "id": "native-agent-live-test", "object": "model", "owned_by": "local-test-harness"}]})
        else:
            self.send_json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            return self.send_json(404, {"error": {"message": "not found"}})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16 * 1024 * 1024:
                raise ValueError("invalid request size")
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict) or not isinstance(request.get("messages"), list):
                raise ValueError("messages must be an array")
        except (ValueError, TypeError):
            return self.send_json(400, {"error": {"message": "invalid completion request"}})
        with self.server.counter_lock:
            self.server.counter += 1
            request_id = f"{self.server.counter:04d}-{uuid.uuid4().hex[:8]}"
        root = self.server.spool
        target = root / f"{request_id}.request.json"
        temp = root / f"{request_id}.tmp"
        temp.write_text(json.dumps(request, ensure_ascii=False, indent=2))
        temp.replace(target)
        response = root / f"{request_id}.response.json"
        deadline = time.monotonic() + self.server.response_timeout
        while not response.exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        if not response.exists():
            (root / f"{request_id}.expired").touch()
            return self.send_json(504, {"error": {"message": "Native test agent did not respond; configure a real provider for normal use."}})
        try:
            message = json.loads(response.read_text())
            if not isinstance(message, dict) or message.get("role", "assistant") != "assistant":
                raise ValueError("expected assistant message")
            message["role"] = "assistant"
            if "content" not in message and not message.get("tool_calls"):
                raise ValueError("empty assistant message")
            calls = message.get("tool_calls", [])
            if not isinstance(calls, list):
                raise ValueError("tool_calls must be an array")
            for call in calls:
                if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                    raise ValueError("invalid tool call")
                if call.get("type") != "function" or not isinstance(call["function"].get("arguments"), str):
                    raise ValueError("tool calls require OpenAI function/arguments format")
        except (ValueError, TypeError):
            return self.send_json(502, {"error": {"message": "invalid native agent response"}})
        self.send_json(200, {
            "id": f"chatcmpl-{request_id}", "object": "chat.completion",
            "created": int(time.time()), "model": "native-agent-live-test",
            "choices": [{"index": 0, "message": message,
                         "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
            # Deliberately omit usage: the bridge cannot measure the agent's
            # token consumption. Reporting invented token counts is misleading.
        })

    def log_message(self, *_args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--spool", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8124)
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    args.spool.mkdir(parents=True, exist_ok=True, mode=0o700)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.spool = args.spool
    server.counter = 0
    server.counter_lock = threading.Lock()
    server.response_timeout = args.timeout
    print(f"Native model test bridge at http://127.0.0.1:{args.port}/v1", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
