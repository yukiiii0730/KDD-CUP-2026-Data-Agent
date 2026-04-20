<div align="center">

# DataAgent-Bench Starter Kit

English | [中文](README.zh.md)

[![Official Website](https://img.shields.io/badge/Official%20Website-Visit%20dataagent.top-0ea5e9?style=for-the-badge&logo=googlechrome&logoColor=white&labelColor=0f172a)](https://dataagent.top)
[![Demo Dataset](https://img.shields.io/badge/Demo%20Dataset-Download%20Phase%201-f59e0b?style=for-the-badge&logo=googledrive&logoColor=white&labelColor=0f172a)](https://drive.google.com/file/d/1n8vrRIjhVz0STj1DYZ7fSNL2JHtswu4J/view?usp=share_link)
[![Discord](https://img.shields.io/badge/Discord-Join%20Community-5865F2?style=for-the-badge&logo=discord&logoColor=white&labelColor=0f172a)](https://discord.gg/vRr7uyK9)

</div>

> Official starter kit for the KDD Cup 2026 DataAgent-Bench challenge. The repository reads tasks from `data/public/input/` and writes predictions for downstream evaluation.

## Overview

| Item | Value |
| --- | --- |
| Dataset input | `data/public/input/` |
| Public demo ground truth | `data/public/output/task_<id>/gold.csv` |
| Hidden test data | `input/` only, no `output/` |
| Entry command | `uv run dabench <command> --config PATH` |
| Default run output | `artifacts/runs/` |

## Quick Start

1. Install `uv` by following the official guide:
   - https://docs.astral.sh/uv/getting-started/installation/
2. On macOS and Linux, the standalone installer is:

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

3. Install project dependencies:

   ```bash
   uv sync
   ```

4. Configure API key via environment file:

  ```bash
  cp .env.example .env
  # edit .env and set DASHSCOPE_API_KEY=...
  ```

5. Confirm the dataset root is visible:

   ```bash
   uv run dabench status --config configs/react_baseline.example.yaml
   ```

6. Run the baseline:

   ```bash
   uv run dabench run-benchmark --config configs/react_baseline.example.yaml
   ```

## Dataset

The public demo dataset lives under `data/public/input/`. Each task directory follows this structure:

```text
data/public/input/task_<id>/
├── task.json
└── context/
```

The corresponding public demo answers live separately under `data/public/output/task_<id>/gold.csv`.
Hidden test sets only include `input/`, so there is no `output/` directory there.

`task.json` contains:

- `task_id`
- `difficulty`
- `question`

The `context/` directory may contain one or more of:

- CSV files
- JSON files
- SQLite / DB files
- Text documents

## Configuration

An example config file lives at `configs/react_baseline.example.yaml`. An optimized config is at `configs/me2ai_optimized.yaml`.

```yaml
dataset:
  root_path: data/public/input

agent:
  model: qwen3-coder-plus      # fallback model
  api_base: https://dashscope.aliyuncs.com/compatible-mode/v1
  api_key: ${DASHSCOPE_API_KEY}
  temperature: 0.0
  max_retries_per_step: 2
  mode: hierarchical           # "react" or "hierarchical"

  # Per-role per-difficulty model routing (hierarchical mode only)
  model_planner_easy:    qwen3-coder-flash
  model_planner_medium:  qwen3-coder-plus
  model_planner_hard:    qwen3-coder-plus
  model_planner_extreme: qwen3-coder-plus
  model_executor_easy:   qwen3-coder-flash
  model_executor_medium: qwen3-coder-plus
  model_executor_hard:   qwen3-coder-plus
  model_executor_extreme: qwen3-max
  model_verifier:        qwen3.5-flash

  # Difficulty-adaptive step limits
  max_steps_easy:    15
  max_steps_medium:  20
  max_steps_hard:    30
  max_steps_extreme: 40

run:
  output_dir: artifacts/runs
  max_workers: 8
  task_timeout_seconds: 1500
```

Config fields:

| Field | Meaning |
| --- | --- |
| `dataset.root_path` | Root directory of the public demo `input/` dataset. Relative paths are resolved from the project root. |
| `agent.model` | Fallback model name used when a specific role/difficulty is not configured. |
| `agent.mode` | Agent mode: `react` (single-layer) or `hierarchical` (Planner → Executor → Verifier). |
| `agent.api_base` | OpenAI-compatible API base URL. |
| `agent.api_key` | API key. Supports environment expansion such as `${DASHSCOPE_API_KEY}`. The project auto-loads `.env` from the repository root. |
| `agent.model_planner_*` | Planner model per difficulty (hierarchical mode). |
| `agent.model_executor_*` | Executor model per difficulty (hierarchical mode). |
| `agent.model_verifier` | Verifier model (hierarchical mode). |
| `agent.max_steps_*` | Per-difficulty step limits (hierarchical mode). |
| `agent.max_retries_per_step` | Maximum same-step retries when model output cannot be parsed or a tool call fails. |
| `agent.temperature` | Sampling temperature. |
| `run.output_dir` | Output directory for run artifacts. |
| `run.run_id` | Optional run directory name. Defaults to a UTC timestamp if omitted. Must be a single directory name; existing run directories are rejected. |
| `run.max_workers` | Parallel worker count for `run-benchmark`. |
| `run.task_timeout_seconds` | Maximum wall-clock time per task. Set to `0` or a negative value to disable the task-level timeout. |

## CLI

```bash
uv run dabench <command> --config PATH [options]
```

| Command | Purpose | Example |
| --- | --- | --- |
| `status` | Show project paths, config path, dataset root, and public task counts. | `uv run dabench status --config configs/react_baseline.example.yaml` |
| `inspect-task` | Show task metadata and list accessible files under `context/`. | `uv run dabench inspect-task task_1 --config configs/react_baseline.local.yaml` |
| `run-task` | Run the baseline on one task and write outputs. | `uv run dabench run-task task_1 --config configs/react_baseline.local.yaml` |
| `run-benchmark` | Run the baseline across the public dataset. | `uv run dabench run-benchmark --config configs/react_baseline.local.yaml` |

`run-benchmark` also supports `--limit N` to cap the number of tasks.

## Tools

The baseline exposes these tools to the model:

| Tool | Purpose | Inputs |
| --- | --- | --- |
| `list_context` | List files and directories under `context/` (includes file sizes). | `max_depth` |
| `read_csv` | Read a CSV preview. | `path`, `max_rows` |
| `read_json` | Read a JSON preview. | `path`, `max_chars` |
| `read_doc` | Read a text document preview (recommended for files ≤ 20 KB). | `path`, `max_chars` |
| `search_in_doc` | Keyword search inside a (potentially large) document; returns matching lines with context. Use for files > 20 KB instead of reading the whole file. | `path`, `query`, `context_lines`, `max_matches` |
| `read_doc_page` | Read a specific chunk of a large document by character offset. Use to paginate through files larger than 8 000 chars. | `path`, `start_char`, `max_chars` |
| `query_csv_duckdb` | Execute a DuckDB SQL query directly on CSV files (supports JOIN, GROUP BY, HAVING, ORDER BY). | `sql`, `limit` |
| `inspect_sqlite_schema` | Inspect tables in a SQLite / DB file. | `path` |
| `execute_context_sql` | Execute read-only SQL against a SQLite / DB file in `context/`. | `path`, `sql`, `limit` |
| `execute_python` | Execute arbitrary Python code inside the task `context/` directory. Libraries: pandas, polars, duckdb, numpy. | `code` |
| `answer` | Submit the final answer table and terminate the task. | `columns`, `rows` |

All file paths passed to tools must be relative to the task `context/` directory.

## Outputs

Each successful task run may produce:

- `trace.json`
- `prediction.csv`

Per-task outputs are written to:

```text
artifacts/runs/<run_id>/<task_id>/
├── trace.json
└── prediction.csv
```

Benchmark runs also write:

```text
artifacts/runs/<run_id>/summary.json
```

Additionally, run logs are recorded for analysis:

- `artifacts/runs/<run_id>/task_runs.jsonl` (one record per task)
- `artifacts/runs/run_history.jsonl` (one record per benchmark run)

## Contact

- Open issues: https://github.com/HKUSTDial/kddcup2026-data-agents-starter-kit/issues
- Official website: https://dataagent.top
- Discord: https://discord.gg/vRr7uyK9
- WeChat official account: `数据智能与分析实验室 DIAL`

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
        Official Website
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
        WeChat Official Account
      </td>
    </tr>
  </table>
</div>

## Main Modules

| Module | Responsibility |
| --- | --- |
| `src/data_agent_baseline/benchmark/dataset.py` | Public dataset loader |
| `src/data_agent_baseline/tools/filesystem.py` | `list_context`, `read_csv`, `read_json`, `read_doc` |
| `src/data_agent_baseline/tools/python_exec.py` | `execute_python` |
| `src/data_agent_baseline/tools/sqlite.py` | `inspect_sqlite_schema`, `execute_context_sql` |
| `src/data_agent_baseline/tools/registry.py` | Tool registration, `query_csv_duckdb`, `search_in_doc`, `read_doc_page`, answer post-processing |
| `src/data_agent_baseline/agents/model.py` | `OpenAIModelAdapter` (with retry), `ModelRouter` |
| `src/data_agent_baseline/agents/prompt.py` | System prompt with column-priority and large-doc rules |
| `src/data_agent_baseline/agents/react.py` | ReAct runtime with emergency-submit mechanism |
| `src/data_agent_baseline/agents/hierarchical.py` | Three-phase hierarchical agent (Planner → Executor → Verifier) |
| `src/data_agent_baseline/run/runner.py` | Benchmark execution with pre-serial planning phase |
| `src/data_agent_baseline/run/log_generator.py` | Auto-generates timestamped Markdown run reports in `logs/` |
