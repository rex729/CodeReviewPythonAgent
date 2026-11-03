"""The long-running loop over every repository in repos.txt.

A round reviews the repositories one after another. The wait from review.config starts only after
the whole round finishes, so a slow round delays the next one instead of overlapping it. Both files
are read again every round: add a repository or change the interval without restarting."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from .agent import RECOVERABLE_ERRORS, ReviewAgent
from .config import RepoTarget, read_interval_minutes, read_repos

log = logging.getLogger("review_agent")


class Watcher:
    def __init__(
        self,
        repos_file: Path,
        interval_file: Path,
        make_agent: Callable[[RepoTarget], ReviewAgent],
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._repos_file = repos_file
        self._interval_file = interval_file
        self._make_agent = make_agent
        self._sleep = sleep
        self._agents: dict[str, ReviewAgent] = {}
        self._interval: int | None = None

    def run_forever(self) -> None:
        while True:
            self.run_round()
            minutes = self.interval_minutes()
            log.info("這一輪結束，%d 分鐘後開始下一輪", minutes)
            self._sleep(minutes * 60)

    def run_round(self) -> None:
        targets = self._read_targets()
        if not targets:
            log.warning("%s 裡沒有任何 repo", self._repos_file.name)
        started = time.monotonic()
        for target in targets:
            agent = self._agents.get(target.url)
            if agent is None:
                agent = self._agents[target.url] = self._make_agent(target)
            try:
                agent.tick()
            except RECOVERABLE_ERRORS as e:
                # One unreachable repository shouldn't stop the others.
                log.warning("%s：這一輪跳過，下一輪再試：%s", target.project_path, e)
        # Forget repositories removed from repos.txt.
        current = {target.url for target in targets}
        for url in set(self._agents) - current:
            del self._agents[url]
        if targets:
            log.info("這一輪 review 了 %d 個 repo，花了 %.1f 分鐘", len(targets), (time.monotonic() - started) / 60)

    def interval_minutes(self) -> int:
        """Reads review.config. A bad value mid-run keeps the last good one; with none yet, it raises."""
        try:
            self._interval = read_interval_minutes(self._interval_file)
        except (ValueError, OSError) as e:
            if self._interval is None:
                raise
            log.warning("%s；沿用上一次的 %d 分鐘", e, self._interval)
        return self._interval

    def _read_targets(self) -> list[RepoTarget]:
        try:
            targets, problems = read_repos(self._repos_file)
        except OSError as e:
            log.warning("讀不到 %s：%s", self._repos_file, e)
            return []
        for problem in problems:
            log.warning("%s（已略過）", problem)
        return targets
