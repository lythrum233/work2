"""core/node.py —— 节点统一契约

对应 Design.md §7.1：
    Engine 只认识 BaseNode.invoke(state) -> NodeResult，
    因此新增一个 Agent 无需改动 Engine（可扩展性）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class NodeStatus:
    OK = "OK"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"


@dataclass
class NodeResult:
    """节点统一返回：表达「建议」，由 Engine 独占提交到状态白板。"""

    status: str = NodeStatus.OK
    output: dict | None = None
    error: str | None = None
    metrics: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == NodeStatus.OK

    @classmethod
    def success(cls, output: dict, metrics: dict | None = None) -> "NodeResult":
        return cls(status=NodeStatus.OK, output=output, metrics=metrics or {})

    @classmethod
    def failure(cls, error: str, metrics: dict | None = None) -> "NodeResult":
        return cls(status=NodeStatus.ERROR, output=None, error=error, metrics=metrics or {})

    def __str__(self) -> str:
        if self.ok:
            return f"NodeResult(OK, keys={list((self.output or {}).keys())})"
        return f"NodeResult(ERROR, {(self.error or '')[:80]})"


class BaseNode(ABC):
    """所有业务 Agent 节点的抽象基类。"""

    name: str = "base"

    @abstractmethod
    def invoke(self, state) -> NodeResult:
        """读取全局状态，产出一个建议变更的小包。

        约定：
            1. 不得修改传入的 state（Engine 是唯一写者）；
            2. 不得直接调用其它节点；
            3. 任何异常都应被节点内部消化为 NodeResult(ERROR)，而非向上抛。
        """
