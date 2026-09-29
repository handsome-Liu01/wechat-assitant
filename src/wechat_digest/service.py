from __future__ import annotations

import hashlib
import logging
import time
from datetime import date, datetime, time as wall_time, timedelta

from .analyzer import OpenAICompatibleLLM, analyze_rows
from .collector import create_collector
from .config import AppConfig
from .db import Database
from .feishu import FeishuClient
from .github_issues import run_github_issue_triage
from .report import write_report


def day_window(report_date: date, config: AppConfig) -> tuple[datetime, datetime]:
    start = datetime.combine(report_date, wall_time.min, tzinfo=config.tz)
    return start, start + timedelta(days=1)


def analyze_date(
    config: AppConfig,
    database: Database,
    report_date: date,
    *,
    rules_only: bool = False,
    collector=None,
) -> str:
    start, end = day_window(report_date, config)
    if collector is not None and hasattr(collector, "collect_range"):
        pulled = collector.collect_range(start, end)
        database.insert_messages(pulled)
    rows = database.messages_between(config.group_name, start, end)
    llm = None if rules_only else OpenAICompatibleLLM(config.llm)
    issues = analyze_rows(rows, report_date.isoformat(), llm, config.llm.max_chunk_characters)
    report = write_report(issues, report_date, config.storage.report_dir, message_count=len(rows))
    if config.feishu.enabled:
        _sync_feishu(config, database, report_date, issues)
    if config.github_issues.enabled and not rules_only:
        triage_results = run_github_issue_triage(config, database)
        logging.getLogger(__name__).info(
            "GitHub Issue 自动分拣完成: %s 条结果", len(triage_results)
        )
    database.mark_run(
        report_date,
        "rules_only" if rules_only else "success",
        datetime.now(config.tz),
        message_count=len(rows),
        issue_count=len(issues),
        report_path=str(report),
    )
    return str(report)


def _sync_key(issue) -> str:
    evidence = ",".join(str(value) for value in sorted(set(issue.evidence_ids)))
    material = f"{issue.date}|{evidence}|{int(issue.forced)}"
    if not evidence:
        material += f"|{issue.title}|{issue.description}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _sync_feishu(config: AppConfig, database: Database, report_date: date, issues) -> None:
    pending = [(issue, _sync_key(issue)) for issue in issues]
    pending = [item for item in pending if database.feishu_record_id(item[1]) is None]
    if not pending:
        return
    with FeishuClient(config.feishu) as client:
        fields = client.list_fields()
        for issue, sync_key in pending:
            record_id = client.create_issue(issue, fields)
            database.mark_feishu_synced(
                sync_key, report_date, record_id, datetime.now(config.tz)
            )


def run_forever(config: AppConfig, database: Database) -> None:
    logger = logging.getLogger(__name__)
    collector = create_collector(config)
    try:
        while True:
            try:
                collector.connect()
                break
            except Exception:
                logger.exception(
                    "消息源尚未就绪，将在 %s 秒后重新连接", config.collector.retry_seconds
                )
                time.sleep(config.collector.retry_seconds)
        while True:
            try:
                records = collector.collect_once()
                inserted = database.insert_messages(records)
                if inserted:
                    logger.info("采集到 %s 条新消息", inserted)
            except Exception:
                logger.exception("消息采集失败，将在 %s 秒后重试", config.collector.retry_seconds)
                time.sleep(config.collector.retry_seconds)
                continue

            now = datetime.now(config.tz)
            scheduled = now.replace(
                hour=config.schedule.hour,
                minute=config.schedule.minute,
                second=0,
                microsecond=0,
            )
            if now >= scheduled:
                report_date = now.date() - timedelta(days=1)
                if database.should_run(report_date, now, config.schedule.failed_retry_minutes):
                    try:
                        path = analyze_date(config, database, report_date, collector=collector)
                        logger.info("日报已生成: %s", path)
                    except Exception as exc:
                        logger.exception("日报生成失败")
                        database.mark_run(report_date, "failed", now, error=str(exc))
            time.sleep(config.collector.poll_seconds)
    finally:
        close = getattr(collector, "close", None)
        if callable(close):
            close()

