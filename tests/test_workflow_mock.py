"""tests/test_workflow_mock.py —— Workflow 确定性测试（mock LLM）

背景：真实模型行为不稳定，难以稳定复现边界分支，故用假 LLM 固定返回做断言
      （沿用作业 1 §9.3 的方法）。

必测分支：
    1. 正常收敛       审查一次通过 → PASSED
    2. 修复后收敛     首轮 FAIL → Fixer 修好 → 复审 PASS
    3. 轮次耗尽       始终 FAIL → MAX_ROUNDS_EXCEEDED
    4. 节点异常       节点抛错 → 重试 MAX_NODE_RETRY 次 → NODE_ERROR，不崩溃
    5. 修复引入回归   Fixer 改名破坏已通过用例 → 确定性回归拦截
"""

import json
import shutil
import tempfile
import unittest

from config import MAX_NODE_RETRY
from core.engine import WorkflowEngine
from core.node import BaseNode
from core.state import Status

REQUIREMENT = "写一个加法函数 add(a, b)，并测试 add(1, 2)"

GOOD_CODE = (
    "def add(a, b):\n"
    "    return a + b\n"
    "\n"
    "print(add(1, 2))\n"
)

FIXED_CODE = (
    "def add(a, b):\n"
    "    if a is None or b is None:\n"
    "        return None\n"
    "    return a + b\n"
    "\n"
    "print(add(1, 2))\n"
)

# 把 add 改名成 sum2：上一轮通过的用例会直接 NameError
REGRESSED_CODE = (
    "def sum2(a, b):\n"
    "    return a + b\n"
    "\n"
    "print(sum2(1, 2))\n"
)

PROBE_SCRIPT = "print('reviewer probe: ok')"
NORMAL_CASE = {"name": "正常入参", "snippet": "print(add(1, 2))",
               "passed": True, "output": "3"}


# ============================ 测试替身 ============================
class ScriptedLLM:
    """按脚本顺序返回固定响应的假 LLM。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.call_count = 0

    def chat(self, messages, tools=None, temperature=None):
        self.call_count += 1
        if not self.responses:
            raise AssertionError(f"脚本响应耗尽（第 {self.call_count} 次调用）")
        return self.responses.pop(0)


class BrokenNode(BaseNode):
    """总是抛异常的节点，用于验证 Engine 的异常隔离。"""

    name = "coder"

    def __init__(self, llm, verbose: bool = False):
        self.calls = 0

    def invoke(self, state):
        self.calls += 1
        raise RuntimeError("节点故意崩溃")


def tool_call(code: str, call_id: str = "call_1"):
    return {"content": "", "tool_calls": [
        {"id": call_id, "name": "execute_python_code", "arguments": {"code": code}}
    ]}


def final(text: str):
    return {"content": text, "tool_calls": []}


def code_answer(code: str, thinking: str = "实现思路：直接返回两数之和。"):
    return final(f"{thinking}\n\n```python\n{code}\n```\n运行结果为 3。")


def fix_answer(code: str, resolved: str = "I1"):
    return final(f"增加了入参判空。\n\n```python\n{code}\n```\n\n"
                 f"RESOLVED_ISSUES: {resolved}")


def review_answer(verdict: str, score: int, issues: list, tests: list,
                  summary: str = ""):
    data = {
        "verdict": verdict,
        "score": score,
        "summary": summary,
        "issues": issues,
        "tests_run": tests,
    }
    return final("```json\n" + json.dumps(data, ensure_ascii=False) + "\n```")


def high_issue(issue_id: str = "I1"):
    return [{"id": issue_id, "category": "boundary", "severity": "high",
             "line": 2, "desc": "未处理 None 入参", "suggestion": "增加判空"}]


def medium_issue(issue_id: str = "I1"):
    return [{"id": issue_id, "category": "robustness", "severity": "medium",
             "line": 4, "desc": "遇到 None 会抛 TypeError",
             "suggestion": "增加类型检查"}]


# ============================ 测试用例 ============================
class TestWorkflow(unittest.TestCase):
    def setUp(self):
        self.runs_dir = tempfile.mkdtemp(prefix="agent-hw2-test-")

    def tearDown(self):
        shutil.rmtree(self.runs_dir, ignore_errors=True)

    def _engine(self, llm, **kwargs):
        return WorkflowEngine(llm, runs_dir=self.runs_dir, verbose=False, **kwargs)

    # ---------- 分支 1：正常收敛 ----------
    def test_01_converge_at_first_review(self):
        llm = ScriptedLLM([
            tool_call(GOOD_CODE),                       # coder 生成并执行
            code_answer(GOOD_CODE),                     # coder 最终答复
            tool_call(PROBE_SCRIPT),                    # reviewer 跑用例
            review_answer("PASS", 92, [], [NORMAL_CASE], "无明显问题"),
        ])
        state = self._engine(llm).run(REQUIREMENT)

        self.assertEqual(state.status, Status.PASSED)
        self.assertEqual(len(state.code_history), 1)
        self.assertEqual(len(state.review_history), 1)
        self.assertEqual(state.round, 0)
        self.assertIn("def add", state.latest_code().code)

    # ---------- 分支 2：修复后收敛 ----------
    def test_02_converge_after_fix(self):
        llm = ScriptedLLM([
            tool_call(GOOD_CODE),
            code_answer(GOOD_CODE),
            tool_call(PROBE_SCRIPT),
            review_answer("FAIL", 55, high_issue(), [NORMAL_CASE], "缺少边界处理"),
            tool_call(FIXED_CODE),                      # fixer 验证修复
            fix_answer(FIXED_CODE),
            tool_call(PROBE_SCRIPT),
            review_answer("PASS", 88, [], [NORMAL_CASE], "边界已处理"),
        ])
        state = self._engine(llm).run(REQUIREMENT)

        self.assertEqual(state.status, Status.PASSED)
        self.assertEqual(len(state.code_history), 2)
        self.assertEqual(state.round, 1)
        self.assertEqual(len(state.fix_history), 1)
        self.assertEqual(state.fix_history[0].handled_issue_ids, ["I1"])
        self.assertTrue(state.fix_history[0].regression_passed)
        self.assertEqual(state.review_history[0].issues[0].status, "fixed")

    # ---------- 分支 3：轮次耗尽 ----------
    def test_03_max_rounds_exceeded(self):
        llm = ScriptedLLM([
            tool_call(GOOD_CODE),
            code_answer(GOOD_CODE),
            # 第 1 轮：FAIL → 修复
            tool_call(PROBE_SCRIPT),
            review_answer("FAIL", 55, high_issue("I1"), [NORMAL_CASE]),
            tool_call(FIXED_CODE),
            fix_answer(FIXED_CODE, "I1"),
            # 第 2 轮：FAIL → 修复
            tool_call(PROBE_SCRIPT),
            review_answer("FAIL", 60, high_issue("I2"), [NORMAL_CASE]),
            tool_call(FIXED_CODE),
            fix_answer(FIXED_CODE, "I2"),
            # 第 3 轮：仍 FAIL，此时 round 已达上限
            tool_call(PROBE_SCRIPT),
            review_answer("FAIL", 62, high_issue("I3"), [NORMAL_CASE]),
        ])
        state = self._engine(llm, max_rounds=2).run(REQUIREMENT)

        self.assertEqual(state.status, Status.MAX_ROUNDS_EXCEEDED)
        self.assertEqual(len(state.code_history), 3)     # v1 + 两次修复
        self.assertEqual(len(state.review_history), 3)
        self.assertEqual(len(state.fix_history), 2)
        self.assertEqual(state.round, 2)
        # 降级终止时仍必须留下产物
        self.assertTrue(state.latest_code().code.strip())

    # ---------- 分支 4：节点异常隔离 ----------
    def test_04_node_error_isolated(self):
        llm = ScriptedLLM([])                            # 不该有任何 LLM 调用
        engine = self._engine(llm, registry={"coder": BrokenNode})

        state = engine.run(REQUIREMENT)

        self.assertEqual(state.status, Status.NODE_ERROR)
        self.assertEqual(len(state.code_history), 0)
        self.assertEqual(engine._nodes["coder"].calls, MAX_NODE_RETRY + 1)
        self.assertEqual(llm.call_count, 0)
        self.assertTrue(state.errors)

    # ---------- 分支 5：修复引入回归 ----------
    def test_05_regression_detected(self):
        llm = ScriptedLLM([
            tool_call(GOOD_CODE),
            code_answer(GOOD_CODE),
            tool_call(PROBE_SCRIPT),
            review_answer("FAIL", 55, high_issue(), [NORMAL_CASE]),
            tool_call(REGRESSED_CODE),
            fix_answer(REGRESSED_CODE),
            tool_call(PROBE_SCRIPT),
            review_answer("FAIL", 40, high_issue("I2"), [NORMAL_CASE]),
        ])
        state = self._engine(llm, max_rounds=1).run(REQUIREMENT)

        self.assertEqual(state.status, Status.MAX_ROUNDS_EXCEEDED)
        self.assertEqual(len(state.fix_history), 1)
        # 关键断言：上一轮通过的用例被改坏，确定性回归必须抓到
        self.assertFalse(state.fix_history[0].regression_passed)


    # ---------- 补充：模型「光说不练」时应就地提醒而非整节点重试 ----------
    def test_06_nudge_instead_of_node_retry(self):
        from core.state import WorkflowState
        from nodes.coder import CoderAgent

        llm = ScriptedLLM([
            final("我只说一下思路，不跑代码。"),          # 没有 tool_calls
            tool_call(GOOD_CODE),                        # 被提醒后才真的执行
            code_answer(GOOD_CODE),
        ])
        node = CoderAgent(llm, verbose=False)
        state = WorkflowState.create(REQUIREMENT)

        result = node.invoke(state)

        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(llm.call_count, 3)
        self.assertEqual(result.metrics["sandbox_runs"], 1)
        self.assertIn("def add", result.output["code"])

    # ---------- 补充：提交空代码不应消耗自修复轮次 ----------
    def test_07_empty_code_call_is_not_an_attempt(self):
        from core.state import WorkflowState
        from nodes.coder import CoderAgent

        llm = ScriptedLLM([
            tool_call("   "),                             # code 为空
            tool_call(GOOD_CODE),
            code_answer(GOOD_CODE),
        ])
        node = CoderAgent(llm, verbose=False)
        state = WorkflowState.create(REQUIREMENT)

        result = node.invoke(state)

        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(llm.call_count, 3)
        # 空调用没有计入沙盒执行次数
        self.assertEqual(result.metrics["sandbox_runs"], 1)


    # ---------- 分支 8：verdict=PASS 却带着 high issue（自相矛盾） ----------
    def test_08_pass_with_high_issue_is_overruled(self):
        llm = ScriptedLLM([
            tool_call(GOOD_CODE),
            code_answer(GOOD_CODE),
            tool_call(PROBE_SCRIPT),
            review_answer("PASS", 90, high_issue(), [NORMAL_CASE], "基本可用"),
            tool_call(FIXED_CODE),
            fix_answer(FIXED_CODE),
            tool_call(PROBE_SCRIPT),
            review_answer("PASS", 92, [], [NORMAL_CASE], "已处理"),
        ])
        state = self._engine(llm).run(REQUIREMENT)

        self.assertEqual(state.status, Status.PASSED)
        # 关键：这一条 high 必须真的被修一轮，而不是被 PASS 静默吞掉
        self.assertEqual(len(state.code_history), 2)
        self.assertEqual(state.review_history[0].verdict, "FAIL")

    # ---------- 分支 9：PASS + medium 不升级，避免过度修复 ----------
    def test_09_pass_with_medium_issue_stays_passed(self):
        llm = ScriptedLLM([
            tool_call(GOOD_CODE),
            code_answer(GOOD_CODE),
            tool_call(PROBE_SCRIPT),
            review_answer("PASS", 95, medium_issue(), [NORMAL_CASE], "仅 robustness 建议"),
        ])
        state = self._engine(llm).run(REQUIREMENT)

        self.assertEqual(state.status, Status.PASSED)
        self.assertEqual(len(state.code_history), 1)     # 没有额外修复轮
        self.assertEqual(len(state.fix_history), 0)
        # 建议本身仍要保留在报告里，不能悄悄丢掉
        self.assertEqual(len(state.review_history[0].issues), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
