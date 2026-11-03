"""Reviews one repository: each call to tick() reviews its merge requests that are new or have new commits."""

from __future__ import annotations

import logging
from urllib.error import URLError

from .config import Settings
from .context import ProjectContext
from .gitlab import GitLabClient, GitLabError, MergeRequest
from .llm import OllamaError
from .repo import GitError, GitRepo
from .report import ReportWriter
from .reviewer import Reviewer, has_findings
from .state import StateStore
from .thread import MergeRequestThread, split_points
from .tools import RepoTools

log = logging.getLogger("review_agent")

# Problems worth waiting out (network down, Ollama restarting, no access); anything else is a bug.
RECOVERABLE_ERRORS = (GitError, GitLabError, OllamaError, URLError, TimeoutError, ConnectionError)


class ReviewAgent:
    def __init__(
        self,
        name: str,
        settings: Settings,
        gitlab: GitLabClient,
        repo: GitRepo,
        reviewer: Reviewer,
        context: ProjectContext,
        state_store: StateStore,
        reports: ReportWriter,
        thread: MergeRequestThread | None = None,
    ):
        self.name = name
        self._settings = settings
        self._gitlab = gitlab
        self._repo = repo
        self._reviewer = reviewer
        self._context = context
        self._store = state_store
        self._reviewed = state_store.load()
        self._reports = reports
        # None when posting is off: reviews then only go to the local reports.
        self._thread = thread

    def tick(self) -> None:
        """Review every open MR whose latest commit hasn't been reviewed yet."""
        open_mrs = self._gitlab.open_merge_requests(self._settings.target_branch)
        open_ids = {str(mr.iid) for mr in open_mrs}
        for closed in set(self._reviewed) - open_ids:
            del self._reviewed[closed]

        pending = [mr for mr in open_mrs if self._reviewed.get(str(mr.iid)) != mr.sha]
        if not pending:
            log.info("%s：沒有需要 review 的 MR（開著的 MR：%d 個）", self.name, len(open_mrs))
        for mr in pending:
            if self._already_reviewed_on_gitlab(mr):
                log.info("%s：MR !%d 的 thread 已標記 review 過 commit %s，跳過", self.name, mr.iid, mr.sha[:8])
            else:
                self._review(mr)
            # Saved after each MR, so a crash or restart only redoes the one in progress.
            self._reviewed[str(mr.iid)] = mr.sha
            self._store.save(self._reviewed)
        self._store.save(self._reviewed)

    def _already_reviewed_on_gitlab(self, mr: MergeRequest) -> bool:
        """Whether the MR's thread says its current commit was reviewed, e.g. by a run whose local
        state is gone or by another copy of the agent."""
        if self._thread is None:
            return False
        reviewed = self._thread.last_reviewed_commit(mr.iid)
        return reviewed is not None and mr.sha.startswith(reviewed)

    def _review(self, mr: MergeRequest) -> None:
        log.info("%s：review MR !%d：%s", self.name, mr.iid, mr.title)
        target = self._repo.fetch_branch(mr.target_branch)
        head = self._repo.fetch_merge_request(mr.iid)
        base = self._repo.merge_base(target, head)

        paths = [
            path for status, path in self._repo.changed_files(base, head)
            if status != "D" and self._settings.is_reviewable(path)
        ]
        if not paths:
            log.info("%s：MR !%d 沒有需要 review 的程式碼檔案", self.name, mr.iid)
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
        report = self._reports.write(mr, summary, findings, model=self._settings.model, brief=brief)
        flagged = sum(1 for text in findings.values() if has_findings(text))
        log.info("%s：MR !%d 完成，%d 個檔案中 %d 個有問題，報告在 %s", self.name, mr.iid, len(findings), flagged, report)

        if self._thread:
            points = [point for text in findings.values() if has_findings(text) for point in split_points(text)]
            # If posting fails, the error skips marking the MR reviewed, so the next round tries again;
            # the thread's own content keeps that retry from repeating anything already posted.
            outcome = self._thread.publish(mr, head, points)
            log.info("%s：MR !%d %s", self.name, mr.iid, outcome)
