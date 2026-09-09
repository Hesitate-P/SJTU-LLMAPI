"""FR12/FR13 响应归一：客户端可见模型名回写与 <think> 前缀剥离。

归一失败的唯一后果是"少归一"，绝不丢数据、绝不抛异常（降级原样透出）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("gateway")

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


def _strip_think(message: dict) -> None:
    """就地剥离单个 message 的 <think> 前缀；不满足条件则原样不动。

    仅当 content 为 str 且以 <think> 开头且含闭合标签：
    - 闭合前的内容（去开头标签）作为思考文本；已有 reasoning_content
      则前缀拼接（think_text + 原值），否则新建；
    - content = 闭合标签后的剩余内容（lstrip）。
    """
    content = message.get("content")
    if not isinstance(content, str):
        return
    if not content.startswith(_THINK_OPEN) or _THINK_CLOSE not in content:
        return
    close_at = content.index(_THINK_CLOSE)
    think_text = content[len(_THINK_OPEN):close_at]
    rest = content[close_at + len(_THINK_CLOSE):].lstrip()
    existing = message.get("reasoning_content")
    if isinstance(existing, str):
        message["reasoning_content"] = think_text + existing
    else:
        message["reasoning_content"] = think_text
    message["content"] = rest


def normalize_response(j: dict, client_model: str) -> dict:
    """FR12 非流式响应归一（就地修改并返回 j）：
    1. j["model"] = client_model；
    2. 逐 choices[*].message 剥离 <think> 前缀；
    3. 任何异常 / 结构不符合 chat 形状（非 dict、choices 缺失或非 list）
       → 返回原对象（降级，绝不抛）。
    """
    try:
        if not isinstance(j, dict) or not isinstance(j.get("choices"), list):
            return j
        j["model"] = client_model
        for choice in j["choices"]:
            if isinstance(choice, dict):
                message = choice.get("message")
                if isinstance(message, dict):
                    _strip_think(message)
        return j
    except Exception:  # noqa: BLE001 —— 归一绝不向上抛
        logger.warning("非流式响应归一失败，降级原样返回", exc_info=True)
        return j
