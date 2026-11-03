"""Which merge requests have already been reviewed, kept on disk so a restart doesn't redo work."""

from __future__ import annotations

import json
from pathlib import Path


class StateStore:
    """Maps each open merge request's iid to the head commit last reviewed."""

    def __init__(self, path: Path):
        self._path = path

    def load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        return json.loads(self._path.read_text(encoding="utf-8")).get("merge_requests", {})

    def save(self, reviewed: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Write then rename, so a crash mid-write can't leave a half-written state file.
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"merge_requests": reviewed}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)
