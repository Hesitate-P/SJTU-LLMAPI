"""请求 token 估算（FR11）：字符启发式，够做路由决策即可。"""
from __future__ import annotations

_PER_MESSAGE = 4
_ZH_FACTOR, _ASCII_FACTOR, _SAFETY = 0.6, 0.25, 1.1


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def estimate_request_tokens(body: dict) -> int:
    base = 0.0
    for m in body.get("messages") or []:
        text = _text_of(m.get("content") if isinstance(m, dict) else None)
        zh = sum(1 for ch in text if ord(ch) > 0x2E80)
        base += zh * _ZH_FACTOR + (len(text) - zh) * _ASCII_FACTOR
    max_out = body.get("max_tokens") or body.get("max_completion_tokens") or 0
    return int(base * _SAFETY + _PER_MESSAGE * len(body.get("messages") or []) + max_out)
