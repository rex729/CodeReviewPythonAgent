"""Entry point: python -m review_agent [--config config.toml] [--once]"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .agent import ReviewAgent
from .config import load_config
from .context import ProjectContext
from .gitlab import GitLabClient
from .llm import OllamaClient, OllamaError
from .repo import GitRepo
from .report import ReportWriter
from .reviewer import Reviewer
from .state import StateStore


def main() -> int:
    parser = argparse.ArgumentParser(description="持續監看一個 GitLab 專案的 MR，用本機 Ollama 模型做 code review。")
    parser.add_argument("--config", type=Path, default=Path("config.toml"), help="設定檔路徑（預設 config.toml）")
    parser.add_argument("--once", action="store_true", help="只檢查一次就結束，不持續監看")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    log = logging.getLogger("review_agent")

    if not args.config.exists():
        log.error("找不到設定檔 %s。請複製 config.example.toml 成 config.toml 並填入專案網址。", args.config)
        return 1
    config = load_config(args.config.resolve())

    llm = OllamaClient(config.ollama_host, config.model, config.num_ctx)
    try:
        if not llm.has_model():
            log.error("Ollama 裡沒有模型 %s，請先執行：ollama pull %s", config.model, config.model)
            return 1
    except OllamaError as e:
        log.error("%s", e)
        return 1

    repo = GitRepo(config.repo_dir, config.clone_url, config.gitlab_token)
    reviewer = Reviewer(llm)
    agent = ReviewAgent(
        config=config,
        gitlab=GitLabClient(config.gitlab_base_url, config.project_path, config.gitlab_token),
        repo=repo,
        reviewer=reviewer,
        context=ProjectContext(repo, llm, config.workdir / "briefs", reviewer.context_chars, config.is_reviewable),
        state_store=StateStore(config.state_file),
        reports=ReportWriter(config.reports_dir),
        model=config.model,
    )
    target = f"合併到 {config.target_branch} 的" if config.target_branch else "所有"
    log.info("監看 %s %s MR，模型 %s", config.project_url, target, config.model)
    try:
        if args.once:
            agent.tick()
        else:
            agent.run_forever()
    except KeyboardInterrupt:
        log.info("已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
