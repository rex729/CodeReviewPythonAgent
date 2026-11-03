"""The slice of the GitLab REST API the agent needs: open merge requests and their discussion threads."""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

PAGE_SIZE = 100


class GitLabError(RuntimeError):
    pass


@dataclass(frozen=True)
class MergeRequest:
    iid: int
    title: str
    sha: str
    target_branch: str
    web_url: str
    description: str = ""


@dataclass(frozen=True)
class Note:
    id: int
    body: str


@dataclass(frozen=True)
class Discussion:
    id: str
    notes: list[Note]

    @property
    def note_bodies(self) -> list[str]:
        return [note.body for note in self.notes]


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
            for item in self._request("GET", "/merge_requests", params=params)
        ]

    def discussions(self, iid: int) -> list[Discussion]:
        """Every discussion on the merge request, oldest first."""
        discussions = []
        page = 1
        while True:
            batch = self._request("GET", f"/merge_requests/{iid}/discussions", params={"per_page": PAGE_SIZE, "page": page})
            discussions += [
                Discussion(id=item["id"], notes=[Note(note["id"], note["body"]) for note in item.get("notes", [])])
                for item in batch
            ]
            if len(batch) < PAGE_SIZE:
                return discussions
            page += 1

    def start_thread(self, iid: int, body: str) -> str:
        """Starts a resolvable thread (the "Start thread" button, not a plain comment). Returns its id."""
        return self._request("POST", f"/merge_requests/{iid}/discussions", payload={"body": body})["id"]

    def delete_note(self, iid: int, note_id: int) -> None:
        """Deletes a note; a thread disappears once all its notes are gone. Only the token's own
        notes can be deleted (unless the account maintains the project)."""
        self._request("DELETE", f"/merge_requests/{iid}/notes/{note_id}")

    def _request(self, method: str, endpoint: str, params: dict | None = None, payload: dict | None = None):
        url = f"{self._project_api}{endpoint}" + (f"?{urlencode(params)}" if params else "")
        headers = dict(self._headers)
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        try:
            with urlopen(Request(url, data=data, headers=headers, method=method), timeout=self._timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else None  # DELETE answers 204 with no body
        except HTTPError as e:
            raise GitLabError(_explain(e.code, method)) from e


def _explain(status: int, method: str) -> str:
    if status in (401, 403):
        need = "api" if method != "GET" else "read_api"
        return f"GitLab 拒絕存取（HTTP {status}）：GITLAB_TOKEN 需要 {need} 權限，而且帳號要能存取這個專案"
    if status == 404:
        return "GitLab 找不到這個專案或 MR（HTTP 404）：私有專案需要設定 GITLAB_TOKEN"
    return f"GitLab 回傳錯誤（HTTP {status}）"
