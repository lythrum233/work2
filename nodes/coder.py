"""nodes/coder.py —— 编码 Agent（复用作业 1 的 Agent 并结构化其输出）

继承 BaseLLMAgent，工具为 execute_python_code。

输入：state.requirement（自然语言需求）
输出：payload = {"code", "explanation", "result"}
      —— 由 Engine 写入状态白板并分配版本号（节点不写状态）

内部闭环（内层循环，Design.md §3.5）：
    LLM 生成代码 → sandbox 执行
      → 成功：封装为代码产物返回
      → 失败：完整 traceback 回填 LLM 修复，最多 MAX_REPAIR_ROUNDS 轮
      → 仍失败：invoke() 返回 NodeResult(ERROR)，由 Engine 决定降级或终止

边界：只负责「代码能不能跑通」，不负责「写得好不好」——后者交给 Reviewer/Fixer。
"""

from __future__ import annotations

from config import TEMPERATURE_CODER
from core.base_agent import BaseLLMAgent
from llm.schema import extract_python_block, extract_thinking
from nodes.prompts import CODER_PROMPT
from tools.sandbox import TOOL_SCHEMAS


class CoderAgent(BaseLLMAgent):
    name = "coder"
    system_prompt = CODER_PROMPT
    node_tools = TOOL_SCHEMAS
    temperature = TEMPERATURE_CODER
    require_success = True

    # ------------------------------ 输入构造 ------------------------------
    def build_user_input(self, state) -> str:
        parts = [f"用户需求：\n{state.requirement}"]

        previous = state.latest_code()
        if previous is None:
            parts.append("\n请按 System Prompt 的要求生成代码、调用工具验证，并输出最终答案。")
        else:
            # 非首轮被再次调度时，带上当前版本，避免从零重写
            parts.append(
                f"\n当前已有代码版本 v{previous.version}：\n"
                f"```python\n{previous.code}\n```"
            )
            report = state.latest_report()
            if report is not None and report.summary:
                parts.append(f"\n上一轮审查意见：{report.summary}")
            parts.append("\n请在现有版本基础上继续满足需求，不要完全重写。")

        return "\n".join(parts)

    # ------------------------------ 产物解析 ------------------------------
    def parse_output(self, outcome, state) -> dict:
        """取出「已经验证通过的那份代码」。

        优先级：答复中的代码块 > 最后一次执行成功的候选代码 > 最后一次候选代码。
        """
        code = extract_python_block(outcome.final_text)
        best = outcome.last_success()

        if not code and best is not None:
            self._log("答复中未找到代码块，回退到最后一次执行成功的代码")
            code = best["code"]
        if not code and outcome.attempts:
            self._log("答复中未找到代码块且无成功执行，回退到最后一次候选代码")
            code = outcome.attempts[-1]["code"]

        if best is not None:
            result = best["result"]
        elif outcome.attempts:
            result = outcome.attempts[-1]["result"]
        else:
            result = {}

        return {
            "code": code,
            "explanation": extract_thinking(outcome.final_text) or "（Coder 未给出思路说明）",
            "result": result,
        }
