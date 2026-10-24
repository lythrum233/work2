"""core/engine.py —— WorkflowEngine 编排器（流程的唯一知情者）

职责：
    1. 顺序推进：Coder → Reviewer → (Fixer → Reviewer)*
    2. 条件分支：PASS 收敛；含 medium/high issue 进 Fixer；
       解析失败且无 issue → REVIEW_INCONCLUSIVE（不空转烧 API）
    3. 循环回边：round 自增，达到 max_rounds 降级终止
    4. 节点级异常隔离：有限重试后仍失败则终止
    5. 状态写入：节点只返回 NodeResult 建议，由 Engine 独占提交到 WorkflowState
    6. 持久化：runs/<task_id>/ 下写 v*.py、trace.log、state.json

不做的事：不实现任何业务判断（如「这段代码有没有 bug」），业务全在节点内；
          也不 import 具体节点模块，只依赖 core/registry.py。
"""

from __future__ import annotations

import json
import os
import time
import traceback

from config import MAX_NODE_RETRY, MAX_REVIEW_ROUNDS, RUNS_DIR
from core.message import (
    BROADCAST, CODE_ARTIFACT, ERROR, FIX_REQUEST, REVIEW_REPORT, STATE_PATCH,
    MessageBus,
)
from core.node import NodeResult
from core.registry import DEFAULT_FLOW, NODE_REGISTRY, get_node_class
from core.state import FixRecord, Severity, Stage, Status, WorkflowState
from tools.test_runner import run_cases, summarize_cases


class WorkflowEngine:
    """编排器：顺序 + 条件分支 + 循环回边 + 异常隔离 + 持久化。"""

    def __init__(self, llm, registry: dict | None = None, flow: dict | None = None,
                 runs_dir: str = RUNS_DIR, max_rounds: int | None = None,
                 verbose: bool = True):
        self.llm = llm
        self.registry = registry if registry is not None else NODE_REGISTRY
        self.flow = flow or DEFAULT_FLOW
        self.runs_dir = runs_dir
        self.max_rounds = max_rounds if max_rounds is not None else MAX_REVIEW_ROUNDS
        self.verbose = verbose
        self.bus = MessageBus()
        self._nodes: dict = {}
        self._started = 0.0

    # ============================ 主流程 ============================
    def run(self, requirement: str, task_id: str | None = None) -> WorkflowState:
        state = WorkflowState.create(requirement, task_id)
        self.bus = MessageBus()
        self._nodes = {}
        self._started = time.time()
        run_dir = self._prepare_run_dir(state.task_id)
        self._banner(state)

        # ---------------- ① 编码 ----------------
        state.set_stage(Stage.CODING)
        self._section("Stage 1 · 编码 CoderAgent")
        coder_result = self._invoke("entry", state)
        if not coder_result.ok:
            return self._finish(state, Status.NODE_ERROR, run_dir, coder_result.error)

        payload = coder_result.output or {}
        artifact = state.add_code(
            code=payload.get("code", ""),
            explanation=payload.get("explanation", ""),
            result=payload.get("result", {}),
            source=self.flow["entry"],
        )
        self._emit(self.flow["entry"], BROADCAST, CODE_ARTIFACT, state, {
            "version": artifact.version,
            "summary": (artifact.explanation or "")[:120],
        })
        self._save_version(run_dir, artifact)
        self._flush_trace(state, run_dir)

        # ---------------- ② 审查 → 修复 → 复审 ----------------
        while True:
            state.set_stage(Stage.REVIEWING)
            self._section(f"审查 ReviewerAgent（第 {state.round + 1} 轮）")
            review_result = self._invoke("reviewer", state)
            if not review_result.ok:
                return self._finish(state, Status.NODE_ERROR, run_dir,
                                    review_result.error)

            report = state.add_report(
                (review_result.output or {}).get("review_report"))
            self._emit(self.flow["reviewer"], BROADCAST, REVIEW_REPORT, state, {
                "verdict": report.verdict,
                "score": report.score,
                "issues": len(report.issues),
                "summary": (report.summary or "")[:120],
            })
            self._flush_trace(state, run_dir)

            # ---- 终止条件 1：审查通过 ----
            if report.passed:
                return self._finish(state, Status.PASSED, run_dir)

            # ---- 终止条件 2：结论不可用，继续修复没有意义 ----
            if report.parse_failed and not report.issues:
                return self._finish(state, Status.REVIEW_INCONCLUSIVE, run_dir,
                                    "审查结论无法解析且没有可执行的 issue")

            # ---- 只剩 low 或无待处理项：视同收敛 ----
            if not self._has_actionable(report):
                self._print("仅有低危问题，视同审查通过")
                return self._finish(state, Status.PASSED, run_dir)

            # ---- 终止条件 3：轮次耗尽 ----
            if state.round >= self.max_rounds:
                return self._finish(state, Status.MAX_ROUNDS_EXCEEDED, run_dir)

            # ---------------- ③ 修复 ----------------
            state.set_stage(Stage.FIXING)
            targets = self._actionable(report)
            self._emit(self.flow["reviewer"], self.flow["fixer"], FIX_REQUEST, state, {
                "from_version": artifact.version,
                "issue_ids": [i.id for i in targets],
            })
            self._section(f"修复 FixerAgent（第 {state.round + 1}/{self.max_rounds} 轮）")

            fix_result = self._invoke("fixer", state)
            if not fix_result.ok:
                return self._finish(state, Status.NODE_ERROR, run_dir, fix_result.error)

            fix_payload = fix_result.output or {}
            new_artifact = state.add_code(
                code=fix_payload.get("code", ""),
                explanation=fix_payload.get("explanation", ""),
                result=fix_payload.get("result", {}),
                source=self.flow["fixer"],
            )
            self._save_version(run_dir, new_artifact)
            self._emit(self.flow["fixer"], BROADCAST, CODE_ARTIFACT, state, {
                "version": new_artifact.version,
                "resolved": fix_payload.get("fixed_issue_ids", []),
            })

            # ---- 确定性回归：不依赖模型自觉 ----
            regression = self._regress(new_artifact.code, report)
            target_ids = {i.id for i in targets}
            fixed_ids = [i for i in (fix_payload.get("fixed_issue_ids") or [])
                         if i in target_ids]
            state.add_fix(FixRecord(
                round=0,
                from_version=artifact.version,
                to_version=new_artifact.version,
                handled_issue_ids=fixed_ids,
                regression_passed=regression["passed"],
                regression_detail=regression["detail"],
            ))
            report.close_issues(fixed_ids)

            if regression["passed"] is False:
                # 这件事只有 Engine 知道（Reviewer 自己跑的是旧版本），必须写进
                # notes 传给下一轮 Agent，否则它们会无意识地重复踩同一个坑
                broken = [c.get("name") for c in regression["detail"]
                          if not c.get("passed")]
                state.add_note(
                    f"第 {len(state.fix_history)} 轮修复后出现回归倒退："
                    f"用例 {broken} 在修复前通过、v{new_artifact.version} 上失败了。"
                    f"请优先确认并修复这几个用例对应的行为。"
                )
                self._print("警告：回归用例出现倒退，已写入 notes 交由下一轮处理")
            elif regression["passed"] is True:
                self._print(f"回归通过: {summarize_cases(regression['detail'])}")

            artifact = new_artifact
            state.round += 1
            self._flush_trace(state, run_dir)

    # ============================ 节点调用 ============================
    def _node(self, role: str):
        name = self.flow[role]
        if name not in self._nodes:
            node_class = (self.registry[name] if isinstance(self.registry, dict)
                          else get_node_class(name))
            self._nodes[name] = node_class(self.llm, verbose=self.verbose)
        return self._nodes[name]

    def _invoke(self, role: str, state) -> NodeResult:
        """带重试的节点调用（Design.md §6「节点级」异常隔离）。"""
        node = self._node(role)
        attempts_allowed = MAX_NODE_RETRY + 1
        last = None

        for attempt in range(1, attempts_allowed + 1):
            try:
                result = node.invoke(state)
            except Exception as e:      # 双保险：节点忘了消化异常也不拖垮流程
                result = NodeResult.failure(
                    f"{node.name} 抛出未捕获异常: {e}\n{traceback.format_exc()}")

            state.bump_metrics(result.metrics or {})
            if result.ok:
                return result

            last = result
            if attempt < attempts_allowed:
                self._print(f"节点 {node.name} 执行失败，重试 {attempt}/{MAX_NODE_RETRY}")

        state.record_error((last.error or "未知节点错误"), node.name)
        self._emit(node.name, BROADCAST, ERROR, state,
                   {"error": (last.error or "")[:300]})
        return last

    # ============================ 业务辅助 ============================
    @staticmethod
    def _actionable(report) -> list:
        """待处理问题：medium 及以上（low 不值得消耗一轮修复）。"""
        if report is None:
            return []
        return [i for i in report.open_issues()
                if Severity.rank(getattr(i, "severity", ""))
                >= Severity.rank(Severity.MEDIUM)]

    def _has_actionable(self, report) -> bool:
        return bool(self._actionable(report))

    def _regress(self, code: str, report) -> dict:
        """用上一轮 Reviewer 记录的 snippet 做确定性回归。

        规则：上一轮「通过」的用例，本轮不允许变成失败。
        """
        cases = list(report.tests_run) if report else []
        if not cases:
            return {"passed": None, "detail": []}

        detail, _ = run_cases(code, cases)
        if not detail:
            return {"passed": None, "detail": []}

        before = {c.get("name"): c.get("passed") for c in cases}
        regression_ok = True
        for case in detail:
            name = case.get("name")
            if before.get(name) and not case.get("passed"):
                regression_ok = False
        return {"passed": regression_ok, "detail": detail}

    # ============================ 消息与持久化 ============================
    def _emit(self, sender: str, receiver: str, msg_type: str, state, payload: dict):
        """登记一条消息，并同步进状态（保持 bus 与 state 一致）。"""
        msg = self.bus.emit(sender, receiver, msg_type, payload,
                            round=state.round, correlation_id=state.task_id)
        state.sync_messages(self.bus.messages)
        return msg

    def _prepare_run_dir(self, task_id: str) -> str:
        path = os.path.join(self.runs_dir, task_id)
        os.makedirs(path, exist_ok=True)
        return path

    def _save_version(self, run_dir: str, artifact):
        path = os.path.join(run_dir, f"v{artifact.version}.py")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"# 版本 v{artifact.version} | 来源: {artifact.source} "
                        f"| 第 {artifact.round} 轮修复后\n")
                f.write(artifact.code)
        except OSError as e:
            self._print(f"版本文件写入失败: {e}")

    def _flush_trace(self, state, run_dir: str):
        try:
            path = os.path.join(run_dir, "trace.log")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"# task_id={state.task_id} status={state.status}\n")
                f.write(self.bus.format_trace())
                f.write("\n")
        except OSError as e:
            self._print(f"trace 写入失败: {e}")

    def _write_state(self, state, run_dir: str) -> str:
        path = os.path.join(run_dir, "state.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)
        except OSError as e:
            self._print(f"state 写入失败: {e}")
        return path

    # ============================ 收尾 ============================
    def _finish(self, state, status: str, run_dir: str,
                detail: str | None = None) -> WorkflowState:
        """统一收尾：定终态、落盘、告知。任何情况下都保证有产物。"""
        state.status = status
        state.stage = Stage.DONE if status == Status.PASSED else Stage.FAILED
        state.metrics["elapsed_sec"] = round(time.time() - self._started, 2)
        if detail:
            state.record_error(detail[:500])

        self._emit("engine", BROADCAST, STATE_PATCH, state,
                   {"status": status, "stage": state.stage})
        self._flush_trace(state, run_dir)
        self._write_state(state, run_dir)
        self._print(f"流程结束: {state.final_status_message} | 产物: {run_dir}")
        return state

    # ============================ 输出 ============================
    def _banner(self, state):
        if not self.verbose:
            return
        print("=" * 64)
        print(f"  Workflow 启动 | task_id={state.task_id} "
              f"| max_rounds={self.max_rounds}")
        print(f"  需求: {state.requirement[:70]}")
        print("=" * 64)

    def _section(self, title: str):
        if self.verbose:
            print(f"\n── {title} ──")

    def _print(self, message: str):
        if self.verbose:
            print(f"  [engine] {message}")
