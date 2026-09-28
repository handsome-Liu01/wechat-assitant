from datetime import datetime

import httpx

from wechat_digest.config import AppConfig, CollectorConfig
from wechat_digest.weflow import WeFlowCollector


def test_weflow_resolves_group_and_collects_messages(monkeypatch):
    monkeypatch.setenv("WEFLOW_API_TOKEN", "test-token")
    config = AppConfig(
        group_name="发行版反馈群",
        collector=CollectorConfig(
            driver="weflow",
            weflow_base_url="http://127.0.0.1:5031",
            weflow_page_size=1000,
        ),
        force_keywords=("#重要",),
    )
    sent = int(datetime(2026, 9, 27, 10, 30, tzinfo=config.tz).timestamp())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-token"
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/v1/sessions":
            return httpx.Response(
                200,
                json={
                    "sessions": [
                        {"id": "room@chatroom", "name": "发行版反馈群", "type": "group"}
                    ]
                },
            )
        if request.url.path == "/api/v1/group-members":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "members": [
                        {"wxid": "wxid_a", "groupNickname": "测试同学", "displayName": "小王"}
                    ],
                },
            )
        if request.url.path == "/api/v1/messages":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "hasMore": False,
                    "messages": [
                        {
                            "localId": 1,
                            "serverId": "server-1",
                            "localType": 1,
                            "createTime": sent,
                            "isSend": 0,
                            "senderUsername": "wxid_a",
                            "content": "#重要 2.4.1 启动失败",
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected URL: {request.url}")

    client = httpx.Client(
        base_url="http://127.0.0.1:5031", transport=httpx.MockTransport(handler)
    )
    collector = WeFlowCollector(config, client=client)
    collector.connect()
    rows = collector.collect_range(
        datetime(2026, 9, 27, 0, 0, tzinfo=config.tz),
        datetime(2026, 9, 28, 0, 0, tzinfo=config.tz),
    )
    assert len(rows) == 1
    assert rows[0].sender == "测试同学"
    assert rows[0].forced_keyword == "#重要"
    assert rows[0].source_id == "server-1"

