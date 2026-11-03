"""Client for a local Ollama server's chat API."""

from __future__ import annotations

import json
from urllib.error import URLError
from urllib.request import Request, urlopen


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, host: str, model: str, num_ctx: int, timeout: float = 900):
        self._host = host
        self.model = model
        self.num_ctx = num_ctx
        self._timeout = timeout

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """Sends the conversation and returns the model's reply message (may contain `tool_calls`)."""
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            # Low temperature: a review should be repeatable, not creative.
            "options": {"num_ctx": self.num_ctx, "temperature": 0.2},
        }
        if tools:
            payload["tools"] = tools
        return self._post("/api/chat", payload)["message"]

    def has_model(self) -> bool:
        names = {m["name"] for m in self._get("/api/tags").get("models", [])}
        return self.model in names or f"{self.model}:latest" in names

    def _get(self, endpoint: str) -> dict:
        return self._send(Request(f"{self._host}{endpoint}"))

    def _post(self, endpoint: str, payload: dict) -> dict:
        body = json.dumps(payload).encode()
        return self._send(Request(f"{self._host}{endpoint}", data=body, headers={"Content-Type": "application/json"}))

    def _send(self, request: Request) -> dict:
        try:
            with urlopen(request, timeout=self._timeout) as response:
                return json.load(response)
        except URLError as e:
            raise OllamaError(f"連不到 Ollama（{self._host}）：{e.reason}。Ollama 有在執行嗎？") from e
