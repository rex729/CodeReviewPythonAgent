"""A local clone of the watched project. Every read goes through git at a given revision,
so reviewing a merge request never touches the working tree."""

from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


class GitRepo:
    def __init__(self, path: Path, clone_url: str, token: str | None):
        self._path = path
        self._clone_url = clone_url
        self._env = os.environ | {"GIT_TERMINAL_PROMPT": "0"}
        if token:
            # Passed through the environment so the token never lands in .git/config or the process list.
            credentials = base64.b64encode(f"oauth2:{token}".encode()).decode()
            self._env |= {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credentials}",
            }

    def fetch_branch(self, branch: str) -> str:
        """Clones on first use, then fetches the branch. Returns the branch's latest commit."""
        self._ensure_cloned()
        ref = f"refs/remotes/origin/{branch}"
        self._git("fetch", "--quiet", "origin", f"+refs/heads/{branch}:{ref}")
        return self.rev_parse(ref)

    def fetch_merge_request(self, iid: int) -> str:
        """Fetches the MR's head (works for MRs from forks too). Returns its latest commit."""
        self._ensure_cloned()
        ref = f"refs/review/mr/{iid}"
        self._git("fetch", "--quiet", "origin", f"+refs/merge-requests/{iid}/head:{ref}")
        return self.rev_parse(ref)

    def rev_parse(self, rev: str) -> str:
        return self._git("rev-parse", rev).strip()

    def merge_base(self, a: str, b: str) -> str:
        return self._git("merge-base", a, b).strip()

    def list_files(self, rev: str) -> list[str]:
        return self._git("ls-tree", "-r", "--name-only", rev).splitlines()

    def read_file(self, rev: str, path: str) -> str:
        return self._git("show", f"{rev}:{path}")

    def changed_files(self, base: str, head: str) -> list[tuple[str, str]]:
        """(status, path) for files that differ, e.g. ("M", "src/app.py"). Renames show as delete + add."""
        changes = []
        for line in self._git("diff", "--name-status", "--no-renames", base, head).splitlines():
            status, path = line.split("\t", 1)
            changes.append((status, path))
        return changes

    def diff(self, base: str, head: str, path: str) -> str:
        return self._git("diff", "--unified=5", base, head, "--", path)

    def grep(self, rev: str, text: str, limit: int, regex: bool = False, word: bool = False) -> list[str]:
        """`path:line:content` for matching lines (binary files skipped). `text` is a literal unless
        `regex` (POSIX extended); `word` only matches whole words."""
        flags = ["-E" if regex else "--fixed-strings"] + (["-w"] if word else [])
        output = self._git("grep", "-n", "-I", *flags, "-e", text, rev, ok_codes=(0, 1))
        prefix = f"{rev}:"
        return [line.removeprefix(prefix) for line in output.splitlines()[:limit]]

    def _ensure_cloned(self) -> None:
        if not (self._path / ".git").exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._git("clone", "--quiet", "--no-checkout", self._clone_url, str(self._path), in_repo=False)

    def _git(self, *args: str, in_repo: bool = True, ok_codes: tuple[int, ...] = (0,)) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self._path if in_repo else None,
            env=self._env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode not in ok_codes:
            raise GitError(f"git {args[0]} 失敗：{result.stderr.strip()}")
        return result.stdout
