"""The agent's settings: repos.txt (which repositories to watch), review.config (minutes to wait
between rounds) and the optional config.toml (model and review options)."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

INTERVAL_MIN_MINUTES = 1
INTERVAL_MAX_MINUTES = 10

DEFAULT_INCLUDE_EXTENSIONS = (
    ".py", ".kt", ".kts", ".java", ".swift", ".m", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".sql", ".sh", ".ps1",
    ".gradle", ".xml", ".yaml", ".yml", ".toml",
)
DEFAULT_EXCLUDE_DIRS = (
    ".git", "build", ".gradle", ".idea", "node_modules", "dist", "out", "venv", ".venv",
    "__pycache__", "Binaries", "Intermediate", "Saved", "DerivedData", "Pods",
)

# scp-style SSH address, e.g. git@gitlab.com:group/project.git
_SCP_LIKE = re.compile(r"^(?:[\w.-]+@)?([\w.-]+):(?!//)(.+)$")


@dataclass(frozen=True)
class RepoTarget:
    url: str            # as written in repos.txt
    api_base_url: str   # e.g. https://gitlab.com
    project_path: str   # e.g. group/subgroup/project
    clone_url: str

    @property
    def host(self) -> str:
        return urlparse(self.api_base_url).netloc

    def workdir(self, root: Path) -> Path:
        """Where this repository's clone, review state and project overview cache live."""
        return root / "repos" / self.host / self.project_path


def parse_repo_url(text: str) -> RepoTarget:
    """Accepts HTTPS URLs and SSH addresses. SSH repos are cloned over SSH (your SSH key);
    the GitLab API is always reached over HTTPS on the same host."""
    url = text.strip()
    if url.startswith(("http://", "https://")):
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
        path = parsed.path.strip("/").removesuffix(".git")
        clone = f"{base}/{path}.git"
    elif url.startswith("ssh://"):
        parsed = urlparse(url)
        base = f"https://{parsed.hostname}"
        path = parsed.path.strip("/").removesuffix(".git")
        clone = url
    elif match := _SCP_LIKE.match(url):
        base = f"https://{match.group(1)}"
        path = match.group(2).strip("/").removesuffix(".git")
        clone = url
    else:
        raise ValueError(f"看不懂的 repo 網址：{url}")
    if "/" not in path:
        raise ValueError(f"網址缺少「群組/專案」路徑：{url}")
    return RepoTarget(url=url, api_base_url=base, project_path=path, clone_url=clone)


def read_repos(path: Path) -> tuple[list[RepoTarget], list[str]]:
    """Repositories listed in repos.txt, one URL per line; blank lines and `#` comments are skipped.
    Returns the valid ones (duplicates removed) and a message for each line that couldn't be used."""
    targets: dict[tuple[str, str], RepoTarget] = {}
    problems = []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            target = parse_repo_url(line)
        except ValueError as e:
            problems.append(f"{path.name} 第 {number} 行：{e}")
            continue
        targets.setdefault((target.host, target.project_path), target)
    return list(targets.values()), problems


def read_interval_minutes(path: Path) -> int:
    """The number in review.config: minutes to wait after a round finishes before starting the next."""
    text = path.read_text(encoding="utf-8-sig").strip()
    try:
        minutes = int(text)
    except ValueError:
        minutes = None
    if minutes is None or not INTERVAL_MIN_MINUTES <= minutes <= INTERVAL_MAX_MINUTES:
        raise ValueError(
            f"{path.name} 只能放 {INTERVAL_MIN_MINUTES} 到 {INTERVAL_MAX_MINUTES} 的整數（分鐘），現在是「{text}」"
        )
    return minutes


@dataclass(frozen=True)
class Settings:
    # Only review MRs into this branch; None reviews every open MR.
    target_branch: str | None
    # Post findings to a thread on the MR. Needs a token: GitLab doesn't allow anonymous comments.
    post_comments: bool
    gitlab_token: str | None
    ollama_host: str
    model: str
    num_ctx: int
    include_extensions: tuple[str, ...]
    exclude_dirs: tuple[str, ...]
    workdir: Path

    def is_reviewable(self, path: str) -> bool:
        """Whether a repository path is source code worth sending to the model."""
        parts = path.split("/")
        if any(part in self.exclude_dirs for part in parts[:-1]):
            return False
        return Path(parts[-1]).suffix.lower() in self.include_extensions


def read_token(token_file: Path) -> str | None:
    """The bot account's GitLab token: GITLAB_TOKEN if set, otherwise the first non-comment line
    of the token file (bot_account.config). None if neither has one."""
    token = os.environ.get("GITLAB_TOKEN", "").strip()
    if token:
        return token
    if not token_file.exists():
        return None
    for line in token_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return None


def load_settings(path: Path, token_file: Path) -> Settings:
    """Reads config.toml if it exists; every setting has a default."""
    raw = {}
    if path.exists():
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    gitlab = raw.get("gitlab", {})
    ollama = raw.get("ollama", {})
    review = raw.get("review", {})

    workdir = Path(raw.get("workdir", "workdir"))
    if not workdir.is_absolute():
        workdir = path.parent / workdir

    return Settings(
        target_branch=gitlab.get("target_branch") or None,
        post_comments=bool(gitlab.get("post_comments", True)),
        # The token stays out of config.toml so that file can be shared or committed.
        gitlab_token=read_token(token_file),
        ollama_host=ollama.get("host", "http://localhost:11434").rstrip("/"),
        model=ollama.get("model", "qwen2.5-coder:7b"),
        num_ctx=int(ollama.get("num_ctx", 16384)),
        include_extensions=tuple(e.lower() for e in review.get("include_extensions", DEFAULT_INCLUDE_EXTENSIONS)),
        exclude_dirs=tuple(review.get("exclude_dirs", DEFAULT_EXCLUDE_DIRS)),
        workdir=workdir,
    )
