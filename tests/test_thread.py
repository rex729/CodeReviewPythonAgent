import unittest

from review_agent.gitlab import Discussion, MergeRequest, Note
from review_agent.thread import (
    MARKER, REVIEWED_PREFIX, MergeRequestThread, cited_path, split_points, unique_points,
)

MR = MergeRequest(iid=3, title="Discount", sha="s", target_branch="main", web_url="u")
DIVIDE = "[高] pricing.py:12 — 傳入空清單會除以零。建議：先檢查"
NAMING = "[低] checkout.py:4 — 變數名稱 x 不清楚。建議：改成 subtotal"
CALLER = "[中] checkout.py:9 — apply_discount 改成百分比，這裡還傳 0.1。建議：改傳 10"


class FakeGitLab:
    """Keeps discussions in memory the way GitLab returns them."""

    def __init__(self):
        self.threads: list[Discussion] = []
        self.started = 0
        self.deleted: list[int] = []
        self._next_note = 100

    def discussions(self, iid):
        return [Discussion(d.id, list(d.notes)) for d in self.threads if d.notes]

    def start_thread(self, iid, body):
        self.started += 1
        discussion_id = f"d{self.started}"
        self.threads.append(Discussion(discussion_id, [self._note(body)]))
        return discussion_id

    def delete_note(self, iid, note_id):
        self.deleted.append(note_id)
        for discussion in self.threads:
            discussion.notes[:] = [n for n in discussion.notes if n.id != note_id]

    def reply_by_person(self, discussion_index, body):
        self.threads[discussion_index].notes.append(self._note(body))

    def _note(self, body):
        self._next_note += 1
        return Note(self._next_note, body)

    def visible_threads(self):
        return [d for d in self.threads if d.notes]


def bullets(body):
    return [line[2:] for line in body.splitlines() if line.startswith("- ")]


class SplitPointsTest(unittest.TestCase):
    def test_one_point_per_bullet(self):
        review = f"- {DIVIDE}\n* {NAMING}\n1. {CALLER}\n說明文字"

        self.assertEqual(split_points(review), [DIVIDE, NAMING, CALLER])

    def test_text_without_bullets_is_one_point(self):
        self.assertEqual(split_points("  某個問題  "), ["某個問題"])
        self.assertEqual(split_points(""), [])

    def test_wrapped_lines_stay_with_their_point(self):
        review = "- [高] stats.cpp:8 — 陣列越界存取。\n  建議：改成 `i < values.size()`。\n\n- [低] stats.cpp:20 — 命名"

        self.assertEqual(split_points(review), [
            "[高] stats.cpp:8 — 陣列越界存取。 建議：改成 `i < values.size()`。",
            "[低] stats.cpp:20 — 命名",
        ])


class CitedPathTest(unittest.TestCase):
    def test_reads_the_path_after_the_severity(self):
        self.assertEqual(cited_path(DIVIDE), "pricing.py")
        self.assertEqual(cited_path("[中] `src/app/main.kt:3` — x"), "src/app/main.kt")
        self.assertIsNone(cited_path("沒有標位置的問題"))


class UniquePointsTest(unittest.TestCase):
    def test_repeats_differing_only_in_line_numbers_or_punctuation_are_dropped(self):
        repeats = [DIVIDE, DIVIDE.replace(":12", ":15"), DIVIDE.replace("。", "，"), NAMING]

        self.assertEqual(unique_points(repeats), [DIVIDE, NAMING])

    def test_the_same_defect_in_other_words_is_kept(self):
        # Accepted on purpose: merging rewordings needs a model, which dropped real findings.
        leak_caller = "[中] helloworld.cpp:12 — copy_values 回傳的指標沒有 delete[]"
        leak_impl = "[中] stats.cpp:14 — copy_values 用 new 配置的記憶體沒有釋放"

        self.assertEqual(unique_points([leak_caller, leak_impl]), [leak_caller, leak_impl])


class MergeRequestThreadTest(unittest.TestCase):
    def setUp(self):
        self.gitlab = FakeGitLab()

    def thread(self):
        # A fresh object each time: everything it knows must come from GitLab, like after a restart.
        return MergeRequestThread(self.gitlab, "qwen2.5-coder:7b")

    def test_starts_a_thread_with_one_bullet_per_point_ending_with_the_reviewed_commit(self):
        outcome = self.thread().publish(MR, "1234567890abcdef", [DIVIDE, NAMING])

        body = self.gitlab.threads[0].notes[0].body
        self.assertTrue(body.startswith(MARKER))
        self.assertIn("`12345678`", body)
        self.assertEqual(bullets(body), [DIVIDE, NAMING])
        self.assertEqual(body.rsplit("\n", 1)[-1], f"{REVIEWED_PREFIX} 1234567890ab")
        self.assertIn("開了新的 thread，2 個注意事項", outcome)

    def test_a_clean_review_still_starts_a_thread_so_the_commit_is_marked(self):
        self.thread().publish(MR, "1234567890abcdef", [])

        body = self.gitlab.threads[0].notes[0].body
        self.assertIn("沒有發現需要注意的地方", body)
        self.assertEqual(bullets(body), [])
        self.assertTrue(body.endswith(f"{REVIEWED_PREFIX} 1234567890ab"))

    def test_a_new_commit_replaces_the_old_thread(self):
        self.thread().publish(MR, "c1c1c1c1c1c1c1", [DIVIDE, NAMING])

        outcome = self.thread().publish(MR, "c2c2c2c2c2c2c2", [CALLER])

        threads = self.gitlab.visible_threads()
        self.assertEqual(len(threads), 1)
        self.assertEqual(bullets(threads[0].notes[0].body), [CALLER])
        self.assertTrue(threads[0].notes[0].body.endswith(f"{REVIEWED_PREFIX} c2c2c2c2c2c2"))
        self.assertIn("刪掉舊的 thread（1 則留言）", outcome)

    def test_peoples_replies_in_the_old_thread_are_left_alone(self):
        self.thread().publish(MR, "c1c1c1c1c1c1c1", [DIVIDE, NAMING])
        self.gitlab.reply_by_person(0, "這點我晚點修")
        person_note = self.gitlab.threads[0].notes[1].id

        self.thread().publish(MR, "c2c2c2c2c2c2c2", [CALLER])

        self.assertNotIn(person_note, self.gitlab.deleted)
        self.assertEqual(self.gitlab.threads[0].notes[0].body, "這點我晚點修")

    def test_other_peoples_threads_are_never_touched(self):
        self.gitlab.threads.append(Discussion("human", [Note(1, f"- 我自己發現的問題\n\n{REVIEWED_PREFIX} c1c1c1c1c1c1")]))

        self.thread().publish(MR, "c2c2c2c2c2c2c2", [CALLER])

        self.assertEqual(self.gitlab.deleted, [])

    def test_last_reviewed_commit_comes_from_the_agents_own_note(self):
        self.assertIsNone(self.thread().last_reviewed_commit(3))
        self.thread().publish(MR, "1234567890abcdef", [DIVIDE, NAMING])
        self.gitlab.reply_by_person(0, f"我修好了\n\n{REVIEWED_PREFIX} 999999999999")

        self.assertEqual(self.thread().last_reviewed_commit(3), "1234567890ab")

    def test_a_thread_without_the_reviewed_line_counts_as_not_reviewed(self):
        # Threads posted before the reviewed line existed.
        self.gitlab.threads.append(Discussion("old", [Note(1, f"{MARKER}\n**AI code review**\n\n- {DIVIDE}")]))

        self.assertIsNone(self.thread().last_reviewed_commit(3))


if __name__ == "__main__":
    unittest.main()
