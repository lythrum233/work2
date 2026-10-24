"""core/message.py —— Agent 间消息定义与消息总线

设计要点见 Design.md §4：
    1. Agent 之间禁止直接调用，一律经由「Engine 中转 + 白板读写」；
    2. 消息既是通信载体，也是可追溯的 trace 数据源；
    3. payload 承载结构化 dict，不承载自然语言长文本。

本模块不依赖任何其它模块（除标准库），避免出现循环导入。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any

# ------------------------------ 消息类型 ------------------------------
CODE_ARTIFACT = "CODE_ARTIFACT"      # Coder / Fixer 产出的代码工件
REVIEW_REPORT = "REVIEW_REPORT"      # Reviewer 产出的审查结论
FIX_REQUEST = "FIX_REQUEST"          # 请求 Fixer 修复指定 issue
STATE_PATCH = "STATE_PATCH"          # 节点建议的状态变更
ERROR = "ERROR"                      # 节点级异常上报
BROADCAST = "broadcast"


@dataclass
class AgentMessage:
    """结构化消息：一次 Agent 间的显式通信。"""

    id: str
    sender: str
    receiver: str
    msg_type: str
    round: int = 0
    timestamp: str = ""
    correlation_id: str = ""         # 同一次修复请求的因果链，便于串联 trace
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AgentMessage":
        known = {f: data[f] for f in (
            "id", "sender", "receiver", "msg_type", "round",
            "timestamp", "correlation_id", "payload",
        ) if f in data}
        return cls(**known)

    def __str__(self) -> str:
        return f"[{self.id}] {self.sender} -> {self.receiver} | {self.msg_type} | round={self.round}"


class MessageBus:
    """append-only 消息总线：负责编号、登记消息，并输出人类可读 trace。

    Engine 独占持有，节点不得直接调用（EMP节点只能通过 NodeResult 上报）。
    """

    def __init__(self):
        self._messages: list[AgentMessage] = []

    # ------------------------------ 发送 ------------------------------
    def emit(self, sender: str, receiver: str, msg_type: str,
             payload: dict | None = None, round: int = 0,
             correlation_id: str = "") -> AgentMessage:
        seq = len(self._messages) + 1
        msg = AgentMessage(
            id=f"msg-{seq:04d}",
            sender=sender,
            receiver=receiver,
            msg_type=msg_type,
            round=round,
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            correlation_id=correlation_id,
            payload=payload or {},
        )
        self._messages.append(msg)
        return msg

    # ------------------------------ 读取 ------------------------------
    @property
    def messages(self) -> list[AgentMessage]:
        return list(self._messages)

    def size(self) -> int:
        return len(self._messages)

    def to_list(self) -> list[dict]:
        return [m.to_dict() for m in self._messages]

    # ------------------------------ 追踪 ------------------------------
    def format_trace(self) -> str:
        """导出人类可读的 trace 文本（写入 trace.log）。"""
        lines = []
        for m in self._messages:
            brief = _brief_payload(m.payload)
            lines.append(
                f"{m.timestamp} | {m.id} | {m.sender:>9} -> {m.receiver:<9} "
                f"| {m.msg_type:<14} | round={m.round} | {brief}"
            )
        return "\n".join(lines)


def _brief_payload(payload: dict) -> str:
    """把 payload 压成一行摘要，避免 trace 被长代码刷屏。"""
    if not payload:
        return "-"
    parts = []
    for key, value in payload.items():
        if isinstance(value, str):
            v = value.replace("\n", " ").strip()
            v = v[:60] + ("…" if len(v) > 60 else "")
        elif isinstance(value, (int, float, bool)):
            v = str(value)
        elif isinstance(value, list):
            v = f"[{len(value)} 项]"
        elif isinstance(value, dict):
            v = "{…}"
        else:
            v = "-"
        parts.append(f"{key}={v}")
    return " ".join(parts)
