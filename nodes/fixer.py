"""nodes/fixer.py —— 修复 Agent

继承 BaseLLMAgent，工具为沙盒。

输入：state.code_history[-1] + 最新审查报告中的未解决 issue + 回归用例
输出：payload = {"code", "explanation", "result", "fixed_issue_ids"}
      —— Engine 负责写入新版本、执行确定性回归、落 FixRecord

工作方式（Design.md §3.4）：
    1. 按 issue 列表做最小化修复，禁止顺手重构
    2. 修复后调用沙盒执行，确认自身可运行
    3. 回归：prompt 要求其自带回归用例；Engine 还会用 Reviewer 记录的 snippet
       做一次确定性回归（不依赖模型自觉）
    4. 标记本轮修复的 issue id（RESOLVED_ISSUES），供下一轮 Reviewer 复检

低危过滤的原因：一轮最多处理 MAX_ISSUES_TO_FIX 个、优先 high/medium，
避免上下文被琐事塞满导致修复质量下降。
"""

from __future__ import annotations

from config import MAX_ISSUES_TO_FIX, TEMPERATURE_FIXER
from core.base_agent import BaseLLMAgent
from core.state import Severity, number_code
from llm.schema import extract_python_block, extract_resolved_ids, extract_thinking
from nodes.prompts import FIXER_PROMPT
from tools.sandbox import TOOL_SCHEMAS


def pick_targets(report, limit: int = MAX_ISSUES_TO_FIX) -> list:
    """挑选本轮要处理的 issue：优先 medium 及以上，数量受限。

    全是 low 时也保留（否则本轮无事可做），但 Engine 会据此判断是否继续。
    """
    if report is None:
        return []
    issues = report.actionable_issues()
    if not issues:
        return []
    keyed = sorted(issues, key=lambda i: Severity.rank(getattr(i, "severity", "")),
                   reverse=True)
    picked = [i for i in keyed
              if Severity.rank(i.severity) >= Severity.rank(Severity.MEDIUM)]
    return (picked or keyed)[:limit]


class FixerAgent(BaseLLMAgent):
    name = "fixer"
    system_prompt = FIXER_PROMPT
    node_tools = TOOL_SCHEMAS
    temperature = TEMPERATURE_FIXER
    require_success = True

    # ------------------------------ 输入构造 ------------------------------
    def build_user_input(self, state) -> str:
        artifact = state.latest_code()
        if artifact is None:
            raise ValueError("没有可修复的代码版本")
        report = state.latest_report()

        issue_lines = []
        for issue in pick_targets(report):
            loc = f"L{issue.line}" if issue.line not in (None, "", 0) else "-"
            line = f"- {issue.id} [{issue.severity}][{issue.category}] {loc}: {issue.desc}"
            if issue.suggestion:
                line += f"\n  建议: {issue.suggestion}"
            issue_lines.append(line)

        case_lines = []
        for idx, case in enumerate(list(report.tests_run) if report else [], 1):
            name = case.get("name") or f"case{idx}"
            flag = "通过" if case.get("passed") else "失败"
            snippet = (case.get("snippet") or "").replace("\n", "\n  ")
            case_lines.append(f"- {name}（上一轮{flag}）:\n  ```python\n  {snippet}\n  ```")

        return "\n".join([
            f"原始需求：\n{state.requirement}",
            f"\n待修复代码（当前版本 v{artifact.version}，已加行号）：\n"
            f"```python\n{number_code(artifact.code)}\n```",
            "\n需要处理的问题（id 必须原样引用）：\n"
            + ("\n".join(issue_lines) or "（没有需要处理的 issue）"),
            "\n回归用例（修复后必须全部通过）：\n"
            + ("\n".join(case_lines) or "（上一轮没有留下用例）"),
            "\n请按 System Prompt 的要求做最小化修复，把「修复后的完整代码 + 上述回归用例」"
            "拼成一个脚本调用工具运行，最后输出代码块与 RESOLVED_ISSUES 行。",
        ])

        notes = state.notes_text()
        if notes:
            parts.insert(-1, "\n编排器提示（上一轮遗留的事实，务必处理）：\n" + notes)
        return "\n".join(parts)

    # ------------------------------ 产物解析 ------------------------------
    def parse_output(self, outcome, state) -> dict:
        code = extract_python_block(outcome.final_text)
        best = outcome.last_success()

        if not code and best is not None:
            self._log("答复中未找到代码块，回退到最后一次执行成功的代码")
            code = best["code"]
        if not code and outcome.attempts:
            code = outcome.attempts[-1]["code"]

        if best is not None:
            result = best["result"]
        elif outcome.attempts:
            result = outcome.attempts[-1]["result"]
        else:
            result = {}

        fixed_ids = extract_resolved_ids(outcome.final_text)
        if fixed_ids:
            self._log(f"声明已解决: {', '.join(fixed_ids)}")

        return {
            "code": code,
            "explanation": extract_thinking(outcome.final_text) or "（Fixer 未给出说明）",
            "result": result,
            "fixed_issue_ids": fixed_ids,
        }
