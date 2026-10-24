"""nodes/reviewer.py —— 审查 Agent

继承 BaseLLMAgent，工具为沙盒（跑自己写的测试用例）。

输入：state.code_history[-1]（待审代码）+ 原始需求
输出：payload = {"review_report": ReviewReport, "static_hints": list}
      —— Engine 负责把 report 写入 review_history（节点不写状态）

工作方式（Design.md §3.3）：
    1. 按维度静态审查：正确性 / 边界 / 健壮性 / 复杂度 / 规范 / 安全 / 可读性
    2. 针对可疑点编写边界测试用例，调用沙盒真实执行，用事实而非猜测支撑结论
    3. 输出严格 JSON（ReviewReport），解析失败走三级兜底，再失败则降级不中断流程

关键设计：
    审查结论必须结构化，否则 Fixer 无法精确定位；禁止输出自由文本 blob。
    tests_run 中的 snippet 会被 Engine 拿去当回归用例，形成「用例传递」契约。

require_success = False 的原因：
    它的测试脚本可能刻意让某个用例失败（脚本整体仍正常退出），
    或被测代码本身会抛异常——这都不代表节点失败。
"""

from __future__ import annotations

from config import TEMPERATURE_REVIEWER
from core.base_agent import BaseLLMAgent
from core.state import Issue, ReviewReport, Severity, Verdict, number_code
from llm.schema import parse_review_report
from nodes.prompts import REVIEWER_PROMPT
from tools import static_check
from tools.sandbox import TOOL_SCHEMAS
from tools.test_runner import summarize_cases


class ReviewerAgent(BaseLLMAgent):
    name = "reviewer"
    system_prompt = REVIEWER_PROMPT
    node_tools = TOOL_SCHEMAS
    temperature = TEMPERATURE_REVIEWER
    require_success = False

    # ------------------------------ 输入构造 ------------------------------
    def build_user_input(self, state) -> str:
        artifact = state.latest_code()
        if artifact is None:
            raise ValueError("没有可审查的代码版本")

        static_hints = static_check.check(artifact.code)
        round_index = len(state.review_history)

        parts = [
            f"原始需求：\n{state.requirement}",
            f"\n待审查代码（候选版本 v{artifact.version}，已加行号）：\n"
            f"```python\n{number_code(artifact.code)}\n```",
            "\n静态检查线索（确定性扫描，仅供参考，请自行判断是否真有问题）：\n"
            f"{static_check.format_issues(static_hints)}",
            f"\n本次是第 {round_index + 1} 轮审查。",
        ]

        if round_index > 0:
            parts.append(
                "此前已经过多轮修复，请重点确认：上一轮指出的问题是否真的解决，"
                "以及修复过程中是否引入了新问题。"
            )

        if artifact.result and artifact.result.get("stdout"):
            parts.append(
                "\n该版本最近一次运行的输出：\n"
                f"```\n{artifact.result['stdout'][:800]}\n```"
            )

        notes = state.notes_text()
        if notes:
            # 编排器留下的跨轮次提示（例如上一轮修复导致的回归倒退）
            parts.append("\n编排器提示（务必优先处理）：\n" + notes)

        parts.append(
            "\n请按 System Prompt 的要求：编写边界测试用例 → 调用工具执行 → "
            "然后只输出 JSON 结论。"
        )
        return "\n".join(parts)

    # ------------------------------ 产物解析 ------------------------------
    def parse_output(self, outcome, state) -> dict:
        data = parse_review_report(outcome.final_text)

        issues = [Issue.from_dict(i) for i in data.get("issues", [])]
        report = ReviewReport(
            verdict=data.get("verdict", "FAIL"),
            score=data.get("score", 0),
            summary=data.get("summary", ""),
            issues=issues,
            tests_run=data.get("tests_run", []),
            parse_failed=data.get("parse_failed", False),
            raw=data.get("raw", ""),
        )

        overruled = self._normalize_contradiction(report)
        if overruled:
            self._log(f"审查自相矛盾(PASS 但含 high: {', '.join(overruled)})，改判 FAIL")

        if report.parse_failed:
            self._log(f"审查结论解析失败: {data.get('parse_error', '')}")
        else:
            self._log(
                f"审查完成 verdict={report.verdict} score={report.score} "
                f"issues={len(report.issues)} 用例={summarize_cases(report.tests_run)}"
            )

        current = state.latest_code()
        return {
            "review_report": report,
            "static_hints": static_check.check(current.code) if current else [],
        }

    @staticmethod
    def _normalize_contradiction(report: ReviewReport):
        """消除「verdict=PASS 却带着高危 issue」的自相矛盾。

        实测遇到过：模型判 PASS、score 95，却在 issues 里列了一条 high。
        Engine 若盲信 PASS，这条记录在案的缺陷就永远不会被修复——它既不是
        「通过」，也没进入修复流程，属于沉默失败。

        取值规则（宁可多修一轮，也不静默放行）：
            PASS + 未解决 high   → 以证据为准，改判 FAIL（交给 Fixer）
            PASS + medium / low  → 保持 PASS，当作改进建议（避免过度修复）
        """
        if report.verdict != Verdict.PASS:
            return
        blocking = [i for i in report.open_issues()
                    if Severity.rank(i.severity) >= Severity.rank(Severity.HIGH)]
        if not blocking:
            return

        report.verdict = Verdict.FAIL
        report.summary = (report.summary or "") + f"｜改判：{len(blocking)} 个 high 未解决"
        return [i.id for i in blocking]
