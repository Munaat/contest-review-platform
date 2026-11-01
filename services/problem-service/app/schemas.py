"""Схемы запросов и ответов HTTP API."""
import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.domain import Problem, ProblemStatus


class ProblemCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200, description="Название задачи")
    statement: str = Field(min_length=1, description="Условие задачи")
    author_id: uuid.UUID = Field(description="Автор задачи")


class CriterionIn(BaseModel):
    name: str = Field(min_length=1, max_length=200, description="Аспект решения")
    max_points: int = Field(ge=1, le=100, description="Максимальный балл за критерий")


class CriteriaSet(BaseModel):
    criteria: list[CriterionIn] = Field(min_length=1, max_length=10)


class CriterionOut(BaseModel):
    id: uuid.UUID
    name: str
    max_points: int


class ProblemOut(BaseModel):
    id: uuid.UUID
    title: str
    statement: str
    author_id: uuid.UUID
    status: ProblemStatus
    max_score: int
    criteria: list[CriterionOut]
    created_at: datetime
    published_at: datetime | None

    @classmethod
    def of(cls, p: Problem) -> "ProblemOut":
        return cls(id=p.id, title=p.title, statement=p.statement, author_id=p.author_id,
                   status=p.status, max_score=p.max_score,
                   criteria=[CriterionOut(id=c.id, name=c.name, max_points=c.max_points)
                             for c in p.criteria],
                   created_at=p.created_at, published_at=p.published_at)


class ProblemPage(BaseModel):
    items: list[ProblemOut]
    total: int
    offset: int
    count: int
