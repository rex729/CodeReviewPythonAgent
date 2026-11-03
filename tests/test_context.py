import tempfile
import unittest
from pathlib import Path

from review_agent.context import ProjectContext
from review_agent.gitlab import MergeRequest
from tests.test_repo_and_tools import RepoTestCase, git

PRICING = "def apply_discount(price, rate):\n    return price * (1 - rate)\n"
CHECKOUT = "from pricing import apply_discount\n\ndef total(items):\n    return sum(apply_discount(i, 0.1) for i in items)\n"
REPORT = "from pricing import apply_discount\n\ndef yearly(x):\n    return apply_discount(x, 0.2)\n"


class CountingLLM:
    def __init__(self):
        self.requests = []

    def chat(self, messages, tools=None):
        self.requests.append(messages)
        return {"role": "assistant", "content": "一個購物車專案"}


def mr(description=""):
    return MergeRequest(iid=5, title="折扣改用百分比", sha="s", target_branch="main", web_url="u", description=description)


class ProjectContextTest(RepoTestCase):
    INITIAL_FILES = {
        "README.md": "# Shop\n購物車\n",
        "pricing.py": PRICING,
        "checkout.py": CHECKOUT,
        "report.py": REPORT,
        "assets/logo.svg": "<svg/>",
    }

    def setUp(self):
        super().setUp()
        self.llm = CountingLLM()
        self._cache = tempfile.TemporaryDirectory()
        self.context = ProjectContext(
            self.repo, self.llm, Path(self._cache.name), max_chars=20_000,
            is_reviewable=lambda path: path.endswith(".py"),
        )
        self.main = self.repo.fetch_branch("main")

    def tearDown(self):
        self._cache.cleanup()
        super().tearDown()

    def test_brief_is_built_from_tree_and_docs_and_cached(self):
        first = self.context.project_brief(self.main)
        second = self.context.project_brief(self.main)

        self.assertEqual(first, "一個購物車專案")
        self.assertEqual(second, first)
        self.assertEqual(len(self.llm.requests), 1)
        material = self.llm.requests[0][1]["content"]
        self.assertIn("pricing.py", material)
        self.assertIn("# Shop", material)
        self.assertNotIn("logo.svg", material)

    def test_brief_is_rebuilt_when_the_project_changes(self):
        self.context.project_brief(self.main)
        self.commit({"tax.py": "def tax(x):\n    return x\n"}, "add tax")

        self.context.project_brief(self.repo.fetch_branch("main"))

        self.assertEqual(len(self.llm.requests), 2)

    def test_changed_function_brings_in_its_callers_from_other_files(self):
        git(self.work, "checkout", "-b", "percent")
        self.commit({"pricing.py": "def apply_discount(price, percent):\n    return price * (100 - percent) / 100\n"},
                    "percent", ref="refs/merge-requests/5/head")
        head = self.repo.fetch_merge_request(5)
        diff = self.repo.diff(self.main, head, "pricing.py")

        context = self.context.for_file(mr("改成傳百分比"), head, "pricing.py", diff, ["pricing.py"], "概要")

        self.assertIn("說明：改成傳百分比", context)
        self.assertIn("pricing.py 在這個 MR 之後的完整內容", context)
        self.assertIn("(100 - percent)", context)
        self.assertIn("其他檔案中使用 `apply_discount` 的地方", context)
        self.assertIn("checkout.py:4:", context)
        self.assertIn("report.py:4:", context)

    def test_called_function_brings_in_its_definition(self):
        git(self.work, "checkout", "-b", "invoice")
        self.commit({"invoice.py": "from pricing import apply_discount\n\ndef invoice(x):\n    return apply_discount(x, 0.3)\n"},
                    "invoice", ref="refs/merge-requests/5/head")
        head = self.repo.fetch_merge_request(5)
        diff = self.repo.diff(self.main, head, "invoice.py")

        context = self.context.for_file(mr(), head, "invoice.py", diff, ["invoice.py"], "概要")

        self.assertIn("`apply_discount` 的定義（pricing.py:1）", context)
        self.assertIn("return price * (1 - rate)", context)

    def test_cpp_function_definitions_and_their_callers_are_found(self):
        git(self.work, "checkout", "-b", "cpp")
        self.commit({
            "stats.h": "#pragma once\nint* copy_values(const std::vector<int>& values);\n",
            "stats.cpp": '#include "stats.h"\n\nint* copy_values(const std::vector<int>& values) {\n'
                         "    int* buffer = new int[values.size()];\n    return buffer;\n}\n",
            "main.cpp": '#include "stats.h"\n\nint main() {\n    int* copy = copy_values(scores);\n    return 0;\n}\n',
        }, "cpp", ref="refs/merge-requests/5/head")
        head = self.repo.fetch_merge_request(5)

        main_context = self.context.for_file(mr(), head, "main.cpp", self.repo.diff(self.main, head, "main.cpp"), [], "")
        stats_context = self.context.for_file(mr(), head, "stats.cpp", self.repo.diff(self.main, head, "stats.cpp"), [], "")

        # The body in stats.cpp, not the declaration in stats.h.
        self.assertIn("`copy_values` 的定義（stats.cpp:3）", main_context)
        self.assertIn("int* buffer = new int[values.size()];", main_context)
        self.assertIn("其他檔案中使用 `copy_values` 的地方", stats_context)
        self.assertIn("main.cpp:4:", stats_context)

    def test_file_too_big_for_the_budget_is_left_to_the_tools(self):
        # checkout.py with line numbers is ~150 characters; the MR header alone uses most of this budget.
        small = ProjectContext(self.repo, self.llm, Path(self._cache.name), max_chars=120, is_reviewable=lambda p: True)
        diff = "+x"

        context = small.for_file(mr(), self.main, "checkout.py", diff, ["checkout.py"], "")

        self.assertIn("太大放不下", context)


if __name__ == "__main__":
    unittest.main()
