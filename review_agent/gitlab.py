"""The small slice of the GitLab REST API the agent needs: listing open merge requests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class MergeRequest:
    iid: int
    title: str
    sha: str
    target_branch: str
    web_url: str
    description: str = ""


class GitLabClient:
    def __init__(self, base_url: str, project_path: str, token: str | None, timeout: float = 30):
        self._project_api = f"{base_url}/api/v4/projects/{quote(project_path, safe='')}"
        self._headers = {"PRIVATE-TOKEN": token} if token else {}
        self._timeout = timeout

    def open_merge_requests(self, target_branch: str | None = None) -> list[MergeRequest]:
        params = {"state": "opened", "per_page": 50}
        if target_branch:
            params["target_branch"] = target_branch
        return [
            MergeRequest(
                iid=item["iid"], title=item["title"], sha=item["sha"],
                target_branch=item["target_branch"], web_url=item["web_url"],
                description=item.get("description") or "",
            )
            for item in self._get("/merge_requests", params)
        ]

    def _get(self, endpoint: str, params: dict) -> list | dict:
        url = f"{self._project_api}{endpoint}?{urlencode(params)}"
        with urlopen(Request(url, headers=self._headers), timeout=self._timeout) as response:
            return json.load(response)
