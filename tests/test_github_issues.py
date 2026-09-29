from pathlib import Path

import httpx

from wechat_digest.config import AppConfig, FeishuConfig, GitHubIssuesConfig
from wechat_digest.db import Database
from wechat_digest.feishu import FeishuRecord
from wechat_digest.github_issues import (
    GitHubClient,
    assess_record,
    discover_repositories,
    is_candidate,
    run_github_issue_triage,
)


class FakeLLM:
    def __init__(self, repository: str):
        self.repository = repository
        self.calls = 0

    def complete_json(self, _system, _prompt):
        self.calls += 1
        if self.calls % 2 == 1:
            return {
                "repository": self.repository,
                "confidence": 0.91,
                "reason": "README 与问题匹配",
                "search_terms": ["inbox", "send"],
            }
        return {
            "solvable": True,
            "confidence": 0.9,
            "reason": "存在明确发送入口",
            "root_cause": "缺少空内容判断",
            "solution": ["发送前过滤空内容"],
            "files": ["src/app.py", "../outside.txt"],
            "tests": ["增加空 inbox 单元测试"],
        }


class FakeFeishu:
    def __init__(self, records):
        self.records = records

    def list_records(self):
        return self.records


class FakeGitHub:
    def __init__(self):
        self.created = []

    def find_marker(self, _repository, _marker):
        return None

    def create_issue(self, repository, title, body):
        self.created.append((repository, title, body))
        return 12, f"https://github.com/HarnessApex/{repository}/issues/12"


def make_repo(root: Path, name: str = "demo") -> Path:
    repo = root / name
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        f'[remote "origin"]\n\turl = https://github.com/HarnessApex/{name}.git\n',
        encoding="utf-8",
    )
    (repo / "src").mkdir()
    (repo / "README.md").write_text("Inbox message service", encoding="utf-8")
    (repo / "src" / "app.py").write_text(
        "def send_inbox(value):\n    return send(value)\n", encoding="utf-8"
    )
    return repo


def settings(root: Path) -> GitHubIssuesConfig:
    return GitHubIssuesConfig(
        enabled=True,
        organization="HarnessApex",
        repositories_root=root,
        severity_field="严重程度",
        severity_values=("一般", "轻微"),
        title_field="问题标题",
        min_confidence=0.82,
        dry_run=True,
    )


def test_discovers_only_matching_organization_and_assesses_real_file(tmp_path: Path):
    make_repo(tmp_path, "demo")
    other = make_repo(tmp_path, "foreign")
    (other / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/someone/foreign.git\n',
        encoding="utf-8",
    )
    config = settings(tmp_path)
    repositories = discover_repositories(config)
    assert [repo.name for repo in repositories] == ["demo"]
    record = FeishuRecord(
        "rec1",
        {"严重程度": "一般", "问题标题": "自动发送 inbox 空内容", "问题描述": "偶发"},
    )
    assert is_candidate(record, config)
    fixed = FeishuRecord(
        "rec-fixed",
        {"严重程度": "一般", "状态": "已修复", "问题标题": "无需继续分析"},
    )
    regressed = FeishuRecord(
        "rec-regressed",
        {"严重程度": "轻微", "状态": [{"text": "已回归"}]},
    )
    assert not is_candidate(fixed, config)
    assert not is_candidate(regressed, config)
    assessment = assess_record(record, repositories, config, FakeLLM("demo"))
    assert assessment.solvable is True
    assert assessment.files == ("src/app.py",)


def test_triage_creates_once_and_database_deduplicates(tmp_path: Path):
    make_repo(tmp_path, "demo")
    github_config = settings(tmp_path)
    config = AppConfig(
        group_name="测试群",
        feishu=FeishuConfig(enabled=True, app_token="base", table_id="table"),
        github_issues=github_config,
    )
    record = FeishuRecord(
        "rec1",
        {"严重程度": [{"text": "轻微", "type": "text"}], "问题标题": "inbox 重复发送"},
    )
    first_row = FeishuRecord(
        "rec0", {"严重程度": "严重", "问题标题": "不应被本次选择处理"}
    )
    db = Database(tmp_path / "db.sqlite")
    db.init_schema()
    github = FakeGitHub()
    first = run_github_issue_triage(
        config,
        db,
        dry_run=False,
        llm=FakeLLM("demo"),
        feishu_client=FakeFeishu([first_row, record]),
        github_client=github,
        record_index=2,
    )
    second = run_github_issue_triage(
        config,
        db,
        dry_run=False,
        llm=FakeLLM("demo"),
        feishu_client=FakeFeishu([first_row, record]),
        github_client=github,
        record_index=2,
    )
    assert first[0].decision == "created"
    assert first[0].issue_url.endswith("/issues/12")
    assert second == []
    assert len(github.created) == 1
    assert "wechat-digest:feishu-record:rec1" in github.created[0][2]
    db.close()


def test_github_client_uses_issue_endpoint(monkeypatch):
    monkeypatch.setenv("TEST_GITHUB_TOKEN", "github-token")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["version"] = request.headers["X-GitHub-Api-Version"]
        return httpx.Response(
            201,
            json={"number": 7, "html_url": "https://github.com/HarnessApex/demo/issues/7"},
        )

    config = GitHubIssuesConfig(
        token_env="TEST_GITHUB_TOKEN", organization="HarnessApex"
    )
    client = GitHubClient(config, httpx.Client(transport=httpx.MockTransport(handler)))
    number, url = client.create_issue("demo", "问题", "内容")
    assert number == 7
    assert url.endswith("/issues/7")
    assert seen == {"path": "/repos/HarnessApex/demo/issues", "version": "2026-03-10"}
