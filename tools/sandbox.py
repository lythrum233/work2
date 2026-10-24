"""tools/sandbox.py —— 代码执行沙盒工具

来源：作业 1（D:\\agent\\tools.py），原样复用，未作逻辑改动。

简易沙盒实现(满足作业要求)：
  1. LLM 生成的代码写入临时 .py 文件
  2. 用 subprocess 在独立子进程中运行(进程级隔离)
  3. 捕获 stdout / stderr(含完整异常堆栈 traceback)
  4. 超时(timeout 参数)后强制终止子进程，防止死循环

返回结构固定为：
  {"success": bool,      # 是否成功(退出码为0)
   "stdout":  str,       # 标准输出
   "stderr":  str,       # 标准错误(原始内容)
   "error_msg": str,     # 完整异常堆栈/失败原因(成功时为空)，供上层 Agent 修复用
   "duration": float}    # 运行耗时(秒)

同时提供 TOOL_SCHEMAS：OpenAI function calling 格式的工具描述，
随请求发给 LLM，让它知道可以调用 execute_python_code。
"""

import os
import subprocess
import sys
import tempfile
import time
import traceback

EXECUTE_TIMEOUT = 20   # 默认单次代码执行超时(秒)
MAX_OUTPUT_CHARS = 10000  # stdout/stderr 安全截断上限，防止超长输出撑爆上下文


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """超长输出截断保护：保留前 limit 字符并注明总长度。"""
    if len(text) > limit:
        return text[:limit] + f"\n……(输出过长，已截断，共 {len(text)} 字符)"
    return text


# 工具描述(function calling schema)，LLM 据此决定是否调用工具
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "execute_python_code",
            "description": (
                "在沙盒中运行一段完整的 Python 代码，返回运行结果，"
                "包括 stdout 输出、完整异常堆栈 error_msg、是否超时。"
                "生成代码后必须先用它验证，确认无误再答复用户；"
                "如果执行失败，请分析 error_msg 中的报错并修复代码后重新调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "完整的可独立运行的 Python 脚本",
                    },
                },
                "required": ["code"],
            },
        },
    }
]


def execute_python_code(code: str, timeout: int = EXECUTE_TIMEOUT) -> dict:
    """在独立子进程中运行 Python 代码。

    任何异常(空代码/超时/内部错误)都不会向外抛出，而是把完整原因写入
    error_msg 字段返回，保证上层 Agent 一定能拿到全部报错用于修复。
    """
    result = {"success": False, "stdout": "", "stderr": "",
              "error_msg": "", "duration": 0.0}

    if not code or not code.strip():
        result["error_msg"] = "代码为空，请提供完整 Python 脚本"
        return result

    tmp_path = None
    try:
        # 1. 写入临时文件
        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            tmp_path = f.name

        # 2. 独立子进程运行；强制 UTF-8 输出，避免 Windows 下中文乱码
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        start = time.time()
        proc = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",       # 非法字节替换而非崩溃
            timeout=timeout,
            env=env,
        )
        result["duration"] = round(time.time() - start, 2)
        result["stdout"] = _truncate((proc.stdout or "").strip())
        result["stderr"] = _truncate((proc.stderr or "").strip())
        result["success"] = proc.returncode == 0
        if not result["success"]:
            # error_msg 携带完整异常堆栈(stderr 即 Python 的 traceback)，
            # 而非只给简短提示，方便上层 LLM 精确定位修复
            if result["stderr"]:
                result["error_msg"] = result["stderr"]
            else:
                result["error_msg"] = (
                    f"进程异常退出(退出码 {proc.returncode})，且无 stderr 输出"
                )
        return result

    except subprocess.TimeoutExpired:
        # 超时保护：subprocess 会自动 kill 子进程
        result["error_msg"] = (
            f"代码执行超时(>{timeout}秒)，已强制终止，"
            f"可能存在死循环或过于耗时的操作"
        )
        return result
    except Exception:
        # 兜底：任何意外异常都不打断 Agent 循环，返回完整内部堆栈
        result["error_msg"] = "工具内部错误:\n" + traceback.format_exc()
        return result
    finally:
        # 3. 清理临时文件
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
