"""main.py —— 命令行入口

职责：
    1. 解析命令行参数（自然语言需求 / 需求文件 / 轮次 / 超时）
    2. 加载 .env 配置，初始化 LLMClient
    3. 构造 WorkflowState 并交给 WorkflowEngine.run()
    4. 展示最终产物：终态、最终代码、审查报告、未解决问题、版本 diff、指标

原则：入口只做装配与展示，不含任何流程逻辑（流程全在 core/engine.py）。
"""

from __future__ import annotations

import argparse
import difflib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

import config
from config import DEFAULT_BASE_URL, DEFAULT_MODEL, EXECUTE_TIMEOUT, MAX_REVIEW_ROUNDS
from core.engine import WorkflowEngine
from core.state import Status, Verdict
from llm.llm_client import APIKeyError, LLMClient

EXIT_WORDS = ("exit", "quit", "退出")


# ============================ 配置装配 ============================
def build_llm() -> LLMClient:
    load_dotenv()
    api_key = os.getenv("LLM_API_KEY", "").strip()
    base_url = os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
    model = os.getenv("LLM_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    return LLMClient(api_key=api_key, base_url=base_url, model=model,
                     max_retries=config.MAX_HTTP_RETRIES, timeout=config.HTTP_TIMEOUT)


def check_key(llm: LLMClient) -> bool:
    if not llm.api_key or llm.api_key.startswith("sk-你的") or "填入" in llm.api_key:
        print("\n[错误] 尚未配置 API Key！")
        print("请在 .env 中填写 LLM_API_KEY")
        print("获取地址: https://bailian.console.aliyun.com -> API-KEY")
        return False
    return True


# ============================ 结果展示 ============================
def print_report(state, run_dir: str):
    line = "=" * 64
    print(f"\n{line}\n  运行结果\n{line}")
    print(f"终态    : {state.status}（{state.final_status_message}）")
    print(f"版本数  : {len(state.code_history)}  | 审查轮次: {len(state.review_history)}"
          f"  | 修复轮次: {len(state.fix_history)}")

    metrics = state.metrics
    print(f"LLM调用 : {metrics.get('llm_calls', 0)}  | 沙盒执行: "
          f"{metrics.get('sandbox_runs', 0)}  | 耗时: "
          f"{metrics.get('elapsed_sec', 0)}s")

    if state.notes:
        print("\n----- 跨轮次提示（编排器 → Agent） -----")
        for note in state.notes:
            print(f"  · {note}")

    final = state.latest_code()
    if final is not None:
        print(f"\n----- 最终代码 (v{final.version}, 来源 {final.source}) -----")
        print(final.code if final.code.strip() else "（空）")

    report = state.latest_report()
    if report is not None:
        print(f"\n----- 最近一次审查 -----")
        print(f"结论: {report.verdict}  评分: {report.score}")
        if report.summary:
            print(f"摘要: {report.summary}")
        unresolved = [i for i in report.issues if i.status != "fixed"]
        if unresolved and report.verdict == Verdict.PASS:
            print("  （以下为改进建议：审查判定可接受，未阻塞交付）")
        if report.issues:
            print("问题清单:")
            for issue in report.issues:
                loc = f"L{issue.line}" if issue.line not in (None, "", 0) else "-"
                mark = "✓" if issue.status == "fixed" else "✗"
                print(f"  {mark} {issue.id} [{issue.severity}][{issue.category}] "
                      f"{loc} {issue.desc}")
                if issue.suggestion:
                    print(f"      建议: {issue.suggestion}")
        if report.tests_run:
            print("用例执行:")
            for case in report.tests_run:
                flag = "通过" if case.get("passed") else "失败"
                print(f"  [{flag}] {case.get('name')} -> "
                      f"{(case.get('output') or '')[:80]}")
        if report.parse_failed:
            print("（注意：本次审查结论解析失败，建议查看 trace.log 中的原始答复）")

    # 版本 diff：直观展示「改了什么」
    if len(state.code_history) >= 2:
        old = state.code_history[-2]
        new = state.code_history[-1]
        diff = list(difflib.unified_diff(
            old.code.splitlines(), new.code.splitlines(),
            fromfile=f"v{old.version}", tofile=f"v{new.version}",
            lineterm="", n=1))
        if diff:
            print(f"\n----- 版本 diff (v{old.version} → v{new.version}) -----")
            print("\n".join(diff[:40]))

    if state.fix_history:
        print("\n----- 修复记录 -----")
        for record in state.fix_history:
            flag = {True: "通过", False: "倒退", None: "未执行"}[record.regression_passed]
            print(f"  第{record.round}轮: v{record.from_version} → v{record.to_version}"
                  f" | 处理 {record.handled_issue_ids or '无'} | 回归: {flag}")

    if state.errors:
        print("\n----- 错误与降级记录 -----")
        for err in state.errors[-5:]:
            print(f"  {err[:200]}")

    print(f"\n产物目录: {run_dir}")
    print(line)


def run_once(llm: LLMClient, requirement: str, args) -> int:
    engine = WorkflowEngine(llm, max_rounds=args.rounds, verbose=not args.quiet)
    try:
        state = engine.run(requirement)
    except APIKeyError as e:
        print(f"\n[Key错误] {e}")
        print("请修正 .env 中的 LLM_API_KEY 后重新运行程序。")
        return 2
    except Exception as e:
        print(f"\n[请求失败] {type(e).__name__}: {e}")
        print("本轮处理失败，可直接重试上一条需求。")
        return 3

    print_report(state, os.path.join(config.RUNS_DIR, state.task_id))
    return 0 if state.status == Status.PASSED else 1


# ============================ 入口 ============================
def main():
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="多 Agent 协作代码审查 Workflow（编码 → 审查 → 修复）")
    parser.add_argument("requirement", nargs="?",
                        help="自然语言需求；不填则进入交互模式")
    parser.add_argument("-f", "--file", help="从文件读取需求")
    parser.add_argument("--rounds", type=int, default=MAX_REVIEW_ROUNDS,
                        help=f"最大审查-修复轮次（默认 {MAX_REVIEW_ROUNDS}）")
    parser.add_argument("--timeout", type=int, default=EXECUTE_TIMEOUT,
                        help=f"沙盒单次执行超时秒数（默认 {EXECUTE_TIMEOUT}）")
    parser.add_argument("-q", "--quiet", action="store_true", help="减少过程输出")
    args = parser.parse_args()

    config.EXECUTE_TIMEOUT = args.timeout

    llm = build_llm()
    if not check_key(llm):
        sys.exit(1)

    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            args.requirement = f.read().strip()

    if args.requirement:
        sys.exit(run_once(llm, args.requirement, args))

    # 交互模式：允许多次需求，直到输入退出词
    print(f"模型: {llm.model} | 最大轮次: {args.rounds}")
    print("输入自然语言需求，输入 exit / quit / 退出 结束。\n")
    while True:
        try:
            requirement = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break
        if not requirement:
            continue
        if requirement.lower() in EXIT_WORDS:
            print("再见！")
            break
        run_once(llm, requirement, args)


if __name__ == "__main__":
    main()
