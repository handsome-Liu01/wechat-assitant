from __future__ import annotations

import hashlib
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .config import AppConfig
from .db import MessageRecord
from .keywords import find_force_keyword


class CollectorError(RuntimeError):
    pass


def create_collector(config: AppConfig):
    if config.collector.driver == "wechat_data_analysis":
        from .wechat_data_analysis import WeChatDataAnalysisCollector

        return WeChatDataAnalysisCollector(config)
    if config.collector.driver == "weflow":
        from .weflow import WeFlowCollector

        return WeFlowCollector(config)
    return WxAutoCollector(config)


def _parse_time(value: Any, now: datetime, tz: ZoneInfo) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or tz).astimezone(tz)
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("年", "-").replace("月", "-").replace("日", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if text.startswith("昨天"):
        clock = text.replace("昨天", "").strip()
        try:
            parsed = datetime.strptime(clock, "%H:%M")
            yesterday = now.date() - timedelta(days=1)
            return datetime.combine(yesterday, parsed.time(), tzinfo=tz)
        except ValueError:
            return None
    formats = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m-%d %H:%M", "%H:%M")
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == "%m-%d %H:%M":
            parsed = parsed.replace(year=now.year)
        elif fmt == "%H:%M":
            parsed = datetime.combine(now.date(), parsed.time())
        return parsed.replace(tzinfo=tz)
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.replace(tzinfo=parsed.tzinfo or tz).astimezone(tz)
    except ValueError:
        return None


def _safe_sender(sender: str, config: AppConfig) -> str:
    if not config.privacy.hash_senders:
        return sender
    salt = os.environ.get(config.privacy.sender_hash_salt_env, "")
    if not salt:
        raise CollectorError(
            f"已启用 hash_senders，但环境变量 {config.privacy.sender_hash_salt_env} 未设置"
        )
    digest = hashlib.sha256(f"{salt}:{sender}".encode("utf-8")).hexdigest()[:12]
    return f"用户-{digest}"


class WxAutoCollector:
    """Non-injection collector based on the logged-in Windows WeChat UI."""

    def __init__(self, config: AppConfig):
        self.config = config
        self._wx: Any = None
        self.driver_name: str | None = None
        self._last_anchor: datetime | None = None
        self._history_loaded = False

    def connect(self) -> None:
        WeChat = self._load_driver()
        try:
            self._wx = WeChat(resize=True)
            self._wx.ChatWith(self.config.group_name, exact=True)
            info = self._wx.ChatInfo()
        except Exception as exc:
            raise CollectorError(f"无法连接微信或打开目标群: {exc}") from exc
        actual = str((info or {}).get("chat_name", ""))
        chat_type = str((info or {}).get("chat_type", ""))
        if actual != self.config.group_name or chat_type != "group":
            raise CollectorError(
                f"目标校验失败，期望群聊 {self.config.group_name!r}，实际为 {info!r}"
            )

    def _load_driver(self):
        requested = self.config.collector.driver
        errors: list[str] = []
        if requested in {"auto", "plus"}:
            try:
                from wxautox4 import WeChat

                self.driver_name = "plus"
                return WeChat
            except ImportError as exc:
                errors.append(f"wxautox4: {exc}")
        if requested in {"auto", "free"}:
            try:
                from wxauto4 import WeChat

                self.driver_name = "free"
                return WeChat
            except ImportError as exc:
                errors.append(f"wxauto4: {exc}")
        raise CollectorError(
            "未安装可用的微信采集驱动。增强版执行 pip install -e .[wechat-plus]；"
            "兼容旧版微信的免费版执行 pip install -e .[wechat-free]。详情: "
            + "; ".join(errors)
        )

    def collect_once(self, now: datetime | None = None) -> list[MessageRecord]:
        if self._wx is None:
            self.connect()
        now = now or datetime.now(self.config.tz)
        try:
            self._wx.ChatWith(self.config.group_name, exact=True)
            messages = self._get_messages()
        except Exception as exc:
            self._wx = None
            raise CollectorError(f"读取微信消息失败: {exc}") from exc
        return normalize_wx_messages(messages, self.config, now, self)

    def _get_messages(self) -> list[Any]:
        history_count = self.config.collector.history_message_count
        if history_count > 0 and not self._history_loaded:
            method = getattr(self._wx, "GetHistoryMessage", None)
            if callable(method):
                self._history_loaded = True
                return list(method(n=history_count))
        return list(self._wx.GetAllMessage())


def normalize_wx_messages(
    messages: Iterable[Any], config: AppConfig, now: datetime, state: Any | None = None
) -> list[MessageRecord]:
    anchor = getattr(state, "_last_anchor", None) or now.replace(second=0, microsecond=0)
    occurrences: defaultdict[tuple[str, str, str, str], int] = defaultdict(int)
    records: list[MessageRecord] = []

    for message in messages:
        message_type = str(getattr(message, "type", "other") or "other")
        if message_type == "time":
            parsed = _parse_time(getattr(message, "time", None), now, config.tz)
            if parsed:
                anchor = parsed
            continue
        attr = str(getattr(message, "attr", "other") or "other")
        if attr == "system":
            continue
        sender = str(getattr(message, "sender", "") or ("我" if attr == "self" else "未知成员"))
        sender = _safe_sender(sender, config)
        content = str(getattr(message, "content", "") or f"[{message_type}]").strip()
        if not content:
            content = f"[{message_type}]"
        key = (anchor.isoformat(), sender, message_type, content)
        occurrence = occurrences[key]
        occurrences[key] += 1
        stable_hash = getattr(message, "hash", None)
        source_id = str(stable_hash or getattr(message, "id", "") or "") or None
        if stable_hash:
            material = f"{config.group_name}|{stable_hash}"
        else:
            material = "|".join((*key, str(occurrence), config.group_name))
        fingerprint = hashlib.sha256(material.encode("utf-8")).hexdigest()
        info = getattr(message, "info", {})
        records.append(
            MessageRecord(
                fingerprint=fingerprint,
                source_id=source_id,
                group_name=config.group_name,
                sender=sender,
                sent_at=anchor,
                observed_at=now,
                message_type=message_type,
                content=content,
                forced_keyword=find_force_keyword(content, config.force_keywords),
                raw={"attr": attr, "info": info, "ui_id": getattr(message, "id", None)},
            )
        )
    if state is not None:
        state._last_anchor = anchor
    return records

