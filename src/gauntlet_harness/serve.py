"""Expose one SWEEnv over HTTP so any agent, in any language, can drive it.

    gauntlet serve --tasks tasks.jsonl --task <instance_id> --policy my-agent -o runs/

    POST /reset                      -> {"obs", "info"}
    POST /step   {"type": "bash", ...} -> {"obs", "reward", "terminated", "truncated", "info"}
    GET  /health

The episode lands in the same places as `gauntlet run`: results.jsonl, rl/ and trajectories/.
Binds to localhost only. The env runs commands in a sandbox, but the server itself has no auth.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .env import SWEEnv
from .runner import episode_id
from .schema import HarnessTask


class EnvServer:
    def __init__(
        self,
        task: HarnessTask,
        *,
        policy: str,
        out_dir: Path,
        env_factory: Callable[..., Any] = SWEEnv,
        **env_kwargs: Any,
    ) -> None:
        self.task, self.policy, self.out_dir = task, policy, out_dir
        self.env_factory, self.env_kwargs = env_factory, env_kwargs
        self.env: Any = None
        self.attempt = -1
        self.transcript: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        for sub in ("trajectories", "rl"):
            (out_dir / sub).mkdir(parents=True, exist_ok=True)

    def reset(self) -> dict[str, Any]:
        with self.lock:
            if self.env is not None:
                self.env.close()
            self.attempt += 1
            eid = episode_id(self.task, self.policy, self.attempt)
            self.env = self.env_factory(
                self.task, trajectory_path=self.out_dir / "trajectories" / f"{eid}.jsonl", **self.env_kwargs
            )
            obs, info = self.env.reset()
            self.transcript = [{"role": "user", "content": obs}]
            return {"obs": obs, "info": info, "episode_id": eid}

    def step(self, action: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.env is None:
                raise RuntimeError("POST /reset first")
            obs, reward, terminated, truncated, info = self.env.step(action)
            self.transcript.append({"role": "assistant", "content": {"action": action}})
            self.transcript.append({"role": "tool", "content": obs})
            out: dict[str, Any] = {
                "obs": obs,
                "reward": reward,
                "terminated": terminated,
                "truncated": truncated,
                "info": {k: v for k, v in info.items() if k != "result"},
            }
            if "result" in info:
                out["result"] = self._record(info["result"])
            return out

    def _record(self, result: Any) -> dict[str, Any]:
        eid = episode_id(self.task, self.policy, self.attempt)
        result.episode_id, result.policy, result.attempt = eid, self.policy, self.attempt
        rl_path = self.out_dir / "rl" / f"{eid}.json"
        rl_path.write_text(
            json.dumps(
                {
                    "episode_id": eid,
                    "instance_id": self.task.instance_id,
                    "policy": self.policy,
                    "messages": self.transcript,
                    "actions": self.env.actions,
                    "prompt": getattr(self.env, "prompt", ""),
                    "steps": [
                        {"action": a, "obs": o}
                        for a, o in zip(self.env.actions, getattr(self.env, "observations", []), strict=False)
                    ],
                    "reward": result.reward,
                    "reward_breakdown": result.reward_breakdown,
                    "resolved": bool(result.grade and result.grade.resolved),
                },
                indent=1,
                default=str,
            )
        )
        result.rl_path = str(rl_path)
        with (self.out_dir / "results.jsonl").open("a") as f:
            f.write(result.model_dump_json() + "\n")
        return result.model_dump(exclude={"patch"})

    def close(self) -> None:
        if self.env is not None:
            self.env.close()


def make_handler(server: EnvServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict[str, Any]) -> None:
            data = json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._send(200, {"ok": True, "task": server.task.instance_id, "policy": server.policy})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            try:
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/reset":
                    self._send(200, server.reset())
                elif self.path == "/step":
                    self._send(200, server.step(body))
                else:
                    self._send(404, {"error": "not found"})
            except (RuntimeError, ValueError) as e:
                self._send(400, {"error": str(e)})

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

    return Handler


def serve(server: EnvServer, *, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(server))
