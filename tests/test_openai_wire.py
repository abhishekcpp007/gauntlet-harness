"""Drive OpenAIPolicy through the real openai SDK against a local OpenAI-compatible server.

The scripted-client tests in test_policies.py check the mapping. This one checks the wire:
that the request really carries a system prompt and tool definitions, that tool calls come
back through the SDK as the policy expects, and that tool results are posted by id. It is
what backs the claim that pointing OPENAI_BASE_URL at vLLM or Ollama works.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

openai = pytest.importorskip("openai")

from gauntlet_harness.policies import make_policy  # noqa: E402

SCRIPT = [
    {"tool": ("bash", '{"command": "ls -la"}')},
    {"tool": ("edit", '{"command": "str_replace", "path": "/repo/a.py", "old_str": "x", "new_str": "y"}')},
    {"content": "Replaced x with y in a.py."},
]


class _Server(HTTPServer):
    seen: list[dict] = []
    step = 0


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep pytest output clean
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append(body)
        step = SCRIPT[min(self.server.step, len(SCRIPT) - 1)]
        self.server.step += 1
        if "tool" in step:
            name, arguments = step["tool"]
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{self.server.step}",
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    }
                ],
            }
        else:
            message = {"role": "assistant", "content": step["content"]}
        payload = json.dumps(
            {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def server():
    srv = _Server(("127.0.0.1", 0), _Handler)
    srv.seen, srv.step = [], 0
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


class WireEnv:
    def __init__(self):
        self.actions = []

    def reset(self):
        return "fix the bug in a.py", {}

    def step(self, action):
        self.actions.append(action)
        if action["type"] == "submit":
            return "submitted", 1.0, True, False, {}
        return f"ok: {action['type']}", 0.0, False, False, {"error": False}


def test_openai_policy_over_real_http(server, monkeypatch):
    host, port = server.server_address
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://{host}:{port}/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "not-needed")

    policy = make_policy("openai:Qwen/Qwen3-32B")
    env = WireEnv()
    policy.run(env)

    assert policy.name == "openai:Qwen/Qwen3-32B"
    assert env.actions == [
        {"type": "bash", "command": "ls -la"},
        {"type": "edit", "command": "str_replace", "path": "/repo/a.py", "old_str": "x", "new_str": "y"},
        {"type": "submit"},
    ]
    assert policy.usage == {"input_tokens": 36, "output_tokens": 12}

    first = server.seen[0]
    assert first["model"] == "Qwen/Qwen3-32B"
    assert first["messages"][0]["role"] == "system"
    assert {t["function"]["name"] for t in first["tools"]} == {"bash", "edit"}

    # the second request has to carry the assistant turn and the tool result, by id
    second = server.seen[1]
    assert second["messages"][-2]["tool_calls"][0]["id"] == "call_1"
    assert second["messages"][-1] == {"role": "tool", "tool_call_id": "call_1", "content": "ok: bash"}
