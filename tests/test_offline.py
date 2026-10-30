"""Offline checks for learning, dedup and comment tracking. Run: .venv/bin/python -m unittest discover tests"""

import os
import tempfile
import unittest
from unittest import mock

os.environ["GREPTILE_SUB_DB"] = os.path.join(tempfile.mkdtemp(), "reviews.db")

from greptile_sub import cli, github, learn, store  # noqa: E402
from greptile_sub.review import Review, comment_body, duplicate_of  # noqa: E402

FINDING = {
    "path": "src/app.py", "line": 42, "severity": "major", "category": "bug",
    "title": "Division by zero when count is empty", "body": "`total / count` fails when count is 0.",
}


def gh_comment(**overrides):
    c = {"id": 1, "path": "src/app.py", "line": 42, "original_line": 42, "body": comment_body(FINDING),
         "reactions": {"+1": 0, "-1": 0}, "in_reply_to_id": None, "html_url": "https://x/1",
         "user": {"login": "alicin"}}
    c.update(overrides)
    return c


def never_judge(*_):
    raise AssertionError("judge should not run")


class ClassifyTest(unittest.TestCase):
    def test_signals_in_priority_order(self):
        cases = [
            (None, [], "OPEN", "dismissed"),
            (gh_comment(reactions={"+1": 0, "-1": 1}), [], "OPEN", "dismissed"),
            (gh_comment(reactions={"+1": 1, "-1": 0}), [], "OPEN", "acknowledged"),
            (gh_comment(line=None), [], "OPEN", "addressed"),
            (gh_comment(), [], "MERGED", "ignored"),
            (gh_comment(), [], "OPEN", None),
        ]
        for comment, replies, state, expected in cases:
            self.assertEqual(learn.classify(comment, replies, 0, state, judge=never_judge)[0], expected)

    def test_replies_are_judged_once(self):
        reply = gh_comment(id=2, in_reply_to_id=1, body="This is intended, the caller guards it.")
        judge = mock.Mock(return_value=("dismissed", "intended; caller guards it"))
        self.assertEqual(learn.classify(gh_comment(), [reply], 0, "OPEN", judge=judge)[0], "dismissed")
        self.assertEqual(learn.classify(gh_comment(), [reply], 1, "OPEN", judge=judge)[0], None)
        judge.assert_called_once()


class DedupTest(unittest.TestCase):
    def test_near_duplicate_on_nearby_line(self):
        prior = [gh_comment(line=40)]
        self.assertIsNotNone(duplicate_of({**FINDING, "title": "Possible division by zero if count is empty"}, prior))

    def test_different_issue_or_outdated_is_not_duplicate(self):
        self.assertIsNone(duplicate_of({**FINDING, "title": "SQL built with string formatting"}, [gh_comment()]))
        self.assertIsNone(duplicate_of(FINDING, [gh_comment(line=None)]))
        self.assertIsNone(duplicate_of(FINDING, [gh_comment(line=60)]))


class LoopTest(unittest.TestCase):
    def test_post_track_collect_and_learn(self):
        db = store.connect()
        review = Review(repo="o/r", number=7, head_sha="abc", summary="s", inline=[dict(FINDING)])
        with mock.patch.object(github, "post_review", return_value={"id": 99, "html_url": "https://x/r"}), \
             mock.patch.object(github, "review_comments", return_value=[gh_comment(id=555)]):
            cli.publish(db, review, post=True)
        self.assertEqual([r["id"] for r in store.pending_comments(db, "o/r")], [555])

        reply = gh_comment(id=556, in_reply_to_id=555, body="Not a bug: count is validated upstream.")
        with mock.patch.object(github, "pr_state", return_value="OPEN"), \
             mock.patch.object(github, "pr_review_comments", return_value=[gh_comment(id=555), reply]), \
             mock.patch.object(learn, "judge_replies", return_value=("dismissed", "count is validated upstream")):
            self.assertEqual(learn.collect(db, "o/r"), 1)
        self.assertEqual(store.outcome_counts(db, "o/r"), {"dismissed": 1})
        self.assertIn("count is validated upstream", learn.lessons(db, "o/r")[0])
        self.assertEqual(store.reviewed_shas(db, "o/r", 7), {"abc"})

    def test_rereview_with_nothing_new_is_not_posted(self):
        review = Review(repo="o/r", number=8, head_sha="def", summary="s", is_rereview=True)
        with mock.patch.object(github, "post_review") as post:
            cli.publish(store.connect(), review, post=True)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
