from __future__ import annotations

import csv
import json
import multiprocessing
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

from data_agent_baseline.agents.hierarchical import HierarchicalAgentConfig, HierarchicalDataAgent
from data_agent_baseline.agents.model import ModelRouter, OpenAIModelAdapter
from data_agent_baseline.agents.react import ReActAgent, ReActAgentConfig
from data_agent_baseline.benchmark.dataset import DABenchPublicDataset
from data_agent_baseline.config import AppConfig
from data_agent_baseline.tools.registry import ToolRegistry, create_default_tool_registry


@dataclass(frozen=True, slots=True)
class TaskRunArtifacts:
    task_id: str
    task_output_dir: Path
    prediction_csv_path: Path | None
    trace_path: Path
    succeeded: bool
    failure_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_output_dir": str(self.task_output_dir),
            "prediction_csv_path": str(self.prediction_csv_path) if self.prediction_csv_path else None,
            "trace_path": str(self.trace_path),
            "succeeded": self.succeeded,
            "failure_reason": self.failure_reason,
        }


def create_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def resolve_run_id(run_id: str | None = None) -> str:
    if run_id is None:
        return create_run_id()

    normalized = run_id.strip()
    if not normalized:
        raise ValueError("run_id must not be empty.")
    if normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
        raise ValueError("run_id must be a single directory name, not a path.")
    return normalized


def create_run_output_dir(output_root: Path, *, run_id: str | None = None) -> tuple[str, Path]:
    effective_run_id = resolve_run_id(run_id)
    run_output_dir = output_root / effective_run_id
    run_output_dir.mkdir(parents=True, exist_ok=False)
    return effective_run_id, run_output_dir


def build_model_adapter(config: AppConfig) -> OpenAIModelAdapter:
    """Build the default (fallback) model adapter from config."""
    return OpenAIModelAdapter(
        model=config.agent.model,
        api_base=config.agent.api_base,
        api_key=config.agent.api_key,
        temperature=config.agent.temperature,
    )


def build_model_router(config: AppConfig) -> ModelRouter:
    """
    Build a ModelRouter that routes calls to different models based on
    agent role (planner / executor / verifier) and task difficulty.

    Falls back to config.agent.model if a specific role/difficulty is not configured.
    """
    default_adapter = build_model_adapter(config)
    a = config.agent  # shortcut

    # qwen3 non-coder small models (8b/14b/32b/30b-a3b/235b) require enable_thinking=False
    # Coder models (qwen3-coder-*) and qwen3-max do NOT need this flag
    import re as _re
    _THINKING_REQUIRED = _re.compile(
        r"qwen3-(8b|14b|32b|30b|235b|next|3\.5)",
        _re.IGNORECASE,
    )

    def _adapter(model_name: str) -> OpenAIModelAdapter:
        """Create an adapter; auto-adds enable_thinking=False for qwen3 non-coder models."""
        name = model_name.strip()
        if not name or name == a.model:
            return default_adapter
        extra_body = {"enable_thinking": False} if _THINKING_REQUIRED.search(name) else {}
        return OpenAIModelAdapter(
            model=name,
            api_base=a.api_base,
            api_key=a.api_key,
            temperature=a.temperature,
            extra_body=extra_body,
        )

    planner_models = {
        "easy":    _adapter(a.model_planner_easy),
        "medium":  _adapter(a.model_planner_medium),
        "hard":    _adapter(a.model_planner_hard),
        "extreme": _adapter(a.model_planner_extreme),
    }
    executor_models = {
        "easy":    _adapter(a.model_executor_easy),
        "medium":  _adapter(a.model_executor_medium),
        "hard":    _adapter(a.model_executor_hard),
        "extreme": _adapter(a.model_executor_extreme),
    }
    verifier_model = _adapter(a.model_verifier)

    return ModelRouter(
        default_model=default_adapter,
        planner_models=planner_models,
        executor_models=executor_models,
        verifier_model=verifier_model,
    )


def build_agent(config: AppConfig, model=None, tools: ToolRegistry | None = None):
    """Build either a HierarchicalDataAgent or a plain ReActAgent based on config.agent.mode."""
    effective_tools = tools or create_default_tool_registry()

    if config.agent.mode == "hierarchical":
        # Use ModelRouter for multi-model routing; fall back to plain model if explicitly provided
        effective_model = model if model is not None else build_model_router(config)
        hierarchical_config = HierarchicalAgentConfig(
            max_steps=config.agent.max_steps,
            max_retries_per_step=config.agent.max_retries_per_step,
            max_steps_easy=config.agent.max_steps_easy,
            max_steps_medium=config.agent.max_steps_medium,
            max_steps_hard=config.agent.max_steps_hard,
            max_steps_extreme=config.agent.max_steps_extreme,
            verifier_enabled=config.agent.verifier_enabled,
            max_verification_rounds=config.agent.max_verification_rounds,
        )
        return HierarchicalDataAgent(
            model=effective_model,
            tools=effective_tools,
            config=hierarchical_config,
        )
    else:
        effective_model = model if model is not None else build_model_adapter(config)
        react_config = ReActAgentConfig(
            max_steps=config.agent.max_steps,
            max_retries_per_step=config.agent.max_retries_per_step,
        )
        return ReActAgent(
            model=effective_model,
            tools=effective_tools,
            config=react_config,
        )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _write_csv(path: Path, columns: list[str], rows: list[list[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in rows:
            writer.writerow(row)


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _failure_run_result_payload(task_id: str, failure_reason: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "answer": None,
        "steps": [],
        "failure_reason": failure_reason,
        "succeeded": False,
    }


def _run_single_task_core(
    *,
    task_id: str,
    config: AppConfig,
    model=None,
    tools: ToolRegistry | None = None,
    pre_computed_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    public_dataset = DABenchPublicDataset(config.dataset.root_path)
    task = public_dataset.get_task(task_id)
    agent = build_agent(config, model=model, tools=tools)
    # Pass pre-computed plan to skip planner phase inside subprocess
    if pre_computed_plan is not None and hasattr(agent, "run"):
        import inspect as _inspect
        if "plan" in _inspect.signature(agent.run).parameters:
            run_result = agent.run(task, plan=pre_computed_plan)
            return run_result.to_dict()
    run_result = agent.run(task)
    return run_result.to_dict()


def _run_single_task_in_subprocess(
    task_id: str,
    config: AppConfig,
    queue: multiprocessing.Queue[Any],
    pre_computed_plan: dict[str, Any] | None = None,
) -> None:
    try:
        queue.put(
            {
                "ok": True,
                "run_result": _run_single_task_core(
                    task_id=task_id,
                    config=config,
                    pre_computed_plan=pre_computed_plan,
                ),
            }
        )
    except BaseException as exc:  # noqa: BLE001
        queue.put(
            {
                "ok": False,
                "error": str(exc),
            }
        )


def _run_single_task_with_timeout(
    *,
    task_id: str,
    config: AppConfig,
    pre_computed_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    timeout_seconds = config.run.task_timeout_seconds
    if timeout_seconds <= 0:
        return _run_single_task_core(
            task_id=task_id, config=config, pre_computed_plan=pre_computed_plan
        )

    queue: multiprocessing.Queue[Any] = multiprocessing.Queue()
    process = multiprocessing.Process(
        target=_run_single_task_in_subprocess,
        args=(task_id, config, queue, pre_computed_plan),
    )
    process.start()
    process.join(timeout_seconds)

    if process.is_alive():
        process.terminate()
        process.join(timeout=1.0)
        if process.is_alive():
            process.kill()
            process.join()
        return _failure_run_result_payload(task_id, f"Task timed out after {timeout_seconds} seconds.")

    if queue.empty():
        exit_code = process.exitcode
        if exit_code not in (None, 0):
            return _failure_run_result_payload(
                task_id,
                f"Task exited unexpectedly with exit code {exit_code}.",
            )
        return _failure_run_result_payload(task_id, "Task exited without returning a result.")

    result = queue.get()
    if result.get("ok"):
        return dict(result["run_result"])
    return _failure_run_result_payload(task_id, f"Task failed with uncaught error: {result['error']}")


def _write_task_outputs(task_id: str, run_output_dir: Path, run_result: dict[str, Any]) -> TaskRunArtifacts:
    task_output_dir = run_output_dir / task_id
    task_output_dir.mkdir(parents=True, exist_ok=True)
    trace_path = task_output_dir / "trace.json"
    _write_json(trace_path, run_result)

    prediction_csv_path: Path | None = None
    answer = run_result.get("answer")
    if isinstance(answer, dict):
        prediction_csv_path = task_output_dir / "prediction.csv"
        _write_csv(
            prediction_csv_path,
            list(answer.get("columns", [])),
            [list(row) for row in answer.get("rows", [])],
        )

    return TaskRunArtifacts(
        task_id=task_id,
        task_output_dir=task_output_dir,
        prediction_csv_path=prediction_csv_path,
        trace_path=trace_path,
        succeeded=bool(run_result.get("succeeded")),
        failure_reason=run_result.get("failure_reason"),
    )


def run_single_task(
    *,
    task_id: str,
    config: AppConfig,
    run_output_dir: Path,
    model=None,
    tools: ToolRegistry | None = None,
    pre_computed_plan: dict[str, Any] | None = None,
) -> TaskRunArtifacts:
    started_at = perf_counter()
    if model is None and tools is None:
        run_result = _run_single_task_with_timeout(
            task_id=task_id, config=config, pre_computed_plan=pre_computed_plan
        )
    else:
        run_result = _run_single_task_core(
            task_id=task_id, config=config, model=model, tools=tools,
            pre_computed_plan=pre_computed_plan,
        )
    run_result["e2e_elapsed_seconds"] = round(perf_counter() - started_at, 3)
    artifact = _write_task_outputs(task_id, run_output_dir, run_result)
    _append_jsonl(
        run_output_dir / "task_runs.jsonl",
        {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "task_id": artifact.task_id,
            "succeeded": artifact.succeeded,
            "failure_reason": artifact.failure_reason,
            "trace_path": str(artifact.trace_path),
            "prediction_csv_path": (
                str(artifact.prediction_csv_path) if artifact.prediction_csv_path is not None else None
            ),
        },
    )
    return artifact


def run_benchmark(
    *,
    config: AppConfig,
    model=None,
    tools: ToolRegistry | None = None,
    limit: int | None = None,
    progress_callback: Callable[[TaskRunArtifacts], None] | None = None,
    config_path: str = "",
    optimizations_this_run: list[str] | None = None,
) -> tuple[Path, list[TaskRunArtifacts]]:
    effective_run_id, run_output_dir = create_run_output_dir(config.run.output_dir, run_id=config.run.run_id)

    dataset = DABenchPublicDataset(config.dataset.root_path)
    tasks = dataset.iter_tasks()
    if limit is not None:
        tasks = tasks[:limit]

    effective_workers = config.run.max_workers
    if effective_workers < 1:
        raise ValueError("max_workers must be at least 1.")
    if model is not None or tools is not None:
        effective_workers = 1

    task_ids = [task.task_id for task in tasks]

    # ── Pre-plan phase (hierarchical mode only) ───────────────────────────────
    # Run all planners serially in the MAIN thread (no subprocess timeout pressure).
    # This prevents the planner LLM call from eating into the per-task timeout,
    # which caused 0-step timeouts when the API was slow under high concurrency.
    pre_computed_plans: dict[str, dict[str, Any]] = {}
    if config.agent.mode == "hierarchical" and model is None and tools is None:
        dataset_for_planning = DABenchPublicDataset(config.dataset.root_path)
        router = build_model_router(config)
        from data_agent_baseline.agents.hierarchical import HierarchicalDataAgent, HierarchicalAgentConfig
        from data_agent_baseline.tools.registry import create_default_tool_registry as _ctr
        _planner_agent = HierarchicalDataAgent(
            model=router,
            tools=_ctr(),
            config=HierarchicalAgentConfig(),
        )
        print(f"[planner] Pre-computing plans for {len(task_ids)} tasks (serial)...")
        for i, task_id in enumerate(task_ids, 1):
            _task = dataset_for_planning.get_task(task_id)
            plan = _planner_agent._plan(_task)
            pre_computed_plans[task_id] = plan
            print(f"[planner] {i}/{len(task_ids)} {task_id} ✓")

    task_artifacts: list[TaskRunArtifacts]
    if effective_workers == 1:
        shared_model = model or build_model_adapter(config)
        shared_tools = tools or create_default_tool_registry()
        task_artifacts = []
        for task_id in task_ids:
            artifact = run_single_task(
                task_id=task_id,
                config=config,
                run_output_dir=run_output_dir,
                model=shared_model,
                tools=shared_tools,
                pre_computed_plan=pre_computed_plans.get(task_id),
            )
            task_artifacts.append(artifact)
            if progress_callback is not None:
                progress_callback(artifact)
    else:
        with ThreadPoolExecutor(max_workers=effective_workers) as executor:
            future_to_index = {
                executor.submit(
                    run_single_task,
                    task_id=task_id,
                    config=config,
                    run_output_dir=run_output_dir,
                    pre_computed_plan=pre_computed_plans.get(task_id),
                ): index
                for index, task_id in enumerate(task_ids)
            }
            indexed_artifacts: list[TaskRunArtifacts | None] = [None] * len(task_ids)
            for future in as_completed(future_to_index):
                artifact = future.result()
                indexed_artifacts[future_to_index[future]] = artifact
                if progress_callback is not None:
                    progress_callback(artifact)
            task_artifacts = [artifact for artifact in indexed_artifacts if artifact is not None]

    summary_path = run_output_dir / "summary.json"
    _write_json(
        summary_path,
        {
            "run_id": effective_run_id,
            "agent_mode": config.agent.mode,
            "task_count": len(task_artifacts),
            "succeeded_task_count": sum(1 for artifact in task_artifacts if artifact.succeeded),
            "max_workers": effective_workers,
            "tasks": [artifact.to_dict() for artifact in task_artifacts],
        },
    )

    failed_tasks = [artifact.task_id for artifact in task_artifacts if not artifact.succeeded]
    _append_jsonl(
        config.run.output_dir / "run_history.jsonl",
        {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "run_id": effective_run_id,
            "agent_mode": config.agent.mode,
            "run_output_dir": str(run_output_dir),
            "task_count": len(task_artifacts),
            "succeeded_task_count": sum(1 for artifact in task_artifacts if artifact.succeeded),
            "failed_task_count": len(failed_tasks),
            "failed_tasks": failed_tasks,
            "summary_path": str(summary_path),
        },
    )

    # Auto-generate Markdown run log
    from data_agent_baseline.run.log_generator import generate_run_log
    logs_dir = Path(__file__).resolve().parent.parent.parent.parent / "logs"
    try:
        log_path = generate_run_log(
            run_id=effective_run_id,
            run_output_dir=run_output_dir,
            task_artifacts=task_artifacts,
            logs_dir=logs_dir,
            config_path=config_path,
            optimizations_this_run=optimizations_this_run,
        )
        print(f"[log] Run report saved → {log_path}")
    except Exception as _log_exc:
        print(f"[log] Warning: could not generate run log: {_log_exc}")

    return run_output_dir, task_artifacts
