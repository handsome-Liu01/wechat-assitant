from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml


@dataclass(frozen=True)
class CollectorConfig:
    driver: str = "wechat_data_analysis"
    poll_seconds: int = 300
    retry_seconds: int = 60
    history_message_count: int = 0
    weflow_base_url: str = "http://127.0.0.1:5031"
    weflow_api_token_env: str = "WEFLOW_API_TOKEN"
    weflow_session_id: str = ""
    weflow_page_size: int = 1000
    weflow_timeout_seconds: int = 30
    lookback_hours: int = 48
    wda_base_url: str = "http://127.0.0.1:10392"
    wda_account: str = ""
    wda_session_id: str = ""
    wda_source: str = "auto"
    wda_page_size: int = 500
    wda_timeout_seconds: int = 60


@dataclass(frozen=True)
class LLMConfig:
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "LLM_API_KEY"
    model: str = ""
    timeout_seconds: int = 120
    max_chunk_characters: int = 24000


@dataclass(frozen=True)
class ScheduleConfig:
    hour: int = 10
    minute: int = 0
    failed_retry_minutes: int = 60


@dataclass(frozen=True)
class StorageConfig:
    database_path: Path = Path("data/agent.db")
    report_dir: Path = Path("reports")
    log_path: Path = Path("logs/agent.log")


@dataclass(frozen=True)
class PrivacyConfig:
    hash_senders: bool = False
    sender_hash_salt_env: str = "SENDER_HASH_SALT"


@dataclass(frozen=True)
class AppConfig:
    group_name: str
    timezone: str = "Asia/Shanghai"
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    force_keywords: tuple[str, ...] = ("#重要", "#阻塞", "[P0]", "[P1]")
    llm: LLMConfig = field(default_factory=LLMConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    config_path: Path = Path("config.yaml")

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"配置项 {name} 必须是对象")
    return value


def _resolve(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("配置文件顶层必须是对象")

    group_name = str(data.get("group_name", "")).strip()
    if not group_name:
        raise ValueError("group_name 不能为空")

    collector = _section(data, "collector")
    llm = _section(data, "llm")
    schedule = _section(data, "schedule")
    storage = _section(data, "storage")
    privacy = _section(data, "privacy")
    keywords = _section(data, "keywords")
    base = config_path.parent

    result = AppConfig(
        group_name=group_name,
        timezone=str(data.get("timezone", "Asia/Shanghai")),
        collector=CollectorConfig(
            driver=str(collector.get("driver", "wechat_data_analysis")),
            poll_seconds=int(collector.get("poll_seconds", 300)),
            retry_seconds=int(collector.get("retry_seconds", 60)),
            history_message_count=int(collector.get("history_message_count", 0)),
            weflow_base_url=str(collector.get("weflow_base_url", "http://127.0.0.1:5031")),
            weflow_api_token_env=str(collector.get("weflow_api_token_env", "WEFLOW_API_TOKEN")),
            weflow_session_id=str(collector.get("weflow_session_id", "")),
            weflow_page_size=int(collector.get("weflow_page_size", 1000)),
            weflow_timeout_seconds=int(collector.get("weflow_timeout_seconds", 30)),
            lookback_hours=int(collector.get("lookback_hours", 48)),
            wda_base_url=str(collector.get("wda_base_url", "http://127.0.0.1:10392")),
            wda_account=str(collector.get("wda_account", "")),
            wda_session_id=str(collector.get("wda_session_id", "")),
            wda_source=str(collector.get("wda_source", "auto")),
            wda_page_size=int(collector.get("wda_page_size", 500)),
            wda_timeout_seconds=int(collector.get("wda_timeout_seconds", 60)),
        ),
        force_keywords=tuple(str(x) for x in keywords.get("force_include", [])),
        llm=LLMConfig(
            base_url=str(llm.get("base_url", "https://api.openai.com/v1")),
            api_key_env=str(llm.get("api_key_env", "LLM_API_KEY")),
            model=str(llm.get("model", "")),
            timeout_seconds=int(llm.get("timeout_seconds", 120)),
            max_chunk_characters=int(llm.get("max_chunk_characters", 24000)),
        ),
        schedule=ScheduleConfig(
            hour=int(schedule.get("hour", 10)),
            minute=int(schedule.get("minute", 0)),
            failed_retry_minutes=int(schedule.get("failed_retry_minutes", 60)),
        ),
        storage=StorageConfig(
            database_path=_resolve(base, str(storage.get("database_path", "data/agent.db"))),
            report_dir=_resolve(base, str(storage.get("report_dir", "reports"))),
            log_path=_resolve(base, str(storage.get("log_path", "logs/agent.log"))),
        ),
        privacy=PrivacyConfig(
            hash_senders=bool(privacy.get("hash_senders", False)),
            sender_hash_salt_env=str(privacy.get("sender_hash_salt_env", "SENDER_HASH_SALT")),
        ),
        config_path=config_path,
    )
    _validate(result)
    return result


def _validate(config: AppConfig) -> None:
    ZoneInfo(config.timezone)
    if config.collector.poll_seconds < 2:
        raise ValueError("collector.poll_seconds 不能小于 2 秒")
    if config.collector.driver not in {"wechat_data_analysis", "weflow", "auto", "free", "plus"}:
        raise ValueError(
            "collector.driver 只能是 wechat_data_analysis、weflow、auto、free 或 plus"
        )
    if not 1 <= config.collector.weflow_page_size <= 10000:
        raise ValueError("collector.weflow_page_size 必须在 1 到 10000 之间")
    if config.collector.lookback_hours < 1:
        raise ValueError("collector.lookback_hours 不能小于 1")
    if not 1 <= config.collector.wda_page_size <= 500:
        raise ValueError("collector.wda_page_size 必须在 1 到 500 之间")
    if config.collector.wda_source not in {"auto", "decrypted", "realtime"}:
        raise ValueError("collector.wda_source 只能是 auto、decrypted 或 realtime")
    if not 0 <= config.schedule.hour <= 23 or not 0 <= config.schedule.minute <= 59:
        raise ValueError("schedule.hour/minute 超出范围")
    if config.llm.max_chunk_characters < 1000:
        raise ValueError("llm.max_chunk_characters 不能小于 1000")

