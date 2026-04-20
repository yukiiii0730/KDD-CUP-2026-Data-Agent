"""
Auto-generate a timestamped Markdown run log after each benchmark.

Saved to: logs/run_<timestamp>.md
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from data_agent_baseline.run.runner import TaskRunArtifacts


def _count_tokens_in_trace(trace: dict[str, Any]) -> int:
    """Estimate token count by counting characters in all LLM messages (rough proxy)."""
    total = 0
    for step in trace.get("steps", []):
        raw = step.get("raw_response", "")
        total += len(raw) // 4  # ~4 chars per token
    return total


def _count_llm_calls_in_trace(trace: dict[str, Any]) -> int:
    return len(trace.get("steps", []))


def _load_trace(artifact: TaskRunArtifacts) -> dict[str, Any]:
    try:
        return json.loads(artifact.trace_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_previous_run(logs_dir: Path) -> dict[str, Any] | None:
    """Load the most recent run log to compare results."""
    existing = sorted(logs_dir.glob("run_*.json"))
    if not existing:
        return None
    try:
        return json.loads(existing[-1].read_text(encoding="utf-8"))
    except Exception:
        return None


def generate_run_log(
    *,
    run_id: str,
    run_output_dir: Path,
    task_artifacts: list[TaskRunArtifacts],
    logs_dir: Path,
    config_path: str = "",
    optimizations_this_run: list[str] | None = None,
) -> Path:
    """Generate a timestamped Markdown report and save supporting JSON."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    # ── Collect per-task metrics ──────────────────────────────────────────────
    total_tasks = len(task_artifacts)
    succeeded = [a for a in task_artifacts if a.succeeded]
    failed = [a for a in task_artifacts if not a.succeeded]
    accuracy = len(succeeded) / total_tasks if total_tasks else 0.0

    total_tokens = 0
    total_llm_calls = 0
    failed_details: list[dict[str, Any]] = []

    for artifact in task_artifacts:
        trace = _load_trace(artifact)
        tokens = _count_tokens_in_trace(trace)
        calls = _count_llm_calls_in_trace(trace)
        total_tokens += tokens
        total_llm_calls += calls
        if not artifact.succeeded:
            failed_details.append({
                "task_id": artifact.task_id,
                "failure_reason": artifact.failure_reason or "unknown",
                "llm_calls": calls,
                "estimated_tokens": tokens,
            })

    # ── Compare with previous run ─────────────────────────────────────────────
    prev_data = _load_previous_run(logs_dir)
    prev_accuracy = prev_data.get("accuracy") if prev_data else None
    delta_str = ""
    if prev_accuracy is not None:
        delta = accuracy - prev_accuracy
        sign = "+" if delta >= 0 else ""
        delta_str = f" ({sign}{delta:.1%} vs last run)"

    # ── Error categorization ──────────────────────────────────────────────────
    timeout_tasks = [d for d in failed_details if "timed out" in d["failure_reason"].lower() or "timeout" in d["failure_reason"].lower()]
    no_answer_tasks = [d for d in failed_details if "did not submit" in d["failure_reason"].lower() or "max_steps" in d["failure_reason"].lower()]
    other_failed = [d for d in failed_details if d not in timeout_tasks and d not in no_answer_tasks]

    # ── Build Markdown ────────────────────────────────────────────────────────
    opt_lines = "\n".join(f"- {o}" for o in (optimizations_this_run or ["(not specified)"]))

    error_table_rows = ""
    for d in failed_details:
        reason_short = d["failure_reason"][:120].replace("|", "/")
        error_table_rows += f"| {d['task_id']} | {reason_short} | {d['llm_calls']} | ~{d['estimated_tokens']:,} |\n"

    md = f"""# Benchmark Run: {run_id}

**Date**: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
**Config**: `{config_path}`
**Output dir**: `{run_output_dir}`

---

## 1. Accuracy

| Metric | Value |
|--------|-------|
| Total tasks | {total_tasks} |
| Correct | {len(succeeded)} |
| Failed | {len(failed)} |
| **Accuracy** | **{accuracy:.1%}{delta_str}** |

---

## 2. Resource Usage

| Metric | Value |
|--------|-------|
| Total LLM calls | {total_llm_calls} |
| Estimated tokens (chars÷4) | ~{total_tokens:,} |
| Avg calls / task | {total_llm_calls / total_tasks:.1f} |
| Avg tokens / task | ~{total_tokens // total_tasks if total_tasks else 0:,} |

---

## 3. Optimizations Applied This Run

{opt_lines}

---

## 4. Failed Tasks

| Task ID | Failure Reason | LLM Calls | Est. Tokens |
|---------|---------------|-----------|-------------|
{error_table_rows.strip()}

---

## 5. Error Analysis

- **Timeouts** ({len(timeout_tasks)} tasks): {', '.join(d['task_id'] for d in timeout_tasks) or 'none'}
- **No answer submitted** ({len(no_answer_tasks)} tasks): {', '.join(d['task_id'] for d in no_answer_tasks) or 'none'}
- **Other failures** ({len(other_failed)} tasks): {', '.join(d['task_id'] for d in other_failed) or 'none'}

---

## 6. Next Improvement Suggestions

- [ ] Analyze the {len(failed)} failed tasks' traces to identify common patterns
- [ ] For timeout tasks: increase step limits or improve tool efficiency
- [ ] For no-answer tasks: verify emergency submit is triggering correctly
- [ ] For column-mismatch tasks: refine the answer prompt or add post-processing rules
- [ ] Consider task-specific retries for high-value tasks that nearly succeeded
"""

    # ── Save files ────────────────────────────────────────────────────────────
    md_path = logs_dir / f"run_{timestamp}.md"
    md_path.write_text(md, encoding="utf-8")

    # Save JSON for comparison in next run
    json_path = logs_dir / f"run_{timestamp}.json"
    json_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "timestamp": timestamp,
                "accuracy": accuracy,
                "total_tasks": total_tasks,
                "succeeded": len(succeeded),
                "failed": len(failed),
                "total_llm_calls": total_llm_calls,
                "total_estimated_tokens": total_tokens,
                "failed_task_ids": [d["task_id"] for d in failed_details],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    return md_path
