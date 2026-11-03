import os
import tempfile
import unittest
from pathlib import Path

from review_agent.config import parse_repo_url, read_interval_minutes, read_repos, read_token


class ParseRepoUrlTest(unittest.TestCase):
    def test_https_url(self):
        target = parse_repo_url("https://gitlab.com/k8stest2/sub/app.git")

        self.assertEqual(target.api_base_url, "https://gitlab.com")
        self.assertEqual(target.project_path, "k8stest2/sub/app")
        self.assertEqual(target.clone_url, "https://gitlab.com/k8stest2/sub/app.git")

    def test_https_url_without_git_suffix_and_with_port(self):
        target = parse_repo_url("http://gitlab.local:8080/team/app/")

        self.assertEqual(target.api_base_url, "http://gitlab.local:8080")
        self.assertEqual(target.clone_url, "http://gitlab.local:8080/team/app.git")

    def test_ssh_addresses_clone_over_ssh_and_use_https_for_the_api(self):
        scp = parse_repo_url("git@gitlab.com:machinelearning7311303/codereviewpythonagent.git")
        ssh = parse_repo_url("ssh://git@gitlab.com/group/app.git")

        self.assertEqual(scp.api_base_url, "https://gitlab.com")
        self.assertEqual(scp.project_path, "machinelearning7311303/codereviewpythonagent")
        self.assertEqual(scp.clone_url, "git@gitlab.com:machinelearning7311303/codereviewpythonagent.git")
        self.assertEqual(ssh.project_path, "group/app")
        self.assertEqual(ssh.clone_url, "ssh://git@gitlab.com/group/app.git")

    def test_rejects_what_is_not_a_project_url(self):
        for bad in ["not a url", "https://gitlab.com/only-group", "ftp://gitlab.com/a/b"]:
            with self.assertRaises(ValueError, msg=bad):
                parse_repo_url(bad)

    def test_workdir_is_per_host_and_project(self):
        target = parse_repo_url("git@gitlab.com:group/app.git")

        self.assertEqual(target.workdir(Path("w")), Path("w/repos/gitlab.com/group/app"))


class ConfigFileTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, text, encoding="utf-8"):
        path = self.dir / name
        path.write_text(text, encoding=encoding)
        return path


class ReadReposTest(ConfigFileTestCase):
    def test_one_url_per_line_skipping_blanks_comments_and_duplicates(self):
        path = self.write("repos.txt", "\n".join([
            "# 要監看的 repo",
            "https://gitlab.com/group/app",
            "",
            "   git@gitlab.com:group/lib.git   ",
            "https://gitlab.com/group/app.git",
        ]))

        targets, problems = read_repos(path)

        self.assertEqual([t.project_path for t in targets], ["group/app", "group/lib"])
        self.assertEqual(problems, [])

    def test_bad_lines_are_reported_with_line_numbers_and_skipped(self):
        path = self.write("repos.txt", "https://gitlab.com/group/app\nhello\n")

        targets, problems = read_repos(path)

        self.assertEqual(len(targets), 1)
        self.assertEqual(len(problems), 1)
        self.assertIn("第 2 行", problems[0])

    def test_reads_files_saved_with_a_bom(self):
        # Windows Notepad can save UTF-8 with a byte order mark.
        path = self.write("repos.txt", "https://gitlab.com/group/app\n", encoding="utf-8-sig")

        targets, _ = read_repos(path)

        self.assertEqual(targets[0].project_path, "group/app")


class ReadTokenTest(ConfigFileTestCase):
    def setUp(self):
        super().setUp()
        self._saved = os.environ.pop("GITLAB_TOKEN", None)

    def tearDown(self):
        os.environ.pop("GITLAB_TOKEN", None)
        if self._saved is not None:
            os.environ["GITLAB_TOKEN"] = self._saved
        super().tearDown()

    def test_reads_the_token_file_skipping_comments_and_whitespace(self):
        path = self.write("bot_account.config", "# uck729 的 token\n  glpat-abc  \n", encoding="utf-8-sig")

        self.assertEqual(read_token(path), "glpat-abc")

    def test_environment_variable_wins_over_the_file(self):
        os.environ["GITLAB_TOKEN"] = "glpat-env"

        self.assertEqual(read_token(self.write("bot_account.config", "glpat-file")), "glpat-env")

    def test_no_token_anywhere(self):
        self.assertIsNone(read_token(self.dir / "missing.config"))
        self.assertIsNone(read_token(self.write("bot_account.config", "# 還沒填\n\n")))


class ReadIntervalTest(ConfigFileTestCase):
    def test_accepts_whole_minutes_from_1_to_10(self):
        for text, minutes in [("1", 1), ("5\n", 5), (" 10 ", 10)]:
            self.assertEqual(read_interval_minutes(self.write("review.config", text)), minutes)

    def test_rejects_anything_else(self):
        for text in ["0", "11", "-3", "2.5", "five", ""]:
            with self.assertRaises(ValueError, msg=text):
                read_interval_minutes(self.write("review.config", text))


if __name__ == "__main__":
    unittest.main()
