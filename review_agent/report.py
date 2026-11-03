"""Writes each merge request review as a Markdown file."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .gitlab import MergeRequest
from .reviewer import has_findings


class ReportWriter:
    def __init__(self, directory: Path, now=datetime.now):
        self._directory = directory
        self._now = now

    def write(self, mr: MergeRequest, summary: str, findings: dict[str, str], model: str, brief: str) -> Path:
        now = self._now()
        flagged = {path: text for path, text in findings.items() if has_findings(text)}
        clean = [path for path in findings if path not in flagged]

        lines = [
            f"# MR !{mr.iid}：{mr.title}",
            "",
            f"- 連結：{mr.web_url}",
            f"- 合併到：`{mr.target_branch}`",
            f"- Commit：`{mr.sha[:12]}`",
            f"- 時間：{now:%Y-%m-%d %H:%M}",
            f"- 模型：{model}",
            f"- 檔案：共 {len(findings)} 個，{len(flagged)} 個有問題",
            "",
            "## 總結",
            "",
            summary,
            "",
            "## 各檔案",
        ]
        for path, text in flagged.items():
            lines += ["", f"### `{path}`", "", text]
        if clean:
            lines += ["", "## 沒有發現問題的檔案", "", *(f"- `{path}`" for path in clean)]
        # Shown so a reader can tell when a finding came from the model misunderstanding the project.
        lines += ["", "## 模型對專案的理解", "", brief]

        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._directory / f"{now:%Y%m%d-%H%M%S}_mr-{mr.iid}.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path
