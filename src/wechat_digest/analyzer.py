from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Iterable, Protocol

import httpx

from .config import LLMConfig
from .keywords import normalize_text, priority_from_text


@dataclass
class Issue:
    date: str
    priority: str
    title: str
    category: str
    description: str
    affected_version: str = ""
    reproduction_steps: list[str] = field(default_factory=list)
    reporter_count: int = 0
    first_seen: str = ""
    last_seen: str = ""
    reporters: list[str] = field(default_factory=list)
    error_codes: list[str] = field(default_factory=list)
    forced: bool = False
    inclusion_reason: str = "模型分析"
    evidence_ids: list[int] = field(default_factory=list)
    confidence: float | None = None


class LLM(Protocol):
    def analyze(self, transcript: str) -> dict[str, Any]: ...


class OpenAICompatibleLLM:
    def __init__(self, config: LLMConfig):
        api_key = os.environ.get(config.api_key_env, "")
        if not api_key:
            raise ValueError(f"模型密钥环境变量未设置: {config.api_key_env}")
        if not config.model:
            raise ValueError("llm.model 不能为空")
        self.config = config
        self.api_key = api_key

    def analyze(self, transcript: str) -> dict[str, Any]:
        payload = self.complete_json(
            "你是软件发行版问题分拣员，只输出合法 JSON。", _prompt(transcript)
        )
        if not isinstance(payload.get("issues", []), list):
            raise ValueError("模型输出必须包含 issues 数组")
        return payload

    def complete_json(self, system: str, prompt: str) -> dict[str, Any]:
        endpoint = f"{self.config.base_url.rstrip('/')}/chat/completions"
        response = httpx.post(
            endpoint,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.config.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=self.config.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        return _parse_json(content)


def _parse_json(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("模型输出必须是 JSON 对象")
    return value


def _prompt(transcript: str) -> str:
    return f"""分析下面的软件发行版问题反馈群聊天记录，只保留真正值得跟踪的问题。

重要问题包括：崩溃、无法启动、数据丢失、安全或权限问题、核心流程阻断、版本回归、多人重复反馈，以及包含明确错误码或复现步骤且尚未解决的问题。忽略闲聊、通知、已明确解决且无需跟踪的内容。相同问题必须合并。

严格返回：
{{
  "issues": [
    {{
      "title": "简短标题",
      "category": "崩溃/安装/性能/功能异常/兼容性/安全/其他",
      "priority": "P0/P1/P2/P3",
      "summary": "问题与影响",
      "affected_version": "未知时为空字符串",
      "reproduction_steps": ["步骤"],
      "error_codes": ["错误码"],
      "source_message_ids": [1, 2],
      "confidence": 0.0
    }}
  ]
}}

source_message_ids 只能使用记录开头给出的整数 ID，不得编造。聊天记录：
{transcript}
"""


def _row_time(row: Any) -> datetime:
    return datetime.fromisoformat(str(row["sent_at"]))


def forced_issues(rows: Iterable[Any], report_date: str) -> list[Issue]:
    result: list[Issue] = []
    for row in rows:
        if not row["forced_keyword"]:
            continue
        when = str(row["sent_at"])
        result.append(
            Issue(
                date=report_date,
                priority=priority_from_text(str(row["content"])),
                title=str(row["content"])[:80],
                category="关键词标记",
                description=str(row["content"]),
                reporter_count=1,
                first_seen=when,
                last_seen=when,
                reporters=[str(row["sender"])],
                forced=True,
                inclusion_reason=f"关键词：{row['forced_keyword']}",
                evidence_ids=[int(row["id"])],
            )
        )
    return result


def _chunks(rows: list[Any], max_chars: int) -> list[list[Any]]:
    result: list[list[Any]] = []
    current: list[Any] = []
    size = 0
    for row in rows:
        line_size = len(str(row["content"])) + len(str(row["sender"])) + 80
        if current and size + line_size > max_chars:
            result.append(current)
            current, size = [], 0
        current.append(row)
        size += line_size
    if current:
        result.append(current)
    return result


def analyze_rows(rows: list[Any], report_date: str, llm: LLM | None, max_chars: int) -> list[Issue]:
    forced = forced_issues(rows, report_date)
    ordinary = [row for row in rows if not row["forced_keyword"]]
    if llm is None:
        return forced
    issues = list(forced)
    by_id = {int(row["id"]): row for row in ordinary}
    for chunk in _chunks(ordinary, max_chars):
        transcript = "\n".join(
            f"[{row['id']}] {row['sent_at']} | {row['sender']} | {row['message_type']} | {row['content']}"
            for row in chunk
        )
        payload = llm.analyze(transcript)
        for item in payload.get("issues", []):
            evidence_ids = []
            for value in item.get("source_message_ids", []):
                try:
                    message_id = int(value)
                except (TypeError, ValueError):
                    continue
                if message_id in by_id:
                    evidence_ids.append(message_id)
            evidence_ids = list(dict.fromkeys(evidence_ids))
            if not evidence_ids:
                continue
            evidence = [by_id[value] for value in evidence_ids]
            times = [_row_time(row) for row in evidence]
            reporters = sorted({str(row["sender"]) for row in evidence})
            issues.append(
                Issue(
                    date=report_date,
                    priority=str(item.get("priority", "P3")),
                    title=str(item.get("title", "未命名问题"))[:120],
                    category=str(item.get("category", "其他")),
                    description=str(item.get("summary", "")),
                    affected_version=str(item.get("affected_version", "")),
                    reproduction_steps=[str(x) for x in item.get("reproduction_steps", [])],
                    reporter_count=len(reporters),
                    first_seen=min(times).isoformat(),
                    last_seen=max(times).isoformat(),
                    reporters=reporters,
                    error_codes=[str(x) for x in item.get("error_codes", [])],
                    evidence_ids=evidence_ids,
                    confidence=_confidence(item.get("confidence")),
                )
            )
    return _merge_similar(issues)


def _confidence(value: Any) -> float | None:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return None


def _merge_similar(issues: list[Issue]) -> list[Issue]:
    merged: list[Issue] = []
    for issue in issues:
        if issue.forced:
            merged.append(issue)
            continue
        target = next(
            (
                current
                for current in merged
                if not current.forced
                and current.category == issue.category
                and SequenceMatcher(
                    None, normalize_text(current.title), normalize_text(issue.title)
                ).ratio() >= 0.82
            ),
            None,
        )
        if target is None:
            merged.append(issue)
            continue
        target.evidence_ids = sorted(set(target.evidence_ids + issue.evidence_ids))
        target.reporters = sorted(set(target.reporters + issue.reporters))
        target.reporter_count = len(target.reporters)
        target.first_seen = min(target.first_seen, issue.first_seen)
        target.last_seen = max(target.last_seen, issue.last_seen)
        target.error_codes = sorted(set(target.error_codes + issue.error_codes))
        target.reproduction_steps = list(dict.fromkeys(target.reproduction_steps + issue.reproduction_steps))
        if issue.confidence is not None:
            target.confidence = max(target.confidence or 0.0, issue.confidence)
    return merged

