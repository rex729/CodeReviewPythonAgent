"""Read-only tools the model may call while reviewing a change, all pinned to one revision."""

from __future__ import annotations

import json

from .repo import GitError, GitRepo

MAX_TOOL_OUTPUT_CHARS = 8_000
MAX_READ_LINES = 300

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "讀取專案中某個檔案的內容（附行號）。可指定行號範圍，一次最多 300 行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "從專案根目錄開始的路徑，例如 src/app/main.py"},
                    "start_line": {"type": "integer", "description": "起始行號，預設 1"},
                    "end_line": {"type": "integer", "description": "結束行號（含）"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出某個資料夾底下的所有檔案路徑。",
            "parameters": {
                "type": "object",
                "properties": {"directory": {"type": "string", "description": "資料夾路徑，空字串代表專案根目錄"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "在整個專案中搜尋包含某段文字的程式碼行，例如函式名稱或類別名稱。",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "要搜尋的文字（不是正規表示式）"}},
                "required": ["text"],
            },
        },
    },
]


def number_lines(text: str, start: int = 1) -> str:
    return "\n".join(f"{n:>5}| {line}" for n, line in enumerate(text.splitlines(), start))


class RepoTools:
    def __init__(self, repo: GitRepo, rev: str):
        self._repo = repo
        self._rev = rev

    def call(self, name: str, arguments: dict | str | None) -> str:
        """Runs a tool the model asked for. Failures come back as text so the model can adjust."""
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                return f"錯誤：工具參數不是合法的 JSON：{arguments}"
        arguments = arguments or {}
        handler = {"read_file": self._read_file, "list_files": self._list_files, "search_code": self._search_code}.get(name)
        if handler is None:
            return f"錯誤：沒有名為 {name} 的工具。可用的工具：read_file、list_files、search_code"
        try:
            output = handler(**arguments)
        except TypeError as e:
            return f"錯誤：{name} 的參數不正確：{e}"
        except GitError as e:
            return f"錯誤：{e}"
        if len(output) > MAX_TOOL_OUTPUT_CHARS:
            output = output[:MAX_TOOL_OUTPUT_CHARS] + "\n…（輸出過長，已截斷）"
        return output

    def _read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        lines = self._repo.read_file(self._rev, path.strip("/")).splitlines()
        start = max(1, int(start_line))
        end = min(len(lines), int(end_line) if end_line else start + MAX_READ_LINES - 1, start + MAX_READ_LINES - 1)
        if start > len(lines):
            return f"{path} 只有 {len(lines)} 行"
        return f"{path}（第 {start}–{end} 行，共 {len(lines)} 行）\n" + number_lines("\n".join(lines[start - 1:end]), start)

    def _list_files(self, directory: str = "") -> str:
        prefix = directory.strip("/")
        prefix = f"{prefix}/" if prefix else ""
        paths = [p for p in self._repo.list_files(self._rev) if p.startswith(prefix)]
        if not paths:
            return f"資料夾 {directory or '/'} 底下沒有檔案"
        shown = paths[:200]
        more = f"\n…還有 {len(paths) - len(shown)} 個檔案" if len(paths) > len(shown) else ""
        return "\n".join(shown) + more

    def _search_code(self, text: str) -> str:
        matches = self._repo.grep(self._rev, text, limit=50)
        return "\n".join(matches) if matches else f"找不到「{text}」"
