<div align="center">

# DataAgent-Bench Starter Kit

[English](README.md) | 中文

[![官方网站](https://img.shields.io/badge/Official%20Website-Visit%20dataagent.top-0ea5e9?style=for-the-badge&logo=googlechrome&logoColor=white&labelColor=0f172a)](https://dataagent.top)
[![Demo 数据集](https://img.shields.io/badge/Demo%20Dataset-Download%20Phase%201-f59e0b?style=for-the-badge&logo=googledrive&logoColor=white&labelColor=0f172a)](https://drive.google.com/file/d/1n8vrRIjhVz0STj1DYZ7fSNL2JHtswu4J/view?usp=share_link)
[![Discord](https://img.shields.io/badge/Discord-Join%20Community-5865F2?style=for-the-badge&logo=discord&logoColor=white&labelColor=0f172a)](https://discord.gg/vRr7uyK9)

</div>

> 面向 KDD Cup 2026 DataAgent-Bench 挑战的官方 starter kit。仓库默认读取 `data/public/input/`，并为后续评测生成预测结果。

## Overview

| 项目 | 内容 |
| --- | --- |
| 数据输入 | `data/public/input/` |
| 公开 demo 标准答案 | `data/public/output/task_<id>/gold.csv` |
| hidden test 数据 | 仅提供 `input/`，不提供 `output/` |
| 入口命令 | `uv run dabench <command> --config PATH` |
| 默认输出目录 | `artifacts/runs/` |

## 快速开始

1. 请先按照 `uv` 官方安装指南安装 `uv`：
   - https://docs.astral.sh/uv/getting-started/installation/
2. 在 macOS 和 Linux 上，官方独立安装命令为：

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

3. 安装项目依赖：

   ```bash
   uv sync
   ```

4. 通过环境文件配置 API key：

  ```bash
  cp .env.example .env
  # 编辑 .env，设置 DASHSCOPE_API_KEY=...
  ```

5. 检查数据集根目录是否可见：

   ```bash
   uv run dabench status --config configs/react_baseline.example.yaml
   ```

6. 运行 baseline：

   ```bash
   uv run dabench run-benchmark --config configs/react_baseline.example.yaml
   ```

## 数据集

公开 demo 数据集默认位于 `data/public/input/`。每个任务目录结构如下：

```text
data/public/input/task_<id>/
├── task.json
└── context/
```

公开 demo 的标准答案文件单独放在 `data/public/output/task_<id>/gold.csv`。
hidden test set 只提供 `input/`，不会包含 `output/`。

`task.json` 包含：

- `task_id`
- `difficulty`
- `question`

`context/` 中可能包含一种或多种数据：

- CSV 文件
- JSON 文件
- SQLite / DB 文件
- 文本文档

## 配置

示例配置文件位于 `configs/react_baseline.example.yaml`，优化配置位于 `configs/me2ai_optimized.yaml`。

```yaml
dataset:
  root_path: data/public/input

agent:
  model: qwen3-coder-plus      # 兜底模型
  api_base: https://dashscope.aliyuncs.com/compatible-mode/v1
  api_key: ${DASHSCOPE_API_KEY}
  temperature: 0.0
  max_retries_per_step: 2
  mode: hierarchical           # "react" 或 "hierarchical"

  # 按角色+难度的模型路由（hierarchical 模式）
  model_planner_easy:    qwen3-coder-flash
  model_planner_medium:  qwen3-coder-plus
  model_planner_hard:    qwen3-coder-plus
  model_planner_extreme: qwen3-coder-plus
  model_executor_easy:   qwen3-coder-flash
  model_executor_medium: qwen3-coder-plus
  model_executor_hard:   qwen3-coder-plus
  model_executor_extreme: qwen3-max
  model_verifier:        qwen3.5-flash

  # 难度自适应步骤上限
  max_steps_easy:    15
  max_steps_medium:  20
  max_steps_hard:    30
  max_steps_extreme: 40

run:
  output_dir: artifacts/runs
  max_workers: 8
  task_timeout_seconds: 1500
```

配置字段说明：

| 字段 | 含义 |
| --- | --- |
| `dataset.root_path` | 公开 demo `input/` 数据集根目录。相对路径按项目根目录解析。 |
| `agent.model` | 兜底模型名称，当特定角色/难度未配置时使用。 |
| `agent.mode` | Agent 模式：`react`（单层）或 `hierarchical`（Planner → Executor → Verifier）。 |
| `agent.api_base` | OpenAI-compatible 接口根地址。 |
| `agent.api_key` | API key。支持环境变量展开，如 `${DASHSCOPE_API_KEY}`。项目会自动读取仓库根目录 `.env`。 |
| `agent.model_planner_*` | 各难度的 Planner 模型（hierarchical 模式）。 |
| `agent.model_executor_*` | 各难度的 Executor 模型（hierarchical 模式）。 |
| `agent.model_verifier` | Verifier 模型（hierarchical 模式）。 |
| `agent.max_steps_*` | 各难度的步骤上限（hierarchical 模式）。 |
| `agent.max_retries_per_step` | 当模型输出无法解析或工具调用失败时，当前步骤允许的重试次数。 |
| `agent.temperature` | 模型采样温度。 |
| `run.output_dir` | 运行产物输出目录。 |
| `run.run_id` | 可选，指定运行目录名。不传时默认使用 UTC 时间戳；必须是单个目录名，已存在会报错。 |
| `run.max_workers` | `run-benchmark` 并行 worker 数。 |
| `run.task_timeout_seconds` | 单个任务允许的最长墙钟时间。设为 `0` 或负数可关闭任务级超时。 |

## CLI

```bash
uv run dabench <command> --config PATH [options]
```

| 命令 | 作用 | 示例 |
| --- | --- | --- |
| `status` | 查看项目路径、配置路径、数据集根目录和公开任务数量。 | `uv run dabench status --config configs/react_baseline.example.yaml` |
| `inspect-task` | 查看任务元信息，并列出 `context/` 下可访问文件。 | `uv run dabench inspect-task task_1 --config configs/react_baseline.local.yaml` |
| `run-task` | 对单个任务运行 baseline，并写出结果。 | `uv run dabench run-task task_1 --config configs/react_baseline.local.yaml` |
| `run-benchmark` | 批量运行整个公开数据集。 | `uv run dabench run-benchmark --config configs/react_baseline.local.yaml` |

`run-benchmark` 还支持 `--limit N`，用于限制任务数量。

## Tools

当前暴露给模型的工具有：

| 工具 | 作用 | 输入 |
| --- | --- | --- |
| `list_context` | 列出 `context/` 下的文件和目录（含文件大小）。 | `max_depth` |
| `read_csv` | 读取 CSV 预览。 | `path`、`max_rows` |
| `read_json` | 读取 JSON 预览。 | `path`、`max_chars` |
| `read_doc` | 读取文本文档预览（推荐用于 ≤20KB 的文件）。 | `path`、`max_chars` |
| `search_in_doc` | 在（可能很大的）文档中搜索关键词，返回匹配行及上下文。适用于 >20KB 的文件。 | `path`、`query`、`context_lines`、`max_matches` |
| `read_doc_page` | 按字符偏移分页读取大文档，适用于超过 8000 字符的文件。 | `path`、`start_char`、`max_chars` |
| `query_csv_duckdb` | 直接对 CSV 文件执行 DuckDB SQL（支持 JOIN、GROUP BY、HAVING、ORDER BY）。 | `sql`、`limit` |
| `inspect_sqlite_schema` | 查看 SQLite / DB 文件中的表结构。 | `path` |
| `execute_context_sql` | 对 `context/` 内 SQLite / DB 文件执行只读 SQL。 | `path`、`sql`、`limit` |
| `execute_python` | 在任务 `context/` 目录内执行任意 Python 代码。可用库：pandas、polars、duckdb、numpy。 | `code` |
| `answer` | 提交最终答案表格并结束当前任务。 | `columns`、`rows` |

所有文件路径都必须是相对于任务 `context/` 目录的相对路径。

## 输出

每个任务运行后可能生成：

- `trace.json`
- `prediction.csv`

单任务产物路径：

```text
artifacts/runs/<run_id>/<task_id>/
├── trace.json
└── prediction.csv
```

批量运行还会额外生成：

```text
artifacts/runs/<run_id>/summary.json
```

另外会写入便于 badcase 分析的执行记录：

- `artifacts/runs/<run_id>/task_runs.jsonl`（每个任务一条记录）
- `artifacts/runs/run_history.jsonl`（每次 benchmark 一条记录）

## Contact

- 问题反馈： https://github.com/HKUSTDial/kddcup2026-data-agents-starter-kit/issues
- 官方网站： https://dataagent.top
- Discord： https://discord.gg/vRr7uyK9
- 微信公众号：`数据智能与分析实验室 DIAL`

<div align="center">
  <table>
    <tr>
      <td align="center">
        <a href="https://dataagent.top">
          <img
            src="https://api.qrserver.com/v1/create-qr-code/?size=144x144&data=https://dataagent.top&bgcolor=ffffff&color=111827&margin=8"
            alt="Official website QR code"
            width="144"
          />
        </a>
        <br />
        官方网站
      </td>
      <td align="center">
        <a href="https://discord.gg/vRr7uyK9">
          <img
            src="https://api.qrserver.com/v1/create-qr-code/?size=144x144&data=https://discord.gg/vRr7uyK9&bgcolor=ffffff&color=111827&margin=8"
            alt="Discord QR code"
            width="144"
          />
        </a>
        <br />
        Discord
      </td>
      <td align="center">
        <img
          src="assets/HKUSTGZ_DIAL.jpg"
          alt="WeChat official account QR code"
          width="144"
        />
        <br />
        微信公众号
      </td>
    </tr>
  </table>
</div>

## 主要模块

| 模块 | 责任 |
| --- | --- |
| `src/data_agent_baseline/benchmark/dataset.py` | 公开数据集加载器 |
| `src/data_agent_baseline/tools/filesystem.py` | `list_context`、`read_csv`、`read_json`、`read_doc` |
| `src/data_agent_baseline/tools/python_exec.py` | `execute_python` |
| `src/data_agent_baseline/tools/sqlite.py` | `inspect_sqlite_schema`、`execute_context_sql` |
| `src/data_agent_baseline/tools/registry.py` | 工具注册、`query_csv_duckdb`、`search_in_doc`、`read_doc_page`、答案后处理 |
| `src/data_agent_baseline/agents/model.py` | `OpenAIModelAdapter`（含重试）、`ModelRouter` |
| `src/data_agent_baseline/agents/prompt.py` | 含列优先和大文档规则的系统提示 |
| `src/data_agent_baseline/agents/react.py` | 含紧急提交机制的 ReAct runtime |
| `src/data_agent_baseline/agents/hierarchical.py` | 三阶段分层 Agent（Planner → Executor → Verifier）|
| `src/data_agent_baseline/run/runner.py` | 含预串行规划阶段的批量运行逻辑 |
| `src/data_agent_baseline/run/log_generator.py` | 每次 benchmark 后自动在 `logs/` 生成 Markdown 报告 |
