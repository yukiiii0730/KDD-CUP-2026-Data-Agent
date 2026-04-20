from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import time

from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, OpenAI


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class ModelStep:
    thought: str
    action: str
    action_input: dict[str, Any]
    raw_response: str


class ModelAdapter(Protocol):
    def complete(self, messages: list[ModelMessage]) -> str:
        raise NotImplementedError


class OpenAIModelAdapter:
    def __init__(
        self,
        *,
        model: str,
        api_base: str,
        api_key: str,
        temperature: float,
        extra_body: dict | None = None,
    ) -> None:
        self.model = model
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.temperature = temperature
        # extra_body: passed verbatim to the API (e.g. {"enable_thinking": False} for qwen3 small models)
        self.extra_body = extra_body or {}

    def complete(self, messages: list[ModelMessage]) -> str:
        if not self.api_key:
            raise RuntimeError("Missing model API key in config.agent.api_key.")

        client = OpenAI(
            api_key=self.api_key,
            base_url=self.api_base,
        )

        kwargs: dict[str, Any] = dict(
            model=self.model,
            messages=[{"role": m.role, "content": m.content} for m in messages],
            temperature=self.temperature,
        )
        if self.extra_body:
            kwargs["extra_body"] = self.extra_body

        last_exc: Exception | None = None
        for _attempt in range(5):
            try:
                response = client.chat.completions.create(**kwargs)
                break
            except (APIConnectionError, APITimeoutError) as exc:
                # Network-level errors — retry with exponential back-off
                last_exc = exc
                time.sleep(min(2 ** _attempt, 30))  # 1, 2, 4, 8, 16s (cap 30s)
            except APIStatusError as exc:
                # Retry on 5xx server errors; raise immediately on 4xx
                if exc.status_code >= 500:
                    last_exc = exc
                    time.sleep(min(2 ** _attempt, 30))
                else:
                    raise RuntimeError(f"Model request failed: {exc}") from exc
            except APIError as exc:
                raise RuntimeError(f"Model request failed: {exc}") from exc
        else:
            raise RuntimeError(f"Model request failed: {last_exc}") from last_exc

        choices = response.choices or []
        if not choices:
            raise RuntimeError("Model response missing choices.")
        content = choices[0].message.content
        if not isinstance(content, str):
            raise RuntimeError("Model response missing text content.")
        return content


class ScriptedModelAdapter:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    def complete(self, messages: list[ModelMessage]) -> str:
        del messages
        if not self._responses:
            raise RuntimeError("No scripted model responses remaining.")
        return self._responses.pop(0)


class ModelRouter:
    """
    Routes LLM calls to different OpenAIModelAdapter instances
    based on agent role (planner / executor / verifier) and task difficulty.

    Routing table (all keys optional — falls back to `default_model`):
        planner_models:  dict[difficulty, ModelAdapter]
        executor_models: dict[difficulty, ModelAdapter]
        verifier_model:  ModelAdapter
        default_model:   ModelAdapter  (used when no specific entry is found)
    """

    def __init__(
        self,
        *,
        default_model: ModelAdapter,
        planner_models: dict[str, ModelAdapter] | None = None,
        executor_models: dict[str, ModelAdapter] | None = None,
        verifier_model: ModelAdapter | None = None,
    ) -> None:
        self._default = default_model
        self._planner_models: dict[str, ModelAdapter] = planner_models or {}
        self._executor_models: dict[str, ModelAdapter] = executor_models or {}
        self._verifier_model: ModelAdapter = verifier_model or default_model

    def get_planner(self, difficulty: str) -> ModelAdapter:
        return self._planner_models.get(difficulty.lower(), self._default)

    def get_executor(self, difficulty: str) -> ModelAdapter:
        return self._executor_models.get(difficulty.lower(), self._default)

    def get_verifier(self) -> ModelAdapter:
        return self._verifier_model

    # Convenience: behave as a plain ModelAdapter (uses default)
    def complete(self, messages: list[ModelMessage]) -> str:
        return self._default.complete(messages)
