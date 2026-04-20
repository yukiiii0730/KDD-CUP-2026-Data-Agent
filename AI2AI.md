# AI2AI — 优化迭代记录

本文件记录每次架构优化的背景、变更内容和实际效果，供后续迭代参考。

---

## Iteration 001 — 2026-04-01  初始优化

### 背景
- 原 baseline 为单层 ReAct Agent，固定 16 步，无规划无验证
- 评分机制为二元列匹配：缺列直接 0 分
- 50 个公开任务（15 easy / 23 medium / 10 hard / 2 extreme）

### 问题分析

| 问题 | 影响 |
|---|---|
| 无规划阶段，Agent 经常浪费步骤在探索上 | 步骤耗尽但未找到正确数据 |
| 无验证阶段，提交答案可能缺少列 | 直接 0 分 |
| 所有难度固定 16 步 | Easy 浪费，Extreme 不足 |
| CSV 只有 preview，无法全量查询 | 数据截断导致结果不完整 |
| 系统提示未强调列完整性 | 模型不知道这是评分关键 |

### 变更内容

#### 新增文件
- `me2AI.md` — 整体架构与约束文档
- `AI2AI.md` — 迭代记录（本文件）
- `configs/me2ai_optimized.yaml` — 统一参数配置
- `src/data_agent_baseline/agents/hierarchical.py` — 分层三阶段 Agent
- `run.sh` — 解决 PYTHONPATH UTF-8 路径问题的启动脚本
- `src/data_agent_baseline/__main__.py` — 支持 `python -m` 调用

#### 修改文件
- `src/data_agent_baseline/agents/prompt.py` — 增强系统提示（列优先、工具使用范式）
- `src/data_agent_baseline/config.py` — 新增 hierarchical 模式配置字段
- `src/data_agent_baseline/tools/registry.py` — 新增 `query_csv_duckdb` 工具
- `src/data_agent_baseline/run/runner.py` — 支持 hierarchical agent 模式

### 核心创新点

1. **分层三阶段架构**（Planner → Executor → Verifier）
2. **难度自适应步骤上限**（easy:10 / medium:20 / hard:30 / extreme:40）
3. **query_csv_duckdb 工具**（含 JOIN / GROUP BY / HAVING）
4. **列优先提示**（Executor 接收 Planner 输出的 expected_columns）

### 实际效果（Run ID: 20260401T073652Z）
- **总分：30/50（60%）**

---

## Iteration 002 — 2026-04-01  Prompt 精细化 + 自动后处理

### 背景
基于 Iteration 001 跑分（60%）对 20 个失败任务做 trace 分析，识别出 6 类高频错误。

### 变更内容

#### `src/data_agent_baseline/agents/prompt.py`
新增以下规则：

| 规则 | 解决问题 |
|---|---|
| `"Which event?" → return NAME not ID` | 列选择错误 |
| `"What is the comment?" → return TEXT column` | 返回 ID 而非文本 |
| `"Name the user" → return DisplayName not user_id` | 返回整数 ID |
| `"How many times more A than B?" → ratio A/B` | 返回 COUNT 而非比值 |
| `"Average monthly X" → verify monthly vs annual` | 聚合单位错误 |
| `NEVER add WHERE to filter 0/null unless asked` | 自行添加无效过滤 |
| `Include NULL rows with empty string` | 丢弃含空值行 |

#### `src/data_agent_baseline/tools/registry.py`
新增 `_split_name_columns_if_applicable()` 后处理器：
- 在 `answer` 工具提交前自动检测 `full_name` 类列
- 值为 "First Last" 双词格式 → 自动拆为 `first_name` / `last_name`
- 列名匹配正则 `^full.?name$`，不依赖上下文数据源类型

---

## Iteration 003 — 2026-04-01  多模型路由（成本优化 + 精度提升）

### 背景
- 每次完整 benchmark 约调用 **492 次 API**，全部使用 qwen3-max 费用极高
- 核心发现：80% 的调用在 Executor（写 SQL/Python），coder 专用模型更合适

### 最终路由策略

```
难度        Planner              Executor             Verifier
──────────────────────────────────────────────────────────────
easy        qwen3-coder-flash    qwen3-coder-flash    qwen3.5-flash
medium      qwen3-coder-plus     qwen3-coder-plus     qwen3.5-flash
hard        qwen3-coder-plus     qwen3-coder-plus     qwen3.5-flash
extreme     qwen3-coder-plus     qwen3-max            qwen3.5-flash
```

（注：hard planner 原为 qwen3-max，后因 thinking 模式耗时过长改为 coder-plus，见 Iteration 004）

### 变更内容
- 新增 `ModelRouter` 类（`agents/model.py`）：按角色+难度路由
- 新增 `build_model_router()`（`run/runner.py`）：从 YAML 配置构建，自动为小模型添加 `enable_thinking=False`
- `OpenAIModelAdapter` 新增 `extra_body` 参数
- `HierarchicalDataAgent` 接受 `ModelRouter`
- `AgentConfig` 新增 9 个模型路由字段

### 费用节省（估算）

| 难度 | 全 qwen3-max | 新方案 | 节省 |
|---|---|---|---|
| easy（15任务）| 13500 | 1005 | 93% |
| medium（23任务）| 23000 | 7314 | 68% |
| hard（10任务）| 12000 | 4530 | 62% |
| **合计** | **51500** | **~15000** | **≈70%** |

---

## Iteration 004 — 2026-04-02  大文档工具 + 紧急提交 + 日志系统

### 背景
- 上次跑分（86%）失败任务分析：5 个 hard/extreme 超时（0步），1 个 easy 步数耗尽，1 个网络错误

### 变更内容

#### 1. 大文档工具（`tools/registry.py`）
新增两个工具解决 hard/extreme 任务的大文档问题（50KB-180KB MD 文件）：
- **`search_in_doc`**：关键词搜索文档，返回匹配行及上下文。避免读整个文件，省步骤
- **`read_doc_page`**：按字符偏移分页读取。接续 read_doc 的 `total_chars` 信息继续读

Executor prompt 新增规则：文件 size > 20000 bytes 时禁止 read_doc，改用 search_in_doc。

#### 2. 紧急提交机制（`agents/react.py`）
当 `steps_remaining <= 2` 时，在消息中注入 `⚠️ URGENT` 警告，强制 agent 调用 `answer` 而非继续工具调用。避免答案就在手边但步数耗尽得 0 分。

#### 3. DuckDB Timestamp 序列化修复（`tools/registry.py`）
`_safe()` 函数新增 `datetime.date`、`datetime.datetime`、`pandas.Timestamp` 转 `.isoformat()`，修复 task_86 的 `TypeError: Object of type Timestamp is not JSON serializable`。

#### 4. API 重试机制（`agents/model.py`）
`OpenAIModelAdapter.complete()` 对以下异常重试最多 5 次，指数退避（最大 30s）：
- `APIConnectionError`、`APITimeoutError`：网络级错误
- `APIStatusError` (5xx)：服务端错误

4xx 错误（权限/参数问题）仍立即抛出不重试。

#### 5. 日志系统（`run/log_generator.py`）
每次 benchmark 结束后自动在 `logs/` 目录生成时间戳 MD 报告，包含：
- 准确率（含与上次对比 delta）
- LLM 调用次数、估算 token 消耗
- 所有失败任务表格（含失败原因、调用次数）
- 错误分类（超时/无答案/其他）
- 下次改进方向建议

同时保存同名 JSON 供下次运行对比。

### 实际效果（Run ID: 20260402T053308Z）
- **总分：43/50（86%）**，相比上次 +26%
- 失败：5 个超时（planner qwen3-max thinking 太慢）、1 个步数耗尽、1 个网络错误

---

## Iteration 005 — 2026-04-02  预串行规划 + 并发超时根治

### 背景
分析 Iteration 004 的 5 个超时任务（task_344/352/355/396/418）：
- trace 全为 0 步，精确 900s 超时
- 8 并发子进程同时发 planner API 请求 → API 限速 → planner 等待耗尽 900s → executor 从未启动
- 验证：将 hard planner 从 qwen3-max 改为 coder-plus 后，task_408 在 61.5s 内成功（26步）

另发现：task_27/task_86 的失败原因是 `max_steps_easy=10` 太少，第 10 步刚拿到数据，没有第 11 步来提交。

### 变更内容

#### 1. 预串行规划（`run/runner.py`）
核心架构改动：在 `run_benchmark()` 的 **主线程**（不受 task timeout 约束）串行完成所有 50 个 planner 调用，再把 plan 传入子进程。

```
改动前：
  子进程1: [planner等待API 500s] → timeout（executor从未运行）

改动后：
  主线程: plan_1...plan_50（串行，~3min）
  子进程1-8: [executor并行]（timeout只计执行时间，利用率100%）
```

- `HierarchicalDataAgent.run()` 新增 `plan` 可选参数，接收外部传入计划
- `_run_single_task_core()`、`_run_single_task_with_timeout()`、`run_single_task()` 全部传递 `pre_computed_plan`

#### 2. 步数调整
- `max_steps_easy`: 10 → **15**（easy 任务有时需要多次调试才能拿到数据，需留余量提交）

#### 3. Hard Planner 改为 coder-plus
- `model_planner_hard`: qwen3-max → **qwen3-coder-plus**
- qwen3-max 默认开 thinking 模式，planner 单次调用 3-8 分钟。Planner 只产简单 JSON 计划，coder-plus 足够且快

#### 4. Task Timeout 提升
- `task_timeout_seconds`: 900 → **1500**
- 单独运行时这 4 个任务需 80-240s，并发下 API 响应更慢，需要更大缓冲

### 实际效果（Run ID: 20260402T133846Z）
- **总分：45/50（90%）**，相比上次 +4%
- 新修复：task_27 ✓、task_86 ✓、task_408 ✓、task_250 ✓（重试）、task_355 ✓
- 仍失败（5个）：task_344/352/396/418 在全量 8 并发下 API 限速导致 1500s 内仍超时
  - 单独串行运行时这 4 个全部 100% 成功（81-240s）
  - 根本矛盾：大文档 hard/extreme 任务需 20-26 步，全量并发下 API 响应时间倍增

### 当前得分演进

| 迭代 | 得分 | 变化 |
|---|---|---|
| Iter 001（初版） | 30/50 (60%) | 基准 |
| Iter 002-003（Prompt + 多模型路由） | 43/50 (86%) | +26% |
| Iter 004（大文档工具 + 紧急提交） | 43/50 (86%) | +0%（修复 2，引入 2 回归） |
| **Iter 005（预串行规划 + 步数调整）** | **45/50 (90%)** | **+4%** |

---

## 仍失败的任务（待正式开榜后优化）

| 任务 | 失败模式 | 根因 | 建议 |
|---|---|---|---|
| task_344 | 全量并发 1500s 超时 | 28KB CSV + 55KB Patient.md，约 26 步 | 降低并发数或单独补跑 |
| task_352 | 全量并发 1500s 超时 | 62KB budget.md，约 13 步 | 同上 |
| task_396 | 全量并发 1500s 超时 | 178KB superhero.md，约 23 步 | 同上 |
| task_418 | 全量并发 1500s 超时 | 280KB + 84KB 双大文档，约 9 步 | 同上 |
