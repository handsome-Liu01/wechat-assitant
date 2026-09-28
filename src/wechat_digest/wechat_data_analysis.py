from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

from .collector import CollectorError, _safe_sender
from .config import AppConfig
from .db import MessageRecord
from .keywords import find_force_keyword


class WeChatDataAnalysisCollector:
    """Read-only collector for WeChatDataAnalysis's local chat API."""

    driver_name = "wechat-data-analysis"

    def __init__(self, config: AppConfig, client: httpx.Client | None = None):
        self.config = config
        self.base_url = config.collector.wda_base_url.rstrip("/")
        self._validate_loopback_url()
        self.client = client or httpx.Client(
            base_url=self.base_url,
            timeout=config.collector.wda_timeout_seconds,
        )
        self._account: str | None = config.collector.wda_account.strip() or None
        self._session_id: str | None = config.collector.wda_session_id.strip() or None

    def _validate_loopback_url(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise CollectorError(
                "出于安全考虑，WeChatDataAnalysis API 只允许使用本机 HTTP 回环地址"
            )

    def close(self) -> None:
        self.client.close()

    def connect(self) -> None:
        self.resolve_account()
        self.resolve_session_id()

    def resolve_account(self) -> str:
        payload = self._get_json("/api/chat/accounts")
        if payload.get("status") != "success":
            detail = payload.get("message") or payload
            raise CollectorError(f"WeChatDataAnalysis 尚无可用微信账号: {detail}")
        accounts = [str(value).strip() for value in payload.get("accounts", []) if str(value).strip()]
        if self._account:
            if self._account not in accounts:
                raise CollectorError(
                    f"WeChatDataAnalysis 中找不到账号 {self._account!r}；可用账号: {accounts!r}"
                )
            return self._account
        default = str(payload.get("default_account") or "").strip()
        if default:
            self._account = default
        elif len(accounts) == 1:
            self._account = accounts[0]
        elif len(accounts) > 1:
            raise CollectorError(f"存在多个微信账号，请在 wda_account 中指定一个: {accounts!r}")
        else:
            raise CollectorError("WeChatDataAnalysis 未返回任何微信账号，请先完成数据读取配置")
        return self._account

    def resolve_session_id(self) -> str:
        if self._session_id:
            return self._session_id
        payload = self._get_json(
            "/api/chat/sessions",
            params={
                "account": self.resolve_account(),
                "limit": 2000,
                "include_hidden": "true",
                "include_official": "false",
                "preview": "none",
                "source": self.config.collector.wda_source,
            },
        )
        sessions = payload.get("sessions", [])
        if not isinstance(sessions, list):
            raise CollectorError("WeChatDataAnalysis sessions 字段不是数组")
        matches = [
            item
            for item in sessions
            if isinstance(item, dict)
            and str(item.get("name") or "") == self.config.group_name
            and bool(item.get("isGroup"))
        ]
        if not matches:
            available = [
                str(item.get("name") or "")
                for item in sessions
                if isinstance(item, dict) and item.get("isGroup")
            ][:20]
            raise CollectorError(
                f"WeChatDataAnalysis 中找不到目标群 {self.config.group_name!r}；"
                f"可见群聊示例: {available!r}"
            )
        if len(matches) > 1:
            ids = [str(item.get("username") or item.get("id") or "") for item in matches]
            raise CollectorError(f"存在多个同名群，请在 wda_session_id 中指定群 ID: {ids!r}")
        self._session_id = str(matches[0].get("username") or matches[0].get("id") or "").strip()
        if not self._session_id:
            raise CollectorError("目标群会话缺少 username/id")
        return self._session_id

    def collect_once(self, now: datetime | None = None) -> list[MessageRecord]:
        now = now or datetime.now(self.config.tz)
        start = now - timedelta(hours=self.config.collector.lookback_hours)
        return self.collect_range(start, now)

    def collect_range(self, start: datetime, end: datetime) -> list[MessageRecord]:
        if start.tzinfo is None or end.tzinfo is None:
            raise CollectorError("collect_range 的开始和结束时间必须包含时区")
        session_id = self.resolve_session_id()
        page_size = self.config.collector.wda_page_size
        offset = 0
        records: list[MessageRecord] = []
        seen_page_signatures: set[str] = set()

        while True:
            payload = self._get_json(
                "/api/chat/messages",
                params={
                    "username": session_id,
                    "account": self.resolve_account(),
                    "limit": page_size,
                    "offset": offset,
                    "order": "desc",
                    "source": self.config.collector.wda_source,
                },
            )
            if payload.get("status") not in {None, "success"}:
                raise CollectorError(f"WeChatDataAnalysis 消息查询失败: {payload!r}")
            messages = payload.get("messages", [])
            if not isinstance(messages, list):
                raise CollectorError("WeChatDataAnalysis messages 字段不是数组")
            if not messages:
                break

            signature = hashlib.sha256(repr(messages[:3]).encode("utf-8")).hexdigest()
            if signature in seen_page_signatures:
                raise CollectorError("WeChatDataAnalysis 分页返回重复数据，已停止以避免死循环")
            seen_page_signatures.add(signature)

            oldest: datetime | None = None
            for item in messages:
                if not isinstance(item, dict):
                    continue
                record = self._normalize(item, session_id)
                oldest = record.sent_at if oldest is None else min(oldest, record.sent_at)
                if start <= record.sent_at < end:
                    records.append(record)

            if oldest is not None and oldest < start:
                break
            offset += len(messages)
            if not payload.get("hasMore") or len(messages) < page_size:
                break
            if offset > 1_000_000:
                raise CollectorError("WeChatDataAnalysis 分页数量异常，已停止拉取")

        records.sort(key=lambda row: row.sent_at)
        return records

    def _normalize(self, item: dict[str, Any], session_id: str) -> MessageRecord:
        raw_timestamp = item.get("createTime")
        try:
            timestamp = float(raw_timestamp)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            sent_at = datetime.fromtimestamp(timestamp, tz=self.config.tz)
        except (TypeError, ValueError, OSError) as exc:
            raise CollectorError(
                f"WeChatDataAnalysis 消息缺少有效 createTime: {item!r}"
            ) from exc

        sender_id = str(item.get("senderUsername") or "").strip()
        if item.get("isSent"):
            sender = "我"
        else:
            sender = str(item.get("senderDisplayName") or sender_id or "未知成员").strip()
        sender = _safe_sender(sender, self.config)

        message_type = str(item.get("renderType") or self._type_name(item.get("type")))
        content = str(item.get("content") or "").strip()
        if message_type == "voice" and item.get("voiceTranscript"):
            content = str(item["voiceTranscript"]).strip()
        elif not content and item.get("title"):
            content = str(item["title"]).strip()
        if not content:
            content = f"[{message_type}]"

        stable_id = str(item.get("id") or item.get("serverIdStr") or item.get("serverId") or "")
        if stable_id:
            material = f"wechat-data-analysis|{session_id}|{stable_id}"
        else:
            material = (
                f"wechat-data-analysis|{session_id}|{sent_at.isoformat()}|{sender_id}|{content}"
            )
        return MessageRecord(
            fingerprint=hashlib.sha256(material.encode("utf-8")).hexdigest(),
            source_id=stable_id or None,
            group_name=self.config.group_name,
            sender=sender,
            sent_at=sent_at,
            observed_at=datetime.now(self.config.tz),
            message_type=message_type,
            content=content,
            forced_keyword=find_force_keyword(content, self.config.force_keywords),
            raw=item,
        )

    @staticmethod
    def _type_name(local_type: Any) -> str:
        try:
            value = int(local_type)
        except (TypeError, ValueError):
            return "other"
        return {
            1: "text",
            3: "image",
            34: "voice",
            43: "video",
            47: "emoji",
            49: "link",
            10000: "system",
        }.get(value, "other")

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = self.client.get(path, params=params)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise CollectorError(f"WeChatDataAnalysis 返回的不是 JSON 对象: {path}")
                return payload
            except (httpx.HTTPError, ValueError, CollectorError) as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.5)
        raise CollectorError(
            f"调用 WeChatDataAnalysis API 失败 {path}: {last_error}；"
            "请确认桌面程序正在运行且已成功读取当前微信账号"
        ) from last_error
