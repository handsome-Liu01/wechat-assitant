from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable
from urllib.parse import quote

import httpx

from .analyzer import Issue
from .config import FeishuConfig


DEFAULT_FIELD_MAPPING = {
    "date": "日期",
    "priority": "优先级",
    "title": "问题标题",
    "category": "问题分类",
    "description": "问题描述",
    "affected_version": "影响版本",
    "reproduction_steps": "复现步骤",
    "reporter_count": "反馈人数",
    "first_seen": "首次反馈时间",
    "last_seen": "最后反馈时间",
    "reporters": "反馈成员",
    "error_codes": "相关错误码",
    "forced": "是否强制收录",
    "inclusion_reason": "收录原因",
    "evidence_ids": "原始消息ID",
    "confidence": "模型置信度",
}

WRITABLE_FIELD_TYPES = {1, 2, 3, 4, 5, 7, 13, 15}


@dataclass(frozen=True)
class FeishuField:
    name: str
    field_id: str
    field_type: int
    is_primary: bool = False


class FeishuClient:
    def __init__(self, config: FeishuConfig, client: httpx.Client | None = None):
        self.config = config
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=config.timeout_seconds)
        self._token: str | None = None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "FeishuClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _credentials(self) -> tuple[str, str]:
        app_id = os.environ.get(self.config.app_id_env, "").strip()
        app_secret = os.environ.get(self.config.app_secret_env, "").strip()
        missing = []
        if not app_id:
            missing.append(self.config.app_id_env)
        if not app_secret:
            missing.append(self.config.app_secret_env)
        if missing:
            raise ValueError(f"飞书密钥环境变量未设置: {', '.join(missing)}")
        return app_id, app_secret

    def _tenant_token(self) -> str:
        if self._token:
            return self._token
        app_id, app_secret = self._credentials()
        response = self.client.post(
            f"{self.config.base_url.rstrip('/')}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
        )
        payload = self._response_payload(response, "获取 tenant_access_token")
        self._check(payload, "获取 tenant_access_token")
        token = str(payload.get("tenant_access_token", ""))
        if not token:
            raise RuntimeError("飞书未返回 tenant_access_token")
        self._token = token
        return token

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {self._tenant_token()}"
        response = self.client.request(
            method,
            f"{self.config.base_url.rstrip('/')}{path}",
            headers=headers,
            **kwargs,
        )
        payload = self._response_payload(response, f"飞书接口 {method} {path}")
        self._check(payload, f"飞书接口 {method} {path}")
        return payload

    @staticmethod
    def _response_payload(response: httpx.Response, action: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.is_error:
            if isinstance(payload, dict):
                error = payload.get("error") or {}
                details = str(error.get("message", "")).strip() if isinstance(error, dict) else ""
                log_id = str(error.get("log_id", "")).strip() if isinstance(error, dict) else ""
                message = (
                    f"{action}失败: HTTP {response.status_code}, "
                    f"code={payload.get('code')}, msg={payload.get('msg', '未知错误')}"
                )
                if details:
                    message += f", details={details}"
                if log_id:
                    message += f", log_id={log_id}"
                raise RuntimeError(message)
            raise RuntimeError(
                f"{action}失败: HTTP {response.status_code}, 响应={response.text[:500]}"
            )
        if not isinstance(payload, dict):
            raise RuntimeError(f"{action}返回了无法识别的数据")
        return payload

    @staticmethod
    def _check(payload: Any, action: str) -> None:
        if not isinstance(payload, dict):
            raise RuntimeError(f"{action}返回了无法识别的数据")
        try:
            code = int(payload.get("code", -1))
        except (TypeError, ValueError):
            code = -1
        if code != 0:
            raise RuntimeError(
                f"{action}失败: code={payload.get('code')}, msg={payload.get('msg', '未知错误')}"
            )

    def _table_path(self, suffix: str) -> str:
        app_token = quote(self.config.app_token, safe="")
        table_id = quote(self.config.table_id, safe="")
        return f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/{suffix}"

    def list_fields(self) -> list[FeishuField]:
        fields: list[FeishuField] = []
        page_token = ""
        while True:
            params = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            payload = self._request("GET", self._table_path("fields"), params=params)
            data = payload.get("data") or {}
            for item in data.get("items") or []:
                fields.append(
                    FeishuField(
                        name=str(item.get("field_name", "")),
                        field_id=str(item.get("field_id", "")),
                        field_type=int(item.get("type", 0)),
                        is_primary=bool(item.get("is_primary", False)),
                    )
                )
            if not data.get("has_more"):
                break
            page_token = str(data.get("page_token", ""))
            if not page_token:
                break
        if not fields:
            raise RuntimeError("飞书数据表没有返回任何字段")
        return fields

    def create_record(self, values: dict[str, Any]) -> str:
        payload = self._request("POST", self._table_path("records"), json={"fields": values})
        record = (payload.get("data") or {}).get("record") or {}
        record_id = str(record.get("record_id", ""))
        if not record_id:
            raise RuntimeError("飞书写入成功但未返回 record_id")
        return record_id

    def create_test_record(self, fields: Iterable[FeishuField]) -> str:
        primary = next((field for field in fields if field.is_primary), None)
        if primary is None:
            raise RuntimeError("未找到飞书多维表格的主字段")
        value: Any = f"[接入测试] 微信问题日报 {datetime.now().astimezone().isoformat(timespec='seconds')}"
        if primary.field_type == 2:
            value = int(datetime.now().timestamp())
        return self.create_record({primary.name: value})

    def issue_fields(self, issue: Issue, fields: Iterable[FeishuField]) -> dict[str, Any]:
        by_name = {field.name: field for field in fields}
        mapping = dict(DEFAULT_FIELD_MAPPING)
        mapping.update(self.config.field_mapping)
        result: dict[str, Any] = {}
        for attribute, destination in mapping.items():
            field = by_name.get(destination)
            if field is None or field.field_type not in WRITABLE_FIELD_TYPES:
                continue
            value = _issue_value(issue, attribute)
            if value in (None, "", []):
                continue
            result[destination] = _field_value(value, field.field_type)

        primary = next((field for field in fields if field.is_primary), None)
        if primary and primary.name not in result:
            result[primary.name] = _field_value(issue.title, primary.field_type)
        if not result:
            raise RuntimeError("飞书字段映射没有匹配到任何可写字段")
        return result

    def create_issue(self, issue: Issue, fields: Iterable[FeishuField]) -> str:
        return self.create_record(self.issue_fields(issue, fields))


def _issue_value(issue: Issue, attribute: str) -> Any:
    value = getattr(issue, attribute, None)
    if attribute in {"reproduction_steps", "reporters", "error_codes"}:
        return "\n".join(str(item) for item in value or [])
    if attribute == "evidence_ids":
        return ",".join(str(item) for item in value or [])
    if attribute == "forced":
        return bool(value)
    return value


def _field_value(value: Any, field_type: int) -> Any:
    if field_type == 2:
        return float(value)
    if field_type == 4:
        if isinstance(value, list):
            return [str(item) for item in value]
        return [item.strip() for item in str(value).replace("\n", "、").split("、") if item.strip()]
    if field_type == 5:
        if isinstance(value, (datetime, date)):
            parsed = value
        else:
            parsed = datetime.fromisoformat(str(value))
        if isinstance(parsed, date) and not isinstance(parsed, datetime):
            parsed = datetime.combine(parsed, datetime.min.time()).astimezone()
        elif parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return int(parsed.timestamp() * 1000)
    if field_type == 7:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "是"}
        return bool(value)
    return str(value)
