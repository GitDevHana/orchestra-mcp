"""Completion decisions from explicit evidence, not prose or a bare 'done'."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class CriterionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criterion: str = Field(min_length=1, max_length=2000)
    status: Literal["pass", "needs_fix", "unverifiable"]
    evidence: str = Field(min_length=1, max_length=4000)


class Assessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pass", "needs_fix", "unverifiable"]
    blockers: list[str] = Field(default_factory=list, max_length=100)
    evidence: list[str] = Field(default_factory=list, max_length=100)
    criteria: list[CriterionResult] = Field(default_factory=list, max_length=50)


def assess(result: dict, criteria: list[str]) -> dict:
    try:
        value = Assessment.model_validate(result.get("assessment"))
    except ValidationError:
        return {"status": "unverifiable", "blockers": [], "evidence": [], "criteria": [],
                "reason": "구조화된 판정이 없거나 형식이 잘못되었습니다. done만으로 완료하지 않습니다."}
    data = value.model_dump()
    if any(not s.strip() or len(s) > 8000 for s in value.blockers + value.evidence):
        data.update(status="unverifiable", reason="비어 있거나 너무 긴 검토 근거입니다.")
        return data
    if value.blockers or any(c.status == "needs_fix" for c in value.criteria):
        data.update(status="needs_fix", reason="수정할 차단 결함 또는 미충족 완료 기준이 있습니다.")
    elif any(c.status == "unverifiable" for c in value.criteria):
        data.update(status="unverifiable", reason="검증하지 못한 완료 기준이 있습니다.")
    elif value.status == "pass":
        reports = {c.criterion: c for c in value.criteria}
        if (not value.evidence or len(reports) != len(value.criteria)
                or any(c not in reports or reports[c].status != "pass" for c in criteria)):
            data.update(status="unverifiable", reason="통과 근거 또는 완료 기준별 확인 결과가 부족합니다.")
    return data


def role_for(mode: str, key: str) -> str:
    if mode == "implement_review" and key == "b":
        return "읽기 전용 검토자: A의 현재 코드를 검토하고 차단 결함을 A에게 돌려보낸다."
    if mode == "dual_implementers":
        return "독립 구현자: 자신의 코드만 구현·검증하고 자신에게 전달된 피드백을 반영한다."
    return "구현자: 코드를 구현·검증하고 검토자가 발견한 차단 결함을 수정한다."


def check_task_roles(task: str, mode: str) -> None:
    # Only explicit agent-role declarations; do not classify arbitrary prose.
    # Continuations check new instructions, not archived roles from older stages.
    if mode != "dual_implementers":
        return
    for line in task.splitlines():
        if re.match(r"^\s*(?:[-*]\s*)?(?:Agent\s+)?[ABab]\s*[:：]", line):
            if re.search(r"읽기\s*전용|read[- ]only", line, re.I):
                raise ValueError("옵션 2는 A/B 독립 구현입니다. 과제의 A/B 읽기 전용 역할과 충돌합니다. "
                                 "옵션 1을 선택하거나 해당 역할 지시를 수정하세요.")


def completion_gate(assessments: list[dict], validation_passed: bool | None) -> dict:
    if validation_passed is False or any(a.get("status") == "needs_fix" for a in assessments):
        return {"status": "needs_fix", "reason": "차단 결함 또는 검증 실패가 남아 있습니다."}
    if validation_passed is None:
        return {"status": "unverified", "reason": "등록된 검증 명령이 없어 완료를 확인하지 못했습니다."}
    if not assessments or any(a.get("status") != "pass" for a in assessments):
        return {"status": "unverified", "reason": "구현·검토의 통과 근거가 부족하거나 검증 불가입니다."}
    return {"status": "completed", "reason": "등록된 검증과 구현·검토의 완료 기준을 통과했습니다."}
