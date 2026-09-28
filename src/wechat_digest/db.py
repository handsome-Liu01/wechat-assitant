from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class MessageRecord:
    fingerprint: str
    group_name: str
    sender: str
    sent_at: datetime
    observed_at: datetime
    message_type: str
    content: str
    forced_keyword: str | None = None
    source_id: str | None = None
    raw: dict | None = None


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self.connection.close()

    def init_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL UNIQUE,
                source_id TEXT,
                group_name TEXT NOT NULL,
                sender TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                message_type TEXT NOT NULL,
                content TEXT NOT NULL,
                forced_keyword TEXT,
                raw_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_messages_time
                ON messages(group_name, sent_at);

            CREATE TABLE IF NOT EXISTS analysis_runs (
                report_date TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                message_count INTEGER NOT NULL DEFAULT 0,
                issue_count INTEGER NOT NULL DEFAULT 0,
                report_path TEXT,
                last_error TEXT,
                updated_at TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def insert_messages(self, messages: Iterable[MessageRecord]) -> int:
        inserted = 0
        for message in messages:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO messages (
                    fingerprint, source_id, group_name, sender, sent_at,
                    observed_at, message_type, content, forced_keyword, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.fingerprint,
                    message.source_id,
                    message.group_name,
                    message.sender,
                    message.sent_at.isoformat(),
                    message.observed_at.isoformat(),
                    message.message_type,
                    message.content,
                    message.forced_keyword,
                    json.dumps(message.raw or {}, ensure_ascii=False, default=str),
                ),
            )
            inserted += cursor.rowcount
        self.connection.commit()
        return inserted

    def messages_between(self, group_name: str, start: datetime, end: datetime) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """
                SELECT * FROM messages
                WHERE group_name = ? AND sent_at >= ? AND sent_at < ?
                ORDER BY sent_at, id
                """,
                (group_name, start.isoformat(), end.isoformat()),
            )
        )

    def run_info(self, report_date: date) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM analysis_runs WHERE report_date = ?", (report_date.isoformat(),)
        ).fetchone()

    def should_run(self, report_date: date, now: datetime, retry_minutes: int) -> bool:
        row = self.run_info(report_date)
        if row is None:
            return True
        if row["status"] == "success":
            return False
        if row["status"] == "rules_only":
            return True
        updated = datetime.fromisoformat(row["updated_at"])
        return now - updated >= timedelta(minutes=retry_minutes)

    def mark_run(
        self,
        report_date: date,
        status: str,
        now: datetime,
        *,
        message_count: int = 0,
        issue_count: int = 0,
        report_path: str | None = None,
        error: str | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO analysis_runs (
                report_date, status, attempts, message_count, issue_count,
                report_path, last_error, updated_at
            ) VALUES (?, ?, 1, ?, ?, ?, ?, ?)
            ON CONFLICT(report_date) DO UPDATE SET
                status=excluded.status,
                attempts=analysis_runs.attempts + 1,
                message_count=excluded.message_count,
                issue_count=excluded.issue_count,
                report_path=excluded.report_path,
                last_error=excluded.last_error,
                updated_at=excluded.updated_at
            """,
            (
                report_date.isoformat(), status, message_count, issue_count,
                report_path, error, now.isoformat(),
            ),
        )
        self.connection.commit()

