"""Secret redaction applied before anything is persisted or printed."""
from __future__ import annotations

import os
import re
from typing import Any

from .config import load_env

_PATTERNS = [
    (re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{16,}"), "[REDACTED:api-key]"),  # not the "sk-" in "task-"
    (re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_\-]{20,}"), "[REDACTED:api-key]"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/\-]{12,}=*"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\b(authorization|cookie|set-cookie|x-api-key|api[_-]?key)(\"?\s*[:=]\s*\"?)[^\s\",;}]+"),
     r"\1\2[REDACTED]"),
]
_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.I)
_SENSITIVE_KEY = re.compile(
    r"(?i)^(authorization|cookie|set-cookie|x-api-key|api[_-]?key|password|passwd|secret|client_secret|"
    r"access_token|refresh_token|auth_token|bearer)$")
MAX_ARG_CHARS = 160


def _secret_values() -> list[str]:
    load_env()
    vals = {v.strip() for k, v in os.environ.items() if _SECRET_NAME.search(k) and len(v.strip()) >= 8}
    return sorted(vals, key=len, reverse=True)


def redact_text(s: str) -> str:
    for v in _secret_values():  # exact configured secrets first
        s = s.replace(v, "[REDACTED:env-secret]")
    for pat, repl in _PATTERNS:
        s = pat.sub(repl, s)
    return s


def redact(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {k: ("[REDACTED]" if isinstance(k, str) and _SENSITIVE_KEY.match(k) else redact(v))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    return obj


def summarize_args(args: dict) -> dict:
    """Safe tool-argument summary: redacted, long strings shortened with their length."""
    out = {}
    for k, v in redact(args).items():
        if isinstance(v, str) and len(v) > MAX_ARG_CHARS:
            v = v[:MAX_ARG_CHARS] + f"…(+{len(v) - MAX_ARG_CHARS} chars)"
        elif isinstance(v, (list, dict)):
            v = f"<{type(v).__name__} len={len(v)}>"
        out[k] = v
    return out
