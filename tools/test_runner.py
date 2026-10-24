"""tools/test_runner.py —— 测试用例执行器

职责：
    把 Reviewer 生成的测试用例、Fixer 需要的回归用例，统一包装为可在沙盒中
    执行的完整脚本（被测代码 + 用例片段 + 结果汇总），再交给 sandbox 执行，
    最后解析出结构化的通过/失败明细。

用例格式（由 Reviewer 输出，约定字段）：
    {"name": "空列表", "snippet": "print(sum_of_reciprocals([]))"}

设计要点：
    每个用例单独 try/except 包裹，因此「某个用例失败」不会导致整个脚本非零退出，
    从而能一次拿到全部用例的结果。
"""

from __future__ import annotations

import json

from tools.sandbox import execute_python_code
from config import EXECUTE_TIMEOUT

CASES_MARKER = "__AGENT_CASES__"
MAX_CASE_OUTPUT = 300     # 单用例输出摘要长度上限


def build_case_script(code: str, cases: list) -> str:
    """把被测代码与用例片段拼成一个完整可运行脚本。"""
    lines = [
        (code or "").rstrip(),
        "",
        "# " + "-" * 60,
        "# 以下为测试用例执行框架（由 test_runner 自动生成）",
        "import io, json, traceback, contextlib",
        "_case_results = []",
        "",
        "def _run_case(name, snippet):",
        "    buf = io.StringIO()",
        "    try:",
        "        with contextlib.redirect_stdout(buf):",
        "            exec(snippet, globals())",
        "        _case_results.append({'name': name, 'passed': True,",
        "                              'output': buf.getvalue().strip()[:%d]})" % MAX_CASE_OUTPUT,
        "    except Exception:",
        "        tb = traceback.format_exc(limit=3)",
        "        _case_results.append({'name': name, 'passed': False,",
        "                              'output': tb[-%d:]})" % MAX_CASE_OUTPUT,
        "",
    ]
    for idx, case in enumerate(cases, 1):
        name = (case.get("name") or f"case{idx}") if isinstance(case, dict) else f"case{idx}"
        snippet = case.get("snippet", "") if isinstance(case, dict) else str(case)
        snippet = (snippet or "").strip()
        if not snippet:
            continue
        lines.append(f"_run_case({name!r}, {snippet!r})")
    lines.append("")
    lines.append(f'print("{CASES_MARKER}" + json.dumps(_case_results, ensure_ascii=False))')
    return "\n".join(lines)


def parse_case_output(stdout: str):
    """从沙盒 stdout 中解析用例结果；解析不到返回 None。"""
    for line in (stdout or "").splitlines():
        idx = line.find(CASES_MARKER)
        if idx >= 0:
            raw = line[idx + len(CASES_MARKER):].strip()
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return None
    return None


def run_cases(code: str, cases: list, timeout: int = EXECUTE_TIMEOUT):
    """在沙盒中执行一组用例。

    返回:
        (cases_result, sandbox_result)
        cases_result 为 None 表示脚本未能输出结构化结果（例如被测代码本身崩溃）。
    """
    if not cases:
        return [], {"success": True, "stdout": "(无用例)", "stderr": "",
                    "error_msg": "", "duration": 0.0}

    script = build_case_script(code, cases)
    sandbox_result = execute_python_code(script, timeout=timeout)
    cases_result = parse_case_output(sandbox_result.get("stdout", ""))
    if cases_result is None:
        # 被测代码本身在执行阶段就崩了：所有用例视为失败，并保留原始报错
        reason = (sandbox_result.get("error_msg")
                  or sandbox_result.get("stdout")
                  or "未能输出用例结果")[:MAX_CASE_OUTPUT]
        cases_result = [
            {"name": (c.get("name") if isinstance(c, dict) else "") or f"case{i}",
             "passed": False,
             "output": reason}
            for i, c in enumerate(cases, 1)
        ]
    return cases_result, sandbox_result


def summarize_cases(cases_result: list) -> str:
    """用例结果的一句话摘要，用于日志与终端展示。"""
    if not cases_result:
        return "未执行用例"
    passed = sum(1 for c in cases_result if c.get("passed"))
    return f"{passed}/{len(cases_result)} 通过"
