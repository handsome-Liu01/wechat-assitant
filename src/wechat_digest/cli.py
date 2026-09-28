from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import platform
import sys
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .collector import create_collector
from .config import AppConfig, load_config
from .db import Database, MessageRecord
from .keywords import find_force_keyword
from .service import analyze_date, run_forever


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="微信群软件问题日报 Agent")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("doctor", help="检查运行环境和微信连接")
    commands.add_parser("collect", help="采集一次当前目标群消息")

    analyze = commands.add_parser("analyze", help="分析指定日期并生成 Excel")
    analyze.add_argument("--date", default="yesterday", help="YYYY-MM-DD 或 yesterday")
    analyze.add_argument("--rules-only", action="store_true", help="只导出关键词消息，不调用模型")

    importer = commands.add_parser("import-jsonl", help="导入 JSONL 消息，用于测试和补录")
    importer.add_argument("path", type=Path)

    commands.add_parser("run", help="持续采集，并在每天设定时间生成日报")
    return parser


def _logging(config: AppConfig) -> None:
    config.storage.log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_handler = RotatingFileHandler(
        config.storage.log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler])


def _report_date(value: str, config: AppConfig) -> date:
    if value == "yesterday":
        return datetime.now(config.tz).date() - timedelta(days=1)
    return date.fromisoformat(value)


def _doctor(config: AppConfig) -> int:
    print(f"Python: {sys.version.split()[0]}")
    print(f"系统: {platform.platform()}")
    print(f"配置: {config.config_path}")
    print(f"目标群: {config.group_name}")
    print(f"数据库: {config.storage.database_path}")
    print(f"模型密钥: {'已设置' if os.environ.get(config.llm.api_key_env) else '未设置'}")
    try:
        collector = create_collector(config)
        collector.connect()
        print(f"消息源: 成功，目标群校验通过（{collector.driver_name}）")
        close = getattr(collector, "close", None)
        if callable(close):
            close()
    except Exception as exc:
        print(f"消息源: 失败 - {exc}")
        return 3
    return 0


def _import_jsonl(config: AppConfig, database: Database, path: Path) -> int:
    records: list[MessageRecord] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        data = json.loads(line)
        sent_at = datetime.fromisoformat(str(data["sent_at"]))
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=config.tz)
        sender = str(data.get("sender", "未知成员"))
        content = str(data.get("content", ""))
        message_type = str(data.get("type", "text"))
        material = f"import|{config.group_name}|{line_number}|{sent_at.isoformat()}|{sender}|{content}"
        records.append(
            MessageRecord(
                fingerprint=hashlib.sha256(material.encode()).hexdigest(),
                group_name=config.group_name,
                sender=sender,
                sent_at=sent_at,
                observed_at=datetime.now(config.tz),
                message_type=message_type,
                content=content,
                forced_keyword=find_force_keyword(content, config.force_keywords),
                raw=data,
            )
        )
    inserted = database.insert_messages(records)
    print(f"已导入 {inserted} 条新消息")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except Exception as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 1
    _logging(config)
    database = Database(config.storage.database_path)
    database.init_schema()
    try:
        if args.command == "doctor":
            return _doctor(config)
        if args.command == "collect":
            collector = create_collector(config)
            try:
                collector.connect()
                records = collector.collect_once()
            finally:
                close = getattr(collector, "close", None)
                if callable(close):
                    close()
            print(f"消息源读取 {len(records)} 条，新增 {database.insert_messages(records)} 条")
            return 0
        if args.command == "analyze":
            collector = create_collector(config)
            try:
                collector.connect()
                path = analyze_date(
                    config,
                    database,
                    _report_date(args.date, config),
                    rules_only=args.rules_only,
                    collector=collector,
                )
            finally:
                close = getattr(collector, "close", None)
                if callable(close):
                    close()
            print(f"日报已生成: {path}")
            return 0
        if args.command == "import-jsonl":
            return _import_jsonl(config, database, args.path)
        if args.command == "run":
            run_forever(config, database)
            return 0
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logging.getLogger(__name__).exception("执行失败")
        print(f"执行失败: {exc}", file=sys.stderr)
        return 1
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())

