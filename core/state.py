"""core/state.py —— 全局状态数据结构定义

对应 Design.md §5：
    1. 本文件只放数据结构，不放业务逻辑；
    2. 每个结构支持 to_dict() / from_dict()，以保证 JSON 持久化与断点恢复；
    3. code_history / review_history / fix_history 为 append-only 版本链，索引对齐：
         review_history[i] 审的是 code_history[i]
         fix_history[i]   产出的是 code_history[i + 1]

注意：虽然结构上允许修改，但**只有 Engine 可以调用写入方法**（单一写者原则），
节点只能读取，并通过 NodeResult 表达建议。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any

from core.message import AgentMessage


# ============================ 枚举 ============================
class Stage:
    """流程阶段。"""
    CODING = "CODING"
    REVIEWING = "REVIEWING"
    FIXING = "FIXING"
    DONE = "DONE"
    FAILED = "FAILED"


class Status:
    """任务终态 / 运行态。"""
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    MAX_ROUNDS_EXCEEDED = "MAX_ROUNDS_EXCEEDED"
    REVIEW_INCONCLUSIVE = "REVIEW_INCONCLUSIVE"   # 审查结论无法解析且无可用 issue
    NODE_ERROR = "NODE_ERROR"

    @staticmethod
    def is_terminal(status: str) -> bool:
        return status in (
            Status.PASSED, Status.MAX_ROUNDS_EXCEEDED,
            Status.REVIEW_INCONCLUSIVE, Status.NODE_ERROR,
        )


class Severity:
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    _ORDER = {HIGH: 3, MEDIUM: 2, LOW: 1}

    @classmethod
    def rank(cls, severity: str) -> int:
        return cls._ORDER.get((severity or "").lower(), 0)


class Verdict:
    PASS = "PASS"
    FAIL = "FAIL"


# ============================ 产物结构 ============================
@dataclass
class CodeArtifact:
    """一个版本的代码产物。"""
    version: int
    code: str
    explanation: str = ""
    result: dict = field(default_factory=dict)   # 沙盒返回的结构化结果
    source: str = "coder"                        # coder / fixer
    round: int = 0                               # 该版本产出时已完成的修复轮次

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CodeArtifact":
        return cls(
            version=data.get("version", 0),
            code=data.get("code", ""),
            explanation=data.get("explanation", ""),
            result=data.get("result", {}),
            source=data.get("source", "coder"),
            round=data.get("round", 0),
        )

    @property
    def succeeded(self) -> bool:
        return bool(self.result.get("success"))


@dataclass
class Issue:
    """一条审查问题。"""
    id: str
    category: str = "correctness"
    severity: str = Severity.MEDIUM
    line: Any = None
    desc: str = ""
    suggestion: str = ""
    status: str = "open"          # open / fixed

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Issue":
        return cls(
            id=str(data.get("id", "")),
            category=data.get("category", "correctness"),
            severity=(data.get("severity") or Severity.MEDIUM).lower(),
            line=data.get("line"),
            desc=data.get("desc", data.get("description", "")),
            suggestion=data.get("suggestion", ""),
            status=data.get("status", "open"),
        )

    def __str__(self) -> str:
        loc = f"L{self.line}" if self.line not in (None, "", 0) else "-"
        return f"[{self.id}][{self.severity}][{self.category}] {loc} {self.desc}"


@dataclass
class ReviewReport:
    """Reviewer 产出的结构化审查结论。"""
    verdict: str = Verdict.FAIL
    score: int = 0
    summary: str = ""
    issues: list = field(default_factory=list)      # list[Issue]
    tests_run: list = field(default_factory=list)   # list[dict]
    round: int = 0
    parse_failed: bool = False                      # JSON 解析是否降级过
    raw: str = ""                                   # 原始答复（截断），便于排查

    def to_dict(self) -> dict:
        data = asdict(self)
        data["issues"] = [i.to_dict() if isinstance(i, Issue) else i
                          for i in self.issues]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ReviewReport":
        issues = [Issue.from_dict(i) if isinstance(i, dict) else i
                  for i in data.get("issues", [])]
        return cls(
            verdict=data.get("verdict", Verdict.FAIL),
            score=data.get("score", 0),
            summary=data.get("summary", ""),
            issues=issues,
            tests_run=data.get("tests_run", []),
            round=data.get("round", 0),
            parse_failed=data.get("parse_failed", False),
            raw=data.get("raw", ""),
        )

    # ------------------------------ 便捷读取 ------------------------------
    @property
    def passed(self) -> bool:
        return self.verdict == Verdict.PASS

    def open_issues(self) -> list:
        return [i for i in self.issues if getattr(i, "status", "open") == "open"]

    def actionable_issues(self, limit: int | None = None) -> list:
        """按严重度排序后返回待处理 issue（默认剔除 low 由调用方决定）。"""
        items = sorted(self.open_issues(),
                       key=lambda i: Severity.rank(getattr(i, "severity", "")),
                       reverse=True)
        return items[:limit] if limit else items

    def find_issue(self, issue_id: str):
        for i in self.issues:
            if getattr(i, "id", None) == issue_id:
                return i
        return None

    def close_issues(self, issue_ids: list):
        for iid in issue_ids or []:
            issue = self.find_issue(iid)
            if issue is not None:
                issue.status = "fixed"


@dataclass
class FixRecord:
    """一次「审查 → 修复」的审计记录。"""
    round: int
    from_version: int
    to_version: int
    handled_issue_ids: list = field(default_factory=list)
    regression_passed: bool | None = None      # None = 未执行回归
    regression_detail: list = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "FixRecord":
        return cls(
            round=data.get("round", 0),
            from_version=data.get("from_version", 0),
            to_version=data.get("to_version", 0),
            handled_issue_ids=data.get("handled_issue_ids", []),
            regression_passed=data.get("regression_passed"),
            regression_detail=data.get("regression_detail", []),
            note=data.get("note", ""),
        )


# ============================ 全局状态白板 ============================
@dataclass
class WorkflowState:
    """全局状态白板（唯一真相源）。"""

    task_id: str
    requirement: str
    language: str = "python"

    stage: str = Stage.CODING
    status: str = Status.RUNNING
    round: int = 0

    code_history: list = field(default_factory=list)      # list[CodeArtifact]
    review_history: list = field(default_factory=list)    # list[ReviewReport]
    fix_history: list = field(default_factory=list)       # list[FixRecord]
    messages: list = field(default_factory=list)          # list[AgentMessage]

    metrics: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    # Engine 写给 Agent 的跨轮次提示（回归倒退、重复失败的行为差异等）。
    # 存在的意义：某些事实只有 Engine 掌握（它做了确定性回归），
    # 但下一轮 Reviewer/Fixer 又是全新的对话，必须靠这个通道传下去，
    # 否则每轮都在重复发现同一个问题。
    notes: list = field(default_factory=list)

    # ------------------------------ 构造 ------------------------------
    @classmethod
    def create(cls, requirement: str, task_id: str | None = None,
               language: str = "python") -> "WorkflowState":
        tid = task_id or time.strftime("%Y%m%d-%H%M%S")
        return cls(
            task_id=tid,
            requirement=requirement,
            language=language,
            metrics={
                "llm_calls": 0,
                "sandbox_runs": 0,
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "elapsed_sec": 0.0,
            },
        )

    # ------------------------------ 读取辅助 ------------------------------
    def latest_code(self):
        return self.code_history[-1] if self.code_history else None

    def latest_report(self):
        return self.review_history[-1] if self.review_history else None

    def version_count(self) -> int:
        return len(self.code_history)

    @property
    def final_status_message(self) -> str:
        if self.status == Status.PASSED:
            return "审查通过"
        if self.status == Status.MAX_ROUNDS_EXCEEDED:
            return f"已达最大修复轮次({len(self.fix_history)})仍未通过审查"
        if self.status == Status.REVIEW_INCONCLUSIVE:
            return "审查结论无法解析，需人工复核"
        if self.status == Status.NODE_ERROR:
            return "节点执行失败"
        return "流程未正常结束"

    # ------------------------------ 写入（仅 Engine 调用） ------------------------------
    def add_code(self, code: str, explanation: str, result: dict,
                 source: str = "coder") -> CodeArtifact:
        artifact = CodeArtifact(
            version=len(self.code_history) + 1,
            code=code,
            explanation=explanation,
            result=result,
            source=source,
            round=self.round,
        )
        self.code_history.append(artifact)
        return artifact

    def add_report(self, report: ReviewReport) -> ReviewReport:
        report.round = len(self.review_history)
        self.review_history.append(report)
        return report

    def add_fix(self, record: FixRecord) -> FixRecord:
        record.round = len(self.fix_history) + 1
        self.fix_history.append(record)
        return record

    def set_stage(self, stage: str):
        self.stage = stage

    def record_error(self, error: str, node: str = ""):
        stamp = time.strftime("%H:%M:%S")
        prefix = f"[{node}] " if node else ""
        self.errors.append(f"{stamp} {prefix}{error}")

    def add_note(self, note: str) -> str:
        """登记一条给后续轮次 Agent 看的提示（仅 Engine 调用）。"""
        self.notes.append(note)
        return note

    def notes_text(self, limit: int = 5) -> str:
        """把最近 limit 条提示拼成可嵌入 Prompt 的文本。"""
        recent = self.notes[-limit:]
        return "\n".join(f"- {n}" for n in recent) if recent else ""

    def bump_metrics(self, metrics: dict):
        for key, value in (metrics or {}).items():
            if isinstance(value, (int, float)):
                self.metrics[key] = round(self.metrics.get(key, 0) + value, 3)
            else:
                self.metrics[key] = value

    def sync_messages(self, bus_messages: list):
        """把消息总线的消息同步进状态（保持两者一致，便于持久化）。"""
        self.messages = list(bus_messages)

    # ------------------------------ 序列化 ------------------------------
    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "requirement": self.requirement,
            "language": self.language,
            "stage": self.stage,
            "status": self.status,
            "round": self.round,
            "code_history": [c.to_dict() for c in self.code_history],
            "review_history": [r.to_dict() for r in self.review_history],
            "fix_history": [f.to_dict() for f in self.fix_history],
            "messages": [m.to_dict() if isinstance(m, AgentMessage) else m
                         for m in self.messages],
            "metrics": dict(self.metrics),
            "errors": list(self.errors),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WorkflowState":
        state = cls(
            task_id=data.get("task_id", ""),
            requirement=data.get("requirement", ""),
            language=data.get("language", "python"),
            stage=data.get("stage", Stage.CODING),
            status=data.get("status", Status.RUNNING),
            round=data.get("round", 0),
            metrics=data.get("metrics", {}),
            errors=data.get("errors", []),
            notes=data.get("notes", []),
        )
        state.code_history = [CodeArtifact.from_dict(c)
                              for c in data.get("code_history", [])]
        state.review_history = [ReviewReport.from_dict(r)
                                for r in data.get("review_history", [])]
        state.fix_history = [FixRecord.from_dict(f)
                             for f in data.get("fix_history", [])]
        state.messages = [AgentMessage.from_dict(m)
                          for m in data.get("messages", [])]
        return state


# ============================ 展示辅助 ============================
def number_code(code: str) -> str:
    """给代码加上行号，方便 LLM 在 issue 中给出准确的 line。"""
    lines = (code or "").splitlines()
    width = len(str(len(lines))) if lines else 1
    return "\n".join(f"{idx:>{width}} | {line}" for idx, line in enumerate(lines, 1))
