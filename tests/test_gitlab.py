"""GitLabClient against a local HTTP server, to check the requests it actually sends."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from review_agent.gitlab import GitLabClient, GitLabError


class FakeGitLabServer:
    def __init__(self):
        self.requests = []
        self.responses = {}  # (method, path without query) -> (status, json body)
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                server.requests.append({
                    "method": self.command, "path": self.path, "body": body,
                    "token": self.headers.get("PRIVATE-TOKEN"), "type": self.headers.get("Content-Type"),
                })
                key = (self.command, self.path.split("?")[0])
                status, payload = server.responses.get(key, (404, {"message": "404 Not Found"}))
                if callable(payload):
                    payload = payload(self.path)
                data = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_DELETE = _handle

            def log_message(self, *args):
                pass

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._httpd.server_port}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()


PROJECT = "/api/v4/projects/group%2Fapp"


class GitLabClientTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeGitLabServer()
        self.client = GitLabClient(self.server.base_url, "group/app", token="glpat-test")

    def tearDown(self):
        self.server.close()

    def test_lists_open_merge_requests(self):
        self.server.responses[("GET", f"{PROJECT}/merge_requests")] = (200, [{
            "iid": 3, "title": "Fix", "sha": "abc", "target_branch": "main", "web_url": "u", "description": None,
        }])

        mrs = self.client.open_merge_requests("main")

        self.assertEqual(mrs[0].iid, 3)
        self.assertEqual(mrs[0].description, "")
        request = self.server.requests[0]
        self.assertIn("state=opened", request["path"])
        self.assertIn("target_branch=main", request["path"])
        self.assertEqual(request["token"], "glpat-test")

    def test_start_thread_posts_a_discussion_not_a_note(self):
        self.server.responses[("POST", f"{PROJECT}/merge_requests/3/discussions")] = (201, {"id": "d1", "notes": []})

        discussion_id = self.client.start_thread(3, "- 問題")

        self.assertEqual(discussion_id, "d1")
        request = self.server.requests[0]
        self.assertEqual(request["body"], {"body": "- 問題"})
        self.assertEqual(request["type"], "application/json")

    def test_delete_note_accepts_an_empty_204_answer(self):
        self.server.responses[("DELETE", f"{PROJECT}/merge_requests/3/notes/42")] = (204, None)

        self.client.delete_note(3, 42)

        self.assertEqual(self.server.requests[0]["method"], "DELETE")

    def test_reads_every_page_of_discussions_with_note_ids(self):
        def page(path):
            number = int(parse_qs(urlparse(path).query)["page"][0])
            count = 100 if number == 1 else 1
            return [{"id": f"p{number}-{i}", "notes": [{"id": i, "body": "b"}]} for i in range(count)]
        self.server.responses[("GET", f"{PROJECT}/merge_requests/3/discussions")] = (200, page)

        discussions = self.client.discussions(3)

        self.assertEqual(len(discussions), 101)
        self.assertEqual(discussions[-1].id, "p2-0")
        self.assertEqual((discussions[1].notes[0].id, discussions[1].note_bodies), (1, ["b"]))

    def test_permission_errors_explain_the_needed_scope(self):
        self.server.responses[("POST", f"{PROJECT}/merge_requests/3/discussions")] = (403, {"message": "403 Forbidden"})

        with self.assertRaises(GitLabError) as error:
            self.client.start_thread(3, "x")

        self.assertIn("api 權限", str(error.exception))

    def test_without_a_token_no_token_header_is_sent(self):
        self.server.responses[("GET", f"{PROJECT}/merge_requests")] = (200, [])

        GitLabClient(self.server.base_url, "group/app", token=None).open_merge_requests()

        self.assertIsNone(self.server.requests[0]["token"])


if __name__ == "__main__":
    unittest.main()
