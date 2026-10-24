"""core/registry.py —— 节点注册表

职责：
    维护 名称 -> Node 类 的映射（NODE_REGISTRY），供 Engine 按流程配置动态取用。

约定：
    1. 新节点在 nodes/ 下实现 BaseNode 后，仅在此处注册一行；
    2. Engine 不 import 具体节点模块，只依赖注册表，避免循环依赖；
    3. 测试可用自定义 registry 覆盖，便于注入会抛异常的故障节点。
"""

from nodes.coder import CoderAgent
from nodes.fixer import FixerAgent
from nodes.reviewer import ReviewerAgent

# 名称 -> 节点类
NODE_REGISTRY = {
    CoderAgent.name: CoderAgent,
    ReviewerAgent.name: ReviewerAgent,
    FixerAgent.name: FixerAgent,
}

# 默认流程角色配置：先编码，再反复「审查 → 修复 → 复审」
DEFAULT_FLOW = {
    "entry": "coder",
    "reviewer": "reviewer",
    "fixer": "fixer",
}


def get_node_class(name: str):
    if name not in NODE_REGISTRY:
        raise KeyError(f"未注册的节点: {name}，请检查 core/registry.py")
    return NODE_REGISTRY[name]


def register(name: str, node_class):
    """供扩展时调用：NODE_REGISTRY 之外的动态注册入口。"""
    NODE_REGISTRY[name] = node_class
