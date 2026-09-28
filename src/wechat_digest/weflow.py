from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

from .collector import CollectorError, _safe_sender
from .config import AppConfig
from .db import MessageRecord
from .keywords import find_force_keyword


class WeFlowCollector:
    """Read-only collector for WeFlow's loopback HTTP API."""

    driver_name = "weflow"

    def __init__(self, config: AppConfig, client: httpx.Client | None = None):
        self.config = config
        self.base_url = config.collector.weflow_base_url.rstrip("/")
        self._validate_loopback_url()
        token = os.environ.get(config.collector.weflow_api_token_env, "").strip()
        if not token:
            raise CollectorError(
                f"WeFlow API Token 环境变量未设置: {config.collector.weflow_api_token_env}"
            )
        self.client = client or httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=config.collector.weflow_timeout_seconds,
        )
        if client is not None:
            self.client.headers.update({"Authorization": f"Bearer {token}"})
        self._session_id: str | None = config.collector.weflow_session_id.strip() or None
        self._member_names: dict[str, str] = {}

    def _validate_loopback_url(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise CollectorError("出于安全考虑，WeFlow API 只允许使用本机 HTTP 回环地址")

    def close(self) -> None:
        self.client.close()

    def connect(self) -> None:
        response = self.client.get("/health")
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "ok":
            raise CollectorError(f"WeFlow 健康检查异常: {payload!r}")
        self.resolve_session_id()

    def resolve_session_id(self) -> str:
        if self._session_id:
            return self._session_id
        payload = self._get_json(
            "/api/v1/sessions",
            params={"format": "chatlab", "keyword": self.config.group_name, "limit": 1000},
        )
        sessions = payload.get("sessions", [])
        matches = [
            item
            for item in sessions
            if str(item.get("name", "")) == self.config.group_name
            and str(item.get("type", "")) == "group"
        ]
        if not matches:
            available = [str(item.get("name", "")) for item in sessions[:10]]
            raise CollectorError(
                f"WeFlow 中找不到目标群 {self.config.group_name!r}；查询结果: {available!r}"
            )
        if len(matches) > 1:
            ids = [str(item.get("id", "")) for item in matches]
            raise CollectorError(
                f"存在多个同名群，请在 weflow_session_id 中指定群 ID: {ids!r}"
            )
        self._session_id = str(matches[0]["id"])
        return self._session_id

    def collect_once(self, now: datetime | None = None) -> list[MessageRecord]:
        now = now or datetime.now(self.config.tz)
        start = now - timedelta(hours=self.config.collector.lookback_hours)
        return self.collect_range(start, now)

    def collect_range(self, start: datetime, end: datetime) -> list[MessageRecord]:
        session_id = self.resolve_session_id()
        self._refresh_member_names(session_id)
        page_size = self.config.collector.weflow_page_size
        offset = 0
        records: list[MessageRecord] = []
        seen_page_signatures: set[str] = set()

        while True:
            payload = self._get_json(
                "/api/v1/messages",
                params={
                    "talker": session_id,
                    "start": int(start.timestamp()),
                    "end": int(end.timestamp()),
                    "limit": page_size,
                    "offset": offset,
                    "media": 0,
                },
            )
            if payload.get("success") is False:
                raise CollectorError(f"WeFlow 消息查询失败: {payload!r}")
            messages = payload.get("messages", [])
            if not isinstance(messages, list):
                raise CollectorError("WeFlow messages 字段不是数组")
            if not messages:
                break
            signature = hashlib.sha256(repr(messages[:3]).encode("utf-8")).hexdigest()
            if signature in seen_page_signatures:
                raise CollectorError("WeFlow 分页返回重复数据，已停止以避免死循环")
            seen_page_signatures.add(signature)
            for item in messages:
                record = self._normalize(item, session_id)
                if start <= record.sent_at < end:
                    records.append(record)
            offset += len(messages)
            if not payload.get("hasMore") or len(messages) < page_size:
                break
            if offset > 1_000_000:
                raise CollectorError("WeFlow 分页数量异常，已停止拉取")
        return records

    def _refresh_member_names(self, session_id: str) -> None:
        try:
            payload = self._get_json(
                "/api/v1/group-members",
                params={"chatroomId": session_id, "forceRefresh": 0},
            )
        except Exception:
            return
        mapping: dict[str, str] = {}
        for member in payload.get("members", []):
            member_id = str(member.get("wxid", ""))
            name = next(
                (
                    str(member.get(field, "")).strip()
                    for field in ("groupNickname", "displayName", "remark", "nickname")
                    if str(member.get(field, "")).strip()
                ),
                member_id,
            )
            if member_id:
                mapping[member_id] = name
        self._member_names = mapping

    def _normalize(self, item: dict[str, Any], session_id: str) -> MessageRecord:
        try:
            sent_at = datetime.fromtimestamp(float(item["createTime"]), tz=self.config.tz)
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise CollectorError(f"WeFlow 消息缺少有效 createTime: {item!r}") from exc
        sender_id = str(item.get("senderUsername", "") or "")
        if item.get("isSend"):
            sender = "我"
        else:
            sender = self._member_names.get(sender_id, sender_id or "未知成员")
        sender = _safe_sender(sender, self.config)
        message_type = str(item.get("mediaType") or self._type_name(item.get("localType")))
        content = str(item.get("parsedContent") or item.get("content") or "").strip()
        if not content:
            content = f"[{message_type}]"
        stable_id = str(item.get("serverId") or item.get("localId") or "")
        if stable_id:
            material = f"weflow|{session_id}|{stable_id}"
        else:
            material = f"weflow|{session_id}|{sent_at.isoformat()}|{sender_id}|{content}"
        fingerprint = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return MessageRecord(
            fingerprint=fingerprint,
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
        return {1: "text", 3: "image", 34: "voice", 43: "video", 49: "link", 47: "emotion"}.get(
            value, "other"
        )

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = self.client.get(path, params=params)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise CollectorError(f"WeFlow 返回的不是 JSON 对象: {path}")
                return payload
            except (httpx.HTTPError, ValueError, CollectorError) as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.5)
        raise CollectorError(f"调用 WeFlow API 失败 {path}: {last_error}") from last_error

