from __future__ import annotations

from pathlib import Path
from typing import Literal
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["codex", "cursor", "claude", "gemini"]
    name: str = Field(default="", max_length=40)
    role: str = Field(default="구현하고 검증한다", max_length=2000)
    model: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9._:/-]{1,100}$")
    max_tokens: int | None = Field(default=30000, ge=100, le=10000000,
                            validation_alias=AliasChoices("max_tokens", "max_total_tokens"))


class LimitsInput(BaseModel):
    """Optional convenience input. Values are user choices, not fixed example defaults."""
    model_config = ConfigDict(extra="forbid")
    codex_five_hour_stop_at_used_percent: float | None = Field(default=None, gt=0, le=100)
    codex_weekly_stop_at_used_percent: float | None = Field(default=None, gt=0, le=100)
    max_runtime_minutes: float | None = Field(default=None, gt=0, le=1440)
    max_rounds: int | None = Field(default=None, ge=1, le=30)


class RunSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: str = Field(min_length=1, max_length=100000)
    workspace: str
    agents: list[AgentSpec] = Field(min_length=1, max_length=2)
    mode: Literal["implement_review", "dual_implementers"] = "implement_review"
    max_seconds: float = Field(default=600, ge=1, le=86400)
    cleanup_seconds: float = Field(default=600, ge=0, le=600)
    max_rounds: int = Field(default=3, ge=1, le=30)
    access: Literal["read-only", "workspace-write"] = "workspace-write"
    validation_commands: list[list[str]] = Field(default_factory=list, max_length=8)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=50)
    dual_feedback_action: Literal["revise", "wait"] = "revise"
    execution_commands: list[list[str]] = Field(default_factory=list, max_length=32)
    validation_timeout: float = Field(default=120, ge=1, le=3600)
    command_backend: Literal["container", "trusted_host"] = "container"
    command_image: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}$")
    codex_five_hour_stop_at_used_percent: float | None = Field(default=None, gt=0, le=100)
    codex_weekly_stop_at_used_percent: float | None = Field(default=None, gt=0, le=100)
    codex_quota_limit_ids: list[str] = Field(default_factory=lambda: ["codex"], min_length=1, max_length=16)
    codex_quota_poll_seconds: float = Field(default=15, ge=1, le=300)
    missing_quota_policy: Literal["pause_for_confirmation", "stop", "continue_with_warning"] = "pause_for_confirmation"

    codex_reserve_percent: float = Field(default=0, ge=0, lt=100)

    @model_validator(mode="after")
    def collaboration(self):
        if self.codex_reserve_percent and any(a.provider == "codex" for a in self.agents):
            ceiling = 100 - self.codex_reserve_percent
            for name in ("codex_five_hour_stop_at_used_percent", "codex_weekly_stop_at_used_percent"):
                chosen = getattr(self, name)
                setattr(self, name, ceiling if chosen is None else min(chosen, ceiling))
        for agent in self.agents:
            if agent.max_tokens is None:
                if agent.provider != "codex":
                    raise ValueError("Only Codex supports subscription-only budgets")
                if self.codex_five_hour_stop_at_used_percent is None and self.codex_weekly_stop_at_used_percent is None:
                    raise ValueError("Subscription-only budgets require a usage percentage threshold")
                if self.missing_quota_policy == "continue_with_warning":
                    raise ValueError("Subscription-only budgets require pause or stop on missing quota")
        if self.mode == "dual_implementers":
            if len(self.agents) != 2:
                raise ValueError("dual_implementers requires exactly two agents")
            if self.execution_commands:
                raise ValueError("dual mode does not execute follow-up commands against the original workspace")
        return self

    @model_validator(mode="before")
    @classmethod
    def expand_limits(cls, value):
        if isinstance(value, dict) and "limits" in value:
            value = dict(value)
            nested = LimitsInput.model_validate(value.pop("limits"))
            for key, field in nested.model_dump(exclude_unset=True).items():
                if field is None:
                    continue
                target, field = ("max_seconds", field * 60) if key == "max_runtime_minutes" else (key, field)
                if target in value:
                    raise ValueError(f"{target} is configured both directly and in limits")
                value[target] = field
        return value

    @field_validator("codex_quota_limit_ids")
    @classmethod
    def quota_ids(cls, values):
        if any(not value or len(value) > 100 for value in values) or len(set(values)) != len(values):
            raise ValueError("quota bucket IDs must be distinct, nonempty and at most 100 characters")
        return values

    @field_validator("acceptance_criteria")
    @classmethod
    def criteria_valid(cls, values):
        values = [v.strip() for v in values]
        if any(not v or len(v) > 2000 for v in values) or len(set(values)) != len(values):
            raise ValueError("완료 기준은 비어 있지 않은 고유한 문장이어야 합니다 (각 2,000자 이하).")
        return values

    @field_validator("workspace")
    @classmethod
    def workspace_exists(cls, value: str) -> str:
        path = Path(value).expanduser().resolve()
        if not path.is_dir():
            raise ValueError("workspace must be an existing directory")
        return str(path)

    @field_validator("validation_commands", "execution_commands")
    @classmethod
    def valid_commands(cls, commands: list[list[str]]) -> list[list[str]]:
        for command in commands:
            if not command or any(not arg or "\0" in arg for arg in command):
                raise ValueError("validation commands must be nonempty argv arrays")
            if Path(command[0]).suffix.lower() in (".cmd", ".bat", ".ps1"):
                raise ValueError("use a native executable (python.exe, node.exe), not a shell script")
        return commands


class StageBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_tokens: int | None = Field(ge=100, le=10000000)
    provider: Literal["codex", "cursor", "claude", "gemini"] | None = None
    model: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9._:/-]{1,100}$")


class ResumeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_seconds: float = Field(ge=1, le=86400)
    agents: list[StageBudget] = Field(min_length=1, max_length=2)
    candidate: Literal["a", "b"] | None = None
    mode: Literal["implement_review", "dual_implementers"] | None = None
    max_rounds: int | None = Field(default=None, ge=1, le=30)
    feedback: str = Field(default="남은 작업을 이어서 수행하세요.", max_length=20000)
    validation_commands: list[list[str]] | None = None
    acceptance_criteria: list[str] | None = None
    dual_feedback_action: Literal["revise", "wait"] | None = None
    command_backend: Literal["container", "trusted_host"] | None = None
    command_image: str | None = None


def selected_agent(previous: dict, budget: StageBudget) -> dict:
    selected = {**previous, "max_tokens": budget.max_tokens}
    if budget.provider is not None:
        selected["provider"] = budget.provider
        if budget.provider != previous["provider"]:
            selected["model"] = None
    if "model" in budget.model_fields_set:
        selected["model"] = budget.model
    return selected


class NextStageSpec(BaseModel):
    """A user decision and fresh budgets; no inherited time/token allowance."""
    model_config = ConfigDict(extra="forbid")
    codex_reserve_percent: float = Field(default=0, ge=0, lt=100)
    codex_five_hour_stop_at_used_percent: float | None = Field(default=None, gt=0, le=100)
    codex_weekly_stop_at_used_percent: float | None = Field(default=None, gt=0, le=100)
    feedback: str = Field(min_length=1, max_length=100000)
    base_candidate: Literal["a", "b"]
    take_from_a: str = Field(default="", max_length=20000)
    take_from_b: str = Field(default="", max_length=20000)
    selected_files: dict[str, Literal["a", "b"]] = Field(default_factory=dict, max_length=500)
    agents: list[StageBudget] = Field(min_length=2, max_length=2)
    max_seconds: float = Field(ge=1, le=86400)
    max_rounds: int = Field(default=3, ge=1, le=30)
    mode: Literal["implement_review", "dual_implementers"] = "implement_review"
    validation_commands: list[list[str]] | None = None
    acceptance_criteria: list[str] | None = None
    dual_feedback_action: Literal["revise", "wait"] | None = None
    command_backend: Literal["container", "trusted_host"] | None = None
    command_image: str | None = None


TERMINAL = {"completed", "unverified", "round_limit", "failed", "cancelled", "time_limit", "token_limit", "interrupted", "quota_limit", "quota_unavailable", "awaiting_user_review"}
