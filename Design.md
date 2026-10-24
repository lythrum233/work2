# 设计文档 —— 多 Agent 协作代码审查 Workflow

> 课程：SE2026 · Homework2（Simple Workflow MVP）
> 选题方向：代码审查流程｜CoderAgent + ReviewerAgent + FixerAgent
> 版本：v1（骨架阶段，随实现迭代更新）

---

## 1. 总体架构

### 1.1 分层架构

```
┌──────────────────────────────────────────────────────────────┐
│ main.py  CLI 入口：解析参数 → 建 State → engine.run() → 展示   │
└───────────────────────────┬──────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────┐
│ core/engine.py  WorkflowEngine（流程的唯一知情者）             │
│   顺序推进 / 循环回边 / 分支判定 / 轮次上限 / 节点异常隔离       │
│   状态提交（独占写） / trace 与 state 持久化                    │
└───────┬────────────────────────────────────┬─────────────────┘
        │ 读写唯一真相源                       │ NodeResult 上报
        ▼                                    │
┌───────────────────────────┐      ┌─────────▼──────────────────┐
│ core/state.py             │      │ nodes/  三个业务 Agent       │
│   WorkflowState（白板）    │◄────►│   CoderAgent                │
│   CodeArtifact            │      │   ReviewerAgent             │
│   ReviewReport / Issue    │      │   FixerAgent                │
│   AgentMessage（消息日志） │      │   —— 统一实现 BaseNode       │
└───────────────────────────┘      └─────────┬──────────────────┘
                                              ▼
                        ┌───────────────────────────────────────┐
                        │ core/base_agent.py  BaseLLMAgent      │
                        │   「LLM推理 → 工具 → 结果回填」通用环  │
                        │   ← 由作业1 agent.py 重构而来          │
                        └───────┬──────────────────┬────────────┘
                                ▼                  ▼
                  ┌──────────────────┐  ┌────────────────────────┐
                  │ llm/llm_client.py│  │ tools/sandbox.py       │
                  │ ← 作业1 原样复用  │  │ ← 作业1 原样复用        │
                  └──────────────────┘  │ tools/static_check.py  │
                                        └────────────────────────┘
```

**分层原则**：上层依赖下层，下层绝不反向 import 上层。`core/` 不 import `nodes/`（`registry.py` 是唯一接触点），`Engine` 不 import 具体节点模块。这条约束直接支撑评分维度「可扩展性」。

### 1.2 组件职责

| 组件 | 职责 | 不做的事 |
|---|---|---|
| `main.py` | 装配依赖、读取输入、展示产物 | 不含流程逻辑 |
| `core/engine.py` | 编排：顺序/分支/循环、轮次控制、异常隔离、持久化 | 不做业务判断（如代码是否有 bug） |
| `core/state.py` | 全局状态数据结构定义 | 不含业务逻辑 |
| `core/message.py` | 消息结构与消息日志 | 不参与路由决策 |
| `core/base_agent.py` | 通用 LLM-工具循环 | 不含具体任务语义 |
| `core/node.py` | 节点抽象契约 | — |
| `core/registry.py` | 节点注册表 | — |
| `nodes/*.py` | 各自的业务语义与产物解析 | 不直接调用其它节点、不直接改全局状态 |
| `llm/llm_client.py` | HTTP 调用、重试、错误分类 | 不理解业务 |
| `tools/sandbox.py` | 子进程执行代码并返回结构化结果 | 不解释结果含义 |

---

## 2. 业务流程图

详见 [docs/workflow.mmd](docs/workflow.mmd)（可直接渲染为图片）。文字版：

```
需求 ──► ① CoderAgent ──► CodeArtifact v1
              │   LLM生成 → sandbox执行
              │   失败则完整 traceback 回填 LLM 自修复（≤ MAX_REPAIR_ROUNDS）
              ▼
         ② ReviewerAgent ──► ReviewReport（verdict / issues / tests_run）
              │   静态审查 + 自写边界用例 → sandbox 真实执行
     ┌────────┴────────┐
   PASS              FAIL 且 round < MAX
     │                  │
     ▼                  ▼
   收敛结束         ③ FixerAgent ──► CodeArtifact v2 ──► 回到 ② 复审
                       │   按 issue 最小化修复 → sandbox执行 → 回归原失败用例
                       └─ round += 1

终止条件：verdict == PASS | round >= MAX_REVIEW_ROUNDS | 节点重试后仍异常
```

### 2.1 状态机

```
                    ┌──────────► FIXING ───────┐
                    │                          │
CODING ──► REVIEWING ──► DONE(PASSED)          │  round += 1
                    │                          │
                    └──► FAILED ◄──────────────┘
                         ├─ MAX_ROUNDS_EXCEEDED（轮次耗尽，降级汇总）
                         └─ NODE_ERROR（节点重试后仍不可恢复）
```

| 终态 | 触发条件 | 产物 |
|---|---|---|
| `PASSED` | `verdict == PASS`，或待处理项只剩 low | 最终代码 + 审查报告 + 版本 diff |
| `MAX_ROUNDS_EXCEEDED` | `round >= MAX_REVIEW_ROUNDS` 且仍 FAIL | 汇总：各版本代码、历次报错、未解决 issue、人工建议 |
| `REVIEW_INCONCLUSIVE` | JSON 解析失败且 `issues` 为空（无可执行目标） | 当前代码 + 原始答复（`trace.log`） |
| `NODE_ERROR` | 节点重试 `MAX_NODE_RETRY` 次仍失败 | 已完成部分 + 错误上下文 |

> 补充 `REVIEW_INCONCLUSIVE` 的理由：三级降级都失败时若继续让 Fixer 空转，
> 只会白白消耗两轮 API；不如停在这里并给出原始答复让人复核。

---

## 3. Agent 设计

### 3.1 BaseLLMAgent（由作业 1 重构）

作业 1 的 `agent.py` 里，「推断 → 调工具 → 拿结果 → 判断成败 → 再推断」这一段与"生成代码"这一具体任务无关。抽出后作为三节点共同基类。

| 保留能力 | 位置 / 做法 |
|---|---|
| ReAct 式循环 | `invoke()` 主循环 |
| 工具结果回填 | tool 结果 JSON 序列化后追加进 `messages` |
| 防死循环 | `MAX_STEPS` 硬上限兜底 |
| 历史裁剪 | `_trim_history()`：保 system + 最近 N 条 |
| 失败汇总 | 迭代耗尽时本地生成汇总（**不额外调用 API**，输出确定可控） |
| 异常兜底 | 请求中断时移除悬空 user 消息，保持历史一致性 |

开放给子类三点：

```python
class CoderAgent(BaseLLMAgent):
    system_prompt = CODER_PROMPT      # 人设与行为约束
    tools         = [EXECUTE_TOOL]    # 可用工具集
    def parse_output(self, text, attempts) -> dict: ...
```

> 关于多个 tool_calls：作业 1 已兼容一次请求返回并行 tool_calls 的情形（逐个执行逐个回填），该行为在基类保留。

### 3.2 CoderAgent

- **输入**：`state.requirement` + 「当前第几轮、上一轮审查遗留了什么」（若非首轮）
- **输出**：`NodeResult(output={"code_artifact": {...}})`
- **内部闭环**：生成 → 沙盒执行 → 失败按完整 traceback 自修复 → 最多 `MAX_REPAIR_ROUNDS` 轮
- **边界**：只保证「代码能跑通」，不保证「写得好」

### 3.3 ReviewerAgent

审查维度：

| 维度 | 关注点 | 默认严重度权重 |
|---|---|---|
| correctness | 结果与需求是否一致 | high |
| boundary | 空输入、极值、类型异常 | high |
| robustness | 异常处理、防御是否合理 | medium |
| complexity | 时间/空间复杂度是否明显糟糕 | medium |
| style | 命名、PEP8、可读性 | low |
| security | 危险调用、注入面 | medium |

**结论一致性校验（`_normalize_contradiction`）**：模型会自相矛盾地输出
`verdict=PASS` 同时列着未解决的 high issue。节点在交付前做一次确定性归一化：
PASS 且存在未解决 **high** → 改判 FAIL；medium/low 则保持 PASS 仅作建议。
依据是「哪一侧代价更可控」，详见 §8.3.4。

**关键设计：审查必须落地执行。** Reviewer 不被允许只输出"这段可能有问题"这类猜测；对可疑逻辑必须编写边界测试用例，调用同一个 `sandbox` 真实执行，把通过/失败写进 `tests_run`。这样：

1. 审查结论有证据，避免 LLM 幻觉导致的误判；
2. 这些用例天然成为 Fixer 的**回归测试集**——Reviewer 与 Fixer 之间形成「用例传递」的隐式契约。

输出遵循 JSON Schema（见 `llm/schema.py`），示例：

```json
{
  "verdict": "FAIL",
  "score": 62,
  "summary": "功能正确，但缺少边界与异常处理",
  "issues": [
    {"id": "I1", "category": "boundary", "severity": "high",
     "line": 12, "desc": "lst 含 0 时除零崩溃", "suggestion": "跳过 0 或显式处理"},
    {"id": "I2", "category": "style", "severity": "low",
     "line": 3, "desc": "函数名不符合 snake_case", "suggestion": "重命名"}
  ],
  "tests_run": [
    {"name": "空列表", "input": "[]", "passed": true,  "output": "0"},
    {"name": "含零列表", "input": "[1,2,0,4]", "passed": false, "output": "ZeroDivisionError: division by zero"}
  ]
}
```

### 3.4 FixerAgent

- **输入**：`code_history[-1]` + `review_history[-1].issues`（仅取未解决的 high/medium，low 可选忽略）
- **输出**：新版 `CodeArtifact` + `fixed_issue_ids`
- **约束**：提示词要求**最小化修复**，禁止借机重构——这是为了防止修复引入新问题
- **验证三连**：① 自身沙盒执行通过 → ② 回归重跑 Reviewer 记录的**全部**用例（不只失败的那批）→ ③ 把 fixed_issue_ids 写进 `FixRecord`，供下一轮 Reviewer 复检并关闭 issue

> 「重跑全部用例」而非只跑失败用例，是为了拦截「修好 A 却破坏 B」。

### 3.5 双层闭环的职责边界（重点）

| 循环 | 驱动者 | 反馈来源 | 解决的问题 | 上限 |
|---|---|---|---|---|
| 内层 | Coder / Fixer 节点内部 | 运行时 stdout / traceback / 超时 | **跑不通**：语法错误、异常、死循环 | `MAX_REPAIR_ROUNDS` |
| 外层 | Reviewer → Fixer | `ReviewReport.issues` | **跑得通但不好**：边界、健壮性、规范、复杂度 | `MAX_REVIEW_ROUNDS` |

**为什么不让 Reviewer 兼任运行期自愈？** 两者的判断依据不同：运行期错误是确定信号（traceback），质量问题是主观判断（需要模型权衡）。混在一起会导致 prompt 职责模糊、终止条件耦合，且丢失"质量 gates"这一层独立的把关点的价值。分层后每层可独立调参、独立测试。

---

## 4. Agent 间通信

### 4.1 方式选型

| 方式 | 做法 | 结论 |
|---|---|---|
| 共享状态（Blackboard） | 唯一 `WorkflowState` 实例，节点只读所需分区 | ✅ **主用**：唯一真相源，无序列化歧义，天然一致 |
| 显式消息队列 | `MessageBus` 登记 `AgentMessage`（sender/receiver/type/payload） | ✅ **辅用**：满足「消息传递」，且天然生成可追溯 trace |
| 直接函数返回 | Orchestrator 拿着上一步结果直接喂下一步 | ⚠️ 仅作 MVP 兜底，会丢失数据契约 |

**结论：白板为主 + 消息日志为辅。**
白板解决「当前状态是什么」，消息日志解决「是怎么演变到这里的」。二者互补，且消息日志可直接充当「执行日志与状态追踪」（作业推荐实现项）的数据源。

**硬性约束：节点之间禁止直接调用。** 所有跨 Agent 交互必须经由 Engine 中转。

### 4.2 消息协议

```python
@dataclass
class AgentMessage:
    id: str              # msg-0007
    sender: str          # "reviewer"
    receiver: str        # "fixer" | "broadcast"
    msg_type: str        # CODE_ARTIFACT | REVIEW_REPORT | FIX_REQUEST | STATE_PATCH | ERROR
    round: int           # 所属迭代轮次
    timestamp: str
    correlation_id: str  # 同一次修复请求的因果链，便于串联 trace
    payload: dict        # 承载 CodeArtifact / ReviewReport 的 dict 形式
```

一次典型链路：

```
Coder    --(CODE_ARTIFACT, v1)-->   Engine ──写 code_history──> msg#1
Reviewer <─读 code_history[-1]──    Engine
Reviewer --(REVIEW_REPORT, FAIL)--> Engine ──写 review_history─> msg#2
Fixer    <─读 code_history[-1] + review_history[-1]── Engine
Fixer    --(CODE_ARTIFACT, v2)-->   Engine ──写 code_history──> msg#3
```

### 4.3 为什么必须用结构化消息

若 Agent 之间传递自然语言长文本（"我觉得第 12 行好像会有问题…"），会带来：下游需要二次 LLM 解析才能使用（额外成本与非确定性）、无法断言和测试、无法做 issue 生命周期追踪、token 浪费。结构化后这些全部变成普通字典操作，可直接写单元测试。

**代价与控制**：LLM 输出 JSON 偶发不规范 → 三级降级解析（见 §6 错误处理），且永远不让解析失败中断主流程。

---

## 5. 全局状态管理

### 5.1 结构定义

```python
@dataclass
class WorkflowState:
    task_id: str
    requirement: str
    language: str = "python"

    stage:  Stage      # CODING / REVIEWING / FIXING / DONE / FAILED
    status: Status     # RUNNING / PASSED / MAX_ROUNDS_EXCEEDED
                       #          / REVIEW_INCONCLUSIVE / NODE_ERROR
    round: int = 0

    code_history:   list[CodeArtifact]   # append-only 版本链 v1..vn
    review_history: list[ReviewReport]   # review_history[i] 审的是 code_history[i]
    fix_history:    list[FixRecord]      # 每次修复的输入输出，用于 diff 与审计

    messages: list[AgentMessage]         # append-only 消息日志
    metrics:  dict                       # llm_calls / sandbox_runs / elapsed
    errors:   list[str]                  # 节点级异常汇总
    notes:    list[str]                  # Engine → Agent 的跨轮次提示（单向只读）
```

### 5.2 设计原则

| 原则 | 做法 | 解决的问题 |
|---|---|---|
| 单一真相源 | 只有 `WorkflowState` 保存跨 Agent 信息 | 多份拷贝不一致 |
| 单一写者 | **只有 Engine** 提交状态变更；节点返回 `NodeResult` 表达建议 | 并发覆盖；MVP 无需加锁 |
| 追加式版本链 | `code_history` 只 append 不原地修改 | 天然支持 diff、回滚、复现 |
| 索引对齐 | `review_history[i]` ↔ `code_history[i]` ↔ `fix_history[i]` | 若干轮后仍可精确回答"第 2 轮的 I1 修没修" |
| 有界终止 | `MAX_REVIEW_ROUNDS` / `MAX_STEPS` 双上限 | 死循环与 API 成本失控 |
| 记忆分离 | 每 Agent 私有 `messages`（短记忆），`WorkflowState` 为共享长记忆 | 避免审查意见灌入 Coder 完整历史导致 token 爆炸 |
| 单一职责 | 状态结构只含数据，不含行为方法（除 `to_dict`/`from_dict`） | 序列化困难 |
| 跨轮次事实通道 | `notes` 列表：Engine 写入只有它掌握的事实，Reviewer/Fixer 读取 | Agent 各自是全新对话会「重复踩坑」（实测第 2 轮倒退，见 §8.3.1 问题 3） |

> **单一写者的意义**：节点是「纯函数式」的——读 state，返回建议。这让节点可以脱离引擎单独单测（给定 state → 断言 NodeResult），测试成本大幅下降。

> **为什么需要 `notes` 而不直接写进 `messages`**：三个 Agent 各有私有短记忆，
> Engine 做的事（确定性回归）不在任何 Agent 的上下文里。若只靠代码产物传递，
> 下一轮 Reviewer 会把「上一轮修复引入的倒退」当成新发现重讲一遍。
> `notes` 是 Engine → Agent 的**单向只读**通道，不破坏单一写者原则。

### 5.3 持久化与恢复

每次运行在 `runs/<task_id>/` 落盘：

```
runs/20261024-153012/
├─ state.json     # 完整 WorkflowState 快照（含 messages）
├─ trace.log      # 人类可读：时间 / sender → receiver / msg_type / 耗时
└─ v1.py v2.py …  # 各版本代码
```

因为 `WorkflowState` 全结构可 `to_dict()`，从 `state.json` 恢复即可从中断点继续——这是「错误恢复机制」（作业推荐实现项）的基础。

### 5.4 有界性与降级

三条终止闸门，任一触达即跳出循环：

1. `verdict == PASS` —— 正常收敛
2. `round >= MAX_REVIEW_ROUNDS` —— 降级终止，**必须有产物**（汇总各版本、历次报错、未解决 issue、人工建议）
3. 节点 `status == ERROR` 且重试耗尽 —— 隔离该步，输出已完成部分

---

## 6. 错误处理

分层处理矩阵，核心原则：**任何单点故障都不应让整条 Workflow 崩溃，且终止时必须留下可用产物。**

| 层级 | 异常 | 处理 | 是否重试 |
|---|---|---|---|
| LLM 网络 | 超时 / 连接失败 / 429 / 5xx | 指数退避重试（2s→4s→8s） | ✅ 最多 `max_retries=3` |
| LLM 鉴权 | 401 / 403 | 抛 `APIKeyError`，明确提示检查 `.env` | ❌ 重试无意义 |
| LLM 其它 4xx | 400 参数错误 | 直接失败并记录 | ❌ |
| 工具执行 | 代码报错 / 超时 / 内部异常 | 写入 `error_msg` 返回给 LLM（**绝不抛出打断循环**） | — |
| 输出解析 | Reviewer 未输出合法 JSON | 三级降级：`json.loads` → 正则抽取 ```json 块 → 标记 `parse_failed` 降级 | ❌ |
| 节点级 | 节点代码抛异常 | Engine 捕获 → `NodeResult(ERROR)` → 重试 `MAX_NODE_RETRY` 次 | ✅ 有限次 |
| 流程级 | 轮次耗尽 / 重试后仍失败 | 降级终止，输出汇聚式汇总 | — |

> 作业 1 已验证的经验保留：**工具内部任何异常都写进返回结构的 error 字段，而不是向上抛**——这条保证了 Agent 循环永不被工具拖死。

---

## 7. 流程控制与可扩展性

### 7.1 节点契约

```python
class BaseNode(ABC):
    name: str
    @abstractmethod
    def invoke(self, state: WorkflowState) -> NodeResult: ...

@dataclass
class NodeResult:
    status: str         # OK | ERROR | SKIPPED
    output: dict|None   # 交给状态白板的 payload
    error:  str|None
    metrics: dict       # llm_calls / sandbox_runs / elapsed
```

### 7.2 新增一个 Agent 的成本（可扩展性论据）

以新增 `DocAgent`（据最终代码生成使用文档）为例：

1. 在 `nodes/doc.py` 实现 `BaseNode.invoke()`；
2. 在 `core/registry.py` 的 `NODE_REGISTRY` 注册一行；
3. 在 `core/engine.py` 的流程配置序列末尾追加 `"doc"` 一步。

**Engine 零改动，其余节点零改动。** 这正是前面「分层依赖约束」与「注册表」换来的收益。

### 7.3 流程控制能力

| 能力 | MVP（必须） | 增强（推荐） |
|---|---|---|
| 顺序执行 | ✅ Coder → Reviewer → Fixer | — |
| 循环 | ✅ 审查-修复回边，`round` 控制 | 支持 per-issue 级的定向回边 |
| 条件分支 | △ 最小形式：PASS / FAIL 二选一 | ✅ 按 `severity` 路由（仅 low 直接 PASS；含 high 必进 Fixer） |
| 并行 | ❌ | ✅ Reviewer 多项检查并行、测试用例并行执行 |
| 持久化恢复 | ✅ state.json | ✅ 断点续跑 |
| 可视化 | ❌ | ✅ `docs/workflow.mmd` 渲染图 / 轻量 Web UI |

---

## 8. 测试方案

沿用作业 1 已验证的方法论：能离线的确定性测试尽量离线，真实 LLM 只做端到端冒烟。

### 8.1 沙盒单元测试（迁移自作业 1 §9.1）

| 编号 | 用例 | 预期 |
|---|---|---|
| T1 | 正常执行 | `success=True`，捕获 stdout |
| T2 | 除零异常 | `success=False`，`error_msg` 含完整 traceback |
| T3 | 索引越界 | 同上 |
| T4 | 超时保护 | 强制终止并返回明确超时提示 |
| T5 | 空代码 | 不执行，返回明确提示 |
| T6 | 中文输出 | UTF-8 无乱码 |

### 8.2 Workflow 确定性测试（mock LLM）

真实模型行为不稳定，边界分支难以稳定复现，故用假 LLM 固定返回做断言：

| # | 分支 | 断言 |
|---|---|---|
| 1 | 正常收敛 | 审查一次 PASS → `status == PASSED`，round = 0 |
| 2 | 修复后收敛 | 首轮 FAIL → Fixer 修好 → 复审 PASS，round = 1 |
| 3 | 轮次耗尽 | 始终 FAIL → 达到 `MAX_REVIEW_ROUNDS` 终止并降级汇总，产物非空 |
| 4 | 节点异常 | 某节点抛错 → 重试 `MAX_NODE_RETRY` → 仍失败则 `NODE_ERROR`，不崩溃 |
| 5 | 修复引入回归 | Fixer 修好 A 破坏 B → 回归用例拦截，流程正确处理 |

### 8.2.1 当前自测结果

```
python -m unittest discover -s tests -t .
Ran 15 tests in 9.8s —— OK
```

| 测试 | 断言要点 | 结果 |
|---|---|---|
| 分支 1 正常收敛 | `PASSED`，1 个版本，round=0 | ✅ |
| 分支 2 修复后收敛 | `PASSED`，2 个版本，`handled_issue_ids=["I1"]`，issue 标记 `fixed`，回归通过 | ✅ |
| 分支 3 轮次耗尽 | `MAX_ROUNDS_EXCEEDED`，3 次审查 + 2 次修复，终止时仍有产物 | ✅ |
| 分支 4 节点异常 | `NODE_ERROR`，节点被调用 `MAX_NODE_RETRY+1` 次，无 LLM 调用，errors 非空 | ✅ |
| 分支 5 修复引入回归 | 上一轮通过的用例被改坏 → `regression_passed=False` | ✅ |
| 分支 6 模型不调工具 | 答复无 tool_calls → 就地提醒后重来，不触发整节点重试 | ✅ |
| 分支 7 空代码调用 | `code` 为空不计入 `attempts`，不消耗自修复轮次 | ✅ |
| 分支 8 PASS 却含 high | 自相矛盾时以证据为准改判 FAIL，缺陷必须真被修一轮 | ✅ |
| 分支 9 PASS 含 medium | 不升级、不额外修复，但建议仍保留在报告里 | ✅ |

### 8.3 真实 LLM 端到端

| 用例 | 输入 | 验证点 |
|---|---|---|
| A | 除零列表倒数之和（作业 1 指定用例） | 内层自愈闭环 + 审查指出边界问题 |
| B | 多轮连续需求 | 上下文记忆与状态延续 |
| C | 明显违反规范的代码 | Reviewer 能稳定产出结构化 issues |

#### 8.3.1 实测记录（qwen-plus，已完成）

用例 A 的完整运行结果见 README §4。核心数据：

| 项 | 实测值 |
|---|---|
| 终态 | `MAX_ROUNDS_EXCEEDED`（有界终止，非崩溃） |
| 版本链 | v1(coder) → v2(fixer) → v3(fixer) |
| 评分轨迹 | 70 → 75 → 80（单调递增，说明修复确实在生效） |
| 回归拦截 | 第 2 轮抓到 1 次倒退（Fixer 为处理布尔值改变了既有行为） |
| 开销 | LLM 13 次、沙盒 5 次、381s（含 2 次 60s 网络读超时） |

这张表说明三件事：

1. **质量提升与"通过"是两回事**。LLM 审查没有天然终点，分数可以一直涨而 verdict 始终是 FAIL；
   这正是终止条件设计里"有界终止优先于无限逼近"的现实依据。
2. **跨 Agent 的事实必须由编排器兜底**。倒退是 Engine 自己做确定性回归发现的，
   三个 Agent 都不知道——Agent 只对本轮负责，全局一致性是 Engine 的职责。
3. **降级终止不等于失败**。达到轮次上限后依然交付了最终代码 + 审查报告 + 版本 diff + trace。

#### 8.3.2 实测驱动的三次修正

mock 测试能保证分支可达，但覆盖不到"模型不按套路出牌"。真实运行暴露了三个问题，
均已修复并补了回归用例：

| # | 现象 | 根因 | 修复位置 | 回归用例 |
|---|---|---|---|---|
| 1 | 每轮修复都先报 `fixer 执行失败，重试 1/1` | 模型未调工具就输出结论 → 节点判失败 → Engine 整轮重试，重复追加 user 消息，白白多一轮 API | `core/base_agent.py::_loop` 增加就地提醒（`MAX_TOOL_NUDGE`），优先纠正而非重试 | `test_06` |
| 2 | Fixer 反复陷入自修复 | 回归用例本身期望抛异常，脚本非零退出被误判为"执行失败" | `nodes/prompts.py::FIXER_PROMPT` 强制每条用例 try/except 并打印 | prompt 层，用例见 README §4.1 |
| 3 | 倒退信息只在终端一闪而过 | 下一轮 Agent 是全新对话，拿不到上轮事实 | 新增 `WorkflowState.notes` 跨轮次通道，Engine 写、Reviewer/Fixer 读 | 状态层 |

> 附带收获：第 1 条修正同时解决了"Reviewer 提交空代码调用"浪费自修复轮次的问题
> （空调用不计入 `attempts`，见 `test_07`）。

#### 8.3.3 第二轮实测：修正效果的量化

同一需求（`倒数之和 / [1,2,0,4]`）复跑：

| 指标 | 修正前 | 修正后 | 变化 |
|---|---|---|---|
| 终态 | `MAX_ROUNDS_EXCEEDED` | `PASSED` | 收敛 |
| 耗时 | 381.25s | 37.15s | -90% |
| LLM 调用 | 13 | 4 | -69% |
| 节点重试次数 | 3 | 0 | 消失 |
| 版本数 | 3 | 1 | 一次成型 |

#### 8.3.4 第二轮暴露的第 4 个问题：审查结论自相矛盾

实测日志：

```
verdict=PASS  score=95  issues=1
  ✗ I1 [medium][robustness] L4 遇到 None 会抛 TypeError
```

这是**静默失败**：缺陷已被定性并记录，却因为整体判 PASS，永远不会进入修复流程——
它既不算通过，也没被修。根因是 LLM 不严格遵守自己 prompt 里的判定规则。

处理规则（`nodes/reviewer.py::_normalize_contradiction`）：

| 情形 | 处置 | 理由 |
|---|---|---|
| PASS + 未解决 **high** | **以证据为准改判 FAIL**，进入 Fixer | 高危缺陷必须闭环，不能靠一句"可用"放过 |
| PASS + medium / low | 保持 PASS，作为改进建议保留在报告里 | 否则会退回"永远不通过"，见 `test_09` |

> 这个取舍值得在答辩时讲：升级与否的判据不是"要不要更严格"，
> 而是**哪一侧的代价更可控**——多修一轮的成本是几秒 API，
> 静默放行一个高危缺陷的成本是整个 Workflow 失去意义。

---

## 9. 局限性

1. **审查质量受模型能力上限约束**——已用「真实执行测试」和「可选静态检查」缓解，但无法根除漏审。
2. **JSON 输出偶发不规范**——三级降级保证不中断，但降级路径下审查维度可能不全。
3. **沙盒为作业级简易沙盒**：仅子进程隔离 + 超时强杀，无 Docker/seccomp 等系统级隔离，**不可用于执行不可信代码**。
4. **记忆分离带来信息损耗**：跨 Agent 只经 `WorkflowState` 传递，长链路的细节可能被摘要丢失。
5. **成本与轮次权衡**：`MAX_REVIEW_ROUNDS` 越小越省 API 调用，但收敛概率下降；默认 2 是作业场景下的折中。
6. 当前仅支持 Python；支持其它语言需替换沙盒执行器与审查规则。

---

## 10. 附录：与作业 1（D:\agent）的映射

| 作业 1 | 本项目 | 处置 |
|---|---|---|
| `llm_client.py` | `llm/llm_client.py` | 原样复用 |
| `tools.py` | `tools/sandbox.py` | 原样复用 |
| `agent.py` 的 ReAct 循环 | `core/base_agent.py::BaseLLMAgent` | 抽出为基类 |
| `agent.py` 的 `_build_failure_summary` | 基类保留 + Engine 层汇总增强 | 扩展 |
| `MAX_REPAIR_ROUNDS/MAX_STEPS/MAX_HISTORY` | `config.py` | 沿用并新增 `MAX_REVIEW_ROUNDS` 等 |
| Design.md §9 测试 T1~T6 | `tests/test_sandbox.py` | 迁移 |
| Design.md §9.3 mock 方法 | `tests/test_workflow_mock.py` | 扩展为 5 个分支 |
