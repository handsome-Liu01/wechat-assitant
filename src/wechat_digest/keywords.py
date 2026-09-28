from __future__ import annotations

import re
import unicodedata


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def find_force_keyword(content: str, keywords: tuple[str, ...] | list[str]) -> str | None:
    normalized = normalize_text(content)
    for keyword in keywords:
        if normalize_text(keyword) in normalized:
            return keyword
    return None


def priority_from_text(content: str) -> str:
    match = re.search(r"[\[【]\s*(P[0-3])[\]】]", normalize_text(content), re.IGNORECASE)
    return match.group(1).upper() if match else "待确认"

