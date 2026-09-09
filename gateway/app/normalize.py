"""FR12/FR13 响应归一：客户端可见模型名回写与 <think> 前缀剥离/改投。

归一失败的唯一后果是"少归一"，绝不丢数据、绝不抛异常（降级原样透出）。
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("gateway")

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"
_CLOSE_LEN = len(_THINK_CLOSE)  # 8
_HOLD_BACK = _CLOSE_LEN - 1  # 7：THINK 态可能构成 </think> 半标签的滞留上限


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


class StreamNormalizer:
    """FR13 流式归一状态机：SSE ``data:`` 行级 model 回写 + <think> 改投。

    - THINK/NORMAL 两态，从 NORMAL 起步；仅当首个含非空 content 的 delta
      以 ``<think>`` 开头才切 THINK（role/空 content/usage/[DONE] 行不参与
      起始判定，只做 model 回写）。
    - THINK 态：content 增量改投 ``delta.reasoning_content``（content 置 ""）。
      ``</think>`` 可能跨 data 事件分裂（发生在 JSON 字符串内部即跨行），
      故滞留尾部 ≤7 字符等待下段拼接；出现闭合则其后内容回到 content 并切
      NORMAL。
    - 任何解析/重序列化/编码失败 → 该行原样透出 + warning（``degraded``
      计数自增），绝不让归一杀掉成功流。

    行级接口约定：``feed``/``finish`` 的行均**含 ``data:`` 前缀、不含行尾
    换行**；bytes 级接口（``feed_bytes``/``finish_bytes``）供 forward 直接
    喂上游 chunk，内部完成跨 chunk 的按行组装。
    """

    def __init__(self, client_model: str) -> None:
        self.client_model = client_model
        self.degraded = 0  # 降级行计数（JSON 解析失败 / UTF-8 编解码失败）
        self._in_think = False  # THINK 态标志
        self._decided = False  # 首个非空 content 是否已做过 think 起始判定
        self._tag_tail = ""  # THINK 态滞留的 </think> 半标签候选尾
        self._line_buf = bytearray()  # feed_bytes 的跨 chunk 行缓冲

    # ---------- 行级（str）接口 ----------

    def feed(self, line: str) -> str:
        """输入一条完整 SSE 行（含前缀、不含换行），输出归一后的同格式行。"""
        if not line.startswith("data:"):
            return line  # 注释/空行/event 等其他 SSE 字段：原样
        payload = line[len("data:"):].lstrip(" ")
        if not payload.strip() or payload.strip() == "[DONE]":
            return line  # 空载荷 / 结束标记：原样
        try:
            j = json.loads(payload)
        except Exception:
            return self._degrade(line, "JSON 解析失败")
        if not isinstance(j, dict):
            return line  # 合法 JSON 但非对象：原样
        changed = False
        if "model" in j and j["model"] != self.client_model:
            j["model"] = self.client_model
            changed = True
        if isinstance(j.get("choices"), list):
            for choice in j["choices"]:
                if isinstance(choice, dict):
                    delta = choice.get("delta")
                    if isinstance(delta, dict) and self._redirect(delta):
                        changed = True
        if not changed:
            return line  # 无改动：保留原始行（不重排格式）
        try:
            return "data: " + json.dumps(j, ensure_ascii=False)
        except Exception:  # noqa: BLE001 —— 归一绝不向上抛
            return self._degrade(line, "重序列化失败")

    def finish(self) -> list[str]:
        """流结束冲刷：THINK 态滞留尾按 reasoning_content 增量补发（可为空）。"""
        if not self._in_think or not self._tag_tail:
            return []
        tail, self._tag_tail = self._tag_tail, ""
        event = {"model": self.client_model,
                 "choices": [{"index": 0,
                              "delta": {"content": "", "reasoning_content": tail}}]}
        return ["data: " + json.dumps(event, ensure_ascii=False)]

    # ---------- bytes 级接口（forward 集成：跨 chunk 行组装）----------

    def feed_bytes(self, chunk: bytes) -> bytes:
        """喂入一个上游 bytes 块（可能含多行/半行），返回归一后的同内容 bytes。

        按 ``\\n`` 切出完整行逐行归一；不完整行滞留缓冲，待后续块或
        ``finish_bytes`` 补齐。仅含半行时返回 ``b""``。
        """
        self._line_buf.extend(chunk)
        parts: list[bytes] = []
        while True:
            i = self._line_buf.find(b"\n")
            if i < 0:
                break
            raw = bytes(self._line_buf[:i])
            del self._line_buf[:i + 1]
            parts.append(self._feed_bytes_line(raw))
            parts.append(b"\n")
        return b"".join(parts)

    def finish_bytes(self) -> bytes:
        """流结束：冲刷残余行缓冲（补 ``\\n``）与 THINK 滞留尾；无残余返回 ``b""``。"""
        parts: list[bytes] = []
        if self._line_buf:
            raw = bytes(self._line_buf)
            self._line_buf.clear()
            parts.append(self._feed_bytes_line(raw))
            parts.append(b"\n")
        for line in self.finish():
            try:
                parts.append(line.encode("utf-8"))
            except UnicodeEncodeError:
                self.degraded += 1
                logger.warning("流结束冲刷滞留尾编码失败，该增量丢弃（降级）")
                continue
            parts.append(b"\n")
        return b"".join(parts)

    # ---------- 内部 ----------

    def _feed_bytes_line(self, raw: bytes) -> bytes:
        """单行 bytes 的 decode→归一→encode；任一步失败原样透出。"""
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError:
            self.degraded += 1
            logger.warning("SSE 行 UTF-8 解码失败，原样透出（降级）")
            return raw
        out = self.feed(line)
        try:
            return out.encode("utf-8")
        except UnicodeEncodeError:
            # 改写后含未配对代理（上游 "\\ud800" 转义）：整行原样降级
            self._degrade(line, "重编码失败（未配对代理）")
            return raw

    def _degrade(self, line: str, why: str) -> str:
        """降级记账：计数 + warning，返回原行。"""
        self.degraded += 1
        logger.warning("SSE data 行归一降级（%s），原样透出：%.120s", why, line)
        return line

    def _redirect(self, delta: dict) -> bool:
        """就地改投单个 delta 的 content 增量；返回是否发生修改。"""
        content = delta.get("content")
        if not isinstance(content, str) or content == "":
            return False  # 无 content 增量：不参与判定也不改投
        if not self._in_think:
            if self._decided:
                return False  # 已定局 NORMAL：content 直通
            self._decided = True
            if not content.startswith(_THINK_OPEN):
                return False  # 首个非空 content 无 <think> 前缀：永久 NORMAL
            self._in_think = True
            content = content[len(_THINK_OPEN):]
        combined = self._tag_tail + content
        close_at = combined.find(_THINK_CLOSE)
        if close_at >= 0:
            post = combined[close_at + _CLOSE_LEN:]
            self._tag_tail = ""
            self._in_think = False  # 闭合：其后内容回到 content
            self._apply_think(delta, combined[:close_at])
            delta["content"] = post
            return True
        if len(combined) > _HOLD_BACK:
            emit, self._tag_tail = combined[:-_HOLD_BACK], combined[-_HOLD_BACK:]
        else:
            emit, self._tag_tail = "", combined  # 全部滞留等待拼接判断
        self._apply_think(delta, emit)
        delta["content"] = ""
        return True

    @staticmethod
    def _apply_think(delta: dict, increment: str) -> None:
        """把 think 增量并入 delta.reasoning_content（已有则前接，不丢）。"""
        if increment == "":
            return
        existing = delta.get("reasoning_content")
        delta["reasoning_content"] = (existing if isinstance(existing, str) else "") + increment
