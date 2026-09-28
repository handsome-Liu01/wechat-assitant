from datetime import datetime

import httpx

from wechat_digest.config import AppConfig, CollectorConfig
from wechat_digest.wechat_data_analysis import WeChatDataAnalysisCollector


def test_wda_resolves_group_and_collects_time_range():
    config = AppConfig(
        group_name="发行版反馈群",
        collector=CollectorConfig(
            driver="wechat_data_analysis",
            wda_base_url="http://127.0.0.1:10392",
            wda_page_size=2,
        ),
        force_keywords=("#重要",),
    )
    wanted = int(datetime(2026, 9, 27, 10, 30, tzinfo=config.tz).timestamp())
    too_new = int(datetime(2026, 9, 28, 1, 0, tzinfo=config.tz).timestamp())
    too_old = int(datetime(2026, 9, 26, 23, 0, tzinfo=config.tz).timestamp())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/chat/accounts":
            return httpx.Response(
                200,
                json={"status": "success", "accounts": ["wxid_me"], "default_account": "wxid_me"},
            )
        if request.url.path == "/api/chat/sessions":
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "sessions": [
                        {
                            "id": "room@chatroom",
                            "username": "room@chatroom",
                            "name": "发行版反馈群",
                            "isGroup": True,
                        }
                    ],
                },
            )
        if request.url.path == "/api/chat/messages":
            offset = int(request.url.params["offset"])
            if offset == 0:
                messages = [
                    {
                        "id": "message_0:msg:3",
                        "serverIdStr": "3",
                        "type": 1,
                        "createTime": too_new,
                        "isSent": False,
                        "senderUsername": "wxid_a",
                        "senderDisplayName": "测试同学",
                        "renderType": "text",
                        "content": "明天的新消息",
                    },
                    {
                        "id": "message_0:msg:2",
                        "serverIdStr": "2",
                        "type": 1,
                        "createTime": wanted,
                        "isSent": False,
                        "senderUsername": "wxid_a",
                        "senderDisplayName": "测试同学",
                        "renderType": "text",
                        "content": "#重要 2.4.1 启动失败",
                    },
                ]
            else:
                messages = [
                    {
                        "id": "message_0:msg:1",
                        "serverIdStr": "1",
                        "type": 1,
                        "createTime": too_old,
                        "isSent": True,
                        "senderUsername": "wxid_me",
                        "renderType": "text",
                        "content": "更早的消息",
                    }
                ]
            return httpx.Response(
                200,
                json={"status": "success", "hasMore": offset == 0, "messages": messages},
            )
        raise AssertionError(f"unexpected URL: {request.url}")

    client = httpx.Client(
        base_url="http://127.0.0.1:10392", transport=httpx.MockTransport(handler)
    )
    collector = WeChatDataAnalysisCollector(config, client=client)
    collector.connect()
    rows = collector.collect_range(
        datetime(2026, 9, 27, 0, 0, tzinfo=config.tz),
        datetime(2026, 9, 28, 0, 0, tzinfo=config.tz),
    )

    assert len(rows) == 1
    assert rows[0].sender == "测试同学"
    assert rows[0].forced_keyword == "#重要"
    assert rows[0].source_id == "message_0:msg:2"


def test_wda_rejects_non_loopback_address():
    config = AppConfig(
        group_name="发行版反馈群",
        collector=CollectorConfig(
            driver="wechat_data_analysis", wda_base_url="http://192.168.1.8:10392"
        ),
    )
    try:
        WeChatDataAnalysisCollector(config)
    except RuntimeError as exc:
        assert "本机" in str(exc)
    else:
        raise AssertionError("non-loopback URL should be rejected")
