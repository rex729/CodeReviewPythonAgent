"""Loads config.toml into a typed Config."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_INCLUDE_EXTENSIONS = (
    ".py", ".kt", ".kts", ".java", ".swift", ".m", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".sql", ".sh", ".ps1",
    ".gradle", ".xml", ".yaml", ".yml", ".toml",
)
DEFAULT_EXCLUDE_DIRS = (
    ".git", "build", ".gradle", ".idea", "node_modules", "dist", "out", "venv", ".venv",
    "__pycache__", "Binaries", "Intermediate", "Saved", "DerivedData", "Pods",
)


@dataclass(frozen=True)
class Config:
    project_url: str
    # Only review MRs into this branch; None reviews every open MR.
    target_branch: str | None
    poll_seconds: int
    gitlab_token: str | None
    ollama_host: str
    model: str
    num_ctx: int
    include_extensions: tuple[str, ...]
    exclude_dirs: tuple[str, ...]
    workdir: Path

    @property
    def gitlab_base_url(self) -> str:
        parsed = urlparse(self.project_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def project_path(self) -> str:
        """`group/subgroup/project` from the project's web URL."""
        return urlparse(self.project_url).path.strip("/").removesuffix(".git")

    @property
    def clone_url(self) -> str:
        return f"{self.gitlab_base_url}/{self.project_path}.git"

    @property
    def repo_dir(self) -> Path:
        return self.workdir / "repo"

    @property
    def state_file(self) -> Path:
        return self.workdir / "state.json"

    @property
    def reports_dir(self) -> Path:
        return self.workdir / "reports"

    def is_reviewable(self, path: str) -> bool:
        """Whether a repository path is source code worth sending to the model."""
        parts = path.split("/")
        if any(part in self.exclude_dirs for part in parts[:-1]):
            return False
        return Path(parts[-1]).suffix.lower() in self.include_extensions


def load_config(path: Path) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    gitlab = raw.get("gitlab", {})
    ollama = raw.get("ollama", {})
    review = raw.get("review", {})

    project_url = gitlab.get("project_url", "").strip()
    if not project_url.startswith(("http://", "https://")):
        raise ValueError("config.toml 的 gitlab.project_url 必須是專案網址，例如 https://gitlab.com/group/project")

    workdir = Path(raw.get("workdir", "workdir"))
    if not workdir.is_absolute():
        workdir = path.parent / workdir

    return Config(
        project_url=project_url,
        target_branch=gitlab.get("target_branch") or None,
        poll_seconds=int(gitlab.get("poll_seconds", 300)),
        # The token stays out of the config file so the file can be shared or committed.
        gitlab_token=os.environ.get("GITLAB_TOKEN") or None,
        ollama_host=ollama.get("host", "http://localhost:11434").rstrip("/"),
        model=ollama.get("model", "qwen2.5-coder:7b"),
        num_ctx=int(ollama.get("num_ctx", 16384)),
        include_extensions=tuple(e.lower() for e in review.get("include_extensions", DEFAULT_INCLUDE_EXTENSIONS)),
        exclude_dirs=tuple(review.get("exclude_dirs", DEFAULT_EXCLUDE_DIRS)),
        workdir=workdir,
    )
