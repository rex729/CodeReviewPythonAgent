"""Runs against real git repositories in a temp folder; a local bare repo stands in for GitLab."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from review_agent.repo import GitRepo
from review_agent.tools import RepoTools


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class RepoTestCase(unittest.TestCase):
    """A project on `main`, pushed to a bare "origin", plus the agent's clone of it."""

    INITIAL_FILES = {"src/app.py": "def add(a, b):\n    return a + b\n", "README.md": "hi\n"}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.origin = root / "origin.git"
        self.work = root / "work"
        git(root, "init", "--bare", "--initial-branch=main", str(self.origin))
        git(root, "clone", str(self.origin), str(self.work))
        git(self.work, "config", "user.email", "test@example.com")
        git(self.work, "config", "user.name", "Test")
        self.commit(self.INITIAL_FILES, "first")
        self.repo = GitRepo(root / "clone", self.origin.as_uri(), token=None)

    def tearDown(self):
        self._tmp.cleanup()

    def commit(self, files, message, ref="refs/heads/main"):
        for path, content in files.items():
            target = self.work / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-m", message)
        git(self.work, "push", "origin", f"HEAD:{ref}")


class GitRepoTest(RepoTestCase):
    def test_merge_request_is_compared_against_its_merge_base(self):
        main = self.repo.fetch_branch("main")
        # GitLab exposes every MR as refs/merge-requests/<iid>/head; push one by hand.
        git(self.work, "checkout", "-b", "feature")
        self.commit({"src/app.py": "def add(a, b):\n    return a - b\n"}, "feature", ref="refs/merge-requests/4/head")

        head = self.repo.fetch_merge_request(4)
        base = self.repo.merge_base(main, head)

        self.assertEqual(base, main)
        self.assertEqual(self.repo.changed_files(base, head), [("M", "src/app.py")])
        self.assertIn("-    return a + b", self.repo.diff(base, head, "src/app.py"))
        self.assertIn("return a - b", self.repo.read_file(head, "src/app.py"))

    def test_list_files(self):
        head = self.repo.fetch_branch("main")

        self.assertEqual(sorted(self.repo.list_files(head)), ["README.md", "src/app.py"])

    def test_grep_literal_regex_and_whole_word(self):
        head = self.repo.fetch_branch("main")

        self.assertEqual(self.repo.grep(head, "return", limit=10), ["src/app.py:2:    return a + b"])
        self.assertEqual(self.repo.grep(head, "(def|class)[[:space:]]+add", limit=10, regex=True, word=True),
                         ["src/app.py:1:def add(a, b):"])
        self.assertEqual(self.repo.grep(head, "ad", limit=10, word=True), [])
        self.assertEqual(self.repo.grep(head, "nothing-matches", limit=10), [])


class RepoToolsTest(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.tools = RepoTools(self.repo, self.repo.fetch_branch("main"))

    def test_read_file_numbers_lines(self):
        output = self.tools.call("read_file", {"path": "src/app.py", "start_line": 2})

        self.assertIn("第 2–2 行，共 2 行", output)
        self.assertIn("    2|     return a + b", output)

    def test_accepts_arguments_as_json_text(self):
        self.assertIn("src/app.py", self.tools.call("list_files", '{"directory": "src"}'))

    def test_errors_come_back_as_text(self):
        self.assertTrue(self.tools.call("read_file", {"path": "missing.py"}).startswith("錯誤"))
        self.assertTrue(self.tools.call("delete_everything", {}).startswith("錯誤"))
        self.assertTrue(self.tools.call("read_file", {"wrong": 1}).startswith("錯誤"))
        self.assertTrue(self.tools.call("read_file", "{not json").startswith("錯誤"))

    def test_search_code(self):
        self.assertEqual(self.tools.call("search_code", {"text": "def add"}), "src/app.py:1:def add(a, b):")


if __name__ == "__main__":
    unittest.main()
