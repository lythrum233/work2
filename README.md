# 多 Agent 协作代码审查 Workflow

> 课程：SE2026 · Homework2（Simple Workflow MVP）
> 选题方向：**代码审查流程** —— 编码 Agent + 审查 Agent + 修复 Agent
> 状态：✅ 已实现（代码全量就位，本地自测 11/11 通过；端到端需自行配置 API Key）

---

## 1. 项目简介

一个由三个 Agent 协作完成的多智能体 Workflow：**编码 → 审查 → 修复 → 复审**，直到代码通过审查或达到轮次上限。

核心思路是把作业 1「单个代码生成 Agent」升级为一条流水线：

- **CoderAgent**（复用作业 1）负责让代码**跑得通**——生成代码后必须在沙盒中真实执行，报错则按完整 traceback 自修复；
- **ReviewerAgent** 负责审视代码**跑得好不好**——静态审查之外，还会自己编写边界测试用例并真跑，用执行事实而非模型猜测支撑结论；
- **FixerAgent** 接收结构化的审查意见做**最小化修复**，修复后必须通过原失败用例的回归验证，证明「修好且没改坏」。

三者不互相直接调用，统一由 `WorkflowEngine` 编排，通过**全局状态白板 + 结构化消息**协作。

---

## 2. 核心特性

| 特性 | 说明 |
|---|---|
| 三 Agent 端到端闭环 | Coder → Reviewer → Fixer → Reviewer …，收敛或达上限终止 |
| 双层闭环，职责不重叠 | 内层：运行时自愈（作业1已有）；外层：质量修复（本次新增） |
| 结构化消息协议 | `AgentMessage` + JSON化的 `ReviewReport`，而非自然语言 blob |
| 全局状态白板 | 唯一真相源 `WorkflowState`，单一写者，append-only 版本链 |
| 审查结论可动手验证 | Reviewer 自己写用例、自己跑沙盒，避免"只动嘴"的审查 |
| 修复带回归验证 | Fixer 修完必须重跑 Reviewer 记录的用例，拦截"修 A 坏 B" |
| 统一节点契约 | `BaseNode.invoke(state) -> NodeResult`，新增 Agent 零改引擎 |
| 全链路可追踪 | 消息日志 + `trace.log` + `state.json` 持久化，支持复现与断点续跑 |
| 降级而非崩溃 | 节点级异常隔离、轮次硬上限、JSON 解析三级兜底 |

---

## 3. 快速开始

### 3.1 环境要求

- Python 3.9 及以上
- 一个可用的通义千问（阿里云百炼）API Key

### 3.2 安装与配置

```bash
cd D:\agent-hw2
pip install -r requirements.txt
```

复制配置模板并填入 Key：

```bash
copy .env.example .env
```

`.env` 内容：

```
LLM_API_KEY=sk-你的真实Key
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-plus
```

### 3.3 运行

```bash
python main.py "写一个函数，计算列表中所有元素的倒数之和，用 [1,2,0,4] 测试"
```

退出：`exit` / `quit` / `Ctrl+C`。

> 未配置 Key 时会明确提示并以退出码 1 结束（沿用作业 1 的行为）。
> 支持 `--rounds` 调整最大修复轮次、`--timeout` 调整沙盒超时、`-q` 静默过程输出。

---

## 4. 真实运行记录

`qwen-plus`（通义千问）实测，模型温度：Coder 0.2 / Reviewer 0.1 / Fixer 0.2。

```
$ python main.py "写一个函数，计算列表中所有元素的倒数之和，用 [1,2,0,4] 测试"
================================================================
  Workflow 启动 | task_id=20261024-235224 | max_rounds=2
  需求: 写一个函数，计算列表中所有元素的倒数之和，用 [1,2,0,4] 测试
================================================================

── Stage 1 · 编码 CoderAgent ──
    · 第1步: 执行成功 (0.09s) 详细计算: 1/1 + 1/2 + 1/4 = 1 + 0.5 + 0.25 = 1.75

── 审查 ReviewerAgent（第 1 轮） ──
    · 第1步: 执行成功 (0.08s) 混合类型: unsupported operand type(s)
    · 审查完成 verdict=FAIL score=70 issues=2 用例=6/8 通过

── 修复 FixerAgent（第 1/2 轮） ──
    · 第1步: 执行成功 (0.08s) 混合类型: 1.3333333333333333
    · 声明已解决: I1, I2
  [engine] 回归通过: 8/8 通过

── 审查 ReviewerAgent（第 2 轮） ──
    · 审查完成 verdict=FAIL score=75 issues=2 用例=7/7 通过

── 修复 FixerAgent（第 2/2 轮） ──
    · 声明已解决: I1, I2
  [engine] 警告：回归用例出现倒退，交由下一轮审查复核

── 审查 ReviewerAgent（第 3 轮） ──
    · 审查完成 verdict=FAIL score=80 issues=2 用例=5/5 通过
  [engine] 流程结束: 已达最大修复轮次(2)仍未通过审查

终态    : MAX_ROUNDS_EXCEEDED（已达最大修复轮次(2)仍未通过审查）
版本数  : 3  | 审查轮次: 3  | 修复轮次: 2
LLM调用 : 13  | 沙盒执行: 5  | 耗时: 381.25s
```

几点值得说明（**这也是本项目的真实价值所在**，不是"跑通了就交"）：

| 观察 | 说明 |
|---|---|
| 分数在涨，verdict 仍是 FAIL | 70 → 75 → 80，代码质量确实在被逐步改善，但 Reviewer 每轮都能找到新问题。**这是正常的**：LLM 审查没有绝对终点，`MAX_REVIEW_ROUNDS` 就是为此存在的——有界终止而不是无限逼近。 |
| 第 2 轮抓到"回归倒退" | Fixer 为了处理布尔值把 `True` 跳过，导致上一轮通过的用例失败。**这条是 Engine 自己做确定性回归抓到的**，三个 Agent 都不知道——这正是设计 §5.2 里"Agent 只负责本轮、跨轮次事实由编排器兜底"的意义。 |
| 最终 v3 里有未使用的 `import math` | 模型为了改残留下了无用导入，被 Reviewer 如实标注为 low。说明审查不是走过场。 |
| 降级终止仍有完整产物 | 达到轮次上限后照样输出最终代码、审查报告、版本 diff、trace——不会白跑。 |

### 4.1 第二次实测（修复后，同一需求复跑）

```
$ python main.py "写一个函数，计算列表中所有元素的倒数之和，用 [1,2,0,4] 测试"

── Stage 1 · 编码 CoderAgent ──
    · 第1步: 执行成功 (0.07s) 详细计算: 1/1 + 1/2 + 1/4 = 1.75

── 审查 ReviewerAgent（第 1 轮） ──
    · 第1步: 执行成功 (0.08s) 含None列表: [1,2,None,4] -> unsupported operand type(s)
    · 审查完成 verdict=PASS score=95 issues=1 用例=6/6 通过
  [engine] 流程结束: 审查通过

终态    : PASSED（审查通过）
版本数  : 1  | 审查轮次: 1  | 修复轮次: 0
LLM调用 : 4  | 沙盒执行: 2  | 耗时: 37.15s
```

| 指标 | 修复前 | 修复后 | 变化 |
|---|---|---|---|
| 终态 | MAX_ROUNDS_EXCEEDED | **PASSED** | 收敛 |
| 耗时 | 381.25s | **37.15s** | -90% |
| LLM 调用 | 13 | **4** | -69% |
| 节点重试 | 3 次（含 reviewer） | 0 次 | 消失 |

上一轮的修复全部兑现：`重试 1/1` 不再出现，也没有自修复死循环。

这一轮又暴露一个**新问题**：`verdict=PASS` 但同时列了 `I1 [medium] 遇到 None 会抛 TypeError`
——审查员自己违反了自己「存在 medium/high 就 FAIL」的判定规则。缺陷被记录在案，
却因为整体判 PASS 而永远不会进入修复流程，属于**静默失败**。已在 §4.2 顺次修复。

### 4.2 真实运行暴露的问题及修复

首答跑去跑真模型，暴露了三个 mock 测不到的问题，均已修复：

| # | 现象 | 根因 | 修复 |
|---|---|---|---|
| 1 | 每次修复都先打印 `节点 fixer 执行失败，重试 1/1` | 模型没调工具就直接给结论 → 节点判失败 → Engine 整轮重试（重复追加 user 消息，白烧一轮） | `base_agent._loop` 增加**就地提醒**：未调用工具就答复时提示重来，不再等到节点失败 |
| 2 | Fixer 反复自修复 | 回归用例本身会抛异常（如传 `None` 应抛 `ValueError`），脚本非零退出被判为"执行失败" | `FIXER_PROMPT` 强制每条用例用 try/except 包住并打印；明确"看取值而非退出码" |
| 3 | 倒退信息只在终端闪过 | 下一轮 Reviewer 是全新对话，完全不知道上轮倒退过 | 新增 `WorkflowState.notes` 跨轮次通道，Engine 写入、Reviewer/Fixer 读取 |
| 4 | `verdict=PASS` 却列着 high/medium issue | 模型自相矛盾；Engine 盲信 PASS，记录在案的缺陷永远不会被修（静默失败） | `reviewer._normalize_contradiction()`：PASS + 未解决 **high** → 以证据为准改判 FAIL；PASS + medium/low → 保持 PASS 当作改进建议（避免过度修复） |

对应的回归用例：`test_06`（光说不练）、`test_07`（空代码不消耗自修复轮次）、
`test_08`（PASS+high 必须真修一轮）、`test_09`（PASS+medium 不升级，但建议仍保留）。

> 第 4 条里「high 才改判」是刻意的取舍：若 medium 也一律升级为 FAIL，
> 就会退回修复前「分数一直涨却永远不通过」的状态。宁可放行一个可选改进，也不要烧掉整条链路。

### 4.3 调参建议

- 想更快收敛：`python main.py "需求" --rounds 1`
- 网络不稳时：`config.py` 里把 `HTTP_TIMEOUT` 从 60 降到 30（读超时会更快重试，而不是卡满 60s）
- 想让 demo 更短：`MAX_REPAIR_ROUNDS` 调到 1

最终产物包含：

| 产物 | 位置 | 说明 |
|---|---|---|
| 最终代码 | 终端 + `runs/<task_id>/vN.py` | 通过审查的版本 |
| 审查报告 | 终端 + `state.json` | 历次 `ReviewReport`，含各 issue 的生命周期 |
| 版本 diff | 终端 | v1 → v2 的改动摘要 |
| 执行追踪 | `runs/<task_id>/trace.log` | 每条消息、每次工具调用与耗时 |
| 状态快照 | `runs/<task_id>/state.json` | 完整 `WorkflowState`，可用于复现 |

---

## 5. 项目结构

```
agent-hw2/
├─ main.py                    # CLI 入口：装配依赖 → 建 State → engine.run() → 展示产物
├─ config.py                  # 全局常量（轮次/步数上限、模型、沙盒、路径）
│
├─ core/                      # ★ 框架层：与具体业务无关
│  ├─ state.py                #   WorkflowState / CodeArtifact / ReviewReport / Issue / FixRecord
│  ├─ message.py              #   AgentMessage + MessageBus（消息日志）
│  ├─ base_agent.py           #   ★ 由作业1 agent.py 重构的「LLM→工具→回填」通用循环
│  ├─ node.py                 #   BaseNode(ABC) + NodeResult 统一契约
│  ├─ registry.py             #   NODE_REGISTRY 节点注册表
│  └─ engine.py               #   WorkflowEngine：顺序/分支/循环、异常隔离、持久化
│
├─ nodes/                     # ★ 业务层：每个文件一个 Agent
│  ├─ coder.py                #   编码 Agent（复用作业1，输出 CodeArtifact）
│  ├─ reviewer.py             #   审查 Agent（静态+动态用例，输出 ReviewReport）
│  ├─ fixer.py                #   修复 Agent（按 issue 修复 + 回归验证）
│  └─ prompts.py              #   三个 Agent 的 System Prompt
│
├─ llm/
│  ├─ llm_client.py           # ← 作业1 原样复用（requests + 重试 + APIKeyError）
│  └─ schema.py               #   ReviewReport JSON Schema 与三级降级解析器
│
├─ tools/
│  ├─ sandbox.py              # ← 作业1 tools.py 原样复用
│  ├─ test_runner.py          #   把 Reviewer/回归用例包装成可执行脚本
│  └─ static_check.py         #   可选：ast / pyflakes 确定性静态检查
│
├─ runs/<task_id>/            # 每次运行产物：state.json / trace.log / v1.py v2.py …
├─ tests/
│  ├─ test_sandbox.py         #    ← 作业1 §9.1 T1~T6 迁移
│  ├─ test_workflow_mock.py   #    假 LLM 确定性验证 5 个分支
│  └─ cases/                  #    端到端需求样例
├─ docs/workflow.mmd          # 流程图源文件
│
├─ README.md                  # 本文件
├─ Design.md                  # 设计文档
├─ requirements.txt
├─ .env.example
└─ .gitignore
```

---

## 6. 配置说明

### 6.1 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `LLM_API_KEY` | ✅ | — | 百炼控制台创建的 API Key |
| `LLM_BASE_URL` | ❌ | `https://dashscope.aliyuncs.com/compatible-mode/v1` | OpenAI 兼容端点 |
| `LLM_MODEL` | ❌ | `qwen-plus` | 模型名 |

### 6.2 可调常量（`config.py`）

| 常量 | 默认值 | 作用 |
|---|---|---|
| `MAX_REVIEW_ROUNDS` | 2 | 外层「审查→修复」最大轮次，超限则降级终止 |
| `MAX_REPAIR_ROUNDS` | 2 | 单个节点内部沙盒自修复轮次（沿用作业1） |
| `MAX_STEPS` | 10 | 单节点推理步数硬上限，防死循环 |
| `MAX_NODE_RETRY` | 1 | 节点抛异常时的重试次数 |
| `EXECUTE_TIMEOUT` | 20 | 沙盒单次执行超时（秒） |
| `MAX_OUTPUT_CHARS` | 10000 | stdout / stderr 截断上限 |
| `RUNS_DIR` | `runs/` | 运行产物落盘目录 |

---

## 7. 整体流程

```
需求 ──► ① CoderAgent ──► code_v1
              │  （生成 → 沙盒执行 → 报错则自修复）
              ▼
         ② ReviewerAgent ──► verdict?
              │  （静态审查 + 自写边界用例真实执行）
     ┌────────┴────────┐
   PASS              FAIL 且 round < MAX
     │                  │
     ▼                  ▼
   收敛             ③ FixerAgent ──► code_v2 ──► 回到 ②
                       （按 issue 修复 + 回归原失败用例）

round >= MAX 或节点不可恢复 ──► 降级终止，输出汇总（各版本 + 报错 + 建议）
```

状态机：`CODING → REVIEWING → [FIXING → REVIEWING]* → DONE / FAILED`
终止条件：`verdict == PASS` | `round >= MAX_REVIEW_ROUNDS` | 节点重试后仍异常。

---

## 8. 常见问题

| 现象 | 原因与处理 |
|---|---|
| `[Key错误] HTTP 401/403` | `.env` 中 Key 无效或未配置，修正后重跑 |
| 中文输出乱码 | 已强制 UTF-8；若仍乱码检查控制台代码页（`chcp 65001`） |
| 一直超时 | 调大 `EXECUTE_TIMEOUT`，或检查代码是否含死循环 |
| 两轮就没收敛 | 调大 `MAX_REVIEW_ROUNDS`；注意会增加 API 调用次数 |
| 想换模型 | 改 `.env` 的 `LLM_MODEL`，接口为 OpenAI 兼容格式即可 |
| `runs/` 占空间 | 可随时删除，仅为调试与演示材料 |
| 分数一直在涨却始终 FAIL | **符合预期**：LLM 审查没有天然终点，`MAX_REVIEW_ROUNDS` 就是为了有界终止。想看收敛效果可把轮次调到 3 |
| 出现 `回归用例出现倒退` | Engine 的确定性回归发现修复改坏了旧行为；该信息会写入 `state.notes` 交给下一轮处理，属正常工作机制 |

---

## 9. 本地自测

```bash
python -m unittest discover -s tests -t .
```

| 测试 | 覆盖内容 | 结果 |
|---|---|---|
| `tests/test_sandbox.py` T1~T6 | 沙盒：正常/除零/越界/超时/空代码/中文 | 6/6 通过 |
| `tests/test_workflow_mock.py` | ① 一次通过 ② 修复后通过 ③ 轮次耗尽降级 ④ 节点异常隔离 ⑤ 修复引入回归被拦截 ⑥ 模型不调工具时就地提醒 ⑦ 空代码调用不消耗自修复轮次 ⑧ PASS 却含 high 必须真修 ⑨ PASS 含 medium 不升级 | 9/9 通过 |

workflow 用例用脚本化的假 LLM 驱动，**不消耗 API 额度**且结果确定，
因此"两轮耗尽""节点抛异常""回归倒退"这些真实模型难以稳定复现的分支都能被测到。

## 10. 与作业 1 的复用关系

| 作业 1（D:\agent） | 本项目的处置 | 改动量 |
|---|---|---|
| `llm_client.py` | 原样复用 → `llm/llm_client.py` | 0 |
| `tools.py` | 原样复用 → `tools/sandbox.py` | 0 |
| `agent.py` | 抽出通用循环 → `core/base_agent.py::BaseLLMAgent` | 重构 |
| Design.md §9 测试用例 T1~T6 | 迁移 → `tests/test_sandbox.py` | 少量 |

三个 Agent 共用同一个工具循环基类，差异仅在 **System Prompt / 工具集 / 输出解析器** 三点。

---

## 11. 局限性

1. 审查质量受底层模型能力约束，可能存在漏审；已用静态检查与真实执行降低该风险，但无法根除。
2. 自动修复有轮次上限（`MAX_REVIEW_ROUNDS`），超限会降级终止并给出人工建议。
3. 沙盒为作业级简易沙盒：仅子进程隔离 + 超时强杀，**无操作系统级权限隔离**，不可用于执行不可信代码。
4. 目前仅支持 Python；支持其它语言需替换沙盒执行器与审查规则。
5. 三个 Agent 的上下文各自独立（短记忆），仅通过 `WorkflowState` 共享（长记忆），极长任务的跨轮细节可能丢失。

---

详细架构设计、消息协议、状态管理与测试方案见 [Design.md](Design.md)。
