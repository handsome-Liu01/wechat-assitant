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

            CREATE TABLE IF NOT EXISTS feishu_sync (
                sync_key TEXT PRIMARY KEY,
                report_date TEXT NOT NULL,
                record_id TEXT NOT NULL,
                synced_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS github_issue_runs (
                source_record_id TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                decision TEXT NOT NULL,
                repository TEXT,
                confidence REAL,
                issue_number INTEGER,
                issue_url TEXT,
                details_json TEXT,
                evaluated_at TEXT NOT NULL,
                PRIMARY KEY (source_record_id, source_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_github_issue_record
                ON github_issue_runs(source_record_id, decision);
            """
        )
        self.connection.commit()

    def feishu_record_id(self, sync_key: str) -> str | None:
        row = self.connection.execute(
            "SELECT record_id FROM feishu_sync WHERE sync_key = ?", (sync_key,)
        ).fetchone()
        return str(row["record_id"]) if row else None

    def mark_feishu_synced(
        self, sync_key: str, report_date: date, record_id: str, synced_at: datetime
    ) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO feishu_sync (sync_key, report_date, record_id, synced_at)
            VALUES (?, ?, ?, ?)
            """,
            (sync_key, report_date.isoformat(), record_id, synced_at.isoformat()),
        )
        self.connection.commit()

    def github_issue_result(self, source_record_id: str, source_hash: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT * FROM github_issue_runs
            WHERE source_record_id = ? AND source_hash = ?
            """,
            (source_record_id, source_hash),
        ).fetchone()

    def github_created_issue(self, source_record_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT * FROM github_issue_runs
            WHERE source_record_id = ? AND decision IN ('created', 'existing')
            ORDER BY evaluated_at DESC LIMIT 1
            """,
            (source_record_id,),
        ).fetchone()

    def mark_github_issue_result(
        self,
        source_record_id: str,
        source_hash: str,
        decision: str,
        evaluated_at: datetime,
        *,
        repository: str | None = None,
        confidence: float | None = None,
        issue_number: int | None = None,
        issue_url: str | None = None,
        details: dict | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO github_issue_runs (
                source_record_id, source_hash, decision, repository, confidence,
                issue_number, issue_url, details_json, evaluated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_record_id, source_hash) DO UPDATE SET
                decision=excluded.decision,
                repository=excluded.repository,
                confidence=excluded.confidence,
                issue_number=excluded.issue_number,
                issue_url=excluded.issue_url,
                details_json=excluded.details_json,
                evaluated_at=excluded.evaluated_at
            """,
            (
                source_record_id,
                source_hash,
                decision,
                repository,
                confidence,
                issue_number,
                issue_url,
                json.dumps(details or {}, ensure_ascii=False, default=str),
                evaluated_at.isoformat(),
            ),
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

