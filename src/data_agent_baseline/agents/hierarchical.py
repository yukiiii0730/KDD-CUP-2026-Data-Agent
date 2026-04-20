"""
HierarchicalDataAgent — Three-Phase Data Agent

Phase 1 (Planner):   One LLM call to analyze context files and produce an execution plan.
Phase 2 (Executor):  Plan-guided ReAct loop with difficulty-adaptive step limits.
Phase 3 (Verifier):  One LLM call to verify answer completeness; triggers re-execution if needed.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from data_agent_baseline.agents.model import ModelAdapter, ModelMessage, ModelRouter
from data_agent_baseline.agents.prompt import (
    PLANNER_SYSTEM_PROMPT,
    VERIFIER_SYSTEM_PROMPT,
    build_executor_system_prompt,
    build_planner_prompt,
    build_verifier_prompt,
)
from data_agent_baseline.agents.react import ReActAgent, ReActAgentConfig
from data_agent_baseline.agents.runtime import AgentRunResult
from data_agent_baseline.benchmark.schema import PublicTask
from data_agent_baseline.tools.filesystem import list_context_tree
from data_agent_baseline.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class HierarchicalAgentConfig:
    # Fallback max_steps when difficulty is not in the map
    max_steps: int = 20
    max_retries_per_step: int = 2

    # Per-difficulty step limits
    max_steps_easy: int = 10
    max_steps_medium: int = 20
    max_steps_hard: int = 30
    max_steps_extreme: int = 40

    # Verifier controls
    verifier_enabled: bool = True
    max_verification_rounds: int = 2


_DIFFICULTY_STEP_MAP = {
    "easy": "max_steps_easy",
    "medium": "max_steps_medium",
    "hard": "max_steps_hard",
    "extreme": "max_steps_extreme",
}


def _parse_json_response(raw: str) -> dict:
    """Extract and parse a JSON object from a raw LLM response."""
    # Try to strip markdown fences first
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.DOTALL | re.IGNORECASE)
    text = fence_match.group(1).strip() if fence_match else raw.strip()

    # Find first { ... } span
    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in response.")
    payload, _ = json.JSONDecoder().raw_decode(text, start)
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object.")
    return payload


class HierarchicalDataAgent:
    """
    Three-phase hierarchical data agent:
      1. Planner  — survey context, produce execution plan
      2. Executor — plan-guided ReAct with adaptive step limits
      3. Verifier — check answer completeness, optionally re-execute

    Model routing (via ModelRouter):
      - Planner  uses get_planner(difficulty)  → strong reasoning model for hard/extreme
      - Executor uses get_executor(difficulty)  → coder model (best at SQL/Python)
      - Verifier uses get_verifier()            → cheap text model (just checks result)
    """

    def __init__(
        self,
        *,
        model: ModelAdapter | ModelRouter,
        tools: ToolRegistry,
        config: HierarchicalAgentConfig | None = None,
    ) -> None:
        self.tools = tools
        self.config = config or HierarchicalAgentConfig()
        # Wrap plain ModelAdapter in a pass-through router for uniform API
        if isinstance(model, ModelRouter):
            self.router = model
        else:
            self.router = ModelRouter(default_model=model)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_max_steps(self, difficulty: str) -> int:
        attr = _DIFFICULTY_STEP_MAP.get(difficulty.lower())
        if attr:
            return getattr(self.config, attr)
        return self.config.max_steps

    def _plan(self, task: PublicTask) -> dict:
        """Phase 1: one-shot planning call — uses planner model."""
        file_listing = list_context_tree(task, max_depth=4)
        planner_prompt = build_planner_prompt(task, file_listing)
        messages = [
            ModelMessage(role="system", content=PLANNER_SYSTEM_PROMPT),
            ModelMessage(role="user", content=planner_prompt),
        ]
        planner_model = self.router.get_planner(task.difficulty)
        try:
            raw_response = planner_model.complete(messages)
            return _parse_json_response(raw_response)
        except Exception:
            # Graceful fallback: empty plan (executor still runs with base prompt)
            return {
                "approach": "Inspect context files and answer the question.",
                "data_sources": [],
                "query_strategy": "combination",
                "expected_columns": [],
                "key_filters": [],
            }

    def _execute(self, task: PublicTask, plan: dict, feedback: str = "") -> AgentRunResult:
        """Phase 2: plan-guided ReAct execution — uses executor model (coder-specialized)."""
        react_config = ReActAgentConfig(
            max_steps=self._get_max_steps(task.difficulty),
            max_retries_per_step=self.config.max_retries_per_step,
        )
        system_prompt = build_executor_system_prompt(plan, feedback)
        executor_model = self.router.get_executor(task.difficulty)
        react_agent = ReActAgent(
            model=executor_model,
            tools=self.tools,
            config=react_config,
            system_prompt=system_prompt,
        )
        return react_agent.run(task)

    def _verify(self, task: PublicTask, result: AgentRunResult, plan: dict) -> dict:
        """Phase 3: one-shot verification — uses cheap verifier model."""
        if result.answer is None:
            return {
                "is_correct": False,
                "issues": ["No answer was submitted."],
                "feedback": "The agent did not submit any answer. Re-execute and make sure to call the answer tool.",
            }

        answer_dict = result.answer.to_dict()
        verify_prompt = build_verifier_prompt(task, answer_dict, plan)
        messages = [
            ModelMessage(role="system", content=VERIFIER_SYSTEM_PROMPT),
            ModelMessage(role="user", content=verify_prompt),
        ]
        verifier_model = self.router.get_verifier()
        try:
            raw_response = verifier_model.complete(messages)
            return _parse_json_response(raw_response)
        except Exception:
            # If verification itself fails, trust the answer as-is
            return {"is_correct": True, "issues": [], "feedback": ""}

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self, task: PublicTask, plan: dict | None = None) -> AgentRunResult:
        # Phase 1: Plan (planner model) — skip if a pre-computed plan is provided
        if plan is None:
            plan = self._plan(task)

        # Phase 2: Execute (executor / coder model)
        result = self._execute(task, plan)

        # Phase 3: Verify (verifier / cheap model)
        if self.config.verifier_enabled:
            for _round in range(self.config.max_verification_rounds):
                verification = self._verify(task, result, plan)
                if verification.get("is_correct", True):
                    break
                feedback = verification.get("feedback", "")
                if not feedback:
                    break
                # Re-execute with feedback injected into system prompt
                result = self._execute(task, plan, feedback=feedback)

        return result
