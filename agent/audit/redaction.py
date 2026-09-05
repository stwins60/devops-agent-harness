"""Secret detection and redaction.

Applied to every audit record, every tool result stored in task state, every
memory write and every outbound comment (Jira / PR). The patterns are
conservative: it is better to over-redact than to leak.
"""
from __future__ import annotations

import re
from typing import Any

MASK = "********"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # AWS access key ids and secret keys
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), r"\1" + "*" * 16),
    (re.compile(r"(?i)(aws_secret_access_key\s*[=:]\s*)([A-Za-z0-9/+=]{20,})"), r"\1" + MASK),
    # Bearer / basic auth tokens
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/]+=*"), r"\1 " + MASK),
    (re.compile(r"(?i)\b(basic)\s+[A-Za-z0-9+/]{16,}=*"), r"\1 " + MASK),
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(\S+)"), r"\1" + MASK),
    # GitHub / GitLab / Slack / Jira style tokens
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), MASK),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), MASK),
    (re.compile(r"\bglpat-[A-Za-z0-9\-_]{20,}\b"), MASK),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"), MASK),
    (re.compile(r"\bsk-[A-Za-z0-9\-_]{20,}\b"), MASK),
    (re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"), MASK),
    # key=value style secrets (password=, token=, secret=, api_key=, ...)
    (re.compile(r"(?i)\b((?:password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret)"
                r"\s*[=:]\s*)(['\"]?)([^'\"\s,;&]+)(\2)"), r"\1\2" + MASK + r"\4"),
    # JSON style "password": "..."
    (re.compile(r"(?i)(\"(?:password|passwd|token|secret|api_key|apikey|access_key|private_key|client_secret)\"\s*:\s*\")([^\"]*)(\")"),
     r"\1" + MASK + r"\3"),
    # URLs with credentials  scheme://user:pass@host
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.\-]*://[^/\s:@]+:)([^@\s/]+)(@)"), r"\1" + MASK + r"\3"),
    # PEM private keys
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     "-----BEGIN PRIVATE KEY-----" + MASK + "-----END PRIVATE KEY-----"),
    # Kubernetes secret data blocks (base64 blobs after data: keys are handled via key=value rule)
    (re.compile(r"(?i)(x-api-key\s*[:=]\s*)(\S+)"), r"\1" + MASK),
]

_SECRET_KEY_NAMES = re.compile(
    r"(?i)^(.*)(password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret|credential)(.*)$"
)


def redact_text(text: str) -> str:
    if not text:
        return text
    out = text
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


def contains_secret(text: str) -> bool:
    """True if any secret pattern matches the text."""
    if not text:
        return False
    for pattern, _ in _PATTERNS:
        if pattern.search(text):
            return True
    return False


def redact(obj: Any, _depth: int = 0) -> Any:
    """Recursively redact strings inside dicts/lists. Keys that look like secrets are masked wholesale."""
    if _depth > 25:
        return MASK
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        result: dict[Any, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and _SECRET_KEY_NAMES.match(k) and not k.lower().endswith(("_name", "_ref", "_path", "_file", "_id")):
                result[k] = MASK if v not in (None, "", False) else v
            else:
                result[k] = redact(v, _depth + 1)
        return result
    if isinstance(obj, (list, tuple)):
        return [redact(v, _depth + 1) for v in obj]
    return obj
