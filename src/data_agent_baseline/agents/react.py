from __future__ import annotations

import json
import re
from dataclasses import dataclass

from data_agent_baseline.agents.model import ModelAdapter, ModelMessage, ModelStep
from data_agent_baseline.agents.prompt import (
    REACT_SYSTEM_PROMPT,
    build_observation_prompt,
    build_system_prompt,
    build_task_prompt,
)
from data_agent_baseline.agents.runtime import AgentRunResult, AgentRuntimeState, StepRecord
from data_agent_baseline.benchmark.schema import PublicTask
from data_agent_baseline.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class ReActAgentConfig:
    max_steps: int = 16
    max_retries_per_step: int = 1


def _strip_json_fence(raw_response: str) -> str:
    text = raw_response.strip()
    fence_match = re.search(r"```json\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fence_match is not None:
        return fence_match.group(1).strip()
    generic_fence_match = re.search(r"```\s*(.*?)\s*```", text, flags=re.DOTALL)
    if generic_fence_match is not None:
        return generic_fence_match.group(1).strip()
    return text


def _load_single_json_object(text: str) -> dict[str, object]:
    payload, end = json.JSONDecoder().raw_decode(text)
    remainder = text[end:].strip()
    if remainder:
        cleaned_remainder = re.sub(r"(?:\\[nrt])+", "", remainder).strip()
        if cleaned_remainder:
            raise ValueError("Model response must contain only one JSON object.")
    if not isinstance(payload, dict):
        raise ValueError("Model response must be a JSON object.")
    return payload


def parse_model_step(raw_response: str) -> ModelStep:
    normalized = _strip_json_fence(raw_response)
    payload = _load_single_json_object(normalized)

    thought = payload.get("thought", "")
    action = payload.get("action")
    action_input = payload.get("action_input", {})
    if not isinstance(thought, str):
        raise ValueError("thought must be a string.")
    if not isinstance(action, str) or not action:
        raise ValueError("action must be a non-empty string.")
    if not isinstance(action_input, dict):
        raise ValueError("action_input must be a JSON object.")

    return ModelStep(
        thought=thought,
        action=action,
        action_input=action_input,
        raw_response=raw_response,
    )


class ReActAgent:
    def __init__(
        self,
        *,
        model: ModelAdapter,
        tools: ToolRegistry,
        config: ReActAgentConfig | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.config = config or ReActAgentConfig()
        self.system_prompt = system_prompt or REACT_SYSTEM_PROMPT

    def _build_messages(
        self,
        task: PublicTask,
        state: AgentRuntimeState,
        retry_error: str | None = None,
        steps_remaining: int | None = None,
    ) -> list[ModelMessage]:
        system_content = build_system_prompt(
            self.tools.describe_for_prompt(),
            system_prompt=self.system_prompt,
        )
        messages = [ModelMessage(role="system", content=system_content)]
        messages.append(ModelMessage(role="user", content=build_task_prompt(task)))
        for step in state.steps:
            messages.append(ModelMessage(role="assistant", content=step.raw_response))
            messages.append(
                ModelMessage(role="user", content=build_observation_prompt(step.observation))
            )
        if retry_error is not None:
            messages.append(
                ModelMessage(
                    role="user",
                    content=(
                        "Your previous response could not be executed. "
                        f"Error: {retry_error}\n"
                        "Please try the same step again and return exactly one valid JSON "
                        "object in a single ```json fenced block with keys thought, action, action_input."
                    ),
                )
            )
        if steps_remaining is not None and steps_remaining <= 2:
            messages.append(
                ModelMessage(
                    role="user",
                    content=(
                        f"⚠️ URGENT: You have only {steps_remaining} step(s) remaining. "
                        "You MUST call the `answer` tool NOW with your best current answer. "
                        "Do NOT use any other tool. Submitting a partial answer is better than no answer."
                    ),
                )
            )
        return messages

    def run(self, task: PublicTask) -> AgentRunResult:
        state = AgentRuntimeState()
        for step_index in range(1, self.config.max_steps + 1):
            retry_error: str | None = None
            raw_response = ""
            max_attempts = max(1, self.config.max_retries_per_step + 1)
            steps_remaining = self.config.max_steps - step_index + 1
            for attempt in range(1, max_attempts + 1):
                raw_response = self.model.complete(
                    self._build_messages(
                        task,
                        state,
                        retry_error=retry_error,
                        steps_remaining=steps_remaining,
                    )
                )
                try:
                    model_step = parse_model_step(raw_response)
                    tool_result = self.tools.execute(task, model_step.action, model_step.action_input)
                    observation = {
                        "ok": tool_result.ok,
                        "tool": model_step.action,
                        "content": tool_result.content,
                    }
                    step_record = StepRecord(
                        step_index=step_index,
                        thought=model_step.thought,
                        action=model_step.action,
                        action_input=model_step.action_input,
                        raw_response=raw_response,
                        observation=observation,
                        ok=tool_result.ok,
                    )
                    state.steps.append(step_record)
                    if tool_result.is_terminal:
                        state.answer = tool_result.answer
                    break
                except Exception as exc:
                    retry_error = str(exc)
                    if attempt == max_attempts:
                        observation = {
                            "ok": False,
                            "error": retry_error,
                            "attempts": attempt,
                        }
                        state.steps.append(
                            StepRecord(
                                step_index=step_index,
                                thought="",
                                action="__error__",
                                action_input={},
                                raw_response=raw_response,
                                observation=observation,
                                ok=False,
                            )
                        )

            if state.answer is not None:
                break

        if state.answer is None and state.failure_reason is None:
            state.failure_reason = "Agent did not submit an answer within max_steps."

        return AgentRunResult(
            task_id=task.task_id,
            answer=state.answer,
            steps=list(state.steps),
            failure_reason=state.failure_reason,
        )
