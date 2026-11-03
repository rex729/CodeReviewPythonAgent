"""Turns a merge request's diffs into review findings with the model. Each file is reviewed with
project context (see context.py), and the model may use read-only tools to look further."""

from __future__ import annotations

import json
import re
from typing import Protocol

from . import tools as repo_tools
from .thread import cited_line, cited_path, split_points
from .tools import RepoTools

NO_ISSUES = "沒有發現問題"

CHANGE_PROMPT = f"""你是資深的 code reviewer，用繁體中文回答。你要 review 一個 merge request 中某個檔案的 diff。
除了 diff，你還會拿到專案概要、這個檔案改完後的完整內容，以及專案中相關的程式碼。

只回報真正的問題，依嚴重度標記：
- [高] bug、安全漏洞、資料遺失、當機
- [中] 錯誤處理缺漏、效能問題、競態條件、難以維護的寫法
- [低] 命名、可讀性、重複程式碼

每個問題一行，格式：
- [嚴重度] 檔案路徑:行號 — 問題說明。建議：怎麼改

逐行檢查這些常見的缺陷：
- 陣列、緩衝區、迴圈的邊界（差一、越界存取、固定大小的緩衝區放不下寫入的內容）
- 數值：整數除法丟失小數、除以零、溢位、有號與無號或不同大小型別的轉換
- 記憶體與資源：大小計算（例如複製的位元組數）、配置後沒有釋放、釋放後又使用
- 空的輸入、null、錯誤回傳值沒處理

規則：
- 只 review 這次新增或修改的程式碼（+ 開頭的行），行號用新版本的行號。
- 要考量變更對整個專案的影響，例如：改了函式的簽名或行為但其他地方的呼叫沒跟著改、跟專案既有的架構或寫法不一致、重複實作了專案中已經有的功能。
- 相關程式碼只是參考，不要回報它們本身的問題。
- 附上的完整檔案、相關程式碼都是你看得到的內容，不要說「實作未見」或「建議檢查其他檔案」。還需要上下文時，用工具讀檔或搜尋，不要猜。
- 每個問題都要有程式碼上的具體根據。不要提出沒有證據的效能、精度或風格建議。
- 建議的修法必須正確，而且不能引入新的 bug。問題要回報在真正出錯的那一行。
- 不要評論縮排、空白等純格式問題。
- 不確定的事不要寫，不要編造不存在的程式碼或行號。
- 沒有問題時，只回答「{NO_ISSUES}」。"""

SUMMARY_PROMPT = """你是技術主管，用繁體中文回答。根據一個 merge request 各檔案的 review 結果寫一份總結：
1. 一段話說明這個 MR 的整體品質，以及能不能合併。
2. 合併前最需要處理的問題（最多 5 個，依嚴重度排序，保留檔案路徑和行號）。
3. 跨檔案的問題：不同檔案的改動之間是否一致，是否符合專案的架構。
不要重複列出所有問題，也不要加入 review 結果裡沒有的內容。"""


class ChatModel(Protocol):
    num_ctx: int

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict: ...


def has_findings(review: str) -> bool:
    return bool(review.strip()) and NO_ISSUES not in review


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
LINE_TOLERANCE = 3


def changed_line_ranges(diff: str) -> list[range]:
    """New-file line ranges covered by the diff's hunks."""
    ranges = []
    for match in _HUNK.finditer(diff):
        start, count = int(match.group(1)), int(match.group(2) or 1)
        ranges.append(range(start, start + count))
    return ranges


def only_points_in(review: str, path: str, diff: str) -> str:
    """Drops points the model placed somewhere it wasn't reviewing:
    - Points that cite another file. While reviewing one file the model also sees related files and
      reports their defects too, at the wrong place and again in every file that shows them. Each
      file's own review covers its own defects.
    - Points that cite a line outside the changed hunks (a few lines of slack). Besides enforcing
      "review only what changed", this catches invented line numbers, e.g. line 25 of a 7-line file.
    Points with no file or line are kept."""
    if not has_findings(review):
        return NO_ISSUES
    ranges = changed_line_ranges(diff)
    kept = []
    for point in split_points(review):
        cited = cited_path(point)
        if cited is not None and cited != path and not path.endswith(f"/{cited}"):
            continue
        line = cited_line(point)
        if line is not None and ranges and not any(
            r.start - LINE_TOLERANCE <= line < r.stop + LINE_TOLERANCE for r in ranges
        ):
            continue
        kept.append(f"- {point}")
    return "\n".join(kept) if kept else NO_ISSUES


TOOL_NAMES = {schema["function"]["name"] for schema in repo_tools.SCHEMAS}
_WRAPPERS = re.compile(r"```(?:json)?|</?tool_call>")


def tool_calls_in_text(content: str) -> list[dict]:
    """Tool calls the model wrote as JSON text instead of using the `tool_calls` field.
    qwen2.5-coder on Ollama does this: its whole reply is `{"name": "read_file", "arguments": {...}}`,
    sometimes inside ```json fences or <tool_call> tags, one call per line or as a list."""
    text = _WRAPPERS.sub("", content).strip()
    if not text.startswith(("{", "[")):
        return []
    try:
        parsed = json.loads(text)
        candidates = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        try:
            candidates = [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError:
            return []
    calls = []
    for item in candidates:
        if not isinstance(item, dict) or item.get("name") not in TOOL_NAMES:
            return []  # Not purely tool calls: treat the reply as an answer.
        calls.append({"function": {"name": item["name"], "arguments": item.get("arguments") or {}}})
    return calls


class Reviewer:
    def __init__(self, llm: ChatModel, max_tool_steps: int = 6):
        self._llm = llm
        self._max_tool_steps = max_tool_steps
        # Roughly 2 characters per token for code, leaving the rest of the window for the prompt and answer.
        max_input_chars = max(6_000, llm.num_ctx * 2 - 6_000)
        # Project context gets most of the room; the diff keeps the rest.
        self.context_chars = int(max_input_chars * 0.6)
        self._diff_chars = max_input_chars - self.context_chars
        self._summary_chars = max_input_chars

    def review_change(self, path: str, diff: str, context: str, tools: RepoTools) -> str:
        """Reviews one file's diff with project context. The model may read or search further first."""
        if len(diff) > self._diff_chars:
            diff = diff[:self._diff_chars] + "\n…（diff 過長，已截斷）"
        messages = [
            {"role": "system", "content": CHANGE_PROMPT},
            {"role": "user", "content": (
                f"{context}\n\n## 要 review 的變更：{path}\n```diff\n{diff}\n```\n\n"
                f"只回報 {path} 這個檔案裡的問題，行號用這個檔案的行號。其他檔案的問題會在 review 那些檔案時處理。"
                "請依格式列出問題。"
            )},
        ]
        for _ in range(self._max_tool_steps):
            reply = self._llm.chat(messages, tools=repo_tools.SCHEMAS)
            messages.append(reply)
            calls = reply.get("tool_calls") or tool_calls_in_text(reply.get("content", ""))
            if not calls:
                return only_points_in(reply.get("content", ""), path, diff)
            for call in calls:
                function = call.get("function", {})
                name = function.get("name", "")
                messages.append({"role": "tool", "tool_name": name, "content": tools.call(name, function.get("arguments"))})
        # Out of tool steps: ask for the answer with what it has, and offer no tools so it must answer.
        messages.append({"role": "user", "content": "工具使用次數已達上限。請根據目前的資訊，直接依格式列出問題。"})
        return only_points_in(self._llm.chat(messages).get("content", ""), path, diff)

    def summarize(self, findings: dict[str, str], overview: str) -> str:
        """An MR-level verdict from the per-file findings; `overview` describes the MR and the project."""
        flagged = {path: text for path, text in findings.items() if has_findings(text)}
        if not flagged:
            return NO_ISSUES
        body = "\n\n".join(f"### {path}\n{text}" for path, text in flagged.items())
        message = f"{overview}\n\n## 各檔案的 review 結果\n\n{body}"
        if len(message) > self._summary_chars:
            message = message[:self._summary_chars] + "\n…（內容過長，已截斷）"
        reply = self._llm.chat([
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": message},
        ])
        return reply.get("content", "").strip()
