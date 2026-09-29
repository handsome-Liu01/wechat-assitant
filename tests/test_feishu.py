from datetime import datetime

import httpx
import pytest

from wechat_digest.analyzer import Issue
from wechat_digest.config import FeishuConfig
from wechat_digest.feishu import FeishuClient


def test_list_fields_and_create_issue(monkeypatch):
    monkeypatch.setenv("TEST_FEISHU_ID", "cli_test")
    monkeypatch.setenv("TEST_FEISHU_SECRET", "secret_test")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token"})
        if request.method == "GET" and request.url.path.endswith("/fields"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [
                            {"field_id": "fld1", "field_name": "问题标题", "type": 1, "is_primary": True},
                            {"field_id": "fld2", "field_name": "优先级", "type": 3},
                            {"field_id": "fld3", "field_name": "反馈人数", "type": 2},
                            {"field_id": "fld4", "field_name": "首次反馈时间", "type": 5},
                            {"field_id": "fld5", "field_name": "是否强制收录", "type": 7},
                        ]
                    },
                },
            )
        if request.method == "POST" and request.url.path.endswith("/records"):
            seen.update(__import__("json").loads(request.content)["fields"])
            return httpx.Response(200, json={"code": 0, "data": {"record": {"record_id": "rec1"}}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    config = FeishuConfig(
        enabled=True,
        app_id_env="TEST_FEISHU_ID",
        app_secret_env="TEST_FEISHU_SECRET",
        app_token="base_token",
        table_id="table_id",
    )
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    client = FeishuClient(config, http)
    fields = client.list_fields()
    issue = Issue(
        date="2026-09-28",
        priority="P1",
        title="启动失败",
        category="崩溃",
        description="无法启动",
        reporter_count=2,
        first_seen="2026-09-28T10:00:00+08:00",
        forced=True,
    )
    assert client.create_issue(issue, fields) == "rec1"
    assert seen["问题标题"] == "启动失败"
    assert seen["优先级"] == "P1"
    assert seen["反馈人数"] == 2.0
    assert seen["首次反馈时间"] == int(
        datetime.fromisoformat("2026-09-28T10:00:00+08:00").timestamp() * 1000
    )
    assert seen["是否强制收录"] is True


def test_http_error_keeps_feishu_error_details(monkeypatch):
    monkeypatch.setenv("TEST_FEISHU_ID", "cli_test")
    monkeypatch.setenv("TEST_FEISHU_SECRET", "secret_test")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token"})
        return httpx.Response(
            403,
            json={
                "code": 91403,
                "msg": "Forbidden",
                "error": {"message": "no edit permission", "log_id": "log-test"},
            },
        )

    config = FeishuConfig(
        enabled=True,
        app_id_env="TEST_FEISHU_ID",
        app_secret_env="TEST_FEISHU_SECRET",
        app_token="base_token",
        table_id="table_id",
    )
    client = FeishuClient(config, httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RuntimeError, match=r"HTTP 403.*91403.*no edit permission.*log-test"):
        client.create_record({"问题标题": "测试"})
