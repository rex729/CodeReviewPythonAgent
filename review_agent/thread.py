"""Posts review findings to one GitLab thread per merge request.

Each thread holds one complete review of one commit ("Start thread", not a plain comment). Its last
line names that commit. When the MR gets a new commit, the agent deletes its old thread and starts a
new one with the new review, so a thread never mixes findings from different commits. The thread
itself is the record of what was reviewed: notes carry a hidden marker and the reviewed commit, so
the agent finds them again even if its local state is lost."""

from __future__ import annotations

import re

from .gitlab import Discussion, GitLabClient, MergeRequest

# An HTML comment: GitLab renders nothing for it, but it identifies the agent's own notes.
MARKER = "<!-- ollama-review-agent -->"
# The last line of every note the agent posts, naming the commit it reviewed.
REVIEWED_PREFIX = "##agent reviewed"
REVIEWED_SHA_LENGTH = 12
_REVIEWED_LINE = re.compile(rf"^{REVIEWED_PREFIX} ([0-9a-f]+)$")

_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*\S)")


def split_points(review: str) -> list[str]:
    """The individual points in a review: one per bullet, with indented lines under it (e.g. a
    suggestion on the next line) joined on. A review with no bullets is one point."""
    points: list[str] = []
    for line in review.splitlines():
        if match := _BULLET.match(line):
            points.append(match.group(1))
        elif points and line.strip() and line[0].isspace():
            points[-1] += " " + line.strip()
    return points or ([review.strip()] if review.strip() else [])


_CITED_LOCATION = re.compile(r"^\[[^\]]*\]\s*`?([^\s`:]+):(\d+)")


def cited_path(point: str) -> str | None:
    """The file a point cites, from its `[嚴重度] path:line — ...` prefix."""
    match = _CITED_LOCATION.match(point)
    return match.group(1) if match else None


def cited_line(point: str) -> int | None:
    """The line number a point cites, from its `[嚴重度] path:line — ...` prefix."""
    match = _CITED_LOCATION.match(point)
    return int(match.group(2)) if match else None


def _normalize(point: str) -> str:
    # Line numbers and spacing or punctuation vary between files and runs.
    without_lines = re.sub(r":\d+(?:-\d+)?", "", point)
    return re.sub(r"[\W_]+", "", without_lines.lower())


def unique_points(points: list[str]) -> list[str]:
    """The points with exact repeats removed (ignoring line numbers, spacing and punctuation).

    The same defect reworded, e.g. one leak reported at both the allocation and the caller, is
    kept twice on purpose: asking qwen2.5-coder:7b to group rewordings sometimes grouped different
    defects and dropped real findings, and a repeated point costs less than a lost one."""
    seen = set()
    unique = []
    for point in points:
        key = _normalize(point)
        if key and key not in seen:
            seen.add(key)
            unique.append(point)
    return unique


class MergeRequestThread:
    def __init__(self, gitlab: GitLabClient, model: str):
        self._gitlab = gitlab
        self._model = model

    def last_reviewed_commit(self, iid: int) -> str | None:
        """The (abbreviated) commit named on the last line of the agent's latest note, if any."""
        own = [body for thread in self._own_threads(iid) for body in thread.note_bodies if MARKER in body]
        last_line = own[-1].rstrip().rsplit("\n", 1)[-1].strip() if own else ""
        match = _REVIEWED_LINE.match(last_line)
        return match.group(1) if match else None

    def publish(self, mr: MergeRequest, head: str, points: list[str]) -> str:
        """Replaces the agent's thread on the MR with one holding this review. Returns what happened."""
        deleted = 0
        for thread in self._own_threads(mr.iid):
            for note in thread.notes:
                if MARKER in note.body:  # People's replies stay; only the agent's own notes go.
                    self._gitlab.delete_note(mr.iid, note.id)
                    deleted += 1

        unique = unique_points(points)
        title = f"**AI code review**（{self._model}，commit `{head[:8]}`）"
        findings = "\n".join(f"- {point}" for point in unique) if unique else "沒有發現需要注意的地方。"
        reviewed = f"{REVIEWED_PREFIX} {head[:REVIEWED_SHA_LENGTH]}"
        self._gitlab.start_thread(mr.iid, f"{MARKER}\n{title}\n\n{findings}\n\n{reviewed}")

        replaced = f"刪掉舊的 thread（{deleted} 則留言），" if deleted else ""
        return f"{replaced}開了新的 thread，{len(unique)} 個注意事項"

    def _own_threads(self, iid: int) -> list[Discussion]:
        return [d for d in self._gitlab.discussions(iid) if d.notes and MARKER in d.notes[0].body]
