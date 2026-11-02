"""Схемы запросов и ответов HTTP API review-service."""
import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.domain import (Appeal, AppealStatus, Assignment, AssignmentStatus, Role, Work,
                        WorkStatus)


class CriterionScore(BaseModel):
    criterion_id: uuid.UUID = Field(description="Критерий задачи из problem-service")
    points: int = Field(ge=0, description="Баллы по критерию")


class GradeRequest(BaseModel):
    reviewer_id: uuid.UUID = Field(description="Судья или арбитр с активным назначением")
    scores: list[CriterionScore] = Field(min_length=1, description="Баллы по каждому критерию")
    comment: str | None = Field(None, max_length=2000, description="Комментарий для участника")


class DeclineRequest(BaseModel):
    judge_id: uuid.UUID = Field(description="Судья, отказывающийся от проверки")


class AppealCreate(BaseModel):
    work_id: uuid.UUID
    participant_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=2000)


class AppealResolve(BaseModel):
    arbiter_id: uuid.UUID
    new_score: int = Field(ge=0)
    comment: str | None = Field(None, max_length=2000)


class AssignmentOut(BaseModel):
    id: uuid.UUID
    reviewer_id: uuid.UUID
    role: Role
    slot: int
    attempt: int
    status: AssignmentStatus
    deadline_at: datetime | None
    score: int | None
    comment: str | None

    @classmethod
    def of(cls, a: Assignment) -> "AssignmentOut":
        return cls(id=a.id, reviewer_id=a.reviewer_id, role=a.role, slot=a.slot, attempt=a.attempt,
                   status=a.status, deadline_at=a.deadline_at, score=a.score, comment=a.comment)


class WorkOut(BaseModel):
    id: uuid.UUID
    submission_id: uuid.UUID
    contest_id: uuid.UUID
    problem_id: uuid.UUID
    participant_id: uuid.UUID
    status: WorkStatus
    final_score: int | None
    max_score: int | None
    approved_by: Role | None
    approved_at: datetime | None
    created_at: datetime
    assignments: list[AssignmentOut]

    @classmethod
    def of(cls, w: Work) -> "WorkOut":
        return cls(id=w.id, submission_id=w.submission_id, contest_id=w.contest_id,
                   problem_id=w.problem_id, participant_id=w.participant_id, status=w.status,
                   final_score=w.final_score, max_score=w.max_score, approved_by=w.approved_by,
                   approved_at=w.approved_at, created_at=w.created_at,
                   assignments=[AssignmentOut.of(a) for a in w.assignments])


class WorkPage(BaseModel):
    items: list[WorkOut]
    total: int
    offset: int
    count: int


class AppealOut(BaseModel):
    id: uuid.UUID
    work_id: uuid.UUID
    participant_id: uuid.UUID
    reason: str
    status: AppealStatus
    old_score: int | None
    new_score: int | None
    comment: str | None
    created_at: datetime
    resolved_at: datetime | None

    @classmethod
    def of(cls, a: Appeal) -> "AppealOut":
        return cls(id=a.id, work_id=a.work_id, participant_id=a.participant_id, reason=a.reason,
                   status=a.status, old_score=a.old_score, new_score=a.new_score,
                   comment=a.comment, created_at=a.created_at, resolved_at=a.resolved_at)
