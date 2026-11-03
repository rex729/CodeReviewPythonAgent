"""OllamaClient against a local HTTP server standing in for Ollama."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from review_agent.llm import MAX_ANSWER_TOKENS, OllamaClient


class FakeOllama:
    def __init__(self, reply):
        self.payloads = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                server.payloads.append(json.loads(self.rfile.read(length)))
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_port}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()


def ollama_reply(content, done_reason="stop"):
    return {"message": {"role": "assistant", "content": content}, "done": True, "done_reason": done_reason}


class OllamaClientTest(unittest.TestCase):
    def chat(self, reply):
        server = FakeOllama(reply)
        self.addCleanup(server.close)
        message = OllamaClient(server.url, "qwen2.5-coder:7b", 16384).chat([{"role": "user", "content": "hi"}])
        return message, server.payloads[0]

    def test_sends_the_generation_limits(self):
        _, payload = self.chat(ollama_reply("ok"))

        options = payload["options"]
        self.assertEqual(options["num_predict"], MAX_ANSWER_TOKENS)
        self.assertEqual(options["num_ctx"], 16384)
        self.assertGreater(options["repeat_penalty"], 1.1)
        self.assertGreater(options["repeat_last_n"], 64)

    def test_a_complete_answer_is_returned_as_is(self):
        message, _ = self.chat(ollama_reply("- [高] a.cpp:1 — x\n- [低] a.cpp:2 — y"))

        self.assertEqual(message["content"], "- [高] a.cpp:1 — x\n- [低] a.cpp:2 — y")

    def test_a_cut_off_answer_loses_its_unfinished_last_line(self):
        # The real case: a repetition loop hit the token limit mid-bullet.
        content = "- [中] stats.cpp:22 — sprintf 緩衝區溢位\n- [中] stats.cpp:24 — `std::cout` 可能會導致格式錯誤，如果 `values[i]`"

        with self.assertLogs("review_agent", "WARNING"):
            message, _ = self.chat(ollama_reply(content, done_reason="length"))

        self.assertEqual(message["content"], "- [中] stats.cpp:22 — sprintf 緩衝區溢位")

    def test_a_cut_off_single_line_answer_becomes_empty(self):
        with self.assertLogs("review_agent", "WARNING"):
            message, _ = self.chat(ollama_reply('{"name": "read_fi', done_reason="length"))

        self.assertEqual(message["content"], "")


if __name__ == "__main__":
    unittest.main()
