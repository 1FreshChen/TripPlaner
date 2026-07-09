from __future__ import annotations

import re

import bleach

ALLOWED_TAGS: list[str] = []
ALLOWED_ATTRIBUTES: dict[str, list[str]] = {}

INJECTION_PATTERNS = [
    r"(?i)(ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?|messages?))",
    r"(?i)(you\s+are\s+now\s+(DAN|free|unrestricted))",
    r"(?i)(system\s*[:：]\s*)",
    r"(?i)(<\|im_start\|>)",
    r"(?i)(\[INST\])",
    r"(?i)(\[SYS\])",
    r"(?i)(prompt\s*injection)",
    r"(?i)(jailbreak)",
]

SENSITIVE_KEYWORDS = [
    "暴力",
    "色情",
    "赌博",
    "毒品",
    "颠覆国家政权",
    "分裂国家",
    "恐怖主义",
    "极端主义",
    "邪教",
    "宣扬民族仇恨",
    "煽动颠覆",
]


def sanitize_text(value: str) -> str:
    """Strip HTML and normalize whitespace for plain-text user input."""
    if not isinstance(value, str):
        return value
    cleaned = bleach.clean(
        value,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        strip=True,
    )
    return " ".join(cleaned.split())


def detect_injection(value: str) -> bool:
    """Return True when text resembles a prompt-injection attempt."""
    if not isinstance(value, str):
        return False
    return any(re.search(pattern, value) for pattern in INJECTION_PATTERNS)


def filter_llm_output(text: str) -> tuple[str, list[str]]:
    """Replace configured sensitive keywords in LLM output."""
    if not isinstance(text, str):
        return text, []

    issues: list[str] = []
    filtered = text
    for keyword in SENSITIVE_KEYWORDS:
        pattern = re.compile(re.escape(keyword), flags=re.IGNORECASE)
        if pattern.search(filtered):
            filtered = pattern.sub("[已过滤]", filtered)
            issues.append(f"检测到敏感内容: {keyword}")
    return filtered, issues
