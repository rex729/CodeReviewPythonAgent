"""Entry point: python -m review_agent [--once]

Reads repos.txt (one repository URL per line), review.config (minutes between rounds, 1-10) and
the optional config.toml, all from the current folder unless given other paths."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .agent import ReviewAgent
from .config import RepoTarget, Settings, load_settings
from .context import ProjectContext
from .gitlab import GitLabClient
from .llm import OllamaClient, OllamaError
from .repo import GitRepo
from .report import ReportWriter
from .reviewer import Reviewer
from .state import StateStore
from .thread import MergeRequestThread
from .watcher import Watcher


def main() -> int:
    parser = argparse.ArgumentParser(description="持續監看多個 GitLab repo 的 MR，用本機 Ollama 模型做 code review。")
    parser.add_argument("--repos", type=Path, default=Path("repos.txt"), help="repo 清單，一行一個網址（預設 repos.txt）")
    parser.add_argument("--interval", type=Path, default=Path("review.config"), help="每輪間隔分鐘數的檔案（預設 review.config）")
    parser.add_argument("--config", type=Path, default=Path("config.toml"), help="進階設定（選填，預設 config.toml）")
    parser.add_argument("--token-file", type=Path, default=Path("bot_account.config"),
                        help="bot 帳號的 GitLab token（預設 bot_account.config；環境變數 GITLAB_TOKEN 優先）")
    parser.add_argument("--once", action="store_true", help="只跑一輪就結束")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    log = logging.getLogger("review_agent")

    if not args.repos.exists():
        log.error("找不到 %s。請建立這個檔案，每一行放一個 repo 網址。", args.repos)
        return 1
    settings = load_settings(args.config.resolve(), args.token_file.resolve())
    llm = OllamaClient(settings.ollama_host, settings.model, settings.num_ctx)
    try:
        if not llm.has_model():
            log.error("Ollama 裡沒有模型 %s，請先執行：ollama pull %s", settings.model, settings.model)
            return 1
    except OllamaError as e:
        log.error("%s", e)
        return 1

    reviewer = Reviewer(llm)
    posting = settings.post_comments and settings.gitlab_token is not None
    if settings.post_comments and not posting:
        log.warning("找不到 GitLab token（%s 或環境變數 GITLAB_TOKEN）：只寫本機報告，不在 MR 上留言"
                    "（GitLab 不允許匿名留言，需要 api 權限的 token）", args.token_file)

    def make_agent(target: RepoTarget) -> ReviewAgent:
        workdir = target.workdir(settings.workdir)
        repo = GitRepo(workdir / "clone", target.clone_url, settings.gitlab_token)
        gitlab = GitLabClient(target.api_base_url, target.project_path, settings.gitlab_token)
        return ReviewAgent(
            name=target.project_path,
            settings=settings,
            gitlab=gitlab,
            repo=repo,
            reviewer=reviewer,
            context=ProjectContext(repo, llm, workdir / "briefs", reviewer.context_chars, settings.is_reviewable),
            state_store=StateStore(workdir / "state.json"),
            reports=ReportWriter(settings.workdir / "reports" / target.host / target.project_path, target.project_path),
            thread=MergeRequestThread(gitlab, settings.model) if posting else None,
        )

    watcher = Watcher(args.repos.resolve(), args.interval.resolve(), make_agent)
    try:
        if not args.once:
            minutes = watcher.interval_minutes()  # fail fast on a bad review.config
            log.info("模型 %s，每輪結束後等 %d 分鐘", settings.model, minutes)
        watcher.run_round() if args.once else watcher.run_forever()
    except (ValueError, OSError) as e:
        log.error("%s", e)
        return 1
    except KeyboardInterrupt:
        log.info("已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
