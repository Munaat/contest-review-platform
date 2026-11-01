"""Доменная модель контекста задач: агрегат «Задача» с критериями оценивания.

Инварианты агрегата:
  * у задачи от 1 до 10 критериев с уникальными названиями;
  * максимальный балл критерия — от 1 до 100;
  * опубликовать можно только задачу с заданными критериями;
  * опубликованная задача не изменяется: по ней уже проверяют решения.
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

MAX_CRITERIA = 10
MAX_POINTS = 100


class DomainError(Exception):
    """Нарушение бизнес-правила (HTTP 409)."""


class InvalidInput(DomainError):
    """Некорректные данные (HTTP 422)."""


class NotFound(Exception):
    """Объект не найден (HTTP 404)."""


class ProblemStatus(str, enum.Enum):
    DRAFT = "draft"
    PUBLISHED = "published"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Criterion:
    id: uuid.UUID
    name: str
    max_points: int
    position: int


@dataclass
class Problem:
    id: uuid.UUID
    title: str
    statement: str
    author_id: uuid.UUID
    status: ProblemStatus = ProblemStatus.DRAFT
    criteria: list[Criterion] = field(default_factory=list)
    created_at: datetime = field(default_factory=utcnow)
    published_at: datetime | None = None
    version: int = 0

    @classmethod
    def create(cls, title: str, statement: str, author_id: uuid.UUID) -> "Problem":
        title, statement = title.strip(), statement.strip()
        if not title:
            raise InvalidInput("title must not be empty")
        if not statement:
            raise InvalidInput("statement must not be empty")
        return cls(id=uuid.uuid4(), title=title, statement=statement, author_id=author_id)

    @property
    def max_score(self) -> int:
        return sum(c.max_points for c in self.criteria)

    def set_criteria(self, items: list[tuple[str, int]]) -> None:
        """Задать критерии оценивания (заменяет прежний набор)."""
        if self.status is ProblemStatus.PUBLISHED:
            raise DomainError("published problem cannot be changed")
        if not 1 <= len(items) <= MAX_CRITERIA:
            raise InvalidInput(f"problem must have from 1 to {MAX_CRITERIA} criteria")
        names = [name.strip() for name, _ in items]
        if any(not n for n in names):
            raise InvalidInput("criterion name must not be empty")
        if len({n.lower() for n in names}) != len(names):
            raise InvalidInput("criterion names must be unique")
        for _, points in items:
            if not 1 <= points <= MAX_POINTS:
                raise InvalidInput(f"criterion max_points must be from 1 to {MAX_POINTS}")
        self.criteria = [
            Criterion(id=uuid.uuid4(), name=name, max_points=points, position=i)
            for i, (name, (_, points)) in enumerate(zip(names, items))
        ]

    def publish(self, now: datetime | None = None) -> None:
        if self.status is ProblemStatus.PUBLISHED:
            raise DomainError("problem is already published")
        if not self.criteria:
            raise DomainError("criteria must be set before publishing")
        self.status = ProblemStatus.PUBLISHED
        self.published_at = now or utcnow()
