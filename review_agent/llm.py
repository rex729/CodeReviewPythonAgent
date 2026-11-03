"""Client for a local Ollama server's chat API."""

from __future__ import annotations

import json
import logging
from urllib.error import URLError
from urllib.request import Request, urlopen

log = logging.getLogger("review_agent")


class OllamaError(RuntimeError):
    pass


# A review answer is a short bullet list. Small models sometimes fall into repeating themselves and
# would otherwise write until the context window is full, which took over nine minutes in practice.
MAX_ANSWER_TOKENS = 1536
# Penalize tokens repeated within the last REPEAT_WINDOW tokens. Ollama's defaults (1.1 over 64
# tokens) are too short-sighted to stop a whole sentence coming back three times. Kept mild because
# reviews quote code, which legitimately repeats identifiers.
REPEAT_PENALTY = 1.15
REPEAT_WINDOW = 256


class OllamaClient:
    def __init__(self, host: str, model: str, num_ctx: int, timeout: float = 300):
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
            "options": {
                "num_ctx": self.num_ctx,
                "temperature": 0.2,  # A review should be repeatable, not creative.
                "num_predict": MAX_ANSWER_TOKENS,
                "repeat_penalty": REPEAT_PENALTY,
                "repeat_last_n": REPEAT_WINDOW,
            },
        }
        if tools:
            payload["tools"] = tools
        response = self._post("/api/chat", payload)
        message = response["message"]
        if response.get("done_reason") == "length":
            # Cut off at MAX_ANSWER_TOKENS: the last line stops mid-sentence, so drop it rather
            # than post half a finding.
            content = message.get("content", "")
            message["content"] = content.rsplit("\n", 1)[0] if "\n" in content else ""
            log.warning("模型的回答超過 %d 個 token 被截斷，已丟掉最後一行不完整的內容", MAX_ANSWER_TOKENS)
        return message

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
        except TimeoutError as e:
            raise OllamaError(f"Ollama 超過 {self._timeout:.0f} 秒沒有回應") from e
        except URLError as e:
            raise OllamaError(f"連不到 Ollama（{self._host}）：{e.reason}。Ollama 有在執行嗎？") from e
