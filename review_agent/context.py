"""Gathers what the model needs to review one file of a merge request with the whole project in
mind: a cached overview of the project, the file's full new version, and related code elsewhere."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Callable, Protocol

from .gitlab import MergeRequest
from .repo import GitError, GitRepo
from .tools import number_lines

# Root-level files that describe what a project is and how it's built.
PROJECT_DOCS = (
    "README.md", "README", "ARCHITECTURE.md", "CLAUDE.md",
    "settings.gradle.kts", "settings.gradle", "build.gradle.kts", "build.gradle", "app/build.gradle.kts",
    "package.json", "pyproject.toml", "requirements.txt", "Project.swift", "Package.swift",
    "Cargo.toml", "go.mod", "CMakeLists.txt",
)

BRIEF_PROMPT = """你是資深工程師，用繁體中文回答。根據專案的檔案結構和文件，寫一份給 code reviewer 參考的專案概要（400 字以內）：
- 這個專案是做什麼的
- 主要模組或資料夾，各自負責什麼
- 架構模式（例如 MVVM、分層、依賴注入的方式）
- 使用的語言、框架和明顯的程式慣例
只寫從資料看得出來的事，不要猜測。"""

# A definition of `name` in the common languages: `def name`, `fun name`, `class name`, `func name`...
DEFINITION_KEYWORDS = "def|fun|func|function|class|interface|struct|enum|protocol|object|trait|typealias"
DEFINITION = re.compile(rf"\b(?:{DEFINITION_KEYWORDS})\s+([A-Za-z_]\w*)")
CALL = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
NOT_PROJECT_SYMBOLS = {
    "if", "for", "while", "switch", "when", "catch", "return", "super", "self", "this", "init", "new",
    "print", "println", "len", "str", "int", "float", "list", "dict", "set", "tuple", "range", "isinstance",
    "map", "filter", "get", "let", "apply", "also", "run", "with", "require", "check", "assert", "format",
    "listOf", "setOf", "mapOf", "mutableListOf", "mutableStateOf", "remember", "launch", "emit", "Text",
}
MAX_CALLED_SYMBOLS = 6
MAX_CHANGED_SYMBOLS = 5


class ChatModel(Protocol):
    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict: ...


class ProjectContext:
    def __init__(
        self,
        repo: GitRepo,
        llm: ChatModel,
        cache_dir: Path,
        max_chars: int,
        is_reviewable: Callable[[str], bool],
    ):
        self._repo = repo
        self._llm = llm
        self._cache_dir = cache_dir
        self._max_chars = max_chars
        self._is_reviewable = is_reviewable

    def project_brief(self, rev: str) -> str:
        """An overview of the project at the revision, written by the model and cached until the
        file list or the project's docs change."""
        files = self._repo.list_files(rev)
        source = [path for path in files if self._is_reviewable(path)]
        tree = "\n".join(source[:300])
        if len(source) > 300:
            tree += f"\n…還有 {len(source) - 300} 個程式碼檔案"
        present = set(files)
        docs = [f"### {name}\n{self._repo.read_file(rev, name)[:3000]}" for name in PROJECT_DOCS if name in present]
        material = f"## 程式碼檔案\n{tree}\n\n## 專案文件與設定\n" + ("\n\n".join(docs) or "（沒有）")
        material = material[:self._max_chars]

        cache = self._cache_dir / f"{hashlib.sha256(material.encode()).hexdigest()[:16]}.md"
        if cache.exists():
            return cache.read_text(encoding="utf-8")
        reply = self._llm.chat([{"role": "system", "content": BRIEF_PROMPT}, {"role": "user", "content": material}])
        brief = reply.get("content", "").strip()
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(brief, encoding="utf-8")
        return brief

    def for_file(self, mr: MergeRequest, head: str, path: str, diff: str, changed_paths: list[str], brief: str) -> str:
        """Context for reviewing one file's diff, kept within the character budget."""
        about_mr = f"!{mr.iid}：{mr.title}"
        if mr.description.strip():
            about_mr += f"\n說明：{mr.description.strip()[:1000]}"
        about_mr += "\n這個 MR 改了這些檔案：" + "、".join(changed_paths[:30])
        sections = [("這個 MR", about_mr), ("專案概要", brief[:2500])]

        remaining = self._max_chars - sum(len(text) for _, text in sections)
        related = self._related_code(head, path, diff, max_chars=remaining // 3)
        full_file = self._full_file(head, path, max_chars=remaining - len(related))
        sections.append((f"{path} 在這個 MR 之後的完整內容", full_file))
        if related:
            sections.append(("專案中相關的程式碼（僅供參考，不要 review 它們本身）", related))
        return "\n\n".join(f"## {title}\n{text}" for title, text in sections)

    def _full_file(self, head: str, path: str, max_chars: int) -> str:
        content = self._repo.read_file(head, path)
        numbered = number_lines(content)
        if len(numbered) > max_chars:
            return f"（檔案共 {content.count(chr(10)) + 1} 行，太大放不下，需要時用 read_file 工具讀取）"
        return f"```\n{numbered}\n```"

    def _related_code(self, head: str, path: str, diff: str, max_chars: int) -> str:
        """Definitions of what the change calls, and other places that use what the change modifies."""
        changed_lines, added_lines = [], []
        for line in diff.splitlines():
            if line.startswith(("+++", "---")):
                continue
            if line.startswith(("+", "-")):
                changed_lines.append(line[1:])
            if line.startswith("+"):
                added_lines.append(line[1:])

        changed_symbols = _unique(DEFINITION.findall("\n".join(changed_lines)))
        called_symbols = _unique(
            name for name in CALL.findall("\n".join(added_lines))
            if len(name) >= 3 and name not in NOT_PROJECT_SYMBOLS and name not in changed_symbols
        )

        parts: list[str] = []
        for name in called_symbols[:MAX_CALLED_SYMBOLS]:
            definition = self._find_definition(head, name, exclude_path=path)
            if definition:
                parts.append(definition)
        for name in changed_symbols[:MAX_CHANGED_SYMBOLS]:
            usages = [hit for hit in self._repo.grep(head, name, limit=30, word=True) if not hit.startswith(f"{path}:")]
            if usages:
                parts.append(f"### 其他檔案中使用 `{name}` 的地方\n" + "\n".join(usages[:8]))

        result = ""
        for part in parts:
            if len(result) + len(part) + 2 > max_chars:
                break
            result += part + "\n\n"
        return result.strip()

    def _find_definition(self, head: str, name: str, exclude_path: str) -> str | None:
        pattern = f"({DEFINITION_KEYWORDS})[[:space:]]+{name}"
        for hit in self._repo.grep(head, pattern, limit=5, regex=True, word=True):
            hit_path, line, _ = hit.split(":", 2)
            if hit_path == exclude_path:
                continue  # Already visible in the full file.
            try:
                lines = self._repo.read_file(head, hit_path).splitlines()
            except GitError:
                continue
            start = max(1, int(line) - 2)
            snippet = "\n".join(lines[start - 1:start + 27])
            return f"### `{name}` 的定義（{hit_path}:{line}）\n```\n{number_lines(snippet, start)}\n```"
        return None


def _unique(names) -> list[str]:
    return list(dict.fromkeys(names))
