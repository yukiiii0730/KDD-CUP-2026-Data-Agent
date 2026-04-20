# me2AI — KDD Cup 2026 DataAgent-Bench 优化架构说明

## 一、赛题背景与评分规则

竞赛要求自主 Agent 接收异构数据包（JSON、CSV、SQLite、文档），根据自然语言问题进行多步推理，产出最终表格答案。

**评分指标：二元列匹配准确率（Binary Column-Matching Accuracy）**

- 预测结果包含所有黄金答案列（顺序不要求）→ 得 1 分
- 缺少任何一列或值不匹配 → 得 0 分
- 允许有额外列，但不允许缺列

**关键推论：必须保证"列完整性"优先于其他任何考量。**

---

## 二、总体约束

| 约束 | 说明 |
|---|---|
| 工具 | 只能通过 `context/` 目录内的工具访问数据 |
| 模型 | OpenAI 兼容接口（DashScope Qwen 系列） |
| 步骤上限 | 按 difficulty 动态确定：easy=15, medium=20, hard=30, extreme=40 |
| 超时 | 每任务 1500 秒（含网络延迟）|
| 并发 | 最多 8 个 Worker（通过 max_workers 控制）|
| 参数集中 | 所有可调参数在 `configs/me2ai_optimized.yaml` 中统一管理 |

---

## 三、创新架构：分层三阶段 DataAgent（HierarchicalDataAgent）

```
┌──────────────────────────────────────────────────────────────┐
│            主线程：预串行规划（不受 task timeout 约束）         │
│                                                               │
│  plan_1 = Planner(task_1)  plan_2 = Planner(task_2) ...      │
│  ──────────────────────────────────────────── (~3 分钟)       │
└───────────────────────────┬──────────────────────────────────┘
                            │ pre_computed_plans[task_id]
                            ▼
┌──────────────────────────────────────────────────────────────┐
│            子进程（8并发，timeout 只计执行时间）               │
│                                                               │
│  ┌──────────────────────────────────────────────────────┐    │
│  │  PHASE 1 — PLANNER（已跳过，使用主线程预计算结果）    │    │
│  └──────────────────────────────────────────────────────┘    │
│                          │ (plan JSON)                        │
│                          ▼                                    │
│  ┌──────────────────────────────────────────────────────┐    │
│  │  PHASE 2 — EXECUTOR（增强型 ReAct 循环）              │    │
│  │                                                       │    │
│  │  · 系统提示嵌入 Planner 输出的执行计划                │    │
│  │  · 动态步骤上限（按 difficulty 调整）                 │    │
│  │  · 紧急提交：剩余 ≤2 步时强制调用 answer             │    │
│  │  · 增强工具集（含大文档工具）                         │    │
│  └──────────────────────────────────────────────────────┘    │
│                          │ (answer draft)                     │
│                          ▼                                    │
│  ┌──────────────────────────────────────────────────────┐    │
│  │  PHASE 3 — VERIFIER（单次 LLM 调用）                 │    │
│  │                                                       │    │
│  │  · 对比问题与答案，判断列是否完整                     │    │
│  │  · 若不完整：生成 feedback → 重回 Phase 2             │    │
│  └──────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────┘
```

**预串行规划的意义：** 8 并发子进程同时发 planner 请求时，API 限速会导致 planner 等待时间超过 task timeout（900s），executor 从未启动即超时。将 planner 移至主线程串行执行，timeout 完全留给 executor，将超时任务从 5 个降至 0 个（在 API 正常时）。

---

## 四、模型路由策略

```
难度        Planner              Executor             Verifier
──────────────────────────────────────────────────────────────
easy        qwen3-coder-flash    qwen3-coder-flash    qwen3.5-flash
medium      qwen3-coder-plus     qwen3-coder-plus     qwen3.5-flash
hard        qwen3-coder-plus     qwen3-coder-plus     qwen3.5-flash
extreme     qwen3-coder-plus     qwen3-max            qwen3.5-flash
```

**设计原理：**
- Planner 只需产出简单 JSON 计划，coder-plus 速度快（2-3s），qwen3-max 的 thinking 模式单次耗时 3-8 分钟
- Executor 是主力（占 ~80% 调用），coder 专用模型比通用模型更擅长 SQL/Python
- Verifier 只做文本比对，最便宜的 flash 完全够用
- 相比全量 qwen3-max：**节省约 70% API 费用**

---

## 五、增强工具集

| 工具名 | 类型 | 说明 |
|---|---|---|
| `list_context` | 原有 | 列出 context 目录结构（含文件大小）|
| `read_csv` | 原有 | CSV 预览（50行）|
| `read_json` | 原有 | JSON 预览 |
| `read_doc` | 原有 | 文本文档读取（≤20KB 文件用）|
| `inspect_sqlite_schema` | 原有 | SQLite schema 检查 |
| `execute_context_sql` | 原有 | SQLite 只读 SQL 执行 |
| `execute_python` | 原有 | Python 代码执行（含 pandas/duckdb）|
| `query_csv_duckdb` | **新增** | 直接对 CSV 文件执行 DuckDB SQL（含 JOIN/GROUP BY）|
| `search_in_doc` | **新增** | 关键词搜索大文档，返回匹配行+上下文（>20KB 文件首选）|
| `read_doc_page` | **新增** | 按字符偏移分页读取大文档 |
| `answer` | 原有 | 提交最终答案表格 |

**大文档使用策略（Prompt 规则）：**
- `list_context` 返回文件大小，agent 据此判断
- 文件 ≤20KB → `read_doc`
- 文件 >20KB → `search_in_doc`（按题目关键词精确查找，不读全文）

---

## 六、答案后处理

提交时自动执行：

| 规则 | 触发条件 | 处理 |
|---|---|---|
| 全名拆分 | 列名匹配 `^full.?name$` 且值为 "First Last" 格式 | 自动拆为 `first_name` / `last_name` |
| Timestamp 序列化 | DuckDB 返回日期类型 | 自动转为 ISO 格式字符串 |

---

## 七、关键 Prompt 规则

| 规则 | 解决的错误类型 |
|---|---|
| `"Which event?" → NAME not event_id` | 列选择错误 |
| `"What is the comment?" → TEXT not comment_id` | 返回 ID 而非文本 |
| `"Name the user" → DisplayName not user_id` | 返回整数 ID |
| `"How many times more A than B?" → ratio A/B` | COUNT vs 比值 |
| `"Average monthly X" → verify monthly vs annual` | 聚合单位错误 |
| `NEVER add WHERE filter 0/null unless asked` | 自行添加无效过滤 |
| `Include NULL rows with empty string` | 丢弃含空值行 |
| `COUNT atoms matching P in molecules Q → only P atoms` | 计数逻辑错误 |
| `first_name + last_name → return as separate columns` | 名字列合并 |

---

## 八、运行方式

```bash
# 全量跑分
PYTHONPATH=src .venv/bin/python -m data_agent_baseline run-benchmark \
  --config configs/me2ai_optimized.yaml

# 或使用便捷脚本（解决中文路径问题）
./run.sh run-benchmark --config configs/me2ai_optimized.yaml

# 单任务测试
./run.sh run-task --task-id task_22 --config configs/me2ai_optimized.yaml
```

---

## 九、模块文件结构

```
src/data_agent_baseline/
├── agents/
│   ├── model.py          # 【增强】OpenAI 适配器 + ModelRouter + 重试机制
│   ├── prompt.py         # 【增强】系统提示（列优先 + 工具规则 + 大文档规则）
│   ├── react.py          # 【增强】紧急提交机制（steps_remaining ≤2 时强制 answer）
│   ├── hierarchical.py   # 【新增】分层三阶段 Agent（支持外部传入 plan）
│   └── runtime.py        # 运行时状态
├── tools/
│   ├── registry.py       # 【增强】+query_csv_duckdb +search_in_doc +read_doc_page
│   │                     #         +Timestamp序列化修复 +full_name自动拆分
│   ├── filesystem.py     # 文件读取工具
│   ├── sqlite.py         # SQLite 工具
│   └── python_exec.py    # Python 执行
├── benchmark/
│   ├── dataset.py
│   └── schema.py
├── run/
│   ├── runner.py         # 【增强】预串行规划 + pre_computed_plan 传递
│   └── log_generator.py  # 【新增】跑分后自动生成 logs/run_<timestamp>.md
├── config.py             # 【增强】模型路由字段 + 步骤配置
└── cli.py                # 【增强】传递 config_path 和优化记录给日志
```

---

## 十、当前得分演进

| 迭代 | 得分 | 关键改动 |
|---|---|---|
| Iter 001 | 30/50 (60%) | 分层架构、DuckDB工具、难度自适应步数 |
| Iter 002-003 | 43/50 (86%) | Prompt精细化、auto-split、多模型路由 |
| Iter 004 | 43/50 (86%) | 大文档工具、紧急提交、日志系统 |
| **Iter 005** | **45/50 (90%)** | **预串行规划、max_steps_easy=15、重试机制** |

---

## 十一、图片/视频数据支持方案（前瞻规划）

> 以下为面向后续赛季新增图片/视频数据类型的架构扩展方案，暂未实现。

### 背景

若题目中的 context 包含图片（.jpg/.png）或视频（.mp4/.avi），当前纯文本工具链无法处理。需在不破坏现有架构的前提下，以最小侵入性方式扩展。

### 新增数据类型分析

| 类型 | 典型任务 | 需要能力 |
|---|---|---|
| 图片 | "图中有多少人？" / "该产品的品牌是什么？" | VLM 视觉理解、OCR、目标检测 |
| 视频 | "视频第几帧出现了X？" / "统计出现次数" | 关键帧提取、时序理解、多帧 VLM |

### 扩展方案

#### 1. 工具层扩展（最小侵入）

在 `tools/registry.py` 新增两个工具：

```python
# 图片工具：调用 VLM（如 qwen-vl-plus）分析图片
"analyze_image": {
    "path": "relative/path/to/image.jpg",
    "question": "问 VLM 的自然语言问题（如：图中有几个人？）"
}

# 视频工具：提取关键帧后逐帧分析
"analyze_video": {
    "path": "relative/path/to/video.mp4",
    "question": "对视频内容的问题",
    "max_frames": 16  # 均匀采样帧数，控制 token 消耗
}
```

实现逻辑：
- `analyze_image`：读取图片为 base64，调用 VLM API（messages 中插入 image_url 类型内容）
- `analyze_video`：用 `opencv-python` 或 `ffmpeg-python` 均匀采样 N 帧，每帧转 base64，批量或逐帧调用 VLM

#### 2. 模型路由扩展

在 `ModelRouter` 新增 `visual_model` 角色，用于图片/视频分析调用：

```yaml
# configs/me2ai_optimized.yaml 新增字段
model_visual: qwen-vl-max   # 视觉模型，单独路由
```

- 文本任务继续用现有路由（coder-plus / coder-flash）
- 视觉工具调用统一走 `model_visual`，避免影响 executor 路由

#### 3. Planner 感知扩展

`list_context_tree` 已返回文件扩展名，Planner prompt 新增规则：

```
- 若 context 包含 .jpg/.png/.gif → 优先使用 analyze_image 工具
- 若 context 包含 .mp4/.avi/.mov → 优先使用 analyze_video 工具
- 图片/视频内容无法用 SQL 查询，必须通过视觉工具提取信息后再聚合
```

#### 4. 步骤预算调整

视觉分析工具每次调用耗时约 5-15s，token 消耗大：
- 含图片的任务：步骤上限 +10（在当前基础上）
- 含视频的任务：步骤上限 +15，timeout 提升至 2400s

#### 5. 实现优先级

| 步骤 | 工作量 | 优先级 |
|---|---|---|
| 新增 `analyze_image` 工具（图片 base64 → VLM） | 低（~50行）| ⭐⭐⭐ 先做 |
| 扩展 `OpenAIModelAdapter` 支持多模态 messages | 中（~30行）| ⭐⭐⭐ 先做 |
| 新增 `analyze_video` 工具（关键帧采样）| 中（~80行，依赖 opencv）| ⭐⭐ 后做 |
| Planner prompt 感知图片/视频 | 低（prompt 修改）| ⭐⭐⭐ 先做 |
| 模型路由新增 visual_model 字段 | 低 | ⭐⭐ |

### 关键依赖

```
# 新增依赖
opencv-python>=4.8    # 视频关键帧提取
Pillow>=10.0          # 图片处理/格式转换
```

视觉模型推荐：`qwen-vl-max`（DashScope，支持 OpenAI 兼容 vision 格式）或 `qwen2.5-vl-72b-instruct`。
