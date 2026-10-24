"""core/base_agent.py —— 通用「LLM 推理 → 工具调用 → 结果回填」循环基类

由作业 1（D:\\agent\\agent.py）重构而来：抽掉「生成代码」的具体业务语义后，
保留完整 ReAct 闭环，作为 Coder / Reviewer / Fixer 三个节点的共同基类。

保留自作业 1：
    ReAct 式循环、工具完整结果回填、MAX_STEPS 防死循环、
    _trim_history 历史裁剪、本地生成失败汇总（不额外调 API）、
    异常兜底时移除悬空 user 消息、兼容一次请求返回多个 tool_calls

开放给子类三点（Design.md §3.1）：
    system_prompt / node_tools / build_user_input() + parse_output()

新增（相对作业 1）：
    require_success  是否要求「至少一次工具执行成功」才算节点成功。
                     Reviewer 需关闭：它的测试脚本可能刻意触发异常。
    metrics          统计 LLM 调用与沙盒执行次数，汇总进全局状态
"""

from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field

import config
from config import (
    MAX_REPAIR_ROUNDS, MAX_STEPS, MAX_HISTORY, EXECUTE_TIMEOUT, MAX_TOOL_NUDGE,
)

# 模型「光说不练」时的提醒语：遇到过 Fixer 直接输出结论而不调用沙盒，
# 若放任其返回，会导致整个节点失败并被 Engine 整轮重试（重复追加 user 消息，
# 白白多花一轮 API）。就地提醒的成本远低于整节点重试。
NO_TOOL_NUDGE = (
    "你还没有调用任何工具就给出了答复。请继续调用 execute_python_code，"
    "把完整脚本真正运行一遍，然后再按要求的格式输出最终答案。"
)

# 模型偶尔会提交 code 为空的工具调用。这种调用不算「尝试」，
# 不该消耗宝贵的自修复轮次，直接提醒重发即可。
EMPTY_CODE_NUDGE = (
    "你刚才调用 execute_python_code 时 code 参数是空的。"
    "请重新提交一份完整、可直接运行的 Python 脚本。"
)
from core.node import BaseNode, NodeResult
from tools.sandbox import execute_python_code


@dataclass
class LoopOutcome:
    """一次「推理 → 工具 → 反馈」循环的原始结果。"""

    final_text: str = ""
    attempts: list = field(default_factory=list)      # [{"code","result"}]
    llm_calls: int = 0
    exhausted: bool = False
    error: str | None = None

    @property
    def sandbox_runs(self) -> int:
        return len(self.attempts)

    @property
    def has_success(self) -> bool:
        return any(a["result"].get("success") for a in self.attempts)

    def last_success(self):
        for attempt in reversed(self.attempts):
            if attempt["result"].get("success"):
                return attempt
        return None

    def failure_summary(self) -> str:
        """本地生成的失败汇总（不额外调用 API，保证输出确定可控）。"""
        failed = [a for a in self.attempts if not a["result"].get("success")]
        repair_used = max(0, len(failed) - 1)
        lines = [
            f"代码经过初始执行 + {repair_used} 轮自修复后仍然失败，"
            f"已达最大修复轮次({MAX_REPAIR_ROUNDS})。",
            "以下是历次尝试的代码与报错：",
        ]
        for i, a in enumerate(failed, 1):
            code = a["code"].strip() or "(空代码)"
            error = (a["result"].get("error_msg")
                     or a["result"].get("stderr") or "未知错误")
            if len(error) > 800:
                error = error[:800] + "\n……(报错过长，已截断)"
            lines.append(f"\n【尝试 {i}】代码:\n```python\n{code}\n```")
            lines.append(f"报错:\n{error}")
        lines.append("\n建议：根据上述报错人工检查逻辑，或换一种实现思路后再试。")
        return "\n".join(lines)


class BaseLLMAgent(BaseNode):
    """带工具闭环的 LLM Agent 基类。"""

    name: str = "base"
    system_prompt: str = ""
    node_tools: list | None = None
    temperature: float = 0.2
    require_success: bool = True

    # 工具名 → 实现
    tool_impl = {"execute_python_code": execute_python_code}

    def __init__(self, llm, name: str | None = None, verbose: bool = True):
        self.llm = llm
        if name:
            self.name = name
        self.verbose = verbose
        # 私有上下文记忆（短记忆），跨轮次保留；共享记忆在 WorkflowState
        self.messages: list = [{"role": "system", "content": self.system_prompt}]

    # ============================ BaseNode 契约 ============================
    def invoke(self, state) -> NodeResult:
        """统一入口：构造输入 → 跑闭环 → 解析为结构化产物。

        任何异常都被消化为 NodeResult(ERROR)，保证 Engine 能隔离单节点失败。
        """
        started = time.time()
        try:
            user_input = self.build_user_input(state)
        except Exception as e:
            return NodeResult.failure(
                f"{self.name} 构造输入失败: {e}\n{traceback.format_exc()}")

        try:
            outcome = self._loop(user_input)
        except Exception as e:
            self._drop_dangling_user()
            return NodeResult.failure(
                f"{self.name} 推理循环异常: {e}\n{traceback.format_exc()}")

        metrics = {
            "llm_calls": outcome.llm_calls,
            "sandbox_runs": outcome.sandbox_runs,
            "elapsed": round(time.time() - started, 2),
        }

        # 没有一次执行成功 → 节点失败（Reviewer 通过 require_success=False 豁免）
        if self.require_success and not outcome.has_success:
            error = outcome.error or outcome.failure_summary()
            return NodeResult.failure(
                f"{self.name} 未能产出可运行的代码:\n{error}", metrics)

        try:
            payload = self.parse_output(outcome, state)
        except Exception as e:
            return NodeResult.failure(
                f"{self.name} 解析产物失败: {e}\n{traceback.format_exc()}", metrics)

        return NodeResult.success(payload, metrics)

    # ============================ 子类扩展点 ============================
    def build_user_input(self, state) -> str:
        """根据全局状态构造本轮的 user 消息。"""
        raise NotImplementedError

    def parse_output(self, outcome: LoopOutcome, state) -> dict:
        """把最终答复解析为结构化产物（由 Engine 写入状态白板）。"""
        raise NotImplementedError

    # ============================ 核心闭环 ============================
    def _loop(self, user_input: str) -> LoopOutcome:
        """作业 1 的 ReAct 闭环，去掉业务语义后在此通用化。"""
        self.messages.append({"role": "user", "content": user_input})

        attempts: list = []
        repair_rounds = 0
        nudges = 0
        llm_calls = 0

        for step in range(1, MAX_STEPS + 1):
            # ---- 1. LLM 推理 ----
            resp = self.llm.chat(self.messages, tools=self.node_tools,
                                 temperature=self.temperature)
            llm_calls += 1

            # ---- 2. 无工具调用 → 最终答复，退出循环 ----
            if not resp["tool_calls"]:
                self.messages.append({"role": "assistant", "content": resp["content"]})

                # 「光说不练」兜底：一次都没跑过代码就下结论，先就地提醒，
                # 别让节点失败后被 Engine 整轮重试（会重复追加 user 消息，白白烧一轮）
                if not attempts and nudges < MAX_TOOL_NUDGE:
                    nudges += 1
                    self._log("答复没有调用工具，已提醒其先实际运行代码")
                    self.messages.append({"role": "user", "content": NO_TOOL_NUDGE})
                    continue

                self._trim_history()
                return LoopOutcome(final_text=resp["content"],
                                   attempts=attempts, llm_calls=llm_calls)

            # ---- 3. 记录 assistant 的工具调用意图 ----
            self.messages.append({
                "role": "assistant",
                "content": resp["content"],
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                        },
                    }
                    for tc in resp["tool_calls"]
                ],
            })

            # ---- 4. 逐个执行工具，完整结果回填上下文 ----
            round_failed = False
            empty_code_call = False
            for tc in resp["tool_calls"]:
                result = self._run_tool(tc)
                if tc["name"] == "execute_python_code":
                    code = tc["arguments"].get("code") or ""
                    if not code.strip():
                        # 空调用不计入 attempts，也就不会消耗自修复轮次
                        empty_code_call = True
                        self._log("工具调用的 code 参数为空，要求重新提交")
                    else:
                        attempts.append({"code": code, "result": result})
                        self._print_step(step, result)
                if not result.get("success"):
                    round_failed = True

                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                })

            # ---- 5. 修复迭代控制 ----
            if empty_code_call and nudges < MAX_TOOL_NUDGE:
                nudges += 1
                self.messages.append({"role": "user", "content": EMPTY_CODE_NUDGE})
                continue

            if round_failed:
                if repair_rounds >= MAX_REPAIR_ROUNDS:
                    self._log(f"已达最大自修复轮次({MAX_REPAIR_ROUNDS})且仍失败，终止迭代")
                    return LoopOutcome(attempts=attempts, llm_calls=llm_calls,
                                       exhausted=True)
                repair_rounds += 1
                self._log(f"进入第 {repair_rounds}/{MAX_REPAIR_ROUNDS} 轮自修复(报错已回填)")

        # 总步数兜底（理论上到不了：修复上限远小于 MAX_STEPS）
        self._log("已达最大推理步数，请求模型总结当前进展")
        resp = self.llm.chat(self.messages)
        self.messages.append({"role": "assistant", "content": resp["content"]})
        self._trim_history()
        return LoopOutcome(final_text=resp["content"], attempts=attempts,
                           llm_calls=llm_calls + 1)

    # ============================ 辅助 ============================
    def _run_tool(self, tool_call: dict) -> dict:
        """执行工具；未知工具或异常都转成失败字典返回，绝不向上抛出。"""
        name = tool_call.get("name", "")
        impl = self.tool_impl.get(name)
        if impl is None:
            return {"success": False, "stdout": "", "stderr": "",
                    "error_msg": f"未知工具: {name}", "duration": 0.0}
        args = tool_call.get("arguments") or {}
        try:
            if name == "execute_python_code":
                # 动态取值：允许 main.py 的 --timeout 覆盖默认值
                return impl(args.get("code", ""), timeout=config.EXECUTE_TIMEOUT)
            return impl(**args)
        except Exception:
            return {"success": False, "stdout": "", "stderr": "",
                    "error_msg": "工具调用异常:\n" + traceback.format_exc(),
                    "duration": 0.0}

    def _print_step(self, step: int, result: dict):
        if not self.verbose:
            return
        if result.get("success"):
            lines = (result.get("stdout") or "").strip().splitlines()
            brief = lines[-1][:60] if lines else ""
            self._log(f"第{step}步: 执行成功 ({result.get('duration', 0)}s) {brief}")
        else:
            lines = (result.get("error_msg") or "未知错误").strip().splitlines()
            tail = lines[-1][:100] if lines else "未知错误"
            self._log(f"第{step}步: 执行失败 [{tail}]")

    def _drop_dangling_user(self):
        """移除悬空的 user 消息，保持历史一致性（沿用作业 1 做法）。"""
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages.pop()

    def _trim_history(self):
        if len(self.messages) > MAX_HISTORY:
            self.messages = [self.messages[0]] + self.messages[-MAX_HISTORY:]

    def _log(self, message: str):
        """带节点前缀的日志，便于在终端区分三个 Agent 的输出。"""
        if self.verbose:
            print(f"    · {message}")
