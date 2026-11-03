import unittest

from review_agent.reviewer import NO_ISSUES, Reviewer, has_findings, tool_calls_in_text


class ScriptedLLM:
    """Replies with the given messages in order and records every request."""

    def __init__(self, replies, num_ctx=16384):
        self.num_ctx = num_ctx
        self._replies = list(replies)
        self.requests = []

    def chat(self, messages, tools=None):
        self.requests.append({"messages": list(messages), "tools": tools})
        return self._replies.pop(0)


class FakeTools:
    def __init__(self):
        self.calls = []

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        return f"result of {name}"


def tool_call(name, arguments):
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}


def answer(text):
    return {"role": "assistant", "content": text}


class ReviewChangeTest(unittest.TestCase):
    def test_sends_project_context_with_the_diff(self):
        llm = ScriptedLLM([answer(NO_ISSUES)])

        Reviewer(llm).review_change("a.py", "+x = 1", "## 專案概要\n記帳 App", FakeTools())

        prompt = llm.requests[0]["messages"][1]["content"]
        self.assertTrue(prompt.startswith("## 專案概要\n記帳 App"))
        self.assertIn("```diff\n+x = 1\n```", prompt)

    def test_runs_requested_tools_and_returns_final_answer(self):
        llm = ScriptedLLM([
            tool_call("read_file", {"path": "b.py"}),
            answer("- [高] a.py:3 — 會除以零。建議：先檢查"),
        ])
        tools = FakeTools()

        review = Reviewer(llm).review_change("a.py", "+x = 1 / n", "", tools)

        self.assertEqual(review, "- [高] a.py:3 — 會除以零。建議：先檢查")
        self.assertEqual(tools.calls, [("read_file", {"path": "b.py"})])
        tool_message = llm.requests[1]["messages"][-1]
        self.assertEqual(tool_message, {"role": "tool", "tool_name": "read_file", "content": "result of read_file"})

    def test_tool_call_written_as_text_is_executed(self):
        # What qwen2.5-coder:7b actually replied in a smoke test against Ollama 0.40.13.
        llm = ScriptedLLM([
            answer('{"name": "read_file", "arguments": {"path": "src/pricing.py"}}'),
            answer(NO_ISSUES),
        ])
        tools = FakeTools()

        review = Reviewer(llm).review_change("a.py", "+x", "", tools)

        self.assertEqual(review, NO_ISSUES)
        self.assertEqual(tools.calls, [("read_file", {"path": "src/pricing.py"})])

    def test_forces_an_answer_without_tools_after_step_limit(self):
        llm = ScriptedLLM([tool_call("search_code", {"text": "x"})] * 2 + [answer(NO_ISSUES)])

        review = Reviewer(llm, max_tool_steps=2).review_change("a.py", "+x", "", FakeTools())

        self.assertEqual(review, NO_ISSUES)
        self.assertIsNone(llm.requests[-1]["tools"])

    def test_empty_answer_counts_as_no_issues(self):
        review = Reviewer(ScriptedLLM([answer("  ")])).review_change("a.py", "+x", "", FakeTools())

        self.assertEqual(review, NO_ISSUES)

    def test_long_diff_is_truncated_to_its_share_of_the_window(self):
        llm = ScriptedLLM([answer(NO_ISSUES)], num_ctx=4096)  # smallest window: 6000 chars, 2400 for the diff

        Reviewer(llm).review_change("a.py", "+" + "x" * 10_000, "", FakeTools())

        prompt = llm.requests[0]["messages"][1]["content"]
        self.assertIn("diff 過長，已截斷", prompt)
        self.assertLess(len(prompt), 3_000)


class SummarizeTest(unittest.TestCase):
    def test_skips_the_model_when_nothing_was_found(self):
        llm = ScriptedLLM([])

        self.assertEqual(Reviewer(llm).summarize({"a.py": NO_ISSUES}, "概要"), NO_ISSUES)
        self.assertEqual(llm.requests, [])

    def test_sends_overview_and_only_flagged_files(self):
        llm = ScriptedLLM([answer("總結")])

        Reviewer(llm).summarize({"a.py": NO_ISSUES, "b.py": "- [中] b.py:2 — 沒處理錯誤"}, "## 這個 MR\n!3：登入")

        prompt = llm.requests[0]["messages"][1]["content"]
        self.assertTrue(prompt.startswith("## 這個 MR\n!3：登入"))
        self.assertIn("b.py", prompt)
        self.assertNotIn("a.py", prompt)


class ToolCallsInTextTest(unittest.TestCase):
    def test_parses_the_shapes_models_write(self):
        read = {"function": {"name": "read_file", "arguments": {"path": "a.py"}}}
        search = {"function": {"name": "search_code", "arguments": {"text": "x"}}}

        self.assertEqual(tool_calls_in_text('{"name": "read_file", "arguments": {"path": "a.py"}}'), [read])
        self.assertEqual(tool_calls_in_text('```json\n{"name": "read_file", "arguments": {"path": "a.py"}}\n```'), [read])
        self.assertEqual(tool_calls_in_text('<tool_call>\n{"name": "read_file", "arguments": {"path": "a.py"}}\n</tool_call>'), [read])
        self.assertEqual(
            tool_calls_in_text('{"name": "read_file", "arguments": {"path": "a.py"}}\n{"name": "search_code", "arguments": {"text": "x"}}'),
            [read, search],
        )

    def test_ignores_answers_and_unknown_tools(self):
        self.assertEqual(tool_calls_in_text("- [高] a.py:1 — bug"), [])
        self.assertEqual(tool_calls_in_text(NO_ISSUES), [])
        self.assertEqual(tool_calls_in_text('{"name": "rm_rf", "arguments": {}}'), [])
        self.assertEqual(tool_calls_in_text('{"name": "read_file"'), [])


class HasFindingsTest(unittest.TestCase):
    def test_detects_no_issue_answers(self):
        self.assertFalse(has_findings(NO_ISSUES))
        self.assertFalse(has_findings(f"{NO_ISSUES}。"))
        self.assertFalse(has_findings(""))
        self.assertTrue(has_findings("- [低] a.py:1 — 命名"))


if __name__ == "__main__":
    unittest.main()
