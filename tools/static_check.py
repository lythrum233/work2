"""tools/static_check.py —— 静态检查（确定性扫描）

职责：
    用 ast 等确定性手段扫描代码，产出规范化 Issue 结构（dict），
    用来给 Reviewer 提供「确定存在的线索」，从而把有限的模型注意力
    留给真正需要判断的问题。

定位（Design.md §3.3）：
    确定性规则能查的问题，不占模型推理。未安装第三方库时静默降级，
    绝不影响主流程；任何内部异常都被吞掉并返回空列表。

注意：
    返回的是 dict（与 ReviewReport.issues 的字段一致），而非 Issue 对象，
    这里刻意不依赖 core.state，保持工具层纯净。
"""

from __future__ import annotations

import ast

DANGEROUS_CALLS = {"eval", "exec", "compile", "os.system", "subprocess.call",
                   "subprocess.Popen", "pickle.loads", "shutil.rmtree"}


def _call_name(node: ast.Call) -> str:
    """尽量还原调用的名字（支持 a.b.c() 形式）。"""
    parts = []
    cur = node.func
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def _imported_names(tree: ast.AST) -> list:
    """收集所有 import 进来的名字，用于检测未使用 import。"""
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append((alias.asname or alias.name.split(".")[0], node.lineno))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.append((alias.asname or alias.name, node.lineno))
    return names


def check_syntax(code: str) -> list:
    """语法层面的确定性检查。返回 issue 列表（dict）。"""
    issues = []
    try:
        tree = ast.parse(code or "")
    except SyntaxError as e:
        issues.append({
            "id": "S0",
            "category": "correctness",
            "severity": "high",
            "line": e.lineno,
            "desc": f"语法错误: {e.msg}",
            "suggestion": "修正语法错误后再运行",
        })
        return issues
    except Exception:
        return issues

    # 1) 危险调用
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name and name in DANGEROUS_CALLS:
                issues.append({
                    "id": f"S{len(issues) + 1}",
                    "category": "security",
                    "severity": "medium",
                    "line": node.lineno,
                    "desc": f"使用了危险调用 {name}()",
                    "suggestion": "确认输入可控；作业场景建议改用安全等价实现",
                })

    # 2) 裸 except（会吞掉所有异常，掩盖真实问题）
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            issues.append({
                "id": f"S{len(issues) + 1}",
                "category": "robustness",
                "severity": "low",
                "line": node.lineno,
                "desc": "使用了裸 except，会吞掉所有异常",
                "suggestion": "捕获具体的异常类型",
            })

    # 3) 未使用的 import
    imported = _imported_names(tree)
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            cur = node
            while isinstance(cur, ast.Attribute):
                cur = cur.value
            if isinstance(cur, ast.Name):
                used.add(cur.id)
    for name, lineno in imported:
        if name not in used and name != "*":
            issues.append({
                "id": f"S{len(issues) + 1}",
                "category": "style",
                "severity": "low",
                "line": lineno,
                "desc": f"导入了未使用的模块/名字: {name}",
                "suggestion": "删除无用 import",
            })

    # 4) 函数命名不符合 snake_case
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fname = node.name
            if fname and not fname.islower() and "_" not in fname:
                issues.append({
                    "id": f"S{len(issues) + 1}",
                    "category": "style",
                    "severity": "low",
                    "line": node.lineno,
                    "desc": f"函数名 {fname} 不符合 snake_case 规范",
                    "suggestion": "改用小写下划线命名",
                })

    return issues


def check(code: str) -> list:
    """统一入口：任何情况下都不抛异常。"""
    try:
        return check_syntax(code)
    except Exception:
        return []


def format_issues(issues: list) -> str:
    """把静态检查结果渲染成给 LLM 看的文本线索。"""
    if not issues:
        return "（静态检查未发现明显问题）"
    lines = []
    for issue in issues:
        line = issue.get("line")
        loc = f"L{line}" if line not in (None, "", 0) else "-"
        lines.append(
            f"- [{issue.get('severity', 'low')}][{issue.get('category', '')}] "
            f"{loc} {issue.get('desc', '')}"
            + (f"｜建议: {issue['suggestion']}" if issue.get("suggestion") else "")
        )
    return "\n".join(lines)


if __name__ == "__main__":
    sample = "import os\ndef Foo():\n    try:\n        eval('1')\n    except:\n        pass\n"
    print(format_issues(check(sample)))
