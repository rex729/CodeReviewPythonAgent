"""The long-running loop: every poll, review merge requests that are new or have new commits."""

from __future__ import annotations

import logging
import time
from urllib.error import URLError

from .config import Config
from .context import ProjectContext
from .gitlab import GitLabClient, MergeRequest
from .llm import OllamaError
from .repo import GitError, GitRepo
from .report import ReportWriter
from .reviewer import Reviewer, has_findings
from .state import StateStore
from .tools import RepoTools

log = logging.getLogger("review_agent")

# Problems worth waiting out (network down, Ollama restarting); anything else is a bug and should stop the agent.
RECOVERABLE_ERRORS = (GitError, OllamaError, URLError, TimeoutError, ConnectionError)


class ReviewAgent:
    def __init__(
        self,
        config: Config,
        gitlab: GitLabClient,
        repo: GitRepo,
        reviewer: Reviewer,
        context: ProjectContext,
        state_store: StateStore,
        reports: ReportWriter,
        model: str,
        sleep=time.sleep,
    ):
        self._config = config
        self._gitlab = gitlab
        self._repo = repo
        self._reviewer = reviewer
        self._context = context
        self._store = state_store
        self._reviewed = state_store.load()
        self._reports = reports
        self._model = model
        self._sleep = sleep

    def run_forever(self) -> None:
        while True:
            try:
                self.tick()
            except RECOVERABLE_ERRORS as e:
                log.warning("這一輪失敗，下一輪重試：%s", e)
            log.info("等待 %d 秒後再檢查", self._config.poll_seconds)
            self._sleep(self._config.poll_seconds)

    def tick(self) -> None:
        """One poll: review every open MR whose latest commit hasn't been reviewed yet."""
        open_mrs = self._gitlab.open_merge_requests(self._config.target_branch)
        open_ids = {str(mr.iid) for mr in open_mrs}
        for closed in set(self._reviewed) - open_ids:
            del self._reviewed[closed]

        pending = [mr for mr in open_mrs if self._reviewed.get(str(mr.iid)) != mr.sha]
        if not pending:
            log.info("沒有需要 review 的 MR（開著的 MR：%d 個）", len(open_mrs))
        for mr in pending:
            self._review(mr)
            # Saved after each MR, so a crash or restart only redoes the one in progress.
            self._reviewed[str(mr.iid)] = mr.sha
            self._store.save(self._reviewed)
        self._store.save(self._reviewed)

    def _review(self, mr: MergeRequest) -> None:
        log.info("Review MR !%d：%s", mr.iid, mr.title)
        target = self._repo.fetch_branch(mr.target_branch)
        head = self._repo.fetch_merge_request(mr.iid)
        base = self._repo.merge_base(target, head)

        paths = [
            path for status, path in self._repo.changed_files(base, head)
            if status != "D" and self._config.is_reviewable(path)
        ]
        if not paths:
            log.info("MR !%d 沒有需要 review 的程式碼檔案", mr.iid)
            return

        # The overview describes the project the MR merges into, so it comes from the target branch.
        log.info("整理專案概要（%s）", mr.target_branch)
        brief = self._context.project_brief(target)

        tools = RepoTools(self._repo, head)
        findings = {}
        for i, path in enumerate(paths, 1):
            log.info("[%d/%d] %s", i, len(paths), path)
            diff = self._repo.diff(base, head, path)
            context = self._context.for_file(mr, head, path, diff, paths, brief)
            findings[path] = self._reviewer.review_change(path, diff, context, tools)

        overview = f"## 這個 MR\n!{mr.iid}：{mr.title}\n{mr.description.strip()[:1000]}\n\n## 專案概要\n{brief}"
        summary = self._reviewer.summarize(findings, overview)
        report = self._reports.write(mr, summary, findings, model=self._model, brief=brief)
        flagged = sum(1 for text in findings.values() if has_findings(text))
        log.info("MR !%d 完成：%d 個檔案中 %d 個有問題，報告在 %s", mr.iid, len(findings), flagged, report)
