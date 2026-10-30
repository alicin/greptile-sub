import json
import os
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from greptile_sub.review import Review

DB_PATH = Path(os.getenv("GREPTILE_SUB_DB", Path.home() / ".local/state/greptile-sub/reviews.db"))


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """CREATE TABLE IF NOT EXISTS reviews (
            repo TEXT NOT NULL,
            number INTEGER NOT NULL,
            head_sha TEXT NOT NULL,
            reviewed_at REAL NOT NULL,
            review_url TEXT,
            review_json TEXT NOT NULL,
            PRIMARY KEY (repo, number, head_sha)
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY,
            repo TEXT NOT NULL,
            number INTEGER NOT NULL,
            head_sha TEXT NOT NULL,
            path TEXT NOT NULL,
            line INTEGER NOT NULL,
            severity TEXT NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            posted_at REAL NOT NULL,
            outcome TEXT,
            outcome_reason TEXT,
            replies_seen INTEGER NOT NULL DEFAULT 0,
            decided_at REAL
        )"""
    )
    db.row_factory = sqlite3.Row
    return db


def reviewed_shas(db: sqlite3.Connection, repo: str, number: int) -> set[str]:
    rows = db.execute("SELECT head_sha FROM reviews WHERE repo = ? AND number = ?", (repo, number))
    return {sha for (sha,) in rows}


def record(db: sqlite3.Connection, review: Review, review_url: Optional[str]) -> None:
    db.execute(
        "INSERT OR REPLACE INTO reviews VALUES (?, ?, ?, ?, ?, ?)",
        (review.repo, review.number, review.head_sha, time.time(), review_url, json.dumps(asdict(review))),
    )
    db.commit()


def record_comment(db: sqlite3.Connection, comment_id: int, review: Review, finding: dict) -> None:
    db.execute(
        """INSERT OR IGNORE INTO comments
           (id, repo, number, head_sha, path, line, severity, category, title, body, posted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (comment_id, review.repo, review.number, review.head_sha, finding["path"], finding["line"],
         finding["severity"], finding["category"], finding["title"], finding["body"], time.time()),
    )
    db.commit()


def pending_comments(db: sqlite3.Connection, repo: str) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM comments WHERE repo = ? AND outcome IS NULL ORDER BY number", (repo,)
    ).fetchall()


def set_outcome(db: sqlite3.Connection, comment_id: int, outcome: Optional[str], reason: str,
                replies_seen: int) -> None:
    db.execute(
        """UPDATE comments SET outcome = ?, outcome_reason = ?, replies_seen = ?,
           decided_at = CASE WHEN ? IS NULL THEN NULL ELSE ? END WHERE id = ?""",
        (outcome, reason, replies_seen, outcome, time.time(), comment_id),
    )
    db.commit()


def dismissed(db: sqlite3.Connection, repo: str, limit: int = 25) -> list[sqlite3.Row]:
    return db.execute(
        """SELECT path, severity, category, title, outcome_reason FROM comments
           WHERE repo = ? AND outcome = 'dismissed' ORDER BY decided_at DESC LIMIT ?""",
        (repo, limit),
    ).fetchall()


def outcome_counts(db: sqlite3.Connection, repo: str) -> dict[str, int]:
    rows = db.execute(
        "SELECT COALESCE(outcome, 'pending'), COUNT(*) FROM comments WHERE repo = ? GROUP BY 1", (repo,)
    )
    return {outcome: n for outcome, n in rows}
