"""config.py —— 全局常量集中定义

所有可调参数集中在此，业务代码中不出现魔法数字。
调整策略（轮次、超时、模型）只需改本文件一处。

说明：tools/sandbox.py 内部保留了作业 1 的同名默认值（EXECUTE_TIMEOUT /
MAX_OUTPUT_CHARS），那是沙盒自身的默认参数；本文件是 Workflow 层面的调参入口，
节点调用沙盒时会显式传入本文件的值。
"""

import os

# ============================ LLM 配置 ============================
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen-plus"
HTTP_TIMEOUT = 60            # 单次 HTTP 请求超时(秒)
MAX_HTTP_RETRIES = 3         # 网络类异常最大重试次数

# 不同节点用不同温度：审查要稳定，编码/修复保留一点创造力
TEMPERATURE_CODER = 0.2
TEMPERATURE_REVIEWER = 0.1
TEMPERATURE_FIXER = 0.2

# ============================ 流程控制 ============================
MAX_REVIEW_ROUNDS = 2        # 外层「审查 → 修复」最大轮次
MAX_REPAIR_ROUNDS = 2        # 单个节点内部沙盒自修复轮次（沿用作业1）
MAX_STEPS = 10               # 单节点推理步数硬上限，防死循环
MAX_NODE_RETRY = 1           # 节点抛异常时的重试次数
MAX_TOOL_NUDGE = 2           # 模型「光说不练 / 提交空代码」时的提醒次数（见 base_agent._loop）
MAX_HISTORY = 40             # 单个 Agent 的私有历史条数上限
MAX_ISSUES_TO_FIX = 8        # 一轮最多交给 Fixer 处理的问题数

# ============================ 沙盒 ============================
EXECUTE_TIMEOUT = 20         # 单次代码执行超时(秒)
MAX_OUTPUT_CHARS = 10000     # stdout / stderr 截断上限

# ============================ 路径 ============================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(BASE_DIR, "runs")   # 运行产物落盘目录
