"""llm/schema.py —— 结构化输出的 Schema 定义与解析器

包含：
    REVIEW_REPORT_SCHEMA    ReviewReport 的 JSON Schema，用于提示 LLM 输出格式
    parse_review_report()   三级降级解析：
                              ① json.loads（含去掉围栏后）
                              ② 正则抽取 ```json 代码块，或按花括号配对捞取对象
                              ③ 返回降级结果（标记 parse_failed，交由 Engine 决策）
    extract_python_block()  从自然语言答复中抽取最终代码块
    extract_resolved_ids()  抽取 Fixer 声明的已解决 issue id

原则：
    解析失败绝不让整条 Workflow 崩溃——这是「错误处理与恢复」的一部分
    （Design.md §6）。
"""

from __future__ import annotations

import json
import re

MAX_RAW_CHARS = 1500       # 原始答复留存长度（便于排查，又不撑爆存储）

REVIEW_REPORT_SCHEMA = {
    "type": "object",
    "required": ["verdict", "issues"],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "summary": {"type": "string"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "severity", "desc"],
                "properties": {
                    "id": {"type": "string"},
                    "category": {"type": "string"},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "line": {"type": ["integer", "string", "null"]},
                    "desc": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
            },
        },
        "tests_run": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "snippet": {"type": "string"},
                    "passed": {"type": "boolean"},
                    "output": {"type": "string"},
                },
            },
        },
    },
}

VALID_SEVERITY = {"high", "medium", "low"}


# ============================ 通用文本抽取 ============================
def _strip_fences(text: str) -> str:
    """去掉 ```json / ``` 围栏，只留内容。"""
    fence = re.search(r"```(?:json|python|py)?\s*\n(.*?)```", text or "",
                      re.DOTALL | re.IGNORECASE)
    return fence.group(1).strip() if fence else (text or "").strip()


def _brace_object(text: str):
    """从任意文本中按花括号配对捞出一个 JSON 对象（二级兜底的核心）。"""
    text = text or ""
    start = text.find("{")
    while start != -1:
        depth, in_str, escape = 0, False, False
        for idx in range(start, len(text)):
            ch = text[idx]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:idx + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def extract_python_block(text: str) -> str:
    """抽取答复中最后一个 ```python 代码块（Coder/Fixer 的最终代码）。

    约定：模型输出的最后一个代码块即为「已验证通过的最终代码」。
    若没有代码块，返回空串，由调用方回退到「最后一次成功执行的候选代码」。
    """
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text or "",
                        re.DOTALL | re.IGNORECASE)
    return blocks[-1].strip() if blocks else ""


def extract_thinking(text: str) -> str:
    """抽取代码块之外的说明文字，作为 explanation。"""
    cleaned = re.sub(r"```(?:python|py)?\s*\n.*?```", "", text or "",
                     flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned[:MAX_RAW_CHARS]


def extract_resolved_ids(text: str) -> list:
    """抽取形如 `RESOLVED_ISSUES: I1, I3` 的声明。"""
    match = re.search(r"RESOLVED_ISSUES\s*[:：]\s*(.+)", text or "", re.IGNORECASE)
    if not match:
        return []
    raw = match.group(1).strip()
    if raw.lower() in ("none",) or raw in ("无", "-", "—"):
        return []
    ids = re.findall(r"[A-Za-z][A-Za-z0-9_-]*", raw)
    stop = {"none", "None", "NONE", "无"}
    return [i for i in ids if i not in stop]


# ============================ ReviewReport 解析 ============================
def _normalize_report(data: dict) -> dict:
    """把任意 LLM 输出规整成 ReviewReport 字段。"""
    verdict = str(data.get("verdict", "")).strip().upper()
    if verdict in ("通过", "TRUE"):
        verdict = "PASS"
    elif verdict != "PASS":
        verdict = "FAIL"

    try:
        score = int(float(data.get("score", 0) or 0))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))

    issues = []
    for idx, item in enumerate(data.get("issues") or [], 1):
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity", "medium")).strip().lower()
        if severity not in VALID_SEVERITY:
            severity = "medium"
        line = item.get("line")
        if isinstance(line, str) and line.isdigit():
            line = int(line)
        if not isinstance(line, int):
            line = None
        issues.append({
            "id": str(item.get("id") or f"I{idx}"),
            "category": str(item.get("category") or "correctness"),
            "severity": severity,
            "line": line,
            "desc": str(item.get("desc") or item.get("description") or "").strip(),
            "suggestion": str(item.get("suggestion") or "").strip(),
        })

    # issue id 去重，避免 Fixer 的 RESOLVED 引用歧义
    seen, unique = set(), []
    for issue in issues:
        if issue["id"] in seen:
            issue["id"] = f"{issue['id']}-{len(unique) + 1}"
        seen.add(issue["id"])
        unique.append(issue)

    tests_run = []
    for item in data.get("tests_run") or []:
        if not isinstance(item, dict):
            continue
        tests_run.append({
            "name": str(item.get("name") or "case").strip()[:60],
            "snippet": str(item.get("snippet") or "").strip(),
            "passed": bool(item.get("passed", False)),
            "output": str(item.get("output") or "").strip()[:300],
        })

    return {
        "verdict": verdict,
        "score": score,
        "summary": str(data.get("summary") or "").strip()[:500],
        "issues": unique,
        "tests_run": tests_run,
    }


def parse_review_report(text: str) -> dict:
    """三级降级解析 Reviewer 的答复。

    返回:
        dict，含 ReviewReport 的全部字段，外加：
            parse_failed: bool   是否走到了降级分支
            parse_error:  str    降级原因（成功时为空）
            raw:          str    原始答复（截断）
    """
    raw = (text or "").strip()[:MAX_RAW_CHARS]

    # ① 直接解析（含去掉围栏后）
    data = None
    try:
        data = json.loads(_strip_fences(text))
    except json.JSONDecodeError:
        # ② 正则/花括号配对捞对象
        data = _brace_object(text)

    if isinstance(data, dict) and ("verdict" in data or "issues" in data):
        result = _normalize_report(data)
        result["parse_failed"] = False
        result["parse_error"] = ""
        result["raw"] = raw
        return result

    # ③ 完全降级：保守判定为 FAIL 且不提供 issue，
    #    由 Engine 识别「parse_failed 且无 issue」走 REVIEW_INCONCLUSIVE 分支
    return {
        "verdict": "FAIL",
        "score": 0,
        "summary": "",
        "issues": [],
        "tests_run": [],
        "parse_failed": True,
        "parse_error": "审查结论无法解析为 JSON",
        "raw": raw,
    }
