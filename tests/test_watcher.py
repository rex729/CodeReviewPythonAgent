import tempfile
import unittest
from pathlib import Path

from review_agent.repo import GitError
from review_agent.watcher import Watcher


class FakeAgent:
    def __init__(self, target, log, fail=False):
        self.target = target
        self._log = log
        self._fail = fail

    def tick(self):
        self._log.append(self.target.project_path)
        if self._fail:
            raise GitError("git fetch 失敗：Permission denied")


class StopLoop(Exception):
    pass


class WatcherTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.repos = self.dir / "repos.txt"
        self.interval = self.dir / "review.config"
        self.ticks = []
        self.created = []
        self.failing = set()
        self.sleeps = []

    def tearDown(self):
        self._tmp.cleanup()

    def make_agent(self, target):
        self.created.append(target.project_path)
        return FakeAgent(target, self.ticks, fail=target.project_path in self.failing)

    def make_watcher(self, sleep=None):
        return Watcher(self.repos, self.interval, self.make_agent, sleep=sleep or self.sleeps.append)

    def set_repos(self, *paths):
        self.repos.write_text("\n".join(f"https://gitlab.com/{p}" for p in paths), encoding="utf-8")

    def test_reviews_every_repo_in_order(self):
        self.set_repos("g/a", "g/b", "g/c")

        self.make_watcher().run_round()

        self.assertEqual(self.ticks, ["g/a", "g/b", "g/c"])

    def test_repos_added_to_the_file_are_picked_up_next_round(self):
        watcher = self.make_watcher()
        self.set_repos("g/a")
        watcher.run_round()

        self.set_repos("g/a", "g/new")
        watcher.run_round()

        self.assertEqual(self.ticks, ["g/a", "g/a", "g/new"])
        self.assertEqual(self.created, ["g/a", "g/new"])  # existing agents are reused

    def test_a_failing_repo_does_not_stop_the_others(self):
        self.set_repos("g/a", "g/broken", "g/c")
        self.failing.add("g/broken")

        self.make_watcher().run_round()

        self.assertEqual(self.ticks, ["g/a", "g/broken", "g/c"])

    def test_waits_the_configured_minutes_after_each_round_finishes(self):
        self.set_repos("g/a", "g/b")
        self.interval.write_text("3", encoding="utf-8")
        events = []

        def sleep(seconds):
            events.append(("sleep", seconds))
            if len(events) > 4:
                raise StopLoop

        watcher = Watcher(self.repos, self.interval, self.make_agent, sleep=sleep)
        self.ticks = events  # interleave ticks and sleeps in one timeline
        with self.assertRaises(StopLoop):
            watcher.run_forever()

        self.assertEqual(events[:6], ["g/a", "g/b", ("sleep", 180), "g/a", "g/b", ("sleep", 180)])

    def test_interval_changes_apply_to_the_next_wait(self):
        self.set_repos("g/a")
        watcher = self.make_watcher()
        self.interval.write_text("2", encoding="utf-8")
        self.assertEqual(watcher.interval_minutes(), 2)

        self.interval.write_text("7", encoding="utf-8")

        self.assertEqual(watcher.interval_minutes(), 7)

    def test_bad_interval_mid_run_keeps_the_last_good_one(self):
        watcher = self.make_watcher()
        self.interval.write_text("4", encoding="utf-8")
        watcher.interval_minutes()

        self.interval.write_text("99", encoding="utf-8")

        self.assertEqual(watcher.interval_minutes(), 4)

    def test_bad_interval_at_start_is_an_error(self):
        self.interval.write_text("0", encoding="utf-8")

        with self.assertRaises(ValueError):
            self.make_watcher().interval_minutes()

    def test_missing_repos_file_is_an_empty_round(self):
        self.make_watcher().run_round()

        self.assertEqual(self.ticks, [])


if __name__ == "__main__":
    unittest.main()
