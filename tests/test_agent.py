import tempfile
import unittest
from pathlib import Path

from review_agent.agent import ReviewAgent
from review_agent.config import DEFAULT_EXCLUDE_DIRS, DEFAULT_INCLUDE_EXTENSIONS, Config
from review_agent.gitlab import MergeRequest
from review_agent.state import StateStore


def make_config(workdir=Path("."), project_url="https://gitlab.com/group/project", target_branch=None):
    return Config(
        project_url=project_url, target_branch=target_branch, poll_seconds=1, gitlab_token=None,
        ollama_host="http://localhost:11434", model="qwen2.5-coder:7b", num_ctx=16384,
        include_extensions=DEFAULT_INCLUDE_EXTENSIONS, exclude_dirs=DEFAULT_EXCLUDE_DIRS, workdir=workdir,
    )


def mr(iid, sha, target="main"):
    return MergeRequest(iid=iid, title=f"MR {iid}", sha=sha, target_branch=target, web_url=f"https://gitlab.com/mr/{iid}")


class FakeRepo:
    def __init__(self):
        self.changes = [("M", "src/a.py"), ("D", "src/old.py"), ("A", "notes.txt"), ("A", "build/gen.py")]
        self.fetched_branches = []

    def fetch_branch(self, branch):
        self.fetched_branches.append(branch)
        return f"{branch}-head"

    def fetch_merge_request(self, iid):
        return f"mr{iid}-head"

    def merge_base(self, a, b):
        return f"base({a},{b})"

    def changed_files(self, base, head):
        return self.changes

    def diff(self, base, head, path):
        return f"{base}..{head} {path}"


class FakeReviewer:
    def __init__(self):
        self.diffs = []
        self.contexts = []
        self.overviews = []

    def review_change(self, path, diff, context, tools):
        self.diffs.append(diff)
        self.contexts.append(context)
        return "- [中] 問題"

    def summarize(self, findings, overview):
        self.overviews.append(overview)
        return "總結"


class FakeContext:
    def __init__(self):
        self.brief_revs = []

    def project_brief(self, rev):
        self.brief_revs.append(rev)
        return f"brief of {rev}"

    def for_file(self, mr, head, path, diff, changed_paths, brief):
        return f"context {path} {changed_paths} {brief}"


class FakeGitLab:
    def __init__(self):
        self.mrs = []
        self.requested_targets = []

    def open_merge_requests(self, target_branch=None):
        self.requested_targets.append(target_branch)
        return self.mrs


class FakeReports:
    def __init__(self):
        self.iids = []

    def write(self, mr, summary, findings, model, brief):
        self.iids.append(mr.iid)
        return Path(f"mr-{mr.iid}.md")


class ReviewAgentTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config = make_config(workdir=Path(self._tmp.name))
        self.repo = FakeRepo()
        self.reviewer = FakeReviewer()
        self.gitlab = FakeGitLab()
        self.reports = FakeReports()
        self.context = FakeContext()
        self.store = StateStore(self.config.state_file)

    def tearDown(self):
        self._tmp.cleanup()

    def make_agent(self, config=None):
        return ReviewAgent(
            config or self.config, self.gitlab, self.repo, self.reviewer, self.context, self.store, self.reports, model="m",
        )

    def test_reviews_only_changed_source_files_against_the_merge_base(self):
        self.gitlab.mrs = [mr(7, "s1", target="develop")]

        self.make_agent().tick()

        self.assertEqual(self.repo.fetched_branches, ["develop"])
        self.assertEqual(self.reviewer.diffs, ["base(develop-head,mr7-head)..mr7-head src/a.py"])
        self.assertEqual(self.reports.iids, [7])

    def test_each_file_is_reviewed_with_the_target_branch_project_context(self):
        self.gitlab.mrs = [mr(7, "s1", target="develop")]

        self.make_agent().tick()

        self.assertEqual(self.context.brief_revs, ["develop-head"])
        self.assertEqual(self.reviewer.contexts, ["context src/a.py ['src/a.py'] brief of develop-head"])
        self.assertIn("brief of develop-head", self.reviewer.overviews[0])

    def test_reviews_again_only_after_new_commits(self):
        agent = self.make_agent()
        self.gitlab.mrs = [mr(7, "s1")]
        agent.tick()
        agent.tick()

        self.gitlab.mrs = [mr(7, "s2")]
        agent.tick()

        self.assertEqual(self.reports.iids, [7, 7])

    def test_progress_survives_a_restart(self):
        self.gitlab.mrs = [mr(7, "s1")]
        self.make_agent().tick()

        self.make_agent().tick()

        self.assertEqual(self.reports.iids, [7])

    def test_closed_merge_requests_are_forgotten(self):
        agent = self.make_agent()
        self.gitlab.mrs = [mr(7, "s1"), mr(8, "s1")]
        agent.tick()

        self.gitlab.mrs = [mr(8, "s1")]
        agent.tick()

        self.assertEqual(self.store.load(), {"8": "s1"})

    def test_mr_without_source_changes_is_marked_reviewed_without_a_report(self):
        self.repo.changes = [("A", "notes.txt")]
        self.gitlab.mrs = [mr(7, "s1")]

        self.make_agent().tick()

        self.assertEqual(self.reports.iids, [])
        self.assertEqual(self.store.load(), {"7": "s1"})

    def test_passes_target_branch_filter_to_gitlab(self):
        self.make_agent(make_config(Path(self._tmp.name), target_branch="main")).tick()

        self.assertEqual(self.gitlab.requested_targets, ["main"])


class ConfigTest(unittest.TestCase):
    def test_project_paths_come_from_the_url(self):
        config = make_config(project_url="https://gitlab.com/k8stest2/sub/app.git")

        self.assertEqual(config.project_path, "k8stest2/sub/app")
        self.assertEqual(config.clone_url, "https://gitlab.com/k8stest2/sub/app.git")
        self.assertTrue(config.is_reviewable("src/Main.KT"))
        self.assertFalse(config.is_reviewable("build/generated/Main.kt"))
        self.assertFalse(config.is_reviewable("docs/notes.md"))


if __name__ == "__main__":
    unittest.main()
