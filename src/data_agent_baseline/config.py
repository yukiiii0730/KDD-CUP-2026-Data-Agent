from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _load_project_dotenv() -> None:
    dotenv_path = PROJECT_ROOT / ".env"
    if not dotenv_path.exists():
        return

    for line in dotenv_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].strip()
        if "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        env_key = key.strip()
        env_value = value.strip()
        if (
            len(env_value) >= 2
            and ((env_value.startswith('"') and env_value.endswith('"'))
            or (env_value.startswith("'") and env_value.endswith("'")))
        ):
            env_value = env_value[1:-1]
        if env_key and env_key not in os.environ:
            os.environ[env_key] = env_value


def _expand_env_vars(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith("env:"):
        env_key = stripped[4:].strip()
        return os.getenv(env_key, "")

    return ENV_VAR_PATTERN.sub(lambda match: os.getenv(match.group(1), ""), value)


def _default_dataset_root() -> Path:
    return PROJECT_ROOT / "data" / "public" / "input"


def _default_run_output_dir() -> Path:
    return PROJECT_ROOT / "artifacts" / "runs"


@dataclass(frozen=True, slots=True)
class DatasetConfig:
    root_path: Path = field(default_factory=_default_dataset_root)


@dataclass(frozen=True, slots=True)
class AgentConfig:
    model: str = "gpt-4.1-mini"
    api_base: str = "https://api.openai.com/v1"
    api_key: str = ""
    max_steps: int = 20
    max_retries_per_step: int = 2
    temperature: float = 0.0

    # Hierarchical agent mode: "react" | "hierarchical"
    mode: str = "hierarchical"

    # Verifier phase controls
    verifier_enabled: bool = True
    max_verification_rounds: int = 2

    # Difficulty-adaptive step limits
    max_steps_easy: int = 10
    max_steps_medium: int = 20
    max_steps_hard: int = 30
    max_steps_extreme: int = 40

    # ── Per-role × per-difficulty model routing ──────────────
    # Empty string means "fall back to self.model"
    model_planner_easy:    str = ""
    model_planner_medium:  str = ""
    model_planner_hard:    str = ""
    model_planner_extreme: str = ""

    model_executor_easy:    str = ""
    model_executor_medium:  str = ""
    model_executor_hard:    str = ""
    model_executor_extreme: str = ""

    # Single verifier model (text-only, one difficulty tier)
    model_verifier: str = ""


@dataclass(frozen=True, slots=True)
class RunConfig:
    output_dir: Path = field(default_factory=_default_run_output_dir)
    run_id: str | None = None
    max_workers: int = 4
    task_timeout_seconds: int = 600


@dataclass(frozen=True, slots=True)
class AppConfig:
    dataset: DatasetConfig = field(default_factory=DatasetConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    run: RunConfig = field(default_factory=RunConfig)


def _path_value(raw_value: str | None, default_value: Path) -> Path:
    if not raw_value:
        return default_value
    candidate = Path(raw_value)
    if candidate.is_absolute():
        return candidate
    return (PROJECT_ROOT / candidate).resolve()


def _string_value(raw_value: object, default_value: str) -> str:
    if raw_value is None:
        return default_value
    return _expand_env_vars(str(raw_value))


def load_app_config(config_path: Path) -> AppConfig:
    _load_project_dotenv()
    payload = yaml.safe_load(config_path.read_text()) or {}
    dataset_defaults = DatasetConfig()
    agent_defaults = AgentConfig()
    run_defaults = RunConfig()

    dataset_payload = payload.get("dataset", {})
    agent_payload = payload.get("agent", {})
    run_payload = payload.get("run", {})

    dataset_config = DatasetConfig(
        root_path=_path_value(dataset_payload.get("root_path"), dataset_defaults.root_path),
    )
    def _model_str(key: str) -> str:
        return _string_value(agent_payload.get(key), "")

    agent_config = AgentConfig(
        model=_string_value(agent_payload.get("model"), agent_defaults.model),
        api_base=_string_value(agent_payload.get("api_base"), agent_defaults.api_base),
        api_key=_string_value(agent_payload.get("api_key"), agent_defaults.api_key),
        max_steps=int(agent_payload.get("max_steps", agent_defaults.max_steps)),
        max_retries_per_step=int(
            agent_payload.get("max_retries_per_step", agent_defaults.max_retries_per_step)
        ),
        temperature=float(agent_payload.get("temperature", agent_defaults.temperature)),
        mode=str(agent_payload.get("mode", agent_defaults.mode)),
        verifier_enabled=bool(agent_payload.get("verifier_enabled", agent_defaults.verifier_enabled)),
        max_verification_rounds=int(
            agent_payload.get("max_verification_rounds", agent_defaults.max_verification_rounds)
        ),
        max_steps_easy=int(agent_payload.get("max_steps_easy", agent_defaults.max_steps_easy)),
        max_steps_medium=int(agent_payload.get("max_steps_medium", agent_defaults.max_steps_medium)),
        max_steps_hard=int(agent_payload.get("max_steps_hard", agent_defaults.max_steps_hard)),
        max_steps_extreme=int(agent_payload.get("max_steps_extreme", agent_defaults.max_steps_extreme)),
        # Per-role × per-difficulty model routing
        model_planner_easy=_model_str("model_planner_easy"),
        model_planner_medium=_model_str("model_planner_medium"),
        model_planner_hard=_model_str("model_planner_hard"),
        model_planner_extreme=_model_str("model_planner_extreme"),
        model_executor_easy=_model_str("model_executor_easy"),
        model_executor_medium=_model_str("model_executor_medium"),
        model_executor_hard=_model_str("model_executor_hard"),
        model_executor_extreme=_model_str("model_executor_extreme"),
        model_verifier=_model_str("model_verifier"),
    )
    raw_run_id = run_payload.get("run_id")
    run_id = run_defaults.run_id
    if raw_run_id is not None:
        normalized_run_id = str(raw_run_id).strip()
        run_id = normalized_run_id or None

    run_config = RunConfig(
        output_dir=_path_value(run_payload.get("output_dir"), run_defaults.output_dir),
        run_id=run_id,
        max_workers=int(run_payload.get("max_workers", run_defaults.max_workers)),
        task_timeout_seconds=int(run_payload.get("task_timeout_seconds", run_defaults.task_timeout_seconds)),
    )
    return AppConfig(dataset=dataset_config, agent=agent_config, run=run_config)
